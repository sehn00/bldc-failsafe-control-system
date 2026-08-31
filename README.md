# BLDC Fail-Safe Control System

A 24 V BLDC motor control and fail-safe system integrating:

- STM32F767 real-time motor control
- Raspberry Pi embedded Linux supervision
- Yocto custom layer and systemd integration
- PC diagnostic console
- automated fault injection and data analysis

## Repository Structure

| Path | Role |
|---|---|
| `firmware/stm32` | STM32F767 real-time motor control firmware. Placeholder: source is not in this repository yet. |
| `embedded_linux/meta_bldc` | Yocto layer for the Raspberry Pi target. Holds the `motor-control` recipe, whose `files/` directory is the canonical location of the supervisor sources: `motor-supervisor.c`, `motorctl.c`, `protocol.c` / `protocol.h`, the `Makefile` and the systemd unit. |
| `embedded_linux/tests` | Host-side integration test driving the supervisor over its client socket. |
| `tools/diagnostic_console` | PC diagnostic console (PySide6): serial telemetry parsing and visualization, CSV and raw logging, SIGKILL fault-injection trials, and the statistics behind the campaign. |
| `results/sigkill_100` | Data from the final 100-run SIGKILL campaign: trial table, telemetry CSV and raw telemetry log. |
| `docs` | Validation and troubleshooting notes. |

The Yocto layer keeps its upstream directory and metadata naming (`meta-bldc`
conventions, `recipes-bldc/`, `motor-control_1.0.bb`) so recipe and build
references stay valid; only the wrapper directory is named in snake_case.

## Key Validation

- Control-loop ISR latency improved from 15.6 us to 7.6 us
- Linux supervisor SIGKILL triggered STM32 COMM fault and latched safe-stop
- 100/100 repeated SIGKILL fault-injection trials passed
- Automatic motor restart after fault: 0/100
- Histogram and I-MR analysis were used to inspect trial-to-trial variation

### How to read the delay figure

The delay recorded per trial is a **PC-observed fault transition delay**: the
interval between the console issuing the kill over SSH and the console seeing
the fault in telemetry. It includes host and SSH execution time and the
telemetry sampling interval. It is **not** the STM32 fault-detection latency and
**not** the physical PWM-off latency, and must not be quoted as either.

## Status

The STM32F767 firmware source has not been added to this repository yet; the
firmware directory is a placeholder. Everything else — the Yocto layer, the
supervisor and `motorctl` sources, the diagnostic console and the campaign data
— is present.
