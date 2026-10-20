#!/usr/bin/env python3
"""
Power meter session: absolute power at each arena centre under Citrus fills.

Citrus fills the whole canvas with one colour at a time (the ``canvas_fill``
calibration scene) while a Thorlabs PM100USB with a photodiode head, lying
flat on the projection film at an arena centre, reads the power. Each
position gets a white drift reading at the start and end and a black reading
for the offset (sensor dark current plus projector black and room light).

The meter's reading assumes all light is at its correction wavelength. That is
right for one LED primary at its peak wavelength; for white and grays the
reading is a single-wavelength placeholder that is corrected later with the
SpecSail spectrum of the same fill and the sensor's responsivity curve.

Citrus is always restored, including after errors and Ctrl+C.

Usage
-----
    pixi run powerrun examples/power_meter_plan_omnifin0.json --socket /tmp/citrus_local_control.sock
    pixi run powerrun PLAN --manual        # type readings from another display instead of USB
"""

import argparse
import csv
import hashlib
import json
import math
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from calibration_run import (Bundle, CanvasFillScenes, ConsoleOperator, RunAborted,
                             _raise_keyboard_interrupt, git_identity, write_json)
from citrus_control import CitrusControl, CitrusControlError


PLAN_SCHEMA_ID = "specsail.power_meter_plan"
SESSION_SCHEMA_ID = "specsail.power_meter_session"
DEFAULT_SOCKET = "/tmp/citrus_local_control.sock"
BLACK = [0, 0, 0, 255]
REPO_DIR = Path(__file__).resolve().parent


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------

def _rgba_ok(value):
    return (isinstance(value, list) and len(value) == 4 and value[3] == 255 and
            all(isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 255 for v in value))


def validate_plan(plan):
    """Raise ``ValueError`` listing every problem with a power meter plan."""
    problems = []

    def need(key, kind):
        if not isinstance(plan.get(key), kind):
            problems.append(f"{key}: required ({kind.__name__})")

    if plan.get("schema_id") != PLAN_SCHEMA_ID or plan.get("schema_version") != 1:
        problems.append(f"schema: expected {PLAN_SCHEMA_ID} v1")
    for key, kind in (("plan_id", str), ("rig_id", str), ("canvas_name", str), ("arena_ids", list),
                      ("positions", list), ("fills", list), ("readings_per_fill", int),
                      ("average_count", int), ("sensor", dict), ("meter", dict), ("drift_fill", dict)):
        need(key, kind)
    if not isinstance(plan.get("settle_s"), (int, float)) or isinstance(plan.get("settle_s"), bool):
        problems.append("settle_s: required (number)")

    arenas = plan.get("arena_ids") if isinstance(plan.get("arena_ids"), list) else []
    seen = set()
    for i, p in enumerate(plan.get("positions") or []):
        if not isinstance(p, dict) or not isinstance(p.get("position_id"), str):
            problems.append(f"positions[{i}]: needs position_id")
            continue
        if p["position_id"] in seen:
            problems.append(f"positions[{i}]: duplicate position_id {p['position_id']}")
        seen.add(p["position_id"])
        if p.get("arena_id") not in arenas:
            problems.append(f"positions[{i}]: arena_id not in arena_ids")

    fill_ids = set()
    for i, f in enumerate(list(plan.get("fills") or []) + [plan.get("drift_fill") or {}]):
        label = f"fills[{i}]" if i < len(plan.get("fills") or []) else "drift_fill"
        if not isinstance(f, dict) or not isinstance(f.get("fill_id"), str):
            problems.append(f"{label}: needs fill_id")
            continue
        if f["fill_id"] in fill_ids:
            problems.append(f"{label}: duplicate fill_id {f['fill_id']}")
        fill_ids.add(f["fill_id"])
        if not _rgba_ok(f.get("rgba8")):
            problems.append(f"{label}: rgba8 must be four integers 0-255 with alpha 255")
        wl = f.get("wavelength_nm")
        if not isinstance(wl, (int, float)) or isinstance(wl, bool) or not 200 <= wl <= 1100:
            problems.append(f"{label}: wavelength_nm must be within the sensor's 200-1100 nm")
    if not any(isinstance(f, dict) and f.get("rgba8") == BLACK for f in plan.get("fills") or []):
        problems.append("fills: must include black [0,0,0,255] (the offset reading)")

    sensor = plan.get("sensor") if isinstance(plan.get("sensor"), dict) else {}
    for key in ("model", "serial_expected", "aperture_provenance"):
        if not isinstance(sensor.get(key), str) or not sensor.get(key):
            problems.append(f"sensor.{key}: required (string)")
    d = sensor.get("aperture_diameter_mm")
    if not isinstance(d, (int, float)) or isinstance(d, bool) or d <= 0:
        problems.append("sensor.aperture_diameter_mm: required (positive number, no default)")
    if sensor.get("nd_filter") not in ("open", "in"):
        problems.append('sensor.nd_filter: "open" or "in"')
    meter = plan.get("meter") if isinstance(plan.get("meter"), dict) else {}
    for key in ("model", "serial_expected"):
        if not isinstance(meter.get(key), str) or not meter.get(key):
            problems.append(f"meter.{key}: required (string)")
    if isinstance(plan.get("readings_per_fill"), int) and plan["readings_per_fill"] < 1:
        problems.append("readings_per_fill: at least 1")
    if problems:
        raise ValueError("invalid power meter plan:\n  " + "\n  ".join(problems))


