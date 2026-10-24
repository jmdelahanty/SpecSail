"""Frame-content (APL) check: does arena 1's light change with what the rest of the canvas shows?

usage: pm_frame_content_check.py <label> [--codes 255,178] [--repeats 4] [--settle 1.0] [--n 10]
                                 [--out runs/pm_20261024]

The power meter stays on arena 1 (disc in). For each patch code it cycles five screen states,
in forward then reverse order so slow drift cancels:
  BLACK   canvas_fill 0                      (all four arenas owned)
  ONE     uniform_gray code on arena_1 only  (arenas 2-4 show their GUI previews: must be black)
  ARENAS  uniform_gray code on all four arenas, outside the arenas black
  FULL    canvas_fill code over the whole canvas
  OTHERS  uniform_gray code on arenas 2-4    (arena 1 shows its GUI preview: must be black)
Citrus fixes a transaction's arena set, so ONE and OTHERS each get their own transaction.
Before measuring, ONE and OTHERS are shown once for the operator to confirm by eye.

Result: (ARENAS - ONE) is the effect of lighting the other arenas; (OTHERS - BLACK) is the stray
light those arenas put on the sensor by scatter. If they agree, the projector does not change its
output with frame content; any excess is the projector. (FULL - ARENAS) is the same for the
canvas outside the arenas, with no scatter control (read it as an upper bound).
Writes <out>/apl_<label>_<utc>/readings.jsonl and summary.json. Needs the canvas_fill (C6) Citrus build.
"""
import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from citrus_control import CitrusControl  # noqa: E402
from power_meter import PM100USB  # noqa: E402

ALL = ["arena_1", "arena_2", "arena_3", "arena_4"]
SETS = {"BLACK": ALL, "ARENAS": ALL, "FULL": ALL, "ONE": ["arena_1"], "OTHERS": ["arena_2", "arena_3", "arena_4"]}
ORDER = ["BLACK", "ONE", "ARENAS", "FULL", "OTHERS"]

ap = argparse.ArgumentParser()
ap.add_argument("label")
ap.add_argument("--codes", default="255,178")
ap.add_argument("--repeats", type=int, default=4)
ap.add_argument("--settle", type=float, default=1.0)
ap.add_argument("--n", type=int, default=10)
ap.add_argument("--out", default="runs/pm_20261024")
args = ap.parse_args()
codes = [int(c) for c in args.codes.split(",")]
stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
out = Path(args.out) / f"apl_{args.label}_{stamp}"
out.mkdir(parents=True)
log = open(out / "readings.jsonl", "w")


def note(event, **kw):
    log.write(json.dumps({"utc": datetime.now(timezone.utc).isoformat(), "event": event, **kw}) + "\n")
    log.flush()


citrus = CitrusControl("/tmp/citrus_local_control.sock", f"apl_{args.label}_{stamp}")
current = {"tx": None, "set": None}


def restore():
    if current["tx"]:
        rid, oid = citrus.restore_scene(current["tx"])
        citrus.wait_for_state(rid, oid, "restored")
        current["tx"] = current["set"] = None


def show(state, code):
    ids = SETS[state]
    if current["set"] != ids:
        restore()
        current["tx"] = f"apl_{args.label}_{stamp}_{state if ids != ALL else 'ALL'}"
        current["set"] = ids
    if state in ("BLACK", "FULL"):
        c = 0 if state == "BLACK" else code
        opts = {"foreground_rgba_u8": [c, c, c, 255], "placement_overlay": False,
                "probe_targets": [{"target_id": "arena_1_center", "arena_id": "arena_1", "offset_mm": [0.0, 0.0]}],
                "active_target": None}
        rid, oid = citrus.set_scene(current["tx"], "canvas_fill", ids, opts)
        st = citrus.wait_for_state(rid, oid, "presented")
        shown = st.get("canvas_fill", {}).get("foreground_rgba_u8")
        if list(shown or []) != [c, c, c, 255] or st["canvas_fill"].get("placement_overlay") is not False:
            raise SystemExit(f"Citrus shows {shown}, expected {[c, c, c, 255]}")
    else:
        rid, oid = citrus.set_scene(current["tx"], "uniform_gray", ids, {"foreground_gray_u8": code})
        st = citrus.wait_for_state(rid, oid, "presented")
        grays = sorted({t.get("uniform_illumination", {}).get("gray_u8") for t in st.get("resolved_targets", [])})
        if grays != [code]:
            raise SystemExit(f"Citrus reports grays {grays}, expected {code}")
    return st.get("visible_from_display_frame_swap_end_utc")


