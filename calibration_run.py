#!/usr/bin/env python3
"""
Projector spectral calibration run.

Steps Citrus calibration scenes while the spectrometer measures, and writes
an immutable capture bundle for the Citrus offline importer. Design:
Citrus ``docs/projector_spectral_calibration_run_design_2026-10-19.md``.

For each probe position in the plan:

1. show a placement scene (crosshair overlay, or the dish outline for the
   gray pilot) and ask the operator to place the fiber;
2. for each patch, set the scene, wait until Citrus reports it presented,
   wait the settle time, check that consecutive scans agree, then record raw
   scans at the native pixel resolution;
3. record a projector-black dark at every integration time used there.

Citrus is always restored, including after errors and Ctrl+C. Nothing is
calibrated, binned or corrected here; the bundle carries raw counts plus the
coefficients needed to correct them later.

Usage
-----
    pixi run calrun examples/calibration_plan_gray_pilot.json
    pixi run calrun PLAN --socket /tmp/citrus_local_control.sock --out runs
"""

import argparse
import hashlib
import json
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from citrus_control import CitrusControl, CitrusControlError


PLAN_SCHEMA_ID = "citrus.projector_spectral_run_plan"
CAPTURE_CONTEXT_SCHEMA_ID = "citrus.projector_spectral_capture_context"
MEASUREMENT_SCHEMA_ID = "specsail.calibration_run.measurement"
SCENE_BACKENDS = ("canvas_fill", "uniform_gray")
TARGET_PLANES = ("projected_surface", "estimated_fish_plane")
ROLES = (
    "projector_red_primary", "projector_green_primary", "projector_blue_primary",
    "projector_white", "projector_rgb_level", "other",
)
DEFAULT_SOCKET = "/tmp/citrus_local_control.sock"
REPO_DIR = Path(__file__).resolve().parent


class RunAborted(RuntimeError):
    """Raised when the operator or a check stops the run."""


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------

def core_v1_patches(include_gray_codes=True):
    """
    The ``core_v1`` patch set from the design doc, without black (darks are
    paired automatically) and without crosstalk (meaningless for a canvas fill).

    Returns
    -------
    list of dict
        Patches with ``patch_id``, ``role`` and ``rgba8``.
    """
    patches = [
        {"patch_id": "red_255", "role": "projector_red_primary", "rgba8": [255, 0, 0, 255]},
        {"patch_id": "green_255", "role": "projector_green_primary", "rgba8": [0, 255, 0, 255]},
        {"patch_id": "blue_255", "role": "projector_blue_primary", "rgba8": [0, 0, 255, 255]},
        {"patch_id": "white_255", "role": "projector_white", "rgba8": [255, 255, 255, 255]},
    ]
    levels = [16, 32, 64, 96, 128, 160, 192, 224]
    for name, mask in (("red", (1, 0, 0)), ("green", (0, 1, 0)), ("blue", (0, 0, 1)), ("gray", (1, 1, 1))):
        for level in levels:
            rgba = [level * m for m in mask] + [255]
            patches.append({"patch_id": f"{name}_{level:03d}", "role": "projector_rgb_level", "rgba8": rgba})
    if include_gray_codes:
        for level, note in ((77, "0.3"), (89, "0.35"), (178, "0.7")):
            patches.append({
                "patch_id": f"gray_{level:03d}_protocol",
                "role": "projector_rgb_level",
                "rgba8": [level, level, level, 255],
                "note": f"code used by live protocols (sRGB {note})",
            })
    return patches


def gray_pilot_patches():
    """Gray-only ramp for the ``uniform_gray`` pilot (works with today's Citrus)."""
    levels = [16, 32, 64, 77, 89, 96, 128, 160, 178, 192, 224, 255]
    return [
        {"patch_id": f"gray_{level:03d}",
         "role": "projector_white" if level == 255 else "projector_rgb_level",
         "rgba8": [level, level, level, 255]}
        for level in levels
    ]


