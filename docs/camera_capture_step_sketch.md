# Sketch: camera brightness maps under a held Citrus scene

Status: design sketch, 2026-10-20. Nothing implemented. Aligned with Orange's scope: branch
agent/calibration/scene-brightness-capture-20261020, commit 71a75f4,
`docs/scene_brightness_capture_scope_2026_10_20.md`. Orange's doc is authoritative for the request and result
schemas.

## Goal

Citrus fills the whole canvas with one colour, and all four Orange cameras capture their arena together. Repeat for a
set of colours and levels. The result is a brightness **map** of each arena disc under each fill, next to the
spectrometer's single-spot measurements.

What the maps answer that the fiber can't:

- whether each disc is evenly lit, or brighter at the edge than the centre;
- film sag, creases, shading by cables or holders;
- whether the arena-to-arena differences measured at the centre hold across each disc.

What they don't answer:

- **Arena-to-arena levels.** Each camera has its own gain, iris and vignetting, so cross-camera ratios aren't
  trustworthy. Use the maps for shape within an arena, normalised to the centre spot. Leave the arena-to-arena
  comparison to the spectrometer.
- **Spectra.** The cameras are monochrome.

First series: bare film (no dish holders, nothing on the shelf), IR arrays off, R72 filters removed but lenses not
refocused (Orange's advice: brightness maps are coarse, so the slight focus shift doesn't matter). A later series can
add the holders, to see what the fish actually get.

## Division of work

| Who | Does | Never does |
|---|---|---|
| SpecSail | owns the Citrus transaction; sets and holds each scene; asks Orange to capture; waits for the answer; records everything in the run bundle | touch cameras |
| Orange | **reads** Citrus `calibration_scene_status`; checks the scene matches what SpecSail said; captures on all cameras; re-reads status; returns an outcome | send any Citrus scene command (Citrus checks ownership by transaction id only, so a set/restore from Orange could change SpecSail's scene) |
| Citrus | shows the scene and reports its status | acquire camera frames |

No Citrus code change is needed (citrus-agent assessment, 2026-10-20).

## Plan schema addition

A plan gains an optional top-level `camera_maps` block. When present, the run is a scene sequence for the cameras;
the spectrometer is optional (`with_spectrometer`).

```json
"camera_maps": {
  "orange_request": "capture_under_presented_scene",
  "cameras": "all",
  "frame_count": 8,
  "settle_ms": 500,
  "exposure": {"mode": "target_dn", "target_mean_dn": [30, 220]},
  "with_spectrometer": false,
  "reference_fill_rgba8": [255, 255, 255, 255],
  "reference_every_n_fills": 6,
  "shelf_state": "bare_film_no_holders",
  "filter_state": "r72_removed",
  "focus_state": "unchanged_from_commissioning",
  "ir_arrays": "off"
}
```

`patches` keeps its current meaning: the fills to show, for example white, R/G/B at 255, a gray ramp and the
protocol grays. Projected black is a SpecSail fill (kind `projected_black`); the lens-cap dark is an Orange operator step.

## Flow per fill

```
SpecSail                              Citrus                      Orange
   │ set_calibration_scene(canvas_fill, rgba, overlay off) ─▶│
   │◀──────────── presented (revision, fingerprint, visible_from) ─│
   │ capture_under_presented_scene(expected ids, cameras, frames, settle) ─────────▶│
   │                                       │◀── read status (no transaction) ───────│
   │                                       │    refuse unless presented + same ids, │
   │                                       │    and exposures start ≥ visible_from  │
   │                                       │    + settle                            │
   │                                       │◀── read status again after capture ────│
   │◀──── outcome: same_scene | changed_scene | refused:<reason>, capture_group_id, │
   │       per-camera frame ids + PTP times, pre/post status verbatim ──────────────│
   │ (optional) spectrometer measurement at the probe spot
   │ next fill only after the outcome arrives
```

- `changed_scene` or `refused` stops the run (or retries once, by plan setting).
- A white reference fill is repeated at the start, middle and end of the series to catch drift in the projector or
  cameras.

## What the bundle records

- Citrus: the full `calibration_scene_status` objects before and after each capture, verbatim, plus one `status` read
  per run for the display mode.
- Orange: its outcome object, capture_group_id, output paths (on Data2) with SHA-256, per-camera read-back (exposure,
  gain, offset, pixel format, iris, focus), and the fraction of pixels at 0 and 255 DN.
- Operator: shelf state, filter state, focus state, IR state, photos, and the no-windows/no-screenshots confirmation.
- Ids: `transaction_id` = SpecSail run id and `operation_id` per fill, so Citrus echoes the link in its own logs.

## Known limits

- **Citrus "presented" doesn't prove the light.** Both 2026-10-20 incidents (a window dragged over DP-3, the GNOME
  screenshot dimming) left Citrus reporting `presented`. Only the camera images, or the spectrometer, show the actual
  light. The operator rule stays: no windows on the projector, no screenshots, no overview or screen lock.
