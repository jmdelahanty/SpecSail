"""Linear mixing model prototype for the omnifin0 projector (BenQ X3100i).

Reads SpecSail calibration run bundles (read-only), builds a per-arena
primary-ramp model, checks additivity against measured white and grays,
and searches cone-catch metamers for monochromatic targets.

Usage: python analysis/mixing/mixing_model.py OUT_DIR [--lmax UV,S,M,L] [--runs DIR]

The run bundles are not in git (runs/ is ignored), so --runs defaults to the
main checkout's ~/SpecSail/runs.
"""

import argparse
import csv
import glob
import json
import os
import sys

import numpy as np

RUNS = os.path.expanduser("~/SpecSail/runs")  # overridden by --runs
RUN_A = "omnifin0_shadow_projection_surface_v1_ir_off_baseline_20261020_20261020T153525Z"
RUN_B = "omnifin0_shadow_projection_surface_v1_ir_off_baseline_20261020_arenas_2b_3_4_20261020T194700Z"
# (label, run, position). Arena 3 of RUN_A is invalid (drift after a box move).
POSITIONS = [
    ("arena_1", RUN_A, "arena_1_center"),
    ("arena_2", RUN_A, "arena_2_center"),
    ("arena_2_bridge", RUN_B, "arena_2_center_bridge"),
    ("arena_3", RUN_B, "arena_3_center"),
    ("arena_4", RUN_B, "arena_4_center"),
]
CHANNELS = ("red", "green", "blue")
TARGETS_NM = (488.0, 561.0, 631.0)
DARK_PIXELS = list(range(0, 4)) + list(range(1040, 1044))
OFFSET_BAND_NM = (720.0, 800.0)
VIS_BAND_NM = (380.0, 720.0)
LIT_FRACTION = 1e-3  # ramp steps below this fraction of full drive are treated as unlit
CHROMA_FLOOR = 0.01  # floor on white-normalised chromaticity in the match error (assumption)


def load_spectrum(mdir):
    """Mean of scans, dark-pixel subtracted, per second, flat offset removed."""
    meta = json.load(open(os.path.join(mdir, "measurement.json")))
    with open(os.path.join(mdir, "spectrum.csv")) as f:
        rows = list(csv.reader(f))
    header, body = rows[0], np.array(rows[1:], dtype=float)
    wl = body[:, 1]
    scans = body[:, [i for i, h in enumerate(header) if h.startswith("counts_scan_")]]
    counts = scans.mean(axis=1)
    counts = counts - counts[DARK_PIXELS].mean()
    counts = counts / (meta["integration_us"] * 1e-6)
    band = (wl >= OFFSET_BAND_NM[0]) & (wl <= OFFSET_BAND_NM[1])
    counts = counts - np.median(counts[band])
    return wl, counts, meta


def load_position(run, position):
    out = {}
    wl = None
    for mdir in sorted(glob.glob(os.path.join(RUNS, run, "measurements", position + "__*"))):
        patch = os.path.basename(mdir)[len(position) + 2:].rsplit("__", 1)[0]
        w, s, meta = load_spectrum(mdir)
        if wl is None:
            wl = w
        if meta.get("any_saturated"):
            print(f"warning: {position} {patch} saturated", file=sys.stderr)
        # The black patch has two integration times in RUN_A; keep the longest.
        key = "black" if patch.startswith("black") else patch
        if key in out and key == "black" and out[key][1] >= meta["integration_us"]:
            continue
        out[key] = (s, meta["integration_us"], meta.get("rgba8"))
    return wl, out


def govardovskii_a1(wl, lmax):
    """Govardovskii et al. 2000 A1 template, alpha + beta bands, peak 1."""
    x = lmax / wl
    a = 0.8795 + 0.0459 * np.exp(-((lmax - 300.0) ** 2) / 11940.0)
    alpha = 1.0 / (np.exp(69.7 * (a - x)) + np.exp(28.0 * (0.922 - x))
                   + np.exp(-14.9 * (1.104 - x)) + 0.674)
    lb = 189.0 + 0.315 * lmax
    bb = -40.5 + 0.195 * lmax
    beta = 0.26 * np.exp(-(((wl - lb) / bb) ** 2))
    return alpha + beta


def ramp(model_patches, channel):
    codes, spectra = [], []
    for key, (s, _, _) in model_patches.items():
        if key.startswith(channel + "_"):
            codes.append(int(key.split("_")[1]))
            spectra.append(s)
    order = np.argsort(codes)
    return np.array(codes)[order], np.array(spectra)[order]


