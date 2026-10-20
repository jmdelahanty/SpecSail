#!/usr/bin/env python3
"""
Thorlabs PM100USB power meter over the Linux kernel USBTMC driver.

The PM100USB is a USB Test & Measurement Class instrument. Linux's ``usbtmc``
driver exposes it as ``/dev/usbtmcN``; SCPI commands are written to that file
and answers read back, so no extra Python packages are needed. A udev rule
must give the user access to the device node, e.g.
``/etc/udev/rules.d/60-thorlabs-power-meter.rules``::

    SUBSYSTEM=="usbmisc", KERNEL=="usbtmc*", ATTRS{idVendor}=="1313", MODE="0660", GROUP="plugdev"

SCPI commands used, checked against the Thorlabs "PM100USB Operation Manual",
Rev C, section 6.3 (PM100USB SCPI Commands):

- ``*IDN?``: manufacturer, model, serial, firmware. Verified (6.3.2.2.1).
- ``SYST:SENS:IDN?``: sensor name, serial, calibration message, type,
  sub-type, flags, comma-separated. Verified (field list in the LabVIEW
  example, ``PM100D_SensorFlag.vi``). The exact quoting of each field is not
  shown in the manual, so parsing strips quotes and whitespace.
- ``SENS:CORR:WAV <nm>`` / ``SENS:CORR:WAV?``: operating wavelength.
  Verified (SENSe:CORRection:WAVelength; the manual's examples use
  ``CORR:WAVE``).
- ``SENS:AVER:COUN <n>`` / ``SENS:AVER:COUN?``: averaging rate, one sample
  about 3 ms. Verified (SENSe:AVERage[:COUNt]).
- ``SENS:POW:RANG:AUTO ON``: auto ranging. Verified (SENSe:POWer[:DC]:RANGe:AUTO).
- ``SENS:POW:UNIT W``: power unit. Verified (SENSe:POWer[:DC]:UNIT {W|DBM}).
- ``MEAS:POW?``: one power measurement, in the selected unit. The manual lists
  ``MEASure[:SCALar][:POWer]`` as "Performs a power measurement"; the query
  form with ``?`` follows standard SCPI and is assumed, not shown verbatim.
  ``READ?`` (verified) is the fallback if ``MEAS:POW?`` is refused.
- ``SENS:CORR:COLL:ZERO:INIT``: zero adjustment. Verified. Only run it with the
  sensor covered; it is never called automatically.
"""

import glob
import os
import time


DEFAULT_DEVICE_GLOB = "/dev/usbtmc*"
READ_SIZE = 4096


class PowerMeterError(RuntimeError):
    """Raised when the meter cannot be opened or answers unexpectedly."""


class UsbtmcTransport:
    """Write a command / read an answer on a ``/dev/usbtmcN`` node."""

    def __init__(self, path):
        self.path = path
        try:
            self.fd = os.open(path, os.O_RDWR)
        except OSError as exc:
            raise PowerMeterError(
                f"cannot open {path}: {exc}. Check the udev rule and that the meter is plugged in."
            ) from exc

    def write(self, command):
        os.write(self.fd, (command.strip() + "\n").encode("ascii"))

    def query(self, command):
        self.write(command)
        return os.read(self.fd, READ_SIZE).decode("ascii", errors="replace").strip()

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


def parse_sensor_info(text):
    """
    Parse the ``SYST:SENS:IDN?`` answer.

    Returns
    -------
    dict
        ``name``, ``serial``, ``calibration``, ``type``, ``subtype``,
        ``flags`` (strings; missing fields are ``None``) and ``raw``.
    """
    fields = [f.strip().strip('"').strip() for f in text.split(",")]
    keys = ("name", "serial", "calibration", "type", "subtype", "flags")
    info = {k: (fields[i] if i < len(fields) and fields[i] != "" else None)
            for i, k in enumerate(keys)}
    info["raw"] = text
    return info


def parse_idn(text):
    """Parse ``*IDN?``: manufacturer, model, serial, firmware."""
    fields = [f.strip() for f in text.split(",")]
    keys = ("manufacturer", "model", "serial", "firmware")
    info = {k: (fields[i] if i < len(fields) else None) for i, k in enumerate(keys)}
    info["raw"] = text
    return info


def parse_float(text, command):
    try:
        return float(text.strip())
    except ValueError as exc:
        raise PowerMeterError(f"{command}: expected a number, got {text!r}") from exc


