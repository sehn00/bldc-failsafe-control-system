# BLDC Diagnostic Console

Real-time telemetry viewer, semi-automated fault verification, and repeated-trial
analysis for the STM32 BLDC USART3 stream.

The console does two things at once. It captures and plots the telemetry, and it
drives repeated SIGKILL fault-injection trials against the Raspberry Pi
supervisor over SSH while that same capture keeps running underneath.

```
PC / BLDC Diagnostic Console
├─ USB-UART → STM32 USART3 telemetry (9600 bps, positional CSV)
└─ Wi-Fi / SSH → Raspberry Pi
                     ↓ UART
                   STM32
                     ↓
                    BLDC
```

## Install & run

```bash
git clone https://github.com/<user>/bldc-diagnostic-console.git
cd bldc-diagnostic-console

python -m venv .venv
.venv/bin/pip install -r requirements.txt        # Windows: .venv\Scripts\pip
.venv/bin/python -m bldc_diagnostic_console      # Windows: .venv\Scripts\python
```

Pick the port, leave Baud at 9600, press **Connect**.

## Log format

The firmware sends its header once at boot and then a `CUR` row every 500 ms:

```
TYPE,RAW_PK_A,FILT_PK_A,FILT_AVG_A,MAX_FLTCNT,DUTY,VDC,RPM,STATE,FAULT
CUR,0.780,0.490,0.150,0,9,23.98,380,2,0
```

Because the header is boot-only, the column order above is compiled into
`constants.DEFAULT_COLUMNS` — attaching mid-stream parses correctly without it.
A header line, if seen, just refreshes the order.

Lines that are neither `TYPE,` nor `CUR,` (boot banners, warnings) go to the raw
log only. Rows with the wrong field count or a non-numeric field are counted in
the `⚠ bad lines` status and dropped.

`STATE` and `FAULT` are plain enumerations, not bitmasks:

| STATE | | FAULT | |
|---|---|---|---|
| 0 | INIT | 0 | NONE |
| 1 | READY | 1 | OVERCURRENT |
| 2 | RUN | 2 | OVERTEMP |
| 3 | FAULT | 3 | COMM |

`DUTY` is a percentage (`VoltageRef / CNT_MAX * 100`).

## Telemetry tab

Five X-linked panels stacked top to bottom — Current (RAW/FILT_PK/FILT_AVG),
RPM, Duty, Vdc, State/Fault — so any event lines up vertically across all
signals. `MAX_FLTCNT` has a sixth panel, hidden by default; a panel disappears
whenever all of its series are unchecked. A thin rule separates the panels.

That sixth panel is titled **OC Fault Count** on screen — the short name for
`MAX_FLTCNT`, the maximum value reached by the firmware's overcurrent fault
counter, i.e. the counter whose threshold raises `FAULT = OVERCURRENT`. Only the
on-screen title is shortened: the telemetry field, the parsed column and the CSV
column all stay `MAX_FLTCNT`.

State and Fault share the bottom panel as staircases (never interpolated), with
Fault drawn offset above State so the two 0..3 enums do not collide. Each
`FAULT: 0 → non-zero` transition drops a dashed marker through every panel.

Each panel's Y axis is fixed to what the signal can physically do, and no axis
uses an SI prefix, so ticks are always read in the unit named on the label:

| Panel | Y axis |
|---|---|
| Current | 0 A upward, autoscaling with 10 % headroom, never narrower than 1 A |
| RPM | 0 rpm upward, autoscaling, never narrower than 100 rpm |
| Duty | fixed 0–100 % |
| Vdc | fixed 0–30 V for the 24 V bus; mouse zoom stays enabled |
| State / Fault | fixed categorical slots, so the labels never move |
| OC Fault Count (`MAX_FLTCNT`) | 0 upward, autoscaling, never narrower than 5 counts |

### Controls

- **Pause** — freezes the drawing only; capture keeps running underneath.
- **Last N** — how many recent samples are drawn. Nothing is discarded; the full
  capture stays in memory and in the CSV.
- **Reset** — drops all captured samples and markers.
- **Save CSV** — parsed wide numeric table (`t_iso,t_elapsed_s,` then one column
  per series). Opens directly in pandas or Excel.
- **Save Raw Log** — the received text, unparsed, as a separate `.log`.

Timestamps come from PC receive time, since the stream carries none. At 500 ms
that is fine for trends; do not read latency off it.

