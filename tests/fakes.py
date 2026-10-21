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

from citrus_control import CitrusControl


BUILD = {"software_version": "0", "git_commit_hash": "fake", "git_describe": "fake",
         "git_dirty": False, "build_utc": "2026-10-19T00:00:00Z"}


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
                 canvas_fill_supported=True, overlay_stuck=False, present_after_polls=2,
                 controller_reject=None, arena_fills_supported=True, stray_fraction=0.0,
                 serve_socket=True):
        self.arenas = list(arenas)
        self.canvas_fill_supported = canvas_fill_supported
        self.overlay_stuck = overlay_stuck
        self.present_after_polls = present_after_polls
        self.controller_reject = controller_reject
        self.arena_fills_supported = arena_fills_supported
        self.stray_fraction = stray_fraction
        self.lock = threading.Lock()
        self.requests = []
        self.active_transaction = None
        self.target_set = None
        self.scene = {"state": "idle", "active": False, "presented": False,
                      "scene_revision": 0, "last_command": None}
        self.polls_left = 0
        self.rgba_by_arena = {a: [0, 0, 0, 255] for a in self.arenas}
        self.restores = 0
        self._server = None
        if not serve_socket:
            return
        self._dir = tempfile.mkdtemp(prefix="fakecitrus")
        self.socket_path = os.path.join(self._dir, "control.sock")
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(self.socket_path)
        self._server.listen(8)
        self._stop = False
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def close(self):
        if self._server is None:
            return
        self._stop = True
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.connect(self.socket_path)
        except OSError:
            pass
        self._server.close()
        self._thread.join(timeout=2)

    def brightness(self, arena_id=None):
        """
        Mean commanded drive (0-1) of what is shown at ``arena_id``; the fake
        spectrometer uses it. ``stray_fraction`` of every other arena's drive is
        added, a crude model of light reaching one arena from the others.
        """
        with self.lock:
            target = arena_id or self.arenas[0]
            level = lambda a: sum(self.rgba_by_arena.get(a, [0, 0, 0, 255])[:3]) / (3 * 255.0)
            own = level(target)
            stray = sum(level(a) for a in self.arenas if a != target)
            return own + self.stray_fraction * stray

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
            return self._response(req, status={"citrus_build": dict(BUILD),
                                               "arenas": [{"arena_id": a} for a in self.arenas]})
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
        if recipe == "arena_fills" and not self.arena_fills_supported:
            return self._response(req, accepted=False, error="unsupported_calibration_scene_recipe")
        if recipe == "arena_fills":
            error = self._arena_fills_request_error(arena_ids, options)
            if error:
                return self._response(req, accepted=False, error=error)
        if recipe == "canvas_fill" and not self.canvas_fill_supported:
            return self._response(req, accepted=False, error="unsupported_calibration_scene_recipe")
        if recipe == "canvas_fill":
            error = self._canvas_fill_request_error(arena_ids, options)
            if error:
                return self._response(req, accepted=False, error=error)
            if self.controller_reject:
                # accepted by the socket, then refused by the controller (Citrus C6)
                self._command(req, "rejected", self.controller_reject)
                return self._response(req)
        self.active_transaction = tx
        self.target_set = sorted(arena_ids)
        self.scene["active"] = True
        self.scene["recipe_id"] = recipe
        self.scene["scene_revision"] += 1
        self.scene.pop("canvas_fill", None)
        self.scene.pop("arena_fills", None)
        self.scene["resolved_targets"] = [{"arena_id": a} for a in arena_ids]
        if recipe == "canvas_fill":
            rgba = list(options["foreground_rgba_u8"])
            overlay = bool(options.get("placement_overlay")) or self.overlay_stuck
            active = options.get("active_target")
            self.scene["canvas_fill"] = {
                "foreground_rgba_u8": rgba,
                "placement_overlay": overlay,
                "overlay_line_rgba_u8": [0, 0, 0, 255] if overlay else None,
                "target_offset_axes": "final_display_canvas_x_right_y_down_mm",
                "framebuffer_size_px": [1920, 1080],
                "probe_targets": [dict(t, canvas_px=[100.0, 100.0], active=t["target_id"] == active)
                                  for t in options.get("probe_targets", [])],
                "active_target": active,
                "illumination_domain": "full_canvas",
                "arenas": [{"arena_id": a, "experimental_area_center_canvas_px": [480.0, 270.0],
                            "pixels_per_mm_projector": 4.0} for a in self.arenas],
                "photodiode_marker": {"configured": None, "drawn": False,
                                      "reason": "drawn_only_while_a_protocol_runs"},
            }
            self.rgba_by_arena = {a: rgba for a in self.arenas}
        elif recipe == "arena_fills":
            fills = options.get("arena_fill_rgba_u8", {})
            background = list(options.get("background_rgba_u8", [0, 0, 0, 255]))
            overlay = bool(options.get("placement_overlay")) or self.overlay_stuck
            domain = options.get("domain", "full_arena_unmasked")
            arenas = []
            for a in self.arenas:
                rgba = list(fills.get(a, [0, 0, 0, 255]))
                lit = any(v > 0 for v in rgba[:3])
                arenas.append({
                    "arena_id": a, "rgba_u8": rgba, "lit": lit,
                    "arena_rect_canvas_px": [self.arenas.index(a) * 480, 0, 480, 1080],
                    "region": {"shape": "arena_rectangle", "x_px": self.arenas.index(a) * 480,
                               "y_px": 0, "width_px": 480, "height_px": 1080},
                    "filled_pixel_count": 480 * 1080,
                    "semantic_role": "physical_target_transmitted_light" if lit else "physical_target_held_dark",
                    "coordinate_target": False,
                    "illumination_domain": "configured_arena_rectangle" if domain == "full_arena_unmasked"
                    else "experimental_area",
                    "mask_policy": domain,
                    "photometry": {"command_encoding": "framebuffer_rgb_u8", "alpha_semantics": "opaque"},
                })
                self.rgba_by_arena[a] = rgba
            self.scene["arena_fills"] = {
                "domain": domain, "background_rgba_u8": background,
                "framebuffer_size_px": [1920, 1080],
                "placement_overlay": overlay,
                "overlay_line_rule": "per_pixel_black_if_underlying_rec709_luma_ge_128_else_white" if overlay else None,
                "probe_targets": options.get("probe_targets", []),
                "active_target": options.get("active_target"),
                "arenas": arenas,
            }
        elif recipe == "uniform_gray":
            gray = options.get("foreground_gray_u8", 76)
            for target in self.scene["resolved_targets"]:
                target["uniform_illumination"] = {"gray_u8": gray}
            for a in arena_ids:
                self.rgba_by_arena[a] = [gray, gray, gray, 255]
        else:
            for a in arena_ids:
                self.rgba_by_arena[a] = [40, 40, 40, 255]
        self.scene["citrus_build"] = dict(BUILD)
        self.scene["swap_end_semantics"] = "swap_call_returned_submitted_not_scanout"
        self.scene["visible_from_display_frame_swap_end_epoch_ns"] = 1_760_000_000_000_000_000
        self.scene["visible_from_display_frame_swap_end_utc"] = "2025-10-09T08:53:20.000000Z"
        self._command(req, "applied")
        self.polls_left = self.present_after_polls
        return self._response(req)


    def _arena_fills_request_error(self, arena_ids, options):
        """
        Request-level refusals in the order Citrus arena_fills (C7) checks them:
        option parsing first (unknown key, fill map type, colours in sorted arena
        order, background, domain), then the arena set, then unknown arenas.
        """
        allowed = {"arena_fill_rgba_u8", "background_rgba_u8", "domain", "placement_overlay",
                   "probe_targets", "active_target"}
        for key in options:
            if key not in allowed:
                return f"scene_options_unknown_key:{key}"
        fills = options.get("arena_fill_rgba_u8", {})
        if not isinstance(fills, dict):
            return "arena_fills_arena_fill_rgba_u8_must_be_object"
        channel = lambda v: isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 255
        valid = lambda c: isinstance(c, list) and len(c) == 4 and all(channel(v) for v in c) and c[3] == 255
        for arena_id in sorted(fills):
            if not valid(fills[arena_id]):
                return f"arena_fills_rgba_invalid:{arena_id}"
        if "background_rgba_u8" in options and not valid(options["background_rgba_u8"]):
            return "arena_fills_background_invalid"
        if options.get("domain", "full_arena_unmasked") not in ("full_arena_unmasked", "experimental_area_masked"):
            return "arena_fills_domain_invalid"
        if "placement_overlay" in options and not isinstance(options["placement_overlay"], bool):
            return "scene_options_placement_overlay_must_be_bool"
        if sorted(arena_ids) != sorted(self.arenas) or len(set(arena_ids)) != len(arena_ids):
            return "arena_fills_requires_all_arenas"
        for arena_id in sorted(fills):
            if arena_id not in arena_ids:
                return f"arena_fills_unknown_arena:{arena_id}"
        return None

    def _canvas_fill_request_error(self, arena_ids, options):
        """Request-level refusals, as the Citrus C6 socket validation returns them."""
        if sorted(arena_ids) != sorted(self.arenas) or len(set(arena_ids)) != len(arena_ids):
            return "canvas_fill_requires_all_arenas"
        allowed = {"foreground_rgba_u8", "placement_overlay", "probe_targets", "active_target"}
        for key in options:
            if key not in allowed:
                return f"scene_options_unknown_key:{key}"
        rgba = options.get("foreground_rgba_u8")
        if not (isinstance(rgba, list) and len(rgba) == 4 and
                all(isinstance(v, int) and 0 <= v <= 255 for v in rgba) and rgba[3] == 255):
            return "scene_options_foreground_rgba_u8_invalid"
        if "placement_overlay" in options and not isinstance(options["placement_overlay"], bool):
            return "scene_options_placement_overlay_must_be_bool"
        targets = options.get("probe_targets", [])
        ids = [t.get("target_id") for t in targets]
        if len(set(ids)) != len(ids) or any(t.get("arena_id") not in arena_ids for t in targets):
            return "probe_targets_invalid"
        if options.get("active_target") is not None and options["active_target"] not in ids:
            return "active_target_unknown"
        return None