def load_plan(path):
    """
    Load and validate a run plan.

    Returns
    -------
    plan : dict
    plan_sha256 : str
    """
    raw = Path(path).read_bytes()
    plan = json.loads(raw)
    errors = []

    def need(key, kind):
        if not isinstance(plan.get(key), kind):
            errors.append(f"{key} must be {kind.__name__}")

    if plan.get("schema_id") != PLAN_SCHEMA_ID or plan.get("schema_version") != 1:
        errors.append(f"schema must be {PLAN_SCHEMA_ID} v1")
    for key in ("plan_id", "rig_id", "canvas_name", "scene_backend", "target_plane"):
        need(key, str)
    for key in ("arena_ids", "positions", "patches"):
        need(key, list)
    if plan.get("scene_backend") not in SCENE_BACKENDS:
        errors.append(f"scene_backend must be one of {SCENE_BACKENDS}")
    if plan.get("target_plane") not in TARGET_PLANES:
        errors.append(f"target_plane must be one of {TARGET_PLANES}")

    arena_ids = plan.get("arena_ids") or []
    seen = set()
    for pos in plan.get("positions") or []:
        pid = pos.get("position_id")
        if not pid or pid in seen:
            errors.append(f"position_id missing or duplicated: {pid!r}")
        seen.add(pid)
        if pos.get("arena_id") not in arena_ids:
            errors.append(f"position {pid}: arena_id {pos.get('arena_id')!r} not in arena_ids")
        offset = pos.get("offset_mm", [0.0, 0.0])
        if not (isinstance(offset, list) and len(offset) == 2):
            errors.append(f"position {pid}: offset_mm must be [x, y]")

    seen = set()
    for patch in plan.get("patches") or []:
        pid = patch.get("patch_id")
        if not pid or pid in seen:
            errors.append(f"patch_id missing or duplicated: {pid!r}")
        seen.add(pid)
        rgba = patch.get("rgba8")
        if not (isinstance(rgba, list) and len(rgba) == 4 and
                all(isinstance(v, int) and 0 <= v <= 255 for v in rgba)):
            errors.append(f"patch {pid}: rgba8 must be four integers 0-255")
        elif rgba[3] != 255:
            errors.append(f"patch {pid}: alpha must be 255 (never attenuate with alpha)")
        elif plan.get("scene_backend") == "uniform_gray" and not (rgba[0] == rgba[1] == rgba[2]):
            errors.append(f"patch {pid}: uniform_gray backend can only show R=G=B")
        if patch.get("role") not in ROLES:
            errors.append(f"patch {pid}: role must be one of {ROLES} (black is paired automatically)")

    placement = plan.get("placement", {})
    if not isinstance(placement, dict) or not isinstance(placement.get("probe_targets", True), bool):
        errors.append("placement.probe_targets must be true or false")
    elif placement.get("probe_targets") is False:
        for pos in plan.get("positions") or []:
            if [float(v) for v in pos.get("offset_mm", [0.0, 0.0])] != [0.0, 0.0]:
                errors.append(f"position {pos.get('position_id')}: without probe targets only "
                              f"the arena centre (offset_mm [0, 0]) can be placed by the overlay")

    integration = plan.get("integration", {})
    if not isinstance(integration, dict):
        errors.append("integration must be an object")
        integration = {}
    min_ms = integration.get("min_ms", 100)
    max_ms = integration.get("max_ms", 10000)
    initial_ms = integration.get("initial_ms", 100)
    rate = integration.get("frame_rate_hz")
    if not all(isinstance(v, (int, float)) and v > 0 for v in (min_ms, max_ms, initial_ms)):
        errors.append("integration min_ms, initial_ms and max_ms must be positive numbers")
    elif not min_ms <= initial_ms <= max_ms:
        errors.append("integration must satisfy min_ms <= initial_ms <= max_ms")
    if rate is not None and not (isinstance(rate, (int, float)) and rate > 0):
        errors.append("integration.frame_rate_hz must be a positive number")
    elif rate is not None and isinstance(min_ms, (int, float)) and min_ms * 1000 < 1e6 / rate:
        errors.append("integration.min_ms must cover at least one frame at frame_rate_hz")
    for patch in plan.get("patches") or []:
        fixed = patch.get("integration_ms")
        if fixed is not None and isinstance(min_ms, (int, float)) and (
                not isinstance(fixed, (int, float)) or fixed < min_ms):
            errors.append(f"patch {patch.get('patch_id')}: integration_ms must be >= integration.min_ms")

    if errors:
        raise ValueError("invalid plan:\n  " + "\n  ".join(errors))
    return plan, hashlib.sha256(raw).hexdigest()


