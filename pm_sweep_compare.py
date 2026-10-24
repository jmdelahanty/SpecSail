"""Compare two code sweeps (e.g. Brightness 11 vs 50) on one plot, in absolute net power.

usage: pm_sweep_compare.py <session_a> <label_a> <session_b> <label_b> <out_png>
Per channel: black from interpolated black refs, drift-corrected to each session's mean white,
and re-zeroed on the codes 0-40 median ONLY for session A (whose codes 0-40 are known black);
session B keeps the black-ref zero so a lifted floor at code 0 shows up.
"""
import csv, os, sys
import numpy as np


def load(d):
    rows = list(csv.DictReader(open(os.path.join(d, "power_meter", "summary.csv"))))
    W = np.array([float(r["mean_W"]) for r in rows]); fid = [r["fill_id"] for r in rows]
    idx = np.arange(len(rows))
    bi = np.array([i for i, f in enumerate(fid) if f.endswith("_black")])
    wi = np.array(sorted([i for i, f in enumerate(fid) if f.startswith("ref") and f.endswith("white_255")]
                         + [fid.index("white_drift_start"), fid.index("white_drift_end")]))
    black = np.interp(idx, bi, W[bi]); white = np.interp(idx, wi, W[wi] - black[wi]); w0 = (W[wi] - black[wi]).mean()
    out = {"black_ref_W": W[bi].mean(), "white_W": w0}
    for ch in ("white", "red", "green", "blue"):
        pts = sorted((int(f.split("_")[1]), i) for i, f in enumerate(fid) if f.startswith(ch + "_") and f.split("_")[1].isdigit())
        c = np.array([p[0] for p in pts]); ii = np.array([p[1] for p in pts])
        out[ch] = (c, (W[ii] - black[ii]) * (w0 / white[ii]), W[ii] * (w0 / white[ii]))
    return out


A, la, B, lb, png = sys.argv[1:6]
a, b = load(A), load(B)
print(f"{la}: black ref {a['black_ref_W']*1e9:.0f} nW, white net {a['white_W']*1e6:.2f} uW")
print(f"{lb}: black ref {b['black_ref_W']*1e9:.0f} nW, white net {b['white_W']*1e6:.2f} uW "
      f"(white x{b['white_W']/a['white_W']:.3f}; raw black {b['black_ref_W']/a['black_ref_W']:.2f}x)")
for ch in ("white", "red", "green", "blue"):
    ca, na, ra = a[ch]; cb, nb, rb = b[ch]
    za = np.median(na[ca <= 40]); na = na - za
    # B: raw reading minus A's zero for this channel (A's codes 0-40 = dark + stray at this meter wavelength)
    zb_raw = np.median(ra[ca <= 40])
    nb_abs = rb - zb_raw
    thr = 5 * np.std(na[ca <= 40])
    first_b = next((k for k in cb if all(nb_abs[cb == j][0] - nb_abs[cb == 0][0] > thr for j in range(k, min(k + 4, 256)))), None)
    print(f"\n{ch}: code0 in {lb} = {nb_abs[cb==0][0]*1e9:+.0f} nW above {la}'s black "
          f"({nb_abs[cb==0][0]/na[ca==255][0]*100:.3f}% of {la} white); first code rising above {lb}'s own code 0: {first_b}")
    for k in (0, 16, 32, 40, 44, 48, 52, 56, 64, 89, 128, 178, 255):
        print(f"   code {k:3d}: {la} {na[ca==k][0]*1e9:9.1f} nW | {lb} {nb_abs[cb==k][0]*1e9:9.1f} nW")
    # contrast available: dimmest distinguishable step above black, in Weber terms
import matplotlib; matplotlib.use("Agg"); import matplotlib.pyplot as plt
fig, ax = plt.subplots(1, 2, figsize=(14, 5))
col = dict(white="k", red="r", green="g", blue="b")
for ch in col:
    ca, na, ra = a[ch]; cb, nb, rb = b[ch]
    za = np.median(na[ca <= 40]); zr = np.median(ra[ca <= 40])
    for axi in ax:
        axi.plot(ca, np.clip(na - za, 1e-10, None) * 1e9, "-", lw=0.8, color=col[ch], label=f"{ch} {la}")
        axi.plot(cb, np.clip(rb - zr, 1e-10, None) * 1e9, "--", lw=0.8, color=col[ch], label=f"{ch} {lb}")
ax[0].set_yscale("log"); ax[0].set(xlabel="code", ylabel="net power above brightness-11 black, nW (log)", title="full range")
ax[1].set(xlim=(0, 110), xlabel="code", ylabel="nW", title="codes 0-110"); ax[1].set_ylim(0, None)
ax[0].legend(fontsize=7, ncol=2); fig.tight_layout(); fig.savefig(png, dpi=110); print("plot:", png)
