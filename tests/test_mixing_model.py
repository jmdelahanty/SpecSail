"""
Tests for analysis/mixing/mixing_model.py on synthetic spectra (no run bundles needed).

Run:  pixi run test
"""

import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "analysis" / "mixing"))
import mixing_model as mm  # noqa: E402


def gaussian(wl, peak, width):
    return np.exp(-(((wl - peak) / width) ** 2))


class TestMixingModel(unittest.TestCase):
    def setUp(self):
        self.wl = np.arange(350.0, 801.0, 1.0)
        self.c0, self.gamma = 50.0, 2.2
        primaries = {"red": gaussian(self.wl, 629, 12), "green": gaussian(self.wl, 550, 30),
                     "blue": gaussian(self.wl, 456, 12)}
        black = 0.002 * (primaries["red"] + primaries["green"] + primaries["blue"])
        codes = [0, 32, 64, 96, 128, 160, 192, 224, 255]
        self.patches = {"black": (black, 1000, None)}
        for ch, p in primaries.items():
            for c in codes:
                self.patches[f"{ch}_{c}"] = (black + self.true_t(c) * p, 1000, None)
        for c in (77, 89, 178, 255):
            s = black + sum(self.true_t(c) * p for p in primaries.values())
            self.patches["white_255" if c == 255 else f"gray_{c}"] = (s, 1000, None)
        self.model = mm.build_model(self.wl, self.patches)

    def true_t(self, c):
        return max((c - self.c0) / (255.0 - self.c0), 0.0) ** self.gamma

    def test_transfer_fit_recovers_offset_power_law(self):
        for ch in mm.CHANNELS:
            c0, gamma = self.model["fit"][ch]
            self.assertAlmostEqual(c0, self.c0, delta=0.5)
            self.assertAlmostEqual(gamma, self.gamma, delta=0.02)

    def test_additivity_on_additive_data(self):
        for key, r in mm.additivity(self.wl, self.model, self.patches).items():
            self.assertLess(abs(r["integrated_error"]), 0.01, key)

    def test_code_for_inverts_transfer(self):
        for c in (80, 128, 200, 255):
            t = float(mm.transfer_at(self.model, "green", c))
            self.assertEqual(mm.code_for(self.model, "green", t), c)

    def test_template_peaks_at_lmax(self):
        for lmax in (360.0, 416.0, 470.0, 550.0):
            s = mm.govardovskii_a1(self.wl, lmax)
            self.assertAlmostEqual(self.wl[np.argmax(s)], lmax, delta=3)

    def test_red_target_needs_red_and_no_exact_match_for_488(self):
        cones = [mm.govardovskii_a1(self.wl, l) for l in (360.0, 416.0, 470.0, 550.0)]
        m631 = mm.metamer(self.wl, self.model, cones, 631.0)
        self.assertEqual(m631["best_rgb_code"][0], 255)
        self.assertGreater(m631["progress_white_to_target"], 0.0)
        m488 = mm.metamer(self.wl, self.model, cones, 488.0)
        self.assertFalse(m488["exact_sml_match_possible"])


if __name__ == "__main__":
    unittest.main()