## Verification tab

Repeated SIGKILL trials against the Pi supervisor. Capture, plotting, raw log
and CSV all keep running while a trial is in flight — both tabs read one store.

**SSH.** Commands go through the system OpenSSH client as a subprocess, so
existing keys, `~/.ssh/config` aliases, agents and jump hosts work unchanged.
No password is stored, prompted for, or accepted: `BatchMode=yes` makes ssh fail
rather than block on a prompt. Leave *Username* blank to let `~/.ssh/config`
supply it; leave the port at 22 so a `Port` line there is not overridden. Every
SSH call runs on a worker thread — a dead link never freezes the GUI.

**Preconditions.** *Run Fault Test* refuses to inject unless telemetry is
connected, `STATE = RUN` and `FAULT = NONE`. When it refuses it shows what it
actually observed and why it stopped.

**The trial.** The console sends one command and then only reads:

1. Timestamps the injection and sends the kill over SSH.
2. Waits for `STATE = FAULT` **and** `FAULT = COMM` (fault-detect timeout).
3. Holds for the auto-run window. `STATE = FAULT` **and** `FAULT = COMM` must
   still be true when the window closes; leaving that latch at any point during
   it **FAILS** the trial:
   - back to `RUN` (systemd restarted the supervisor) records
     `Auto Run Detected = YES`;
   - anything else — `READY / NONE` from a manual `CLEAR_FAULT`, or the fault
     changing off `COMM` — keeps `Auto Run Detected = NO` and says so in the
     Note, e.g. *Fault latch cleared during watch window: STATE=READY /
     FAULT=NONE after 2500 ms*.

   Do not recover manually while this window is open — a latch that did not
   survive the window is not evidence that it would have.
4. Records the trial and stops.

Recovery is yours: `CLEAR_FAULT → ENABLE → RUN` with motorctl between trials.
The console never commands the motor — that is what makes it *semi*-automated.

An injection that never ran (SSH refused, permission denied, supervisor absent) is
reported and **not** recorded, so it cannot pollute the statistics.

### Batch test

*Run Batch* repeats the whole manual procedure — **Batch count** defaults to 20
and accepts far more — while *Current trial n / N* tracks where it is. Each
trial is:

1. Wait until the supervisor is accepting commands again (below).
2. `motorctl clear-fault`, then wait for `STATE = READY` / `FAULT = NONE`.
3. `motorctl enable`.
4. `motorctl target 30 1000`, then wait for `STATE = RUN` / `FAULT = NONE`.
5. Settle 2 s in RUN, so the kill does not land on a motor still spinning up.
6. The same kill, detect timeout, watch window, PASS rule, delay measurement
   and trial row as a single trial — a batch trial *is* a trial, and lands in
   the same table, statistics, charts and CSV.

The next `clear-fault` is sent only after the previous trial's watch window has
closed and its row is recorded. The commands and the windows live in
`verification/config.py`.

**Nothing is retried, and any bench problem stops the batch** — a command that
could not run, `READY` or `RUN` that does not arrive within 8 s, a supervisor
that stays down, a dropped serial link. Retrying would fold a bench fault into
the campaign statistics. A trial that FAILs is a *result*, not a bench problem,
so the batch carries on and the reason stays in that trial's Note.

#### Telemetry decides, not the exit code

A `motorctl` exit code is not a verdict on the STM32. On the bench:

```
motorctl enable          -> ERROR GET_STATE timed out
motorctl target 30 1000  -> WARN TARGET_SENT_NO_ACK STATE=RUN MODE=REMOTE FAULT=NONE
motorctl status          -> OK STATE=RUN MODE=REMOTE FAULT=NONE
```

Both commands had been applied — the motor was turning — and only motorctl's
own follow-up check had timed out. So the batch separates *did the command
run* from *did the system reach the state*:

- **Never ran** — no ssh, refused connection, `command not found`, no
  `/run/motor-supervisor.sock`, or any exit nobody recognises: the batch stops.
  Guessing that an unknown error was harmless is how a broken bench becomes
  data.
- **Sent but unconfirmed** — the `GET_STATE` / `ACK` timeouts listed in
  `MOTORCTL_UNCONFIRMED_MARKERS`: reported in the batch status, written into
  that trial's Note (`setup: motorctl enable unconfirmed (…)`), and left for
  the telemetry post-condition to settle. If `READY` or `RUN` then fails to
  arrive, the batch stops there instead.
