"""Descriptive statistics and I-MR control limits for a trial campaign.

Deliberately limited to run-to-run variability and outlier spotting: no process
capability (Cp/Cpk), no modelling.
"""

import statistics
from dataclasses import dataclass, field

import numpy as np

# Shewhart constants for individuals / moving-range charts.  A moving range of
# successive readings is a subgroup of size 2, so:
D2_N2 = 1.128   # mean range of 2 samples from a unit normal
E2_N2 = 2.660   # 3 / D2_N2 -- individuals-chart limit factor
D4_N2 = 3.267   # upper moving-range factor
D3_N2 = 0.0     # lower moving-range factor (no lower limit at n=2)


@dataclass(frozen=True)
class Summary:
    n: int
    mean: float | None = None
    median: float | None = None
    stdev: float | None = None
    minimum: float | None = None
    maximum: float | None = None
    p95: float | None = None


def summarize(values: list[float]) -> Summary:
    """Sample statistics. ``stdev`` is the n-1 form; P95 is linear-interpolated."""
    if not values:
        return Summary(0)
    return Summary(
        n=len(values),
        mean=statistics.fmean(values),
        median=statistics.median(values),
        # A single trial has no spread to report -- leave it blank rather than
        # printing a misleading 0.
        stdev=statistics.stdev(values) if len(values) > 1 else None,
        minimum=min(values),
        maximum=max(values),
        p95=float(np.percentile(values, 95)),
    )


@dataclass(frozen=True)
class ControlChart:
    values: list[float]
    center: float
    ucl: float
    lcl: float
    out_of_control: list[int] = field(default_factory=list)


@dataclass(frozen=True)
class ImrResult:
    individuals: ControlChart
    moving_range: ControlChart


def moving_ranges(values: list[float]) -> list[float]:
    return [abs(b - a) for a, b in zip(values, values[1:])]


def imr(values: list[float]) -> ImrResult | None:
    """Individuals and moving-range charts from the standard estimator.

    Spread comes from the mean moving range, not from the sample standard
    deviation: MR-bar / d2 estimates within-run (short-term) sigma, so a slow
    drift across the campaign widens the chart's points instead of quietly
    inflating its limits the way mean +/- 3s would.

    Returns ``None`` below two trials, where a moving range does not exist.
    """
    if len(values) < 2:
        return None

    ranges = moving_ranges(values)
    mr_bar = statistics.fmean(ranges)
    center = statistics.fmean(values)

    spread = E2_N2 * mr_bar
    individuals = ControlChart(
        values=list(values),
        center=center,
        ucl=center + spread,
        lcl=center - spread,
    )
    mr_chart = ControlChart(
        values=ranges,
        center=mr_bar,
        ucl=D4_N2 * mr_bar,
        lcl=D3_N2 * mr_bar,
    )
    return ImrResult(
        individuals=_flag(individuals),
        moving_range=_flag(mr_chart),
    )


def _flag(chart: ControlChart) -> ControlChart:
    """Nelson rule 1 only: a single point outside the control limits."""
    out = [
        i for i, v in enumerate(chart.values)
        if v > chart.ucl or v < chart.lcl
    ]
    return ControlChart(chart.values, chart.center, chart.ucl, chart.lcl, out)