# ---------------------------------------------------------------------------
# Scene drivers
# ---------------------------------------------------------------------------

class CanvasFillScenes:
    """
    Scenes for the first pass: one colour over the whole canvas (Citrus C6).

    One transaction covers every arena for the whole run. Placement shows the
    overlay with the current target bold; measurement requires it off.
    """

    recipe_id = "canvas_fill"

    def __init__(self, citrus, plan, run_id):
        self.citrus = citrus
        self.plan = plan
        self.transaction_id = run_id
        self.active = False      # a scene of this transaction was presented
        self.requested = False   # Citrus accepted a scene request (may be open)
        self.targets = [
            {"target_id": p["position_id"], "arena_id": p["arena_id"],
             "offset_mm": [float(v) for v in p.get("offset_mm", [0.0, 0.0])]}
            for p in plan["positions"]
        ]

    def _set(self, rgba8, overlay, position):
        options = {"foreground_rgba_u8": list(rgba8), "placement_overlay": overlay}
        if self.plan.get("placement", {}).get("probe_targets", True):
            options["probe_targets"] = self.targets
            options["active_target"] = position["position_id"] if position else None
        request_id, operation_id = self.citrus.set_scene(
            self.transaction_id, self.recipe_id, self.plan["arena_ids"], options)
        self.requested = True
        try:
            status = self.citrus.wait_for_state(request_id, operation_id, "presented")
        except CitrusControlError as exc:
            if "probe_target_scale_unavailable" in str(exc):
                raise RunAborted(
                    f"{exc}: an arena has no accepted projector scale. Set "
                    f"\"placement\": {{\"probe_targets\": false}} in the plan to place by the "
                    f"arena outlines and centre rings (all offsets must then be [0, 0])") from exc
            raise
        # Only a presented scene proves the transaction is open; a scene refused
        # by the controller before any was presented leaves nothing to restore.
        self.active = True
        return status, request_id, operation_id

    def placement(self, position):
        status, _, _ = self._set([255, 255, 255, 255], True, position)
        return status

    def show(self, rgba8, position):
        status, request_id, operation_id = self._set(rgba8, False, position)
        fill = status.get("canvas_fill")
        if not isinstance(fill, dict):
            raise RunAborted("Citrus status has no canvas_fill object: this Citrus build lacks C6")
        if fill.get("placement_overlay") is not False:
            raise RunAborted("Citrus reports the placement overlay is still on; refusing to capture")
        if list(fill.get("foreground_rgba_u8", [])) != list(rgba8):
            raise RunAborted(f"Citrus shows {fill.get('foreground_rgba_u8')}, expected {rgba8}")
        return self._scene_record(status, request_id, operation_id)

    def _scene_record(self, status, request_id, operation_id):
        return {"transaction_id": self.transaction_id, "recipe_id": self.recipe_id,
                "request_id": request_id, "operation_id": operation_id, "status": status}

    def end_position(self, position):
        pass

    def finish(self):
        if self.active:
            request_id, operation_id = self.citrus.restore_scene(self.transaction_id)
            self.active = self.requested = False
            self.citrus.wait_for_state(request_id, operation_id, "restored")
        elif self.requested:
            # Accepted but never presented: the transaction may or may not be
            # open in Citrus. Try a restore; a refusal means there was nothing open.
            self.requested = False
            try:
                request_id, operation_id = self.citrus.restore_scene(self.transaction_id)
            except CitrusControlError:
                return
            self.citrus.wait_for_state(request_id, operation_id, "restored")


