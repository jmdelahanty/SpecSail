#!/usr/bin/env python3
"""Read a sealed arena_fills bundle; estimate source responses without fitting them.

Rows are receiver arenas, columns are illuminated source arenas. All-on spectra
are held out to test additivity. See docs/source_arena_crosstalk.md for the model,
uncertainty assumptions, exclusions and physical-validation requirements.
"""

import argparse
from collections import defaultdict
import json
from pathlib import Path
import sys

import numpy as np

from calibration_run import ARENA_FILL_DOMAINS, sha256_file
from spectral_capture import SATURATION_FRACTION

BLACK = [0, 0, 0, 255]


class EvidenceError(ValueError):
    """A measurement cannot support this analysis."""


def _read_json(path):
    return json.loads(path.read_text())


def _inside(root, relative):
    path = (root / relative).resolve()
    if not path.is_relative_to(root):
        raise EvidenceError(f"path escapes bundle: {relative}")
    return path


def _checked_file(root, manifest, relative):
    path = _inside(root, relative)
    if relative not in manifest or not path.is_file():
        raise EvidenceError(f"missing manifested file: {relative}")
    if sha256_file(path) != manifest[relative]:
        raise EvidenceError(f"digest mismatch: {relative}")
    return path


def _scene(m, arenas):
    scene = m.get("scene") or {}
    state = scene.get("status", {}).get("arena_fills") or {}
    if scene.get("recipe_id") != "arena_fills" or state.get("placement_overlay") is not False:
        raise EvidenceError("requires an arena_fills scene with overlay off")
    if state.get("background_rgba_u8") != BLACK:
        raise EvidenceError("illuminated/unknown background is an unmeasured source")
    domain = state.get("domain")
    if domain not in ARENA_FILL_DOMAINS or domain != scene.get("domain"):
        raise EvidenceError("unknown or mismatched illumination domain")
    entries = state.get("arenas", [])
    fills = {a["arena_id"]: a.get("rgba_u8") for a in entries}
    if len(entries) != len(arenas) or set(fills) != set(arenas):
        raise EvidenceError("scene does not report exactly the plan arenas")
    sources = []
    for a, color in fills.items():
        if color != BLACK:
            if color != m.get("rgba8"):
                raise EvidenceError("source drive differs from measurement drive")
            sources.append(a)
    if m.get("role") == "projector_black" and sources:
        raise EvidenceError("paired dark is not all black")
    geometry = {}
    for a in entries:
        fields = ("arena_rect_canvas_px", "region", "filled_pixel_count")
        if any(a.get(k) is None for k in fields):
            raise EvidenceError("missing reported arena geometry")
        geometry[a["arena_id"]] = {k: a[k] for k in fields}
    if not state.get("framebuffer_size_px"):
        raise EvidenceError("missing reported framebuffer geometry")
    identity = json.dumps({"arenas": geometry, "framebuffer_size_px": state["framebuffer_size_px"],
                           "citrus_build": scene["status"].get("citrus_build")}, sort_keys=True)
    return domain, tuple(a for a in arenas if a in sources), identity


def _read_measurement(root, manifest, m, instrument, wavelengths):
    if m.get("any_saturated") is not False or any(s.get("saturated_pixel_count", 0) for s in m["scans"]):
        raise EvidenceError("saturated or missing saturation evidence")
    if (m.get("stability") or {}).get("stable") is not True:
        raise EvidenceError("unstable or missing stability evidence")
    if m.get("nonlinearity_correction_applied") is not False or m.get("dark_correction_applied") is not False:
        raise EvidenceError("only raw uncorrected captures are supported")
    exposure = m["integration_us"]
    if not np.isfinite(exposure) or exposure <= 0 or any(s.get("integration_us") != exposure for s in m["scans"]):
        raise EvidenceError("invalid or mixed integration times")
    csv = _checked_file(root, manifest, m["spectrum_csv"])
    raw = np.loadtxt(csv, delimiter=",", skiprows=1, ndmin=2)
    if (raw.shape != (len(wavelengths), 2 + len(m["scans"])) or not len(m["scans"])
            or not np.all(np.isfinite(raw)) or not np.array_equal(raw[:, 0], np.arange(len(wavelengths)))
            or not np.allclose(raw[:, 1], wavelengths, rtol=0, atol=0.000051)):
        raise EvidenceError("invalid spectrum, scan count or wavelength grid")
    counts = raw[:, 2:].T
    if np.any(counts >= SATURATION_FRACTION * instrument["max_intensity_counts"]):
        raise EvidenceError("saturated raw counts")
    # Same-exposure projector-black subtraction removes the baseline. No
    # nonlinearity correction or radiometric calibration is implied.
    rates = counts / (exposure / 1e6)
    n = len(rates)
    variance = rates.var(axis=0, ddof=1) / n if n > 1 else None
    return {"mean": rates.mean(axis=0), "variance": variance, "n_scans": n}