- 8-bit frames: every fill needs its own exposure to stay within ~30–220 DN; dim grays need long exposures.
- No flat-field exists, so a map mixes projector non-uniformity, lens vignetting and sensor response. Day-to-day
  ratios on the same camera with the iris untouched cancel the vignetting.
- Before any recording afterwards: reinstall the R72, pass Orange's exposure check, and run a verification capture
  against the accepted daily registration.

## Implementation outline (SpecSail)

1. `load_plan`: validate `camera_maps`.
2. `OrangeClient` (new): writes the request JSON, runs Orange's CLI with a timeout, and reads the result file and
   exit code.
3. `CameraMapRun` (or a mode of `CalibrationRun`): for each fill, `scenes.show(rgba)`, then the Orange request, then
   the optional spectrometer measurement, then record.
4. `tests/fakes.py`: a `FakeOrange` that reads `FakeCitrus` status and can return `same_scene`, `changed_scene` or
   `refused`.

Tests: outcome handling; refusing to continue on `changed_scene`; the scene never changing before the outcome arrives;
ids echoed; bundle contents.

## Agreed with Orange (2026-10-20)

- **Interface: a CLI.** `scripts/run_scene_brightness_capture.py --request req.json --result out.json`.
  - The result file is always written.
  - Exit codes: 0 `same_scene`, 3 `changed_scene`, 2 `refused`, 1 error.
  - It needs the Orange GUI closed (otherwise it refuses with `rig_process_active`) and opens no window.
- **Request fields:**
  - under `citrus`: `expected_transaction_id`, `expected_scene_revision`, `expected_content_fingerprint`, exactly as
    Citrus reported them;
  - under `condition`: `kind`, `shelf_state`, `filter_state`, `focus_state`, `ir_arrays`, `is_reference_fill`,
    `rgba8`, `label`;
  - under `specsail`: run id, step id, `plan_digest`.
- **Projected black is SpecSail's.** SpecSail shows canvas_fill (0,0,0,255) and calls Orange with
  `condition.kind = "projected_black"`. The lens-cap dark (`"lens_capped_dark"`) is an Orange operator step that
  skips the Citrus checks.
- **Exposure:** `target_dn` with [30, 220]. Orange probes, then confirms, giving each camera its own exposure.
  `ladder` with SpecSail-supplied `exposures_us` is also available.
- **Settle:** Orange waits until CLOCK_REALTIME ≥ visible_from + `settle_ms` (500) before opening the cameras, then
  checks every kept frame's exposure-start bound.
- **Display cadence:** the request carries `expected_display_hz` (120) and a tolerance, with policy `record` or
  `refuse`. Orange always records the Hz it measures from `current_display_frame`. This is a liveness and stall
  check only. `current_display_frame` counts Citrus's own swaps (a 120.000 Hz limiter, swap interval 0), so it
  reads 120 Hz even when presentation is composited. Measure over ≥10 s, because the socket snapshot is rebuilt
  every 100 ms. Only a photodiode or a window-stacking record can detect compositing.
- **Light sanity:** Orange compares each fill's 8×8 tiles with the latest reference fill and warns
  `possible_occlusion` (a window or shell dimming). It warns, never refuses. SpecSail should surface the warning to
  the operator.
- **Iris:** only the first capture in a session re-homes it; later ones leave the lens untouched (still being
  verified in Orange code). The reference fills catch drift.
- **Timeout:** about 180 s per request with target_dn (estimated; the result carries wall times to tighten it).
- **Outputs:** `/mnt/Data2/calibrations/scene_brightness/<specsail_run_id>/<step_id>/`.
  - `condition.json` is the result object: per-camera stats (mean, p01/p99, fractions at 0/255, temporal sigma,
    in_band), the 8×8 centre-normalised tiles inline, verbatim pre/post scene status plus one display status, frame
    ids, PTP times, read-back, and sha256 for every file.
  - Full block-16 maps and raw frames are files only.
  - SpecSail copies the tiles and stats into its run log.

SpecSail's obligations:

- hold the scene from `presented` until Orange returns, with no scene command in between;
- pass the ids verbatim;
- never call Orange while the Orange GUI is open.

Still open:

- Confirmed by citrus-agent (Citrus 25c31f5):
  - `visible_from_display_frame_swap_end_epoch_ns` is CLOCK_REALTIME, taken right after `glfwSwapBuffers` returns
    for the fence frame. It marks the swap-call return, not light.
  - `calibration_scene_status` needs no transaction. Each read needs a fresh `request_id`.
- The sudo wrapper's allow-list may need Data2 (Jeremy).
