#!/usr/bin/env python3
"""
A gentle animation of the cosine rule and what a cosine corrector does.

Scene 1, "the footprint": a beam of light with a fixed width tilts away from
straight on. The patch it lights on a flat surface gets longer, so each
centimetre of the surface gets less of it. The fraction left is called
cos(angle): 1 straight on, 0.87 at 30°, 0.5 at 60°, 0 at 90°.

Scene 2, "a flat surface does this automatically": a flat sensor under an
even, wide beam collects cos(angle) as much light as straight on. That is what
irradiance means: light per area of a flat surface.

Scene 3, "real sensors": side by side under the same tilting beam.
- A photodiode recessed behind a rim and a window: steep light is shaded by
  the rim and bounced off the window, so it collects LESS than cos(angle).
- A cosine corrector: a white diffusing disc that sticks out slightly. Light
  from any angle enters it (steep light through its rim too) and is scrambled
  inside, so what reaches the detector follows cos(angle) closely.
A plot builds up each sensor's response as the beam tilts.

The geometry is a 2D schematic chosen to show the idea, not a model of a
specific product.

Usage:
    pixi run animate-cosine-rule [--out cosine_rule.mp4]
"""

import argparse
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.animation import FFMpegWriter, PillowWriter  # noqa: E402
import numpy as np  # noqa: E402

from animate_cosine_response import fresnel_unpolarised  # noqa: E402

FPS = 20
BEAM = "#f2b632"
SURFACE = "#4a4a4a"
IDEAL = "#3b7dd8"
HEAD = "#d9822b"
CC = "#2e9a58"
TABLE = [(0, 1.00), (30, 0.87), (45, 0.71), (60, 0.50), (75, 0.26), (90, 0.00)]


def eased(n_frames, start, stop):
    """Angles (degrees) going start -> stop -> start smoothly over n_frames."""
    t = np.linspace(0, 1, n_frames)
    s = 0.5 - 0.5 * np.cos(2 * np.pi * t)
    return start + (stop - start) * s


def head_response(th):
    w, recess = 1.0, 0.12
    c = np.cos(th)
    overlap = np.clip(w - recess * np.tan(th), 0, None) / w
    return c * overlap * (1 - fresnel_unpolarised(th)) / (1 - fresnel_unpolarised(0.0))


