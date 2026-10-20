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
from citrus_control import CitrusControl, CitrusControlError
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


def arena_fills_plan(**settings):
    plan = copy.deepcopy(BASE_PLAN)
    plan["scene_backend"] = "arena_fills"
    plan["positions"] = [
        {"position_id": "arena_1_all", "arena_id": "arena_1", "pattern": "all"},
        {"position_id": "arena_1_target_only", "arena_id": "arena_1", "pattern": "target_only"},
        {"position_id": "arena_1_others", "arena_id": "arena_1", "pattern": "all_but_target"},
    ]
    plan["patches"] = [{"patch_id": "white_255", "role": "projector_white", "rgba8": [255, 255, 255, 255]}]
    if settings:
        plan["arena_fills"] = settings
    return plan


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
        self.assertEqual(context["citrus_build"]["git_commit_hash"], "fake")
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

    def test_scene_records_swap_time_and_full_canvas_status(self):
        citrus = self.fake_citrus()
        bundle = self.make_run(BASE_PLAN, citrus).run()
        mdir = next((bundle / "measurements").glob("arena_1_center__white_255__*"))
        status = json.loads((mdir / "measurement.json").read_text())["scene"]["status"]
        self.assertIsInstance(status["visible_from_display_frame_swap_end_epoch_ns"], int)
        self.assertEqual(status["canvas_fill"]["photodiode_marker"]["drawn"], False)
        self.assertIsNone(status["canvas_fill"]["overlay_line_rgba_u8"])
        context = self.read_context(bundle)
        placement = context["probe_observations"][0]["placement_scene_status"]["canvas_fill"]
        self.assertTrue(placement["placement_overlay"])
        self.assertEqual([t["active"] for t in placement["probe_targets"]], [True, False])

    def test_request_refusal_stops_without_restore(self):
        citrus = self.fake_citrus()
        plan = copy.deepcopy(BASE_PLAN)
        plan["arena_ids"] = ["arena_1", "arena_2", "arena_3"]  # Citrus has four
        plan["positions"] = plan["positions"][:1]
        run = self.make_run(plan, citrus)
        with self.assertRaises(CitrusControlError) as caught:
            run.run()
        self.assertIn("canvas_fill_requires_all_arenas", str(caught.exception))
        self.assertEqual(citrus.restores, 0)
        self.assertNotIn("restore failed", self.read_context(run.bundle.root)["error"])

    def test_controller_rejection_stops_without_restore(self):
        citrus = self.fake_citrus(controller_reject="probe_target_scale_unavailable:arena_1")
        run = self.make_run(BASE_PLAN, citrus)
        with self.assertRaises(CitrusControlError) as caught:
            run.run()
        self.assertIn("probe_target_scale_unavailable", str(caught.exception))
        self.assertEqual(citrus.restores, 0)
        self.assertNotIn("restore failed", self.read_context(run.bundle.root)["error"])

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

    def test_pattern_needs_arena_fills_backend(self):
        self.check_invalid(lambda p: p["positions"][0].update(pattern="target_only"),
                           "pattern needs scene_backend arena_fills")

    def test_arena_fills_pattern_must_be_known(self):
        plan = arena_fills_plan()
        plan["positions"][0]["pattern"] = "neighbours"
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError) as caught:
                load_plan(write_plan(tmp, plan))
        self.assertIn("pattern must be one of", str(caught.exception))

    def test_arena_fills_domain_must_be_known(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError) as caught:
                load_plan(write_plan(tmp, arena_fills_plan(domain="circle")))
        self.assertIn("arena_fills.domain", str(caught.exception))

    def test_core_v1_patches_are_valid(self):
        plan = copy.deepcopy(BASE_PLAN)
        plan["patches"] = core_v1_patches()
        with tempfile.TemporaryDirectory() as tmp:
            loaded, _ = load_plan(write_plan(tmp, plan))
        self.assertEqual(len(loaded["patches"]), 4 + 4 * 8 + 3)



