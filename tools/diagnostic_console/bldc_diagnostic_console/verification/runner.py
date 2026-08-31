"""Semi-automated SIGKILL trial: inject, watch telemetry, score, record.

The runner never commands the motor.  It sends one kill over SSH and then only
*reads* the USART3 stream; CLEAR_FAULT / ENABLE / RUN stay a manual motorctl
step between trials, by design.

A trial PASSes only when STATE=FAULT / FAULT=COMM appears within the detect
timeout *and* still holds when the watch window closes.  Leaving that latch at
any point in the window fails the trial, whether the board went back to RUN on
its own or an operator recovered it early -- a latch that did not survive the
window is not evidence that it would have.
"""

from datetime import datetime

from PySide6.QtCore import QObject, QTimer, Signal

from ..constants import (
    FAULT_COMM,
    FAULT_LABELS,
    FAULT_NONE,
    STATE_FAULT,
    STATE_LABELS,
    STATE_RUN,
)
from ..data_store import DataStore
from .config import AUTO_RUN_WATCH_S, FAULT_DETECT_TIMEOUT_S, TEST_TYPE
from .ssh import SshCommand, SshResult, SshTarget
from .trial import TrialLog, TrialRecord

IDLE = "idle"
INJECTING = "injecting"     # kill sent, waiting for STATE=FAULT / FAULT=COMM
WATCHING = "watching"       # fault seen, watching that the COMM latch holds


