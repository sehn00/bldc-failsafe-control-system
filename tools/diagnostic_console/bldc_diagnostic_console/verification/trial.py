"""One fault-injection trial, and the CSV form of a campaign."""

import csv
from dataclasses import dataclass
from datetime import datetime

#: The delay column is named in full everywhere it is written or displayed.
#: It spans "PC sent the kill" -> "PC saw COMM in the telemetry", so it carries
#: the SSH round trip and up to one ~500 ms telemetry frame.  It is a
#: trial-to-trial comparison number, never a PWM-off latency.
DELAY_LABEL = "PC-observed Fault Transition Delay"
DELAY_COLUMN = f"{DELAY_LABEL} [ms]"

#: Same measurement, short enough for a table header that has to share its row
#: with the Note column.  The full name stays on the CSV column, the chart axes,
#: the header tooltip and the statistics caveat, so the definition is never the
#: thing that got abbreviated.
DELAY_SHORT_LABEL = "PC-observed COMM Delay"
DELAY_SHORT_COLUMN = f"{DELAY_SHORT_LABEL} [ms]"

TRIAL_CSV_COLUMNS = [
    "Trial",
    "Test Type",
    "Injection Time",
    "Fault Observed Time",
    DELAY_COLUMN,
    "Final State",
    "Final Fault",
    "Auto Run Detected",
    "Result",
    "Note",
]


@dataclass(frozen=True)
class TrialRecord:
    index: int
    test_type: str
    injection_time: datetime
    fault_observed_time: datetime | None
    delay_ms: float | None
    final_state: str
    final_fault: str
    auto_run_detected: bool
    passed: bool
    note: str = ""

    @property
    def result(self) -> str:
        return "PASS" if self.passed else "FAIL"

    def as_row(self) -> list[str]:
        return [
            str(self.index),
            self.test_type,
            _stamp(self.injection_time),
            _stamp(self.fault_observed_time),
            "" if self.delay_ms is None else f"{self.delay_ms:.1f}",
            self.final_state,
            self.final_fault,
            "YES" if self.auto_run_detected else "NO",
            self.result,
            self.note,
        ]


def _stamp(value: datetime | None) -> str:
    return "" if value is None else value.isoformat(timespec="milliseconds")


class TrialLog:
    """Trials accumulate here across a session; nothing is dropped."""

    def __init__(self) -> None:
        self.trials: list[TrialRecord] = []

    def __len__(self) -> int:
        return len(self.trials)

    def next_index(self) -> int:
        return len(self.trials) + 1

    def add(self, trial: TrialRecord) -> None:
        self.trials.append(trial)

    def clear(self) -> None:
        self.trials.clear()

    def delays(self) -> list[float]:
        """Measured delays, in trial order.

        Trials where the fault was never observed have no delay and are absent,
        so they cannot drag a mean or a control limit around.
        """
        return [t.delay_ms for t in self.trials if t.delay_ms is not None]

    def pass_rate(self) -> float | None:
        if not self.trials:
            return None
        return sum(t.passed for t in self.trials) / len(self.trials)


def write_trials_csv(path: str, trials: list[TrialRecord]) -> int:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(TRIAL_CSV_COLUMNS)
        for trial in trials:
            writer.writerow(trial.as_row())
    return len(trials)
