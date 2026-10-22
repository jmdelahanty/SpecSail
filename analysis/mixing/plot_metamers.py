"""Figure: projector gamut vs 488/561/631 nm targets in larval cone space.

Usage: python analysis/mixing/plot_metamers.py OUT_PNG RESULTS_DIR...

One RESULTS_DIR must end in out_larval (panel A uses its arena 3 matches).
Reads the runs from mixing_model.RUNS (~/SpecSail/runs).
"""

import json
import os
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mixing_model as mm  # noqa: E402

SURFACE, INK, INK2, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#dddcd7"
SERIES = {488.0: "#2a78d6", 561.0: "#1baf7a", 631.0: "#eb6834"}
ARENAS = ("arena_1", "arena_2", "arena_3", "arena_4")
LARVAL = [360.0, 416.0, 470.0, 550.0]


def xy(c):
    """Maxwell triangle: S bottom-left, L bottom-right, M top."""
    c = np.asarray(c)
    return c[..., 1] * 0.5 + c[..., 2] * 1.0, c[..., 1] * np.sqrt(3) / 2


def main():
    out_png, result_dirs = sys.argv[1], sys.argv[2:]
    plt.rcParams.update({"font.size": 10, "text.color": INK, "axes.labelcolor": INK2,
                         "xtick.color": INK2, "ytick.color": INK2, "font.family": "DejaVu Sans"})
    fig, (ax, bx) = plt.subplots(1, 2, figsize=(12.5, 5.6), gridspec_kw={"width_ratios": [1.15, 1]},
                                 facecolor=SURFACE)
    for a in (ax, bx):
        a.set_facecolor(SURFACE)

    # Panel A: cone-space chromaticity, larval cones, all four arenas.
    tri = np.array([[0, 0], [0.5, np.sqrt(3) / 2], [1, 0], [0, 0]])
    ax.plot(tri[:, 0], tri[:, 1], color=GRID, lw=1)
    for (x, y), name, ha, va in [((-0.01, -0.01), "S", "right", "top"),
                                 ((0.5, np.sqrt(3) / 2 + 0.01), "M", "center", "bottom"),
                                 ((1.03, -0.01), "L", "left", "top")]:
        ax.text(x, y, name, ha=ha, va=va, color=INK2, fontsize=11)
    first = None
    for lab in ARENAS:
        run, pos = {p[0]: (p[1], p[2]) for p in mm.POSITIONS}[lab]
        wl, patches = mm.load_position(run, pos)
        model = mm.build_model(wl, patches)
        cones = [mm.govardovskii_a1(wl, l) for l in LARVAL]
        qw = mm.catches(wl, mm.predict(model, (255, 255, 255)), cones)
        prim = [mm.chroma(mm.catches(wl, model["primaries"][ch], cones), qw) for ch in mm.CHANNELS]
        g = np.array(prim + [prim[0]])
        gx, gy = xy(g)
        ax.plot(gx, gy, color=INK2, lw=1, alpha=0.8, label="Projector gamut, 4 arenas" if first is None else None)
        if first is None:
            first = (wl, cones, qw)
            for c, ch in zip(prim, mm.CHANNELS):
                px, py = xy(c)
                ax.annotate(ch, (px, py), textcoords="offset points", xytext={"red": (8, -2), "green": (4, 8), "blue": (-6, 8)}[ch], ha="right" if ch == "blue" else "left",
                            color=INK2, fontsize=9)
    wl, cones, qw = first
    lam = np.arange(420.0, 681.0, 2.0)
    locus = np.array([mm.chroma(np.array([np.interp(l, wl, c) for c in cones]), qw) for l in lam])
    lx, ly = xy(locus)
    ax.plot(lx, ly, color=INK, lw=1.25, label="Monochromatic light, 420-680 nm")
    for tick in (440, 460, 500, 520, 540):
        c = locus[np.argmin(np.abs(lam - tick))]
        tx, ty = xy(c)
        ax.plot(tx, ty, "o", ms=3, color=INK)
        ax.annotate(f"{tick}", (tx, ty), textcoords="offset points", xytext=(5, 3), fontsize=8, color=INK2)
    wx, wy = xy(np.array([1 / 3, 1 / 3, 1 / 3]))
    ax.plot(wx, wy, "s", ms=6, mfc=SURFACE, mec=INK, mew=1.25)
    ax.annotate("projector white", (wx, wy), textcoords="offset points", xytext=(-30, -42), fontsize=9,
                color=INK2, ha="right", arrowprops=dict(arrowstyle="-", color=INK2, lw=0.8))

    larval = json.load(open(os.path.join([d for d in result_dirs if d.rstrip("/").endswith("out_larval")][0],
                                         "results.json")))
    for nm, colour in SERIES.items():
        m = [mt for mt in larval["positions"]["arena_3"]["metamers"] if mt["target_nm"] == nm][0]
        tx, ty = xy(m["target_chroma_S_M_L"])
        mx, my = xy(m["match_chroma_S_M_L"])
        ax.annotate("", (mx, my), (tx, ty), arrowprops=dict(arrowstyle="-", color=colour, lw=1.25, ls=(0, (3, 2))))
        ax.plot(tx, ty, "o", ms=9, color=colour, mec=SURFACE, mew=2, zorder=5)
        ax.plot(mx, my, "o", ms=9, mfc=SURFACE, mec=colour, mew=2, zorder=5)
        rgb = m["best_rgb_code"]
        ax.annotate(f"{nm:.0f} nm target", (tx, ty), textcoords="offset points",
                    xytext=(8, 6) if nm != 488.0 else (8, 4), fontsize=9, color=INK, fontweight="bold")
        offset, ha = {488.0: ((-12, 18), "right"), 561.0: ((12, -4), "left"), 631.0: ((-12, -14), "right")}[nm]
        ax.annotate(f"best mix RGB {rgb[0]},{rgb[1]},{rgb[2]}", (mx, my), textcoords="offset points",
                    xytext=offset, ha=ha, fontsize=8.5, color=INK2)
    ax.plot([], [], "o", ms=8, color=INK2, label="Target (filled)  /  best mix (open)")
    ax.set_aspect("equal")
    ax.set_xlim(-0.08, 1.08)
    ax.set_ylim(-0.08, 0.95)
    ax.axis("off")
    ax.set_title("No RGB mix reaches 488, 561 or 631 nm in larval cone space", loc="left", fontsize=12,
                 fontweight="bold", color=INK)
    ax.legend(loc="upper right", bbox_to_anchor=(1.04, 1.0), frameon=False, fontsize=8.5, labelcolor=INK2)
    ax.text(0, -0.11, "S, M, L catches normalised to projector white. Larval cones UV 360, S 416,\n"
            "M 470, L 550 nm (A1 templates). Arena 3 matches shown.", fontsize=7.5, color=INK2)

    # Panel B: how far from white toward each target the best mix gets, per cone set.
    sets = []
    for d in result_dirs:
        r = json.load(open(os.path.join(d, "results.json")))
        sets.append((r["lmax_UV_S_M_L"], r))
    sets.sort(key=lambda s: (s[0] != LARVAL, s[0]))
    rows = []
    for nm in SERIES:
        for lmax, r in sets:
            vals = [[mt for mt in r["positions"][a]["metamers"] if mt["target_nm"] == nm][0]
                    ["progress_white_to_target"] for a in ARENAS]
            rows.append((nm, lmax, np.mean(vals), min(vals), max(vals)))
    ylabels, y = [], 0
    for nm, colour in SERIES.items():
        bx.text(0.005, y + 0.15, f"{nm:.0f} nm target", ha="left", va="bottom", fontsize=10,
                fontweight="bold", color=colour)
        y -= 0.85
        for (n, lmax, mean, lo, hi) in [row for row in rows if row[0] == nm]:
            default = lmax == LARVAL
            bx.plot([lo, hi], [y, y], color=colour if default else INK2, lw=2 if default else 1)
            bx.plot(mean, y, "o", ms=8 if default else 6, color=colour if default else SURFACE,
                    mec=colour if default else INK2, mew=1.5)
            ylabels.append(("larval (default)" if default else f"M {lmax[2]:.0f}, L {lmax[3]:.0f}"
                            + (" (adult)" if lmax[3] == 564.0 else ""), y))
            y -= 1
        y -= 1.4
    bx.set_yticks([p for _, p in ylabels])
    bx.set_yticklabels([t for t, _ in ylabels], fontsize=8.5)
    bx.tick_params(axis="y", length=0, pad=4)
    bx.set_xlim(0, 1)
    bx.set_ylim(y + 0.6, 1.4)
    bx.set_xlabel("Fraction of the way from white to the target (1 = exact match)")
    bx.grid(axis="x", color=GRID, lw=0.8)
    bx.set_axisbelow(True)
    for s in ("top", "right", "left"):
        bx.spines[s].set_visible(False)
    bx.spines["bottom"].set_color(GRID)
    bx.set_title("488 nm stays near white for every cone set", loc="left", fontsize=12, fontweight="bold",
                 color=INK)
    bx.text(0, -0.15, "Dot = mean of 4 arenas; line = range across arenas.", fontsize=7.5, color=INK2,
            transform=bx.transAxes)
    bx.yaxis.set_label_position("left")
    plt.subplots_adjust(left=0.02, right=0.97, wspace=0.32, top=0.9, bottom=0.14)
    # Leave room for the bold target names left of the tick labels.
    fig.savefig(out_png, dpi=150, facecolor=SURFACE)


if __name__ == "__main__":
    main()