def build_model(wl, patches):
    black = patches["black"][0]
    vis = (wl >= VIS_BAND_NM[0]) & (wl <= VIS_BAND_NM[1])
    model = {"black": black, "primaries": {}, "transfer": {}, "shape": {}, "fit": {}}
    for ch in CHANNELS:
        codes, spectra = ramp(patches, ch)
        excess = spectra - black
        full = excess[codes == 255][0]
        # Transfer = least-squares scale of each ramp step onto the full primary.
        t = (excess[:, vis] @ full[vis]) / (full[vis] @ full[vis])
        # Shape constancy: residual after scaling, as a fraction of the scaled step.
        resid = []
        for k, c in enumerate(codes):
            pred = t[k] * full
            denom = np.abs(pred[vis]).sum()
            resid.append(float(np.abs(excess[k, vis] - pred[vis]).sum() / denom) if denom > 0 else None)
        model["primaries"][ch] = full
        model["transfer"][ch] = (np.concatenate([[0], codes]), np.concatenate([[0.0], t]))
        model["shape"][ch] = dict(zip([int(c) for c in codes], resid))
        model["fit"][ch] = fit_transfer(codes, t)
    return model


def transfer_at(model, ch, code):
    """Drive fraction at a code from the fitted offset power law.

    The ramp is sampled only at 64 and 96 across the steep toe, where linear
    interpolation overshoots the protocol grays 77 and 89 and log-linear
    interpolation undershoots them; a fitted curve sits between.
    """
    c0, gamma = model["fit"][ch]
    x = np.clip((np.asarray(code, dtype=float) - c0) / (255.0 - c0), 0.0, 1.0)
    return x ** gamma


def fit_transfer(codes, t):
    """Fit t = ((c - c0) / (255 - c0)) ** gamma on the lit ramp steps (log residuals)."""
    lit = t > LIT_FRACTION
    c, lt = codes[lit].astype(float), np.log(t[lit])
    best = None
    for c0 in np.arange(0.0, c.min() - 0.5, 0.25):
        lx = np.log((c - c0) / (255.0 - c0))
        gamma = float(lx @ lt / (lx @ lx))
        rss = float(((gamma * lx - lt) ** 2).sum())
        if best is None or rss < best[2]:
            best = (float(c0), gamma, rss)
    return best[0], best[1]


def predict(model, rgb):
    s = model["black"].copy()
    for ch, code in zip(CHANNELS, rgb):
        s = s + float(transfer_at(model, ch, code)) * model["primaries"][ch]
    return s


def code_for(model, ch, t_wanted):
    fine = np.arange(256)
    tf = transfer_at(model, ch, fine)
    return int(fine[np.argmin(np.abs(tf - t_wanted))])


def additivity(wl, model, patches):
    vis = (wl >= VIS_BAND_NM[0]) & (wl <= VIS_BAND_NM[1])
    checks = {"white_255": (255, 255, 255)}
    for key in patches:
        if key.startswith("gray_"):
            c = int(key.split("_")[1])
            checks[key] = (c, c, c)
    out = {}
    for key, rgb in checks.items():
        meas = patches[key][0]
        pred = predict(model, rgb)
        total_err = float(pred[vis].sum() / meas[vis].sum() - 1.0)
        shape_err = float(np.abs(pred[vis] / pred[vis].sum() - meas[vis] / meas[vis].sum()).sum() / 2)
        out[key] = {"integrated_error": total_err, "shape_distance": shape_err}
    return out


def catches(wl, spectrum, cones):
    dl = np.gradient(wl)
    return np.array([np.sum(spectrum * c * dl) for c in cones])


def chroma(q, q_white):
    """S, M, L chromaticity after normalising each cone to its catch of projector white."""
    n = q[..., 1:4] / q_white[1:4]
    return n / n.sum(axis=-1, keepdims=True)


def chroma_error(c, c_target):
    """Spread of log chromaticity ratios across S, M, L, with a floor (CHROMA_FLOOR)."""
    lr = np.log((c + CHROMA_FLOOR) / (c_target + CHROMA_FLOOR))
    return lr.std(axis=-1)