def _estimate(terms, stats, extra=None):
    terms = {k: v for k, v in terms.items() if abs(v) > 1e-14}
    mean = sum(v * stats[k]["mean"] for k, v in terms.items())
    variance = None
    if all(stats[k]["variance"] is not None for k in terms):
        variance = sum(v * v * stats[k]["variance"] for k, v in terms.items())
        if extra is not None:
            variance = variance + extra
    return {"mean": mean, "variance": variance, "terms": terms,
            "extra": np.zeros_like(mean) if extra is None else extra}


def _dark_key(m):
    return (m["position_id"], m["arena_id"], m["integration_us"], m["_domain"],
            m["target_plane"], tuple(m.get("offset_mm") or [0, 0]), m.get("_geometry"))


def _condition(records, darks, stats):
    terms = defaultdict(float)
    corrected = []
    for m in records:
        mid = m["measurement_id"]
        paired = darks[_dark_key(m)]
        terms[mid] += 1 / len(records)
        corrected.append(stats[mid]["mean"] - np.mean([stats[d]["mean"] for d in paired], axis=0))
        for d in paired:
            terms[d] -= 1 / (len(records) * len(paired))
    est = _estimate(terms, stats)
    # Repeated acquisitions can expose temporal variation beyond scan noise.
    # Shared-dark covariance stays in terms; it is never divided by lit count.
    if len(records) > 1 and est["variance"] is not None:
        between = np.var(corrected, axis=0, ddof=1) / len(records)
        extra = np.maximum(0, between - est["variance"])
        est = _estimate(terms, stats, extra)
    est["measurement_ids"] = [m["measurement_id"] for m in records]
    return est


def _drift_check(records, darks, stats, mask, rtol, atol, multiplier):
    """Bracket check only, never silently renormalize signals for drift."""
    start = [m for m in records if m.get("patch_id") == "drift_reference_start"]
    end = [m for m in records if m.get("patch_id") == "drift_reference_end"]
    if len(start) != 1 or len(end) != 1:
        return {"status": "insufficient_evidence", "reason": "missing drift brackets"}
    a, b = start[0], end[0]
    if (_dark_key(a) != _dark_key(b) or a["rgba8"] != b["rgba8"]
            or a["_sources"] != b["_sources"] or not darks.get(_dark_key(a))):
        return {"status": "insufficient_evidence", "reason": "incompatible drift brackets/dark"}
    first, last = [_condition([m], darks, stats) for m in (a, b)]
    delta = _combine([last, first], [1, -1], stats)
    if delta["variance"] is None:
        return {"status": "insufficient_evidence", "reason": "drift uncertainty unavailable"}
    tol = atol + rtol * np.maximum(np.abs(first["mean"]), np.abs(last["mean"]))
    failed = mask & (np.abs(delta["mean"]) - multiplier * np.sqrt(delta["variance"]) > tol)
    return {"status": "drift_detected" if np.any(failed) else "no_drift_detected",
            "measurement_ids": [a["measurement_id"], b["measurement_id"]],
            "reason": "brackets do not rule out drift between conditions or probe movement"}


def _combine(estimates, weights, stats):
    terms = defaultdict(float)
    extra = 0
    for est, weight in zip(estimates, weights):
        for mid, value in est["terms"].items():
            terms[mid] += weight * value
        extra = extra + weight * weight * est["extra"]
    return _estimate(terms, stats, extra)


def _array(value):
    if value is None:
        return None
    return [float(v) if np.isfinite(v) else None for v in value]


def _summary(est):
    if est is None:
        return None
    return {"counts_per_s": _array(est["mean"]),
            "standard_error": _array(np.sqrt(est["variance"])) if est["variance"] is not None else None,
            "measurement_ids": est.get("measurement_ids", []),
            "dark_measurement_ids": [k for k, v in est["terms"].items() if v < 0]}


