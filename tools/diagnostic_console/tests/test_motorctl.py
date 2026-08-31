"""Reading a motorctl exit code: did the command run, or just go unconfirmed?

The bench cases these were written against::

    motorctl enable          -> ERROR GET_STATE timed out
    motorctl target 30 1000  -> WARN TARGET_SENT_NO_ACK STATE=RUN MODE=REMOTE FAULT=NONE
    motorctl status          -> OK STATE=RUN MODE=REMOTE FAULT=NONE

Both commands had been applied -- the motor was turning -- so their non-zero
exits must not stop a batch.  Everything else still must.
"""

import unittest

from bldc_diagnostic_console.verification.motorctl import (
    EXECUTED,
    FAILED,
    UNCONFIRMED,
    classify,
    transport_failed,
)
from bldc_diagnostic_console.verification.ssh import SshResult


def verdict(returncode, stdout="", stderr="", error=""):
    return classify(SshResult("motorctl enable", returncode, stdout, stderr, error))[0]


class ClassifyTest(unittest.TestCase):
    def test_clean_exit(self) -> None:
        self.assertEqual(verdict(0, stdout="OK STATE=RUN"), EXECUTED)

    # --- sent, but not confirmed ------------------------------------------

    def test_get_state_timeout_seen_on_the_bench(self) -> None:
        self.assertEqual(verdict(1, stderr="ERROR GET_STATE timed out"), UNCONFIRMED)

    def test_target_sent_without_an_ack_seen_on_the_bench(self) -> None:
        self.assertEqual(
            verdict(1, stdout="WARN TARGET_SENT_NO_ACK STATE=RUN MODE=REMOTE FAULT=NONE"),
            UNCONFIRMED,
        )

    def test_markers_are_matched_case_insensitively(self) -> None:
        self.assertEqual(verdict(1, stderr="error: get_state TIMED OUT"), UNCONFIRMED)

    def test_an_ack_timeout_on_stdout_counts(self) -> None:
        # motorctl maps OK/WARN to exit 0 and ERROR to exit 1, so 1 is the only
        # code whose text is ever read; exit 2 is its own "could not reach the
        # supervisor" and is never forgiven.
        self.assertEqual(verdict(1, stdout="ERROR ack timed out after 500 ms"),
                         UNCONFIRMED)

    def test_motorctls_own_client_error_never_ran(self) -> None:
        self.assertEqual(verdict(2, stderr="motorctl: connect: timed out"), FAILED)

    # --- scoped to one command --------------------------------------------

    #: Seen on the bench at trial 6 of a 100-trial batch.
    ENABLE_NO_RUN = (
        "ERROR ENABLE did not result in RUN;\n"
        "   STATE=READY MODE=REMOTE FAULT=NONE"
    )

    def test_enable_not_reaching_run_is_unconfirmed(self) -> None:
        """ENABLE readies the driver; RUN comes with the TARGET after it."""
        result = SshResult("motorctl enable", 1, stderr=self.ENABLE_NO_RUN)
        self.assertEqual(classify(result)[0], UNCONFIRMED)

    def test_that_reading_is_not_extended_to_other_commands(self) -> None:
        result = SshResult("motorctl target 30 1000", 1, stderr=self.ENABLE_NO_RUN)
        self.assertEqual(classify(result)[0], FAILED)

    def test_enable_not_reaching_run_from_a_faulted_board_still_fails(self) -> None:
        """The exception is for a board left exactly where ENABLE should."""
        result = SshResult(
            "motorctl enable", 1,
            stderr="ERROR ENABLE did not result in RUN; "
                   "STATE=FAULT MODE=REMOTE FAULT=OVERCURRENT",
        )
        self.assertEqual(classify(result)[0], FAILED)

    def test_the_whole_message_survives_into_the_detail(self) -> None:
        """A wrapped error must not lose the half that names the problem."""
        detail = classify(SshResult("motorctl enable", 1, stderr=self.ENABLE_NO_RUN))[1]
        self.assertIn("ENABLE did not result in RUN", detail)
        self.assertIn("STATE=READY", detail)
        self.assertIn("exit 1", detail)

    def test_a_very_long_message_is_truncated(self) -> None:
        detail = classify(SshResult("motorctl enable", 1, stderr="x" * 500))[1]
        self.assertLess(len(detail), 250)
        self.assertTrue(detail.endswith("…"))

    # --- never ran ---------------------------------------------------------

    def test_ssh_could_not_run_at_all(self) -> None:
        self.assertEqual(verdict(None, error="ssh client not found on PATH"), FAILED)

    def test_command_not_found(self) -> None:
        self.assertEqual(verdict(127, stderr="sh: motorctl: command not found"), FAILED)

    def test_not_executable(self) -> None:
        self.assertEqual(verdict(126, stderr="permission denied"), FAILED)

    def test_ssh_transport_error(self) -> None:
        self.assertEqual(verdict(255, stderr="Connection closed by remote host"), FAILED)

    def test_missing_supervisor_socket(self) -> None:
        self.assertEqual(
            verdict(1, stderr="connect /run/motor-supervisor.sock: No such file or directory"),
            FAILED,
        )

    def test_supervisor_not_listening(self) -> None:
        self.assertEqual(verdict(1, stderr="motorctl: connection refused"), FAILED)

    def test_an_unrecognised_failure_is_not_forgiven(self) -> None:
        """Guessing that an unknown error was harmless is how bad data gets in."""
        self.assertEqual(verdict(1, stderr="ERROR thermal derate engaged"), FAILED)

    def test_a_socket_error_wins_over_a_missing_ack(self) -> None:
        """A command that never landed cannot have been applied."""
        self.assertEqual(
            verdict(1, stderr="no such file or directory; TARGET_SENT_NO_ACK"),
            FAILED,
        )

    # --- transport ---------------------------------------------------------

    def test_transport_failure_detects_ssh_itself(self) -> None:
        self.assertTrue(transport_failed(SshResult("c", None, error="timed out")))
        self.assertTrue(transport_failed(SshResult("c", 255)))
        self.assertFalse(transport_failed(SshResult("c", 1, stderr="not ready")))
        self.assertFalse(transport_failed(SshResult("c", 0)))


if __name__ == "__main__":
    unittest.main()
