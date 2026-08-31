# Validation Report

## 1. Control-Loop Timing

The original STM32 control ISR used blocking ADC polling.

Oscilloscope measurement:

| Version | ISR execution time |
|---|---:|
| Before | 15.6 µs |
| After ADC + DMA redesign | 7.6 µs |

The ADC path was changed to TIM1-triggered triple ADC conversion with DMA double buffering, removing blocking conversion waits from the ISR.

## 2. Fail-Safe Functional Test

The Linux `motor-supervisor` process was intentionally terminated with SIGKILL while the motor was running in REMOTE mode.

Expected sequence:

```text
motor-supervisor SIGKILL
→ Heartbeat loss
→ STM32 COMM fault
→ PWM disabled
→ Motor safe-stop
→ FAULT latched
```

After systemd restarted the supervisor, the STM32 remained in `FAULT / COMM`.

The motor did **not** automatically restart. Recovery required explicit `CLEAR_FAULT` followed by `ENABLE`.

## 3. Repeated Fault-Injection Test

The Diagnostic Console automated the same SIGKILL scenario for 100 trials.

Final result:

- **100/100 PASS**
- **100/100 entered FAULT / COMM**
- **0/100 automatic motor restart**

Each trial recorded:

- Injection time
- PC-observed fault transition delay
- Final state / fault
- Auto-run detection
- PASS / FAIL
- Setup notes

## 4. Data Analysis

The 100 trial delays were analyzed using summary statistics, Histogram, and I-MR charts.

Key statistics:

| Metric | Value |
|---|---:|
| Mean | 1414.46 ms |
| Median | 1400.05 ms |
| Std | 165.95 ms |
| Min | 899.0 ms |
| Max | 2000.3 ms |
| P95 | 1500.3 ms |

Most measurements were concentrated around **1.4–1.5 s**, while a few values appeared near **1.0 s** and **2.0 s**.

The Histogram was used to identify unusual values, and the I-MR chart was used to locate when those variations occurred.

For example, Trial 67 recorded approximately **2000.3 ms**. The trial CSV, telemetry CSV, and raw telemetry log were then compared to verify the actual state transition and motor response.

This drill-down confirmed:

- `RUN / NONE → FAULT / COMM`
- Duty dropped to 0
- RPM decreased as the motor coasted down
- The raw serial data matched the processed telemetry

The analysis also showed that a setup communication anomaly was not sufficient evidence to explain every delay outlier, so it was not treated as a confirmed root cause.

## 5. Measurement Limitation

The recorded value is a **PC-observed fault transition delay**, not the STM32's exact internal fault-detection or physical PWM-off latency.

It includes:

```text
PC injection request
→ SSH execution
→ Linux process termination
→ Heartbeat loss
→ STM32 fault detection
→ Telemetry transmission
→ PC observation
```

Telemetry is sampled at approximately 500 ms intervals, so host-side execution and sampling phase contribute to the measured variation.

For precise physical response timing, the next step would be direct oscilloscope measurement between a heartbeat-loss reference and the PWM-off point.
