#!/usr/bin/env python3
"""
Raw spectrometer capture for calibration runs.

Unlike ``spectrometer_logger.py``, this module never applies a calibration
file, never bins, and never corrects in place. It records raw detector counts
at the native pixel resolution together with everything needed to correct
them later and reproducibly: wavelength coefficients, electrically dark pixel
indices, nonlinearity coefficients, integration time, TEC temperature and
per-scan saturation counts.

The QE Pro (and other TEC spectrometers) are supported through
``seabreeze.cseabreeze`` when available, else ``pyseabreeze``.
"""

import time

import numpy as np


SATURATION_FRACTION = 0.995  # a pixel at or above this fraction of max counts is saturated


def use_seabreeze_backend(preferred="cseabreeze"):
    """
    Select the seabreeze backend before any device is opened.

    Returns
    -------
    str
        The backend actually selected.
    """
    import seabreeze
    for backend in (preferred, "pyseabreeze"):
        try:
            seabreeze.use(backend)
            return backend
        except Exception:
            continue
    raise RuntimeError("No seabreeze backend could be selected")


def _feature(spec, name):
    """Return the first instance of a seabreeze feature, or None."""
    features = getattr(spec, "features", {}) or {}
    instances = features.get(name) or []
    return instances[0] if instances else None


class SpectralDevice:
    """
    A spectrometer opened for raw capture.

    Parameters
    ----------
    spec : seabreeze.spectrometers.Spectrometer
        An open spectrometer. Tests pass a stand-in with the same methods.
    backend : str
        Name of the seabreeze backend in use (recorded in the readout).
    """

    def __init__(self, spec, backend):
        self.spec = spec
        self.backend = backend
        self.wavelengths = np.asarray(spec.wavelengths(), dtype=float)
        self.max_intensity = float(spec.max_intensity)
        self.dark_pixels = [int(i) for i in getattr(spec, "_dp", [])]
        self.integration_limits_us = tuple(int(x) for x in spec.integration_time_micros_limits)
        self.integration_us = None

    @classmethod
    def open_first(cls, serial=None, backend="cseabreeze"):
        """Open the spectrometer with ``serial`` (or the first one found)."""
        backend = use_seabreeze_backend(backend)
        from seabreeze.spectrometers import Spectrometer
        if serial:
            spec = Spectrometer.from_serial_number(serial)
        else:
            spec = Spectrometer.from_first_available()
        return cls(spec, backend)

    def close(self):
        self.spec.close()

    def readout(self):
        """
        Describe the device as read now (identity, coefficients, limits, TEC).

        Unknown values are recorded as ``"unknown"`` rather than guessed.
        """
        nonlinearity = "unknown"
        nl_feature = _feature(self.spec, "nonlinearity_coefficients")
        if nl_feature is not None:
            try:
                nonlinearity = [float(c) for c in nl_feature.get_nonlinearity_coefficients()]
            except Exception:
                pass
        if nonlinearity == "unknown" and getattr(self.spec, "_nc", None) is not None:
            nonlinearity = [float(c) for c in self.spec._nc]

        return {
            "model": str(self.spec.model),
            "serial_number": str(self.spec.serial_number),
            "seabreeze_backend": self.backend,
            "pixel_count": int(self.spec.pixels),
            "wavelength_nm_first_last": [float(self.wavelengths[0]), float(self.wavelengths[-1])],
            "wavelengths_nm": [float(w) for w in self.wavelengths],
            "electric_dark_pixel_indices": self.dark_pixels,
            "nonlinearity_coefficients": nonlinearity,
            "max_intensity_counts": self.max_intensity,
            "integration_time_limits_us": list(self.integration_limits_us),
            "tec": self.read_tec(),
        }

    def read_tec(self):
        """Return TEC temperature in °C as read now, or ``"unknown"`` without a TEC feature."""
        tec = _feature(self.spec, "thermo_electric")
        if tec is None:
            return {"temperature_c": "unknown"}
        try:
            return {"temperature_c": float(tec.read_temperature_degrees_celsius())}
        except Exception:
            return {"temperature_c": "unknown"}

    def set_integration_us(self, integration_us):
        lo, hi = self.integration_limits_us
        integration_us = int(min(max(int(integration_us), lo), hi))
        if integration_us != self.integration_us:
            self.spec.integration_time_micros(integration_us)
            self.integration_us = integration_us
            # The first spectrum after a change can straddle the old setting.
            self.spec.intensities()
        return integration_us

    def scan(self):
        """
        Take one raw scan at the current integration time.

        Returns
        -------
        dict
            ``counts`` (numpy array, raw, uncorrected), host clock brackets,
            TEC reading and saturation summary.
        """
        started_utc_ns = time.time_ns()
        started_mono_ns = time.monotonic_ns()
        counts = np.asarray(
            self.spec.intensities(correct_dark_counts=False, correct_nonlinearity=False),
            dtype=float,
        )
        ended_mono_ns = time.monotonic_ns()
        ended_utc_ns = time.time_ns()
        saturated = int(np.count_nonzero(counts >= SATURATION_FRACTION * self.max_intensity))
        return {
            "counts": counts,
            "integration_us": self.integration_us,
            "host_utc_ns": [started_utc_ns, ended_utc_ns],
            "host_monotonic_ns": [started_mono_ns, ended_mono_ns],
            "saturated_pixel_count": saturated,
            "peak_counts": float(counts.max()),
            "tec": self.read_tec(),
        }

    def dark_level(self, counts):
        """Mean of the electrically dark pixels, or 0 when the device declares none."""
        if not self.dark_pixels:
            return 0.0
        return float(np.mean(counts[self.dark_pixels]))

    def choose_integration_us(self, initial_us, target_fraction=0.8, max_iterations=6):
        """
        Pick an integration time whose peak (above dark) sits near ``target_fraction``
        of the maximum counts.

        Returns
        -------
        int
            The chosen integration time in microseconds.
        """
        integration_us = self.set_integration_us(initial_us)
        lo, hi = self.integration_limits_us
        for _ in range(max_iterations):
            scan = self.scan()
            dark = self.dark_level(scan["counts"])
            peak = max(scan["peak_counts"] - dark, 1.0)
            headroom = self.max_intensity - dark
            fraction = peak / headroom
            if scan["saturated_pixel_count"] == 0 and 0.5 * target_fraction <= fraction <= target_fraction:
                return integration_us
            if scan["saturated_pixel_count"] > 0:
                proposed = integration_us / 4
            else:
                proposed = integration_us * (target_fraction * 0.9) / fraction
            proposed = int(min(max(proposed, lo), hi))
            if proposed == integration_us:
                return integration_us
            integration_us = self.set_integration_us(proposed)
        return integration_us
