# omnifin0 mixing model prototype (2026-10-20)

Read-only analysis of SpecSail runs; nothing on the rig was touched.

A prototype, tied to the two 2026-10-20 runs below. Run from the repo root with the pixi environment:

```
pixi run python analysis/mixing/mixing_model.py results/mixing/out_larval
pixi run python analysis/mixing/mixing_model.py results/mixing/out_adult --lmax 362,417,480,564
pixi run python analysis/mixing/plot_metamers.py results/mixing/metamers.png results/mixing/out_*
```

- `mixing_model.py OUT_DIR [--lmax UV,S,M,L] [--runs DIR]`: builds the per-arena model, checks it against white and the grays, and searches metamers. Writes `OUT_DIR/results.json` and `OUT_DIR/primaries.npz`. `--runs` defaults to `~/SpecSail/runs` because run bundles are not in git.
- `plot_metamers.py OUT_PNG RESULTS_DIR...` writes the figure. One RESULTS_DIR must end in `out_larval`.
- The default cones are the larval set UV 360, S 416, M 470, L 550 nm (zebrafish-vision-teacher). The adult set is 362, 417, 480, 564. The table below also used the M/L ±10 nm cases (`--lmax 360,416,460,550`, `...,480,550`, `...,470,540`, `...,470,560`).
- `tests/test_mixing_model.py` checks the fit, additivity, code inversion and metamer search on synthetic spectra.

Inputs:
- `..._baseline_20261020_20261020T153525Z`: arenas 1 and 2. Its arena 3 is invalid and not used.
- `..._baseline_20261020_arenas_2b_3_4_20261020T194700Z`: arena 2 bridge, arenas 3 and 4.

## Model

- Spectrum preprocessing:
  1. Average the 3 scans.
  2. Subtract the dark pixels (0-3, 1040-1043).
  3. Divide by the integration time.
  4. Subtract the 720-800 nm median.
- Primary X = spectrum(X = 255) - black.
- Drive fraction t_X(code) = the least-squares scale of each ramp step onto the primary.
- The transfer curve is fitted as t = ((c - c0) / (255 - c0))^gamma. Per channel and arena, c0 is 48-51 and gamma is 2.05-2.26.
- Prediction = black + sum over X of t_X · primary_X.

## Validation (380-720 nm integrated, prediction vs measurement)

- **White:** -4.3% at arena 1; within 0.6% at the other arenas.
- **Grays 64-224, including the 77/89/178 protocol grays:** -4.1% to +2.5%.
- **Shape distance** (half L1 between the normalised spectra): 2-4%.
- **Earlier transfer curves that failed:** linear interpolation overshot gray 77 by +31% and gray 89 by +12%. Log-linear undershot by 16-26%.
- **Primary shape** is constant within 1-7% at codes ≥ 96. Codes ≤ 32 sit at black, and code 64 is noisy.

## Metamers (cone-space chromaticity, normalised to projector white)

- Error = std of log((c + 0.01) / (c_target + 0.01)) over S, M, L. The 0.01 floor is an assumption. Without it, near-zero target catches dominate the error.
- UV is reported, not fitted. The projector's UV share of catch is about 0.02-0.06%.
- No target can be matched exactly in S, M and L: the primary weights for an exact match always include a negative one.
- "Progress" = how far from white toward the target the best mix gets (1 = exact):

| Target | Best mix (larval cones) | Progress, larval | Progress, all 6 cone sets |
| --- | --- | --- | --- |
| 488 nm | RGB about 90-135, 255, 150-165 | 0.07-0.10 | 0.01-0.20 |
| 561 nm | red 255 + green 140-255, blue 0 | 0.53-0.63 | 0.51-0.75 |
| 631 nm | red 255 only | 0.62-0.68 | 0.52-0.75 |

- For 561 nm the green code barely changes the error, so that choice is driven by brightness.

## Caveats

- **The spectra are not response-corrected.** There is no calibrated lamp, so these are counts, not photons. A slope in the detector response would shift every cone catch.
- **Dry film only;** the production film is wet.
- **The fiber has a narrow view,** and its placement moves per-colour fractions by up to about 35% (specsail-calibration-protocol). Even so, the results agree across arenas.
- **Cone model:** A1 Govardovskii templates, no ocular media, no self-screening, no receptor-noise weighting.
- **Spectrometer vs power meter for the low grays:** resolved. The power meter's figures subtract projector black, while the raw gray fractions here don't. With black subtracted on both, the two instruments agree within 1-3% of value (specsail-calibration-protocol, arenas 3 and 4: gray 77 0.0121-0.0123 vs 0.0124-0.0125; gray 89 0.0262-0.0267 vs 0.0269-0.0270). The model's additivity check compares like with like, with black included on both sides. Black stays a separate term in the metamer catches because the fish see it.
- **Arena-to-arena scaling uses the power-meter ratios** (1.00 / 1.04 / 1.02 / 0.96), not spectrometer counts.

## Next steps

- Add ramp steps at 48-112 in the next plan. The curve's toe is only constrained by 64 and 96.
- Measure 2-3 candidate mixes directly (for example 488: 120,255,150; 561: 255,200,0; 631: 255,0,0) to check the model.
- Use a lamp correction, then rerun.