def load_plan(path):
    """Read and validate a plan. Returns (plan, sha256 of the file)."""
    raw = Path(path).read_bytes()
    plan = json.loads(raw)
    validate_plan(plan)
    return plan, hashlib.sha256(raw).hexdigest()


def aperture_area_cm2(diameter_mm):
    return math.pi * (diameter_mm / 20.0) ** 2


def aperture_confirmed(sensor):
    return sensor.get("aperture_provenance", "").lower().startswith("confirmed")


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------

class ManualMeter:
    """Readings typed by the operator (µW), e.g. from the Thorlabs software on another computer."""

    def __init__(self, operator):
        self.operator = operator
        self.wavelength_nm = None

    def idn(self):
        return {"manufacturer": None, "model": None, "serial": None, "firmware": None, "raw": "manual"}

    def sensor_info(self):
        return {"name": None, "serial": None, "calibration": None, "type": None,
                "subtype": None, "flags": None, "raw": "manual"}

    def configure(self, average_count):
        self.operator.say(f"Manual mode: set the meter to W, auto range, averaging {average_count}.")

    def set_wavelength(self, nm):
        self.wavelength_nm = nm

    def get_wavelength(self):
        return self.wavelength_nm

    def read_n(self, n):
        values = []
        for i in range(n):
            while True:
                text = self.operator.ask(
                    f"Reading {i + 1}/{n} in µW (meter at {self.wavelength_nm:g} nm)")
                try:
                    values.append({"power_W": float(text) * 1e-6, "host_time_s": time.time()})
                    break
                except ValueError:
                    self.operator.say(f"not a number: {text!r}")
        return values

    def close(self):
        pass


def _stats(values):
    n = len(values)
    mean = sum(values) / n
    std = math.sqrt(sum((v - mean) ** 2 for v in values) / (n - 1)) if n > 1 else 0.0
    return {"n": n, "mean_W": mean, "std_W": std, "min_W": min(values), "max_W": max(values)}


