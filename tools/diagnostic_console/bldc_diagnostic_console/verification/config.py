"""Bench-side settings for the SIGKILL fault-injection trials.

Nothing here belongs to the telemetry contract.  These are PC-side test
parameters plus the command text sent to the Raspberry Pi, kept in one file so
the remote environment can be retargeted without touching the state machine.
"""

# --- remote commands -------------------------------------------------------

#: Sent to the Pi to kill the supervisor.  Edit here, or per-session in the
#: Verification tab's "Remote command" box.
#:
#: Matched on the full command line (``-f``) rather than the process name
#: (``-x``): the bench target runs BusyBox pkill under Yocto, where the name
#: match did not fire reliably against "motor-supervisor".  The real process is
#:
#:     /usr/bin/motor-supervisor --device /dev/serial0
#:
#: so the pattern anchors the absolute path at the start and requires the next
#: character to be a space or end-of-line.  That keeps it from also matching a
#: longer path ending in the same name, or an editor holding the file open.
#:
#: No ``sudo``: the bench SSHes in as root, and a non-interactive ``sudo -n``
#: would only add a way to fail.  This program stores no password, by design, so
#: any command put here must run without prompting -- a prompt cannot be
#: answered and would hang the SSH channel.
#:
#: pkill exits 1 when nothing matched, which the console reports as a failed
#: injection instead of recording a trial.
FAULT_INJECT_COMMAND = "pkill -KILL -f '^/usr/bin/motor-supervisor( |$)'"

# --- batch test ------------------------------------------------------------

#: The motorctl steps a person runs by hand between trials.  The batch runner
#: sends exactly these, in this order, and never invents a recovery command of
#: its own.  Retarget them here if the bench CLI is renamed.
MOTORCTL_CLEAR_FAULT = "motorctl clear-fault"
MOTORCTL_ENABLE = "motorctl enable"
#: Duty and rpm as used on the bench for the SIGKILL campaign.
MOTORCTL_TARGET = "motorctl target 30 1000"

#: How long a commanded STATE (READY after clear-fault, RUN after target) has to
#: show up in telemetry before the batch gives up.  Frames arrive every ~500 ms.
STATE_CONFIRM_TIMEOUT_S = 8.0

#: Time spent in RUN before the kill, so the trial does not measure a motor that
#: is still spinning up.
BATCH_SETTLE_S = 2.0

#: motorctl exit codes are not a verdict on the STM32.  A mutating command can
#: reach the supervisor and be applied while motorctl's own follow-up check
#: times out, which is what these markers name -- see verification/motorctl.py.
#: Matched case-insensitively against stderr then stdout.
MOTORCTL_UNCONFIRMED_MARKERS = (
    "get_state timed out",
    "get_state timeout",
    "target_sent_no_ack",
    "no_ack",
    "ack timed out",
    "ack timeout",
)

#: Unconfirmed failures that are specific to one command, keyed by the exact
#: command text.  Each entry is a group of substrings that must *all* appear.
#:
#: `motorctl enable` reports "ENABLE did not result in RUN" because it expects
#: RUN of its own accord.  In this procedure it should not: ENABLE only readies
#: the driver, and RUN arrives with the TARGET that follows.  So that message,
#: together with a board sitting in READY / REMOTE / NONE -- exactly where
#: ENABLE is supposed to leave it -- is not a failure, and the RUN check after
#: TARGET is what decides.  Scoped to this one command on purpose: the same
#: sentence from anything else is still an unrecognised failure.
MOTORCTL_COMMAND_UNCONFIRMED_MARKERS = {
    MOTORCTL_ENABLE: (
        (
            "enable did not result in run",
            "state=ready",
            "mode=remote",
            "fault=none",
        ),
    ),
}

