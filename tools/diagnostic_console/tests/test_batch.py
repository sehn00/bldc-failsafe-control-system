"""The batch state machine, driven by synthetic telemetry and canned SSH.

No hardware and no SSH: every remote command resolves to a result this file
chose, and the timers are fired by hand, so the whole sequence is
deterministic.  Nothing here invents a measured delay for the real bench.
"""

import unittest
from datetime import datetime, timedelta

from PySide6.QtCore import QCoreApplication, QEventLoop, QObject, QTimer, Signal

from bldc_diagnostic_console.data_store import DataStore
from bldc_diagnostic_console.verification.batch import (
    AWAIT_READY,
    AWAIT_RUN,
    AWAIT_SUPERVISOR,
    BATCH_IDLE,
    SETTLING,
    TARGETING,
    TRIAL,
    BatchRunner,
)
from bldc_diagnostic_console.verification.config import (
    MOTORCTL_CLEAR_FAULT,
    MOTORCTL_ENABLE,
    MOTORCTL_TARGET,
    SUPERVISOR_READY_COMMAND,
    SUPERVISOR_SOCKET,
)
from bldc_diagnostic_console.verification.runner import FaultTestRunner
from bldc_diagnostic_console.verification.ssh import SshResult, SshTarget
from bldc_diagnostic_console.verification.trial import TrialLog

TARGET = SshTarget("pi-bench", "pi")
KILL = "pkill -KILL -f motor-supervisor"

READY, RUN, FAULT = 1, 2, 3
NONE, COMM = 0, 3

#: The supervisor answers `motorctl status` with a state line; a readiness
#: probe that lacks one does not count, so the fake has to produce a real one.
def ready(command=SUPERVISOR_READY_COMMAND, state="READY", fault="NONE"):
    return SshResult(command, 0, stdout=f"OK STATE={state} MODE=REMOTE FAULT={fault}")


def not_ready(command=SUPERVISOR_READY_COMMAND, stderr="motorctl: connection refused"):
    return SshResult(command, 1, stderr=stderr)


#: Probes sent before one setup command, at the default confirmation count.
PROBES = [SUPERVISOR_READY_COMMAND] * 2


def setUpModule() -> None:
    QCoreApplication.instance() or QCoreApplication([])


class FakeSsh(QObject):
    """Emits a canned result on start(); records what was asked of it."""

    completed = Signal(object)

    def __init__(self, target, command, sent, results, on_start=None) -> None:
        super().__init__()
        self.command = command
        self._sent = sent
        self._results = results
        self._on_start = on_start

    def start(self) -> None:
        self._sent.append(self.command)
        if self._on_start is not None:
            self._on_start(self.command)
        self.completed.emit(self._next_result())

    def _next_result(self):
        """A canned result, or the next of a queue that ends by repeating."""
        canned = self._results.get(self.command)
        if canned is None:
            if self.command == SUPERVISOR_READY_COMMAND:
                return ready(self.command)
            return SshResult(self.command, 0)
        if isinstance(canned, list):
            return canned.pop(0) if len(canned) > 1 else canned[0]
        return canned

    def isRunning(self) -> bool:
        return False


