"""SSH argv construction and every failure path a bench link can produce."""

import subprocess
import unittest
from unittest import mock

from bldc_diagnostic_console.verification.config import FAULT_INJECT_COMMAND
from bldc_diagnostic_console.verification.ssh import SshCommand, SshResult, SshTarget

KILL = FAULT_INJECT_COMMAND


class SshTargetTest(unittest.TestCase):
    def test_user_at_host(self) -> None:
        self.assertEqual(SshTarget("pi-bench", "pi").destination(), "pi@pi-bench")

    def test_blank_user_defers_to_ssh_config(self) -> None:
        self.assertEqual(SshTarget("pi-bench").destination(), "pi-bench")

    def test_default_port_is_not_forced(self) -> None:
        """A Port line in ~/.ssh/config must survive the spin box sitting at 22."""
        self.assertNotIn("-p", SshTarget("pi-bench", "pi", 22).argv(KILL))

    def test_non_default_port_is_passed(self) -> None:
        argv = SshTarget("pi-bench", "pi", 2222).argv(KILL)
        self.assertEqual(argv[argv.index("-p") + 1], "2222")

    def test_batch_mode_and_timeout(self) -> None:
        argv = SshTarget("pi-bench", "pi").argv(KILL)
        self.assertEqual(argv[0], "ssh")
        self.assertIn("BatchMode=yes", argv)      # fail, never prompt
        self.assertTrue(any(a.startswith("ConnectTimeout=") for a in argv))

    def test_command_is_last_and_verbatim(self) -> None:
        argv = SshTarget("pi-bench", "pi").argv(KILL)
        self.assertEqual(argv[-1], KILL)
        self.assertEqual(argv[-2], "pi@pi-bench")

    def test_no_password_anywhere_in_the_invocation(self) -> None:
        argv = SshTarget("pi-bench", "pi").argv(KILL)
        joined = " ".join(argv).lower()
        for banned in ("sshpass", "password", "-o passwordauthentication=yes"):
            self.assertNotIn(banned, joined)
        # The default SSHes in as root and carries no sudo at all. Should one
        # ever come back, it has to be the non-interactive form: a password
        # prompt cannot be answered and would hang the channel.
        if "sudo" in KILL:
            self.assertIn("sudo -n", KILL)


class SshResultTest(unittest.TestCase):
    def test_ok_only_on_clean_exit(self) -> None:
        self.assertTrue(SshResult("c", 0).ok)
        self.assertFalse(SshResult("c", 1).ok)
        self.assertFalse(SshResult("c", 0, error="boom").ok)

    def test_describe_surfaces_the_remote_error(self) -> None:
        result = SshResult("c", 1, stderr="sudo: a password is required\n")
        self.assertIn("exit 1", result.describe())
        self.assertIn("password is required", result.describe())


class SshCommandTest(unittest.TestCase):
    """Every failure must arrive as a result object, never as an exception."""

    def _run(self, **patch) -> SshResult:
        captured = []
        command = SshCommand(SshTarget("pi-bench", "pi"), KILL)
        command.completed.connect(captured.append)
        with mock.patch("subprocess.run", **patch):
            command.run()
        self.assertEqual(len(captured), 1)
        return captured[0]

    def test_success(self) -> None:
        proc = subprocess.CompletedProcess([], 0, "ok\n", "")
        result = self._run(return_value=proc)
        self.assertTrue(result.ok)
        self.assertEqual(result.stdout, "ok\n")

    def test_remote_non_zero_exit(self) -> None:
        """pkill exits 1 when the supervisor was not running."""
        proc = subprocess.CompletedProcess([], 1, "", "")
        result = self._run(return_value=proc)
        self.assertFalse(result.ok)
        self.assertEqual(result.returncode, 1)

    def test_missing_ssh_binary(self) -> None:
        result = self._run(side_effect=FileNotFoundError())
        self.assertFalse(result.ok)
        self.assertIn("ssh client not found", result.error)

    def test_timeout(self) -> None:
        result = self._run(side_effect=subprocess.TimeoutExpired("ssh", 15))
        self.assertFalse(result.ok)
        self.assertIn("timed out", result.error)

    def test_unexpected_exception_is_contained(self) -> None:
        result = self._run(side_effect=OSError("no route to host"))
        self.assertFalse(result.ok)
        self.assertIn("no route to host", result.error)


if __name__ == "__main__":
    unittest.main()