- **Sent but unconfirmed, for one command only** — `MOTORCTL_COMMAND_UNCONFIRMED_MARKERS`.
  `motorctl enable` reports

  ```
  ERROR ENABLE did not result in RUN; STATE=READY MODE=REMOTE FAULT=NONE
  ```

  because it expects RUN of its own accord. In this procedure it should not:
  `ENABLE` readies the driver and `RUN` arrives with the `TARGET` that follows,
  so a board sitting in `READY` / `REMOTE` / `NONE` is exactly where `ENABLE`
  is supposed to leave it. The setup carries on to `TARGET`, and the `RUN`
  check after it decides. The reading is scoped to that exact command and that
  exact state — the same sentence from `motorctl target`, or from a board that
  is faulted, still stops the batch.

The post-condition — `STATE`/`FAULT` out of the STM32 itself — is what actually
passes a setup step. `verification/motorctl.py` holds the classification.

- **Refused while the supervisor was out of step** —
  `ERROR supervisor has not synchronized STM32 state`. Handled separately; see
  below.

The exit codes themselves come from `motorctl.c`: an `OK` or `WARN` reply is
exit 0, an `ERROR` reply is exit 1, and motorctl's own failure to reach the
supervisor socket is exit 2. So only exit 1 has text worth reading; 2, 126, 127
and 255 always stop the batch.

#### The supervisor sync guard

`motor-supervisor.c` keeps a `synchronized` flag: a `GET_STATE` timeout clears
it, any completed `GET_STATE` sets it, and it polls one every 500 ms. While it
is clear, `schedule_work` answers a mutating request with

```
ERROR supervisor has not synchronized STM32 state
```

and **returns before writing the UART frame** — so unlike an unconfirmed
command, this one provably never reached the STM32 and is never read as
applied. Three consecutive query failures instead make the supervisor exit,
which the readiness probe then sees.

The batch treats it as recoverable, exactly once per command per trial:

1. If telemetry already shows the state that step wanted, the command is not
   needed — carry on, and note `… unconfirmed due to supervisor resync`.
2. Otherwise wait for the supervisor to come back in step (below) and re-send
   that one command once, noting `… retried once after supervisor sync loss`.
3. If the same command is refused again, or the supervisor does not come back
   inside the readiness timeout, the batch stops. There is no third attempt.

The SIGKILL injection is never re-sent — recovery lives entirely in setup,
before the injection, so it cannot touch a measured delay.

#### Waiting for the supervisor

Before the first setup command of every trial — and again after a sync refusal
— the batch runs

```
test -S /run/motor-supervisor.sock && motorctl status
```

and requires **two consecutive replies carrying a real `STATE=… MODE=… FAULT=…`
line**, not merely exit 0. That is the strong form of the check: the supervisor
answers `status` from the same completion path that sets its `synchronized`
flag, so a state line is direct evidence that it and the STM32 are in step,
which the socket file existing is not. A probe without a state line restarts the
run of confirmations; polling continues every second for up to 20 s, after which
the batch stops rather than fire commands at a stale supervisor.

Every mutating step is sent through that same gate, so gating more of them is a
routing change, not a redesign. All of it happens between trials or during
setup — outside the injection → `COMM` interval — so none of it can affect a
measured delay. A trial that needed extra probes, a resync or a retry says so in
its Note, and the batch box keeps a running **Setup anomalies** count for the
campaign.

*Stop Batch* never cuts a watch window short: a trial in flight is finished and
recorded, and no new trial is started. Stopping between trials is immediate. If
you stop after `target` has gone out, the motor may still be turning — the
console says so and leaves it to you, since it sends no motor command of its
own beyond the three above.

Both events drop a labelled marker through every telemetry panel — `inject #n`
dash-dot, `COMM #n` dotted — so an odd trial can be traced back to the signals
and the raw log at that instant.

### PC-observed Fault Transition Delay

The measured interval is **PC sent the kill → PC saw `FAULT = COMM` in the
telemetry**. It contains the SSH round trip and up to one ~500 ms telemetry
frame, and it is named in full everywhere it appears.

**It is not a PWM-off latency.** It is a number for comparing trials against
each other, not a physical measurement of when the bridge stopped switching.

### Analysis

