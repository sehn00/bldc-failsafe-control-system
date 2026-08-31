"""Repeat the manual SIGKILL procedure without an operator at the bench.

One batch trial is exactly the sequence a person runs by hand:

    motorctl clear-fault  ->  STATE=READY / FAULT=NONE
    motorctl enable
    motorctl target ...   ->  STATE=RUN / FAULT=NONE
    settle
    SIGKILL the supervisor -> FaultTestRunner scores the trial

Only the setup steps are new.  Detection, the delay measurement, the PASS rule
and the trial record all stay in :class:`FaultTestRunner`, so a batch trial and
a hand-run one are the same trial and land in the same table, statistics and
CSV.

Two rules shape everything below.

*Nothing is retried.*  A refused SSH command, a motorctl that fails, a state
that never arrives, a lost serial link: each stops the whole batch and says
why.  Retrying would quietly fold a bench fault into the campaign statistics.

*Nothing touches the board while a watch window is open.*  The next
clear-fault is sent only after the runner has finished and recorded the trial,
which is the moment the window closes.  Before it goes out, the supervisor --
which systemd has just restarted -- is polled until it is actually accepting
commands again.

One thing is deliberately *not* trusted: a motorctl exit code.  A mutating
command can be applied by the STM32 while motorctl's own follow-up check times
out, so a recognised "sent but unconfirmed" failure is recorded and the
telemetry post-condition decides.  See :mod:`.motorctl`.
"""

from PySide6.QtCore import QObject, QTimer, Signal

from ..constants import FAULT_NONE, STATE_READY, STATE_RUN
from ..data_store import DataStore
from .config import (
    AUTO_RUN_WATCH_S,
    SUPERVISOR_READY_CONFIRMATIONS,
    BATCH_SETTLE_S,
    FAULT_DETECT_TIMEOUT_S,
    MOTORCTL_CLEAR_FAULT,
    MOTORCTL_ENABLE,
    MOTORCTL_TARGET,
    STATE_CONFIRM_TIMEOUT_S,
    SUPERVISOR_POLL_S,
    SUPERVISOR_READY_COMMAND,
    SUPERVISOR_READY_TIMEOUT_S,
)
from .motorctl import (
    DESYNCHRONIZED,
    EXECUTED,
    UNCONFIRMED,
    classify,
    reports_state,
    transport_failed,
)
from .runner import FaultTestRunner, sample_at
from .ssh import SshCommand, SshResult, SshTarget

BATCH_IDLE = "idle"
CLEARING = "clearing"        # motorctl clear-fault in flight
AWAIT_READY = "await-ready"  # waiting for STATE=READY / FAULT=NONE
ENABLING = "enabling"        # motorctl enable in flight
TARGETING = "targeting"      # motorctl target in flight
AWAIT_RUN = "await-run"      # waiting for STATE=RUN / FAULT=NONE
SETTLING = "settling"        # holding RUN before the kill
TRIAL = "trial"              # FaultTestRunner owns the sequence
AWAIT_SUPERVISOR = "await-supervisor"   # polling until motorctl works again

#: Phases where the bench has already been commanded but the trial has not
#: started.
SETUP_PHASES = (CLEARING, AWAIT_READY, ENABLING, TARGETING, AWAIT_RUN, SETTLING)

#: What telemetry has to show for a setup step to have worked.  ENABLE has no
#: entry on purpose: it readies the driver and RUN arrives with the TARGET that
#: follows it, so the RUN check after TARGET judges both.
POST_CONDITIONS = {
    CLEARING: (STATE_READY, FAULT_NONE),
    TARGETING: (STATE_RUN, FAULT_NONE),
}


