"""Remote commands over the system OpenSSH client.

The console shells out to ``ssh`` instead of embedding an SSH library so that
existing keys, ``~/.ssh/config`` host aliases, agents and jump hosts keep
working exactly as they do in a terminal.  No password is ever read, prompted
for or stored: ``BatchMode=yes`` makes ssh fail instead of asking.
"""

import subprocess
from dataclasses import dataclass

from PySide6.QtCore import QThread, Signal

from .config import (
    DEFAULT_SSH_PORT,
    SSH_BATCH_OPTIONS,
    SSH_COMMAND_TIMEOUT_S,
    SSH_CONNECT_TIMEOUT_S,
)


@dataclass(frozen=True)
class SshTarget:
    host: str
    user: str = ""
    port: int = DEFAULT_SSH_PORT

    def destination(self) -> str:
        """``user@host``, or bare host so ~/.ssh/config can supply the user."""
        host = self.host.strip()
        user = self.user.strip()
        return f"{user}@{host}" if user else host

    def argv(self, command: str) -> list[str]:
        argv = ["ssh", *SSH_BATCH_OPTIONS,
                "-o", f"ConnectTimeout={SSH_CONNECT_TIMEOUT_S}"]
        # Only pass -p when it differs from the default, so a Port line in
        # ~/.ssh/config is not overridden by the spin box sitting at 22.
        if self.port and self.port != DEFAULT_SSH_PORT:
            argv += ["-p", str(self.port)]
        argv.append(self.destination())
        argv.append(command)
        return argv


@dataclass(frozen=True)
class SshResult:
    command: str
    returncode: int | None
    stdout: str = ""
    stderr: str = ""
    error: str = ""  # set when ssh could not run at all

    @property
    def ok(self) -> bool:
        return not self.error and self.returncode == 0

    def describe(self) -> str:
        if self.error:
            return self.error
        detail = (self.stderr or self.stdout).strip().splitlines()
        tail = detail[-1] if detail else ""
        if self.ok:
            return tail or "ok"
        return f"exit {self.returncode}" + (f": {tail}" if tail else "")


class SshCommand(QThread):
    """Runs one remote command off the GUI thread.

    Every failure mode -- no ssh binary, refused connection, wrong key, remote
    non-zero exit, timeout -- comes back as an :class:`SshResult`, so a broken
    bench link can never raise into the event loop.
    """

    completed = Signal(object)  # SshResult

    def __init__(self, target: SshTarget, command: str,
                 timeout: float = SSH_COMMAND_TIMEOUT_S, parent=None) -> None:
        super().__init__(parent)
        self.target = target
        self.command = command
        self.timeout = timeout

    def run(self) -> None:
        argv = self.target.argv(self.command)
        try:
            proc = subprocess.run(
                argv, capture_output=True, text=True, timeout=self.timeout
            )
        except FileNotFoundError:
            result = SshResult(self.command, None,
                               error="ssh client not found on PATH")
        except subprocess.TimeoutExpired:
            result = SshResult(self.command, None,
                               error=f"ssh timed out after {self.timeout:g} s")
        except Exception as exc:  # subprocess raises several unrelated types
            result = SshResult(self.command, None, error=f"ssh failed: {exc}")
        else:
            result = SshResult(self.command, proc.returncode,
                               proc.stdout, proc.stderr)
        self.completed.emit(result)
