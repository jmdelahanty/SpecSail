"""
Test doubles: a fake Citrus control socket and a fake spectrometer.

The fake Citrus speaks the calibration-scene subset of the local control
protocol, including the ``canvas_fill`` recipe (Citrus C6) with the field
names agreed for the Citrus implementation. The fake spectrometer's signal
follows whatever the fake Citrus is showing.
"""

import json
import os
import socket
import tempfile
import threading

import numpy as np


class FakeCitrus:
    """
    Fake Citrus local control server on a temporary Unix socket.

    Parameters
    ----------
    arenas : list of str
        Arena ids loaded on the fake canvas.
    canvas_fill_supported : bool
        False mimics a Citrus build without C6 (no ``canvas_fill`` object).
    overlay_stuck : bool
        True mimics a bug where the overlay never turns off.
    present_after_polls : int
        Status polls before a scene reports ``presented``.
    """

    def __init__(self, arenas=("arena_1", "arena_2", "arena_3", "arena_4"),
                 canvas_fill_supported=True, overlay_stuck=False, present_after_polls=2):
        self.arenas = list(arenas)
        self.canvas_fill_supported = canvas_fill_supported
        self.overlay_stuck = overlay_stuck
        self.present_after_polls = present_after_polls
        self.lock = threading.Lock()
        self.requests = []
        self.active_transaction = None
        self.target_set = None
        self.scene = {"state": "idle", "active": False, "presented": False,
                      "scene_revision": 0, "last_command": None}
        self.polls_left = 0
        self.rgba_by_arena = {a: [0, 0, 0, 255] for a in self.arenas}
        self.restores = 0
        self._dir = tempfile.mkdtemp(prefix="fakecitrus")
        self.socket_path = os.path.join(self._dir, "control.sock")
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(self.socket_path)
        self._server.listen(8)
        self._stop = False
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def close(self):
        self._stop = True
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.connect(self.socket_path)
        except OSError:
            pass
        self._server.close()
        self._thread.join(timeout=2)

    def brightness(self, arena_id=None):
        """Mean commanded drive (0-1) of what is shown; the fake spectrometer uses it."""
        with self.lock:
            rgba = self.rgba_by_arena.get(arena_id or self.arenas[0], [0, 0, 0, 255])
            return sum(rgba[:3]) / (3 * 255.0)

    def _serve(self):
        while not self._stop:
            try:
                conn, _ = self._server.accept()
            except OSError:
                return
            with conn:
                data = b""
                while b"\n" not in data:
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    data += chunk
                if not data.strip():
                    continue
                req = json.loads(data.split(b"\n", 1)[0])
                with self.lock:
                    self.requests.append(req)
                    response = self._handle(req)
                conn.sendall((json.dumps(response) + "\n").encode())

    def _response(self, req, ok=True, accepted=True, error=None, effect=None, status=None):
        response = {"schema_id": "citrus.local_control.response", "ok": ok, "accepted": accepted,
                    "request_id": req.get("request_id"), "operation_id": req.get("operation_id"),
                    "method": req.get("method"), "effect": effect or {}, "status": status or {}}
        if error:
            response["error"] = error
        return response

    def _command(self, req, status, error=None):
        self.scene["last_command"] = {"status": status, "request_id": req["request_id"],
                                      "operation_id": req["operation_id"], "error": error}

    def _handle(self, req):
        method = req["method"]
        if method == "status":
            return self._response(req, status={"citrus_build": {"git_commit": "fake", "dirty": False}})
        if method == "active_projection_snapshot":
            return self._response(req, effect={"arenas": self.arenas})
        if method == "calibration_scene_status":
            if self.polls_left > 0:
                self.polls_left -= 1
                self.scene["presented"] = False
                self.scene["state"] = "applying" if self.scene["active"] else "restoring"
            elif self.scene["last_command"] and self.scene["last_command"]["status"] == "applied":
                self.scene["presented"] = True
                self.scene["state"] = "presented" if self.scene["active"] else "restored"
            return self._response(req, effect={"calibration_scene": json.loads(json.dumps(self.scene))})
        if method == "set_calibration_scene":
            return self._set(req)
        if method == "restore_calibration_scene":
            params = req["params"]
            if params["transaction_id"] != self.active_transaction:
                return self._response(req, accepted=False, error="calibration_scene_transaction_conflict")
            self.active_transaction = None
            self.target_set = None
            self.scene["active"] = False
            self.rgba_by_arena = {a: [0, 0, 0, 255] for a in self.arenas}
            self.restores += 1
            self._command(req, "applied")
            self.polls_left = self.present_after_polls
            return self._response(req)
        return self._response(req, ok=False, accepted=False, error="unsupported_method")

    def _set(self, req):
        params = req["params"]
        tx, recipe, arena_ids = params["transaction_id"], params["recipe_id"], params["arena_ids"]
        options = params.get("scene_options", {})
        if self.active_transaction and tx != self.active_transaction:
            return self._response(req, accepted=False, error="calibration_scene_transaction_conflict")
        if self.target_set and sorted(arena_ids) != self.target_set:
            self._command(req, "rejected", "calibration_scene_target_set_changed")
            return self._response(req)
        if recipe == "canvas_fill" and not self.canvas_fill_supported:
            return self._response(req, accepted=False, error="unsupported_calibration_scene_recipe")
        if recipe == "canvas_fill" and sorted(arena_ids) != sorted(self.arenas):
            return self._response(req, accepted=False, error="canvas_fill_requires_all_arenas")
        self.active_transaction = tx
        self.target_set = sorted(arena_ids)
        self.scene["active"] = True
        self.scene["recipe_id"] = recipe
        self.scene["scene_revision"] += 1
        self.scene.pop("canvas_fill", None)
        self.scene["resolved_targets"] = [{"arena_id": a} for a in arena_ids]
        if recipe == "canvas_fill":
            rgba = list(options["foreground_rgba_u8"])
            overlay = bool(options.get("placement_overlay")) or self.overlay_stuck
            self.scene["canvas_fill"] = {
                "foreground_rgba_u8": rgba,
                "placement_overlay": overlay,
                "probe_targets": [dict(t, canvas_px=[100.0, 100.0]) for t in options.get("probe_targets", [])],
                "active_target": options.get("active_target"),
                "illumination_domain": "full_canvas",
            }
            self.rgba_by_arena = {a: rgba for a in self.arenas}
        elif recipe == "uniform_gray":
            gray = options.get("foreground_gray_u8", 76)
            for target in self.scene["resolved_targets"]:
                target["uniform_illumination"] = {"gray_u8": gray}
            for a in arena_ids:
                self.rgba_by_arena[a] = [gray, gray, gray, 255]
        else:
            for a in arena_ids:
                self.rgba_by_arena[a] = [40, 40, 40, 255]
        self.scene["citrus_build"] = {"git_commit": "fake", "dirty": False, "software_version": "0"}
        self._command(req, "applied")
        self.polls_left = self.present_after_polls
        return self._response(req)


