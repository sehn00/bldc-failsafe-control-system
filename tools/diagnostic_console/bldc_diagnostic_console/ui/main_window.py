"""Main window: connection bar, telemetry tab, verification tab, actions."""

from datetime import datetime

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..constants import (
    BAUD_RATES,
    DEFAULT_BAUD,
    DEFAULT_LAST_N,
    FAULT_LABELS,
    REDRAW_INTERVAL_MS,
)
from ..csv_writer import write_csv, write_raw_log
from ..data_store import DataStore
from ..parser import TelemetryParser
from ..serial_reader import SerialReader, available_ports
from .log_view import LogView
from .plot_area import PlotArea
from .series_panel import SeriesPanel
from .verification_panel import VerificationPanel

MAX_RAW_LINES = 200_000


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("BLDC Diagnostic Console - Sehyeon Choi")
        self.resize(1200, 860)

        self.parser = TelemetryParser()
        self.store = DataStore()
        self.reader: SerialReader | None = None

        self.paused = False
        self._pending: list[str] = []
        self._raw: list[str] = []
        self._rows = 0
        self._errors = 0

        self._build_ui()
        self._refresh_ports()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(REDRAW_INTERVAL_MS)

    # --- construction ------------------------------------------------------

    def _build_ui(self) -> None:
        root = QWidget()
        outer = QVBoxLayout(root)
        outer.setContentsMargins(6, 6, 6, 6)
        outer.setSpacing(6)
        outer.addLayout(self._build_top_bar())

        self.plot_area = PlotArea()
        self.series_panel = SeriesPanel()
        self.series_panel.series_toggled.connect(self.plot_area.set_series_visible)
        self.series_panel.setFixedWidth(190)

        upper = QSplitter(Qt.Horizontal)
        upper.addWidget(self.plot_area)
        upper.addWidget(self.series_panel)
        upper.setStretchFactor(0, 1)
        upper.setStretchFactor(1, 0)

        self.log_view = LogView()
        split = QSplitter(Qt.Vertical)
        split.addWidget(upper)
        split.addWidget(self.log_view)
        split.setSizes([640, 200])

        # The verification tab reads the same store the telemetry tab fills, so
        # capture, plotting and logging keep running whichever tab is on top.
        self.verification = VerificationPanel(self.store)
        self.verification.marker_requested.connect(self.plot_area.add_event_marker)

        self.tabs = QTabWidget()
        self.tabs.addTab(split, "Telemetry")
        self.tabs.addTab(self.verification, "Verification")
        outer.addWidget(self.tabs, 1)

        outer.addLayout(self._build_bottom_bar())
        self.setCentralWidget(root)

    def _build_top_bar(self) -> QHBoxLayout:
        bar = QHBoxLayout()
        bar.setSpacing(6)

        bar.addWidget(QLabel("Port"))
        self.port_combo = QComboBox()
        self.port_combo.setEditable(True)  # also accepts a typed device path
        self.port_combo.setMinimumWidth(220)
        bar.addWidget(self.port_combo)

        refresh = QToolButton()
        refresh.setText("↻")
        refresh.setToolTip("Rescan serial ports")
        refresh.clicked.connect(self._refresh_ports)
        bar.addWidget(refresh)

        bar.addWidget(QLabel("Baud"))
        self.baud_combo = QComboBox()
        self.baud_combo.setEditable(True)
        self.baud_combo.addItems([str(b) for b in BAUD_RATES])
        self.baud_combo.setCurrentText(str(DEFAULT_BAUD))
        bar.addWidget(self.baud_combo)

        self.connect_button = QPushButton("Connect")
        self.connect_button.clicked.connect(self._toggle_connection)
        bar.addWidget(self.connect_button)

        self.status_label = QLabel()
        self.status_label.setStyleSheet("color: #444;")
        bar.addWidget(self.status_label)
        bar.addStretch(1)
        self._update_status("Disconnected")
        return bar

    def _build_bottom_bar(self) -> QHBoxLayout:
        bar = QHBoxLayout()
        bar.setSpacing(6)

        self.pause_button = QPushButton("Pause")
        self.pause_button.setCheckable(True)
        self.pause_button.toggled.connect(self._on_pause)
        bar.addWidget(self.pause_button)

        reset = QPushButton("Reset")
        reset.setToolTip("Discard all captured samples and fault markers")
        reset.clicked.connect(self._on_reset)
        bar.addWidget(reset)

        bar.addSpacing(12)
        bar.addWidget(QLabel("Last N"))
        self.last_n = QSpinBox()
        self.last_n.setRange(10, 1_000_000)
        self.last_n.setValue(DEFAULT_LAST_N)
        self.last_n.setSingleStep(100)
        self.last_n.setToolTip("Samples kept on screen; all samples stay in memory")
        bar.addWidget(self.last_n)

        bar.addStretch(1)

        clear_log = QPushButton("Clear Log")
        clear_log.clicked.connect(self._on_clear_log)
        bar.addWidget(clear_log)

        save_csv = QPushButton("Save CSV...")
        save_csv.clicked.connect(self._on_save_csv)
        bar.addWidget(save_csv)

        save_raw = QPushButton("Save Raw Log...")
        save_raw.clicked.connect(self._on_save_raw)
        bar.addWidget(save_raw)
        return bar

    # --- connection --------------------------------------------------------

    def _refresh_ports(self) -> None:
        current = self.port_combo.currentData()
        typed = self.port_combo.currentText().strip()
        self.port_combo.clear()
        for device, description in available_ports():
            label = f"{device} - {description}" if description else device
            self.port_combo.addItem(label, device)
        index = self.port_combo.findData(current) if current is not None else -1
        if index >= 0:
            self.port_combo.setCurrentIndex(index)
        elif typed and self.port_combo.findData(typed) < 0:
            self.port_combo.setCurrentText(typed)

    def _toggle_connection(self) -> None:
        if self.reader is not None:
            self._disconnect()
        else:
            self._connect()

    def _connect(self) -> None:
        device = self.port_combo.currentData() or self.port_combo.currentText().strip()
        if not device:
            QMessageBox.warning(self, "No port", "Select a serial port first.")
            return
        try:
            baud = int(self.baud_combo.currentText())
        except ValueError:
            QMessageBox.warning(self, "Bad baud", "Baud rate must be a number.")
            return

        self.parser.reset()
        reader = SerialReader(device, baud, self)
        reader.line_received.connect(self._on_line)
        reader.opened.connect(self._on_opened)
        reader.failed.connect(self._on_failed)
        reader.finished.connect(self._on_reader_finished)
        self.reader = reader
        self.connect_button.setText("Disconnect")
        reader.start()

    def _disconnect(self) -> None:
        if self.reader is not None:
            self.reader.stop()

    def _on_opened(self, port: str) -> None:
        self._update_status(f"Connected {port}")
        self.verification.set_connected(True)

    def _on_reader_finished(self) -> None:
        self.reader = None
        self.connect_button.setText("Connect")
        self._update_status("Disconnected")
        self.verification.set_connected(False)

    def _on_failed(self, message: str) -> None:
        QMessageBox.critical(self, "Serial error", message)

    def _on_line(self, line: str) -> None:
        self._pending.append(line)

    # --- periodic drain ----------------------------------------------------

    def _tick(self) -> None:
        lines, self._pending = self._pending, []
        for line in lines:
            result = self.parser.parse(line)
            if result.kind == "data" and result.values is not None:
                self.store.add_row(result.values)
                self._rows += 1
            elif result.kind == "invalid":
                self._errors += 1

        if lines:
            self._raw.extend(lines)
            if len(self._raw) > MAX_RAW_LINES:
                del self._raw[: len(self._raw) - MAX_RAW_LINES]
            self.log_view.append_lines(lines)
            self._update_status()

        # Verification runs off captured samples, so it must advance even while
        # the drawing is paused.
        self.verification.poll()

        if not self.paused:
            self.plot_area.redraw(self.store, self.last_n.value())

    def _update_status(self, connection: str | None = None) -> None:
        if connection is not None:
            self._connection = connection
        parts = [getattr(self, "_connection", "Disconnected")]
        parts.append(f"{self._rows} rows")
        if self._errors:
            parts.append(f"⚠ {self._errors} bad lines")
        faults = self.store.fault_events
        if faults:
            _t, code = faults[-1]
            name = FAULT_LABELS.get(code, str(code))
            parts.append(f"faults: {len(faults)} (last {name})")
        self.status_label.setText("   |   ".join(parts))

    # --- actions -----------------------------------------------------------

    def _on_pause(self, paused: bool) -> None:
        # Capture continues while paused; only the redraw stops.
        self.paused = paused
        self.pause_button.setText("Resume" if paused else "Pause")
        if not paused:
            self.plot_area.redraw(self.store, self.last_n.value())

    def _on_reset(self) -> None:
        self.store.reset()
        self.plot_area.clear()
        self._rows = 0
        self._errors = 0
        self._update_status()

    def _on_clear_log(self) -> None:
        self.log_view.clear()
        self._raw.clear()

    def _on_save_csv(self) -> None:
        if not len(self.store):
            QMessageBox.information(self, "Nothing to save", "No samples captured yet.")
            return
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path, _ = QFileDialog.getSaveFileName(
            self, "Save CSV", f"telemetry_{stamp}.csv", "CSV files (*.csv)"
        )
        if not path:
            return
        try:
            rows = write_csv(path, self.store)
        except OSError as exc:
            QMessageBox.critical(self, "Save failed", str(exc))
            return
        self._update_status()
        QMessageBox.information(self, "Saved", f"{rows} rows written to\n{path}")

    def _on_save_raw(self) -> None:
        if not self._raw:
            QMessageBox.information(self, "Nothing to save", "No lines received yet.")
            return
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path, _ = QFileDialog.getSaveFileName(
            self, "Save raw log", f"telemetry_{stamp}.log", "Log files (*.log)"
        )
        if not path:
            return
        try:
            count = write_raw_log(path, self._raw)
        except OSError as exc:
            QMessageBox.critical(self, "Save failed", str(exc))
            return
        QMessageBox.information(self, "Saved", f"{count} lines written to\n{path}")

    # --- shutdown ----------------------------------------------------------

    def closeEvent(self, event) -> None:
        if self.reader is not None:
            self.reader.stop()
        self.verification.shutdown()
        event.accept()