def _normalized(est, own, stats, multiplier):
    if est is None or own is None or est["variance"] is None or own["variance"] is None:
        return None
    good = own["mean"] > multiplier * np.sqrt(own["variance"])
    ratio = np.full_like(own["mean"], np.nan)
    se = ratio.copy()
    if est is own:
        ratio[good], se[good] = 1, 0
    else:
        # Delta-method covariance includes any reuse of the same dark scans.
        covariance = sum(v * own["terms"].get(k, 0) * stats[k]["variance"]
                         for k, v in est["terms"].items())
        denom, num = own["mean"][good], est["mean"][good]
        ratio[good] = num / denom
        variance = (est["variance"][good] / denom**2
                    + num**2 * own["variance"][good] / denom**4
                    - 2 * num * np.asarray(covariance)[good] / denom**3)
        se[good] = np.sqrt(np.maximum(0, variance))
    return {"ratio_to_receiver_own_source": _array(ratio), "standard_error": _array(se)}


def _additivity(singles, all_on, stats, mask, rtol, atol, multiplier):
    if any(s is None for s in singles) or all_on is None:
        return {"status": "insufficient_evidence", "reason": "missing single-source or all-on condition"}
    predicted = _combine(singles, [1] * len(singles), stats)
    residual = _combine([all_on, *singles], [1] + [-1] * len(singles), stats)
    result = {"observed_all_on": _summary(all_on), "predicted_sum": _summary(predicted),
              "residual_all_minus_sum": _summary(residual)}
    if residual["variance"] is None:
        return {**result, "status": "insufficient_evidence", "reason": "repeat scans needed to estimate uncertainty"}
    if any(np.any(mask & (e["mean"] + multiplier * np.sqrt(e["variance"]) < 0))
           for e in [*singles, all_on]):
        return {**result, "status": "insufficient_evidence",
                "reason": "significantly negative dark-subtracted response; inspect baseline/conditions"}
    se = np.sqrt(residual["variance"])
    scale = np.maximum(np.abs(all_on["mean"]), np.abs(predicted["mean"]))
    tolerance = atol + rtol * scale
    signal_se = np.maximum(np.sqrt(all_on["variance"]), np.sqrt(predicted["variance"]))
    informative = mask & (scale > multiplier * signal_se) & (scale > 0)
    delta = np.abs(residual["mean"])
    incompatible = informative & (delta - multiplier * se > tolerance)
    consistent = informative & (delta + multiplier * se <= tolerance)
    status = "insufficient_evidence"
    if np.any(incompatible):
        status = "nonadditive"
    elif np.any(mask) and np.all(consistent[mask]):
        status = "consistent_with_additivity"
    labels = np.full(len(mask), "not_tested", dtype=object)
    labels[mask] = "insufficient_evidence"
    labels[consistent] = "consistent_with_additivity"
    labels[incompatible] = "nonadditive"
    return {**result, "status": status, "per_pixel_status": labels.tolist(),
            "tolerance_counts_per_s": _array(tolerance),
            "reason": "pointwise diagnostic; uncertainty excludes unmeasured systematic effects"}


