#!/usr/bin/env python3
"""
Animate how light travels through the spectrometer and builds up a spectrum.

Simulated "photons" leave the fiber, pass the slit, are made parallel by a
mirror, fanned out by wavelength at the grating, focused by a second mirror and
counted by the pixel they land on. Each photon's wavelength is drawn from a
real measurement in a calibration run, so the bars at the bottom grow into
that measurement's spectrum. The movie plays white, then red, green and blue
primaries, so you can see each colour fill a different stretch of pixels.

Each dot stands for very many real photons; the geometry is a schematic of a
crossed Czerny-Turner spectrometer, not to scale.

Usage:
    pixi run animate RUN_DIR [--position arena_1_center] [--out light_path.mp4]
"""

import argparse
import csv
import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.animation import FFMpegWriter, PillowWriter  # noqa: E402
import numpy as np  # noqa: E402

from explain_spectrum import wavelength_to_rgb  # noqa: E402

FPS = 20
N_BINS = 174          # 1044 pixels shown as 174 bars of 6 pixels
SPEED = 0.025         # path length per frame (axes units)
DARK = [0, 1, 2, 3, 1040, 1041, 1042, 1043]

# Schematic geometry (axes coordinates, x 0..1, y 0..1)
FIBER = (0.06, 0.60)
SLIT = (0.16, 0.60)
MIRROR1 = (0.32, 0.86)
GRATING = (0.50, 0.55)
MIRROR2 = (0.76, 0.86)   # centre; each wavelength hits its own spot
MIRROR2_HALF = 0.11
DET_Y = 0.30
DET_X0, DET_X1 = 0.30, 0.95


def load_rate(run_dir, measurement_id):
    mdir = run_dir / "measurements" / measurement_id
    with open(mdir / "spectrum.csv") as f:
        rows = list(csv.reader(f))[1:]
    wl = np.array([float(r[1]) for r in rows])
    counts = np.array([[float(v) for v in r[2:]] for r in rows]).mean(axis=1)
    rec = json.loads((mdir / "measurement.json").read_text())
    rate = (counts - counts[DARK].mean()) / (rec["integration_us"] / 1e6)
    return wl, np.clip(rate, 0, None)


def find(run_dir, patch, position):
    hits = sorted(p.name for p in (run_dir / "measurements").iterdir()
                  if p.name.startswith(position + "__" + patch + "__"))
    return hits[0] if hits else None


def det_x(pixel, n_pixels):
    return DET_X0 + (DET_X1 - DET_X0) * pixel / (n_pixels - 1)


def path_for(pixel, n_pixels):
    """Polyline from fiber to the detector position of this pixel."""
    frac = pixel / (n_pixels - 1)
    spot = (MIRROR2[0] - MIRROR2_HALF + 2 * MIRROR2_HALF * frac, MIRROR2[1])
    return np.array([FIBER, SLIT, MIRROR1, GRATING, spot, (det_x(pixel, n_pixels), DET_Y)])


