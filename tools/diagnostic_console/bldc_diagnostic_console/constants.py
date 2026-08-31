"""Fixed telemetry contract for the BLDC STM32 USART3 stream.

The firmware emits its header line once at boot, so a PC that connects mid-stream
never sees it.  DEFAULT_COLUMNS is therefore the primary column order and the
header line, when it does arrive, only refreshes it.
"""

from dataclasses import dataclass, field

# --- wire format -----------------------------------------------------------

DEFAULT_COLUMNS = [
    "TYPE",
    "RAW_PK_A",
    "FILT_PK_A",
    "FILT_AVG_A",
    "MAX_FLTCNT",
    "DUTY",
    "VDC",
    "RPM",
    "STATE",
    "FAULT",
]

HEADER_FIRST_FIELD = "TYPE"
DATA_RECORD_TYPE = "CUR"

#: Columns carrying numeric samples (everything but the record-type tag).
VALUE_COLUMNS = [c for c in DEFAULT_COLUMNS if c != "TYPE"]

# --- enums (plain enumerations, not bitmasks) ------------------------------

STATE_LABELS = {0: "INIT", 1: "READY", 2: "RUN", 3: "FAULT"}
FAULT_LABELS = {0: "NONE", 1: "OVERCURRENT", 2: "OVERTEMP", 3: "COMM"}

# Named enum members used by the verification runner; the numbering above is
# the firmware's and is not changed here.
STATE_INIT, STATE_READY, STATE_RUN, STATE_FAULT = 0, 1, 2, 3
FAULT_NONE, FAULT_OVERCURRENT, FAULT_OVERTEMP, FAULT_COMM = 0, 1, 2, 3

# STATE and FAULT share one panel but both count 0..3, so FAULT is drawn
# shifted upward to keep the two staircases from overlapping.
FAULT_PLOT_OFFSET = 5.0

# --- series / group layout -------------------------------------------------


@dataclass(frozen=True)
class SeriesDef:
    name: str
    label: str
    group: str
    color: str
    visible: bool = True
    offset: float = 0.0


@dataclass(frozen=True)
class GroupDef:
    """One stacked panel.

    A panel's Y axis is driven one of two ways.  ``y_range`` pins it to a fixed
    physical span (duty is 0..100 % by definition, Vdc by the 24 V bus, the enum
    panel by its category slots).  ``y_floor`` instead anchors the bottom and lets
    the top follow the data — see ``PlotArea._autoscale_y``.  Panels that set
    neither fall back to plain pyqtgraph autoscaling.
    """

    key: str
    title: str
    y_label: str = ""
    step: bool = False
    y_range: tuple[float, float] | None = None
    #: Bottom of the axis; the top autoscales above it. Never panned below.
    y_floor: float | None = None
    #: Smallest span autoscale will show, so an idle signal cannot blow the
    #: axis up to a few milliamps of noise.
    y_min_span: float = 0.0
    #: Fraction of the drawn span left as headroom above the peak.
    y_headroom: float = 0.1
    #: Fixed-range panels refuse mouse Y zoom/pan unless this is False.
    lock_y: bool = True
    y_ticks: list[tuple[float, str]] = field(default_factory=list)
    stretch: int = 2
    #: Long-form explanation, shown as the panel heading's tooltip. The title is
    #: kept short enough to fit the series list, so anything the reader needs
    #: beyond the name goes here.
    description: str = ""


SERIES: list[SeriesDef] = [
    SeriesDef("RAW_PK_A", "RAW_PK_A", "current", "#e15759"),
    SeriesDef("FILT_PK_A", "FILT_PK_A", "current", "#4e79a7"),
    SeriesDef("FILT_AVG_A", "FILT_AVG_A", "current", "#59a14f"),
    SeriesDef("RPM", "RPM", "rpm", "#f28e2b"),
    SeriesDef("DUTY", "DUTY", "duty", "#76b7b2"),
    SeriesDef("VDC", "VDC", "vdc", "#b07aa1"),
    SeriesDef("STATE", "STATE", "state", "#4e79a7"),
    SeriesDef("FAULT", "FAULT", "state", "#e15759", offset=FAULT_PLOT_OFFSET),
    SeriesDef("MAX_FLTCNT", "MAX_FLTCNT", "fltcnt", "#9c755f", visible=False),
]

_STATE_TICKS = [(float(v), f"S:{n}") for v, n in STATE_LABELS.items()]
_FAULT_TICKS = [
    (v + FAULT_PLOT_OFFSET, f"F:{n}") for v, n in FAULT_LABELS.items()
]

GROUPS: list[GroupDef] = [
    # Phase current is a magnitude here, so the axis starts at 0 A and grows to
    # the peak.  1 A is the narrowest useful window: idle draw is ~0.02 A and a
    # tighter axis would just magnify sensor noise.
    GroupDef("current", "Current", "A", y_floor=0.0, y_min_span=1.0, stretch=3),
    # The controller never commands reverse, so negative rpm is not a real state.
    GroupDef("rpm", "RPM", "rpm", y_floor=0.0, y_min_span=100.0),
    # Duty is a percentage of CNT_MAX: the full 0..100 % is the physical range,
    # and holding it keeps a 9 % cruise readable as "low", not as full scale.
    GroupDef("duty", "Duty", "%", y_range=(0.0, 100.0)),
    # 24 V bus, so 0..30 V frames both sag and headroom. Zoom stays enabled for
    # inspecting ripple.
    GroupDef("vdc", "Vdc", "V", y_range=(0.0, 30.0), lock_y=False),
    GroupDef(
        "state",
        "State / Fault",
        "",
        step=True,
        y_range=(-0.5, 3.5 + FAULT_PLOT_OFFSET),
        y_ticks=_STATE_TICKS + _FAULT_TICKS,
        stretch=3,
    ),
    # Wire field stays MAX_FLTCNT; only the on-screen title is shortened.
    GroupDef(
        "fltcnt",
        "OC Fault Count",
        "count",
        y_floor=0.0,
        y_min_span=5.0,
        description=(
            "MAX_FLTCNT \u2014 the peak value reached by the firmware's "
            "overcurrent fault counter, which is what trips FAULT=OVERCURRENT."
        ),
    ),
]

SERIES_BY_NAME = {s.name: s for s in SERIES}
SERIES_BY_GROUP = {
    g.key: [s for s in SERIES if s.group == g.key] for g in GROUPS
}

# --- ui defaults -----------------------------------------------------------

BAUD_RATES = [9600, 19200, 38400, 57600, 115200, 230400, 460800, 921600]
DEFAULT_BAUD = 9600
DEFAULT_LAST_N = 600  # 5 minutes at the firmware's 500 ms cadence
MAX_LOG_LINES = 5000
REDRAW_INTERVAL_MS = 200
