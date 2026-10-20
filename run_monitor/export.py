"""
Flatten a calibration_run.py bundle into JSON rows for the run monitor.

Three row sets, all lists of plain dicts:

- run_log_rows(run_dir): one "event" row per log entry that is not a
  measurement, one "measurement" row per measurement (signal, flags, drift vs
  the first drift reference at the same position), and a final "status" row.
- spectra_rows(run_dir): a "wavelengths" row, then one "spectrum" row per
  measurement (mean of scans minus the electrical dark level, per second,
  every `step`-th detector pixel).
- library_rows(labelled_runs): key patches from several runs on one grid, with
  the 720-800 nm flat offset also removed and per-spectrum shape features, for
  comparing spectral shapes across runs.

Counts are relative (not radiometric). Signals are summed over the detector
and divided by integration time, in M counts/s.
"""

import csv
import json
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

DARK = (0, 1, 2, 3, 1040, 1041, 1042, 1043)
RECENT_S = 300                 # log.jsonl touched this recently: run counts as running
LIBRARY_PATCHES = ("white_255", "red_255", "green_255", "blue_255", "drift_reference_start",
                   "gray_077_protocol", "gray_089_protocol", "gray_178_protocol")


def read_log(run_dir):
    path = Path(run_dir) / "log.jsonl"
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:   # last line may be half written
            continue
    return out


def read_plan(run_dir):
    path = Path(run_dir) / "run_plan.json"
    return json.loads(path.read_text()) if path.exists() else {}


def read_scans(csv_path):
    """Return (wavelengths, scans) with scans as a list of per-pixel count lists."""
    with open(csv_path) as f:
        rows = list(csv.reader(f))[1:]
    n = len(rows[0]) - 2
    wavelengths = [float(r[1]) for r in rows]
    scans = [[float(r[2 + i]) for r in rows] for i in range(n)]
    return wavelengths, scans


def scan_signal(counts):
    """Dark-subtracted counts summed over the illuminated pixels."""
    dark = sum(counts[j] for j in DARK) / len(DARK)
    return sum(v - dark for j, v in enumerate(counts) if j not in DARK)


def measurement_signal(csv_path, integration_us):
    """(mean rate in M counts/s, relative spread between scans) for one measurement."""
    _, scans = read_scans(csv_path)
    seconds = integration_us / 1e6
    rates = [scan_signal(s) / seconds / 1e6 for s in scans]
    mean = sum(rates) / len(rates)
    spread = (max(rates) - min(rates)) / abs(mean) if mean else None
    return mean, spread


def parse_patch(patch_id):
    """(channel, level) from a patch id such as gray_077_protocol or red_255."""
    if patch_id.startswith("drift_reference"):
        return "drift", 255
    m = re.match(r"(red|green|blue|gray|white)_(\d{3})", patch_id)
    if m:
        return m.group(1), int(m.group(2))
    if patch_id.startswith("black"):
        return "black", 0
    return "other", None


def acquirer_process_running():
    try:
        out = subprocess.run(["pgrep", "-f", "calibration_run|calrun"],
                             capture_output=True, text=True, timeout=2)
    except (OSError, subprocess.SubprocessError):
        return False
    return bool(out.stdout.strip())


def run_state(run_dir):
    """sealed | running | stopped."""
    run_dir = Path(run_dir)
    if (run_dir / "capture_context.json").exists():
        return "sealed"
    log = run_dir / "log.jsonl"
    if log.exists() and time.time() - log.stat().st_mtime < RECENT_S:
        return "running"
    return "running" if acquirer_process_running() else "stopped"


def expected_measurements(plan):
    patches = plan.get("patches")
    n_patches = len(patches) if isinstance(patches, list) else 0
    every = (plan.get("drift_reference") or {}).get("every_n_patches") or 0
    interim = n_patches // every if every else 0
    # start and end drift references, interim ones, and at least one paired dark
    per_position = n_patches + 2 + interim + 1
    return per_position * len(plan.get("positions", []))


def _utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def run_log_rows(run_dir):
    run_dir = Path(run_dir)
    plan = read_plan(run_dir)
    rows, first_drift = [], {}
    position = None
    log = read_log(run_dir)
    for rec in log:
        utc = rec.get("utc")
        if rec.get("event") != "measurement":
            detail = {k: v for k, v in rec.items() if k not in ("utc", "event")}
            if "position_id" in detail:
                position = detail["position_id"]
            rows.append({"kind": "event", "utc": utc, "event": rec.get("event"),
                         "detail": json.dumps(detail)[:240]})
            continue
        mid = rec["measurement_id"]
        mdir = run_dir / "measurements" / mid
        try:
            m = json.loads((mdir / "measurement.json").read_text())
            rate, spread = measurement_signal(mdir / "spectrum.csv", m["integration_us"])
        except (OSError, ValueError, KeyError, IndexError, ZeroDivisionError):
            continue   # still being written, or unreadable
        pos = m.get("position_id") or mid.split("__")[0]
        position = pos
        patch_id = m.get("patch_id") or mid.split("__")[1]
        channel, level = parse_patch(patch_id)
        is_drift = bool(m.get("drift_reference"))
        drift_change = None
        if is_drift:
            first_drift.setdefault(pos, rate)
            drift_change = (rate - first_drift[pos]) / first_drift[pos] if first_drift[pos] else None
        stab = m.get("stability") or {}
        tail = mid.rsplit("__", 1)[-1]
        rows.append({
            "kind": "measurement", "utc": utc, "seq": int(tail) if tail.isdigit() else None,
            "measurement_id": mid, "position_id": pos, "arena_id": m.get("arena_id"),
            "patch_id": patch_id, "role": m.get("role"), "channel": channel, "level": level,
            "protocol_gray": patch_id.endswith("_protocol"),
            "integration_s": m["integration_us"] / 1e6, "rate_mcps": round(rate, 5),
            "scan_spread": round(spread, 5) if spread is not None else None,
            "stable": stab.get("stable"), "saturated": bool(m.get("any_saturated")),
            "drift_reference": is_drift,
            "drift_change_vs_first": round(drift_change, 5) if drift_change is not None else None,
        })

    state = run_state(run_dir)
    # Waiting for the operator: the run is live, its last log entry is a
    # measurement, and the measurement is a paired dark (the last step at a
    # position), so the next thing it needs is a placement confirmation.
    waiting = (state == "running" and bool(log) and log[-1].get("event") == "measurement"
               and log[-1].get("role") == "projector_black")
    n_meas = sum(1 for r in rows if r["kind"] == "measurement")
    last = next((r for r in reversed(rows) if r["kind"] == "measurement"), None)
    rows.append({
        "kind": "status", "utc": _utc_now(), "run_id": run_dir.name, "plan_id": plan.get("plan_id"),
        "state": state, "waiting_for_operator": waiting,
        "position_id": position, "measurements_done": n_meas,
        "measurements_expected": expected_measurements(plan),
        "positions_total": len(plan.get("positions", [])),
        "last_measurement_utc": last["utc"] if last else None,
        "last_patch_id": last["patch_id"] if last else None,
    })
    return rows


