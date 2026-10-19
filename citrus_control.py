#!/usr/bin/env python3
"""
Client for the Citrus GUI local control socket.

Citrus (the lab's projector stimulus program) accepts one JSON request per
connection on a Unix-domain socket and answers with one newline-terminated
JSON response. This module wraps the calibration-scene methods that a
spectral calibration run needs:

- ``status`` and ``active_projection_snapshot`` (identity and geometry),
- ``set_calibration_scene`` / ``calibration_scene_status`` /
  ``restore_calibration_scene`` (what is on the projector).

See Citrus ``docs/citrus_gui_automation_control.md`` ("Named calibration
scenes") and ``docs/projector_spectral_calibration_run_design_2026-10-19.md``.
"""

import itertools
import json
import socket
import time
import uuid


REQUEST_SCHEMA_ID = "citrus.local_control.request"
REQUEST_SCHEMA_VERSION = 1
SOURCE = "specsail"
MAX_RESPONSE_BYTES = 64 * 1024 * 1024


class CitrusControlError(RuntimeError):
    """Raised when Citrus refuses a request or a scene cannot be presented."""


class CitrusControl:
    """
    Minimal client for the Citrus local control socket.

    Parameters
    ----------
    socket_path : str or pathlib.Path
        Path of the socket Citrus was started with
        (``CITRUS_GUI_LOCAL_CONTROL_SOCKET``).
    run_id : str
        Prefix for request and operation ids, so every id is unique and
        traceable to the run.
    timeout_s : float, optional
        Socket timeout per request (default: 10).
    """

    def __init__(self, socket_path, run_id, timeout_s=10.0):
        self.socket_path = str(socket_path)
        self.run_id = run_id
        self.timeout_s = timeout_s
        self._counter = itertools.count(1)
        self._nonce = uuid.uuid4().hex[:8]

    def _next_id(self, kind):
        return f"{self.run_id}-{self._nonce}-{kind}-{next(self._counter)}"

    def request(self, method, params=None, operation_id=None):
        """
        Send one request and return the parsed response.

        Parameters
        ----------
        method : str
            Control method name.
        params : dict, optional
            Method parameters.
        operation_id : str, optional
            Required by Citrus for mutating methods.

        Returns
        -------
        dict
            The response object (``ok``, ``accepted``, ``effect``,
            ``status``, ...).
        """
        req = {
            "schema_id": REQUEST_SCHEMA_ID,
            "schema_version": REQUEST_SCHEMA_VERSION,
            "method": method,
            "request_id": self._next_id(method),
            "source": SOURCE,
        }
        if operation_id is not None:
            req["operation_id"] = operation_id
        if params is not None:
            req["params"] = params

        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(self.timeout_s)
            sock.connect(self.socket_path)
            sock.sendall((json.dumps(req) + "\n").encode())
            chunks = []
            received = 0
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
                received += len(chunk)
                if b"\n" in chunk or received > MAX_RESPONSE_BYTES:
                    break
        raw = b"".join(chunks).split(b"\n", 1)[0]
        if not raw:
            raise CitrusControlError(f"{method}: empty response from Citrus")
        response = json.loads(raw)
        response["_request"] = req
        return response

    def status(self):
        """Return the full Citrus ``status`` response."""
        return self.request("status")

    def active_projection_snapshot(self):
        """Return the ``active_projection_snapshot`` response (canvas geometry)."""
        return self.request("active_projection_snapshot")

    def scene_status(self):
        """Return ``effect.calibration_scene`` from ``calibration_scene_status``."""
        response = self.request("calibration_scene_status")
        return response.get("effect", {}).get("calibration_scene", {})

    def set_scene(self, transaction_id, recipe_id, arena_ids, scene_options=None):
        """
        Request a calibration scene. Returns (request_id, operation_id).

        Raises
        ------
        CitrusControlError
            If Citrus does not accept the request.
        """
        operation_id = self._next_id("op-set")
        response = self.request(
            "set_calibration_scene",
            params={
                "transaction_id": transaction_id,
                "recipe_id": recipe_id,
                "arena_ids": list(arena_ids),
                "scene_options": scene_options or {},
            },
            operation_id=operation_id,
        )
        if not (response.get("ok") and response.get("accepted")):
            raise CitrusControlError(
                f"set_calibration_scene refused: {response.get('error', 'unknown')}"
            )
        return response["_request"]["request_id"], operation_id

    def restore_scene(self, transaction_id):
        """Request restore of the pre-transaction projection. Returns (request_id, operation_id)."""
        operation_id = self._next_id("op-restore")
        response = self.request(
            "restore_calibration_scene",
            params={"transaction_id": transaction_id},
            operation_id=operation_id,
        )
        if not (response.get("ok") and response.get("accepted")):
            raise CitrusControlError(
                f"restore_calibration_scene refused: {response.get('error', 'unknown')}"
            )
        return response["_request"]["request_id"], operation_id

    def wait_for_state(self, request_id, operation_id, wanted_state,
                       timeout_s=10.0, poll_s=0.02):
        """
        Poll until the identified command reached ``wanted_state`` and is presented.

        A capture is eligible only when ``last_command.status == "applied"``,
        the echoed ids match the request, ``state == wanted_state`` and
        ``presented`` is true (Citrus control docs).

        Returns
        -------
        dict
            The scene status that satisfied the condition.

        Raises
        ------
        CitrusControlError
            If the command was rejected or the timeout passed.
        """
        deadline = time.monotonic() + timeout_s
        last = {}
        while time.monotonic() < deadline:
            last = self.scene_status()
            command = last.get("last_command") or {}
            ids_match = (command.get("request_id") == request_id and
                         command.get("operation_id") == operation_id)
            if ids_match and command.get("status") == "rejected":
                raise CitrusControlError(
                    f"scene command rejected: {command.get('error', 'unknown')}"
                )
            if (ids_match and command.get("status") == "applied" and
                    last.get("state") == wanted_state and last.get("presented") is True):
                return last
            time.sleep(poll_s)
        raise CitrusControlError(
            f"timed out waiting for state {wanted_state!r}; last state "
            f"{last.get('state')!r}, last command {last.get('last_command')!r}"
        )
