"""
Tests for calibration_run.py against a fake Citrus and a fake spectrometer.

Run:  pixi run test
"""

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from calibration_run import (CalibrationRun, RunAborted, core_v1_patches,
                             gray_pilot_patches, load_plan)
from citrus_control import CitrusControl
from spectral_capture import SpectralDevice
from tests.fakes import FakeCitrus, FakeSpectrometer, ScriptedOperator


BASE_PLAN = {
    "schema_id": "citrus.projector_spectral_run_plan",
    "schema_version": 1,
    "plan_id": "test_plan",
    "rig_id": "omnifin0",
    "canvas_name": "shadow",
    "scene_backend": "canvas_fill",
    "target_plane": "projected_surface",
    "arena_ids": ["arena_1", "arena_2", "arena_3", "arena_4"],
    "settle_s": 0.0,
    "scans_per_patch": 2,
    "stability": {"max_relative_change": 0.01, "max_attempts": 3},
    "integration": {"initial_ms": 100, "target_peak_fraction": 0.8, "max_ms": 10000},
    "drift_reference": {"rgba8": [255, 255, 255, 255], "every_n_patches": 2},
    "positions": [
        {"position_id": "arena_1_center", "arena_id": "arena_1", "offset_mm": [0.0, 0.0]},
        {"position_id": "arena_2_center", "arena_id": "arena_2", "offset_mm": [0.0, 0.0]},
    ],
    "patches": [
        {"patch_id": "red_255", "role": "projector_red_primary", "rgba8": [255, 0, 0, 255]},
        {"patch_id": "gray_128", "role": "projector_rgb_level", "rgba8": [128, 128, 128, 255]},
        {"patch_id": "white_255", "role": "projector_white", "rgba8": [255, 255, 255, 255]},
    ],
    "citations": {"spectrometer_instance_id": "spectrometer-qepro-1"},
}


def write_plan(directory, plan):
    path = Path(directory) / "plan.json"
    path.write_text(json.dumps(plan))
    return path


class CalibrationRunTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def make_run(self, plan, citrus, operator=None, counts_per_ms_at_full=400.0):
        plan_obj, sha = load_plan(write_plan(self.tmp.name, plan))
        device = SpectralDevice(FakeSpectrometer(citrus, arena_id=plan["positions"][0]["arena_id"],
                                                 counts_per_ms_at_full=counts_per_ms_at_full),
                                "fake")
        control = CitrusControl(citrus.socket_path, run_id="t", timeout_s=2.0)
        return CalibrationRun(plan_obj, sha, control, device, operator or ScriptedOperator(),
                              Path(self.tmp.name) / "runs", run_id="run1")

    def fake_citrus(self, **kwargs):
        citrus = FakeCitrus(**kwargs)
        self.addCleanup(citrus.close)
        return citrus

    def read_context(self, bundle):
        return json.loads((bundle / "capture_context.json").read_text())

    def test_canvas_fill_run_seals_complete_bundle(self):
        citrus = self.fake_citrus()
        bundle = self.make_run(BASE_PLAN, citrus).run()
        context = self.read_context(bundle)
        self.assertEqual(context["outcome"], "completed")
        self.assertEqual(context["citrus_build"]["git_commit"], "fake")
        # per position: 3 patches + drift start/after_2/end, then one paired dark per integration
        roles = [m["role"] for m in context["measurements"]]
        self.assertEqual(roles.count("projector_red_primary"), 2)
        self.assertEqual(roles.count("projector_white"), 2 * (1 + 3))
        self.assertGreaterEqual(roles.count("projector_black"), 2)
        self.assertFalse(any(m["any_saturated"] for m in context["measurements"]))
        # every file is listed with a correct digest
        for entry in context["files"]:
            data = (bundle / entry["path"]).read_bytes()
            self.assertEqual(hashlib.sha256(data).hexdigest(), entry["sha256"])
        # one transaction for the run, restored once, overlay sent then turned off
        sets = [r for r in citrus.requests if r["method"] == "set_calibration_scene"]
        self.assertEqual({r["params"]["transaction_id"] for r in sets}, {"run1"})
        self.assertTrue(sets[0]["params"]["scene_options"]["placement_overlay"])
        self.assertEqual(len(sets[0]["params"]["scene_options"]["probe_targets"]), 2)
        self.assertEqual(citrus.restores, 1)
        self.assertIsNone(citrus.active_transaction)

    def test_measurement_records_raw_native_pixels_and_scene(self):
        citrus = self.fake_citrus()
        bundle = self.make_run(BASE_PLAN, citrus).run()
        mdir = next((bundle / "measurements").glob("arena_1_center__red_255__*"))
        record = json.loads((mdir / "measurement.json").read_text())
        self.assertEqual(record["rgba8"], [255, 0, 0, 255])
        self.assertFalse(record["nonlinearity_correction_applied"])
        self.assertTrue(record["stability"]["stable"])
        self.assertEqual(record["scene"]["status"]["canvas_fill"]["placement_overlay"], False)
        lines = (mdir / "spectrum.csv").read_text().splitlines()
        self.assertEqual(lines[0], "pixel,wavelength_nm,counts_scan_0,counts_scan_1")
        self.assertEqual(len(lines), 1 + 1044)
        readout = json.loads((bundle / "instrument" / "device_readout.json").read_text())
        self.assertEqual(readout["electric_dark_pixel_indices"], [0, 1, 2, 3, 1040, 1041, 1042, 1043])
        self.assertEqual(readout["tec"]["temperature_c"], -9.8)

    def test_auto_integration_avoids_saturation(self):
        citrus = self.fake_citrus()
        plan = copy.deepcopy(BASE_PLAN)
        plan["integration"]["initial_ms"] = 2000  # would saturate white at the fake's gain
        bundle = self.make_run(plan, citrus).run()
        context = self.read_context(bundle)
        lit = [m for m in context["measurements"] if m["role"] != "projector_black"]
        self.assertFalse(any(m["any_saturated"] for m in lit))

    def test_integration_is_whole_frames_and_not_below_minimum(self):
        citrus = self.fake_citrus()
        plan = copy.deepcopy(BASE_PLAN)
        plan["integration"].update(min_ms=100, frame_rate_hz=120)
        bundle = self.make_run(plan, citrus, counts_per_ms_at_full=1500.0).run()
        frame_us = 1e6 / 120
        for m in self.read_context(bundle)["measurements"]:
            self.assertGreaterEqual(m["integration_us"], 100000)
            frames = m["integration_us"] / frame_us
            self.assertAlmostEqual(frames, round(frames), delta=1.0 / frame_us)

    def test_saturation_at_minimum_integration_aborts(self):
        citrus = self.fake_citrus()
        run = self.make_run(BASE_PLAN, citrus, counts_per_ms_at_full=5000.0)
        with self.assertRaises(RunAborted) as caught:
            run.run()
        self.assertIn("minimum integration", str(caught.exception))
        self.assertEqual(citrus.restores, 1)

    def test_overlay_left_on_refuses_capture_and_restores(self):
        citrus = self.fake_citrus(overlay_stuck=True)
        run = self.make_run(BASE_PLAN, citrus)
        with self.assertRaises(RunAborted):
            run.run()
        self.assertEqual(citrus.restores, 1)
        context = self.read_context(run.bundle.root)
        self.assertEqual(context["outcome"], "failed")
        self.assertIn("overlay", context["error"])
        self.assertEqual(context["measurements"], [])

    def test_citrus_without_canvas_fill_is_refused(self):
        citrus = self.fake_citrus(canvas_fill_supported=False)
        run = self.make_run(BASE_PLAN, citrus)
        with self.assertRaises(Exception):
            run.run()
        self.assertEqual(self.read_context(run.bundle.root)["outcome"], "failed")

    def test_interrupt_restores_citrus_and_seals(self):
        citrus = self.fake_citrus()
        run = self.make_run(BASE_PLAN, citrus,
                            ScriptedOperator(interrupt_on="Fiber placed"))
        with self.assertRaises(KeyboardInterrupt):
            run.run()
        self.assertIsNone(citrus.active_transaction)
        self.assertEqual(self.read_context(run.bundle.root)["outcome"], "interrupted")

    def test_gray_pilot_uses_one_transaction_per_position(self):
        citrus = self.fake_citrus()
        plan = copy.deepcopy(BASE_PLAN)
        plan["scene_backend"] = "uniform_gray"
        plan["patches"] = [p for p in gray_pilot_patches() if p["rgba8"][0] in (64, 255)]
        plan["drift_reference"] = {"rgba8": [255, 255, 255, 255], "every_n_patches": 0}
        bundle = self.make_run(plan, citrus).run()
        self.assertEqual(self.read_context(bundle)["outcome"], "completed")
        sets = [r for r in citrus.requests if r["method"] == "set_calibration_scene"]
        self.assertEqual({r["params"]["transaction_id"] for r in sets},
                         {"run1-arena_1_center", "run1-arena_2_center"})
        self.assertEqual(sets[0]["params"]["recipe_id"], "experimental_area_center_and_outline")
        self.assertEqual(citrus.restores, 2)


class PlanValidationTest(unittest.TestCase):

    def check_invalid(self, mutate, needle):
        plan = copy.deepcopy(BASE_PLAN)
        mutate(plan)
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError) as caught:
                load_plan(write_plan(tmp, plan))
        self.assertIn(needle, str(caught.exception))

    def test_alpha_must_be_opaque(self):
        self.check_invalid(lambda p: p["patches"][0].update(rgba8=[255, 0, 0, 254]), "alpha")

    def test_uniform_gray_needs_gray_patches(self):
        self.check_invalid(lambda p: p.update(scene_backend="uniform_gray"), "R=G=B")

    def test_black_is_not_a_plan_patch(self):
        self.check_invalid(
            lambda p: p["patches"].append(
                {"patch_id": "black", "role": "projector_black", "rgba8": [0, 0, 0, 255]}),
            "black is paired")

    def test_position_arena_must_be_listed(self):
        self.check_invalid(lambda p: p["positions"][0].update(arena_id="arena_9"), "arena_9")

    def test_minimum_integration_ordering(self):
        self.check_invalid(lambda p: p["integration"].update(min_ms=200), "min_ms <= initial_ms")

    def test_minimum_integration_covers_a_frame(self):
        self.check_invalid(lambda p: p["integration"].update(min_ms=5, initial_ms=5, frame_rate_hz=120),
                           "at least one frame")

    def test_fixed_patch_integration_respects_minimum(self):
        self.check_invalid(lambda p: p["patches"][0].update(integration_ms=50), ">= integration.min_ms")

    def test_core_v1_patches_are_valid(self):
        plan = copy.deepcopy(BASE_PLAN)
        plan["patches"] = core_v1_patches()
        with tempfile.TemporaryDirectory() as tmp:
            loaded, _ = load_plan(write_plan(tmp, plan))
        self.assertEqual(len(loaded["patches"]), 4 + 4 * 8 + 3)


if __name__ == "__main__":
    unittest.main()
