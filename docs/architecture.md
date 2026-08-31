# System Architecture

## Overview

This project is a 24 V BLDC motor control and fail-safe system composed of three layers:

- **STM32F767**: real-time motor control and final fail-safe authority
- **Raspberry Pi 4 / Yocto**: high-level supervision and heartbeat transmission
- **PC Diagnostic Console**: telemetry monitoring, fault injection, logging, and analysis

```text
PC Diagnostic Console
        │
        │ SSH / Telemetry
        ▼
Raspberry Pi 4 / Yocto
motor-supervisor + motorctl
        │
        │ UART2, 115200 bps
        ▼
STM32F767
Real-time Control + Fail-Safe
        │
        ▼
24 V BLDC Motor
```

## Responsibility Split

### STM32F767

- PWM generation and motor control
- Hall-based RPM measurement
- Current / DC-bus voltage sensing
- Local fault detection
- Heartbeat timeout monitoring
- Fault latch and independent PWM shutdown

The STM32 is responsible for placing the motor in a safe state even if the upper Linux layer fails.

### Raspberry Pi / Embedded Linux

- High-level motor commands
- LOCAL / REMOTE mode control
- Target setting
- Periodic heartbeat transmission
- STM32 state monitoring
- `motorctl` CLI
- `motor-supervisor` recovery through systemd

### PC Diagnostic Console

- Current / RPM / Duty / Vdc / State / Fault visualization
- CSV and raw telemetry logging
- SIGKILL fault injection
- Repeated batch validation
- Histogram and I-MR analysis

## Fail-Safe State Flow

```text
READY
  │ ENABLE
  ▼
RUN
  │ Fault / Heartbeat Timeout
  ▼
FAULT
  │ CLEAR_FAULT
  ▼
READY
```

- `FAULT → RUN` is not allowed directly.
- `CLEAR_FAULT` only returns the system to `READY`.
- A separate `ENABLE` is required to run again.
- Restarting the Linux supervisor does not automatically restart the motor.

## Heartbeat Fail-Safe

While operating in REMOTE mode, the STM32 monitors periodic heartbeat messages from the Raspberry Pi.

```text
Supervisor failure
→ Heartbeat stops
→ STM32 timeout
→ FAULT_COMM
→ PWM disabled
→ FAULT latched
```

This allows the STM32 to react to loss of supervisory control independently of the Linux process.

## UART Protocol

Raspberry Pi and STM32 communicate using an 8-byte UART frame:

```text
SOF | CMD | DATA[4] | CRC16
```

Main commands:

- `GET_STATE`
- `SET_MODE`
- `ENABLE`
- `DISABLE`
- `SET_TARGET`
- `HEARTBEAT`
- `CLEAR_FAULT`

CRC16-CCITT is used to validate received frames.

## ADC / DMA Improvement

The original control ISR used sequential ADC polling.

It was changed to:

```text
TIM1 Trigger
→ ADC Triple Simultaneous Conversion
→ DMA Double Buffer
→ Control ISR reads completed frame
```

The measured control ISR execution time was reduced from **15.6 µs to 7.6 µs**, increasing real-time timing margin.