meter = PM100USB()
try:
    idn, sensor = meter.idn(), meter.sensor_info()
    settings = meter.configure(100, range_upper_W=1.78e-3, lowpass=True)
    meter.set_wavelength(550)
    note("started", label=args.label, codes=codes, repeats=args.repeats, settle_s=args.settle, n=args.n,
         meter=str(idn), sensor=str(sensor), meter_settings=settings, wavelength_nm=550)
    print(f"Meter {idn}, sensor {sensor}, {settings}")
    answers = {}
    for state, question in (("ONE", "Is ONLY arena 1 lit (arenas 2-4 and everything outside black)?"),
                            ("OTHERS", "Is arena 1 black, with arenas 2-4 lit and outside black?")):
        show(state, 255)
        a = input(f"\n{state}: {question} Look without touching anything. [y/N]: ").strip().lower()
        answers[state] = a
        if a not in ("y", "yes"):
            note("visual_check_failed", state=state, answer=a)
            raise SystemExit(f"{state} does not look right. Set the arena previews to black in the "
                             "Citrus GUI and run again. Nothing was measured.")
    note("visual_checks", **answers)
    input("\nStep back from the rig. Press Enter to start (about 2 minutes per code)... ")
    for code in codes:
        for r in range(args.repeats):
            for state in (ORDER if r % 2 == 0 else ORDER[::-1]):
                visible = show(state, code)
                time.sleep(args.settle)
                rd = meter.read_n(args.n)
                p = [x["power_W"] for x in rd]
                note("reading", code=code, repeat=r, state=state, visible_from_utc=visible,
                     mean_W=float(np.mean(p)), std_W=float(np.std(p, ddof=1)), readings=rd)
                print(f"  code {code} r{r} {state:6s} {np.mean(p) * 1e6:10.3f} uW")
finally:
    try:
        restore()
    finally:
        meter.close()

rows = [json.loads(line) for line in open(out / "readings.jsonl") if '"reading"' in line]
summary = {"label": args.label, "dir": str(out), "codes": {}}
for code in codes:
    m = {s: np.array([r["mean_W"] for r in rows if r["code"] == code and r["state"] == s]) for s in ORDER}
    ref = m["ONE"].mean() - m["BLACK"].mean()
    d = {
        "one_net_uW": ref * 1e6,
        "others_lit_minus_one_pct": (m["ARENAS"].mean() - m["ONE"].mean()) / ref * 100,
        "scatter_from_others_pct": (m["OTHERS"].mean() - m["BLACK"].mean()) / ref * 100,
        "outside_lit_minus_arenas_pct": (m["FULL"].mean() - m["ARENAS"].mean()) / ref * 100,
        "repeat_sd_pct": {s: float(np.std(m[s], ddof=1) / ref * 100) for s in ORDER},
    }
    d["excess_over_scatter_pct"] = d["others_lit_minus_one_pct"] - d["scatter_from_others_pct"]
    summary["codes"][str(code)] = d
    print(f"\ncode {code}: arena 1 alone {d['one_net_uW']:.2f} uW net")
    print(f"  other arenas lit:      {d['others_lit_minus_one_pct']:+.3f}%  "
          f"(their scatter onto arena 1: {d['scatter_from_others_pct']:+.3f}%; "
          f"excess = projector: {d['excess_over_scatter_pct']:+.3f}%)")
    print(f"  outside arenas lit:    {d['outside_lit_minus_arenas_pct']:+.3f}% (scatter and projector together)")
    print("  repeat sd (% of net):  " + " ".join(f"{s} {v:.3f}" for s, v in d["repeat_sd_pct"].items()))
json.dump(summary, open(out / "summary.json", "w"), indent=1)
print(f"\nSaved {out}. Citrus restored.")