def analyze_bundle(bundle, *, relative_tolerance=0.05, absolute_tolerance=0.0,
                   uncertainty_multiplier=3.0, wavelength_range=(400.0, 700.0)):
    """Return a JSON-ready report. Read-only; never contacts Citrus or a device.

    Different drive codes, domains, exposures, offsets and planes form separate
    matrices. No cross-exposure normalization or least-squares decomposition.
    """
    if (not all(np.isfinite(v) for v in (relative_tolerance, absolute_tolerance,
                                       uncertainty_multiplier, *wavelength_range))
            or min(relative_tolerance, absolute_tolerance) < 0 or uncertainty_multiplier <= 0
            or wavelength_range[0] >= wavelength_range[1]):
        raise ValueError("invalid tolerance, uncertainty multiplier or wavelength range")
    root = Path(bundle).resolve()
    context = _read_json(root / "capture_context.json")
    manifest = {f["path"]: f["sha256"] for f in context["files"]}
    plan = _read_json(_checked_file(root, manifest, "run_plan.json"))
    instrument = _read_json(_checked_file(root, manifest, "instrument/device_readout.json"))
    arenas = plan["arena_ids"]
    if not arenas or len(set(arenas)) != len(arenas):
        raise EvidenceError("arena_ids must be nonempty and unique")
    wavelengths = np.asarray(instrument["wavelengths_nm"], dtype=float)
    if not np.all(np.isfinite(wavelengths)) or not np.all(np.diff(wavelengths) > 0):
        raise EvidenceError("invalid instrument wavelength grid")
    dark_pixels = instrument.get("electric_dark_pixel_indices")
    if (not isinstance(dark_pixels, list) or any(type(p) is not int or not 0 <= p < len(wavelengths)
                                              for p in dark_pixels)):
        raise EvidenceError("invalid or unknown electrically dark pixel indices")
    maximum = instrument.get("max_intensity_counts")
    if not isinstance(maximum, (int, float)) or not np.isfinite(maximum) or maximum <= 0:
        raise EvidenceError("invalid or unknown detector saturation limit")
    mask = (wavelengths >= wavelength_range[0]) & (wavelengths <= wavelength_range[1])
    mask[dark_pixels] = False
    stats, records, excluded = {}, [], []
    for entry in context["measurements"]:
        mid = entry["measurement_id"]
        try:
            path = _checked_file(root, manifest, f"measurements/{mid}/measurement.json")
            m = _read_json(path)
            if m.get("measurement_id") != mid or mid in stats:
                raise EvidenceError("missing, mismatched or duplicate measurement ID")
            if m.get("role") == "projector_off_background":
                continue
            domain, sources, geometry = _scene(m, arenas)
            if m.get("arena_id") not in arenas:
                raise EvidenceError("receiver not in arena_ids")
            stat = _read_measurement(root, manifest, m, instrument, wavelengths)
            m["_domain"], m["_sources"] = domain, sources
            m["_geometry"] = geometry
            stats[mid] = stat
            records.append(m)
        except (EvidenceError, KeyError, ValueError, OSError, TypeError) as exc:
            excluded.append({"measurement_id": mid, "reason": str(exc)})

    darks = defaultdict(list)
    for m in records:
        if m["role"] == "projector_black" and not m["_sources"]:
            if m.get("paired_dark_for_integration_us") == m["integration_us"]:
                darks[_dark_key(m)].append(m["measurement_id"])
    drift_records = defaultdict(list)
    for m in records:
        if m.get("drift_reference"):
            drift_records[m["position_id"]].append(m)
    drift_checks = {p["position_id"]: _drift_check(drift_records[p["position_id"]], darks, stats,
                                                   mask, relative_tolerance, absolute_tolerance,
                                                   uncertainty_multiplier)
                    for p in plan["positions"]}
    groups = defaultdict(lambda: defaultdict(list))
    for m in records:
        if m["role"] == "projector_black" or m.get("drift_reference"):
            continue
        # The actual reported source set, not its label, determines a condition.
        key = (tuple(m["rgba8"]), m["integration_us"], m["_domain"],
               m["target_plane"], tuple(m.get("offset_mm") or [0, 0]), m["_geometry"])
        group = groups[key]  # retain incomplete groups in the report
        paired = darks.get(_dark_key(m), [])
        if not paired:
            excluded.append({"measurement_id": m["measurement_id"], "reason": "missing compatible matched dark"})
            continue
        if len(m["_sources"]) not in (1, len(arenas)):
            excluded.append({"measurement_id": m["measurement_id"], "reason": "aggregate/empty source set cannot identify a matrix cell"})
            continue
        group[(m["arena_id"], m["_sources"])].append(m)

    reports = []
    for key, conditions in sorted(groups.items()):
        estimates = {k: _condition(ms, darks, stats) for k, ms in conditions.items()}
        matrix = [[estimates.get((receiver, (source,))) for source in arenas] for receiver in arenas]
        checks = {receiver: _additivity(matrix[i], estimates.get((receiver, tuple(arenas))),
                                       stats, mask, relative_tolerance, absolute_tolerance,
                                       uncertainty_multiplier) for i, receiver in enumerate(arenas)}
        for receiver, check in checks.items():
            positions = {m["position_id"] for (r, _), ms in conditions.items() if r == receiver for m in ms}
            problems = {p: drift_checks.get(p, {"status": "insufficient_evidence"}) for p in positions
                        if drift_checks.get(p, {}).get("status") != "no_drift_detected"}
            if problems:
                check["spectral_residual_status"] = check["status"]
                check["status"] = "insufficient_evidence"
                check["reason"] = "missing/incompatible drift evidence or detected drift"
                check["drift_problems"] = problems
        states = [c["status"] for c in checks.values()]
        status = ("nonadditive" if "nonadditive" in states else "consistent_with_additivity"
                  if all(s == "consistent_with_additivity" for s in states) else "insufficient_evidence")
        if context.get("outcome") != "completed" or context.get("error"):
            status = "insufficient_evidence"
        reports.append({"rgba8": list(key[0]), "integration_us": key[1], "domain": key[2],
                        "target_plane": key[3], "offset_mm": list(key[4]), "status": status,
                        "scene_geometry_and_build": json.loads(key[5]),
                        "linear_model_supported_for_tested_conditions": status == "consistent_with_additivity",
                        "single_source_response_matrix": [[_summary(e) for e in row] for row in matrix],
                        "receiver_normalized_matrix": [[_normalized(e, row[i], stats, uncertainty_multiplier)
                                                        for e in row] for i, row in enumerate(matrix)],
                        "additivity_by_receiver": checks})
    return {"schema_id": "specsail.spectral_crosstalk_analysis", "schema_version": 1,
            "capture_set_id": context["capture_set_id"],
            "capture_context_sha256": sha256_file(root / "capture_context.json"),
            "capture_outcome": context.get("outcome"), "capture_error": context.get("error"),
            "evidence_kind": "synthetic_software_validation" if instrument.get("seabreeze_backend") == "fake"
                             else "physical_measurements_require_rig_validation",
            "arena_order": arenas, "matrix_axes": ["receiver", "source"],
            "wavelengths_nm": wavelengths.tolist(), "tested_pixels": np.flatnonzero(mask).tolist(),
            "relative_tolerance": relative_tolerance, "absolute_tolerance_counts_per_s": absolute_tolerance,
            "uncertainty_multiplier": uncertainty_multiplier,
            "status": "analyzed" if reports else "insufficient_evidence", "groups": reports,
            "drift_checks_by_position": drift_checks,
            "excluded_measurements": excluded,
            "limitations": [
                "Detector counts/s, not calibrated irradiance or optical power. No detector nonlinearity correction.",
                "Single-source responses remain observations when additivity fails; no linear decomposition is fitted.",
                "SE assumes independent scans; repeated acquisitions add observed variation. Shared dark covariance is retained.",
                "SE excludes unmeasured placement, temporal correlation, drift, calibration and instrument systematics.",
                "Pointwise multiplier bands are diagnostics, not simultaneous confidence intervals or proof of linearity.",
                "Ratios normalize to the receiver's own-source response; low-SNR denominators are null, negatives are not clipped.",
                "All-on tests only the measured drive/geometry; pairwise or other stimulus combinations can still be nonadditive.",
            ]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--out", required=True, type=Path, help="new JSON file outside the immutable bundle")
    parser.add_argument("--relative-tolerance", type=float, default=0.05)
    parser.add_argument("--absolute-tolerance", type=float, default=0.0, help="counts/s per native pixel")
    parser.add_argument("--uncertainty-multiplier", type=float, default=3.0)
    parser.add_argument("--wavelength-range", type=float, nargs=2, default=(400, 700), metavar=("MIN", "MAX"))
    args = parser.parse_args(argv)
    if args.out.resolve().is_relative_to(args.bundle.resolve()):
        parser.error("analysis output must be outside the immutable capture bundle")
    try:
        report = analyze_bundle(args.bundle, relative_tolerance=args.relative_tolerance,
                                absolute_tolerance=args.absolute_tolerance,
                                uncertainty_multiplier=args.uncertainty_multiplier,
                                wavelength_range=args.wavelength_range)
        with args.out.open("x") as f:
            json.dump(report, f, indent=2, allow_nan=False)
            f.write("\n")
    except (ValueError, KeyError, OSError) as exc:
        print(f"Analysis unavailable: {exc}", file=sys.stderr)
        return 2
    print(f"Analysis written: {args.out} ({len(report['groups'])} condition groups)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
