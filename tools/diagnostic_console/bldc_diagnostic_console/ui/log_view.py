"""Raw receive log, capped so a long session cannot exhaust memory."""

from PySide6.QtGui import QFont
from PySide6.QtWidgets import QPlainTextEdit

from ..constants import MAX_LOG_LINES


class LogView(QPlainTextEdit):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setReadOnly(True)
        self.setMaximumBlockCount(MAX_LOG_LINES)
        self.setFont(QFont("Monospace", 9))
        self.setLineWrapMode(QPlainTextEdit.NoWrap)

    def append_lines(self, lines: list[str]) -> None:
        if not lines:
            return
        bar = self.verticalScrollBar()
        at_bottom = bar.value() >= bar.maximum() - 4
        self.appendPlainText("\n".join(lines))
        if at_bottom:
            bar.setValue(bar.maximum())