def cc_response(th):
    protrude, w = 0.035, 1.0
    bottom = np.cos(th) * (1 - fresnel_unpolarised(th))
    side = (protrude / w) * np.sin(th) * (1 - fresnel_unpolarised(np.pi / 2 - th))
    return (bottom + side) / (1 - fresnel_unpolarised(0.0))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, default=Path("cosine_rule.mp4"))
    args = parser.parse_args()

    fig = plt.figure(figsize=(12.8, 7.2), dpi=100)
    title = fig.text(0.03, 0.94, "", fontsize=18, weight="bold")
    subtitle = fig.text(0.03, 0.895, "", fontsize=12, color="#444")
    fig.text(0.03, 0.02, "2D schematic to show the idea; not a model of a specific product.",
             fontsize=8.5, color="#777")
    writer = FFMpegWriter(fps=FPS, bitrate=3000) if args.out.suffix == ".mp4" else PillowWriter(fps=FPS)

    with writer.saving(fig, str(args.out), dpi=100):
        # ---------------- scene 1: the footprint ----------------
        ax = fig.add_axes((0.03, 0.08, 0.62, 0.78))
        ax.set_xlim(-4, 6)
        ax.set_ylim(-1.2, 5.5)
        ax.set_aspect("equal")
        ax.axis("off")
        tab = fig.add_axes((0.68, 0.18, 0.29, 0.62))
        tab.axis("off")
        title.set_text("1. Tilt a beam, and the same light spreads over a longer patch")
        subtitle.set_text("Like the sun: overhead at noon it heats the ground strongly; low in the evening "
                          "the same sunlight is spread thin.")
        surf_y = 0.0
        width = 1.6
        artists = []
        angles = list(np.full(30, 0.0)) + list(eased(220, 0, 75)) + list(eased(140, 0, 60)[:70]) + \
            list(np.full(50, 60.0))
        for theta in angles:
            for a in artists:
                a.remove()
            artists = []
            th = np.radians(theta)
            artists.append(ax.plot([-4, 6], [surf_y, surf_y], color=SURFACE, lw=4)[0])
            # vertical reference and angle arc
            artists.append(ax.plot([0, 0], [0, 3.8], color="#999", lw=1, ls="--")[0])
            arc_t = np.linspace(0, th, 30)
            artists.append(ax.plot(-1.6 * np.sin(arc_t), 1.6 * np.cos(arc_t), color="#555", lw=1.2)[0])
            artists.append(ax.text(-0.9 * np.sin(th / 2) - 0.35, 1.9, f"θ = {theta:.0f}°", fontsize=13,
                                   ha="right"))
            for k in np.linspace(-width / 2, width / 2, 9):
                hit_x = k / max(np.cos(th), 1e-3)
                d = np.array([np.sin(th), -np.cos(th)])
                end = np.array([hit_x, surf_y])
                start = end - d * 4.6
                artists.append(ax.annotate("", xy=end, xytext=start,
                                           arrowprops=dict(arrowstyle="-|>", color=BEAM, lw=1.6)))
            foot = width / max(np.cos(th), 1e-3)
            artists.append(ax.plot([-foot / 2, foot / 2], [surf_y - 0.18] * 2, color="#c0392b", lw=6,
                                   solid_capstyle="butt")[0])
            artists.append(ax.text(0, -0.75, f"lit patch: {foot / width:.2f}× as long as straight on",
                                   ha="center", fontsize=13, color="#c0392b"))
            artists.append(ax.text(0, 4.9, f"light per cm of surface: {100 * np.cos(th):.0f}% of straight on",
                                   ha="center", fontsize=14, weight="bold"))
            # table
            tab.clear()
            tab.axis("off")
            tab.text(0.0, 1.0, "angle θ", fontsize=13, weight="bold", va="top")
            tab.text(0.55, 1.0, "cos θ\n(light per cm)", fontsize=13, weight="bold", va="top")
            for i, (deg, c) in enumerate(TABLE):
                y = 0.78 - i * 0.12
                near = abs(theta - deg) < 4
                kw = dict(fontsize=13, weight="bold" if near else "normal", color="#c0392b" if near else "#222")
                tab.text(0.05, y, f"{deg}°", **kw)
                tab.text(0.6, y, f"{c:.2f}", **kw)
            tab.text(0.0, 0.0, "cos θ is just a name for\nthis shrink factor.", fontsize=11, color="#555",
                     va="bottom")
            writer.grab_frame()
        fig.clear()

        # ---------------- scene 2: flat surface counts cos ----------------
        title = fig.text(0.03, 0.94, "2. A flat surface collects light in proportion to cos θ, automatically",
                         fontsize=18, weight="bold")
        subtitle = fig.text(0.03, 0.895, "That is what irradiance means: light per area of a flat surface. "
                            "The fish's body and the dish floor work this way.", fontsize=12, color="#444")
        fig.text(0.03, 0.02, "2D schematic to show the idea; not a model of a specific product.",
                 fontsize=8.5, color="#777")
        ax = fig.add_axes((0.03, 0.08, 0.62, 0.78))
        ax.set_xlim(-4, 6)
        ax.set_ylim(-1.2, 5.5)
        ax.set_aspect("equal")
        ax.axis("off")
        pax = fig.add_axes((0.70, 0.22, 0.27, 0.5))
        pax.set_xlim(0, 90)
        pax.set_ylim(0, 1.05)
        pax.set_xlabel("angle θ (°)")
        pax.set_ylabel("light collected (1 = straight on)")
        for side in ("top", "right"):
            pax.spines[side].set_visible(False)
        line, = pax.plot([], [], color=IDEAL, lw=2.5, label="flat sensor = cos θ")
        pax.legend(fontsize=9, frameon=False, loc="upper right")
        artists = []
        seen = []
        for theta in list(np.linspace(0, 85, 200)) + [85.0] * 30:
            for a in artists:
                a.remove()
            artists = []
            th = np.radians(theta)
            artists.append(ax.plot([-4, 6], [0, 0], color="#bbb", lw=2)[0])
            artists.append(ax.plot([-0.6, 0.6], [0, 0], color=IDEAL, lw=7, solid_capstyle="butt")[0])
            artists.append(ax.text(0, -0.6, "flat sensor", ha="center", fontsize=12, color=IDEAL))
            spacing = 0.25
            hits = 0
            for k in np.arange(-3.5, 3.5 + 1e-9, spacing):
                hit_x = k / max(np.cos(th), 1e-3)
                if -4 < hit_x < 6:
                    d = np.array([np.sin(th), -np.cos(th)])
                    end = np.array([hit_x, 0.0])
                    start = end - d * 4.6
                    on = abs(hit_x) < 0.6
                    hits += on
                    artists.append(ax.plot([start[0], end[0]], [start[1], end[1]],
                                           color=BEAM if on else "#f3dfa6", lw=1.6 if on else 1)[0])
            artists.append(ax.text(0, 4.9, f"θ = {theta:.0f}°: rays landing on the sensor: {hits}",
                                   ha="center", fontsize=14, weight="bold"))
            seen.append((theta, np.cos(th)))
            line.set_data([s[0] for s in seen], [s[1] for s in seen])
            writer.grab_frame()
        fig.clear()

        # ---------------- scene 3: real sensors ----------------
        title = fig.text(0.03, 0.94, "3. Real sensors: a recessed photodiode vs a cosine corrector",
                         fontsize=18, weight="bold")
        subtitle = fig.text(0.03, 0.895, "A good irradiance sensor must follow the dashed cos θ curve. "
                            "Falling below it means steep light is missed.", fontsize=12, color="#444")
        fig.text(0.03, 0.02, "2D schematic to show the idea; not a model of a specific product.",
                 fontsize=8.5, color="#777")
        ax = fig.add_axes((0.02, 0.08, 0.60, 0.78))
        ax.set_xlim(-1, 9)
        ax.set_ylim(-1.2, 5.5)
        ax.set_aspect("equal")
        ax.axis("off")
        pax = fig.add_axes((0.67, 0.2, 0.30, 0.55))
        pax.set_xlim(0, 90)
        pax.set_ylim(0, 1.08)
        pax.set_xlabel("angle θ (°)")
        pax.set_ylabel("light collected (1 = straight on)")
        for side in ("top", "right"):
            pax.spines[side].set_visible(False)
        thetas_all = np.linspace(0, 85, 200)
        pax.plot(thetas_all, np.cos(np.radians(thetas_all)), color=IDEAL, ls="--", lw=2, label="ideal: cos θ")
        lh, = pax.plot([], [], color=HEAD, lw=2.5, label="recessed photodiode")
        lc, = pax.plot([], [], color=CC, lw=2.5, label="cosine corrector")
        pax.legend(fontsize=9, frameon=False, loc="upper right")

        hx, cx, sy = 1.6, 6.2, 1.2   # sensor centres and face height (sensors face up here, light from above)
        rng = np.random.default_rng(5)
        artists = []
        seen = []
        for theta in list(np.linspace(0, 85, 260)) + [85.0] * 40:
            for a in artists:
                a.remove()
            artists = []
            th = np.radians(theta)
            # photodiode head: housing with a recess; window at the top of the recess
            artists.append(ax.add_patch(plt.Rectangle((hx - 1.0, sy - 0.9), 2.0, 0.9 + 0.35, color="#555")))
            artists.append(ax.add_patch(plt.Rectangle((hx - 0.5, sy), 1.0, 0.35, color="white")))
            artists.append(ax.plot([hx - 0.5, hx + 0.5], [sy, sy], color=HEAD, lw=6, solid_capstyle="butt")[0])
            artists.append(ax.plot([hx - 0.5, hx + 0.5], [sy + 0.35, sy + 0.35], color="#9fc5e8", lw=2)[0])
            artists.append(ax.text(hx, sy - 1.4, "recessed photodiode\n(window + rim)", ha="center",
                                   fontsize=11, color=HEAD))
            # cosine corrector: housing with a diffusing disc sticking out above
            artists.append(ax.add_patch(plt.Rectangle((cx - 1.0, sy - 0.9), 2.0, 0.9, color="#555")))
            artists.append(ax.add_patch(plt.Rectangle((cx - 0.5, sy - 0.1), 1.0, 0.45,
                                                      facecolor="#f4f4f4", edgecolor=CC, lw=2)))
            artists.append(ax.text(cx, sy - 1.4, "cosine corrector\n(white diffusing disc)", ha="center",
                                   fontsize=11, color=CC))
            # beam rays
            d = np.array([np.sin(th), -np.cos(th)])
            for centre, kind in ((hx, "head"), (cx, "cc")):
                for k in np.linspace(-0.45, 0.45, 7):
                    top_y = sy + 0.35
                    hit_x = centre + k
                    end = np.array([hit_x, top_y])
                    start = end - d * 3.6
                    blocked = False
                    if kind == "head":
                        # reaches the photodiode face only if it stays inside the recess
                        x_face = hit_x + 0.35 * np.tan(th)
                        blocked = abs(x_face - centre) >= 0.5 or rng.uniform() < fresnel_unpolarised(th)
                        col = "#bbb" if blocked else BEAM
                        artists.append(ax.plot([start[0], end[0]], [start[1], end[1]], color=col, lw=1.4)[0])
                        if not blocked:
                            artists.append(ax.plot([end[0], x_face], [top_y, sy], color=BEAM, lw=1.4)[0])
                    else:
                        artists.append(ax.plot([start[0], end[0]], [start[1], end[1]], color=BEAM, lw=1.4)[0])
                        # scrambled inside the diffuser: a zig-zag that forgets the arrival direction and ends
                        # on the detector under the disc (same path every frame, so it doesn't flicker)
                        walk_rng = np.random.default_rng(int(round((k + 1) * 1000)))
                        p = end.copy()
                        bottom = sy - 0.08
                        for step in range(4):
                            last = step == 3
                            q = np.array([np.clip(p[0] + walk_rng.normal(0, 0.18), centre - 0.46, centre + 0.46),
                                          bottom if last else walk_rng.uniform(bottom + 0.05, sy + 0.3)])
                            artists.append(ax.plot([p[0], q[0]], [p[1], q[1]], color=BEAM, lw=0.9, alpha=0.8)[0])
                            p = q
            artists.append(ax.plot([cx - 0.5, cx + 0.5], [sy - 0.1, sy - 0.1], color=CC, lw=5,
                                   solid_capstyle="butt")[0])
            artists.append(ax.text(cx, sy - 2.15, "inside the disc the light is scrambled, so the detector\n"
                                   "below gets a share that doesn't depend on the arrival angle",
                                   ha="center", va="top", fontsize=9, color=CC))
            artists.append(ax.text(4.0, 4.9, f"beam at θ = {theta:.0f}°", ha="center", fontsize=14,
                                   weight="bold"))
            artists.append(ax.text(hx, 4.2, f"{100 * head_response(th) / max(np.cos(th), 1e-3):.0f}% of ideal",
                                   ha="center", fontsize=12, color=HEAD, weight="bold"))
            artists.append(ax.text(cx, 4.2, f"{100 * cc_response(th) / max(np.cos(th), 1e-3):.0f}% of ideal",
                                   ha="center", fontsize=12, color=CC, weight="bold"))
            seen.append(theta)
            ts = np.radians(np.array(seen))
            lh.set_data(seen, head_response(ts))
            lc.set_data(seen, cc_response(ts))
            writer.grab_frame()
        fig.clear()

        # ---------------- wrap-up ----------------
        fig.text(0.06, 0.78, "Why this matters for the rig", fontsize=22, weight="bold")
        lines = [
            "• The diffusing film sends light up at every angle, not just straight up.",
            "• A fish's body and the dish floor collect that light in proportion to cos θ, as in scene 2.",
            "• A recessed photodiode head misses some of the steep light, so it reads a little low.",
            "• A cosine corrector is shaped to follow cos θ, so it reads what a flat surface really gets:",
            "   irradiance. That makes readings far less sensitive to small tilts and to where the light comes from.",
            "• A bare fiber sees only a narrow cone (about ±13°), so it measures brightness from one direction.",
        ]
        for i, t in enumerate(lines):
            fig.text(0.06, 0.66 - i * 0.075, t, fontsize=14)
        for _ in range(FPS * 8):
            writer.grab_frame()
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
