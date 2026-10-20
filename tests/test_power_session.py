"""
Tests for power_meter.py and power_session.py against a fake PM100USB and a fake Citrus.

Run:  pixi run test
"""

import copy
import csv
import json
import math
import tempfile
import unittest
from pathlib import Path

from calibration_run import RunAborted
from citrus_control import CitrusControl
from power_meter import PM100USB, PowerMeterError, parse_idn, parse_sensor_info
from power_session import (BLACK, ManualMeter, PowerSession, aperture_area_cm2, load_plan,
                           validate_plan)
from tests.fakes import FakeCitrus, ScriptedOperator


EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "power_meter_plan_omnifin0.json"


class FakeTransport:
    """
    Answers PM100USB SCPI like the real console. Power follows what the fake
    Citrus shows: ``full_W`` × mean commanded drive + ``dark_W``.
    """

    def __init__(self, citrus, full_W=2e-4, dark_W=1e-8, meter_serial="M01326168",
                 sensor_serial="210901234", interrupt_after=None):
        self.citrus = citrus
        self.full_W = full_W
        self.dark_W = dark_W
        self.meter_serial = meter_serial
        self.sensor_serial = sensor_serial
        self.interrupt_after = interrupt_after
        self.commands = []
        self.wavelength = 633.0
        self.average = 1
        self.reads = 0
        self.closed = False

    def write(self, command):
        self.commands.append(command)
        if command.startswith("SENS:CORR:WAV "):
            self.wavelength = float(command.split()[1])
        elif command.startswith("SENS:AVER:COUN "):
            self.average = int(command.split()[1])

    def query(self, command):
        self.commands.append(command)
        if command == "*IDN?":
            return f"Thorlabs,PM100USB,{self.meter_serial},1.7.0\n"
        if command == "SYST:SENS:IDN?":
            return f'"S130VC","{self.sensor_serial}","01-Mar-2024",1,18,289\n'
        if command == "SENS:CORR:WAV?":
            return f"{self.wavelength:.6E}\n"
        if command == "SENS:AVER:COUN?":
            return f"{self.average}\n"
        if command == "MEAS:POW?":
            self.reads += 1
            if self.interrupt_after and self.reads > self.interrupt_after:
                raise KeyboardInterrupt
            return f"{self.full_W * self.citrus.brightness() + self.dark_W:.9E}\n"
        raise AssertionError(f"unexpected query {command}")

    def close(self):
        self.closed = True


class NumberOperator(ScriptedOperator):
    """Answers reading prompts with a fixed number of µW; everything else with the default."""

    def __init__(self, microwatts="12.5"):
        super().__init__()
        self.microwatts = microwatts

    def ask(self, prompt, default=""):
        self.lines.append(prompt)
        return self.microwatts if prompt.startswith("Reading") else default


def small_plan():
    plan = json.loads(EXAMPLE.read_text())
    plan["positions"] = plan["positions"][:2]
    plan["fills"] = [f for f in plan["fills"] if f["fill_id"] in ("white_255", "red_255", "black")]
    plan["readings_per_fill"] = 3
    plan["settle_s"] = 0
    plan["sensor"]["serial_expected"] = "210901234"
    return plan


