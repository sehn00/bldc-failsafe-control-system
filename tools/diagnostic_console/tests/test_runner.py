"""The trial state machine, driven by synthetic telemetry only.

No hardware and no SSH: the runner's ssh factory is replaced, so every path
here is deterministic.  Nothing in this file invents a measured delay for the
real bench -- the timings are inputs chosen by the test.
"""

import unittest
from datetime import timedelta

from PySide6.QtCore import QCoreApplication, QObject, Signal

from bldc_diagnostic_console.data_store import DataStore
from bldc_diagnostic_console.verification.runner import (
    IDLE,
    INJECTING,
    WATCHING,
    FaultTestRunner,
)
from bldc_diagnostic_console.verification.ssh import SshResult, SshTarget
from bldc_diagnostic_console.verification.trial import TrialLog

TARGET = SshTarget("pi-bench", "pi")
OK = SshResult("kill", 0)
DENIED = SshResult("kill", 1, stderr="sudo: a password is required")

READY, RUN, FAULT = 1, 2, 3
NONE, COMM, OVERCURRENT = 0, 3, 1


def setUpModule() -> None:
    QCoreApplication.instance() or QCoreApplication([])


class FakeSsh(QObject):
    """Stands in for SshCommand; emits its canned result on start()."""

    completed = Signal(object)

    def __init__(self, target, command, result) -> None:
        super().__init__()
        self.target, self.command, self.result = target, command, result

    def start(self) -> None:
        self.completed.emit(self.result)

    def isRunning(self) -> bool:
        return False


class RunnerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.log = TrialLog()
        self.store = DataStore()
        self.ssh_result = OK
        self.runner = FaultTestRunner(
            self.log, ssh_factory=lambda t, c: FakeSsh(t, c, self.ssh_result)
        )
        self.aborts: list[str] = []
        self.runner.aborted.connect(self.aborts.append)

    # --- helpers -----------------------------------------------------------

    def _sample(self, state, fault, offset_ms=0.0) -> None:
        """Append one telemetry row, timestamped relative to the injection."""
        base = self.runner._injection
        wall = None if base is None else base + timedelta(milliseconds=offset_ms)
        self.store.add_row({"STATE": state, "FAULT": fault, "RPM": 0}, wall=wall)

    def _running(self) -> None:
        self.store.add_row({"STATE": RUN, "FAULT": NONE, "RPM": 380})

    def _start(self, **kw) -> bool:
        return self.runner.start(TARGET, "kill", self.store, True, **kw)

    # --- preconditions -----------------------------------------------------

    def test_blocked_when_serial_disconnected(self) -> None:
        self._running()
        self.assertEqual(
            self.runner.preconditions(self.store, False),
            "serial telemetry is not connected",
        )

    def test_blocked_before_any_sample(self) -> None:
        self.assertIn("no telemetry", self.runner.preconditions(self.store, True))

    def test_blocked_when_not_running_and_says_what_it_saw(self) -> None:
        self.store.add_row({"STATE": 1, "FAULT": NONE})  # READY
        reason = self.runner.preconditions(self.store, True)
        self.assertIn("STATE must be RUN", reason)
        self.assertIn("READY", reason)

    def test_blocked_when_a_fault_is_already_latched(self) -> None:
        self.store.add_row({"STATE": RUN, "FAULT": OVERCURRENT})
        reason = self.runner.preconditions(self.store, True)
        self.assertIn("FAULT must be NONE", reason)
        self.assertIn("OVERCURRENT", reason)

    def test_start_refused_records_no_trial(self) -> None:
        self.assertFalse(self._start())
        self.assertEqual(len(self.log), 0)
        self.assertEqual(self.runner.phase, IDLE)
        self.assertEqual(len(self.aborts), 1)

    def test_ready_state_passes_preconditions(self) -> None:
        self._running()
        self.assertIsNone(self.runner.preconditions(self.store, True))

    # --- the nominal trial -------------------------------------------------

    def test_pass_when_comm_fault_latches_and_stays(self) -> None:
        self._running()
        self.assertTrue(self._start(detect_timeout=5.0, watch_window=3.0))
        self.assertEqual(self.runner.phase, INJECTING)

        self._sample(FAULT, COMM, offset_ms=520.0)
        self.runner.consume(self.store)
        self.assertEqual(self.runner.phase, WATCHING)

        for offset in (1000.0, 2000.0, 3000.0):   # stays latched
            self._sample(FAULT, COMM, offset_ms=offset)
        self.runner.consume(self.store)
        self.runner._watch_elapsed()              # the window closes

        self.assertEqual(self.runner.phase, IDLE)
        trial = self.log.trials[0]
        self.assertTrue(trial.passed)
        self.assertEqual((trial.final_state, trial.final_fault), ("FAULT", "COMM"))
        self.assertFalse(trial.auto_run_detected)
        self.assertAlmostEqual(trial.delay_ms, 520.0, places=3)
        self.assertEqual(trial.note, "")

    def test_delay_is_measured_from_injection_to_the_observing_sample(self) -> None:
        self._running()
        self._start(detect_timeout=5.0, watch_window=1.0)
        self._sample(FAULT, COMM, offset_ms=1234.5)
        self.runner.consume(self.store)
        self.runner._watch_elapsed()
        self.assertAlmostEqual(self.log.trials[0].delay_ms, 1234.5, places=3)

    def test_pre_injection_samples_cannot_satisfy_the_transition(self) -> None:
        """A fault already on screen must not be scored as this trial's."""
        self._running()
        self._start(detect_timeout=5.0)
        self.runner.consume(self.store)
        self.assertEqual(self.runner.phase, INJECTING)

    # --- failures ----------------------------------------------------------

    def test_auto_run_during_the_watch_window_fails(self) -> None:
        self._running()
        self._start(detect_timeout=5.0, watch_window=10.0)
        self._sample(FAULT, COMM, offset_ms=500.0)
        self.runner.consume(self.store)

        self._sample(RUN, NONE, offset_ms=4000.0)   # systemd restarted it
        self.runner.consume(self.store)

        self.assertEqual(self.runner.phase, IDLE)
        trial = self.log.trials[0]
        self.assertTrue(trial.auto_run_detected)
        self.assertFalse(trial.passed)
        self.assertEqual(trial.result, "FAIL")
        self.assertIn("returned to RUN", trial.note)
        self.assertIsNotNone(trial.delay_ms)  # the transition was still timed

    def test_manual_recovery_during_the_watch_window_fails_without_auto_run(
        self,
    ) -> None:
        """READY/NONE is a person clearing the latch, not the board restarting."""
        self._running()
        self._start(detect_timeout=5.0, watch_window=10.0)
        self._sample(FAULT, COMM, offset_ms=500.0)
        self.runner.consume(self.store)

        self._sample(READY, NONE, offset_ms=3000.0)   # operator ran CLEAR_FAULT
        self.runner.consume(self.store)

        self.assertEqual(self.runner.phase, IDLE)
        trial = self.log.trials[0]
        self.assertFalse(trial.passed)
        self.assertFalse(trial.auto_run_detected)     # it never went to RUN
        self.assertEqual((trial.final_state, trial.final_fault), ("READY", "NONE"))
        self.assertIn("Fault latch cleared during watch window", trial.note)
        self.assertIn("2500 ms", trial.note)          # held only 2.5 s of 10
        self.assertIsNotNone(trial.delay_ms)          # the transition was timed

    def test_fault_changing_off_comm_during_the_window_fails(self) -> None:
        """Still faulted, but no longer the latch the kill produced."""
        self._running()
        self._start(detect_timeout=5.0, watch_window=10.0)
        self._sample(FAULT, COMM, offset_ms=500.0)
        self.runner.consume(self.store)

        self._sample(FAULT, OVERCURRENT, offset_ms=2000.0)
        self.runner.consume(self.store)

        trial = self.log.trials[0]
        self.assertFalse(trial.passed)
        self.assertFalse(trial.auto_run_detected)
        self.assertIn("did not hold", trial.note)
        self.assertIn("OVERCURRENT", trial.note)

    def test_latch_must_hold_to_the_end_of_the_window(self) -> None:
        """Leaving FAULT/COMM fails even if the board is back in it at the end."""
        self._running()
        self._start(detect_timeout=5.0, watch_window=10.0)
        self._sample(FAULT, COMM, offset_ms=500.0)
        self.runner.consume(self.store)

        self._sample(READY, NONE, offset_ms=1000.0)
        self._sample(FAULT, COMM, offset_ms=1500.0)
        self.runner.consume(self.store)
        self.runner._watch_elapsed()                  # a no-op: already finished

        self.assertEqual(len(self.log), 1)
        self.assertFalse(self.log.trials[0].passed)

    def test_timeout_without_the_transition_fails_with_no_delay(self) -> None:
        self._running()
        self._start(detect_timeout=2.0)
        self.runner._detect_timeout()

        trial = self.log.trials[0]
        self.assertFalse(trial.passed)
        self.assertIsNone(trial.delay_ms)
        self.assertIsNone(trial.fault_observed_time)
        self.assertIn("no STATE=FAULT/FAULT=COMM", trial.note)
        self.assertEqual(self.log.delays(), [])  # excluded from the statistics

    def test_a_different_fault_is_reported_in_the_note(self) -> None:
        self._running()
        self._start(detect_timeout=2.0)
        self._sample(FAULT, OVERCURRENT, offset_ms=400.0)
        self.runner.consume(self.store)
        self.assertEqual(self.runner.phase, INJECTING)  # still not COMM
        self.runner._detect_timeout()
        self.assertIn("OVERCURRENT", self.log.trials[0].note)

    def test_ssh_failure_aborts_without_recording_a_trial(self) -> None:
        self.ssh_result = DENIED
        self._running()
        self._start()
        self.assertEqual(len(self.log), 0)   # the injection never happened
        self.assertEqual(self.runner.phase, IDLE)
        self.assertIn("fault injection failed", self.aborts[0])
        self.assertIn("password is required", self.aborts[0])

    def test_serial_disconnect_mid_trial_drops_the_trial(self) -> None:
        self._running()
        self._start()
        self.runner.abort("serial telemetry disconnected during the trial")
        self.assertEqual(len(self.log), 0)
        self.assertEqual(self.runner.phase, IDLE)
        self.assertIn("disconnected", self.aborts[0])

    def test_second_trial_refused_while_one_is_running(self) -> None:
        self._running()
        self._start()
        self.assertFalse(self._start())
        self.assertIn("already running", self.aborts[-1])

    def test_store_reset_mid_trial_does_not_crash(self) -> None:
        self._running()
        self._start(detect_timeout=5.0)
        injection = self.runner._injection
        self.store.reset()
        self.runner.consume(self.store)          # cursor must clamp
        self.store.add_row({"STATE": FAULT, "FAULT": COMM},
                           wall=injection + timedelta(milliseconds=600))
        self.runner.consume(self.store)
        self.assertEqual(self.runner.phase, WATCHING)

    # --- campaign ----------------------------------------------------------

    def test_trials_accumulate_and_number_sequentially(self) -> None:
        for i in range(3):
            self._running()
            self._start(detect_timeout=5.0, watch_window=1.0)
            self._sample(FAULT, COMM, offset_ms=500.0 + 10 * i)
            self.runner.consume(self.store)
            self.runner._watch_elapsed()
            self._running()   # operator recovers manually before the next one

        self.assertEqual([t.index for t in self.log.trials], [1, 2, 3])
        self.assertTrue(all(t.passed for t in self.log.trials))
        self.assertEqual(self.log.delays(), [500.0, 510.0, 520.0])
        self.assertEqual(self.log.pass_rate(), 1.0)

    def test_runner_never_commands_the_motor(self) -> None:
        """Only the configured kill is ever sent -- no CLEAR_FAULT/ENABLE/RUN."""
        sent = []
        self.runner._ssh_factory = lambda t, c: (sent.append(c), FakeSsh(t, c, OK))[1]
        self._running()
        self._start(detect_timeout=5.0, watch_window=1.0)
        self._sample(FAULT, COMM, offset_ms=500.0)
        self.runner.consume(self.store)
        self.runner._watch_elapsed()
        self.assertEqual(sent, ["kill"])


if __name__ == "__main__":
    unittest.main()
