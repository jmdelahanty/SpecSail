#!/usr/bin/env python3
"""
Animate why a bare photodiode head only approximately measures irradiance,
and what a cosine corrector and a bare fiber do differently.

Part 1, "light from a diffusing film": simulated rays leave a diffusing film
in all directions (Lambertian: more light near vertical, less at grazing
angles) and travel up towards four detectors held face-down above it:

  1. an ideal flat surface: counts every ray that crosses its area, which is
     irradiance by definition;
  2. a photodiode head: the sensor sits a little recessed behind a housing rim
     and a window, so steep rays hit the inner wall or reflect off the window;
  3. a cosine corrector: a diffusing disc that sticks out slightly, so it also
     catches steep rays on its rim;
  4. a bare fiber: a tiny face that only accepts rays within about ±13°.

Counters show each detector's total as a percentage of the ideal surface.

Part 2, "cosine response": a parallel beam sweeps from straight on to grazing,
and each detector's response is plotted against the ideal cos(θ) curve.

The geometry and numbers are a 2D schematic chosen to show the effect, not a
model of a specific product. For the S130VC on a diffusing film, the expected
under-read is roughly 5–15% (rig-designer estimate, 2026-10-20).

Usage:
    pixi run animate-cosine [--out cosine_response.mp4]
"""

import argparse
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.animation import FFMpegWriter, PillowWriter  # noqa: E402
import numpy as np  # noqa: E402

FPS = 20
SPEED = 0.16            # path length per frame (scene units)
FILM_Y = 0.6
N_GLASS = 1.5

# Detector geometry (scene units); all face down, apertures at the same height.
APERTURE_Y = 3.2        # rim / window plane, or the corrector's bottom face
WIDTH = 1.0             # aperture width for the three big detectors
RECESS = 0.10           # how far the photodiode sits behind its rim
CC_PROTRUDE = 0.07      # how far the cosine corrector's disc sticks out below the housing
FIBER_WIDTH = 0.35
FIBER_HALF_ANGLE = np.radians(13)

DETECTORS = [
    {"key": "ideal", "label": "ideal flat surface\n(= irradiance)", "x": 1.6, "color": "#3b7dd8"},
    {"key": "head", "label": "photodiode head\n(recessed, windowed)", "x": 4.4, "color": "#d9822b"},
    {"key": "cc", "label": "cosine corrector", "x": 7.2, "color": "#2e9a58"},
    {"key": "fiber", "label": "bare fiber\n(±13° cone)", "x": 10.0, "color": "#a1468c"},
]


def fresnel_unpolarised(theta, n=N_GLASS):
    """Reflectance of an air-glass surface for unpolarised light at incidence angle theta (radians)."""
    theta = np.clip(theta, 0, np.radians(89.9))
    st = np.sin(theta) / n
    ct = np.sqrt(1 - st ** 2)
    ci = np.cos(theta)
    rs = ((ci - n * ct) / (ci + n * ct)) ** 2
    rp = ((ct - n * ci) / (ct + n * ci)) ** 2
    return 0.5 * (rs + rp)


def lambertian_angle(rng, size):
    """Angles from vertical (radians, signed) for a 2D Lambertian emitter: p(θ) ∝ cos θ."""
    return np.arcsin(rng.uniform(-1, 1, size))


def x_at(x0, theta, y):
    return x0 + (y - FILM_Y) * np.tan(theta)