class BatchTest(unittest.TestCase):
    def setUp(self) -> None:
        self.log = TrialLog()
        self.store = DataStore()
        self.sent: list[str] = []
        self.results: dict[str, SshResult] = {}
        # Both objects send over SSH -- the batch the motorctl steps, the
        # runner the kill -- so both get the same fake, and `sent` is the whole
        # conversation with the Pi in order.
        fake = lambda t, c: FakeSsh(t, c, self.sent, self.results)  # noqa: E731
        self.runner = FaultTestRunner(self.log, ssh_factory=fake)
        self.batch = BatchRunner(self.runner, ssh_factory=fake)
        self.finished: list[tuple[bool, str]] = []
        self.progress: list[tuple[int, int]] = []
        self.batch.finished.connect(lambda ok, msg: self.finished.append((ok, msg)))
        self.batch.progress.connect(lambda i, n: self.progress.append((i, n)))

    # --- helpers -----------------------------------------------------------

    def _start(self, count=1, **kw) -> bool:
        self.store.add_row({"STATE": FAULT, "FAULT": COMM})
        return self.batch.start(
            TARGET, KILL, count, self.store, True,
            detect_timeout=5.0, watch_window=10.0, confirm_timeout=8.0,
            settle=2.0, ready_timeout=20.0, poll=1.0, **kw
        )

    def _row(self, state, fault, wall=None) -> None:
        self.store.add_row({"STATE": state, "FAULT": fault, "RPM": 0}, wall=wall)
        self.batch.consume(self.store)
        self.runner.consume(self.store)

    def _through_setup(self) -> None:
        """clear-fault -> READY -> enable -> target -> RUN -> settle -> kill."""
        self._row(READY, NONE)
        self.assertEqual(self.batch.phase, AWAIT_RUN)
        self._row(RUN, NONE)
        self.assertEqual(self.batch.phase, SETTLING)
        self.batch._settled()

    def _latch_comm(self, delay_ms=520.0) -> None:
        injection = self.runner._injection
        self._row(FAULT, COMM, wall=injection + timedelta(milliseconds=delay_ms))

    # --- the nominal sequence ----------------------------------------------

    def test_one_trial_runs_the_manual_sequence_in_order(self) -> None:
        self.assertTrue(self._start(count=1))
        # The supervisor is asked whether it is usable before anything is sent.
        self.assertEqual(self.sent, PROBES + [MOTORCTL_CLEAR_FAULT])
        self.assertEqual(self.batch.phase, AWAIT_READY)

        self._row(READY, NONE)
        self.assertEqual(self.sent[-2:], [MOTORCTL_ENABLE, MOTORCTL_TARGET])
        self.assertEqual(self.batch.phase, AWAIT_RUN)

        self._row(RUN, NONE)
        self.assertEqual(self.batch.phase, SETTLING)   # settles before the kill
        self.assertNotIn(KILL, self.sent)              # no kill yet

        self.batch._settled()
        self.assertEqual(self.batch.phase, TRIAL)
        self.assertEqual(self.sent[-1], KILL)

        self._latch_comm(delay_ms=520.0)
        for offset in (1000.0, 5000.0):                # stays latched
            self._row(FAULT, COMM)
        self.runner._watch_elapsed()

        self.assertEqual(self.batch.phase, BATCH_IDLE)
        self.assertEqual(len(self.log), 1)
        trial = self.log.trials[0]
        self.assertTrue(trial.passed)
        self.assertAlmostEqual(trial.delay_ms, 520.0, places=3)
        self.assertEqual(trial.note, "")                # a clean setup adds none
        self.assertEqual(self.finished, [(True, "Batch complete: 1 trials run.")])
        self.assertEqual(self.progress, [(1, 1)])

    def test_the_next_clear_fault_waits_for_the_watch_window(self) -> None:
        self._start(count=2)
        self._through_setup()
        self._latch_comm()

        # Still inside the watch window: nothing new may be sent.
        self._row(FAULT, COMM)
        self.assertEqual(self.sent.count(MOTORCTL_CLEAR_FAULT), 1)
        self.assertEqual(self.batch.phase, TRIAL)

        self.runner._watch_elapsed()                   # the window closes
        self.assertEqual(self.sent.count(MOTORCTL_CLEAR_FAULT), 2)
        self.assertEqual(self.batch.phase, AWAIT_READY)
        self.assertEqual(self.progress, [(1, 2), (2, 2)])
        # ...and the readiness probe ran again just before it.
        self.assertEqual(self.sent.count(SUPERVISOR_READY_COMMAND), 4)
        self.assertEqual(self.sent[-3:], PROBES + [MOTORCTL_CLEAR_FAULT])

    def test_a_failed_trial_does_not_stop_the_batch(self) -> None:
        """Auto-run is a result, not a bench fault, so the campaign continues."""
        self._start(count=2)
        self._through_setup()
        self._latch_comm()
        self._row(RUN, NONE)                           # systemd restarted it

        trial = self.log.trials[0]
        self.assertFalse(trial.passed)
        self.assertTrue(trial.auto_run_detected)
        self.assertEqual(self.batch.phase, AWAIT_READY)   # trial 2 is under way
        self.assertEqual(self.finished, [])

    # --- stopping ----------------------------------------------------------

    def test_stop_during_a_trial_finishes_it_then_stops(self) -> None:
        self._start(count=5)
        self._through_setup()
        self._latch_comm()

        self.batch.stop()
        self.assertEqual(self.batch.phase, TRIAL)      # the window is not cut short
        self.runner._watch_elapsed()

        self.assertEqual(self.batch.phase, BATCH_IDLE)
        self.assertEqual(len(self.log), 1)             # the trial was still recorded
        self.assertTrue(self.log.trials[0].passed)
        self.assertEqual(self.sent.count(MOTORCTL_CLEAR_FAULT), 1)  # no new trial
        ok, message = self.finished[0]
        self.assertTrue(ok)
        self.assertIn("stopped after 1 trials", message)

    def test_stop_while_waiting_for_a_state_is_not_reported_as_a_timeout(
        self,
    ) -> None:
        self._start(count=3)
        self.assertEqual(self.batch.phase, AWAIT_READY)

        self.batch.stop()
        self.assertEqual(self.batch.phase, BATCH_IDLE)
        ok, message = self.finished[0]
        self.assertTrue(ok)
        self.assertNotIn("never arrived", message)

    def test_stop_while_waiting_for_the_supervisor_starts_no_further_trial(
        self,
    ) -> None:
        self.results[SUPERVISOR_READY_COMMAND] = (
            [ready()] * 2                        # trial 1 is fine
            + [not_ready()]                      # then not back yet
        )
        self._start(count=3)
        self._through_setup()
        self._latch_comm()
        self.runner._watch_elapsed()
        self.assertEqual(self.batch.phase, AWAIT_SUPERVISOR)

        self.batch.stop()
        self.assertEqual(self.batch.phase, BATCH_IDLE)
        self.assertEqual(self.sent.count(MOTORCTL_CLEAR_FAULT), 1)
        self.assertIn("stopped after 1 trials", self.finished[0][1])

    def test_stop_during_setup_warns_that_the_motor_may_run(self) -> None:
        self._start(count=3)
        self._row(READY, NONE)
        self._row(RUN, NONE)
        self.assertEqual(self.batch.phase, SETTLING)

        self.batch.stop()                              # only a timer was pending
        self.assertEqual(self.batch.phase, BATCH_IDLE)
        self.batch._settled()                          # a late timer changes nothing

        self.assertEqual(self.batch.phase, BATCH_IDLE)
        self.assertNotIn(KILL, self.sent)
        self.assertEqual(len(self.log), 0)
        ok, message = self.finished[0]
        self.assertTrue(ok)
        self.assertIn("motor may still be running", message)

    # --- motorctl exit codes vs. the telemetry post-condition --------------

    def test_enable_get_state_timeout_is_settled_by_telemetry(self) -> None:
        """Observed on the bench: the command lands, only its check times out."""
        self.results[MOTORCTL_ENABLE] = SshResult(
            MOTORCTL_ENABLE, 1, stderr="ERROR GET_STATE timed out"
        )
        self._start(count=1)
        self._row(READY, NONE)

        # Not treated as a failure: the sequence carried on to the target.
        self.assertEqual(self.sent[-1], MOTORCTL_TARGET)
        self.assertEqual(self.batch.phase, AWAIT_RUN)
        self.assertEqual(self.finished, [])

        self._row(RUN, NONE)                       # telemetry settles it
        self.assertEqual(self.batch.phase, SETTLING)

    def test_target_ack_timeout_is_settled_by_telemetry(self) -> None:
        self.results[MOTORCTL_TARGET] = SshResult(
            MOTORCTL_TARGET, 1,
            stdout="WARN TARGET_SENT_NO_ACK STATE=RUN MODE=REMOTE FAULT=NONE",
        )
        self._start(count=1)
        self._row(READY, NONE)
        self.assertEqual(self.batch.phase, AWAIT_RUN)

        self._row(RUN, NONE)
        self.assertEqual(self.batch.phase, SETTLING)
        self.assertEqual(self.finished, [])

    def test_unconfirmed_steps_are_recorded_in_the_trial_note(self) -> None:
        self.results[MOTORCTL_ENABLE] = SshResult(
            MOTORCTL_ENABLE, 1, stderr="ERROR GET_STATE timed out"
        )
        self.results[MOTORCTL_TARGET] = SshResult(
            MOTORCTL_TARGET, 1, stdout="WARN TARGET_SENT_NO_ACK STATE=RUN",
        )
        self._start(count=1)
        self._through_setup()
        self._latch_comm()
        self.runner._watch_elapsed()

        trial = self.log.trials[0]
        self.assertTrue(trial.passed)          # a shaky setup is not a FAIL
        self.assertIn("setup:", trial.note)
        self.assertIn("GET_STATE timed out", trial.note)
        self.assertIn("TARGET_SENT_NO_ACK", trial.note)

    def test_an_unconfirmed_step_still_aborts_if_telemetry_disagrees(self) -> None:
        """The post-condition is the verdict, so it must be able to say no."""
        self.results[MOTORCTL_ENABLE] = SshResult(
            MOTORCTL_ENABLE, 1, stderr="ERROR GET_STATE timed out"
        )
        self.results[MOTORCTL_TARGET] = SshResult(
            MOTORCTL_TARGET, 1, stdout="WARN TARGET_SENT_NO_ACK"
        )
        self._start(count=1)
        self._row(READY, NONE)
        self._row(READY, NONE)                 # RUN never comes
        self.batch._confirm_expired()

        self.assertEqual(self.batch.phase, BATCH_IDLE)
        self.assertNotIn(KILL, self.sent)
        self.assertEqual(len(self.log), 0)
        ok, message = self.finished[0]
        self.assertFalse(ok)
        self.assertIn("STATE=RUN / FAULT=NONE never arrived", message)

    def test_enable_not_reaching_run_continues_to_the_target(self) -> None:
        """The bench case that stopped trial 6 of a 100-trial batch."""
        self.results[MOTORCTL_ENABLE] = SshResult(
            MOTORCTL_ENABLE, 1,
            stderr="ERROR ENABLE did not result in RUN;\n"
                   "   STATE=READY MODE=REMOTE FAULT=NONE",
        )
        self._start(count=1)
        self._row(READY, NONE)

        self.assertEqual(self.sent[-1], MOTORCTL_TARGET)   # not an abort
        self.assertEqual(self.batch.phase, AWAIT_RUN)
        self.assertEqual(self.finished, [])

        self._row(RUN, NONE)                               # TARGET brings RUN
        self.assertEqual(self.batch.phase, SETTLING)

    def test_enable_not_reaching_run_still_records_the_trial_note(self) -> None:
        self.results[MOTORCTL_ENABLE] = SshResult(
            MOTORCTL_ENABLE, 1,
            stderr="ERROR ENABLE did not result in RUN; "
                   "STATE=READY MODE=REMOTE FAULT=NONE",
        )
        self._start(count=1)
        self._through_setup()
        self._latch_comm()
        self.runner._watch_elapsed()

        trial = self.log.trials[0]
        self.assertTrue(trial.passed)
        self.assertIn("setup: motorctl enable unconfirmed", trial.note)
        self.assertIn("ENABLE did not result in RUN", trial.note)

    def test_enable_not_reaching_run_aborts_if_run_never_follows(self) -> None:
        self.results[MOTORCTL_ENABLE] = SshResult(
            MOTORCTL_ENABLE, 1,
            stderr="ERROR ENABLE did not result in RUN; "
                   "STATE=READY MODE=REMOTE FAULT=NONE",
        )
        self._start(count=1)
        self._row(READY, NONE)
        self._row(READY, NONE)                             # RUN never comes
        self.batch._confirm_expired()

        self.assertEqual(self.batch.phase, BATCH_IDLE)
        self.assertNotIn(KILL, self.sent)
        self.assertEqual(len(self.log), 0)
        self.assertIn("STATE=RUN / FAULT=NONE never arrived", self.finished[0][1])

    def test_the_same_message_from_target_is_not_forgiven(self) -> None:
        self.results[MOTORCTL_TARGET] = SshResult(
            MOTORCTL_TARGET, 1,
            stderr="ERROR ENABLE did not result in RUN; "
                   "STATE=READY MODE=REMOTE FAULT=NONE",
        )
        self._start(count=1)
        self._row(READY, NONE)

        self.assertEqual(self.batch.phase, BATCH_IDLE)
        self.assertFalse(self.finished[0][0])
        self.assertIn(MOTORCTL_TARGET, self.finished[0][1])

    def test_command_not_found_aborts_even_though_it_is_non_zero(self) -> None:
        self.results[MOTORCTL_ENABLE] = SshResult(
            MOTORCTL_ENABLE, 127, stderr="sh: motorctl: command not found"
        )
        self._start(count=1)
        self._row(READY, NONE)

        self.assertEqual(self.batch.phase, BATCH_IDLE)
        self.assertNotIn(MOTORCTL_TARGET, self.sent)
        self.assertIn("command not found", self.finished[0][1])

    def test_a_missing_supervisor_socket_aborts(self) -> None:
        self.results[MOTORCTL_ENABLE] = SshResult(
            MOTORCTL_ENABLE, 1,
            stderr=f"motorctl: connect {SUPERVISOR_SOCKET}: No such file or directory",
        )
        self._start(count=1)
        self._row(READY, NONE)

        self.assertEqual(self.batch.phase, BATCH_IDLE)
        self.assertNotIn(MOTORCTL_TARGET, self.sent)
        self.assertIn("No such file", self.finished[0][1])

    # --- the supervisor sync guard -----------------------------------------
    #
    # motor-supervisor.c answers this and returns *before* write_uart_frame,
    # so the command provably never reached the STM32.  It is never read as
    # applied: either telemetry already shows the state the step wanted, or the
    # command has to go again once the supervisor is back in step.

    def _desync(self, command):
        return SshResult(
            command, 1, stderr="ERROR supervisor has not synchronized STM32 state"
        )

    def test_a_desync_with_the_state_already_reached_is_not_re_sent(self) -> None:
        """A mutating command the board no longer needs must not go again.

        Driven at the handler, because the guard fires before the frame is
        written: in the normal procedure RUN can only come *from* the TARGET,
        so telemetry cannot reach it on its own while the reply is in flight.
        """
        self._start(count=1)
        self._row(READY, NONE)                        # -> enable -> target
        self.assertEqual(self.batch.phase, AWAIT_RUN)
        self.store.add_row({"STATE": RUN, "FAULT": NONE, "RPM": 0})
        sent_before = list(self.sent)

        self.batch._set_phase(TARGETING)              # as the reply lands
        self.batch._on_desync(MOTORCTL_TARGET, "exit 1: ERROR supervisor …")

        self.assertEqual(self.sent, sent_before)      # not re-sent
        self.assertEqual(self.batch.phase, AWAIT_RUN)
        self.assertEqual(self.batch.retries, 0)
        self.assertEqual(self.batch.resyncs, 1)
        self.assertIn(
            f"{MOTORCTL_TARGET} unconfirmed due to supervisor resync",
            self.batch._setup_note(),
        )

    def test_target_desync_resynchronizes_then_retries_once(self) -> None:
        self.results[MOTORCTL_TARGET] = [
            self._desync(MOTORCTL_TARGET),            # refused the first time
            SshResult(MOTORCTL_TARGET, 0),            # accepted after the resync
        ]
        self._start(count=1)
        self._row(READY, NONE)                        # still READY, not RUN

        # Refused: it re-gated on the supervisor and only then sent it again.
        self.assertEqual(self.batch.phase, AWAIT_RUN)
        self.assertEqual(self.sent.count(MOTORCTL_TARGET), 2)
        self.assertEqual(self.batch.retries, 1)
        self.assertEqual(self.batch.resyncs, 1)

        self._row(RUN, NONE)
        self.assertEqual(self.batch.phase, SETTLING)
        self.assertEqual(self.finished, [])

        self.batch._settled()
        self._latch_comm(delay_ms=610.0)
        self.runner._watch_elapsed()

        trial = self.log.trials[0]
        self.assertTrue(trial.passed)                 # a resync is not a FAIL
        self.assertIn("retried once after supervisor sync loss", trial.note)
        self.assertAlmostEqual(trial.delay_ms, 610.0, places=3)

    def test_the_same_desync_after_a_retry_stops_the_batch(self) -> None:
        self.results[MOTORCTL_TARGET] = self._desync(MOTORCTL_TARGET)
        self._start(count=1)
        self._row(READY, NONE)

        self.assertEqual(self.batch.phase, BATCH_IDLE)
        self.assertEqual(self.sent.count(MOTORCTL_TARGET), 2)   # never a third
        self.assertEqual(len(self.log), 0)
        ok, message = self.finished[0]
        self.assertFalse(ok)
        self.assertIn("again after a resync", message)
        self.assertIn("motor may still be running", message)

    def test_a_desync_waits_for_the_supervisor_before_re_sending(self) -> None:
        """Readiness that succeeded once can lapse; do not press on blind."""
        self.results[MOTORCTL_TARGET] = [
            self._desync(MOTORCTL_TARGET), SshResult(MOTORCTL_TARGET, 0),
        ]
        self.results[SUPERVISOR_READY_COMMAND] = (
            [ready()] * 2                    # the gate before clear-fault
            + [not_ready()]                  # then it lapses
            + [ready()] * 2                  # and comes back
        )
        self._start(count=1)
        self._row(READY, NONE)

        # Not confirmed yet, so TARGET has not gone out a second time.
        self.assertEqual(self.batch.phase, AWAIT_SUPERVISOR)
        self.assertEqual(self.sent.count(MOTORCTL_TARGET), 1)

        self.batch._probe_supervisor()                # the poll timer fires
        self.assertEqual(self.sent.count(MOTORCTL_TARGET), 2)
        self.assertEqual(self.batch.phase, AWAIT_RUN)

    def test_a_supervisor_that_never_resynchronizes_stops_the_batch(self) -> None:
        self.results[MOTORCTL_TARGET] = self._desync(MOTORCTL_TARGET)
        self.results[SUPERVISOR_READY_COMMAND] = (
            [ready()] * 2 + [not_ready(stderr="still stale")]
        )
        self._start(count=1)
        self._row(READY, NONE)
        self.assertEqual(self.batch.phase, AWAIT_SUPERVISOR)

        self.batch._ready_expired()

        self.assertEqual(self.batch.phase, BATCH_IDLE)
        self.assertEqual(len(self.log), 0)
        self.assertIn("not in step with the STM32 within", self.finished[0][1])
        self.assertIn("still stale", self.finished[0][1])

    def test_a_desynced_clear_fault_is_retried_not_assumed(self) -> None:
        self.results[MOTORCTL_CLEAR_FAULT] = [
            self._desync(MOTORCTL_CLEAR_FAULT), SshResult(MOTORCTL_CLEAR_FAULT, 0),
        ]
        self._start(count=1)                          # board is FAULT/COMM

        self.assertEqual(self.sent.count(MOTORCTL_CLEAR_FAULT), 2)
        self.assertEqual(self.batch.phase, AWAIT_READY)
        self.assertEqual(self.batch.retries, 1)

    def test_the_kill_is_never_retried_after_a_desync(self) -> None:
        """Setup recovery stops at the injection; the trial itself never repeats."""
        self.results[KILL] = self._desync(KILL)
        self._start(count=1)
        self._through_setup()

        self.assertEqual(self.sent.count(KILL), 1)
        self.assertEqual(self.batch.retries, 0)
        # The runner owns the kill: a refused injection is dropped, not redone.
        self.assertEqual(len(self.log), 0)
        self.assertEqual(self.batch.phase, BATCH_IDLE)

    # --- supervisor readiness ----------------------------------------------

    def test_the_next_trial_waits_for_the_supervisor_to_come_back(self) -> None:
        self.results[SUPERVISOR_READY_COMMAND] = (
            [ready()] * 2                    # trial 1 starts at once
            + [not_ready()]                  # trial 2: not back yet
            + [ready()] * 2                  # back up on the next poll
        )
        self._start(count=2)
        self._through_setup()
        self._latch_comm(delay_ms=480.0)
        self.runner._watch_elapsed()

        # Not ready yet: no clear-fault, and the batch is still waiting.
        self.assertEqual(self.batch.phase, AWAIT_SUPERVISOR)
        self.assertEqual(self.sent.count(MOTORCTL_CLEAR_FAULT), 1)

        self.batch._probe_supervisor()                # the poll timer fires
        self.assertEqual(self.sent.count(MOTORCTL_CLEAR_FAULT), 2)
        self.assertEqual(self.batch.phase, AWAIT_READY)

        # The wait sits between trials, so it cannot touch a measured delay.
        self.assertAlmostEqual(self.log.trials[0].delay_ms, 480.0, places=3)

    def test_a_supervisor_that_never_returns_stops_the_batch(self) -> None:
        self.results[SUPERVISOR_READY_COMMAND] = (
            [ready()] * 2 + [not_ready(stderr="no socket")]
        )
        self._start(count=2)
        self._through_setup()
        self._latch_comm()
        self.runner._watch_elapsed()
        self.assertEqual(self.batch.phase, AWAIT_SUPERVISOR)

        self.batch._ready_expired()                   # the overall deadline

        self.assertEqual(self.batch.phase, BATCH_IDLE)
        self.assertEqual(self.sent.count(MOTORCTL_CLEAR_FAULT), 1)
        self.assertEqual(len(self.log), 1)            # trial 1 is kept
        ok, message = self.finished[0]
        self.assertFalse(ok)
        self.assertIn("not in step with the STM32 within", message)
        self.assertIn("no socket", message)

    def test_a_probe_that_cannot_reach_the_pi_stops_the_batch_at_once(self) -> None:
        self.results[SUPERVISOR_READY_COMMAND] = SshResult(
            SUPERVISOR_READY_COMMAND, None, error="ssh: connect to host closed"
        )
        self._start(count=2)

        self.assertEqual(self.batch.phase, BATCH_IDLE)
        self.assertEqual(self.sent, [SUPERVISOR_READY_COMMAND])
        self.assertIn("readiness probe could not run", self.finished[0][1])

    def test_a_slow_supervisor_is_noted_on_the_trial(self) -> None:
        self.results[SUPERVISOR_READY_COMMAND] = (
            [not_ready(stderr="not yet")] + [ready()] * 2
        )
        self._start(count=1)
        self.batch._probe_supervisor()          # the poll timer fires
        self._through_setup()
        self._latch_comm()
        self.runner._watch_elapsed()

        # One refusal then the two confirmations it takes to start.
        self.assertIn("supervisor ready after 3 probes", self.log.trials[0].note)

    # --- safety stops ------------------------------------------------------

    def test_an_unrecognised_motorctl_failure_stops_the_batch(self) -> None:
        """Not every non-zero exit is forgiven -- only the known ones."""
        self.results[MOTORCTL_ENABLE] = SshResult(
            MOTORCTL_ENABLE, 1, stderr="motorctl: driver not present"
        )
        self._start(count=4)
        self._row(READY, NONE)

        self.assertEqual(self.batch.phase, BATCH_IDLE)
        self.assertEqual(self.sent.count(MOTORCTL_ENABLE), 1)   # not retried
        self.assertNotIn(MOTORCTL_TARGET, self.sent)
        self.assertEqual(len(self.log), 0)
        ok, message = self.finished[0]
        self.assertFalse(ok)
        self.assertIn(MOTORCTL_ENABLE, message)
        self.assertIn("driver not present", message)

    def test_a_failed_clear_fault_stops_before_anything_else_is_sent(self) -> None:
        self.results[MOTORCTL_CLEAR_FAULT] = SshResult(
            MOTORCTL_CLEAR_FAULT, 255, error="ssh: connect refused"
        )
        self._start(count=4)

        self.assertEqual(self.sent, PROBES + [MOTORCTL_CLEAR_FAULT])
        self.assertEqual(self.batch.phase, BATCH_IDLE)
        self.assertIn("connect refused", self.finished[0][1])

    def test_ready_never_arriving_stops_the_batch(self) -> None:
        self._start(count=4)
        self._row(FAULT, COMM)                         # never clears
        self.batch._confirm_expired()

        self.assertEqual(self.batch.phase, BATCH_IDLE)
        self.assertNotIn(MOTORCTL_ENABLE, self.sent)
        ok, message = self.finished[0]
        self.assertFalse(ok)
        self.assertIn("STATE=READY / FAULT=NONE never arrived", message)

    def test_run_never_arriving_stops_the_batch(self) -> None:
        self._start(count=4)
        self._row(READY, NONE)
        self.batch._confirm_expired()

        self.assertEqual(self.batch.phase, BATCH_IDLE)
        self.assertNotIn(KILL, self.sent)
        self.assertIn("STATE=RUN / FAULT=NONE never arrived", self.finished[0][1])

    def test_serial_disconnect_stops_the_batch_immediately(self) -> None:
        self._start(count=4)
        self._row(READY, NONE)
        self.batch.set_connected(False)

        self.assertEqual(self.batch.phase, BATCH_IDLE)
        self.assertFalse(self.finished[0][0])
        self.assertIn("disconnected", self.finished[0][1])

    def test_a_dropped_trial_stops_the_batch(self) -> None:
        self._start(count=4)
        self._through_setup()
        self.runner.abort("serial telemetry disconnected during the trial")

        self.assertEqual(self.batch.phase, BATCH_IDLE)
        self.assertEqual(len(self.log), 0)
        self.assertFalse(self.finished[0][0])
        self.assertIn("dropped", self.finished[0][1])

    def test_stale_samples_cannot_confirm_a_commanded_state(self) -> None:
        """READY from before the clear-fault must not satisfy the wait."""
        self.store.add_row({"STATE": READY, "FAULT": NONE})
        self.batch.start(TARGET, KILL, 1, self.store, True, settle=2.0)
        self.batch.consume(self.store)
        self.assertEqual(self.batch.phase, AWAIT_READY)
        self.assertNotIn(MOTORCTL_ENABLE, self.sent)

    # --- refusals ----------------------------------------------------------

    def test_refused_without_serial(self) -> None:
        self.assertFalse(self.batch.start(TARGET, KILL, 5, self.store, False))
        self.assertEqual(self.sent, [])
        self.assertIn("not connected", self.finished[0][1])

    def test_refused_without_a_host(self) -> None:
        self.assertFalse(
            self.batch.start(SshTarget(""), KILL, 5, self.store, True)
        )
        self.assertIn("host", self.finished[0][1])

    def test_refused_while_a_single_trial_is_running(self) -> None:
        self.store.add_row({"STATE": RUN, "FAULT": NONE})
        self.runner._injection = datetime.now()
        self.runner._set_phase("injecting")
        self.assertFalse(self._start(count=2))
        self.assertIn("single trial", self.finished[0][1])

    def test_a_second_batch_is_refused_while_one_runs(self) -> None:
        self._start(count=2)
        self.assertFalse(self._start(count=2))
        self.assertIn("already running", self.finished[0][1])

    def test_batch_of_one_hundred_is_accepted(self) -> None:
        self.assertTrue(self._start(count=100))
        self.assertEqual(self.batch.total, 100)
        self.assertEqual(self.progress[0], (1, 100))