def metamer(wl, model, cones, target_nm):
    """Search drive codes whose S, M, L chromaticity best matches a monochromatic target.

    Three primaries cannot match four cones; UV is reported, not fitted.
    """
    target = np.array([np.interp(target_nm, wl, c) for c in cones])
    q0 = catches(wl, model["black"], cones)
    qx = np.array([catches(wl, model["primaries"][ch], cones) for ch in CHANNELS])  # 3 x 4
    q_white = catches(wl, predict(model, (255, 255, 255)), cones)
    c_target = chroma(target, q_white)
    c_white = chroma(q_white, q_white)
    # Exact S, M, L match needs all primary weights positive.
    w = np.linalg.solve(qx[:, 1:4].T, target[1:4])
    g = np.linspace(0.0, 1.0, 101)
    tr, tg, tb = np.meshgrid(g, g, g, indexing="ij")
    T = np.stack([tr.ravel(), tg.ravel(), tb.ravel()], axis=1)
    q = q0[None, :] + T @ qx
    err = chroma_error(chroma(q, q_white), c_target)
    best_err = err.min()
    near = err <= best_err + 0.01
    lum = q[:, 2] + q[:, 3]
    idx = int(np.argmax(np.where(near, lum, -np.inf)))
    t_best = T[idx]
    rgb = [code_for(model, ch, t) for ch, t in zip(CHANNELS, t_best)]
    q_rgb = catches(wl, predict(model, rgb), cones)
    c_rgb = chroma(q_rgb, q_white)
    # How far from white toward the target the match gets (1 = all the way).
    d_wt = np.linalg.norm(c_target - c_white)
    progress = float(np.dot(c_rgb - c_white, c_target - c_white) / d_wt ** 2)
    return {
        "target_nm": target_nm,
        "exact_sml_match_possible": bool(np.all(w > 0)),
        "primary_weights_for_exact_match": (w / np.abs(w).max()).tolist(),
        "target_chroma_S_M_L": c_target.tolist(),
        "match_chroma_S_M_L": c_rgb.tolist(),
        "white_chroma_S_M_L": c_white.tolist(),
        "match_error": float(chroma_error(c_rgb, c_target)),
        "white_error": float(chroma_error(c_white, c_target)),
        "progress_white_to_target": progress,
        "best_drive_fraction": t_best.tolist(),
        "best_rgb_code": rgb,
        "uv_catch_match_over_target": float((q_rgb[0] / q_rgb[1:4].sum()) / (target[0] / target[1:4].sum())),
        "uv_share_of_catch_target": float(target[0] / target.sum()),
        "uv_share_of_catch_match": float(q_rgb[0] / q_rgb.sum()),
        "match_ML_fraction_of_white": float((q_rgb[2] + q_rgb[3]) / (q_white[2] + q_white[3])),
    }


def main():
    global RUNS
    ap = argparse.ArgumentParser()
    ap.add_argument("out_dir")
    ap.add_argument("--lmax", default="360,416,470,550")  # larval set, zebrafish-vision-teacher 2026-10-20
    ap.add_argument("--runs", default=RUNS)
    args = ap.parse_args()
    RUNS = os.path.expanduser(args.runs)
    lmax = [float(v) for v in args.lmax.split(",")]
    os.makedirs(args.out_dir, exist_ok=True)

    results = {"lmax_UV_S_M_L": lmax, "runs": [RUN_A, RUN_B], "positions": {}}
    plot_data = {}
    for label, run, pos in POSITIONS:
        wl, patches = load_position(run, pos)
        model = build_model(wl, patches)
        cones = [govardovskii_a1(wl, l) for l in lmax]
        vis = (wl >= VIS_BAND_NM[0]) & (wl <= VIS_BAND_NM[1])
        peaks = {ch: float(wl[vis][np.argmax(model["primaries"][ch][vis])]) for ch in CHANNELS}
        white = patches["white_255"][0]
        r = {
            "primary_peak_nm": peaks,
            "black_fraction_of_white": float(model["black"][vis].sum() / white[vis].sum()),
            "primary_fraction_of_white": {ch: float(model["primaries"][ch][vis].sum() / white[vis].sum())
                                          for ch in CHANNELS},
            "transfer": {ch: dict(zip([int(c) for c in model["transfer"][ch][0]],
                                      [float(v) for v in model["transfer"][ch][1]])) for ch in CHANNELS},
            "transfer_fit": {ch: {"c0": model["fit"][ch][0], "gamma": model["fit"][ch][1]} for ch in CHANNELS},
            "shape_residual": model["shape"],
            "additivity": additivity(wl, model, patches),
            "metamers": [metamer(wl, model, cones, nm) for nm in TARGETS_NM],
        }
        results["positions"][label] = r
        plot_data[label] = (wl, model, patches, cones)

    with open(os.path.join(args.out_dir, "results.json"), "w") as f:
        json.dump(results, f, indent=2)
    np.savez(os.path.join(args.out_dir, "primaries.npz"),
             **{f"{lab}_{ch}": d[1]["primaries"][ch] for lab, d in plot_data.items() for ch in CHANNELS},
             wl=plot_data["arena_1"][0])
    print(json.dumps(results, indent=1)[:200])


if __name__ == "__main__":
    main()
