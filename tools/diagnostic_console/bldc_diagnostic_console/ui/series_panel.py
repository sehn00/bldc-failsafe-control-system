"""Right-hand series list: colour chip + on/off per signal."""

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..constants import GROUPS, SERIES_BY_GROUP


class SeriesPanel(QWidget):
    series_toggled = Signal(str, bool)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.boxes: dict[str, QCheckBox] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(2)

        title = QLabel("<b>Series</b>")
        layout.addWidget(title)

        for group in GROUPS:
            heading = QLabel(group.title)
            heading.setStyleSheet("color: #666; margin-top: 6px;")
            if group.description:
                heading.setToolTip(group.description)
            layout.addWidget(heading)

            for spec in SERIES_BY_GROUP[group.key]:
                row = QHBoxLayout()
                row.setSpacing(6)

                chip = QFrame()
                chip.setFixedSize(12, 12)
                chip.setStyleSheet(
                    f"background: {spec.color}; border: 1px solid #888;"
                )
                row.addWidget(chip)

                box = QCheckBox(spec.label)
                box.setChecked(spec.visible)
                box.toggled.connect(
                    lambda checked, n=spec.name: self.series_toggled.emit(n, checked)
                )
                self.boxes[spec.name] = box
                row.addWidget(box, 1)

                container = QWidget()
                container.setLayout(row)
                layout.addWidget(container)

        buttons = QHBoxLayout()
        show_all = QPushButton("Show all")
        hide_all = QPushButton("Hide all")
        show_all.clicked.connect(lambda: self._set_all(True))
        hide_all.clicked.connect(lambda: self._set_all(False))
        buttons.addWidget(show_all)
        buttons.addWidget(hide_all)
        layout.addSpacing(8)
        layout.addLayout(buttons)
        layout.addStretch(1)

    def _set_all(self, checked: bool) -> None:
        for box in self.boxes.values():
            box.setChecked(checked)
