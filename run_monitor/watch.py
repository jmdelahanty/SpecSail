"""
Watch a calibration run bundle in the terminal and print one line per notable event.

Notable: each drift reference (signal vs the first one at that position), any
saturated / unstable / noisy measurement, a progress summary every 10
measurements, log events other than measurements (placements, errors, end),
and the run stopping. With --state FILE the watcher remembers what it already
printed, so it can be restarted without repeating itself.

Usage:
    pixi run watch runs/<run_id> [--state FILE] [--interval 10]
"""

import argparse
import json
import sys
import time
from pathlib import Path

from run_monitor import export

SCAN_SPREAD_WARN = 0.02
DRIFT_WARN = 0.02


class Watcher:
    def __init__(self, run_dir, state_path=None, out=print):
        self.run_dir = Path(run_dir)
        self.state_path = Path(state_path) if state_path else None
        self.out = out
        if self.state_path and self.state_path.exists():
            self.state = json.loads(self.state_path.read_text())
        else:
            self.state = {"log_lines": 0, "measurements": 0, "drift_first": {}, "summary": []}

    def save(self):
        if self.state_path:
            self.state_path.write_text(json.dumps(self.state))

    def poll(self):
        """Print lines for new log entries. Returns the number of log lines handled."""
        log_path = self.run_dir / "log.jsonl"
        lines = log_path.read_text().splitlines() if log_path.exists() else []
        handled = 0
        for line in lines[self.state["log_lines"]:]:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                break   # half-written last line: read it again next time
            if rec.get("event") == "measurement" and not self._measurement(rec):
                break   # files not ready yet
            if rec.get("event") != "measurement":
                detail = {k: v for k, v in rec.items() if k not in ("utc", "event")}
                self.out(f"[{rec.get('utc', '')[11:19]}] {rec.get('event')}: {json.dumps(detail)[:300]}")
            self.state["log_lines"] += 1
            handled += 1
        self.save()
        return handled

    def _measurement(self, rec):
        mid = rec["measurement_id"]
        mdir = self.run_dir / "measurements" / mid
        try:
            m = json.loads((mdir / "measurement.json").read_text())
            rate_m, spread = export.measurement_signal(mdir / "spectrum.csv", m["integration_us"])
        except (OSError, ValueError, KeyError, IndexError, ZeroDivisionError):
            return False
        self.state["measurements"] += 1
        integ_s = m["integration_us"] / 1e6
        stab = m.get("stability") or {}
        notes = []
        if m.get("any_saturated"):
            notes.append("SATURATED")
        if stab and not stab.get("stable"):
            notes.append(f"unstable (change {stab.get('relative_change', 0):.1%})")
        if spread is not None and spread > SCAN_SPREAD_WARN:
            notes.append(f"scan spread {spread:.1%}")
        spread_text = f"{spread:.1%}" if spread is not None else "n/a"
        line_out = (f"{mid}: {rate_m:.3f} M counts/s, {integ_s:.2f} s, spread {spread_text}"
                    + (f" | {'; '.join(notes)}" if notes else ""))
        self.state["summary"].append(f"{m.get('patch_id', mid)} {rate_m:.3f}")
        self.state["summary"] = self.state["summary"][-10:]
        if m.get("drift_reference"):
            pos = m.get("position_id")
            first = self.state["drift_first"].setdefault(pos, rate_m)
            change = (rate_m - first) / first if first else 0.0
            flag = "  <-- DRIFT" if abs(change) > DRIFT_WARN else ""
            self.out(f"DRIFT REF {line_out}, vs first at {pos}: {change:+.2%}{flag}")
        elif notes:
            self.out(line_out)
        if self.state["measurements"] % 10 == 0:
            self.out(f"progress: {self.state['measurements']} measurements; last 10 (M counts/s): "
                     + ", ".join(self.state["summary"]))
        return True


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--state", type=Path, help="file remembering what was already printed")
    parser.add_argument("--interval", type=float, default=10.0, help="seconds between polls")
    args = parser.parse_args(argv)

    def out(text):
        print(text, flush=True)

    watcher = Watcher(args.run_dir, args.state, out)
    stopped_polls = 0
    while True:
        watcher.poll()
        state = export.run_state(args.run_dir)
        stopped_polls = stopped_polls + 1 if state != "running" else 0
        if stopped_polls >= 2:   # two polls in a row, so a pause between files doesn't end the watch
            watcher.poll()
            out(f"run is {state} ({watcher.state['measurements']} measurements seen)")
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
