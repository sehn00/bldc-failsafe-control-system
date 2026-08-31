"""In-memory sample store.

At 2 Hz an hour of telemetry is ~7200 rows, so samples are kept in full and
``Last N`` only narrows the drawn window.  Nothing is discarded while connected.
"""

from datetime import datetime

from .constants import FAULT_NONE, VALUE_COLUMNS


class DataStore:
    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.t: list[float] = []
        self.wall: list[datetime] = []
        self.series: dict[str, list[float]] = {n: [] for n in VALUE_COLUMNS}
        self.fault_events: list[tuple[float, int]] = []
        self._t0: datetime | None = None
        self._prev_fault: float | None = None

    def __len__(self) -> int:
        return len(self.t)

    def add_row(self, values: dict[str, float], wall: datetime | None = None) -> None:
        wall = wall or datetime.now()
        if self._t0 is None:
            self._t0 = wall
        elapsed = (wall - self._t0).total_seconds()

        self.t.append(elapsed)
        self.wall.append(wall)

        # Union so a header change mid-session neither drops new columns nor
        # leaves retired ones index-misaligned with t.
        names = list(self.series) + [n for n in values if n not in self.series]
        for name in names:
            column = self.series.setdefault(name, [float("nan")] * (len(self.t) - 1))
            column.append(values.get(name, float("nan")))

        self._track_fault(elapsed, values.get("FAULT"))

    def _track_fault(self, elapsed: float, fault: float | None) -> None:
        if fault is None or fault != fault:  # None or NaN
            return
        code = int(fault)
        rising = self._prev_fault is None or int(self._prev_fault) == FAULT_NONE
        if code != FAULT_NONE and rising:
            # A non-zero first sample counts too: it means the board was already
            # latched in fault when we attached.
            self.fault_events.append((elapsed, code))
        self._prev_fault = fault

    def elapsed_at(self, wall: datetime) -> float | None:
        """Plot-axis position for a wall-clock instant.

        Verification events are timestamped by the PC, not by a sample, so they
        need the same origin the plots use.  None until the first row fixes it.
        """
        if self._t0 is None:
            return None
        return (wall - self._t0).total_seconds()

    def window(self, last_n: int) -> tuple[list[float], int]:
        """Return the drawn time slice and the index it starts at."""
        if not self.t:
            return [], 0
        start = max(0, len(self.t) - last_n) if last_n > 0 else 0
        return self.t[start:], start
