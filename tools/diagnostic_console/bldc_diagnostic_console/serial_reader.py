"""Background serial reader.

Runs off the GUI thread and emits complete text lines.  Framing is done here so
the UI never sees a half-received record.
"""

from PySide6.QtCore import QThread, Signal

import serial
from serial.tools import list_ports

MAX_PENDING_BYTES = 64 * 1024


def available_ports() -> list[tuple[str, str]]:
    """Return (device, description) for every serial port the OS reports."""
    return [(p.device, p.description or "") for p in list_ports.comports()]


class SerialReader(QThread):
    line_received = Signal(str)
    opened = Signal(str)
    closed = Signal()
    failed = Signal(str)

    def __init__(self, port: str, baud: int, parent=None) -> None:
        super().__init__(parent)
        self.port = port
        self.baud = baud
        self._running = False

    def run(self) -> None:
        try:
            ser = serial.Serial(self.port, self.baud, timeout=0.1)
        except Exception as exc:  # pyserial raises several unrelated types
            self.failed.emit(f"{self.port}: {exc}")
            return

        self._running = True
        self.opened.emit(self.port)
        buf = bytearray()
        try:
            while self._running:
                try:
                    chunk = ser.read(max(1, ser.in_waiting))
                except Exception as exc:
                    self.failed.emit(f"read failed: {exc}")
                    break
                if not chunk:
                    continue
                buf.extend(chunk)

                if len(buf) > MAX_PENDING_BYTES:
                    # No newline in 64 KB means the baud rate is wrong; drop the
                    # garbage rather than growing without bound.
                    del buf[:-1024]

                while True:
                    nl = buf.find(b"\n")
                    if nl < 0:
                        break
                    raw = bytes(buf[:nl])
                    del buf[: nl + 1]
                    self.line_received.emit(
                        raw.rstrip(b"\r").decode("utf-8", errors="replace")
                    )
        finally:
            try:
                ser.close()
            except Exception:
                pass
            self.closed.emit()

    def stop(self) -> None:
        self._running = False
        self.wait(2000)