class BatchRunner(QObject):
    """Drives FaultTestRunner through a fixed number of trials."""

    status = Signal(str)
    phase_changed = Signal(str)
    progress = Signal(int, int)      # current trial, total
    #: (completed_normally, message).  Emitted once per batch, including for a
    #: start that was refused before anything was sent.
    finished = Signal(bool, str)

    def __init__(
        self,
        runner: FaultTestRunner,
        ssh_factory=SshCommand,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.runner = runner
        self._ssh_factory = ssh_factory
        self._ssh: SshCommand | None = None
        self._phase = BATCH_IDLE
        self._index = 0
        self._total = 0
        self._stop_requested = False
        self._store: DataStore | None = None
        self._connected = False
        self._cursor = 0
        self._expect: tuple[int, int] | None = None
        self._target: SshTarget | None = None
        self._kill = ""
        self._detect_timeout = FAULT_DETECT_TIMEOUT_S
        self._watch_window = AUTO_RUN_WATCH_S
        self._confirm_timeout = STATE_CONFIRM_TIMEOUT_S
        self._settle_s = BATCH_SETTLE_S
        self._ready_timeout = SUPERVISOR_READY_TIMEOUT_S
        self._poll_s = SUPERVISOR_POLL_S
        self._completed = 0
        self._probes = 0
        self._confirmations = 0
        self._confirmations_needed = SUPERVISOR_READY_CONFIRMATIONS
        self._last_probe = ""
        #: (command, phase) to send once the supervisor confirms ready, and
        #: the status line that goes with it.
        self._after_ready: tuple[str, str] | None = None
        self._next_caption = ""
        #: Commands already re-sent once in this trial, so a second sync
        #: failure of the same command stops the batch instead of looping.
        self._retried: set[str] = set()
        #: True once a setup command has gone out for the current trial.
        self._in_setup = False
        #: True from `motorctl target` until the trial is scored: stopping in
        #: that window can leave the motor turning.
        self._motor_commanded = False
        self._resyncs = 0
        self._retries = 0
        #: Steps of this trial's setup that motorctl could not confirm; they
        #: ride along into the trial's Note.
        self._setup_notes: list[str] = []

        self._confirm_timer = _one_shot(self, self._confirm_expired)
        self._settle_timer = _one_shot(self, self._settled)
        self._poll_timer = _one_shot(self, self._probe_supervisor)
        self._ready_timer = _one_shot(self, self._ready_expired)

        runner.trial_finished.connect(self._on_trial_finished)
        runner.aborted.connect(self._on_runner_aborted)

    # --- state -------------------------------------------------------------

    @property
    def phase(self) -> str:
        return self._phase

    @property
    def busy(self) -> bool:
        return self._phase != BATCH_IDLE

    @property
    def index(self) -> int:
        """1-based trial being run, or 0 when idle."""
        return self._index

    @property
    def completed(self) -> int:
        """Trials finished and recorded by this batch."""
        return self._completed

    @property
    def resyncs(self) -> int:
        """Setup steps this batch lost to the supervisor's sync guard."""
        return self._resyncs

    @property
    def retries(self) -> int:
        """Setup commands re-sent after a resync.  Never a kill."""
        return self._retries

    @property
    def total(self) -> int:
        return self._total

    @property
    def stopping(self) -> bool:
        return self._stop_requested

    # --- lifecycle ---------------------------------------------------------

    def start(
        self,
        target: SshTarget,
        kill_command: str,
        count: int,
        store: DataStore,
        connected: bool,
        detect_timeout: float = FAULT_DETECT_TIMEOUT_S,
        watch_window: float = AUTO_RUN_WATCH_S,
        confirm_timeout: float = STATE_CONFIRM_TIMEOUT_S,
        settle: float = BATCH_SETTLE_S,
        ready_timeout: float = SUPERVISOR_READY_TIMEOUT_S,
        poll: float = SUPERVISOR_POLL_S,
        confirmations: int = SUPERVISOR_READY_CONFIRMATIONS,
    ) -> bool:
        refusal = self._refusal(target, kill_command, count, connected)
        if refusal is not None:
            self.finished.emit(False, refusal)
            return False

        self._target = target
        self._kill = kill_command
        self._total = count
        self._index = 0
        self._completed = 0
        self._stop_requested = False
        self._store = store
        self._connected = connected
        self._cursor = len(store.t)
        self._detect_timeout = detect_timeout
        self._watch_window = watch_window
        self._confirm_timeout = confirm_timeout
        self._settle_s = settle
        self._ready_timeout = ready_timeout
        self._poll_s = poll
        self._confirmations_needed = max(1, confirmations)
        self._resyncs = 0
        self._retries = 0

        self.status.emit(f"Batch started: {count} trials.")
        self._begin_trial()
        return True

    def _refusal(
        self, target: SshTarget, kill_command: str, count: int, connected: bool
    ) -> str | None:
        if self.busy:
            return "a batch is already running"
        if self.runner.busy:
            return "a single trial is still running"
        if not target.host.strip():
            return "no Raspberry Pi host set"
        if not kill_command.strip():
            return "the remote kill command is empty"
        if count < 1:
            return "batch count must be at least 1"
        if not connected:
            return "serial telemetry is not connected"
        return None

    def stop(self) -> None:
        """Ask for a clean stop.  A trial already in flight is finished first."""
        if not self.busy or self._stop_requested:
            return
        self._stop_requested = True
        if self._phase == TRIAL:
            self.status.emit(
                f"Stop requested — finishing trial {self._index}/{self._total} "
                f"and its watch window first."
            )
            return
        if self._phase == AWAIT_SUPERVISOR:
            if self._in_setup:
                # A resync in the middle of a setup, not the gate before one.
                self._stopped_during_setup()
            else:
                self._end(True, f"Batch stopped after {self._completed} trials.")
            return
        if self._phase in (AWAIT_READY, AWAIT_RUN, SETTLING):
            # Only a timer is pending, so stop now rather than let the wait run
            # on and report itself as a timeout.
            self._stopped_during_setup()
            return
        # An SSH step is in flight; stop at its boundary instead of orphaning it.
        self.status.emit("Stop requested — stopping at the end of this step.")

    def abort(self, reason: str) -> None:
        """Stop now, without finishing whatever is in flight."""
        if not self.busy:
            return
        self._end(False, f"Batch aborted: {reason}.")

    def set_connected(self, connected: bool) -> None:
        self._connected = connected
        if not connected:
            self.abort("serial telemetry disconnected")

    # --- telemetry ---------------------------------------------------------

    def consume(self, store: DataStore) -> None:
        """Feed captured samples in; safe to call on every UI tick."""
        self._store = store
        if self._phase not in (AWAIT_READY, AWAIT_RUN) or self._expect is None:
            return
        if len(store.t) < self._cursor:
            self._cursor = len(store.t)  # the store was reset under us

        want_state, want_fault = self._expect
        while self._cursor < len(store.t):
            i = self._cursor
            self._cursor += 1
            state = sample_at(store, "STATE", i)
            fault = sample_at(store, "FAULT", i)
            if state is None or fault is None:
                continue
            if int(state) == want_state and int(fault) == want_fault:
                self._confirm_timer.stop()
                self._confirmed()
                return

    # --- the per-trial sequence -------------------------------------------

    def _begin_trial(self) -> None:
        if self._stop_requested:
            self._end(True, f"Batch stopped after {self._completed} trials.")
            return
        self._index += 1
        self._setup_notes = []
        self._retried = set()
        self._in_setup = False
        self._motor_commanded = False
        self.progress.emit(self._index, self._total)
        # systemd has just restarted the supervisor the previous trial killed,
        # so ask it -- rather than a fixed sleep -- when it is usable again.
        self._gate(MOTORCTL_CLEAR_FAULT, CLEARING, "clearing the fault latch")

    # --- supervisor readiness ---------------------------------------------

    def _gate(self, command: str, phase: str, caption: str) -> None:
        """Send `command` only once the supervisor confirms it is in step.

        Every mutating step goes out through here, so gating one more of them
        is a matter of routing it through `_gate` instead of `_send`.
        """
        self._after_ready = (command, phase)
        self._probes = 0
        self._confirmations = 0
        self._last_probe = ""
        self._next_caption = caption
        self._set_phase(AWAIT_SUPERVISOR)
        self._ready_timer.start(int(self._ready_timeout * 1000))
        self._probe_supervisor()

    def _probe_supervisor(self) -> None:
        if self._phase != AWAIT_SUPERVISOR:
            return
        self._probes += 1
        if self._probes == 1:
            self.status.emit(
                f"Trial {self._index}/{self._total}: checking that "
                f"motor-supervisor is in step with the STM32."
            )
        self._send(SUPERVISOR_READY_COMMAND, AWAIT_SUPERVISOR)

    def _on_probe(self, result: SshResult) -> None:
        if transport_failed(result):
            # The bench link is gone; the supervisor is not the problem.
            self._end(False, f"readiness probe could not run — {result.describe()}")
            return
        if not reports_state(result):
            # Not in step yet: either motorctl could not reach it, or it
            # answered without a state line.  Expected while systemd is still
            # restarting it, so this is the one place that polls -- and the
            # run of confirmations starts over.
            self._confirmations = 0
            self._last_probe = result.describe()
            self._poll_timer.start(int(self._poll_s * 1000))
            return

        self._confirmations += 1
        if self._confirmations < self._confirmations_needed:
            # Confirm again straight away: each probe is its own GET_STATE
            # round trip with the board, so back-to-back replies are already
            # separated by real link time.
            self._probe_supervisor()
            return

        self._ready_timer.stop()
        if self._probes > self._confirmations_needed:
            self._setup_notes.append(f"supervisor ready after {self._probes} probes")
        if self._stop_requested:
            self._stopped_during_setup() if self._in_setup else self._end(
                True, f"Batch stopped after {self._completed} trials."
            )
            return
        command, phase = self._after_ready
        self.status.emit(f"Trial {self._index}/{self._total}: {self._next_caption}.")
        self._send(command, phase)

    def _ready_expired(self) -> None:
        if self._phase != AWAIT_SUPERVISOR:
            return
        detail = f" — last probe: {self._last_probe}" if self._last_probe else ""
        self._end(
            False,
            f"motor-supervisor was not in step with the STM32 within "
            f"{self._ready_timeout:g} s{detail}",
        )

    # --- motorctl steps ----------------------------------------------------

    def _send(self, command: str, phase: str) -> None:
        if phase in (CLEARING, ENABLING, TARGETING):
            self._in_setup = True
        if command == MOTORCTL_TARGET:
            # From here until the trial is scored, the motor may be turning.
            self._motor_commanded = True
        self._set_phase(phase)
        self._ssh = self._ssh_factory(self._target, command)
        self._ssh.completed.connect(self._on_ssh)
        self._ssh.start()

    def _on_ssh(self, result: SshResult) -> None:
        if self._phase == AWAIT_SUPERVISOR:
            self._on_probe(result)
            return
        if self._phase not in (CLEARING, ENABLING, TARGETING):
            return  # a late result from a batch that already stopped

        verdict, detail = classify(result)
        if verdict == DESYNCHRONIZED:
            self._on_desync(result.command, detail)
            return
        if verdict == UNCONFIRMED:
            # Sent, but motorctl could not confirm it.  Say so, keep it in the
            # trial Note, and let the telemetry post-condition decide.
            note = f"{result.command} unconfirmed ({detail})"
            self._setup_notes.append(note)
            self.status.emit(
                f"Trial {self._index}/{self._total}: {note} — checking telemetry."
            )
        elif verdict != EXECUTED:
            self._end(False, f"`{result.command}` failed — {detail}")
            return

        if self._stop_requested:
            self._stopped_during_setup()
            return
        self._advance(self._phase)

    def _advance(self, phase: str) -> None:
        """Whatever follows a setup step that is done with."""
        if phase == CLEARING:
            self._await(AWAIT_READY, STATE_READY, "STATE=READY / FAULT=NONE")
        elif phase == ENABLING:
            self.status.emit(f"Trial {self._index}/{self._total}: commanding target.")
            self._send(MOTORCTL_TARGET, TARGETING)
        else:  # TARGETING
            self._await(AWAIT_RUN, STATE_RUN, "STATE=RUN / FAULT=NONE")

    def _on_desync(self, command: str, detail: str) -> None:
        """The supervisor would not forward this command; nothing was sent.

        `schedule_work` answers and returns before writing the UART frame, so
        the STM32 never saw it -- this can never be read as "probably applied".
        Either the board is already where the step needed it, or the command
        has to go again once the supervisor is back in step with it.
        """
        phase = self._phase
        self._resyncs += 1
        wanted = POST_CONDITIONS.get(phase)
        if wanted is not None and self._telemetry_shows(wanted):
            # Nothing left for this step to do, so re-sending would only be a
            # second chance to go wrong.
            self._setup_notes.append(
                f"{command} unconfirmed due to supervisor resync"
            )
            self.status.emit(
                f"Trial {self._index}/{self._total}: {command} hit the "
                f"supervisor sync guard, but telemetry already shows the "
                f"state it wanted — carrying on."
            )
            self._advance(phase)
            return

        if command in self._retried:
            self._end(
                False,
                f"`{command}` hit the supervisor sync guard again after a "
                f"resync — {detail}",
            )
            return

        self._retried.add(command)
        self._retries += 1
        self._setup_notes.append(f"{command} retried once after supervisor sync loss")
        self.status.emit(
            f"Trial {self._index}/{self._total}: {command} refused — "
            f"{detail}. Waiting for the supervisor to resynchronize."
        )
        self._gate(command, phase, f"re-sending {command}")

    def _telemetry_shows(self, wanted: tuple[int, int]) -> bool:
        """True when the newest sample already satisfies a post-condition.

        The newest sample can be up to one ~500 ms frame old.  That cannot
        produce a false yes here: each step is asked for a state the board was
        not in when the step began (FAULT/COMM before clear-fault, READY before
        target), so a stale sample can only fail to confirm, never invent.
        """
        if self._store is None or not self._store.t:
            return False
        i = len(self._store.t) - 1
        state = sample_at(self._store, "STATE", i)
        fault = sample_at(self._store, "FAULT", i)
        if state is None or fault is None:
            return False
        return (int(state), int(fault)) == wanted

    def _await(self, phase: str, state: int, caption: str) -> None:
        self._expect = (state, FAULT_NONE)
        # Only samples captured after the command can confirm it.
        self._cursor = len(self._store.t) if self._store else 0
        self._set_phase(phase)
        self.status.emit(
            f"Trial {self._index}/{self._total}: waiting up to "
            f"{self._confirm_timeout:g} s for {caption}."
        )
        self._confirm_timer.start(int(self._confirm_timeout * 1000))

    def _confirm_expired(self) -> None:
        if self._phase not in (AWAIT_READY, AWAIT_RUN):
            return
        wanted = "STATE=READY / FAULT=NONE" if self._phase == AWAIT_READY \
            else "STATE=RUN / FAULT=NONE"
        self._end(
            False,
            f"{wanted} never arrived within {self._confirm_timeout:g} s",
        )

    def _confirmed(self) -> None:
        if self._stop_requested:
            self._stopped_during_setup()
            return
        if self._phase == AWAIT_READY:
            self.status.emit(f"Trial {self._index}/{self._total}: enabling.")
            self._send(MOTORCTL_ENABLE, ENABLING)
            return
        self._set_phase(SETTLING)
        self.status.emit(
            f"Trial {self._index}/{self._total}: RUN confirmed, settling "
            f"{self._settle_s:g} s before the kill."
        )
        self._settle_timer.start(int(self._settle_s * 1000))

    def _settled(self) -> None:
        if self._phase != SETTLING:
            return
        if self._stop_requested:
            self._stopped_during_setup()
            return
        self._set_phase(TRIAL)
        started = self.runner.start(
            self._target,
            self._kill,
            self._store,
            self._connected,
            detect_timeout=self._detect_timeout,
            watch_window=self._watch_window,
            setup_note=self._setup_note(),
        )
        if not started and self._phase == TRIAL:
            # The runner refused and emitted its own reason through aborted,
            # which stops the batch; this only covers a silent refusal.
            self._end(False, "the trial could not be started")

    # --- runner callbacks --------------------------------------------------

    def _on_trial_finished(self, trial) -> None:
        if self._phase != TRIAL:
            return  # a hand-run trial, not ours
        self._completed += 1
        # The kill has been scored; the board is latched, not turning.
        self._motor_commanded = False
        self.status.emit(
            f"Trial {self._index}/{self._total}: {trial.result}"
            + (f" — {trial.note}" if trial.note else "")
        )
        if self._stop_requested:
            self._end(True, f"Batch stopped after {self._completed} trials.")
            return
        if self._index >= self._total:
            self._end(True, f"Batch complete: {self._total} trials run.")
            return
        # This signal *is* the close of the watch window, so touching the board
        # is safe again from here.  _begin_trial waits for the supervisor
        # before it sends anything; that wait is outside every measured delay.
        self._begin_trial()

    def _on_runner_aborted(self, reason: str) -> None:
        if self._phase != TRIAL:
            return
        self._end(False, f"the trial was dropped — {reason}")

    # --- finishing ---------------------------------------------------------

    def _stopped_during_setup(self) -> None:
        self._end(
            True,
            f"Batch stopped after {self._completed} trials, part way through "
            f"the setup for trial {self._index}.",
        )

    def _end(self, completed: bool, message: str) -> None:
        spinning = self._motor_commanded
        self._stop_timers()
        self._expect = None
        self._set_phase(BATCH_IDLE)
        if spinning:
            message += " The motor may still be running — stop it with motorctl."
        self.finished.emit(completed, message)

    def _setup_note(self) -> str:
        if not self._setup_notes:
            return ""
        return "setup: " + ", ".join(self._setup_notes)

    def _stop_timers(self) -> None:
        self._confirm_timer.stop()
        self._settle_timer.stop()
        self._poll_timer.stop()
        self._ready_timer.stop()

    def _set_phase(self, phase: str) -> None:
        self._phase = phase
        self.phase_changed.emit(phase)


def _one_shot(parent: QObject, slot) -> QTimer:
    timer = QTimer(parent)
    timer.setSingleShot(True)
    timer.timeout.connect(slot)
    return timer
