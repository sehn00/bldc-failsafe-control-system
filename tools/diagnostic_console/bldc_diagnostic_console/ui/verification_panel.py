"""Verification tab: SSH fault injection, trial log, campaign analysis.

Capture is not owned here.  The panel reads the shared DataStore the telemetry
tab is already filling, so plots, raw log and CSV keep running through a trial.
"""

from datetime import datetime

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDoubleSpinBox,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..constants import FAULT_LABELS, STATE_LABELS
from ..data_store import DataStore
from ..verification.batch import BatchRunner
from ..verification.config import (
    AUTO_RUN_WATCH_S,
    BATCH_SETTLE_S,
    CONNECTION_TEST_COMMAND,
    CONNECTION_TEST_TOKEN,
    DEFAULT_BATCH_COUNT,
    DEFAULT_SSH_PORT,
    FAULT_DETECT_TIMEOUT_S,
    FAULT_INJECT_COMMAND,
    MAX_BATCH_COUNT,
    MOTORCTL_CLEAR_FAULT,
    MOTORCTL_ENABLE,
    MOTORCTL_TARGET,
    STATE_CONFIRM_TIMEOUT_S,
)
from ..verification.runner import IDLE, FaultTestRunner
from ..verification.ssh import SshCommand, SshResult, SshTarget
from ..verification.stats import summarize
from ..verification.trial import (
    DELAY_LABEL,
    DELAY_SHORT_COLUMN,
    TrialLog,
    write_trials_csv,
)
from .analysis_plots import DelayHistogram, ImrChart

TABLE_COLUMNS = [
    "Trial", "Injection Time", DELAY_SHORT_COLUMN,
    "Final State", "Final Fault", "Auto Run", "Result", "Note",
]
#: Index of the Note column, which takes whatever width is left over.
NOTE_COLUMN = len(TABLE_COLUMNS) - 1
STAT_ROWS = [
    ("n", "Trials with a measured delay"),
    ("mean", "Mean [ms]"),
    ("median", "Median [ms]"),
    ("stdev", "Std dev (n-1) [ms]"),
    ("min", "Min [ms]"),
    ("max", "Max [ms]"),
    ("p95", "P95 [ms]"),
    ("pass", "PASS rate"),
]

OK_STYLE = "color: #2f7d3a;"
BAD_STYLE = "color: #b3261e;"
BUSY_STYLE = "color: #8a6d00;"
MUTED_STYLE = "color: #555;"


