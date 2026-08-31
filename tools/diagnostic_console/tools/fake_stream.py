"""Emit synthetic CUR telemetry on a pseudo-terminal, for testing without a board.

Linux/macOS only (uses pty).  Run it, then paste the printed device path into the
Port box of the grapher and press Connect.

    python tools/fake_stream.py [--period 0.5] [--no-header]
"""

import argparse
import math
import os
import pty
import sys
import time

HEADER = "TYPE,RAW_PK_A,FILT_PK_A,FILT_AVG_A,MAX_FLTCNT,DUTY,VDC,RPM,STATE,FAULT"


def rows(count: int | None = None):
    """Yield CUR lines: spin up, run, then an overcurrent trip and recovery."""
    i = 0
    while count is None or i < count:
        t = i * 0.5
        if i < 6:
            state, fault, rpm, duty = 0 if i < 2 else 1, 0, 0, 0
            raw = 0.05
        elif i < 40:
            state, fault = 2, 0
            rpm = min(380, 20 * (i - 6))
            duty = 9
            raw = 0.6 + 0.2 * math.sin(t)
        elif i < 46:
            state, fault = 3, 1  # overcurrent trip
            rpm = max(0, 380 - 90 * (i - 40))
            duty = 0
            raw = 3.4 - 0.4 * (i - 40)
        else:
            state, fault = 1, 0
            rpm, duty, raw = 0, 0, 0.05

        filt_pk = raw * 0.63
        filt_avg = raw * 0.19
        fltcnt = 0 if i < 40 else 3
        yield (
            f"CUR,{raw:.3f},{filt_pk:.3f},{filt_avg:.3f},{fltcnt},"
            f"{duty},{23.98 - 0.3 * (rpm / 400):.2f},{rpm},{state},{fault}"
        )
        i += 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--period", type=float, default=0.5)
    ap.add_argument("--no-header", action="store_true")
    args = ap.parse_args()

    master, slave = pty.openpty()
    print(f"fake telemetry on: {os.ttyname(slave)}", flush=True)

    if not args.no_header:
        os.write(master, (HEADER + "\r\n").encode())
    try:
        for line in rows():
            os.write(master, (line + "\r\n").encode())
            time.sleep(args.period)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