def _sig(v):
    return float(f"{v:.4g}")


def spectra_rows(run_dir, step=2):
    run_dir = Path(run_dir)
    rows, wavelengths = [], None
    for rec in read_log(run_dir):
        if rec.get("event") != "measurement":
            continue
        mdir = run_dir / "measurements" / rec["measurement_id"]
        try:
            m = json.loads((mdir / "measurement.json").read_text())
            wl, scans = read_scans(mdir / "spectrum.csv")
        except (OSError, ValueError, KeyError, IndexError):
            continue
        n = len(scans)
        mean = [sum(s[j] for s in scans) / n for j in range(len(wl))]
        dark = sum(mean[j] for j in DARK) / len(DARK)
        seconds = m["integration_us"] / 1e6
        keep = range(4, len(wl) - 4, step)
        if wavelengths is None:
            wavelengths = [round(wl[j], 2) for j in keep]
        rows.append({"kind": "spectrum", "measurement_id": rec["measurement_id"],
                     "position_id": m.get("position_id"), "patch_id": m.get("patch_id"),
                     "integration_s": seconds, "drift_reference": bool(m.get("drift_reference")),
                     "values": [_sig((mean[j] - dark) / seconds) for j in keep]})
    return [{"kind": "wavelengths", "values": wavelengths or []}] + rows


def _band(wl, s, lo, hi):
    return sum(v for w, v in zip(wl, s) if lo <= w <= hi)


def _centroid(wl, s, lo, hi):
    pts = [(w, v) for w, v in zip(wl, s) if lo <= w <= hi]
    if not pts:
        return None
    top = max(v for _, v in pts)
    half = [(w, v) for w, v in pts if v > top / 2]
    total = sum(v for _, v in half)
    return round(sum(w * v for w, v in half) / total, 2) if total else None


def _offset_corrected(mdir):
    """Wavelengths and the per-second spectrum with dark level and 720-800 nm flat offset removed."""
    wl, scans = read_scans(mdir / "spectrum.csv")
    m = json.loads((mdir / "measurement.json").read_text())
    n = len(scans)
    mean = [sum(s[j] for s in scans) / n for j in range(len(wl))]
    dark = sum(mean[j] for j in DARK) / len(DARK)
    seconds = m["integration_us"] / 1e6
    rate = [(v - dark) / seconds for v in mean]
    flat = [v for w, v in zip(wl, rate) if 720 <= w <= 800]
    offset = sum(flat) / len(flat) if flat else 0.0
    return wl, [v - offset for v in rate], m


def library_rows(labelled_runs, patches=LIBRARY_PATCHES, step=2):
    """labelled_runs: iterable of (label, run_dir)."""
    rows, grid = [], None
    for label, run_dir in labelled_runs:
        run_dir = Path(run_dir)
        mroot = run_dir / "measurements"
        if not mroot.is_dir():
            continue
        for mdir in sorted(mroot.iterdir()):
            parts = mdir.name.split("__")
            if len(parts) < 3 or parts[1] not in patches:
                continue
            try:
                wl, s, _ = _offset_corrected(mdir)
            except (OSError, ValueError, KeyError, IndexError):
                continue
            keep = range(4, len(wl) - 4, step)
            if grid is None:
                grid = [round(wl[j], 2) for j in keep]
            total = _band(wl, s, 420, 700)
            rows.append({
                "kind": "spectrum", "key": f"{run_dir.name}/{mdir.name}", "run_label": label,
                "run_id": run_dir.name, "position_id": parts[0], "patch_id": parts[1],
                "area_420_700": round(total, 1),
                "share_blue": round(_band(wl, s, 430, 480) / total, 4) if total else None,
                "share_green": round(_band(wl, s, 500, 580) / total, 4) if total else None,
                "share_red": round(_band(wl, s, 600, 660) / total, 4) if total else None,
                "peak_blue_nm": _centroid(wl, s, 430, 490),
                "peak_red_nm": _centroid(wl, s, 600, 660),
                "values": [_sig(s[j]) for j in keep],
            })
    return [{"kind": "wavelengths", "key": "wavelengths", "values": grid or []}] + rows
