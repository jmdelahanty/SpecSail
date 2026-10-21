#!/usr/bin/env python3
"""
Draw a teaching figure showing how a calibration-run measurement becomes
"counts per second per pixel".

Panels, top to bottom:
1. the detector strip: 1044 pixels coloured by the wavelength that lands on
   each, with the covered (electrical dark) pixels marked;
2. one measurement's raw counts per pixel, every scan, with the dark pixels
   and their mean (the electrical baseline);
3. the same measurement after averaging scans, subtracting the baseline and
   dividing by the integration time, with a band summed to one number;
4. several measurements together, showing that each projector colour lights up
   a different stretch of pixels.

Usage:
    pixi run explain RUN_DIR [--measurement ID] [--compare ID ID ...] [--out FILE]

Without --measurement the first white_255 patch is used; without --compare the
red, green, blue and white 255 patches at the same position are overlaid.
"""

import argparse
import csv
import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

BAND_NM = (420.0, 700.0)


def wavelength_to_rgb(nm):
    """Approximate display colour for a wavelength (grey outside 380-780 nm)."""
    if nm < 380 or nm > 780:
        return (0.55, 0.55, 0.55)
    if nm < 440:
        r, g, b = -(nm - 440) / 60, 0.0, 1.0
    elif nm < 490:
        r, g, b = 0.0, (nm - 440) / 50, 1.0
    elif nm < 510:
        r, g, b = 0.0, 1.0, -(nm - 510) / 20
    elif nm < 580:
        r, g, b = (nm - 510) / 70, 1.0, 0.0
    elif nm < 645:
        r, g, b = 1.0, -(nm - 645) / 65, 0.0
    else:
        r, g, b = 1.0, 0.0, 0.0
    fade = 1.0
    if nm < 420:
        fade = 0.3 + 0.7 * (nm - 380) / 40
    elif nm > 700:
        fade = 0.3 + 0.7 * (780 - nm) / 80
    return (r * fade, g * fade, b * fade)


def load_measurement(run_dir, measurement_id):
    """Return (wavelengths, counts[scan, pixel], measurement record)."""
    mdir = run_dir / "measurements" / measurement_id
    with open(mdir / "spectrum.csv") as f:
        rows = list(csv.reader(f))[1:]
    wavelengths = np.array([float(r[1]) for r in rows])
    counts = np.array([[float(v) for v in r[2:]] for r in rows]).T
    record = json.loads((mdir / "measurement.json").read_text())
    return wavelengths, counts, record


def dark_pixels(run_dir):
    readout = json.loads((run_dir / "instrument" / "device_readout.json").read_text())
    return list(readout.get("electric_dark_pixel_indices") or [])


def counts_per_second(counts, record, dark):
    mean = counts.mean(axis=0)
    baseline = mean[dark].mean() if dark else 0.0
    return (mean - baseline) / (record["integration_us"] / 1e6), baseline


