"""Stacked, X-linked plots — one panel per signal group."""

import pyqtgraph as pg
from PySide6.QtWidgets import QFrame, QVBoxLayout, QWidget

from ..constants import GROUPS, SERIES, SERIES_BY_GROUP, GroupDef
from ..data_store import DataStore

AXIS_WIDTH = 78  # fixed so the stacked panels line up on the left
MARKER_PEN = pg.mkPen("#e15759", width=1, style=pg.QtCore.Qt.DashLine)

#: Verification events, drawn through every panel like the fault marker so a
#: trial can be lined up against the raw signals afterwards.  "detect" lands on
#: the same sample as the red FAULT edge by construction, so it is dotted to
#: stay legible where the two coincide.
EVENT_PENS = {
    "inject": pg.mkPen("#444444", width=1, style=pg.QtCore.Qt.DashDotLine),
    "detect": pg.mkPen("#2f7d3a", width=1, style=pg.QtCore.Qt.DotLine),
}
SEPARATOR_COLOR = "#aab0b8"  # reads as a boundary, still lighter than any trace


def _separator() -> QFrame:
    """A 1 px rule marking where one panel ends and the next begins."""
    line = QFrame()
    line.setFrameShape(QFrame.HLine)
    line.setFrameShadow(QFrame.Plain)  # plain, so Qt draws one flat line, not a bevel
    line.setLineWidth(1)
    line.setFixedHeight(1)
    line.setStyleSheet(f"color: {SEPARATOR_COLOR};")
    return line


def _peak(values: list[float], seen: float | None) -> float | None:
    """Largest finite sample so far; a column padded with NaN must not win."""
    for v in values:
        if v == v and (seen is None or v > seen):  # v == v rejects NaN
            seen = v
    return seen


def _staircase(x: list[float], y: list[float]) -> tuple[list[float], list[float]]:
    """Expand samples into a zero-order hold, so enums never look interpolated."""
    if not x:
        return [], []
    sx = [x[0]]
    sy = [y[0]]
    for i in range(1, len(x)):
        sx.append(x[i])
        sy.append(y[i - 1])
        sx.append(x[i])
        sy.append(y[i])
    return sx, sy