class BatchIntegrationTest(unittest.TestCase):
    """The same machine on real Qt timers and a real event loop.

    Everything above fires the timers by hand, which proves the decisions but
    not the wiring.  This runs two whole trials against a fake board with the
    windows shrunk to milliseconds, so a disconnected timer or a signal that
    was never connected shows up as a hang, not a pass.
    """

    def setUp(self) -> None:
        self.log = TrialLog()
        self.store = DataStore()
        self.sent: list[str] = []
        self.board = FakeBoard()
        fake = lambda t, c: FakeSsh(  # noqa: E731
            t, c, self.sent, {}, on_start=self.board.on_command
        )
        self.runner = FaultTestRunner(self.log, ssh_factory=fake)
        self.batch = BatchRunner(self.runner, ssh_factory=fake)

    def test_two_trials_run_end_to_end(self) -> None:
        outcome: list[tuple[bool, str]] = []
        loop = QEventLoop()
        self.batch.finished.connect(lambda ok, msg: outcome.append((ok, msg)))
        self.batch.finished.connect(lambda *_: loop.quit())

        # Stands in for the main window's tick: telemetry in, machine driven.
        tick = QTimer()
        tick.timeout.connect(self._tick)
        tick.start(20)

        guard = QTimer()          # never hang the suite if a timer is unwired
        guard.setSingleShot(True)
        guard.timeout.connect(loop.quit)
        guard.start(10_000)

        started = self.batch.start(
            TARGET, KILL, 2, self.store, True,
            detect_timeout=2.0, watch_window=0.3, confirm_timeout=2.0,
            settle=0.05, ready_timeout=2.0, poll=0.02,
        )
        self.assertTrue(started)
        loop.exec()
        tick.stop()

        self.assertEqual(outcome, [(True, "Batch complete: 2 trials run.")])
        self.assertEqual(len(self.log), 2)
        self.assertTrue(all(t.passed for t in self.log.trials))
        self.assertEqual([t.index for t in self.log.trials], [1, 2])
        self.assertEqual(self.log.pass_rate(), 1.0)
        self.assertEqual(len(self.log.delays()), 2)
        self.assertEqual(
            self.sent,
            (PROBES + [MOTORCTL_CLEAR_FAULT, MOTORCTL_ENABLE,
                       MOTORCTL_TARGET, KILL]) * 2,
        )

    def _tick(self) -> None:
        state, fault = self.board.reading()
        self.store.add_row({"STATE": state, "FAULT": fault, "RPM": 0})
        self.runner.consume(self.store)
        self.batch.consume(self.store)


class FakeBoard:
    """Answers the motorctl steps the way the bench does."""

    def __init__(self) -> None:
        self._state, self._fault = FAULT, COMM   # left latched by an earlier run

    def on_command(self, command: str) -> None:
        if command == MOTORCTL_CLEAR_FAULT:
            self._state, self._fault = READY, NONE
        elif command == MOTORCTL_TARGET:
            self._state, self._fault = RUN, NONE
        elif command == KILL:
            self._state, self._fault = FAULT, COMM   # and it stays latched

    def reading(self) -> tuple[int, int]:
        return self._state, self._fault


if __name__ == "__main__":
    unittest.main()
