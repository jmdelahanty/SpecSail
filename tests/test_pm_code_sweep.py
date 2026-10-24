"""
Tests for pm_code_sweep.py on a synthetic sweep with a known offset-gamma curve.

Run:  pixi run test
"""

import csv
import tempfile
import unittest
from pathlib import Path

import numpy as np

from pm_code_sweep import fit_channel, load_sweep

FULL_W = {"white": 350e-6, "red": 110e-6, "green": 157e-6, "blue": 93e-6}
# The same dark light reads differently at each channel's meter wavelength.
DARK_W = {"white": 724e-9, "red": 621e-9, "green": 745e-9, "blue": 893e-9}


def write_sweep(root, c0=48.0, gamma=2.22, drift=0.002, seed=1):
    rng = np.random.default_rng(seed)
    fills = [(ch, c) for ch in FULL_W for c in range(256)]
    rng.shuffle(fills)
    rows = []

    def add(fill_id, mean_W):
        rows.append({"position_id": "p", "fill_id": fill_id, "role": "fill", "wavelength_nm": 550,
                     "mean_W": mean_W + rng.normal(0, 3e-9), "std_W": 1e-8, "n": 10,
                     "mean_minus_black_W": "", "irradiance_uW_per_cm2": "", "irradiance_provisional": True})

    def scale():
        return 1 + drift * len(rows) / (len(fills) * 1.1)

    add("white_drift_start", FULL_W["white"] * scale() + DARK_W["white"])
    for k, (ch, c) in enumerate(fills):
        if k % 32 == 0:
            add(f"ref{k // 32:02d}_white_255", FULL_W["white"] * scale() + DARK_W["white"])
            add(f"ref{k // 32:02d}_black", DARK_W["white"])
        frac = ((c - c0) / (255 - c0)) ** gamma if c > c0 else 0.0
        add(f"{ch}_{c:03d}", FULL_W[ch] * frac * scale() + DARK_W[ch])
    add("white_drift_end", FULL_W["white"] * scale() + DARK_W["white"])
    out = Path(root) / "power_meter"
    out.mkdir(parents=True)
    with open(out / "summary.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


class CodeSweepTests(unittest.TestCase):
    def test_fit_recovers_offset_and_gamma_per_channel(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_sweep(tmp)
            sweep = load_sweep(tmp, own_black=True)
        for ch, (codes, net) in sweep["curves"].items():
            f = fit_channel(codes, net)
            self.assertAlmostEqual(f["c0"], 48.0, delta=1.0, msg=ch)
            self.assertAlmostEqual(f["gamma"], 2.22, delta=0.03, msg=ch)
            self.assertLess(f["max_resid"], 0.002, msg=ch)
            self.assertTrue(49 <= f["first_lit"] <= 56, msg=(ch, f["first_lit"]))

    def test_own_black_removes_wavelength_offset(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_sweep(tmp)
            raw = load_sweep(tmp)
            own = load_sweep(tmp, own_black=True)
        codes, red = raw["curves"]["red"]
        self.assertLess(np.median(red[codes <= 40]), -80e-9)   # read below the 550 nm black
        codes, red = own["curves"]["red"]
        self.assertLess(abs(np.median(red[codes <= 40])), 1e-9)

    def test_drift_is_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_sweep(tmp, drift=0.02)
            sweep = load_sweep(tmp, own_black=True)
        codes, white = sweep["curves"]["white"]
        mean_white = sweep["refs"]["white_net_W"].mean()
        self.assertAlmostEqual(white[codes == 255][0] / mean_white, 1.0, delta=0.002)


if __name__ == "__main__":
    unittest.main()