class PlotArea(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        pg.setConfigOptions(antialias=True)

        self.plots: dict[str, pg.PlotWidget] = {}
        self.curves: dict[str, pg.PlotDataItem] = {}
        self.visible: dict[str, bool] = {s.name: s.visible for s in SERIES}
        self._group_visible: dict[str, bool] = {}
        self._separators: dict[str, QFrame] = {}
        self._y_top: dict[str, float] = {}
        self._markers: list[list[pg.InfiniteLine]] = []
        self._events: list[list[pg.InfiniteLine]] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        anchor = None
        for group in GROUPS:
            widget = pg.PlotWidget(background="w")
            item = widget.getPlotItem()
            item.showGrid(x=True, y=True, alpha=0.25)
            label = f"{group.title} [{group.y_label}]" if group.y_label else group.title
            item.setLabel("left", label)
            item.getAxis("left").setWidth(AXIS_WIDTH)
            # Both axes must stay literal.  On X an SI prefix would relabel a
            # 20-minute capture as "Time [s] (x1e3)"; on Y it would retitle an
            # idle 0.018 A trace "(x0.001)" and print the ticks in milliamps.
            item.getAxis("bottom").enableAutoSIPrefix(False)
            item.getAxis("left").enableAutoSIPrefix(False)

            if group.y_ticks:
                item.getAxis("left").setTicks([list(group.y_ticks)])
            if group.y_range:
                item.setYRange(*group.y_range, padding=0)
                item.vb.setMouseEnabled(y=not group.lock_y)
            elif group.y_floor is not None:
                # redraw() owns this axis, so pyqtgraph's own autoscale is off;
                # the limit keeps a manual pan (while paused) out of the
                # negative half-plane the signal cannot physically reach.
                item.disableAutoRange(axis="y")
                item.vb.setLimits(yMin=group.y_floor)
                item.setYRange(
                    group.y_floor, group.y_floor + group.y_min_span, padding=0
                )
            else:
                item.enableAutoRange(axis="y")

            if len(SERIES_BY_GROUP[group.key]) > 1:
                item.addLegend(offset=(-10, 10), labelTextSize="8pt")

            if anchor is None:
                anchor = item
            else:
                item.setXLink(anchor)

            for spec in SERIES_BY_GROUP[group.key]:
                self.curves[spec.name] = item.plot(
                    [],
                    [],
                    pen=pg.mkPen(spec.color, width=2),
                    name=spec.label,
                    connect="finite",
                )

            self.plots[group.key] = widget
            self._group_visible[group.key] = True
            layout.addWidget(widget, group.stretch)

            rule = _separator()
            self._separators[group.key] = rule
            layout.addWidget(rule)

        self._anchor = anchor
        self._sync_group_visibility()

    # --- visibility --------------------------------------------------------

    def set_series_visible(self, name: str, visible: bool) -> None:
        self.visible[name] = visible
        self._sync_group_visibility()

    def _sync_group_visibility(self) -> None:
        shown = []
        for group in GROUPS:
            any_on = any(
                self.visible.get(s.name, False) for s in SERIES_BY_GROUP[group.key]
            )
            self._group_visible[group.key] = any_on
            self.plots[group.key].setVisible(any_on)
            if any_on:
                shown.append(group)

        # Only the bottom-most visible panel carries the time axis labels, and
        # only the rules *between* two visible panels are drawn — the one under
        # the last panel would just float below the stack.
        for rule in self._separators.values():
            rule.setVisible(False)
        for i, group in enumerate(shown):
            axis = self.plots[group.key].getPlotItem().getAxis("bottom")
            is_last = i == len(shown) - 1
            axis.setStyle(showValues=is_last)
            axis.setLabel("Time [s]" if is_last else "")
            self._separators[group.key].setVisible(not is_last)

    # --- data --------------------------------------------------------------

    def redraw(self, store: DataStore, last_n: int) -> None:
        window, start = store.window(last_n)

        for group in GROUPS:
            if not self._group_visible[group.key]:
                continue
            peak: float | None = None
            for spec in SERIES_BY_GROUP[group.key]:
                curve = self.curves[spec.name]
                if not self.visible.get(spec.name, False) or not window:
                    curve.setData([], [])
                    continue
                values = store.series.get(spec.name, [])[start:]
                if spec.offset:
                    values = [v + spec.offset for v in values]
                if group.step:
                    x, y = _staircase(window, values)
                else:
                    x, y = window, values
                curve.setData(x, y)
                peak = _peak(values, peak)
            if group.y_floor is not None:
                self._autoscale_y(group, peak)

        self._sync_markers(store.fault_events)

        if window and self._anchor is not None:
            lo, hi = window[0], window[-1]
            if hi <= lo:
                hi = lo + 1.0
            self._anchor.setXRange(lo, hi, padding=0.02)

    def _autoscale_y(self, group: GroupDef, peak: float | None) -> None:
        """Hold the axis floor and let only the top follow the visible peak."""
        floor = group.y_floor
        top = floor + group.y_min_span
        if peak is not None:
            top = max(top, peak + (peak - floor) * group.y_headroom)

        # Redrawing five times a second, a top recomputed from every new sample
        # would leave the ticks visibly crawling; ignore drift under 2 %.
        previous = self._y_top.get(group.key)
        if previous is not None and abs(top - previous) <= abs(previous - floor) * 0.02:
            return
        self._y_top[group.key] = top
        self.plots[group.key].getPlotItem().setYRange(floor, top, padding=0)

    def _sync_markers(self, events: list[tuple[float, int]]) -> None:
        for t, _code in events[len(self._markers) :]:
            lines = []
            for group in GROUPS:
                line = pg.InfiniteLine(pos=t, angle=90, pen=MARKER_PEN)
                self.plots[group.key].getPlotItem().addItem(line, ignoreBounds=True)
                lines.append(line)
            self._markers.append(lines)

    def add_event_marker(self, t: float, kind: str, text: str = "") -> None:
        """Drop a labelled vertical rule through every panel at time *t*."""
        pen = EVENT_PENS.get(kind, MARKER_PEN)
        lines = []
        for i, group in enumerate(GROUPS):
            # Label only the top panel; repeating it five times just clutters.
            opts = {}
            if text and i == 0:
                opts = {
                    "label": text,
                    "labelOpts": {
                        "position": 0.92,
                        "color": pen.color(),
                        "fill": (255, 255, 255, 200),
                        "movable": False,
                    },
                }
            line = pg.InfiniteLine(pos=t, angle=90, pen=pen, **opts)
            self.plots[group.key].getPlotItem().addItem(line, ignoreBounds=True)
            lines.append(line)
        self._events.append(lines)

    def clear(self) -> None:
        for curve in self.curves.values():
            curve.setData([], [])
        for lines in self._markers + self._events:
            for group, line in zip(GROUPS, lines):
                self.plots[group.key].getPlotItem().removeItem(line)
        self._markers.clear()
        self._events.clear()
