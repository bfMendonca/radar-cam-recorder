#!/usr/bin/env python3
"""Test double for rpicam-vid: same flags, writes video/pts/metadata until SIGINT.

FAKE_RPICAM_MODE=exit1 exits 1 at once; =ignore_sigint ignores SIGINT.
"""

import json
import os
import signal
import sys
import threading
import time

FRAME_S = 0.04


def main(argv: list[str]) -> int:
    args: dict[str, str] = {}
    i = 1
    while i < len(argv):
        if argv[i] in ("-o", "--save-pts", "--metadata"):
            args[argv[i]] = argv[i + 1]
            i += 2
        else:
            i += 1
    mode = os.environ.get("FAKE_RPICAM_MODE", "")
    if mode == "exit1":
        print("fake rpicam: no cameras available", file=sys.stderr)
        return 1
    stop = threading.Event()
    if mode != "ignore_sigint":
        signal.signal(signal.SIGINT, lambda *_: stop.set())
    else:
        signal.signal(signal.SIGINT, signal.SIG_IGN)
    t0 = time.monotonic()
    with (
        open(args["-o"], "wb") as video,
        open(args["--save-pts"], "w") as pts,
        open(args["--metadata"], "w") as meta,
    ):
        pts.write("# timecode format v2\n")
        meta.write("[\n")
        n = 0
        while True:
            frame = {
                "ExposureTime": 10000 + n,
                "Lux": 500.0,
                "FrameWallClock": time.time_ns() // 1000,
                "SensorTimestamp": time.monotonic_ns(),
            }
            video.write(b"\0" * 1024)
            pts.write("%.3f\n" % (n * FRAME_S * 1000))
            meta.write(("" if n == 0 else ",\n") + json.dumps(frame, indent=4))
            for fh in (video, pts, meta):
                fh.flush()
            n += 1
            if stop.wait(max(0.0, t0 + n * FRAME_S - time.monotonic())) and mode != "ignore_sigint":
                break
        meta.write("\n]\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
