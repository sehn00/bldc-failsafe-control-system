"""CSV / raw-log export."""

import csv
from typing import Iterable

from .constants import VALUE_COLUMNS
from .data_store import DataStore


def write_csv(path: str, store: DataStore) -> int:
    """Write parsed samples as a wide numeric table. Returns rows written."""
    columns = list(VALUE_COLUMNS)
    for name in store.series:
        if name not in columns:
            columns.append(name)

    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["t_iso", "t_elapsed_s"] + columns)
        for i in range(len(store.t)):
            row = [
                store.wall[i].isoformat(timespec="milliseconds"),
                f"{store.t[i]:.3f}",
            ]
            for name in columns:
                values = store.series.get(name, [])
                value = values[i] if i < len(values) else float("nan")
                row.append("" if value != value else _fmt(value))
            writer.writerow(row)
    return len(store.t)


def _fmt(value: float) -> str:
    if value == int(value):
        return str(int(value))
    return f"{value:g}"


def write_raw_log(path: str, lines: Iterable[str]) -> int:
    count = 0
    with open(path, "w", encoding="utf-8") as fh:
        for line in lines:
            fh.write(line + "\n")
            count += 1
    return count