class PowerSession:
    """
    One session: positions × fills, read with a PM100USB (or typed by hand).

    Parameters
    ----------
    plan : dict
        A validated ``specsail.power_meter_plan`` v1.
    plan_sha256 : str
    citrus : CitrusControl or compatible
    meter : PM100USB, ManualMeter or compatible
    operator : ConsoleOperator or compatible
    out_dir : str or Path
        Parent directory; the session writes ``<out_dir>/<plan_id>_<UTC>/``.
    manual : bool
    allow_serial_mismatch : bool
    """

    def __init__(self, plan, plan_sha256, citrus, meter, operator, out_dir,
                 manual=False, allow_serial_mismatch=False, run_id=None):
        self.plan = plan
        self.plan_sha256 = plan_sha256
        self.citrus = citrus
        self.meter = meter
        self.operator = operator
        self.manual = manual
        self.allow_serial_mismatch = allow_serial_mismatch
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.run_id = run_id or f"{plan['plan_id']}_{stamp}"
        self.bundle = Bundle(Path(out_dir) / self.run_id)
        self.out = self.bundle.root / "power_meter"
        # CanvasFillScenes needs positions with offsets and the arena list.
        scene_plan = dict(plan, positions=[dict(p, offset_mm=p.get("offset_mm", [0.0, 0.0]))
                                           for p in plan["positions"]])
        self.scenes = CanvasFillScenes(citrus, scene_plan, self.run_id)
        self.records = []
        self.instrument = {}
        self.operator_answers = {}

    # -- steps --------------------------------------------------------------

    def _preflight(self):
        idn = self.meter.idn()
        sensor = self.meter.sensor_info()
        self.instrument = {"meter_idn": idn, "sensor_info": sensor, "device": getattr(self.meter, "device", None),
                           "manual": self.manual}
        if not self.manual:
            mismatches = []
            if idn.get("serial") != self.plan["meter"]["serial_expected"]:
                mismatches.append(f"meter serial {idn.get('serial')!r}, expected "
                                  f"{self.plan['meter']['serial_expected']!r}")
            expected_sensor = self.plan["sensor"]["serial_expected"]
            if expected_sensor.lower().startswith("unknown"):
                self.operator.say(f"Sensor serial not in the plan; the head reports {sensor.get('serial')!r}. "
                                  "Record it in the plan for next time.")
            elif sensor.get("serial") != expected_sensor:
                mismatches.append(f"sensor serial {sensor.get('serial')!r}, expected "
                                  f"{self.plan['sensor']['serial_expected']!r}")
            if mismatches:
                self.instrument["serial_mismatch"] = mismatches
                if not self.allow_serial_mismatch:
                    raise RunAborted("; ".join(mismatches) + " (use --allow-serial-mismatch to override)")
                self.operator.say("WARNING: " + "; ".join(mismatches))
        self.meter.configure(self.plan["average_count"])
        self.bundle.log("preflight_ok", meter_idn=idn.get("raw"), sensor_info=sensor.get("raw"))

    def _operator_checks(self):
        nd = self.plan["sensor"]["nd_filter"]
        self.operator.confirm(f"Is the sensor's sliding ND filter {nd.upper()} (as the plan says)?")
        self.operator_answers = {
            "nd_filter_confirmed": nd,
            "room_lights": self.operator.ask("Room lights (off/on)", "off"),
            "ir_arrays": self.operator.ask("IR light arrays (off/on)", "off"),
            "shelf": self.operator.ask("What is on the shelf besides the sensor", "nothing"),
        }
        self.bundle.log("operator_checks", **self.operator_answers)

    def _place(self, position):
        self.scenes.placement(position)
        note = self.operator.ask(
            f"Lay the sensor flat, face down onto the light, centred on the crosshair of "
            f"{position['position_id']}; keep windows off the projector. Press Enter when placed")
        self.bundle.log("sensor_placed", position_id=position["position_id"], note=note)

    def _measure(self, position, fill, role):
        scene = self.scenes.show(fill["rgba8"], position)
        self.meter.set_wavelength(fill["wavelength_nm"])
        time.sleep(self.plan["settle_s"])
        readings = self.meter.read_n(self.plan["readings_per_fill"])
        stats = _stats([r["power_W"] for r in readings])
        record = {
            "position_id": position["position_id"], "arena_id": position["arena_id"],
            "fill_id": fill["fill_id"], "role": role, "rgba8": fill["rgba8"],
            "wavelength_nm": fill["wavelength_nm"], "wavelength_readback_nm": self.meter.get_wavelength(),
            "note": fill.get("note"), "utc": datetime.now(timezone.utc).isoformat(),
            "readings": readings, **stats, "scene": scene,
        }
        self.records.append(record)
        self.bundle.log("fill_measured", position_id=position["position_id"], fill_id=fill["fill_id"],
                        role=role, mean_W=stats["mean_W"], std_W=stats["std_W"])
        self.operator.say(f"{position['position_id']} {fill['fill_id']}: "
                          f"{stats['mean_W'] * 1e6:.4g} µW (sd {stats['std_W'] * 1e6:.2g})")
        return record

    def run(self):
        """Run the plan. Returns the session directory."""
        self.started_utc = datetime.now(timezone.utc).isoformat()
        self.bundle.log("session_started", run_id=self.run_id, plan_sha256=self.plan_sha256)
        outcome, error = "completed", None
        previous = signal.signal(signal.SIGTERM, _raise_keyboard_interrupt)
        try:
            self._preflight()
            self._operator_checks()
            drift = self.plan["drift_fill"]
            for position in self.plan["positions"]:
                self._place(position)
                self._measure(position, dict(drift, fill_id=f"{drift['fill_id']}_start"), "drift_start")
                for fill in self.plan["fills"]:
                    self._measure(position, fill, "black" if fill["rgba8"] == BLACK else "fill")
                self._measure(position, dict(drift, fill_id=f"{drift['fill_id']}_end"), "drift_end")
        except KeyboardInterrupt:
            outcome, error = "interrupted", "KeyboardInterrupt"
            raise
        except Exception as exc:
            outcome, error = "failed", f"{type(exc).__name__}: {exc}"
            raise
        finally:
            signal.signal(signal.SIGTERM, previous)
            try:
                self.scenes.finish()
            except Exception as exc:
                error = (error or "") + f"; restore failed: {exc}"
                self.operator.say(f"RESTORE FAILED, restore Citrus by hand: {exc}")
            self._seal(outcome, error)
        return self.bundle.root

    # -- output -------------------------------------------------------------

    def summary_rows(self):
        sensor = self.plan["sensor"]
        area = aperture_area_cm2(sensor["aperture_diameter_mm"])
        provisional = not aperture_confirmed(sensor)
        black = {r["position_id"]: r["mean_W"] for r in self.records if r["role"] == "black"}
        rows = []
        for r in self.records:
            b = black.get(r["position_id"])
            net = r["mean_W"] - b if b is not None else None
            rows.append({
                "position_id": r["position_id"], "fill_id": r["fill_id"], "role": r["role"],
                "wavelength_nm": r["wavelength_nm"], "mean_W": r["mean_W"], "std_W": r["std_W"],
                "n": r["n"], "mean_minus_black_W": net,
                "irradiance_uW_per_cm2": net * 1e6 / area if net is not None else None,
                "irradiance_provisional": provisional,
            })
        return rows

    def _seal(self, outcome, error):
        self.out.mkdir(parents=True, exist_ok=True)
        sensor = self.plan["sensor"]
        session = {
            "schema_id": SESSION_SCHEMA_ID, "schema_version": 1, "run_id": self.run_id,
            "outcome": outcome, "error": error,
            "started_utc": getattr(self, "started_utc", None),
            "finished_utc": datetime.now(timezone.utc).isoformat(),
            "plan": self.plan, "plan_sha256": self.plan_sha256,
            "instrument": self.instrument, "operator": self.operator_answers,
            "aperture": {"diameter_mm": sensor["aperture_diameter_mm"],
                         "area_cm2": aperture_area_cm2(sensor["aperture_diameter_mm"]),
                         "provenance": sensor["aperture_provenance"],
                         "irradiance_provisional": not aperture_confirmed(sensor)},
            "correction_note": ("readings assume all light at wavelength_nm; white and gray fills are "
                                "single-wavelength placeholders to correct with SpecSail spectra"),
            "transaction_id": self.scenes.transaction_id,
            "specsail": git_identity(REPO_DIR),
            "records": self.records,
        }
        write_json(self.out / "session.json", session)
        rows = self.summary_rows()
        fields = ["position_id", "fill_id", "role", "wavelength_nm", "mean_W", "std_W", "n",
                  "mean_minus_black_W", "irradiance_uW_per_cm2", "irradiance_provisional"]
        with open(self.out / "summary.csv", "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            for row in rows:
                writer.writerow(row)
        self.bundle.log("session_sealed", outcome=outcome, error=error)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("plan", help="power meter plan JSON (specsail.power_meter_plan v1)")
    parser.add_argument("--socket", default=DEFAULT_SOCKET, help="Citrus local control socket")
    parser.add_argument("--out", default="runs", help="parent directory for session bundles")
    parser.add_argument("--meter", default=None, help="/dev/usbtmcN (default: find the PM100USB)")
    parser.add_argument("--manual", action="store_true", help="type readings in µW instead of USB")
    parser.add_argument("--allow-serial-mismatch", action="store_true",
                        help="continue when the meter or sensor serial differs from the plan")
    args = parser.parse_args(argv)

    try:
        plan, plan_sha256 = load_plan(args.plan)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2

    operator = ConsoleOperator()
    if args.manual:
        meter = ManualMeter(operator)
    else:
        from power_meter import PM100USB
        meter = PM100USB(device=args.meter)
    citrus = CitrusControl(args.socket, run_id=plan["plan_id"])
    session = PowerSession(plan, plan_sha256, citrus, meter, operator, args.out,
                           manual=args.manual, allow_serial_mismatch=args.allow_serial_mismatch)
    try:
        root = session.run()
        print(f"\nSession sealed: {root}\nSummary: {root / 'power_meter' / 'summary.csv'}")
        return 0
    except (RunAborted, CitrusControlError) as exc:
        print(f"\nSession stopped: {exc}\nPartial session sealed: {session.bundle.root}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print(f"\nInterrupted. Citrus restored; partial session sealed: {session.bundle.root}",
              file=sys.stderr)
        return 130
    finally:
        meter.close()


if __name__ == "__main__":
    sys.exit(main())