def detect(det, x0, theta, rng):
    """Return (accepted, stop_y, reflected) for a ray from (x0, FILM_Y) at angle theta."""
    cx, key = det["x"], det["key"]
    if key == "ideal":
        xa = x_at(x0, theta, APERTURE_Y)
        return abs(xa - cx) < WIDTH / 2, APERTURE_Y, False
    if key == "head":
        xa = x_at(x0, theta, APERTURE_Y)
        if abs(xa - cx) >= WIDTH / 2:
            return False, APERTURE_Y, False
        if rng.uniform() < fresnel_unpolarised(abs(theta)):
            return False, APERTURE_Y, True
        xf = x_at(x0, theta, APERTURE_Y + RECESS)
        if abs(xf - cx) >= WIDTH / 2:          # hits the inner wall of the housing
            return False, APERTURE_Y + RECESS * 0.5, False
        return True, APERTURE_Y + RECESS, False
    if key == "cc":
        bottom = APERTURE_Y - CC_PROTRUDE
        xb = x_at(x0, theta, bottom)
        if abs(xb - cx) < WIDTH / 2:
            if rng.uniform() < fresnel_unpolarised(abs(theta)):
                return False, bottom, True
            return True, bottom, False
        # steep rays can enter through the disc's exposed rim
        side_x = cx - WIDTH / 2 if xb < cx else cx + WIDTH / 2
        if abs(np.tan(theta)) > 1e-6:
            y_side = FILM_Y + (side_x - x0) / np.tan(theta)
            if bottom <= y_side <= APERTURE_Y:
                side_angle = np.pi / 2 - abs(theta)
                if rng.uniform() < fresnel_unpolarised(side_angle):
                    return False, y_side, True
                return True, y_side, False
        return False, bottom, False
    if key == "fiber":
        xa = x_at(x0, theta, APERTURE_Y)
        return abs(xa - cx) < FIBER_WIDTH / 2 and abs(theta) < FIBER_HALF_ANGLE, APERTURE_Y, False
    raise ValueError(key)


def response_curve(key, thetas):
    """Relative response to a parallel beam at each angle (2D), normalised to 1 at normal incidence."""
    c = np.cos(thetas)
    r0 = fresnel_unpolarised(0.0)
    if key == "ideal":
        return c
    if key == "head":
        overlap = np.clip(WIDTH - RECESS * np.tan(thetas), 0, None) / WIDTH
        return c * overlap * (1 - fresnel_unpolarised(thetas)) / (1 - r0)
    if key == "cc":
        bottom = c * (1 - fresnel_unpolarised(thetas))
        side = (CC_PROTRUDE / WIDTH) * np.sin(thetas) * (1 - fresnel_unpolarised(np.pi / 2 - thetas))
        return (bottom + side) / (1 - r0)
    if key == "fiber":
        return np.where(thetas < FIBER_HALF_ANGLE, c, 0.0)
    raise ValueError(key)


