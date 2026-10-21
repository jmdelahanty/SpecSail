# Source-specific spectral crosstalk

This extends SpecSail PR #5 (`5457a63`) and uses the `arena_fills` contract in
[Citrus PR #65](https://github.com/JohnsonLabJanelia/citrus/pull/65)
(`2719d0b`). No Citrus change is needed. This document and its tests describe
software behavior; they are not physical calibration results.

## Select sources independently of the receiver

On an `arena_fills` position, `arena_id` continues to identify the **receiver**
where the probe sits. For example, illuminate arena 3 while measuring arena 1:

```json
{
  "position_id": "arena_1_center_from_arena_3",
  "arena_id": "arena_1",
  "offset_mm": [0.0, 0.0],
  "pattern": "selected_sources",
  "source_arena_ids": ["arena_3"]
}
```

`source_arena_ids` is required with `selected_sources`, must contain unique
members of the plan's `arena_ids`, and is refused with any other pattern/backend.
Multiple sources are allowed. An empty list holds all arena regions black;
background still obeys the existing `arena_fills.background` setting.

The defaults and existing `all`, `target_only`, and `all_but_target` patterns
are unchanged. All loaded arenas remain in the transaction, unselected arenas
are explicitly rendered black by Citrus, and placement still uses all-white
with the overlay. Measurement records include the selected source IDs, fill
map, domain and actual Citrus status. Selected IDs describe intent even in a
paired black capture; actual lit regions come from the reported RGBA values.
Capture now also verifies the reported illumination domain.

The old aggregate stray-light example remains unchanged. With four arenas,
`all_but_target` measures three sources together and cannot identify their
individual contributions.

## Optional four-arena plan

`examples/calibration_plan_omnifin0_source_crosstalk.json` is a separate,
opt-in template, not an update to the existing acquisition plan:

- Four receivers, each at its centre.
- Four single-source conditions and an independent all-on control per receiver:
  **16 source/receiver pairs and 4 controls**.
- Three white acquisitions per condition, ten raw scans per acquisition.
- Start/end white drift references at each condition.
- The existing acquisition machinery captures an all-black reference at the
  same position for **every integration time used**, including drift captures.
- Black background, full unmasked arena domain, fixed 100 ms exposure.

There are 20 position entries, kept consecutive by receiver. Keep the probe in
its rigid holder across the five conditions at one receiver; confirm each
placement without repositioning it. Move it only to the next receiver. If it
moves unexpectedly, record that fact and repeat the affected block in a new run.
A centre measurement does not describe the whole spatial extent of an arena.

The 100 ms exposure and inherited 120 Hz setting are starting values, not
verified rig settings. During an appropriate rig session, review frame rate,
exposure/saturation, attenuation, stability/settling, fixture, standoff, room
lighting, projector processing, metadata/citations and the plan before capture.
Keep a common exposure across sources and all-on; if this lacks dynamic range,
use separate complete fixed-exposure blocks and report their limits. Do not
combine automatic exposures as though detector linearity had been calibrated.

Repeated scans and patches measure short-term repeatability. They do not
estimate probe repositioning or long-term drift. Additional independently
repeated blocks, with source order counterbalanced between runs and placement
checks, are needed for that. Cross-run pooling is intentionally not implemented.
The template measures white only; copy it and add separately grouped drive codes
for other colors/levels when those are relevant to the experiment.

## Read-only analysis

After a separately authorized rig run (or on a synthetic test bundle):

```bash
pixi run crosstalk runs/RUN_ID --out crosstalk_RUN_ID.json
```

The output must be a new file outside the immutable capture bundle. The command
never opens Citrus, imports a hardware driver, changes a plan, or starts capture.
It verifies manifest digests for files it reads and reports measurement exclusions.
It uses the actual per-arena RGBA values in scene status, not pattern names.

Every drive code, integration time, domain, target plane and receiver offset
gets a separate matrix. Reported arena regions, filled pixel counts, framebuffer
size and Citrus build must also match; changes split the data into separate groups. Wavelength grids must agree with the instrument readout.
Receiver and source ordering is explicit in `arena_order` and `matrix_axes`.
Native wavelengths are retained; electrically dark pixels are excluded from the
additivity check. The default diagnostic range is 400–700 nm and is configurable.

For receiver `r`, illuminated source `s`, wavelength `λ` and fixed exposure `t`:

```text
M[r,s,λ] = mean(source-only counts[r,s,λ] - matched black counts[r,λ]) / t
A[r,λ]   = mean(all-on counts[r,λ] - matched black counts[r,λ]) / t
R[r,λ]   = A[r,λ] - sum_s M[r,s,λ]
```

`M` is an **observed single-source detector-response matrix** in counts/s per
native pixel at the specified drive. It is not calibrated irradiance, a fraction
of emitted source power, or a transfer function across arbitrary drive levels.
Projector-black includes ambient light and black-level leakage; subtraction
estimates incremental response above that baseline. Detector nonlinearity is
not corrected. Matching exposure prevents an extra unvalidated exposure-scaling
assumption but does not establish detector linearity.

The report also provides `M[r,s]/M[r,r]`, explicitly normalized to the receiver's
own-source response. Low-SNR/nonpositive denominators are null; weak negative
numerators are retained. These are ratios of measured responses, not a fitted
allocation of the all-on signal. Estimates remain available if additivity fails.

### Uncertainty and additivity

Each acquisition's mean has scan standard error `sample_sd / sqrt(n)`. Repeated
acquisitions are equally weighted, with additional between-acquisition variance
when it exceeds the propagated scan variance. The same paired-dark estimate
can be reused by several patches. Its uncertainty is retained as a shared term,
so collecting more illuminated scans does **not** make dark uncertainty vanish.
Shared terms also propagate through residuals and normalized ratios (delta method).

This assumes independent scans and independent extra between-acquisition errors.
It excludes unmeasured temporal correlation, placement, gain, drift between
conditions, detector calibration and other systematic errors. The report says
so; a numerically small SE is not a physical calibration uncertainty budget.

The all-on control is held out: no least-squares fit or forced linear
decomposition is performed. At each tested pixel, compare the absolute residual
with `absolute_tolerance + relative_tolerance * max(abs(A), abs(sum(M)))`:

- Residual plus the uncertainty margin fits inside tolerance:
  `consistent_with_additivity` at this pixel.
- Residual minus the uncertainty margin exceeds tolerance: `nonadditive`.
- Otherwise, or without adequate signal/uncertainty: `insufficient_evidence`.

Defaults are a 5% relative tolerance, zero absolute tolerance, and three standard
errors. These are configurable diagnostic defaults, **not** an experimentally
established acceptance specification or simultaneous confidence intervals.
A receiver passes only if every tested pixel passes; any resolved mismatch is
reported. Inspect `per_pixel_status` and spectral residuals: one isolated pixel
can trigger a diagnostic and repeated testing across pixels is not corrected.
For a scientific decision, choose a justified band/tolerance in advance and
validate the instrument and systematic uncertainty separately.

Missing single-source/all-on conditions, missing compatible darks, fewer than
two scans, weak denominators, high uncertainty, saturation, unstable captures,
unknown grids/domains, illuminated backgrounds, previously corrected data and
significantly negative dark-subtracted signals are handled explicitly. Missing
or incompatible start/end drift evidence, or detected bracket drift, prevents
supporting a linear model. Bracket comparisons never rescale the measurements;
no detected bracket drift does not rule out between-condition drift.
Failed/interrupted runs cannot support the model. Valid partial cell estimates
remain visible, and excluded measurements have reasons and IDs.

A failed additivity check does not locate the cause: projector image processing,
optical interactions, detector nonlinearity, drift or probe movement can all
contribute. A successful all-on check only supports the tested combination;
it cannot rule out nonlinear interactions that cancel there or occur at other
source combinations, wavelengths, drive levels or geometries.

## Software validation versus rig validation

```bash
python -m unittest tests.test_crosstalk -v
pixi run test
```

The new regression suite runs acquisition through an in-process fake Citrus and
a fake spectrometer with a known asymmetric wavelength-dependent matrix. It
checks recovery and uncertainty with an additive model and rejection with a
nonadditive multi-source gain model. It also checks legacy patterns, selector
validation, matched darks, shared-dark covariance, drift, missing conditions,
saturation, incompatible exposure/grid/domain/background, uncertain data and
read-only output behavior. Fake reports are labelled `synthetic_software_validation`.

The normal full suite still uses Unix sockets for the existing transport tests.
Where sockets are prohibited, `python -m tests.run_inprocess` explicitly
substitutes the fake transport in those tests. This validates acquisition/control
logic but does not validate OS socket transport. No fake result validates Citrus
rendering on the projector, physical geometry, spectral accuracy, or the actual
omnifin0 contamination matrix. Those require a separate rig session.
