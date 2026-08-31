# BLDC Linux motor control

This layer provides two small C programs for a Raspberry Pi 4 talking to an
STM32F767 over a dedicated 115200 8N1 UART:

```text
motorctl -> /run/motor-supervisor.sock -> motor-supervisor -> STM32 UART
```

`motor-supervisor` is the sole UART owner. `motorctl` only speaks the local
newline-delimited Unix-socket protocol; it contains no UART access. The STM32
remains the authoritative motor state machine. A process lock at
`/run/motor-supervisor.lock` plus `TIOCEXCL` prevents two supervisor instances
from competing for the interface. One `poll()` loop owns UART receive, one
active local client, and all monotonic deadlines; there are no threads and no
dynamic allocations.

## Safety behavior

- Startup and restart send `GET_STATE` to synchronize. They never send
  `ENABLE`, `SET_MODE`, or `CLEAR_FAULT`, and no prior command is replayed.
- `ENABLE` and `CLEAR_FAULT` are sent only for an explicit `motorctl` request.
- Mutating commands are gated until initial synchronization, except an explicit
  `DISABLE`, which remains available as a safe-stop request.
- Every mutating request is sent once, followed after at least 20 ms by
  `GET_STATE`. Mutating requests are never retried after an ambiguous result.
- `SET_TARGET` has no STM32 acknowledgement. A successful follow-up state query
  therefore returns `WARN TARGET_SENT_NO_ACK`, not a false acknowledgement.
- Heartbeats are sent every 100 ms and state is queried every 500 ms. Three
  consecutive 100 ms `GET_STATE` response timeouts make the process exit
  non-zero so systemd can restart it. Linux does not invent a local fallback or
  duplicate the STM32's 500 ms communication-timeout policy.
- `SIGINT` and `SIGTERM` stop new work and send one best-effort `DISABLE` before
  a clean exit. Because the unit uses `Restart=on-failure`, `systemctl stop`
  stays stopped. A later start synchronizes state but never restarts the motor.

The service socket is mode `0660`. Run `motorctl` with suitable privileges or
adapt ownership in the image for the intended operator group.

## Native/WSL build and tests

No external libraries are required; a C11 compiler and POSIX/Linux headers are
enough.

```sh
cd meta-bldc/recipes-bldc/motor-control/files
make clean
make
make test
```

The protocol test verifies all supplied CRC vectors, little-endian frame
encoding, corruption rejection, state-response validation, and stream parser
resynchronization.

Run the PTY STM32 emulator integration test from the repository root:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 tests/supervisor_integration.py
```

It builds isolated test binaries, checks every motorctl command, observes
heartbeat/state-query timing under sustained RX noise, verifies that startup
sends no mode/enable/fault clear command, checks SIGTERM DISABLE, and proves that
three lost state queries or a disconnected UART cause a nonzero supervisor exit.

For manual hardware use (normally run as root because of UART and `/run`
permissions):

```sh
./motor-supervisor
./motor-supervisor --device /dev/ttyUSB0
```

In another terminal:

```sh
./motorctl status
./motorctl mode local
./motorctl mode remote
./motorctl enable
./motorctl disable
./motorctl target 30 2000
./motorctl clear-fault
```

`motorctl` exits non-zero on usage errors, transport failures, and `ERROR`
responses. `WARN TARGET_SENT_NO_ACK` exits zero because the target frame and
authoritative follow-up query completed, while explicitly stating that no target
acknowledgement exists.

## Yocto integration

Add the layer, ensure the distribution uses systemd, and install the recipe in
the image:

```sh
bitbake-layers add-layer /path/to/meta-bldc
```

```bitbake
IMAGE_INSTALL:append = " motor-control"
```

Then build the image normally. The recipe deliberately does not run `make test`
during cross-compilation because the resulting target binary cannot generally
execute on the build host.

The recipe installs and enables `motor-supervisor.service`. Follow logs with:

```sh
journalctl -u motor-supervisor -f
```

The layer does not configure Raspberry Pi UART overlays, pin multiplexing,
aliases, or console ownership. The image/platform configuration must provide
`/dev/serial0` and dedicate it to the STM32; use `--device` for a different
device during manual runs. If the active Yocto release is not listed in
`LAYERSERIES_COMPAT_bldc`, add that release codename after validation.

## Raspberry Pi acceptance sequence

The following block performs synchronization, normal control, a five-minute
status/stability observation, then the SIGKILL fail-safe test. Run it only with
the motor mechanically and electrically prepared for a 30% duty test.

```sh
set -eu
systemctl restart motor-supervisor
sleep 2
systemctl is-active motor-supervisor
motorctl status

motorctl mode remote
motorctl enable
motorctl target 30 2000

stable_pid="$(pidof motor-supervisor)"
i=0
while [ "$i" -lt 60 ]; do
    state="$(motorctl status)"
    echo "$state"
    case "$state" in
        *"STATE=RUN MODE=REMOTE FAULT=NONE"*) ;;
        *) exit 1 ;;
    esac
    sleep 5
    i=$((i + 1))
done
[ "$(pidof motor-supervisor)" = "$stable_pid" ]

supervisor_pid="$(pidof motor-supervisor)"
kill -KILL "$supervisor_pid"
sleep 3
systemctl is-active motor-supervisor
state="$(motorctl status)"
echo "$state"
journalctl -u motor-supervisor -n 80 --no-pager
case "$state" in
    *"STATE=FAULT MODE=REMOTE FAULT=COMM"*) ;;
    *) exit 1 ;;
esac
```

After SIGKILL the expected final status is STM32 `FAULT/COMM`, never automatic
`READY` or `RUN`. Clear and restart operation only with explicit commands:

```sh
motorctl clear-fault
motorctl status
motorctl enable
```

## Local socket protocol

Each connection carries one newline-terminated command and one response:

```text
STATUS
MODE LOCAL
MODE REMOTE
ENABLE
DISABLE
TARGET <percent 0..100> <ramp_ms 0..10000>
CLEAR_FAULT
```

Responses begin with `OK`, `WARN`, or `ERROR` and include the most recent
authoritative state when a valid `RSP_STATE` was received.
