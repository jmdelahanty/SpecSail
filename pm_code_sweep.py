"""
Analyse a power-meter drive-code sweep: per-channel code-to-light curves, where black ends, and
an offset-gamma fit.

A sweep session (power_session.py with a code-sweep plan) shows every code 0-255 for gray
(``white_NNN``), red, green and blue in shuffled order, with ``refNN_white_255`` and
``refNN_black`` references every 32 fills and ``white_drift_start``/``_end`` around them.

Steps:
1. Black: interpolate the black references in measurement order and subtract.
2. Drift: interpolate the white references (black-subtracted) and scale each fill to their mean.
3. ``--own-black``: re-zero each channel on the median of its own codes 0-40. The references are
   read at the drift fill's meter wavelength (550 nm), while red, green and blue fills are read
   at their own (629/530/456 nm), so the same black light reads differently per channel. Only
   valid when codes 0-40 are known to be dark (check the flat codes 0-40 slope it prints).
4. Fit P/P255 = ((code - c0)/(255 - c0))^gamma above the first steadily lit code + 8, by grid
   search (c0 step 0.1, gamma step 0.005).

Usage:
    python pm_code_sweep.py <session_dir> [--own-black] [--plot out.png]

Outputs go to stdout and the optional plot; the session directory is only read.
"""

import argparse
import csv
import sys
from pathlib import Path

import numpy as np

CHANNELS = ("white", "red", "green", "blue")


def load_sweep(session_dir, own_black=False):
    """
    Read a sealed sweep session.

    Returns
    -------
    dict
        ``refs``: black/white reference stats; ``curves``: {channel: (codes, net_W)} with net
        power black-subtracted and drift-corrected (and re-zeroed per channel if ``own_black``).
    """
    with open(Path(session_dir) / "power_meter" / "summary.csv", newline="") as f:
        rows = list(csv.DictReader(f))
    W = np.array([float(r["mean_W"]) for r in rows])
    S = np.array([float(r["std_W"]) for r in rows])
    fid = [r["fill_id"] for r in rows]
    idx = np.arange(len(rows))
    bi = np.array([i for i, f in enumerate(fid) if f.endswith("_black")])
    wi = np.array(sorted([i for i, f in enumerate(fid) if f.startswith("ref") and f.endswith("white_255")]
                         + [fid.index("white_drift_start"), fid.index("white_drift_end")]))
    black = np.interp(idx, bi, W[bi])
    white = np.interp(idx, wi, W[wi] - black[wi])
    wref = W[wi] - black[wi]
    w0 = wref.mean()
    curves = {}
    for ch in CHANNELS:
        pts = sorted((int(f.split("_")[1]), i) for i, f in enumerate(fid)
                     if f.startswith(ch + "_") and f.split("_")[1].isdigit())
        codes = np.array([c for c, _ in pts])
        ii = np.array([i for _, i in pts])
        net = (W[ii] - black[ii]) * (w0 / white[ii])
        if own_black:
            lo = net[codes <= 40]
            # Only valid when codes 0-40 are dark: at projector Brightness 50 they are lit.
            if np.polyfit(codes[codes <= 40], lo, 1)[0] * 40 > 5 * max(np.std(np.diff(W[bi])), 1e-12):
                raise ValueError(f"--own-black: {ch} codes 0-40 are not dark (they rise with code); "
                                 "zero on the black references instead")
            net = net - np.median(lo)
        curves[ch] = (codes, net)
    refs = {"black_W": W[bi], "black_read_sd_W": S[bi].mean(), "white_net_W": wref,
            "black_noise_W": np.std(np.diff(W[bi])) / np.sqrt(2)}
    return {"refs": refs, "curves": curves}


def offset_gamma(codes, c0, gamma):
    """((code - c0)/(255 - c0))^gamma, zero at and below c0."""
    return np.where(codes > c0, (codes - c0) / (255 - c0), 0) ** gamma