class _Feature:
    def __init__(self, **methods):
        for name, fn in methods.items():
            setattr(self, name, fn)


class FakeSpectrometer:
    """
    Stand-in for ``seabreeze.spectrometers.Spectrometer`` (QE Pro-like).

    Counts = dark offset + brightness x a smooth spectrum x integration time,
    clipped at the 200000-count maximum.
    """

    model = "QE-PRO"
    serial_number = "FAKE0001"
    pixels = 1044
    max_intensity = 200000.0
    integration_time_micros_limits = (8000, 1600000000)

    def __init__(self, citrus, arena_id=None, counts_per_ms_at_full=400.0):
        self.citrus = citrus
        self.arena_id = arena_id
        self.counts_per_ms_at_full = counts_per_ms_at_full
        self._dp = [0, 1, 2, 3, 1040, 1041, 1042, 1043]
        self._nc = [1.0, 1e-7, 0.0]
        self.integration_us = 100000
        self.closed = False
        wl = np.linspace(346.9, 927.3, self.pixels)
        self._shape = np.exp(-0.5 * ((wl - 550.0) / 60.0) ** 2)
        self._shape[self._dp] = 0.0
        self._wl = wl
        self.features = {
            "thermo_electric": [_Feature(read_temperature_degrees_celsius=lambda: -9.8)],
            "nonlinearity_coefficients": [_Feature(get_nonlinearity_coefficients=lambda: self._nc)],
        }

    def wavelengths(self):
        return self._wl

    def integration_time_micros(self, us):
        self.integration_us = us

    def intensities(self, correct_dark_counts=False, correct_nonlinearity=False):
        brightness = self.citrus.brightness(self.arena_id)
        signal = self._shape * brightness * self.counts_per_ms_at_full * self.integration_us / 1000.0
        return np.minimum(1000.0 + signal, self.max_intensity)

    def close(self):
        self.closed = True


class ScriptedOperator:
    """Answers every prompt; optionally raises KeyboardInterrupt on a given confirm."""

    def __init__(self, interrupt_on=None):
        self.interrupt_on = interrupt_on
        self.lines = []

    def ask(self, prompt, default=""):
        return default

    def confirm(self, prompt):
        self.lines.append(prompt)
        if self.interrupt_on and self.interrupt_on in prompt:
            raise KeyboardInterrupt

    def say(self, message):
        self.lines.append(message)
