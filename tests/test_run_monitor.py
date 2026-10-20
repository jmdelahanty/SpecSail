"""
Tests for run_monitor: exporters, the terminal watcher and the local server,
against a small synthetic bundle in the calibration_run.py format.

Run:  pixi run test
"""

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from run_monitor import export
from run_monitor.server import make_server
from run_monitor.watch import Watcher

N_PIXELS = 1044
DARK_LEVEL = 1500.0


def wavelength(pixel):
    return 347.0 + 0.556 * pixel


def counts(level_per_s, integration_us, shape="white"):
    """One scan: dark level everywhere, plus a peak whose height scales with level and time."""
    seconds = integration_us / 1e6
    out = []
    for p in range(N_PIXELS):
        if p in export.DARK:
            out.append(DARK_LEVEL)
            continue
        w = wavelength(p)
        if shape == "white":
            s = sum(a * max(0.0, 1 - abs(w - c) / 20) for a, c in ((1.0, 456), (0.6, 530), (1.0, 629)))
        else:
            s = max(0.0, 1 - abs(w - 629) / 15)
        out.append(DARK_LEVEL + level_per_s * seconds * s)
    return out


class Bundle:
    """Writes a run bundle like calibration_run.py does."""

    def __init__(self, root):
        self.root = Path(root)
        (self.root / "measurements").mkdir(parents=True)
        self.log = self.root / "log.jsonl"
        self.seq = 0
        plan = {"plan_id": "test_plan",
                "positions": [{"position_id": "arena_1_center", "arena_id": "arena_1"},
                              {"position_id": "arena_2_center", "arena_id": "arena_2"}],
                "patches": [{"patch_id": "red_255"}, {"patch_id": "gray_077_protocol"}],
                "drift_reference": {"every_n_patches": 10}}
        (self.root / "run_plan.json").write_text(json.dumps(plan))
        self.event("run_started", run_id="test_run")

    def event(self, name, **fields):
        with open(self.log, "a") as f:
            f.write(json.dumps({"utc": "2026-10-20T15:00:00+00:00", "event": name, **fields}) + "\n")

    def measure(self, position, patch, level_per_s, scans=(1.0, 1.0, 1.0), shape="white",
                role="projector_white", drift=False, integration_us=1_000_000):
        self.seq += 1
        mid = f"{position}__{patch}__{self.seq:04d}"
        mdir = self.root / "measurements" / mid
        mdir.mkdir()
        cols = [counts(level_per_s * k, integration_us, shape) for k in scans]
        lines = ["pixel,wavelength_nm," + ",".join(f"counts_scan_{i}" for i in range(len(cols)))]
        for p in range(N_PIXELS):
            lines.append(f"{p},{wavelength(p):.4f}," + ",".join(f"{c[p]:.1f}" for c in cols))
        (mdir / "spectrum.csv").write_text("\n".join(lines) + "\n")
        record = {"measurement_id": mid, "role": role, "position_id": position,
                  "arena_id": position.split("_center")[0], "integration_us": integration_us,
                  "stability": {"stable": True, "attempts": 1, "relative_change": 0.0, "tolerance": 0.01},
                  "any_saturated": False, "patch_id": patch}
        if drift:
            record["drift_reference"] = True
        (mdir / "measurement.json").write_text(json.dumps(record))
        self.event("measurement", measurement_id=mid, role=role, integration_us=integration_us, saturated=False)
        return mid


def make_bundle(root):
    b = Bundle(root)
    b.event("probe_placed", position_id="arena_1_center")
    b.measure("arena_1_center", "drift_reference_start", 100.0, drift=True)
    b.measure("arena_1_center", "red_255", 50.0, shape="red", role="projector_red_primary")
    b.measure("arena_1_center", "gray_077_protocol", 2.0, scans=(1.0, 1.1, 1.0), role="projector_rgb_level")
    b.measure("arena_1_center", "white_255", 100.0)
    b.measure("arena_1_center", "drift_reference_end", 101.0, drift=True)
    b.measure("arena_1_center", "black", 0.0, role="projector_black")
    return b


class ExportTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.run = Path(self.tmp.name) / "test_run"
        self.bundle = make_bundle(self.run)

    def test_run_log_rows(self):
        rows = export.run_log_rows(self.run)
        meas = [r for r in rows if r["kind"] == "measurement"]
        events = [r for r in rows if r["kind"] == "event"]
        status = rows[-1]
        self.assertEqual(len(meas), 6)
        self.assertEqual([e["event"] for e in events], ["run_started", "probe_placed"])
        self.assertEqual(status["kind"], "status")
        self.assertEqual(status["measurements_done"], 6)
        self.assertEqual(status["position_id"], "arena_1_center")
        self.assertEqual(status["measurements_expected"], 2 * (2 + 2 + 0 + 1))
        self.assertEqual(status["state"], "running")          # log just written
        self.assertTrue(status["waiting_for_operator"])        # last entry is the paired dark
        drift = [r for r in meas if r["drift_reference"]]
        self.assertEqual(drift[0]["drift_change_vs_first"], 0.0)
        self.assertAlmostEqual(drift[1]["drift_change_vs_first"], 0.01, places=4)
        gray = next(r for r in meas if r["patch_id"] == "gray_077_protocol")
        self.assertEqual((gray["channel"], gray["level"], gray["protocol_gray"]), ("gray", 77, True))
        self.assertGreater(gray["scan_spread"], 0.05)
        red = next(r for r in meas if r["patch_id"] == "red_255")
        self.assertEqual((red["channel"], red["level"]), ("red", 255))

    def test_state_sealed(self):
        (self.run / "capture_context.json").write_text("{}")
        status = export.run_log_rows(self.run)[-1]
        self.assertEqual(status["state"], "sealed")
        self.assertFalse(status["waiting_for_operator"])

    def test_spectra_rows(self):
        rows = export.spectra_rows(self.run)
        self.assertEqual(rows[0]["kind"], "wavelengths")
        self.assertEqual(len(rows[0]["values"]), len(range(4, N_PIXELS - 4, 2)))
        spectra = rows[1:]
        self.assertEqual(len(spectra), 6)
        white = next(s for s in spectra if s["patch_id"] == "white_255")
        self.assertEqual(len(white["values"]), len(rows[0]["values"]))
        peak = max(white["values"])
        self.assertAlmostEqual(peak, 100.0, delta=5.0)       # counts/s per pixel at the blue/red peaks
        black = next(s for s in spectra if s["patch_id"] == "black")
        self.assertAlmostEqual(max(abs(v) for v in black["values"]), 0.0, places=6)

    def test_library_rows(self):
        rows = export.library_rows([("A", self.run)])
        self.assertEqual(rows[0]["kind"], "wavelengths")
        keyed = {r["patch_id"]: r for r in rows[1:]}
        self.assertEqual(set(keyed), {"drift_reference_start", "red_255", "gray_077_protocol", "white_255"})
        red = keyed["red_255"]
        self.assertAlmostEqual(red["peak_red_nm"], 629, delta=1.0)
        self.assertGreater(red["share_red"], 0.8)
        self.assertEqual(red["run_label"], "A")
        white = keyed["white_255"]
        self.assertAlmostEqual(white["share_blue"] + white["share_green"] + white["share_red"], 1.0, delta=0.2)

    def test_partial_measurement_is_skipped(self):
        self.bundle.event("measurement", measurement_id="arena_2_center__white_255__0099")
        rows = export.run_log_rows(self.run)
        self.assertEqual(sum(1 for r in rows if r["kind"] == "measurement"), 6)
        self.assertEqual(len(export.spectra_rows(self.run)), 7)


class WatchTest(unittest.TestCase):

    def test_watcher_prints_drift_progress_and_events_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp) / "run"
            bundle = make_bundle(run)
            state = Path(tmp) / "state.json"
            lines = []
            watcher = Watcher(run, state, lines.append)
            watcher.poll()
            self.assertTrue(any(line.startswith("DRIFT REF") and "+1.00%" in line for line in lines))
            self.assertTrue(any("probe_placed" in line for line in lines))
            self.assertTrue(any("scan spread" in line for line in lines))
            n = len(lines)
            Watcher(run, state, lines.append).poll()        # restarted: nothing repeated
            self.assertEqual(len(lines), n)
            for i in range(4):
                bundle.measure("arena_2_center", f"gray_{i:03d}", 5.0, role="projector_rgb_level")
            Watcher(run, state, lines.append).poll()
            self.assertTrue(any(line.startswith("progress: 10 measurements") for line in lines))


class ServerTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.run = Path(self.tmp.name) / "run"
        make_bundle(self.run)
        self.server = make_server(self.run, port=0)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=10) as res:
            return res.status, res.headers.get("Content-Type"), res.read()

    def test_endpoints(self):
        status, ctype, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", ctype)
        self.assertIn(b"SpecSail run monitor", body)
        rows = json.loads(self.get("/data/run_log.json")[2])
        self.assertEqual(rows[-1]["measurements_done"], 6)
        spectra = json.loads(self.get("/data/spectra.json")[2])
        self.assertEqual(len(spectra), 7)
        library = json.loads(self.get("/data/library.json")[2])
        self.assertEqual(library[1]["run_label"], "this run")

    def test_unknown_paths(self):
        for path in ("/data/nope.json", "/etc/passwd", "/../run_plan.json"):
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                self.get(path)
            self.assertEqual(ctx.exception.code, 404)
            ctx.exception.close()

    def test_cache_refreshes_when_log_grows(self):
        first = json.loads(self.get("/data/spectra.json")[2])
        b = Bundle.__new__(Bundle)   # append to the existing bundle without rewriting its plan
        b.root, b.log, b.seq = self.run, self.run / "log.jsonl", 50
        b.measure("arena_2_center", "white_255", 80.0)
        second = json.loads(self.get("/data/spectra.json")[2])
        self.assertEqual(len(second), len(first) + 1)


if __name__ == "__main__":
    unittest.main()