class FaultTestRunner(QObject):
    status = Signal(str)
    phase_changed = Signal(str)
    injected = Signal(object)        # datetime
    fault_observed = Signal(object)  # datetime
    trial_finished = Signal(object)  # TrialRecord
    aborted = Signal(str)            # no trial recorded; reason for the operator

    def __init__(self, log: TrialLog, ssh_factory=SshCommand, parent=None) -> None:
        super().__init__(parent)
        self.log = log
        self._ssh_factory = ssh_factory
        self._ssh: SshCommand | None = None
        self._phase = IDLE
        self._cursor = 0
        self._reset_trial()

        self._detect_timer = QTimer(self)
        self._detect_timer.setSingleShot(True)
        self._detect_timer.timeout.connect(self._detect_timeout)
        self._watch_timer = QTimer(self)
        self._watch_timer.setSingleShot(True)
        self._watch_timer.timeout.connect(self._watch_elapsed)

    @property
    def phase(self) -> str:
        return self._phase

    @property
    def busy(self) -> bool:
        return self._phase != IDLE

    # --- preconditions -----------------------------------------------------

    def preconditions(self, store: DataStore, connected: bool) -> str | None:
        """Reason the trial must not start, or None when it may."""
        if self.busy:
            return "a trial is already running"
        if not connected:
            return "serial telemetry is not connected"
        latest = _latest(store)
        if latest is None:
            return "no telemetry samples received yet"
        state, fault = latest
        if int(state) != STATE_RUN:
            return (
                f"STATE must be RUN before injecting; it is "
                f"{_label(STATE_LABELS, state)}"
            )
        if int(fault) != FAULT_NONE:
            return (
                f"FAULT must be NONE before injecting; it is "
                f"{_label(FAULT_LABELS, fault)}"
            )
        return None

    # --- running -----------------------------------------------------------

    def start(
        self,
        target: SshTarget,
        command: str,
        store: DataStore,
        connected: bool,
        detect_timeout: float = FAULT_DETECT_TIMEOUT_S,
        watch_window: float = AUTO_RUN_WATCH_S,
        setup_note: str = "",
    ) -> bool:
        """Start one trial.

        ``setup_note`` carries anything the caller wants recorded about how the
        board was brought to RUN -- the batch runner puts its unconfirmed
        motorctl steps there, so a trial row shows what was shaky about its
        setup even when the trial itself passed.
        """
        reason = self.preconditions(store, connected)
        if reason is not None:
            self.aborted.emit(reason)
            return False

        self._reset_trial()
        self._setup_note = setup_note
        self._watch_window = watch_window
        self._detect_timeout_s = detect_timeout
        # Only samples that arrive after the kill can count as the transition.
        self._cursor = len(store.t)
        self._last_seen = _latest(store)
        self._injection = datetime.now()

        self._set_phase(INJECTING)
        self.injected.emit(self._injection)
        self.status.emit(
            f"Trial {self.log.next_index()}: kill sent, waiting up to "
            f"{detect_timeout:g} s for STATE=FAULT / FAULT=COMM"
        )

        self._ssh = self._ssh_factory(target, command)
        self._ssh.completed.connect(self._on_ssh)
        self._detect_timer.start(int(detect_timeout * 1000))
        self._ssh.start()
        return True

    def abort(self, reason: str) -> None:
        """Drop the trial in flight without recording it."""
        if not self.busy:
            return
        self._stop_timers()
        self._set_phase(IDLE)
        self.aborted.emit(reason)

    # --- telemetry ---------------------------------------------------------

    def consume(self, store: DataStore) -> None:
        """Feed newly captured samples in.  Safe to call on every UI tick."""
        if len(store.t) < self._cursor:
            self._cursor = len(store.t)  # the store was reset under us
        if self._phase == IDLE:
            self._cursor = len(store.t)
            return

        while self._cursor < len(store.t):
            i = self._cursor
            self._cursor += 1
            state = sample_at(store, "STATE", i)
            fault = sample_at(store, "FAULT", i)
            if state is None or fault is None:
                continue
            self._last_seen = (state, fault)

            if self._phase == INJECTING:
                if _latched(state, fault):
                    self._on_fault_observed(store.wall[i])
                elif int(fault) != FAULT_NONE:
                    # A different fault latched: keep waiting for COMM, but
                    # remember it so the trial note explains the failure.
                    self._other_fault = fault
            elif self._phase == WATCHING and not _latched(state, fault):
                # PASS requires the latch to hold for the whole window.  A
                # return to RUN is the auto-restart this test hunts for; any
                # other move off FAULT/COMM means the board left the latch
                # some other way -- usually a manual recovery run while the
                # window was still open.  Both end the trial as a failure,
                # and only the first is an auto-run.
                self._auto_run = int(state) == STATE_RUN
                self._latch_broken = (state, fault, store.wall[i])
                self._finish()
                return

    # --- internals ---------------------------------------------------------

    def _on_fault_observed(self, wall: datetime) -> None:
        self._detect_timer.stop()
        self._fault_time = wall
        self._delay_ms = (wall - self._injection).total_seconds() * 1000.0
        self._set_phase(WATCHING)
        self.fault_observed.emit(wall)
        self.status.emit(
            f"COMM fault observed after {self._delay_ms:.0f} ms — "
            f"STATE=FAULT / FAULT=COMM must now hold for {self._watch_window:g} s. "
            f"Do not recover yet."
        )
        self._watch_timer.start(int(self._watch_window * 1000))

    def _detect_timeout(self) -> None:
        if self._phase == INJECTING:
            self._finish()

    def _watch_elapsed(self) -> None:
        if self._phase == WATCHING:
            self._finish()

    def _on_ssh(self, result: SshResult) -> None:
        if result.ok:
            self.status.emit("Kill command accepted; watching telemetry…")
            return
        # The injection never happened, so this is not a trial of the STM32:
        # report it and record nothing rather than poisoning the statistics.
        if self._phase == INJECTING and self._fault_time is None:
            self._stop_timers()
            self._set_phase(IDLE)
            self.aborted.emit(f"fault injection failed — {result.describe()}")

    def _finish(self) -> None:
        self._stop_timers()
        state, fault = self._last_seen if self._last_seen else (float("nan"),) * 2
        detected = self._fault_time is not None
        passed = detected and self._latch_broken is None

        trial = TrialRecord(
            index=self.log.next_index(),
            test_type=TEST_TYPE,
            injection_time=self._injection,
            fault_observed_time=self._fault_time,
            delay_ms=self._delay_ms,
            final_state=_label(STATE_LABELS, state),
            final_fault=_label(FAULT_LABELS, fault),
            auto_run_detected=self._auto_run,
            passed=passed,
            note=self._note(detected),
        )
        self.log.add(trial)
        self._set_phase(IDLE)
        self.trial_finished.emit(trial)
        self.status.emit(
            f"Trial {trial.index}: {trial.result}. Recover manually "
            f"(CLEAR_FAULT → ENABLE → RUN) before the next trial."
        )

    def _note(self, detected: bool) -> str:
        parts = [part for part in (self._setup_note, self._outcome_note(detected))
                 if part]
        return "; ".join(parts)

    def _outcome_note(self, detected: bool) -> str:
        if self._latch_broken is not None:
            state, fault, wall = self._latch_broken
            seen = (
                f"STATE={_label(STATE_LABELS, state)} / "
                f"FAULT={_label(FAULT_LABELS, fault)} after "
                f"{(wall - self._fault_time).total_seconds() * 1000.0:.0f} ms"
            )
            if self._auto_run:
                return f"STATE returned to RUN during the watch window: {seen}"
            if int(fault) == FAULT_NONE:
                return f"Fault latch cleared during watch window: {seen}"
            # Still faulted, but no longer the COMM latch the kill produced.
            return f"Fault latch did not hold during watch window: {seen}"
        if not detected:
            note = (
                f"no STATE=FAULT/FAULT=COMM within {self._detect_timeout_s:g} s"
            )
            if self._other_fault is not None:
                note += f"; saw FAULT={_label(FAULT_LABELS, self._other_fault)}"
            return note
        return ""

    def _reset_trial(self) -> None:
        self._injection: datetime | None = None
        self._fault_time: datetime | None = None
        self._delay_ms: float | None = None
        self._auto_run = False
        self._setup_note = ""
        #: (STATE, FAULT, wall) of the first watch-window sample that was
        #: no longer FAULT/COMM.  None means the latch held.
        self._latch_broken: tuple[float, float, datetime] | None = None
        self._other_fault: float | None = None
        self._last_seen: tuple[float, float] | None = None
        self._watch_window = AUTO_RUN_WATCH_S
        self._detect_timeout_s = FAULT_DETECT_TIMEOUT_S

    def _stop_timers(self) -> None:
        self._detect_timer.stop()
        self._watch_timer.stop()

    def _set_phase(self, phase: str) -> None:
        self._phase = phase
        self.phase_changed.emit(phase)


def _latched(state: float, fault: float) -> bool:
    """True while the board still shows the COMM fault the kill produced."""
    return int(state) == STATE_FAULT and int(fault) == FAULT_COMM


def sample_at(store: DataStore, name: str, i: int) -> float | None:
    """One numeric sample, or None when it is missing or NaN."""
    column = store.series.get(name, [])
    if i >= len(column):
        return None
    value = column[i]
    return None if value != value else value  # value != value rejects NaN


def _latest(store: DataStore) -> tuple[float, float] | None:
    if not store.t:
        return None
    i = len(store.t) - 1
    state = sample_at(store, "STATE", i)
    fault = sample_at(store, "FAULT", i)
    if state is None or fault is None:
        return None
    return state, fault


def _label(labels: dict[int, str], value: float) -> str:
    if value != value:  # NaN
        return "?"
    return labels.get(int(value), str(int(value)))
