"""Telling "the command never ran" apart from "motorctl could not confirm it".

``motorctl`` exits non-zero for two very different reasons, and the batch has to
treat them differently.

*The command never reached the supervisor* -- no ssh, refused connection, no
such executable, no socket at ``/run/motor-supervisor.sock``.  Nothing happened
on the STM32, so the batch stops.

*The command was sent but motorctl's own follow-up check timed out.*  Observed
on the bench::

    motorctl enable          -> ERROR GET_STATE timed out
    motorctl target 30 1000  -> WARN TARGET_SENT_NO_ACK STATE=RUN MODE=REMOTE FAULT=NONE
    motorctl status          -> OK STATE=RUN MODE=REMOTE FAULT=NONE

The motor was turning: both commands had in fact been applied, and only the
request/response check after them failed.  Aborting on that exit code throws
away a trial that was about to run correctly, so these are reported and the
telemetry post-condition -- STATE/FAULT out of the STM32 itself -- decides
whether the step worked.

A second kind of unconfirmed failure is scoped to one command, because it is
motorctl disagreeing with the procedure rather than with the board::

    motorctl enable -> ERROR ENABLE did not result in RUN;
                       STATE=READY MODE=REMOTE FAULT=NONE

ENABLE readies the driver; RUN arrives with the TARGET that follows it, so a
board left in READY / REMOTE / NONE is exactly right at that point.  Only
``motorctl enable`` gets that reading -- the same sentence from another command
is still an unrecognised failure.

Only the failures listed in ``config`` are read this way.  An exit code nobody
recognises still stops the batch: guessing that an unknown error was harmless
is how a broken bench turns into data.
"""

from .config import (
    MOTORCTL_COMMAND_UNCONFIRMED_MARKERS,
    MOTORCTL_DESYNC_MARKERS,
    MOTORCTL_NOT_EXECUTED_MARKERS,
    MOTORCTL_UNCONFIRMED_MARKERS,
    SUPERVISOR_STATE_TOKENS,
)
from .ssh import SshResult

#: The command ran and confirmed itself.
EXECUTED = "executed"
#: The command was sent, but motorctl could not confirm it.  Telemetry decides.
UNCONFIRMED = "unconfirmed"
#: The supervisor would not forward it because its STM32 view was stale.  The
#: frame was never written, and it becomes sendable again on its own.
DESYNCHRONIZED = "desynchronized"
#: The command never ran.  Nothing changed on the board.
FAILED = "failed"

#: Exit codes that mean the command itself never started: motorctl's own 2 (it
#: could not reach the supervisor socket, per motorctl.c), 126 not executable,
#: 127 not found, 255 ssh's own transport failure.  motorctl maps OK and WARN
#: responses to 0 and ERROR responses to 1, so 1 is the only code whose text
#: has to be read.
NOT_EXECUTED_CODES = (2, 126, 127, 255)

#: Long enough for a wrapped motorctl error, short enough for a table cell.
_DETAIL_LIMIT = 200


def classify(result: SshResult) -> tuple[str, str]:
    """Return ``(verdict, detail)`` for one remote command."""
    detail = describe(result)
    if result.error:
        return FAILED, detail                      # ssh could not run at all
    if result.returncode == 0:
        return EXECUTED, detail
    if result.returncode in NOT_EXECUTED_CODES:
        return FAILED, detail

    text = f"{result.stderr}\n{result.stdout}".lower()
    # Execution markers are checked first: a message that mentions both a
    # missing socket and a missing ack describes a command that never landed.
    if any(marker in text for marker in MOTORCTL_NOT_EXECUTED_MARKERS):
        return FAILED, detail
    if any(marker in text for marker in MOTORCTL_DESYNC_MARKERS):
        return DESYNCHRONIZED, detail
    if any(marker in text for marker in MOTORCTL_UNCONFIRMED_MARKERS):
        return UNCONFIRMED, detail
    if _matches_command_marker(result.command, text):
        return UNCONFIRMED, detail
    return FAILED, detail


def reports_state(result: SshResult) -> bool:
    """True when a readiness probe came back carrying a real state line.

    The supervisor answers `motorctl status` out of the same completion path
    that sets its `synchronized` flag, so a reply naming STATE/MODE/FAULT is
    evidence the link to the STM32 is live right now.
    """
    if not result.ok:
        return False
    text = f"{result.stdout}\n{result.stderr}".lower()
    return all(token in text for token in SUPERVISOR_STATE_TOKENS)


def _matches_command_marker(command: str, text: str) -> bool:
    """True for a failure this exact command is allowed to report."""
    groups = MOTORCTL_COMMAND_UNCONFIRMED_MARKERS.get(command.strip(), ())
    return any(all(marker in text for marker in group) for group in groups)


def describe(result: SshResult) -> str:
    """The remote output on one line, for a status message or a trial Note.

    ``SshResult.describe`` keeps only the last line, which drops the half of a
    wrapped motorctl error that names what went wrong.  Here the whole thing is
    collapsed instead, so "ENABLE did not result in RUN" survives next to the
    STATE it reported.
    """
    if result.error:
        return result.error
    text = " ".join((result.stderr or result.stdout).split())
    if len(text) > _DETAIL_LIMIT:
        text = text[: _DETAIL_LIMIT - 1].rstrip() + "…"
    prefix = f"exit {result.returncode}"
    return f"{prefix}: {text}" if text else prefix



def transport_failed(result: SshResult) -> bool:
    """True when ssh itself failed, as opposed to the remote command."""
    return bool(result.error) or result.returncode == 255