def find(run_dir, pattern, position=None):
    ids = sorted(p.name for p in (run_dir / "measurements").iterdir() if pattern in p.name)
    if position:
        ids = [i for i in ids if i.startswith(position)] or ids
    return ids[0] if ids else None


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--measurement")
    parser.add_argument("--compare", nargs="*")
    parser.add_argument("--out", type=Path, default=Path("explain_spectrum.png"))
    args = parser.parse_args()

    run_dir = args.run_dir
    dark = dark_pixels(run_dir)
    main_id = args.measurement or find(run_dir, "__white_255__")
    if main_id is None:
        raise SystemExit("no white_255 measurement found; pass --measurement")
    position = main_id.split("__")[0]
    compare = args.compare or [i for i in (find(run_dir, f"__{c}_255__", position)
                                           for c in ("red", "green", "blue", "white")) if i]

    wl, counts, record = load_measurement(run_dir, main_id)
    pixels = np.arange(len(wl))
    rate, baseline = counts_per_second(counts, record, dark)
    seconds = record["integration_us"] / 1e6

    fig, axes = plt.subplots(4, 1, figsize=(11, 13),
                             gridspec_kw={"height_ratios": [0.55, 1.4, 1.4, 1.4]})

    # 1. detector strip
    ax = axes[0]
    strip = np.array([[wavelength_to_rgb(w) for w in wl]])
    strip[0, dark] = (0.05, 0.05, 0.05)
    ax.imshow(strip, aspect="auto", extent=(0, len(wl), 0, 1))
    ax.set_yticks([])
    ticks = [0, 200, 400, 600, 800, 1043]
    ax.set_xticks(ticks)
    ax.set_xticklabels([f"px {t}\n{wl[t]:.0f} nm" for t in ticks], fontsize=9)
    ax.set_title("1. The detector: 1044 identical pixels. The grating spreads the light, so each pixel\n"
                 "receives one narrow band of wavelengths (black ends = covered dark pixels; grey = outside the visible)",
                 fontsize=10, loc="left")

    # 2. raw counts
    ax = axes[1]
    for i, scan in enumerate(counts):
        ax.plot(pixels, scan, lw=0.8, alpha=0.7, label=f"scan {i}")
    ax.scatter(dark, counts.mean(axis=0)[dark], color="k", zorder=5, s=18, label="covered dark pixels")
    ax.axhline(baseline, color="k", ls="--", lw=1, label=f"electrical baseline ≈ {baseline:,.0f} counts")
    ax.set_xlim(0, len(wl) - 1)
    ax.set_xlabel("pixel number")
    ax.set_ylabel("raw counts")
    ax.set_title(f"2. Raw readout of {main_id}: counts per pixel after a {seconds:g} s exposure, "
                 f"{len(counts)} scans", fontsize=10, loc="left")
    ax.legend(fontsize=8, loc="upper right")

    # 3. counts per second per pixel, with a summed band
    ax = axes[2]
    ax.plot(wl, rate, color="#333", lw=1)
    in_band = (wl >= BAND_NM[0]) & (wl <= BAND_NM[1])
    ax.fill_between(wl[in_band], 0, rate[in_band], color="#f2b134", alpha=0.45,
                    label=f"sum over {BAND_NM[0]:.0f}–{BAND_NM[1]:.0f} nm = {rate[in_band].sum() / 1e6:.3f} M counts/s")
    ax.set_xlim(wl[0], wl[-1])
    ax.set_xlabel("wavelength (nm)")
    ax.set_ylabel("counts / s per pixel")
    ax.set_title("3. Average the scans, subtract the baseline, divide by the exposure time. "
                 "Adding pixels in a band gives one number", fontsize=10, loc="left")
    ax.legend(fontsize=8, loc="upper right")

    # 4. compare colours
    ax = axes[3]
    colours = {"red": "#d4483f", "green": "#2e9a58", "blue": "#3d6fd6", "white": "#555555", "gray": "#8b9099"}
    for mid in compare:
        w2, c2, r2 = load_measurement(run_dir, mid)
        rate2, _ = counts_per_second(c2, r2, dark)
        patch = mid.split("__")[1]
        ax.plot(w2, rate2, lw=1.2, color=colours.get(patch.split("_")[0], None), label=patch)
    ax.set_xlim(wl[0], wl[-1])
    ax.set_xlabel("wavelength (nm)")
    ax.set_ylabel("counts / s per pixel")
    ax.set_title(f"4. Different projector colours light up different pixels ({position})",
                 fontsize=10, loc="left")
    ax.legend(fontsize=8, loc="upper right")

    fig.text(0.01, 0.005,
             "Counts are relative: the detector and grating are more sensitive at some wavelengths than others, "
             "and this unit has no irradiance calibration.", fontsize=8, color="#555")
    fig.tight_layout(rect=(0, 0.015, 1, 1))
    fig.savefig(args.out, dpi=130)
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