class UniformGrayScenes(CanvasFillScenes):
    """
    Scenes for the gray pilot, using recipes Citrus already has.

    One transaction per position targets only that arena. Placement shows the
    experimental-area centre and outline; measurement uses ``uniform_gray``.
    The operator sets the other arenas' previews to black in the GUI.
    """

    recipe_id = "uniform_gray"

    def __init__(self, citrus, plan, run_id):
        super().__init__(citrus, plan, run_id)
        self.run_id = run_id
        self.arena_id = None

    def _begin(self, position):
        self.transaction_id = f"{self.run_id}-{position['position_id']}"
        self.arena_id = position["arena_id"]

    def placement(self, position):
        self._begin(position)
        request_id, operation_id = self.citrus.set_scene(
            self.transaction_id, "experimental_area_center_and_outline", [self.arena_id])
        self.requested = True
        status = self.citrus.wait_for_state(request_id, operation_id, "presented")
        self.active = True
        return status

    def show(self, rgba8, position):
        gray = rgba8[0]
        request_id, operation_id = self.citrus.set_scene(
            self.transaction_id, "uniform_gray", [self.arena_id], {"foreground_gray_u8": gray})
        status = self.citrus.wait_for_state(request_id, operation_id, "presented")
        shown = [t.get("uniform_illumination", {}).get("gray_u8")
                 for t in status.get("resolved_targets", []) if t.get("arena_id") == self.arena_id]
        if shown != [gray]:
            raise RunAborted(f"Citrus reports gray {shown} for {self.arena_id}, expected {gray}")
        return self._scene_record(status, request_id, operation_id)

    def end_position(self, position):
        self.finish()


# ---------------------------------------------------------------------------
# Operator
# ---------------------------------------------------------------------------

class ConsoleOperator:
    """Prompts on the terminal. Tests substitute a scripted operator."""

    def ask(self, prompt, default=""):
        suffix = f" [{default}]" if default else ""
        answer = input(f"{prompt}{suffix}: ").strip()
        return answer or default

    def confirm(self, prompt):
        answer = input(f"{prompt} [y/N]: ").strip().lower()
        if answer not in ("y", "yes"):
            raise RunAborted(f"operator declined: {prompt}")

    def say(self, message):
        print(message, flush=True)


# ---------------------------------------------------------------------------
# Bundle
# ---------------------------------------------------------------------------

def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=False) + "\n")