class InProcessCitrusControl(CitrusControl):
    """Test transport only: exercise control semantics without a Unix socket."""

    def __init__(self, citrus):
        super().__init__("unused-in-process", "test")
        self.fake = citrus

    def request(self, method, params=None, operation_id=None):
        req = {"method": method, "request_id": self._next_id(method)}
        if params is not None:
            req["params"] = params
        if operation_id is not None:
            req["operation_id"] = operation_id
        with self.fake.lock:
            self.fake.requests.append(req)
            response = self.fake._handle(req)
        return json.loads(json.dumps(dict(response, _request=req)))


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

    def __init__(self, citrus, arena_id=None, counts_per_ms_at_full=400.0,
                 source_spectra=None, nonadditive_fraction=0.0,
                 noise_counts=0.0, follow_probe_target=False):
        self.citrus = citrus
        self.arena_id = arena_id
        self.counts_per_ms_at_full = counts_per_ms_at_full
        self.source_spectra = source_spectra
        self.nonadditive_fraction = nonadditive_fraction
        self.noise_counts = noise_counts
        self.follow_probe_target = follow_probe_target
        self.rng = np.random.default_rng(615)
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
        spectrum = self._shape * brightness
        if self.source_spectra is not None:
            receiver = self.arena_id or self.citrus.arenas[0]
            state = self.citrus.scene.get("arena_fills", {})
            if self.follow_probe_target and state.get("active_target"):
                receiver = next(t["arena_id"] for t in state["probe_targets"]
                                if t["target_id"] == state["active_target"])
            spectrum = np.zeros(self.pixels)
            active = 0
            for source, rgba in self.citrus.rgba_by_arena.items():
                level = sum(rgba[:3]) / (3 * 255.0)
                active += level > 0
                spectrum += level * self.source_spectra[receiver][source]
            if active > 1:
                spectrum *= 1 + self.nonadditive_fraction
        signal = spectrum * self.counts_per_ms_at_full * self.integration_us / 1000.0
        noise = self.rng.normal(0, self.noise_counts, self.pixels)
        return np.minimum(1000.0 + signal + noise, self.max_intensity)

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
