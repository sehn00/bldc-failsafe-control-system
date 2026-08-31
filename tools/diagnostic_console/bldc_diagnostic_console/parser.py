"""Line parser for the positional CUR telemetry records."""

from dataclasses import dataclass

from .constants import (
    DATA_RECORD_TYPE,
    DEFAULT_COLUMNS,
    HEADER_FIRST_FIELD,
)


@dataclass
class ParseResult:
    kind: str  # "data" | "header" | "other" | "invalid"
    values: dict[str, float] | None = None
    reason: str = ""


class TelemetryParser:
    """Maps ``CUR,...`` lines onto column names.

    The column order is seeded from :data:`DEFAULT_COLUMNS` because the boot-time
    header is missed whenever the PC attaches to an already-running board.  A
    header line, if one does arrive, replaces the seeded order.
    """

    def __init__(self) -> None:
        self.columns: list[str] = list(DEFAULT_COLUMNS)
        self.header_seen = False

    def reset(self) -> None:
        self.columns = list(DEFAULT_COLUMNS)
        self.header_seen = False

    def parse(self, line: str) -> ParseResult:
        text = line.strip()
        if not text:
            return ParseResult("other")

        fields = [f.strip() for f in text.split(",")]
        tag = fields[0].upper()

        if tag == HEADER_FIRST_FIELD:
            if len(fields) < 2:
                return ParseResult("invalid", reason="header has no columns")
            self.columns = fields
            self.header_seen = True
            return ParseResult("header")

        if tag != DATA_RECORD_TYPE:
            # Boot banners, warnings, framing garbage from a baud mismatch.
            return ParseResult("other")

        if len(fields) != len(self.columns):
            return ParseResult(
                "invalid",
                reason=f"expected {len(self.columns)} fields, got {len(fields)}",
            )

        values: dict[str, float] = {}
        for name, raw in zip(self.columns[1:], fields[1:]):
            try:
                values[name] = float(raw)
            except ValueError:
                return ParseResult(
                    "invalid", reason=f"{name}={raw!r} is not a number"
                )
        return ParseResult("data", values=values)
