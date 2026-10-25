"""
Watch the power at one fixed sensor position through a timed sequence of full-canvas fills, without
touching anything: separates a projector that changes with its recent history (for example thermal
regulation) from a change at the sensor.

Usage:
    python pm_monitor.py <label> [--sequence black:180,white:300,black:120,white:180]
                         [--wavelength 550] [--out runs/pm_20261024]

Each step shows canvas_fill at that code (black 0, white 255, or a number) for the given seconds and
reads the meter continuously (about 3 readings/s at averaging 100). The sensor must already be placed.
Writes <out>/monitor_<label>_<utc>/readings.jsonl and prints a per-step summary: first 10 s, last 10 s,
and the time for white to settle within 0.1% of its final level.
"""

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from calibration_run import CanvasFillScenes
from citrus_control import CitrusControl
from power_meter import PM100USB

SOCKET = "/tmp/citrus_local_control.sock"
CODES = {"black": 0, "white": 255}


def parse_sequence(text):
    steps = []
    for part in text.split(","):
        name, seconds = part.split(":")
        code = CODES.get(name, None)
        steps.append((name, int(name) if code is None else code, float(seconds)))
    return steps


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("label")
    ap.add_argument("--sequence", default="black:180,white:300,black:120,white:180")
    ap.add_argument("--wavelength", type=float, default=550)
    ap.add_argument("--out", default="runs/pm_20261024")
    args = ap.parse_args(argv)
    steps = parse_sequence(args.sequence)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = Path(args.out) / f"monitor_{args.label}_{stamp}"
    out.mkdir(parents=True)
    log = open(out / "readings.jsonl", "w")

    run_id = f"monitor_{args.label}_{stamp}"
    plan = {"arena_ids": ["arena_1", "arena_2", "arena_3", "arena_4"],
            "positions": [{"position_id": "fixed", "arena_id": "arena_1", "offset_mm": [0, 0]}]}
    scenes = CanvasFillScenes(CitrusControl(SOCKET, run_id), plan, run_id)
    meter = PM100USB()
    rows = []
    try:
        settings = meter.configure(100, range_upper_W=0.0178, lowpass=True)
        meter.set_wavelength(args.wavelength)
        log.write(json.dumps({"event": "started", "sequence": args.sequence, "meter_settings": settings,
                              "wavelength_nm": args.wavelength}) + "\n")
        total = sum(s for _, _, s in steps)
        print(f"Running {args.sequence} ({total / 60:.1f} min). Do not touch the rig.")
        for i, (name, code, seconds) in enumerate(steps):
            scenes.show([code, code, code, 255], plan["positions"][0])
            t0 = time.time()
            while time.time() - t0 < seconds:
                p = meter.read_power()
                row = {"step": i, "name": name, "code": code, "t_s": time.time() - t0, "utc_s": time.time(),
                       "power_W": p}
                rows.append(row)
                log.write(json.dumps(row) + "\n")
            log.flush()
            print(f"  step {i} {name} done")
    finally:
        try:
            scenes.finish()
        finally:
            meter.close()
            log.close()

    print(f"\n{'step':>4s} {'fill':>6s} {'first 10 s uW':>14s} {'last 10 s uW':>13s} {'change':>8s} {'settled after':>14s}")
    for i, (name, code, seconds) in enumerate(steps):
        r = [x for x in rows if x["step"] == i]
        first = [x["power_W"] for x in r if x["t_s"] < 10]
        last = [x["power_W"] for x in r if x["t_s"] > seconds - 10]
        f, l = sum(first) / len(first), sum(last) / len(last)
        settled = next((x["t_s"] for x in r if all(abs(y["power_W"] - l) <= 0.001 * l
                                                   for y in r if y["t_s"] >= x["t_s"])), None)
        print(f"{i:4d} {name:>6s} {f * 1e6:14.3f} {l * 1e6:13.3f} {(l / f - 1) * 100 if f else 0:+7.2f}% "
              f"{settled if settled is not None else float('nan'):12.0f} s")
    print(f"\nSaved {out}. Citrus restored.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