Statistics over trials that produced a measured delay: N, mean, median, standard
deviation (n−1), min, max, P95 (linear interpolation), plus PASS rate over all
recorded trials.

- **Histogram** — delay against frequency, `numpy` `bins="auto"`.
- **I-MR chart** — individuals and moving range, in trial order. Limits use the
  standard estimator, not an ad-hoc 3σ: sigma comes from the mean moving range,
  `MR̄ / d₂` with `d₂ = 1.128` at subgroup size 2, giving
  `X̄ ± 2.660·MR̄` and `UCL_MR = 3.267·MR̄`. Points outside the limits (Nelson
  rule 1) are ringed in red.

Process capability (Cp/Cpk) is deliberately absent — the question here is
run-to-run variability and odd trials, not conformance to a spec width.

**Export Trials CSV** writes Trial, Test Type, Injection Time, Fault Observed
Time, the delay, Final State, Final Fault, Auto Run Detected, Result and Note.

The trials table heads the delay column **PC-observed COMM Delay [ms]** purely
to leave the Note room to breathe; the measurement is unchanged, and the CSV
column, the chart axes and the header tooltip all keep the full
*PC-observed Fault Transition Delay*. The Note column takes whatever width is
left, elides rather than wraps, and carries its whole text on the cell tooltip.

### Retargeting the remote command

The default lives in `verification/config.py` and is editable per session in the
*Remote command* box:

```python
FAULT_INJECT_COMMAND = "pkill -KILL -f '^/usr/bin/motor-supervisor( |$)'"
```

The match is on the full command line (`-f`), not the process name (`-x`): the
bench target runs BusyBox `pkill` under Yocto, where the name match did not fire
reliably for `motor-supervisor`. The supervisor actually runs as

```
/usr/bin/motor-supervisor --device /dev/serial0
```

so the pattern anchors the absolute path at the start and requires a space or
end-of-line after it — that way a longer path ending in the same name will not
be caught by accident.

There is no `sudo`: the bench logs in as root. Whatever you put in this box must
run without prompting, since the console stores no password and a prompt would
hang the SSH channel — if you retarget it to a non-root account, use the
non-interactive `sudo -n` with a NOPASSWD sudoers entry for the exact command.

The same file holds the SSH timeouts, the fault-detect timeout, the auto-run
watch window, and the batch test's `motorctl` commands, state-confirm timeout,
settle time, default count, supervisor readiness probe and the marker lists
that separate an unconfirmed command from one that never ran.

## Testing without a board

```bash
python tools/fake_stream.py       # prints a /dev/pts/N path (Linux/macOS)
```

Paste that path into the Port box — it accepts a typed device path — and press
Connect. The generator spins up, runs, trips overcurrent, and recovers.

The unit suite needs no hardware, no display and no SSH target:

```bash
.venv/bin/python -m unittest discover -s tests -t .
```

## Layout

```
bldc_diagnostic_console/
├── constants.py      column order, enum labels, group/series/axis definitions
├── parser.py         line -> values, header handling
├── data_store.py     sample accumulation, fault edge detection
├── serial_reader.py  QThread: read, frame on newline, emit lines
├── csv_writer.py     capture CSV / raw log export
├── verification/
│   ├── config.py     remote commands, SSH options, observation windows
│   ├── ssh.py        OpenSSH subprocess on a worker thread
│   ├── runner.py     trial state machine: inject -> detect -> watch -> score
│   ├── batch.py      repeats the manual clear/enable/target/kill sequence
│   ├── motorctl.py   exit code -> ran / unconfirmed / desynced / never ran
│   ├── trial.py      TrialRecord, TrialLog, trial CSV
│   └── stats.py      summary statistics, I-MR control limits
└── ui/
    ├── main_window.py       connection bar, tabs, 200 ms drain+redraw tick
    ├── plot_area.py         stacked X-linked panels, staircases, markers
    ├── series_panel.py      series on/off
    ├── log_view.py          raw receive log (capped at 5000 lines)
    ├── verification_panel.py SSH settings, trial table, statistics
    └── analysis_plots.py    delay histogram, I-MR charts
```

Deliberately out of scope: profile.json, fault bit decoding, derived channels,
offline replay, high-rate telemetry, pre/post-event export, FFT, ML anomaly
detection, Cp/Cpk, and any PC-side re-filtering of the telemetry.