def draw_detector(ax, det):
    cx, key, col = det["x"], det["key"], det["color"]
    housing = "#555555"
    if key == "ideal":
        ax.plot([cx - WIDTH / 2, cx + WIDTH / 2], [APERTURE_Y, APERTURE_Y], color=col, lw=5, solid_capstyle="butt")
    elif key == "head":
        ax.add_patch(plt.Rectangle((cx - WIDTH / 2 - 0.35, APERTURE_Y), WIDTH + 0.7, RECESS + 0.5, color=housing))
        ax.add_patch(plt.Rectangle((cx - WIDTH / 2, APERTURE_Y), WIDTH, RECESS, color="white"))
        ax.plot([cx - WIDTH / 2, cx + WIDTH / 2], [APERTURE_Y + RECESS] * 2, color=col, lw=5, solid_capstyle="butt")
        ax.plot([cx - WIDTH / 2, cx + WIDTH / 2], [APERTURE_Y, APERTURE_Y], color="#9fc5e8", lw=2)
        ax.text(cx + WIDTH / 2 + 0.38, APERTURE_Y + 0.05, "window", fontsize=7, color="#5a7fa3", va="bottom")
    elif key == "cc":
        ax.add_patch(plt.Rectangle((cx - WIDTH / 2 - 0.35, APERTURE_Y), WIDTH + 0.7, 0.6, color=housing))
        ax.add_patch(plt.Rectangle((cx - WIDTH / 2, APERTURE_Y - CC_PROTRUDE), WIDTH, CC_PROTRUDE + 0.25,
                                   facecolor="#f4f4f4", edgecolor=col, lw=2))
    elif key == "fiber":
        ax.add_patch(plt.Rectangle((cx - FIBER_WIDTH / 2, APERTURE_Y), FIBER_WIDTH, 0.9, color="#2e66c9"))
        ax.plot([cx - FIBER_WIDTH / 2, cx + FIBER_WIDTH / 2], [APERTURE_Y, APERTURE_Y], color=col, lw=4)
        for s in (-1, 1):
            ax.plot([cx, cx + s * 2.2 * np.tan(FIBER_HALF_ANGLE)], [APERTURE_Y, APERTURE_Y - 2.2],
                    color=col, lw=0.8, ls=":")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, default=Path("cosine_response.mp4"))
    parser.add_argument("--rays-per-frame", type=int, default=60)
    parser.add_argument("--seed", type=int, default=3)
    args = parser.parse_args()
    rng = np.random.default_rng(args.seed)

    fig = plt.figure(figsize=(12.8, 7.2), dpi=100)
    ax = fig.add_axes((0.02, 0.18, 0.96, 0.72))
    ax.set_xlim(0, 11.6)
    ax.set_ylim(0, 5.2)
    ax.axis("off")
    title = fig.text(0.02, 0.95, "", fontsize=16, weight="bold")
    subtitle = fig.text(0.02, 0.915, "", fontsize=11, color="#444")
    fig.text(0.02, 0.02, "2D schematic; geometry and numbers chosen to show the effect, not a model of a "
             "specific product.", fontsize=8.5, color="#777")

    # film
    ax.add_patch(plt.Rectangle((0, FILM_Y - 0.18), 11.6, 0.18, color="#ece6c8"))
    ax.text(0.1, FILM_Y - 0.45, "diffusing film, lit from below", fontsize=10, color="#8a7a3a")
    for det in DETECTORS:
        draw_detector(ax, det)
        ax.text(det["x"], 4.95, det["label"], ha="center", va="top", fontsize=10, color=det["color"],
                weight="bold")
    counters = {d["key"]: ax.text(d["x"], 1.55, "", ha="center", fontsize=13, color=d["color"], weight="bold")
                for d in DETECTORS}
    dots = ax.scatter([], [], s=7, edgecolors="none")

    writer = FFMpegWriter(fps=FPS, bitrate=3000) if args.out.suffix == ".mp4" else PillowWriter(fps=FPS)
    with writer.saving(fig, str(args.out), dpi=100):
        # ---------------- part 1: diffuse film ----------------
        title.set_text("Light from a diffusing film arrives at every angle")
        subtitle.set_text("Each detector counts the rays it accepts. An ideal flat surface counts all of them: "
                          "that is irradiance.")
        hits = {d["key"]: 0.0 for d in DETECTORS}
        rays = []   # (x0, theta, spawn_frame, stop_y, status, det_color, reflected)
        n_frames = 420
        for frame in range(n_frames):
            if frame < n_frames - 40:
                for det in DETECTORS:
                    k = args.rays_per_frame // len(DETECTORS)
                    span = 2.6
                    x0s = det["x"] + rng.uniform(-span, span, k)
                    ths = lambertian_angle(rng, k)
                    for x0, th in zip(x0s, ths):
                        ok, stop_y, refl = detect(det, x0, th, rng)
                        # only draw rays that come near this detector, to keep the picture readable
                        if abs(x_at(x0, th, APERTURE_Y) - det["x"]) < 1.2 or ok:
                            rays.append([x0, th, frame, stop_y, ok, det["color"], refl])
                        if ok:
                            hits[det["key"]] += 1
            xy, cols, alive = [], [], []
            for r in rays:
                x0, th, sp, stop_y, ok, col, refl = r
                s = (frame - sp) * SPEED
                y_end = stop_y
                length_to_stop = (y_end - FILM_Y) / np.cos(th)
                if s <= length_to_stop:
                    x = x0 + s * np.sin(th)
                    y = FILM_Y + s * np.cos(th)
                    xy.append((x, y))
                    cols.append((0.95, 0.75, 0.2) if y < 1.2 else (0.98, 0.85, 0.35))
                    alive.append(r)
                elif refl and s <= 2 * length_to_stop:
                    back = s - length_to_stop
                    xs = x0 + length_to_stop * np.sin(th) + back * np.sin(th)
                    ys = y_end - back * np.cos(th)
                    xy.append((xs, ys))
                    cols.append((0.6, 0.6, 0.6))
                    alive.append(r)
            rays = alive
            dots.set_offsets(np.array(xy) if xy else np.empty((0, 2)))
            dots.set_color(cols if cols else "none")
            ideal = max(hits["ideal"], 1)
            for k, t in counters.items():
                # windowed detectors lose ~4% even straight on; scale that out so 100% = ideal cosine response
                norm = 1 - fresnel_unpolarised(0.0) if k in ("head", "cc") else 1.0
                t.set_text(f"{100 * hits[k] / norm / ideal:.0f}% of ideal")
            writer.grab_frame()

        for _ in range(FPS * 3):
            writer.grab_frame()

        # ---------------- part 2: cosine response sweep ----------------
        dots.set_offsets(np.empty((0, 2)))
        for t in counters.values():
            t.set_text("")
        title.set_text("Cosine response: one parallel beam, swept from straight on to grazing")
        subtitle.set_text("A detector that measures irradiance must follow the dashed cos θ curve. "
                          "Below it means steep light is under-counted.")
        ax.set_visible(False)
        pax = fig.add_axes((0.08, 0.12, 0.62, 0.72))
        bax = fig.add_axes((0.74, 0.30, 0.24, 0.40))
        bax.set_xlim(-1.2, 1.2)
        bax.set_ylim(-0.4, 1.6)
        bax.set_aspect("equal")
        bax.axis("off")
        bax.plot([-1, 1], [1.1, 1.1], color="#555", lw=6)
        bax.text(0, 1.25, "detector face, looking down", ha="center", va="bottom", fontsize=8)
        angle_text = bax.text(0, -0.35, "", ha="center", fontsize=12, weight="bold")
        pax.set_xlim(0, 90)
        pax.set_ylim(0, 1.08)
        pax.set_xlabel("beam angle from vertical (°)")
        pax.set_ylabel("response (1 = straight on)")
        thetas_all = np.radians(np.linspace(0, 89, 180))
        pax.plot(np.degrees(thetas_all), np.cos(thetas_all), color="#3b7dd8", ls="--", lw=2, label="ideal: cos θ")
        lines = {}
        for det in DETECTORS[1:]:
            lines[det["key"]], = pax.plot([], [], color=det["color"], lw=2.2, label=det["label"].split("\n")[0])
        pax.legend(fontsize=8.5, loc="upper right", frameon=False)
        for side in ("top", "right"):
            pax.spines[side].set_visible(False)
        beam_lines = []
        n_sweep = 260
        for i in range(n_sweep):
            th = thetas_all[int(i / n_sweep * (len(thetas_all) - 1))]
            for bl in beam_lines:
                bl.remove()
            beam_lines = []
            for off in np.linspace(-0.6, 0.6, 5):
                x1, y1 = off, 1.1
                x0, y0 = x1 - 1.2 * np.sin(th), y1 - 1.2 * np.cos(th)
                bl, = bax.plot([x0, x1], [y0, y1], color=(0.95, 0.7, 0.15), lw=1.5)
                beam_lines.append(bl)
            angle_text.set_text(f"beam from below at {np.degrees(th):.0f}°")
            upto = thetas_all[thetas_all <= th + 1e-9]
            for key, ln in lines.items():
                ln.set_data(np.degrees(upto), response_curve(key, upto))
            writer.grab_frame()
        title.set_text("Why the power meter is only approximately irradiance")
        subtitle.set_text("A recessed, windowed photodiode under-counts steep light; a cosine corrector "
                          "follows cos θ closely; a bare fiber sees only a narrow cone.")
        for _ in range(FPS * 5):
            writer.grab_frame()
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