def fit_channel(codes, net):
    """
    Offset-gamma fit of one channel, zeroed on its own codes 0-40.

    Returns a dict with the codes 0-40 offset/noise/slope, the first steadily lit code (it and
    the next three above 5 sd of codes 0-40), c0, gamma and the max residual (fraction of 255).
    """
    lo = net[codes <= 40]
    offset = np.median(lo)
    n2 = net - offset
    full = n2[codes == 255][0]
    noise = np.std(lo)
    thr = 5 * noise
    first = next(k for k in codes if all(n2[codes == j][0] > thr for j in range(k, k + 4)))
    m = codes >= first + 8
    best = None
    for c0 in np.arange(30, first - 0.05, 0.1):
        for g in np.arange(1.5, 3.5, 0.005):
            e = np.sum((n2[m] / full - offset_gamma(codes[m], c0, g)) ** 2)
            if best is None or e < best[0]:
                best = (e, c0, g)
    _, c0, g = best
    res = n2[m] / full - offset_gamma(codes[m], c0, g)
    return {"offset_W": offset, "noise_W": noise, "slope_W_per_code": np.polyfit(codes[codes <= 40], lo, 1)[0],
            "first_lit": int(first), "c0": float(c0), "gamma": float(g), "max_resid": float(np.abs(res).max()),
            "fraction": {int(k): float(n2[codes == k][0] / full) for k in codes}, "full_W": float(full)}


def report(sweep):
    refs = sweep["refs"]
    w = refs["white_net_W"]
    print(f"black refs {len(refs['black_W'])}: mean {refs['black_W'].mean() * 1e9:.1f} nW, "
          f"spread {np.ptp(refs['black_W']) * 1e9:.1f} nW, within-read sd ~{refs['black_read_sd_W'] * 1e9:.1f} nW")
    print(f"white refs {len(w)}: first {w[0] * 1e6:.2f} uW, last {w[-1] * 1e6:.2f} uW, "
          f"drift {(w[-1] / w[0] - 1) * 100:+.2f}%, peak-to-peak {np.ptp(w) / w.mean() * 100:.2f}%")
    fits = {}
    for ch, (codes, net) in sweep["curves"].items():
        f = fits[ch] = fit_channel(codes, net)
        fr = f["fraction"]
        print(f"\n{ch}: code 255 net {f['full_W'] * 1e6:.2f} uW (meter at its own wavelength)")
        print(f"  codes 0-40: offset {f['offset_W'] * 1e9:+.1f} nW, sd {f['noise_W'] * 1e9:.1f} nW, "
              f"slope {f['slope_W_per_code'] * 1e9:+.2f} nW/code")
        print(f"  first steadily lit code {f['first_lit']}; fit c0 = {f['c0']:.1f}, gamma = {f['gamma']:.3f}, "
              f"max residual {f['max_resid'] * 100:.2f}% of 255")
        print("  " + "  ".join(f"c{k} {fr[k] * 100:.3f}%" for k in (64, 89, 128, 178)))
    w_, r_, g_, b_ = (sweep["curves"][c][1] for c in CHANNELS)
    print("\n(R+G+B)/white (mixed meter wavelengths, approximate): "
          + " ".join(f"{c}:{(r_[c] + g_[c] + b_[c]) / w_[c]:.3f}" for c in (96, 128, 178, 255)))
    return fits


def plot(sweep, path, title):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(1, 3, figsize=(16, 5))
    col = dict(white="k", red="r", green="g", blue="b")
    for ch, (codes, net) in sweep["curves"].items():
        full = net[codes == 255][0]
        ax[0].plot(codes, net / full, ".-", ms=2, lw=0.6, color=col[ch], label=ch)
        ax[1].semilogy(codes, np.clip(net, 1e-10, None) * 1e9, ".", ms=2, color=col[ch])
        ax[2].plot(codes[codes <= 110], net[codes <= 110] * 1e9, ".-", ms=3, lw=0.6, color=col[ch])
    ax[0].plot(np.arange(256), (np.arange(256) / 255) ** 2.2, "--", color="gray", lw=0.8, label="code^2.2")
    ax[0].set(xlabel="code", ylabel="fraction of code 255", title=title)
    ax[0].legend()
    ax[1].set(xlabel="code", ylabel="net power nW (log)", title="log scale")
    ax[2].set(xlabel="code", ylabel="net power nW", title="codes 0-110")
    fig.tight_layout()
    fig.savefig(path, dpi=110)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("session_dir")
    ap.add_argument("--own-black", action="store_true", help="re-zero each channel on its codes 0-40")
    ap.add_argument("--plot", help="write a three-panel plot here")
    ap.add_argument("--title", default="code sweep")
    args = ap.parse_args(argv)
    sweep = load_sweep(args.session_dir, own_black=args.own_black)
    report(sweep)
    if args.plot:
        plot(sweep, args.plot, args.title)
        print(f"plot: {args.plot}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
