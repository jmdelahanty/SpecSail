#!/usr/bin/env python3
"""Plot total sub-400nm irradiance across stretches for Arena C06R2 UV data."""

from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
from spectrum_plot import load_all_spectra

results_dir = Path("results")
n_stretches = 20
labels = []
uv_values = []
stretch_indices = []

for i in range(n_stretches):
    data_dir = results_dir / f"Arena C06R2 UV s{i:02d}_data"
    wavelengths, all_values, calibrated, csv_files = load_all_spectra(data_dir)
    if wavelengths is None:
        print(f"Warning: no data in {data_dir}")
        labels.append(f"stretch {i}")
        continue

    # Each file's mean spectrum is one row in all_values
    mask = wavelengths < 400
    for spectrum in all_values:
        uv_total = np.nansum(spectrum[mask])
        uv_values.append(uv_total)
        stretch_indices.append(i)

    labels.append(f"stretch {i}")

x = np.array(stretch_indices)
y = np.array(uv_values)

# Polynomial fit
poly_deg = 2
coeffs = np.polyfit(x, y, poly_deg)
fit_x = np.linspace(0, n_stretches - 1, 100)
fit_y = np.polyval(coeffs, fit_x)

# R² calculation
y_pred = np.polyval(coeffs, x)
ss_res = np.sum((y - y_pred) ** 2)
ss_tot = np.sum((y - np.mean(y)) ** 2)
r_squared = 1 - ss_res / ss_tot

fig, ax = plt.subplots(figsize=(16, 10))
jitter = np.random.default_rng(42).uniform(-0.25, 0.25, size=len(x))
ax.scatter(x + jitter, y, alpha=0.4, s=20, color='mediumvioletred', label='Individual spectra')
ax.plot(fit_x, fit_y, color='black', lw=2,
        label=f'Polynomial fit (deg={poly_deg}, R²={r_squared:.3f})')
ax.set_xticks(range(n_stretches))
ax.set_xticklabels(range(n_stretches))
ax.set_xlabel("Stretch")
ax.set_ylabel("Irradiance (µW/cm² per 5nm bin, summed < 400nm)")
ax.set_title("UV Irradiance (< 400 nm) — Arena C06R2 UV stretches 0–19")
ax.legend()
ax.grid(True, alpha=0.3)
plt.tight_layout()
plt.savefig("uv_stretches.png", dpi=100)
plt.show()
