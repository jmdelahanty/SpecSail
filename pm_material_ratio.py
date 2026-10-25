"""
Bracketed with/without material ratios from a power-meter material session.

A material plan alternates five positions, s1 WITHOUT, s2 WITH, s3 WITHOUT, s4 WITH, s5 WITHOUT. Each
WITH reading is divided by the mean of the WITHOUT readings on either side (s2 by s1 and s3, s4 by s3
and s5), per fill, after subtracting that position's black. Slow drift and placement then enter only
through the WITHOUT spread, which is printed.

Usage:
    python pm_material_ratio.py <session_dir>
"""

import csv
import sys
from pathlib import Path

PAIRS = {"s2": ("s1", "s3"), "s4": ("s3", "s5")}


def load(session_dir):
    """{position prefix (s1..s5): {fill_id: mean_W}} from summary.csv."""
    out = {}
    with open(Path(session_dir) / "power_meter" / "summary.csv", newline="") as f:
        for r in csv.DictReader(f):
            out.setdefault(r["position_id"][:2], {})[r["fill_id"]] = float(r["mean_W"])
    return out


def ratios(data):
    """Per fill: the bracketed WITH/WITHOUT ratio for s2 and s4, and the WITHOUT spread (fraction)."""
    fills = [f for f in data["s1"] if f != "black" and not f.startswith("white_drift")]
    net = {p: {f: v[f] - v["black"] for f in fills} for p, v in data.items()}
    result = {}
    for f in fills:
        without = [net[p][f] for p in ("s1", "s3", "s5")]
        mean_without = sum(without) / 3
        result[f] = {
            "s2": net["s2"][f] / ((net["s1"][f] + net["s3"][f]) / 2),
            "s4": net["s4"][f] / ((net["s3"][f] + net["s5"][f]) / 2),
            "without_spread": (max(without) - min(without)) / mean_without,
            # each WITH against each neighbour alone, to locate a step change in placement
            "adjacent": {f"{w}/{o}": net[w][f] / net[o][f] for w, pair in PAIRS.items() for o in pair},
        }
    return result


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print(__doc__)
        return 2
    res = ratios(load(argv[0]))
    print(f"{'fill':20s} {'s2':>8s} {'s4':>8s} {'mean':>8s} {'s2-s4':>7s} {'WITHOUT spread':>15s}")
    for f, r in res.items():
        print(f"{f:20s} {r['s2']:8.4f} {r['s4']:8.4f} {(r['s2'] + r['s4']) / 2:8.4f} "
              f"{abs(r['s2'] - r['s4']) * 100:6.2f}% {r['without_spread'] * 100:14.2f}%")
    if any(r["without_spread"] > 0.005 for r in res.values()):
        print("\nWITHOUT spread above 0.5%: a placement changed. Ratios against each neighbour alone:")
        for f, r in res.items():
            print(f"{f:20s} " + "  ".join(f"{k} {v:.4f}" for k, v in r["adjacent"].items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