class VerificationPanel(QWidget):
    #: (elapsed seconds, marker kind, label) for the telemetry plots.
    marker_requested = Signal(float, str, str)

    def __init__(self, store: DataStore, parent=None) -> None:
        super().__init__(parent)
        self.store = store
        self.log = TrialLog()
        self.runner = FaultTestRunner(self.log, parent=self)
        self.batch = BatchRunner(self.runner, parent=self)
        self._connected = False
        self._probe: SshCommand | None = None

        self._build()
        self._wire()
        self._refresh_stats()
        self._refresh_readiness()

    # --- construction ------------------------------------------------------

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(6, 6, 6, 6)

        split = QSplitter(Qt.Horizontal)
        split.addWidget(self._left_column())
        split.addWidget(self._right_column())
        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 1)
        split.setSizes([380, 820])
        outer.addWidget(split)

    def _left_column(self) -> QWidget:
        inner = QWidget()
        column = QVBoxLayout(inner)
        column.setContentsMargins(0, 0, 6, 0)
        column.addWidget(self._ssh_box())
        column.addWidget(self._injection_box())
        column.addWidget(self._batch_box())
        column.addWidget(self._stats_box())
        column.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidget(inner)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        scroll.setMinimumWidth(330)
        return scroll

    def _ssh_box(self) -> QGroupBox:
        box = QGroupBox("Raspberry Pi (SSH)")
        grid = QGridLayout(box)
        grid.setColumnStretch(1, 1)

        self.host_edit = QLineEdit()
        self.host_edit.setPlaceholderText("hostname, IP, or ~/.ssh/config alias")
        self.user_edit = QLineEdit()
        self.user_edit.setPlaceholderText("blank = use ~/.ssh/config")
        self.port_spin = QSpinBox()
        self.port_spin.setRange(1, 65535)
        self.port_spin.setValue(DEFAULT_SSH_PORT)

        grid.addWidget(QLabel("Host / IP"), 0, 0)
        grid.addWidget(self.host_edit, 0, 1)
        grid.addWidget(QLabel("Username"), 1, 0)
        grid.addWidget(self.user_edit, 1, 1)
        grid.addWidget(QLabel("SSH port"), 2, 0)
        grid.addWidget(self.port_spin, 2, 1)

        self.test_button = QPushButton("Test Connection")
        grid.addWidget(self.test_button, 3, 0, 1, 2)

        self.ssh_status = QLabel("Not tested.")
        self.ssh_status.setWordWrap(True)
        self.ssh_status.setStyleSheet(MUTED_STYLE)
        grid.addWidget(self.ssh_status, 4, 0, 1, 2)

        hint = QLabel(
            "Uses the system OpenSSH client with your existing keys and "
            "~/.ssh/config. No password is stored or prompted for."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #777; font-size: 11px;")
        grid.addWidget(hint, 5, 0, 1, 2)
        return box

    def _injection_box(self) -> QGroupBox:
        box = QGroupBox("SIGKILL fault injection")
        grid = QGridLayout(box)
        grid.setColumnStretch(1, 1)

        self.command_edit = QLineEdit(FAULT_INJECT_COMMAND)
        self.command_edit.setToolTip(
            "Run verbatim on the Pi. Default lives in verification/config.py."
        )
        grid.addWidget(QLabel("Remote command"), 0, 0)
        grid.addWidget(self.command_edit, 0, 1)

        self.detect_spin = QDoubleSpinBox()
        self.detect_spin.setRange(0.5, 120.0)
        self.detect_spin.setSingleStep(0.5)
        self.detect_spin.setSuffix(" s")
        self.detect_spin.setValue(FAULT_DETECT_TIMEOUT_S)
        self.detect_spin.setToolTip(
            "How long to wait for STATE=FAULT / FAULT=COMM after the kill."
        )
        grid.addWidget(QLabel("Fault detect timeout"), 1, 0)
        grid.addWidget(self.detect_spin, 1, 1)

        self.watch_spin = QDoubleSpinBox()
        self.watch_spin.setRange(0.5, 300.0)
        self.watch_spin.setSingleStep(1.0)
        self.watch_spin.setSuffix(" s")
        self.watch_spin.setValue(AUTO_RUN_WATCH_S)
        self.watch_spin.setToolTip(
            "After the fault, how long STATE=FAULT / FAULT=COMM must hold. "
            "Leaving that latch during the window fails the trial."
        )
        grid.addWidget(QLabel("Auto-run watch window"), 2, 0)
        grid.addWidget(self.watch_spin, 2, 1)

        self.readiness = QLabel()
        self.readiness.setWordWrap(True)
        grid.addWidget(self.readiness, 3, 0, 1, 2)

        self.run_button = QPushButton("Run Fault Test")
        grid.addWidget(self.run_button, 4, 0, 1, 2)

        self.run_status = QLabel("Idle.")
        self.run_status.setWordWrap(True)
        self.run_status.setStyleSheet(MUTED_STYLE)
        grid.addWidget(self.run_status, 5, 0, 1, 2)

        note = QLabel(
            "Recovery is manual: after each trial run CLEAR_FAULT → ENABLE → RUN "
            "with motorctl, then start the next trial. Do not recover while the "
            "auto-run watch window is still open — clearing the latch early "
            "fails the trial."
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: #777; font-size: 11px;")
        grid.addWidget(note, 6, 0, 1, 2)
        return box

    def _batch_box(self) -> QGroupBox:
        box = QGroupBox("Batch test")
        grid = QGridLayout(box)
        grid.setColumnStretch(1, 1)

        self.batch_spin = QSpinBox()
        self.batch_spin.setRange(1, MAX_BATCH_COUNT)
        self.batch_spin.setValue(DEFAULT_BATCH_COUNT)
        self.batch_spin.setToolTip(
            "How many trials to run back to back. Long campaigns are the point; "
            f"up to {MAX_BATCH_COUNT} is accepted."
        )
        grid.addWidget(QLabel("Batch count"), 0, 0)
        grid.addWidget(self.batch_spin, 0, 1)

        buttons = QHBoxLayout()
        self.batch_run_button = QPushButton("Run Batch")
        self.batch_stop_button = QPushButton("Stop Batch")
        self.batch_stop_button.setEnabled(False)
        self.batch_stop_button.setToolTip(
            "Finishes the trial in flight, including its watch window, then "
            "stops. No new trial is started."
        )
        buttons.addWidget(self.batch_run_button)
        buttons.addWidget(self.batch_stop_button)
        grid.addLayout(buttons, 1, 0, 1, 2)

        self.batch_progress = QLabel("Current trial — / —")
        self.batch_progress.setStyleSheet(MUTED_STYLE)
        grid.addWidget(self.batch_progress, 2, 0, 1, 2)

        self.batch_anomalies = QLabel()
        self.batch_anomalies.setStyleSheet(MUTED_STYLE)
        self.batch_anomalies.setToolTip(
            "Setup steps the supervisor refused while its STM32 view was "
            "stale, and how many were re-sent after it came back. Each one is "
            "also on the trial it happened in. Injections are never re-sent."
        )
        self.batch_anomalies.hide()
        grid.addWidget(self.batch_anomalies, 3, 0, 1, 2)

        self.batch_status = QLabel("Idle.")
        self.batch_status.setWordWrap(True)
        self.batch_status.setStyleSheet(MUTED_STYLE)
        grid.addWidget(self.batch_status, 4, 0, 1, 2)

        note = QLabel(
            f"Each trial: wait for the supervisor, {MOTORCTL_CLEAR_FAULT} → "
            f"READY, {MOTORCTL_ENABLE}, {MOTORCTL_TARGET} → RUN, settle "
            f"{BATCH_SETTLE_S:g} s, kill, then the same detect and watch "
            "windows as a single trial. The next clear-fault goes out only "
            "after the watch window has closed and the restarted "
            "motor-supervisor answers again.\n"
            "Telemetry decides, not the exit code: a motorctl that was sent "
            "but could not confirm itself (GET_STATE / ACK timeout) is noted "
            "in the trial and settled by the STATE/FAULT that follows. "
            "Anything else stops the batch, with no retry — a command that "
            f"could not run, a state that never arrives within "
            f"{STATE_CONFIRM_TIMEOUT_S:g} s, a supervisor that stays down, or "
            "a dropped serial link."
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: #777; font-size: 11px;")
        grid.addWidget(note, 5, 0, 1, 2)
        return box

    def _stats_box(self) -> QGroupBox:
        box = QGroupBox("Campaign statistics")
        grid = QGridLayout(box)
        self.stat_labels: dict[str, QLabel] = {}
        for row, (key, caption) in enumerate(STAT_ROWS):
            grid.addWidget(QLabel(caption), row, 0)
            value = QLabel("—")
            value.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self.stat_labels[key] = value
            grid.addWidget(value, row, 1)
        grid.setColumnStretch(1, 1)

        caveat = QLabel(
            f"{DELAY_LABEL}: PC kill-sent → PC saw COMM in telemetry. Includes "
            "the SSH round trip and up to one ~500 ms telemetry frame. Use it to "
            "compare trials, not as a PWM-off latency."
        )
        caveat.setWordWrap(True)
        caveat.setStyleSheet("color: #777; font-size: 11px;")
        grid.addWidget(caveat, len(STAT_ROWS), 0, 1, 2)
        return box

    def _right_column(self) -> QWidget:
        split = QSplitter(Qt.Vertical)

        trials = QGroupBox("Trials")
        layout = QVBoxLayout(trials)
        self.table = QTableWidget(0, len(TABLE_COLUMNS))
        self.table.setHorizontalHeaderLabels(TABLE_COLUMNS)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.verticalHeader().setVisible(False)
        # Everything but the Note is as narrow as its content allows, and the
        # Note takes the rest: it is the column that actually has something to
        # say. Long notes elide rather than wrap, so rows stay one line high --
        # the whole text is on the cell's tooltip instead.
        header = self.table.horizontalHeader()
        for column in range(len(TABLE_COLUMNS)):
            header.setSectionResizeMode(column, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(NOTE_COLUMN, QHeaderView.Stretch)
        header.setStretchLastSection(True)
        self.table.setWordWrap(False)
        self.table.setTextElideMode(Qt.ElideRight)
        self.table.horizontalHeaderItem(2).setToolTip(
            f"{DELAY_LABEL}: injection sent → COMM fault seen in telemetry. "
            "Includes the SSH round trip and up to one ~500 ms telemetry frame."
        )
        self.table.horizontalHeaderItem(NOTE_COLUMN).setToolTip(
            "Setup steps motorctl could not confirm, and why a trial failed. "
            "Hover a cell to read one in full."
        )
        layout.addWidget(self.table)

        buttons = QHBoxLayout()
        self.export_button = QPushButton("Export Trials CSV...")
        self.clear_button = QPushButton("Clear Trials")
        buttons.addWidget(self.export_button)
        buttons.addWidget(self.clear_button)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        split.addWidget(trials)

        charts = QTabWidget()
        self.histogram = DelayHistogram()
        self.imr = ImrChart()
        charts.addTab(self.histogram, "Histogram")
        charts.addTab(self.imr, "I-MR chart")
        split.addWidget(charts)
        split.setSizes([260, 420])
        return split

    def _wire(self) -> None:
        self.test_button.clicked.connect(self._on_test_connection)
        self.run_button.clicked.connect(self._on_run)
        self.batch_run_button.clicked.connect(self._on_run_batch)
        self.batch_stop_button.clicked.connect(self.batch.stop)
        self.export_button.clicked.connect(self._on_export)
        self.clear_button.clicked.connect(self._on_clear)

        self.runner.status.connect(self._set_status)
        self.runner.aborted.connect(self._on_aborted)
        self.runner.injected.connect(self._on_injected)
        self.runner.fault_observed.connect(self._on_fault_observed)
        self.runner.trial_finished.connect(self._on_trial_finished)
        self.runner.phase_changed.connect(lambda _p: self._refresh_readiness())

        self.batch.status.connect(lambda text: self._set_batch_status(text))
        self.batch.progress.connect(self._on_batch_progress)
        self.batch.finished.connect(self._on_batch_finished)
        self.batch.phase_changed.connect(lambda _p: self._refresh_readiness())

    # --- telemetry hooks ---------------------------------------------------

    def set_connected(self, connected: bool) -> None:
        self._connected = connected
        # The batch first, so it does not react to the runner's own abort.
        self.batch.set_connected(connected)
        if not connected and self.runner.busy:
            # Losing the link mid-trial is a bench problem, not an STM32
            # result; drop the trial instead of recording a bogus FAIL.
            self.runner.abort("serial telemetry disconnected during the trial")
        self._refresh_readiness()

    def poll(self) -> None:
        """Called from the main window's tick; drives the trial state machine."""
        self.runner.consume(self.store)
        self.batch.consume(self.store)
        self._refresh_readiness()

    # --- ssh ---------------------------------------------------------------

    def _target(self) -> SshTarget:
        return SshTarget(
            host=self.host_edit.text().strip(),
            user=self.user_edit.text().strip(),
            port=self.port_spin.value(),
        )

    def _on_test_connection(self) -> None:
        target = self._target()
        if not target.host:
            self._set_ssh_status("Enter a host or ~/.ssh/config alias.", BAD_STYLE)
            return
        if self._probe is not None and self._probe.isRunning():
            return
        self.test_button.setEnabled(False)
        self._set_ssh_status(f"Connecting to {target.destination()}…", BUSY_STYLE)
        self._probe = SshCommand(target, CONNECTION_TEST_COMMAND, parent=self)
        self._probe.completed.connect(self._on_test_result)
        self._probe.start()

    def _on_test_result(self, result: SshResult) -> None:
        self.test_button.setEnabled(True)
        if result.ok and CONNECTION_TEST_TOKEN in result.stdout:
            self._set_ssh_status("Connection OK.", OK_STYLE)
        elif result.ok:
            self._set_ssh_status(
                "Connected, but the probe echoed nothing recognisable.", BUSY_STYLE
            )
        else:
            self._set_ssh_status(f"Failed — {result.describe()}", BAD_STYLE)

    # --- running a trial ---------------------------------------------------

    def _on_run(self) -> None:
        target = self._target()
        if not target.host:
            self._set_status("Enter the Raspberry Pi host first.", BAD_STYLE)
            return
        command = self.command_edit.text().strip()
        if not command:
            self._set_status("Remote command is empty.", BAD_STYLE)
            return
        self.runner.start(
            target, command, self.store, self._connected,
            detect_timeout=self.detect_spin.value(),
            watch_window=self.watch_spin.value(),
        )

    def _on_run_batch(self) -> None:
        self.batch.start(
            self._target(),
            self.command_edit.text().strip(),
            self.batch_spin.value(),
            self.store,
            self._connected,
            detect_timeout=self.detect_spin.value(),
            watch_window=self.watch_spin.value(),
        )

    def _on_batch_progress(self, index: int, total: int) -> None:
        self.batch_progress.setText(f"Current trial {index} / {total}")
        self._refresh_batch_anomalies()

    def _refresh_batch_anomalies(self) -> None:
        resyncs, retries = self.batch.resyncs, self.batch.retries
        if not resyncs and not retries:
            self.batch_anomalies.hide()
            return
        self.batch_anomalies.setText(
            f"Setup anomalies: {resyncs} supervisor "
            f"{'resync' if resyncs == 1 else 'resyncs'}, {retries} "
            f"{'retry' if retries == 1 else 'retries'}"
        )
        self.batch_anomalies.show()

    def _on_batch_finished(self, completed: bool, message: str) -> None:
        self._set_batch_status(message, OK_STYLE if completed else BAD_STYLE)
        self._refresh_batch_anomalies()
        if not self.batch.busy and self.batch.total:
            self.batch_progress.setText(
                f"Current trial — / —   (last run: {self.batch.index}"
                f" / {self.batch.total})"
            )

    def _on_injected(self, when: datetime) -> None:
        self._mark(when, "inject", f"inject #{self.log.next_index()}")

    def _on_fault_observed(self, when: datetime) -> None:
        self._mark(when, "detect", f"COMM #{self.log.next_index()}")

    def _mark(self, when: datetime, kind: str, text: str) -> None:
        elapsed = self.store.elapsed_at(when)
        if elapsed is not None:
            self.marker_requested.emit(elapsed, kind, text)

    def _on_trial_finished(self, trial) -> None:
        self._append_row(trial)
        self._refresh_stats()

    def _on_aborted(self, reason: str) -> None:
        self._set_status(f"Not run — {reason}", BAD_STYLE)

    # --- table / stats -----------------------------------------------------

    def _append_row(self, trial) -> None:
        row = self.table.rowCount()
        self.table.insertRow(row)
        cells = [
            str(trial.index),
            trial.injection_time.strftime("%H:%M:%S.%f")[:-3],
            "—" if trial.delay_ms is None else f"{trial.delay_ms:.0f}",
            trial.final_state,
            trial.final_fault,
            "YES" if trial.auto_run_detected else "NO",
            trial.result,
            trial.note,
        ]
        for column, text in enumerate(cells):
            item = QTableWidgetItem(text)
            if column == 6:
                item.setForeground(Qt.darkGreen if trial.passed else Qt.red)
            if column == NOTE_COLUMN and text:
                item.setToolTip(text)   # the cell elides; the tooltip does not
            self.table.setItem(row, column, item)
        self.table.scrollToBottom()

    def _refresh_stats(self) -> None:
        delays = self.log.delays()
        summary = summarize(delays)
        rate = self.log.pass_rate()
        values = {
            "n": str(summary.n),
            "mean": _num(summary.mean),
            "median": _num(summary.median),
            "stdev": _num(summary.stdev),
            "min": _num(summary.minimum),
            "max": _num(summary.maximum),
            "p95": _num(summary.p95),
            "pass": "—" if rate is None
                    else f"{rate * 100:.0f}%  ({sum(t.passed for t in self.log.trials)}/{len(self.log)})",
        }
        for key, text in values.items():
            self.stat_labels[key].setText(text)
        self.histogram.set_delays(delays)
        self.imr.set_delays(delays)

    def _refresh_readiness(self) -> None:
        batch_busy = self.batch.busy
        self.batch_run_button.setEnabled(
            not batch_busy and self.runner.phase == IDLE
        )
        self.batch_stop_button.setEnabled(batch_busy and not self.batch.stopping)

        if self.runner.phase != IDLE or batch_busy:
            where = (
                f"Batch trial {self.batch.index}/{self.batch.total} "
                f"({self.batch.phase})"
                if batch_busy
                else f"Trial in progress ({self.runner.phase})"
            )
            self.readiness.setText(f"{where}.")
            self.readiness.setStyleSheet(BUSY_STYLE)
            self.run_button.setEnabled(False)
            return

        self.run_button.setEnabled(True)
        reason = self.runner.preconditions(self.store, self._connected)
        state, fault = self._latest_labels()
        observed = f"Serial: {'connected' if self._connected else 'disconnected'}  |  STATE={state}  |  FAULT={fault}"
        if reason is None:
            self.readiness.setText(f"{observed}\nReady to inject.")
            self.readiness.setStyleSheet(OK_STYLE)
        else:
            self.readiness.setText(f"{observed}\nBlocked: {reason}.")
            self.readiness.setStyleSheet(BAD_STYLE)

    def _latest_labels(self) -> tuple[str, str]:
        if not self.store.t:
            return "—", "—"
        i = len(self.store.t) - 1
        return (
            _enum(STATE_LABELS, self.store.series.get("STATE", []), i),
            _enum(FAULT_LABELS, self.store.series.get("FAULT", []), i),
        )

    # --- actions -----------------------------------------------------------

    def _on_export(self) -> None:
        if not len(self.log):
            QMessageBox.information(self, "Nothing to save", "No trials recorded yet.")
            return
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path, _ = QFileDialog.getSaveFileName(
            self, "Export trials", f"trials_{stamp}.csv", "CSV files (*.csv)"
        )
        if not path:
            return
        try:
            count = write_trials_csv(path, self.log.trials)
        except OSError as exc:
            QMessageBox.critical(self, "Save failed", str(exc))
            return
        QMessageBox.information(self, "Saved", f"{count} trials written to\n{path}")

    def _on_clear(self) -> None:
        if not len(self.log):
            return
        confirm = QMessageBox.question(
            self, "Clear trials",
            f"Discard all {len(self.log)} recorded trials?\n"
            "Export first if you still need them.",
        )
        if confirm != QMessageBox.Yes:
            return
        self.log.clear()
        self.table.setRowCount(0)
        self._refresh_stats()

    def _set_status(self, text: str, style: str = MUTED_STYLE) -> None:
        self.run_status.setText(text)
        self.run_status.setStyleSheet(style)

    def _set_batch_status(self, text: str, style: str = MUTED_STYLE) -> None:
        self.batch_status.setText(text)
        self.batch_status.setStyleSheet(style)

    def _set_ssh_status(self, text: str, style: str = MUTED_STYLE) -> None:
        self.ssh_status.setText(text)
        self.ssh_status.setStyleSheet(style)

    def shutdown(self) -> None:
        """Stop the batch and let in-flight SSH threads finish on exit."""
        # Closing the window is not a trial result: drop the batch rather than
        # leave its timers firing into a half-torn-down panel.
        self.batch.abort("the console is closing")
        threads = (
            self._probe,
            getattr(self.runner, "_ssh", None),
            getattr(self.batch, "_ssh", None),
        )
        for thread in threads:
            if thread is not None and thread.isRunning():
                thread.wait(3000)


def _num(value: float | None) -> str:
    return "—" if value is None else f"{value:.1f}"


def _enum(labels: dict[int, str], column: list[float], i: int) -> str:
    if i >= len(column):
        return "—"
    value = column[i]
    if value != value:
        return "?"
    return labels.get(int(value), str(int(value)))