class ArenaFillsRunTest(unittest.TestCase):
    """Runs with the arena_fills backend (Citrus C7) against the fake Citrus."""

    setUp = CalibrationRunTest.setUp
    make_run = CalibrationRunTest.make_run
    fake_citrus = CalibrationRunTest.fake_citrus
    read_context = CalibrationRunTest.read_context

    def measurements_by_position(self, bundle):
        out = {}
        for path in sorted((bundle / "measurements").glob("*/measurement.json")):
            m = json.loads(path.read_text())
            if m.get("patch_id") == "white_255":
                out[m["position_id"]] = m
        return out

    def test_arena_fills_patterns_light_the_right_arenas(self):
        citrus = self.fake_citrus()
        bundle = self.make_run(arena_fills_plan(), citrus).run()
        by_pos = self.measurements_by_position(bundle)
        self.assertEqual(by_pos["arena_1_all"]["scene"]["pattern"], "all")
        self.assertEqual(sorted(by_pos["arena_1_all"]["scene"]["arena_fill_rgba_u8"]),
                         ["arena_1", "arena_2", "arena_3", "arena_4"])
        self.assertEqual(list(by_pos["arena_1_target_only"]["scene"]["arena_fill_rgba_u8"]), ["arena_1"])
        self.assertEqual(sorted(by_pos["arena_1_others"]["scene"]["arena_fill_rgba_u8"]),
                         ["arena_2", "arena_3", "arena_4"])
        self.assertEqual(by_pos["arena_1_others"]["scene"]["background_rgba_u8"], [0, 0, 0, 255])
        requests = [r for r in citrus.requests if r["method"] == "set_calibration_scene"]
        self.assertTrue(all(r["params"]["recipe_id"] == "arena_fills" for r in requests))
        self.assertTrue(all(sorted(r["params"]["arena_ids"]) == BASE_PLAN["arena_ids"] for r in requests))
        self.assertEqual(self.read_context(bundle)["outcome"], "completed")
        self.assertEqual(citrus.restores, 1)

    def test_arena_fills_measures_stray_light_from_other_arenas(self):
        citrus = self.fake_citrus(stray_fraction=0.01)
        bundle = self.make_run(arena_fills_plan(), citrus).run()
        by_pos = self.measurements_by_position(bundle)
        # The fiber sits at arena_1. With arena_1 held black and three others lit,
        # it still sees the stray light; with only arena_1 lit it sees less than "all".
        def peak(m):
            return max(s["peak_counts"] for s in m["scans"]) / m["integration_us"]
        self.assertGreater(peak(by_pos["arena_1_others"]), 0.0)
        self.assertLess(peak(by_pos["arena_1_target_only"]), peak(by_pos["arena_1_all"]))

    def test_arena_fills_background_patch_mode(self):
        citrus = self.fake_citrus()
        bundle = self.make_run(arena_fills_plan(background="patch"), citrus).run()
        by_pos = self.measurements_by_position(bundle)
        self.assertEqual(by_pos["arena_1_target_only"]["scene"]["background_rgba_u8"], [255, 255, 255, 255])

    def test_arena_fills_overlay_left_on_refuses_capture(self):
        citrus = self.fake_citrus(overlay_stuck=True)
        run = self.make_run(arena_fills_plan(), citrus)
        with self.assertRaises(RunAborted):
            run.run()
        context = self.read_context(run.bundle.root)
        self.assertEqual(context["outcome"], "failed")
        self.assertIn("overlay", context["error"])
        self.assertEqual(citrus.restores, 1)

    def test_citrus_without_arena_fills_is_refused(self):
        citrus = self.fake_citrus(arena_fills_supported=False)
        run = self.make_run(arena_fills_plan(), citrus)
        with self.assertRaises(CitrusControlError):
            run.run()
        context = self.read_context(run.bundle.root)
        self.assertEqual(context["outcome"], "failed")
        self.assertIn("unsupported_calibration_scene_recipe", context["error"])
        self.assertEqual(citrus.restores, 0)


if __name__ == "__main__":
    unittest.main()
