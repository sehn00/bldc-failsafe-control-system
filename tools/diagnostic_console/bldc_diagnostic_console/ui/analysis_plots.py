"""Campaign views: delay histogram and the I-MR pair.

Both read the same list of measured delays; neither transforms telemetry.
"""

import numpy as np
import pyqtgraph as pg
from PySide6.QtWidgets import QVBoxLayout, QWidget

from ..verification.stats import ControlChart, imr
from ..verification.trial import DELAY_LABEL

DELAY_AXIS = f"{DELAY_LABEL} [ms]"

BAR_BRUSH = "#4e79a7"
POINT_PEN = pg.mkPen("#4e79a7", width=2)
CENTER_PEN = pg.mkPen("#59a14f", width=1)
LIMIT_PEN = pg.mkPen("#e15759", width=1, style=pg.QtCore.Qt.DashLine)
OUT_BRUSH = "#e15759"


def _plot(left: str, bottom: str, title: str = "") -> pg.PlotWidget:
    widget = pg.PlotWidget(background="w")
    item = widget.getPlotItem()
    item.showGrid(x=True, y=True, alpha=0.25)
    if title:
        item.setTitle(title, color="#333", size="9pt")
    item.setLabel("left", left)
    item.setLabel("bottom", bottom)
    # Milliseconds and trial numbers are read literally, never as "(x1e3)".
    item.getAxis("left").enableAutoSIPrefix(False)
    item.getAxis("bottom").enableAutoSIPrefix(False)
    return widget


class DelayHistogram(QWidget):
    """Frequency of the PC-observed delay."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.plot = _plot("Frequency", DELAY_AXIS)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.plot)
        self._bars: pg.BarGraphItem | None = None

    def set_delays(self, delays: list[float]) -> None:
        item = self.plot.getPlotItem()
        if self._bars is not None:
            item.removeItem(self._bars)
            self._bars = None
        if not delays:
            return

        # "auto" = max(Sturges, Freedman-Diaconis); it degrades to a single bin
        # when every trial landed on the same millisecond.
        counts, edges = np.histogram(np.asarray(delays, dtype=float), bins="auto")
        widths = np.diff(edges)
        if not len(counts) or not np.all(np.isfinite(edges)):
            return
        self._bars = pg.BarGraphItem(
            x=edges[:-1] + widths / 2.0,
            height=counts,
            width=widths * 0.9,
            brush=BAR_BRUSH,
            pen=pg.mkPen("#2f4f6f"),
        )
        item.addItem(self._bars)
        item.setYRange(0, max(1, int(counts.max())) * 1.15, padding=0)


class ImrChart(QWidget):
    """Individuals + moving-range charts, stacked and X-linked by trial number."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        # The full name rides in the titles; a rotated left label that long is
        # clipped by the axis, so the axes carry the short form.
        self.individuals = _plot(
            "Delay [ms]", "Trial", f"Individuals (I) — {DELAY_LABEL}"
        )
        self.ranges = _plot(
            "Moving range [ms]", "Trial", "Moving range (MR) of successive trials"
        )
        self.ranges.getPlotItem().setXLink(self.individuals.getPlotItem())

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addWidget(self.individuals)
        layout.addWidget(self.ranges)

        self._items: list[tuple[pg.PlotWidget, object]] = []
        self.hint = pg.TextItem("", color="#777", anchor=(0.5, 0.5))

    def set_delays(self, delays: list[float]) -> None:
        for widget, item in self._items:
            widget.getPlotItem().removeItem(item)
        self._items.clear()

        result = imr(delays)
        if result is None:
            return  # a moving range needs at least two trials
        # Individuals are numbered from trial 1; MR_i sits between i-1 and i,
        # so the moving ranges start at 2 and the two charts line up.
        self._draw(self.individuals, result.individuals,
                   list(range(1, len(delays) + 1)))
        self._draw(self.ranges, result.moving_range,
                   list(range(2, len(delays) + 1)))

    def _draw(self, widget: pg.PlotWidget, chart: ControlChart,
              xs: list[int]) -> None:
        item = widget.getPlotItem()

        def add(obj):
            item.addItem(obj)
            self._items.append((widget, obj))

        add(pg.PlotDataItem(xs, chart.values, pen=POINT_PEN,
                            symbol="o", symbolSize=6,
                            symbolBrush="#4e79a7", symbolPen=POINT_PEN))
        for value, pen, text in (
            (chart.center, CENTER_PEN, f"CL {chart.center:.0f}"),
            (chart.ucl, LIMIT_PEN, f"UCL {chart.ucl:.0f}"),
            (chart.lcl, LIMIT_PEN, f"LCL {chart.lcl:.0f}"),
        ):
            add(pg.InfiniteLine(
                pos=value, angle=0, pen=pen, label=text,
                labelOpts={"position": 0.04, "color": pen.color(),
                           "fill": (255, 255, 255, 200), "movable": False},
            ))
        if chart.out_of_control:
            add(pg.ScatterPlotItem(
                x=[xs[i] for i in chart.out_of_control],
                y=[chart.values[i] for i in chart.out_of_control],
                symbol="o", size=12, brush=OUT_BRUSH,
                pen=pg.mkPen("#7f2b2c", width=2),
            ))