def point_along(path, s):
    seg = np.diff(path, axis=0)
    lengths = np.hypot(seg[:, 0], seg[:, 1])
    total = lengths.sum()
    if s >= total:
        return path[-1], len(path) - 1, True
    acc = 0.0
    for i, length in enumerate(lengths):
        if s <= acc + length:
            f = (s - acc) / length
            return path[i] + f * seg[i], i, False
        acc += length
    return path[-1], len(path) - 1, True


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--position", default="arena_1_center")
    parser.add_argument("--out", type=Path, default=Path("light_path.mp4"))
    parser.add_argument("--photons-per-frame", type=int, default=14)
    parser.add_argument("--seed", type=int, default=1)
    args = parser.parse_args()
    rng = np.random.default_rng(args.seed)

    phases = []
    for patch, label, frames in [("white_255", "white (R, G, B = 255)", 260),
                                 ("red_255", "red primary", 150),
                                 ("green_255", "green primary", 150),
                                 ("blue_255", "blue primary", 150)]:
        mid = find(args.run_dir, patch, args.position)
        if mid:
            wl, rate = load_rate(args.run_dir, mid)
            phases.append({"label": label, "frames": frames, "wl": wl, "p": rate / rate.sum(), "id": mid})
    if not phases:
        raise SystemExit(f"no *_255 measurements for {args.position} in {args.run_dir}")
    wl = phases[0]["wl"]
    n_pixels = len(wl)

    fig = plt.figure(figsize=(12.8, 7.2), dpi=100)
    ax = fig.add_axes((0, 0.30, 1, 0.70))
    hax = fig.add_axes((DET_X0 - 0.003, 0.06, DET_X1 - DET_X0 + 0.006, 0.20))
    ax.set_xlim(0, 1)
    ax.set_ylim(0.2, 1.12)
    ax.axis("off")

    # static schematic
    ax.add_patch(plt.Rectangle((0.0, 0.585), FIBER[0], 0.03, color="#2e66c9"))
    ax.text(0.01, 0.66, "fiber", color="#2e66c9", fontsize=11, weight="bold")
    ax.plot([SLIT[0], SLIT[0]], [0.52, 0.585], color="k", lw=4)
    ax.plot([SLIT[0], SLIT[0]], [0.615, 0.68], color="k", lw=4)
    ax.text(SLIT[0] - 0.02, 0.48, "slit", fontsize=10)
    ax.plot([MIRROR1[0] - 0.05, MIRROR1[0] + 0.05], [MIRROR1[1] + 0.01] * 2, color="#333", lw=5)
    ax.text(MIRROR1[0] - 0.09, MIRROR1[1] + 0.04, "mirror: makes the beam parallel", fontsize=10)
    ax.plot([MIRROR2[0] - MIRROR2_HALF - 0.01, MIRROR2[0] + MIRROR2_HALF + 0.01], [MIRROR2[1] + 0.01] * 2,
            color="#333", lw=5)
    ax.text(MIRROR2[0] - 0.10, MIRROR2[1] + 0.04, "mirror: focuses each colour onto its pixel", fontsize=10)
    ax.plot([GRATING[0] - 0.05, GRATING[0] + 0.05], [GRATING[1] - 0.02, GRATING[1] - 0.02], color="#666", lw=7)
    ax.text(GRATING[0] - 0.30, GRATING[1] - 0.07, "grating: sends each wavelength off at its own angle", fontsize=10)
    for p in range(n_pixels):
        if p % 6 == 0:
            colour = (0.05, 0.05, 0.05) if p in DARK else wavelength_to_rgb(wl[p])
            ax.add_patch(plt.Rectangle((det_x(p, n_pixels), DET_Y - 0.03), (DET_X1 - DET_X0) / N_BINS,
                                       0.03, color=colour))
    ax.text(DET_X0, DET_Y - 0.07, f"pixel 0 ≈ {wl[0]:.0f} nm", fontsize=9)
    ax.text(DET_X1 - 0.11, DET_Y - 0.07, f"pixel {n_pixels - 1} ≈ {wl[-1]:.0f} nm", fontsize=9)
    ax.text(0.40, DET_Y - 0.07, "detector: 1044 identical pixels", fontsize=10, weight="bold")
    title = ax.text(0.01, 1.07, "", fontsize=15, weight="bold")
    subtitle = ax.text(0.01, 1.02, "", fontsize=11, color="#444")
    ax.text(0.02, 0.24, "Each dot stands for very many photons.\nSchematic, not to scale.", fontsize=9, color="#666")
    dots = ax.scatter([], [], s=16, edgecolors="none")

    hist = np.zeros(N_BINS)
    bar_colours = [wavelength_to_rgb(wl[min(i * 6 + 3, n_pixels - 1)]) for i in range(N_BINS)]
    bars = hax.bar(np.arange(N_BINS), np.zeros(N_BINS), width=1.0, color=bar_colours)
    hax.set_xlim(-0.5, N_BINS - 0.5)
    hax.set_xticks([])
    hax.set_yticks([])
    hax.set_ylabel("photons\ncounted", fontsize=9)
    for side in ("top", "right"):
        hax.spines[side].set_visible(False)

    writer = (FFMpegWriter(fps=FPS, bitrate=2400) if args.out.suffix == ".mp4"
              else PillowWriter(fps=FPS))
    with writer.saving(fig, str(args.out), dpi=100):
        for phase in phases:
            hist[:] = 0
            photons = []  # (spawn_frame, pixel, path)
            title.set_text(f"Measuring {phase['label']}")
            subtitle.set_text(f"wavelengths drawn from {phase['id']}")
            for frame in range(phase["frames"]):
                if frame < phase["frames"] - 40:
                    pix = rng.choice(n_pixels, size=args.photons_per_frame, p=phase["p"])
                    for px in pix:
                        photons.append((frame, int(px), path_for(px, n_pixels)))
                xy, cols, alive = [], [], []
                for spawn, px, path in photons:
                    pos, seg, done = point_along(path, (frame - spawn) * SPEED)
                    if done:
                        hist[min(px // 6, N_BINS - 1)] += 1
                        continue
                    alive.append((spawn, px, path))
                    xy.append(pos)
                    cols.append((0.75, 0.75, 0.75) if seg < 3 else wavelength_to_rgb(wl[px]))
                photons = alive
                dots.set_offsets(np.array(xy) if xy else np.empty((0, 2)))
                dots.set_color(cols if cols else "none")
                top = max(hist.max(), 1)
                for b, h in zip(bars, hist):
                    b.set_height(h / top)
                hax.set_ylim(0, 1.05)
                writer.grab_frame()
        title.set_text("Each pixel counts one narrow band of wavelengths")
        subtitle.set_text("counts ÷ exposure time = counts per second per pixel; "
                          "add pixels over a band for one number")
        dots.set_offsets(np.empty((0, 2)))
        for _ in range(FPS * 4):
            writer.grab_frame()
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
