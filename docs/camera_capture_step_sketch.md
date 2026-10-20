# Sketch: camera brightness maps under a held Citrus scene

Status: design sketch, 2026-10-20. Nothing implemented. The Orange side is being scoped by the Orange agent; the
request/response fields below are a proposal to align with it.

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
protocol grays. Black (projector black) and a lens-cap dark phase come from Orange.

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
2. `OrangeClient` (new): calls Orange's request (CLI or local-control socket, per Orange's design) and parses the
   outcome.
3. `CameraMapRun` (or a mode of `CalibrationRun`): for each fill, `scenes.show(rgba)`, then the Orange request, then
   the optional spectrometer measurement, then record.
4. `tests/fakes.py`: a `FakeOrange` that reads `FakeCitrus` status and can return `same_scene`, `changed_scene` or
   `refused`.

Tests: outcome handling; refusing to continue on `changed_scene`; the scene never changing before the outcome arrives;
ids echoed; bundle contents.

## Open questions for Orange

1. CLI or local-control method? SpecSail can call either.
2. The exposure policy: SpecSail-supplied ladder, or Orange's target-DN search?
3. The timeout to expect for 8 frames × 4 cameras at long exposures.
4. Where SpecSail finds the outputs, and whether Orange returns the maps (block means) directly for the run log.