class ParsingTest(unittest.TestCase):

    def test_idn(self):
        info = parse_idn("Thorlabs,PM100USB,M01326168,1.7.0")
        self.assertEqual((info["manufacturer"], info["model"], info["serial"], info["firmware"]),
                         ("Thorlabs", "PM100USB", "M01326168", "1.7.0"))

    def test_sensor_info(self):
        info = parse_sensor_info('"S130VC","210901234","01-Mar-2024",1,18,289')
        self.assertEqual(info["name"], "S130VC")
        self.assertEqual(info["serial"], "210901234")
        self.assertEqual(info["calibration"], "01-Mar-2024")
        self.assertEqual((info["type"], info["subtype"], info["flags"]), ("1", "18", "289"))

    def test_sensor_info_short_answer(self):
        info = parse_sensor_info("S130VC,123")
        self.assertEqual(info["serial"], "123")
        self.assertIsNone(info["calibration"])

    def test_power_and_settings(self):
        citrus = FakeCitrus()
        self.addCleanup(citrus.close)
        meter = PM100USB(transport=FakeTransport(citrus))
        meter.configure(100)
        meter.set_wavelength(629)
        self.assertEqual(meter.get_wavelength(), 629.0)
        self.assertEqual(meter.get_average(), 100)
        self.assertAlmostEqual(meter.read_power(), 1e-8)
        self.assertIn("SENS:POW:UNIT W", meter.transport.commands)
        self.assertIn("SENS:POW:RANG:AUTO ON", meter.transport.commands)
        self.assertNotIn("SENS:CORR:COLL:ZERO:INIT", meter.transport.commands)
        self.assertEqual(len(meter.read_n(4)), 4)

    def test_bad_number(self):
        class Bad:
            def query(self, command):
                return "garbage"

            def write(self, command):
                pass

            def close(self):
                pass
        with self.assertRaises(PowerMeterError):
            PM100USB(transport=Bad()).read_power()


class PlanTest(unittest.TestCase):

    def test_example_plan_is_valid(self):
        plan, digest = load_plan(EXAMPLE)
        self.assertEqual(len(digest), 64)
        self.assertEqual(plan["meter"]["serial_expected"], "M01326168")

    def test_requires_black_and_aperture(self):
        plan = small_plan()
        plan["fills"] = [f for f in plan["fills"] if f["rgba8"] != BLACK]
        del plan["sensor"]["aperture_diameter_mm"]
        with self.assertRaises(ValueError) as ctx:
            validate_plan(plan)
        self.assertIn("black", str(ctx.exception))
        self.assertIn("aperture_diameter_mm", str(ctx.exception))

    def test_wavelength_range(self):
        plan = small_plan()
        plan["fills"][0]["wavelength_nm"] = 1500
        with self.assertRaises(ValueError):
            validate_plan(plan)


class SessionTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.citrus = FakeCitrus()
        self.addCleanup(self.citrus.close)

    def session(self, plan, transport=None, operator=None, manual=False, **kwargs):
        operator = operator or ScriptedOperator()
        if manual:
            meter = ManualMeter(operator)
        else:
            meter = PM100USB(transport=transport or FakeTransport(self.citrus))
        client = CitrusControl(self.citrus.socket_path, run_id="powertest")
        return PowerSession(plan, "0" * 64, client, meter, operator, self.tmp.name,
                            manual=manual, **kwargs)

    def test_session_writes_outputs(self):
        plan = small_plan()
        root = self.session(plan).run()
        session = json.loads((root / "power_meter" / "session.json").read_text())
        self.assertEqual(session["outcome"], "completed")
        # 2 positions × (drift start + 3 fills + drift end)
        self.assertEqual(len(session["records"]), 10)
        self.assertEqual(session["instrument"]["meter_idn"]["serial"], "M01326168")
        self.assertEqual(session["instrument"]["sensor_info"]["name"], "S130VC")
        first = session["records"][0]
        self.assertEqual(first["role"], "drift_start")
        self.assertEqual(len(first["readings"]), 3)
        self.assertFalse(first["scene"]["status"]["canvas_fill"]["placement_overlay"])
        self.assertEqual(self.citrus.restores, 1)
        with open(root / "power_meter" / "summary.csv") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(len(rows), 10)
        self.assertTrue(all(r["irradiance_provisional"] == "True" for r in rows))

    def test_black_subtraction_and_irradiance(self):
        plan = small_plan()
        root = self.session(plan, FakeTransport(self.citrus, full_W=2e-4, dark_W=1e-8)).run()
        with open(root / "power_meter" / "summary.csv") as f:
            rows = {(r["position_id"], r["fill_id"]): r for r in csv.DictReader(f)}
        white = rows[("arena_1_center", "white_255")]
        black = rows[("arena_1_center", "black")]
        self.assertAlmostEqual(float(black["mean_minus_black_W"]), 0.0, places=15)
        self.assertAlmostEqual(float(white["mean_minus_black_W"]), 2e-4, places=12)
        area = aperture_area_cm2(9.5)
        self.assertAlmostEqual(area, math.pi * 0.475 ** 2)
        self.assertAlmostEqual(float(white["irradiance_uW_per_cm2"]), 2e-4 * 1e6 / area, places=6)
        red = rows[("arena_1_center", "red_255")]
        self.assertAlmostEqual(float(red["mean_minus_black_W"]), 2e-4 / 3, places=12)

    def test_confirmed_aperture_not_provisional(self):
        plan = small_plan()
        plan["sensor"]["aperture_provenance"] = "confirmed: Thorlabs S130VC spec sheet"
        root = self.session(plan).run()
        with open(root / "power_meter" / "summary.csv") as f:
            self.assertTrue(all(r["irradiance_provisional"] == "False" for r in csv.DictReader(f)))

    def test_wavelength_set_per_fill(self):
        plan = small_plan()
        transport = FakeTransport(self.citrus)
        self.session(plan, transport).run()
        self.assertIn("SENS:CORR:WAV 629", transport.commands)
        self.assertIn("SENS:CORR:WAV 550", transport.commands)

    def test_refuses_serial_mismatch(self):
        plan = small_plan()
        session = self.session(plan, FakeTransport(self.citrus, meter_serial="M99999999"))
        with self.assertRaises(RunAborted):
            session.run()
        saved = json.loads((session.bundle.root / "power_meter" / "session.json").read_text())
        self.assertEqual(saved["outcome"], "failed")
        self.assertIn("serial_mismatch", saved["instrument"])
        self.assertEqual(saved["records"], [])

    def test_serial_mismatch_allowed(self):
        plan = small_plan()
        root = self.session(plan, FakeTransport(self.citrus, sensor_serial="other"),
                            allow_serial_mismatch=True).run()
        saved = json.loads((root / "power_meter" / "session.json").read_text())
        self.assertEqual(saved["outcome"], "completed")
        self.assertIn("serial_mismatch", saved["instrument"])

    def test_unknown_sensor_serial_recorded_not_enforced(self):
        plan = small_plan()
        plan["sensor"]["serial_expected"] = "unknown"
        root = self.session(plan, FakeTransport(self.citrus, sensor_serial="whatever")).run()
        saved = json.loads((root / "power_meter" / "session.json").read_text())
        self.assertEqual(saved["outcome"], "completed")
        self.assertEqual(saved["instrument"]["sensor_info"]["serial"], "whatever")

    def test_manual_mode(self):
        plan = small_plan()
        root = self.session(plan, operator=NumberOperator("12.5"), manual=True).run()
        saved = json.loads((root / "power_meter" / "session.json").read_text())
        self.assertEqual(saved["outcome"], "completed")
        self.assertTrue(saved["instrument"]["manual"])
        self.assertAlmostEqual(saved["records"][0]["mean_W"], 12.5e-6)

    def test_interrupt_restores_citrus_and_seals(self):
        plan = small_plan()
        session = self.session(plan, FakeTransport(self.citrus, interrupt_after=4))
        with self.assertRaises(KeyboardInterrupt):
            session.run()
        self.assertEqual(self.citrus.restores, 1)
        saved = json.loads((session.bundle.root / "power_meter" / "session.json").read_text())
        self.assertEqual(saved["outcome"], "interrupted")
        self.assertEqual(len(saved["records"]), 1)

    def test_nd_filter_declined_aborts(self):
        plan = small_plan()
        operator = ScriptedOperator()
        operator.confirm = lambda prompt: (_ for _ in ()).throw(RunAborted("operator declined"))
        with self.assertRaises(RunAborted):
            self.session(plan, operator=operator).run()


if __name__ == "__main__":
    unittest.main()