def write_spectrum_csv(path, wavelengths, scans):
    """Raw counts at native pixels: ``pixel,wavelength_nm,counts_scan_0,...``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    header = "pixel,wavelength_nm," + ",".join(f"counts_scan_{i}" for i in range(len(scans)))
    with open(path, "w") as f:
        f.write(header + "\n")
        for px, wl in enumerate(wavelengths):
            values = ",".join(f"{s['counts'][px]:.1f}" for s in scans)
            f.write(f"{px},{wl:.4f},{values}\n")


def git_identity(repo_dir):
    """Commit, dirty state and pixi.lock digest of this SpecSail checkout."""
    def git(*args):
        return subprocess.run(["git", "-C", str(repo_dir), *args],
                              capture_output=True, text=True, check=False).stdout.strip()
    commit = git("rev-parse", "HEAD") or "unknown"
    porcelain = git("status", "--porcelain")
    lock = repo_dir / "pixi.lock"
    return {
        "repository": git("remote", "get-url", "origin") or "unknown",
        "commit": commit,
        "dirty": bool(porcelain),
        "dirty_paths": porcelain.splitlines(),
        "pixi_lock_sha256": sha256_file(lock) if lock.exists() else "unknown",
    }


class Bundle:
    """The capture bundle directory and its event log."""

    def __init__(self, root):
        self.root = Path(root)
        if self.root.exists():
            raise RunAborted(f"bundle directory already exists: {self.root}")
        self.root.mkdir(parents=True)
        self.log_path = self.root / "log.jsonl"

    def log(self, event, **fields):
        record = {"utc": datetime.now(timezone.utc).isoformat(), "event": event, **fields}
        with open(self.log_path, "a") as f:
            f.write(json.dumps(record) + "\n")

    def file_manifest(self, exclude=()):
        files = []
        for path in sorted(self.root.rglob("*")):
            rel = path.relative_to(self.root).as_posix()
            if path.is_file() and rel not in exclude:
                files.append({"path": rel, "bytes": path.stat().st_size, "sha256": sha256_file(path)})
        return files


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

class CalibrationRun:
    """
    One calibration run: plan + Citrus + spectrometer + operator → bundle.

    Parameters
    ----------
    plan : dict
    plan_sha256 : str
    citrus : CitrusControl
    device : spectral_capture.SpectralDevice
    operator : ConsoleOperator-like
    out_dir : pathlib.Path
        Parent directory; the bundle is ``out_dir/<run_id>``.
    """

    def __init__(self, plan, plan_sha256, citrus, device, operator, out_dir, run_id=None):
        self.plan = plan
        self.plan_sha256 = plan_sha256
        self.citrus = citrus
        self.device = device
        self.operator = operator
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.run_id = run_id or f"{plan['plan_id']}_{stamp}"
        citrus.run_id = self.run_id
        self.bundle = Bundle(Path(out_dir) / self.run_id)
        backend = UniformGrayScenes if plan["scene_backend"] == "uniform_gray" else CanvasFillScenes
        self.scenes = backend(citrus, plan, self.run_id)
        self.measurements = []
        self.observations = []
        self.patch_counter = 0
        self.drift_integration_us = None
        self.started_utc = None
        self.scene_build = None

    # -- settings ---------------------------------------------------------

    def _setting(self, *keys, default=None):
        value = self.plan
        for key in keys:
            if not isinstance(value, dict) or key not in value:
                return default
            value = value[key]
        return value

    # -- capture ----------------------------------------------------------

    def _signal(self, counts):
        return float(np.sum(counts - self.device.dark_level(counts)))

    def _wait_stable(self):
        """Take scan pairs until consecutive signals agree within tolerance."""
        tolerance = self._setting("stability", "max_relative_change", default=0.01)
        attempts = self._setting("stability", "max_attempts", default=5)
        floor = 10.0 * len(self.device.wavelengths)
        previous = self._signal(self.device.scan()["counts"])
        change = None
        for attempt in range(1, attempts + 1):
            current = self._signal(self.device.scan()["counts"])
            change = abs(current - previous) / max(abs(previous), floor)
            if change <= tolerance:
                return {"stable": True, "attempts": attempt, "relative_change": change,
                        "tolerance": tolerance}
            previous = current
        return {"stable": False, "attempts": attempts, "relative_change": change,
                "tolerance": tolerance}

    def _integration_limits(self):
        min_ms = self._setting("integration", "min_ms", default=100)
        max_ms = self._setting("integration", "max_ms", default=10000)
        rate = self._setting("integration", "frame_rate_hz")
        return int(min_ms * 1000), int(max_ms * 1000), (1e6 / rate if rate else None)

    def _quantized(self, integration_us):
        min_us, max_us, frame_us = self._integration_limits()
        return self.device.quantize_integration_us(integration_us, min_us, max_us, frame_us)

    def _integration_for(self, patch):
        if patch.get("integration_ms") is not None:
            return self.device.set_integration_us(self._quantized(patch["integration_ms"] * 1000))
        initial_ms = self._setting("integration", "initial_ms", default=100)
        target = self._setting("integration", "target_peak_fraction", default=0.8)
        min_us, max_us, frame_us = self._integration_limits()
        chosen, saturated_at_minimum = self.device.choose_integration_us(
            int(initial_ms * 1000), target, min_us, max_us, frame_us)
        if saturated_at_minimum:
            raise RunAborted(
                f"patch {patch['patch_id']} saturates at the minimum integration "
                f"({chosen / 1000:.1f} ms): attenuate the light at the fiber; "
                f"do not lower integration.min_ms below whole projector frames")
        return self.device.set_integration_us(chosen)

    def _record(self, measurement_id, role, rgba8, position, scene, extra=None):
        n_scans = self._setting("scans_per_patch", default=5)
        stability = self._wait_stable() if scene is not None else None
        scans = [self.device.scan() for _ in range(n_scans)]
        rel_dir = Path("measurements") / measurement_id
        write_spectrum_csv(self.bundle.root / rel_dir / "spectrum.csv", self.device.wavelengths, scans)
        record = {
            "schema_id": MEASUREMENT_SCHEMA_ID,
            "schema_version": 1,
            "measurement_id": measurement_id,
            "role": role,
            "rgba8": rgba8,
            "color_space": "srgb_drive_code" if rgba8 else None,
            "position_id": position["position_id"] if position else None,
            "arena_id": position["arena_id"] if position else None,
            "offset_mm": position.get("offset_mm", [0.0, 0.0]) if position else None,
            "target_plane": self.plan["target_plane"],
            "integration_us": self.device.integration_us,
            "nonlinearity_correction_applied": False,
            "dark_correction_applied": False,
            "stability": stability,
            "scans": [{k: v for k, v in s.items() if k != "counts"} for s in scans],
            "any_saturated": any(s["saturated_pixel_count"] > 0 for s in scans),
            "scene": scene,
            "spectrum_csv": (rel_dir / "spectrum.csv").as_posix(),
            **(extra or {}),
        }
        write_json(self.bundle.root / rel_dir / "measurement.json", record)
        self.measurements.append({
            "measurement_id": measurement_id, "role": role, "rgba8": rgba8,
            "position_id": record["position_id"], "arena_id": record["arena_id"],
            "integration_us": record["integration_us"], "any_saturated": record["any_saturated"],
            "stable": stability["stable"] if stability else None,
        })
        self.bundle.log("measurement", measurement_id=measurement_id, role=role,
                        integration_us=record["integration_us"], saturated=record["any_saturated"])
        if record["any_saturated"]:
            self.operator.say(f"  WARNING {measurement_id}: saturated pixels")
        return record

    def _measure_patch(self, patch, position, integrations_used, extra=None):
        scene = self.scenes.show(patch["rgba8"], position)
        if isinstance(scene["status"].get("citrus_build"), dict):
            self.scene_build = scene["status"]["citrus_build"]
        settle_s = self._setting("settle_s", default=1.0)
        time.sleep(settle_s)
        if extra and extra.get("drift_reference") and self.drift_integration_us:
            self.device.set_integration_us(self.drift_integration_us)
        else:
            self._integration_for(patch)
            if extra and extra.get("drift_reference"):
                self.drift_integration_us = self.device.integration_us
        integrations_used.add(self.device.integration_us)
        self.patch_counter += 1
        measurement_id = f"{position['position_id']}__{patch['patch_id']}__{self.patch_counter:04d}"
        self.operator.say(f"  {measurement_id}: rgba8 {patch['rgba8']}, "
                          f"{self.device.integration_us / 1000:.1f} ms")
        return self._record(measurement_id, patch["role"], patch["rgba8"], position, scene,
                            {"patch_id": patch["patch_id"], "settle_s": settle_s, **(extra or {})})

    def _drift_reference(self, position, integrations_used, when):
        patch = {"patch_id": f"drift_reference_{when}", "role": "projector_white",
                 "rgba8": self._setting("drift_reference", "rgba8", default=[255, 255, 255, 255])}
        return self._measure_patch(patch, position, integrations_used,
                                   {"drift_reference": True})

    def _paired_darks(self, position, integrations_used):
        black = [0, 0, 0, 255]
        for integration_us in sorted(integrations_used):
            scene = self.scenes.show(black, position)
            time.sleep(self._setting("settle_s", default=1.0))
            self.device.set_integration_us(integration_us)
            self.patch_counter += 1
            measurement_id = (f"{position['position_id']}__black_{integration_us}us__"
                              f"{self.patch_counter:04d}")
            self._record(measurement_id, "projector_black", black, position, scene,
                         {"patch_id": "black", "paired_dark_for_integration_us": integration_us})

    def _place(self, position):
        placement_status = self.scenes.placement(position)
        self.operator.say(
            f"\nPosition {position['position_id']} ({position['arena_id']}, offset "
            f"{position.get('offset_mm', [0, 0])} mm). Place the fiber tip on the marked target.")
        self.operator.confirm("Fiber placed and facing the surface")
        observation = {
            "position_id": position["position_id"],
            "fiber_holder": self.operator.ask("Fiber holder (fixture id or hand_placed)",
                                              self._setting("defaults", "fiber_holder",
                                                            default="hand_placed")),
            "geometry_observation": self.operator.ask(
                "Does the projected outline match the physical dish rim/holder? (note or blank)", ""),
            "confirmed_utc": datetime.now(timezone.utc).isoformat(),
        }
        self.observations.append({**observation, "placement_scene_status": placement_status})
        self.bundle.log("probe_placed", **observation)

    def _preflight(self):
        say = self.operator.say
        say(f"Run {self.run_id}\nPlan {self.plan['plan_id']} ({self.plan['scene_backend']}, "
            f"{len(self.plan['positions'])} positions x {len(self.plan['patches'])} patches)")
        self.operator.confirm("Orange acquisition is stopped (850 nm arrays strobe with exposure)")
        warmup = self._setting("warmup_min", default=30)
        self.operator.confirm(f"Projector has been on for at least {warmup} min")
        if self.plan["scene_backend"] == "uniform_gray":
            self.operator.confirm("Arenas not being measured show black previews in the Citrus GUI")
        tec = self.device.read_tec()
        expected = self._setting("tec", "expected_c")
        if expected is not None and tec["temperature_c"] != "unknown":
            tolerance = self._setting("tec", "tolerance_c", default=0.5)
            if abs(tec["temperature_c"] - expected) > tolerance:
                raise RunAborted(f"TEC at {tec['temperature_c']:.2f} °C, expected "
                                 f"{expected} ± {tolerance} °C")
        self.bundle.log("preflight_ok", tec=tec)

    def _projector_off_background(self):
        if not self._setting("projector_off_background", default=False):
            return
        self.operator.confirm("Put the projector in standby (lamp off) now; done?")
        self.device.set_integration_us(
            self._quantized(self._setting("integration", "initial_ms", default=100) * 1000))
        self._record("projector_off_background__0000", "projector_off_background", None, None, None)
        self.operator.confirm(
            f"Turn the projector back on and wait {self._setting('warmup_min', default=30)} min; done?")

    def run(self):
        """Run the plan. Returns the bundle path."""
        self.started_utc = datetime.now(timezone.utc).isoformat()
        write_json(self.bundle.root / "run_plan.json", self.plan)
        write_json(self.bundle.root / "instrument" / "device_readout.json", self.device.readout())
        write_json(self.bundle.root / "citrus" / "status_start.json", self.citrus.status())
        write_json(self.bundle.root / "citrus" / "active_projection_snapshot.json",
                   self.citrus.active_projection_snapshot())
        self.bundle.log("run_started", run_id=self.run_id, plan_sha256=self.plan_sha256)

        outcome = "completed"
        error = None
        previous_handler = signal.signal(signal.SIGTERM, _raise_keyboard_interrupt)
        try:
            self._preflight()
            self._projector_off_background()
            every = self._setting("drift_reference", "every_n_patches", default=15)
            for position in self.plan["positions"]:
                self._place(position)
                integrations_used = set()
                self._drift_reference(position, integrations_used, "start")
                for index, patch in enumerate(self.plan["patches"], start=1):
                    self._measure_patch(patch, position, integrations_used)
                    if every and index % every == 0:
                        self._drift_reference(position, integrations_used, f"after_{index}")
                self._drift_reference(position, integrations_used, "end")
                self._paired_darks(position, integrations_used)
                self.scenes.end_position(position)
        except KeyboardInterrupt:
            outcome, error = "interrupted", "KeyboardInterrupt"
            raise
        except Exception as exc:
            outcome, error = "failed", f"{type(exc).__name__}: {exc}"
            raise
        finally:
            signal.signal(signal.SIGTERM, previous_handler)
            try:
                self.scenes.finish()
            except Exception as exc:
                error = (error or "") + f"; restore failed: {exc}"
                self.operator.say(f"RESTORE FAILED, restore Citrus by hand: {exc}")
            self._seal(outcome, error)
        return self.bundle.root

    def _seal(self, outcome, error):
        try:
            write_json(self.bundle.root / "citrus" / "status_end.json", self.citrus.status())
        except Exception as exc:
            self.bundle.log("status_end_failed", error=str(exc))
        self.bundle.log("run_finished", outcome=outcome, error=error)
        status_start = json.loads((self.bundle.root / "citrus" / "status_start.json").read_text())
        context = {
            "schema_id": CAPTURE_CONTEXT_SCHEMA_ID,
            "schema_version": 1,
            "schema_status": "proposed (Citrus importer not implemented yet)",
            "capture_set_id": self.run_id,
            "outcome": outcome,
            "error": error,
            "captured_utc": {"start": self.started_utc,
                             "end": datetime.now(timezone.utc).isoformat()},
            "plan": {"plan_id": self.plan["plan_id"], "sha256": self.plan_sha256},
            "rig_id": self.plan["rig_id"],
            "canvas_name": self.plan["canvas_name"],
            "target_plane": self.plan["target_plane"],
            "scene_backend": self.plan["scene_backend"],
            "citations": self.plan.get("citations", {}),
            "citrus_build": self.scene_build or _find_build(status_start),
            "producer": {"name": "SpecSail calibration_run.py", **git_identity(REPO_DIR)},
            "probe_observations": self.observations,
            "measurements": self.measurements,
            "corrections_applied": "none: raw counts; device_readout.json has wavelength, "
                                   "dark-pixel and nonlinearity data for later correction",
            "evidence_grade": "unverified_upstream_output",
            "measurand": "detector_counts_native_pixel",
        }
        context["files"] = self.bundle.file_manifest(exclude=("capture_context.json",))
        write_json(self.bundle.root / "capture_context.json", context)


def _find_build(status_response):
    """Citrus build identity from status (Citrus C5), or ``"unknown"`` before C5 lands."""
    status = status_response.get("status", {}) if isinstance(status_response, dict) else {}
    for key in ("citrus_build", "build"):
        if isinstance(status.get(key), dict):
            return status[key]
    return "unknown"


def _raise_keyboard_interrupt(signum, frame):
    raise KeyboardInterrupt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("plan", help="run plan JSON (citrus.projector_spectral_run_plan v1)")
    parser.add_argument("--socket", default=DEFAULT_SOCKET, help="Citrus local control socket")
    parser.add_argument("--out", default="runs", help="parent directory for capture bundles")
    parser.add_argument("--serial", default=None, help="spectrometer serial (default: first found)")
    parser.add_argument("--backend", default="cseabreeze", help="preferred seabreeze backend")
    args = parser.parse_args(argv)

    try:
        plan, plan_sha256 = load_plan(args.plan)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2

    from spectral_capture import SpectralDevice
    device = SpectralDevice.open_first(serial=args.serial or plan.get("spectrometer_serial"),
                                       backend=args.backend)
    citrus = CitrusControl(args.socket, run_id=plan["plan_id"])
    run = CalibrationRun(plan, plan_sha256, citrus, device, ConsoleOperator(), args.out)
    try:
        bundle = run.run()
        print(f"\nBundle sealed: {bundle}")
        return 0
    except (RunAborted, CitrusControlError) as exc:
        print(f"\nRun stopped: {exc}\nPartial bundle sealed: {run.bundle.root}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print(f"\nInterrupted. Citrus restored; partial bundle sealed: {run.bundle.root}",
              file=sys.stderr)
        return 130
    finally:
        device.close()


if __name__ == "__main__":
    sys.exit(main())