class PM100USB:
    """
    Thorlabs PM100USB console with a photodiode sensor head.

    Parameters
    ----------
    device : str, optional
        ``/dev/usbtmcN``. Default: the first ``/dev/usbtmc*`` whose ``*IDN?``
        names a Thorlabs PM100USB.
    transport : object, optional
        Anything with ``query(cmd)``, ``write(cmd)`` and ``close()``; tests
        pass a fake here.
    """

    def __init__(self, device=None, transport=None):
        if transport is not None:
            self.transport = transport
            self.device = device or "fake"
        else:
            self.device = device or find_device()
            self.transport = UsbtmcTransport(self.device)

    def query(self, command):
        return self.transport.query(command)

    def write(self, command):
        self.transport.write(command)

    def idn(self):
        """``*IDN?`` parsed: manufacturer, model, serial, firmware."""
        return parse_idn(self.query("*IDN?"))

    def sensor_info(self):
        """``SYST:SENS:IDN?`` parsed: name, serial, calibration, type, subtype, flags."""
        return parse_sensor_info(self.query("SYST:SENS:IDN?"))

    def configure(self, average_count, range_upper_W=None, lowpass=None):
        """
        Watts, the given averaging rate (one sample ~3 ms), and the range and bandwidth.

        For pulsed sources such as DLP projectors use a FIXED range above the pulse peaks and the low-pass
        filter ON. On 2026-10-20, with a BenQ DLP projector, auto range settled about 20x too sensitive
        (17.8 µW full scale for ~320 µW mean), clipping the pulses so white read 116 µW instead of ~323 µW.
        A fixed 17.8 mW range with the low-pass filter reproduced the spectrometer's gray/white ratios
        (gray 178: 0.348 vs 0.345; gray 128: 0.119 vs 0.120). Without the filter, white read ~14% high.
        Fixed 1.78 mW and 17.8 mW ranges agreed on white (323.7 vs 323.3 µW), so 1.78 mW is safe there.
        Returns the read-back settings.
        """
        self.write("SENS:POW:UNIT W")
        if range_upper_W is None:
            self.write("SENS:POW:RANG:AUTO ON")
        else:
            self.write("SENS:POW:RANG:AUTO OFF")
            self.write(f"SENS:POW:RANG:UPP {float(range_upper_W):g}")
        if lowpass is not None:
            self.write(f"INP:PDI:FILT:LPAS:STAT {'ON' if lowpass else 'OFF'}")
        self.set_average(average_count)
        return self.settings()

    def settings(self):
        """Read-back of range, auto-range and low-pass state (raw strings; missing ones as None)."""
        out = {}
        for key, cmd in (("range_W", "SENS:POW:RANG?"), ("auto_range", "SENS:POW:RANG:AUTO?"),
                         ("lowpass", "INP:PDI:FILT:LPAS:STAT?"), ("average_count", "SENS:AVER:COUN?")):
            try:
                out[key] = self.query(cmd).strip()
            except Exception:  # fake transports may not know every command
                out[key] = None
        return out

    def set_wavelength(self, nm):
        self.write(f"SENS:CORR:WAV {float(nm):g}")

    def get_wavelength(self):
        return parse_float(self.query("SENS:CORR:WAV?"), "SENS:CORR:WAV?")

    def set_average(self, count):
        self.write(f"SENS:AVER:COUN {int(count)}")

    def get_average(self):
        return int(parse_float(self.query("SENS:AVER:COUN?"), "SENS:AVER:COUN?"))

    def read_power(self):
        """One measurement in watts (averaged over the set rate)."""
        return parse_float(self.query("MEAS:POW?"), "MEAS:POW?")

    def read_n(self, n):
        """``n`` consecutive measurements, each with its host UTC time (s since epoch)."""
        values = []
        for _ in range(n):
            values.append({"power_W": self.read_power(), "host_time_s": time.time()})
        return values

    def zero(self):
        """Zero adjustment. Only with the sensor covered; never called automatically."""
        self.write("SENS:CORR:COLL:ZERO:INIT")

    def close(self):
        self.transport.close()


def find_device(pattern=DEFAULT_DEVICE_GLOB):
    """First ``/dev/usbtmc*`` that identifies as a Thorlabs PM100USB."""
    candidates = sorted(glob.glob(pattern))
    if not candidates:
        raise PowerMeterError(
            "no /dev/usbtmc* device: plug the PM100USB in and check the usbtmc driver"
        )
    errors = []
    for path in candidates:
        try:
            transport = UsbtmcTransport(path)
        except PowerMeterError as exc:
            errors.append(str(exc))
            continue
        try:
            idn = parse_idn(transport.query("*IDN?"))
        except OSError as exc:
            errors.append(f"{path}: {exc}")
            continue
        finally:
            transport.close()
        if "thorlabs" in (idn["manufacturer"] or "").lower() and "PM100" in (idn["model"] or ""):
            return path
        errors.append(f"{path}: not a PM100 ({idn['raw']})")
    raise PowerMeterError("no PM100USB found: " + "; ".join(errors))