#: The supervisor refuses to forward a mutating command while its own view of
#: the STM32 is stale.  From motor-supervisor.c `schedule_work`: when
#: `!app->synchronized` and the request is not DISABLE it answers this and
#: returns *without* calling write_uart_frame -- so unlike the markers above,
#: the command provably never reached the STM32 and must never be read as
#: applied.  `synchronized` is cleared by a GET_STATE timeout and set again by
#: any completed GET_STATE, and the supervisor polls one every STATE_INTERVAL_MS
#: (500 ms), so this clears on its own; three consecutive query failures instead
#: make it exit, which the readiness probe then sees.
MOTORCTL_DESYNC_MARKERS = ("supervisor has not synchronized stm32 state",)

#: Messages that mean the command never reached the supervisor.  Checked before
#: the markers above, and anything unrecognised stops the batch too.
MOTORCTL_NOT_EXECUTED_MARKERS = (
    "command not found",
    "not found",
    "no such file or directory",
    "permission denied",
    "connection refused",
    "could not connect",
    "cannot connect",
    "socket",
)

# --- supervisor readiness --------------------------------------------------

#: systemd restarts the supervisor after each kill.  Before the next trial's
#: clear-fault the batch checks that its socket is back *and* that motorctl can
#: actually talk to it -- a fixed sleep only guesses.  `status` is read-only, so
#: this probe commands nothing.  It runs between trials, outside the injection
#: -> COMM interval, so it cannot affect a measured delay.
SUPERVISOR_SOCKET = "/run/motor-supervisor.sock"
SUPERVISOR_READY_COMMAND = f"test -S {SUPERVISOR_SOCKET} && motorctl status"
SUPERVISOR_READY_TIMEOUT_S = 20.0
SUPERVISOR_POLL_S = 1.0

#: A ready probe must come back with a real state line, not merely exit 0.
#: `motorctl status` is answered from `complete_query`, which is the same place
#: that sets `synchronized = true`, so an OK carrying these tokens is direct
#: evidence that the supervisor and the STM32 are in step -- which a bare exit
#: code, or the socket file existing, is not.
SUPERVISOR_STATE_TOKENS = ("state=", "mode=", "fault=")

#: Consecutive good probes required before setup commands are sent.  Each probe
#: is its own GET_STATE round trip with the STM32, so two in a row means the
#: link was up twice, not once by luck.
SUPERVISOR_READY_CONFIRMATIONS = 2

DEFAULT_BATCH_COUNT = 20
#: Only a guard against a typo in the spin box; long campaigns are the point.
MAX_BATCH_COUNT = 10000

#: Harmless probe behind "Test Connection".
CONNECTION_TEST_COMMAND = "echo BLDC_CONSOLE_OK"
CONNECTION_TEST_TOKEN = "BLDC_CONSOLE_OK"

#: Recorded in every trial row, so a CSV of mixed campaigns stays readable.
TEST_TYPE = "supervisor SIGKILL"

# --- ssh client ------------------------------------------------------------

DEFAULT_SSH_PORT = 22

#: BatchMode makes ssh fail rather than prompt, which is what keeps a missing
#: key from hanging the worker thread forever.
SSH_BATCH_OPTIONS = [
    "-o", "BatchMode=yes",
    # Unknown host -> trust on first use; a *changed* key still aborts.  Lab
    # convenience: set "yes" here if the bench Pi is on an untrusted network.
    "-o", "StrictHostKeyChecking=accept-new",
]
SSH_CONNECT_TIMEOUT_S = 5
SSH_COMMAND_TIMEOUT_S = 15

# --- observation windows ---------------------------------------------------

#: How long to wait for STATE=FAULT / FAULT=COMM after the kill is sent.
#: Telemetry arrives every ~500 ms, so this is many frames of margin.
FAULT_DETECT_TIMEOUT_S = 8.0

#: After the fault is seen, how long to keep watching for an unexpected return
#: to RUN (systemd restarts the supervisor; the STM32 must stay latched).
AUTO_RUN_WATCH_S = 10.0
