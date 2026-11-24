"""Integer-pixel translation suggestions for a shifted PPL reference photo.

Coordinate convention
---------------------
The primary photo is the cross-polarized (XPL / 交偏光) original and keeps
its own pixel coordinates.  An adopted translation ``(dx, dy)`` means:

    reference_original_x = x + dx
    reference_original_y = y + dy

for the same physical feature clicked at primary coordinate ``(x, y)``.
Equivalently the reference is drawn shifted by ``(-dx, -dy)`` into the
primary coordinate frame, sampling ``reference[y + dy, x + dx]``.  Sampling
never wraps and never replicates edge pixels: positions outside the
reference frame are reported as missing.

Matching uses sign-invariant gradient orientation so a grain edge that is
bright in one polarization and dark in the other still lines up.  Candidate
scores combine the number of matched boundaries with the effective,
unmasked overlap area.  The search is exhaustive at full resolution for
ordinary bench images and falls back to a deterministic pyramid for large
photos / large search radii.
"""
from __future__ import annotations

import math

import numpy as np
from scipy import ndimage

from .domain import InputError, object_value, whole
from .geometry import segmentation_masks
from .raster import luminance

DEFAULT_MAX_SHIFT = 24
MAX_SEARCH_SHIFT = 2000
MIN_COMPARE_PIXELS = 48
EDGE_TARGET = 18_000
EXHAUSTIVE_BUDGET = 100_000_000
COS2_MATCH = 0.45          # gradient directions within ~48 degrees, either polarity
NEAR_TIE_MARGIN = 0.04


def clamp_max_shift(value):
    """Validate the operator-provided per-axis search radius."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InputError("最大偏移应为整数像素")
    radius = whole(value, "最大偏移", 0, MAX_SEARCH_SHIFT)
    return radius


def adopted_shift(state):
    """Return ``(dx, dy, adopted)`` from a validated study state."""
    alignment = state.get("alignment") if isinstance(state, dict) else None
    if not isinstance(alignment, dict):
        return 0, 0, False
    try:
        dx = int(alignment.get("dx", alignment.get("x", 0)))
        dy = int(alignment.get("dy", alignment.get("y", 0)))
    except (TypeError, ValueError):
        return 0, 0, False
    if not (math.isfinite(dx) and math.isfinite(dy)):
        return 0, 0, False
    return dx, dy, True


def normalize_alignment(value, dimensions=None):
    """Accept the JSON shapes the workbench / API clients may send."""
    if value is None:
        return None
    payload = object_value(value, "平移对位")
    if "shift" in payload and isinstance(payload["shift"], (list, tuple)) and len(payload["shift"]) == 2:
        raw_dx, raw_dy = payload["shift"]
    else:
        raw_dx = next((payload[key] for key in ("dx", "x", "shift_x", "offset_x") if key in payload), 0)
        raw_dy = next((payload[key] for key in ("dy", "y", "shift_y", "offset_y") if key in payload), 0)
    bound = MAX_SEARCH_SHIFT if dimensions is None else max(MAX_SEARCH_SHIFT, max(dimensions))
    dx = whole(raw_dx, "横向平移", -bound, bound)
    dy = whole(raw_dy, "纵向平移", -bound, bound)
    return {"dx": dx, "dy": dy}


def counterpart(x, y, dx, dy, width, height):
    """Map a primary coordinate to the reference original and test coverage."""
    rx, ry = int(x) + int(dx), int(y) + int(dy)
    inside = 0 <= rx < width and 0 <= ry < height
    return {
        "primary_x": int(x),
        "primary_y": int(y),
        "reference_x": rx,
        "reference_y": ry,
        "in_frame": bool(inside),
        "missing": not inside,
    }


def overlap_slices(dx, dy, width, height):
    """Slices satisfying ``reference[y + dy, x + dx]`` inside the reference."""
    px0, px1 = max(0, -dx), min(width, width - dx)
    py0, py1 = max(0, -dy), min(height, height - dy)
    if px1 <= px0 or py1 <= py0:
        return None
    qx0, qy0 = px0 + dx, py0 + dy
    return (
        slice(py0, py1), slice(px0, px1),
        slice(qy0, qy0 + py1 - py0), slice(qx0, qx0 + px1 - px0),
    )


def overlap_area(mask, dx, dy):
    height, width = mask.shape
    slices = overlap_slices(dx, dy, width, height)
    if slices is None:
        return 0
    py, px, _, _ = slices
    return int(np.count_nonzero(mask[py, px]))


def _features(image):
    gray = luminance(image)
    smooth = ndimage.gaussian_filter(gray, 0.9, mode="reflect")
    gx = ndimage.sobel(smooth, axis=1, mode="reflect") / 8.0
    gy = ndimage.sobel(smooth, axis=0, mode="reflect") / 8.0
    magnitude = np.hypot(gx, gy)
    return gx, gy, magnitude


def _edge_threshold(magnitude, mask=None):
    values = magnitude[mask] if mask is not None else magnitude.ravel()
    values = values[np.isfinite(values)]
    if values.size < 32:
        return 10.0
    percentile = float(np.percentile(values, 92))
    return float(min(56.0, max(8.0, 0.12 * percentile)))


def _edge_sites(mask, magnitude, threshold, cap=EDGE_TARGET):
    ys, xs = np.nonzero(mask & (magnitude >= threshold))
    if len(xs) > cap:
        # Deterministic spatial-ish decimation: row-major sites evenly sampled.
        pick = np.linspace(0, len(xs) - 1, cap).astype(np.int64)
        xs, ys = xs[pick], ys[pick]
    return xs.astype(np.int64), ys.astype(np.int64)


def _score_shift(dx, dy, xs, ys, gax, gay, mag_a, gb_x, gb_y, mag_b,
                 threshold_b, valid_mask, area_zero, width, height):
    area = overlap_area(valid_mask, dx, dy)
    if area < MIN_COMPARE_PIXELS:
        return None
    qx = xs + dx
    qy = ys + dy
    inside = (qx >= 0) & (qx < width) & (qy >= 0) & (qy < height)
    sites_in = int(np.count_nonzero(inside))
    if sites_in < 8:
        return None
    ix = qx[inside]
    iy = qy[inside]
    ax = gax[ys[inside], xs[inside]]
    ay = gay[ys[inside], xs[inside]]
    a_mag = mag_a[ys[inside], xs[inside]]
    bx = gb_x[iy, ix]
    by = gb_y[iy, ix]
    b_mag = mag_b[iy, ix]
    dot = ax * bx + ay * by
    cos2 = (dot * dot) / (a_mag * a_mag * b_mag * b_mag + 4096.0)
    joint = b_mag >= threshold_b
    joint_count = int(np.count_nonzero(joint))
    matches = int(np.count_nonzero(joint & (cos2 >= COS2_MATCH)))
    mass = float(cos2[joint].sum()) if joint_count else 0.0
    agreement = mass / joint_count if joint_count else 0.0
    area_ratio = area / max(1, area_zero)
    # Raw boundary correspondence dominates; coverage mildly favours shifts
    # that keep a large usable overlap instead of a narrow aligned sliver.
    objective = (matches + 0.25 * mass) * (0.8 + 0.2 * area_ratio)
    edge_fraction = matches / max(1, sites_in)
    return {
        "dx": int(dx),
        "dy": int(dy),
        "score": round(float(objective), 3),
        "edge_matches": matches,
        "joint_edges": joint_count,
        "edge_sites": sites_in,
        "edge_fraction": round(edge_fraction, 4),
        "agreement": round(float(agreement), 4),
        "area_px": area,
        "area_fraction": round(area_ratio, 4),
    }


def _ranked(scores):
    scores.sort(key=lambda item: (
        -item["score"], -item["area_px"],
        abs(item["dx"]) + abs(item["dy"]), abs(item["dx"]), item["dx"], item["dy"],
    ))
    return scores


def _exhaustive(radius, xs, ys, gax, gay, mag_a, gbx, gby, mag_b,
                threshold_b, valid_mask, area_zero, width, height):
    return _exhaustive_rect(
        radius, radius, xs, ys, gax, gay, mag_a, gbx, gby, mag_b,
        threshold_b, valid_mask, area_zero, width, height,
    )


def _exhaustive_rect(radius_x, radius_y, xs, ys, gax, gay, mag_a, gbx, gby, mag_b,
                     threshold_b, valid_mask, area_zero, width, height):
    found = []
    for dy in range(-radius_y, radius_y + 1):
        for dx in range(-radius_x, radius_x + 1):
            result = _score_shift(
                dx, dy, xs, ys, gax, gay, mag_a, gbx, gby, mag_b,
                threshold_b, valid_mask, area_zero, width, height,
            )
            if result is not None:
                found.append(result)
    return _ranked(found)


def _downsample(image_or_mask, factor, mask=False):
    if mask:
        return image_or_mask[::factor, ::factor]
    blurred = ndimage.gaussian_filter(luminance(image_or_mask), sigma=0.7 * factor, mode="reflect")
    return blurred[::factor, ::factor]


def _scale_arrays(primary, reference, state_mask, factor):
    """Features + mask at one decimated pyramid scale."""
    if factor <= 1:
        gax, gay, ma = _features(primary)
        gbx, gby, mb = _features(reference)
        mask = state_mask
    else:
        ga = _downsample(primary, factor)
        gb = _downsample(reference, factor)
        gax, gay, ma = _gray_features(ga, already_gray=True)
        gbx, gby, mb = _gray_features(gb, already_gray=True)
        mask = _downsample(state_mask, factor, mask=True)
    height, width = ma.shape
    ta = _edge_threshold(ma, mask)
    tb = _edge_threshold(mb)
    xs, ys = _edge_sites(mask, ma, ta, cap=8000)
    return width, height, mask, xs, ys, gax, gay, ma, gbx, gby, mb, tb


def _pyramid_search(radius, xs, ys, gax, gay, mag_a, gbx, gby, mag_b,
                    threshold_b, valid_mask, area_zero, width, height,
                    primary, reference, beam=7):
    """Coarse-to-fine beam search for large radii / megapixel photos.

    Each scale keeps only the best ``beam`` shifts and expands them in a
    small neighbourhood at the next (finer) scale, so work stays bounded
    even for repeating stripes.
    """
    factor = 1
    while math.ceil(radius / (2 * factor)) >= 12 and min(width, height) // (2 * factor) >= 40:
        factor *= 2
    scales = [factor // (2 ** step) for step in range(int(math.log2(factor)) + 1)]
    scales = [max(1, value) for value in scales]

    beam_shifts = None
    last_results = []
    for scale_factor in scales:
        if scale_factor == 1:
            # Reuse the caller's exact full-resolution features and thresholds.
            sw, sh, mask = width, height, valid_mask
            sxs, sys_ = xs, ys
            sax, say, sma = gax, gay, mag_a
            sbx, sby, smb = gbx, gby, mag_b
            stb = threshold_b
            area_scale_zero = area_zero
        else:
            sw, sh, mask, sxs, sys_, sax, say, sma, sbx, sby, smb, stb = _scale_arrays(
                primary, reference, valid_mask, scale_factor,
            )
            area_scale_zero = int(np.count_nonzero(mask))
        if beam_shifts is None:
            scale_radius = min(max(1, math.ceil(radius / scale_factor) + 1), min(sw, sh) - 2)
            results = _exhaustive(
                scale_radius, sxs, sys_, sax, say, sma, sbx, sby, smb, stb,
                mask, area_scale_zero, sw, sh,
            )
        else:
            candidates = set()
            for prev_dx, prev_dy in beam_shifts:
                center_x = int(round(prev_dx / scale_factor))
                center_y = int(round(prev_dy / scale_factor))
                for oy in range(-2, 3):
                    for ox in range(-2, 3):
                        dx = center_x + ox
                        dy = center_y + oy
                        if abs(dx) <= math.ceil(radius / scale_factor) + 1:
                            candidates.add((dx, dy))
            results = []
            for dx, dy in candidates:
                item = _score_shift(
                    dx, dy, sxs, sys_, sax, say, sma, sbx, sby, smb, stb,
                    mask, area_scale_zero, sw, sh,
                )
                if item is not None:
                    results.append(item)
            results = _ranked(results)
        if not results:
            return last_results
        top = results[:beam]
        # Convert scale-pixel shifts into full-resolution coordinates for the
        # next expansion; the final scale (factor 1) keeps exact integers.
        beam_shifts = [(item["dx"] * scale_factor, item["dy"] * scale_factor) for item in top]
        last_results = results

    # Last iteration was at factor 1, so metrics are full-resolution already.
    return _ranked(last_results)


def _gray_features(gray, already_gray=False):
    if not already_gray:
        gray = luminance(gray)
    smooth = ndimage.gaussian_filter(gray, 0.9, mode="reflect")
    gx = ndimage.sobel(smooth, axis=1, mode="reflect") / 8.0
    gy = ndimage.sobel(smooth, axis=0, mode="reflect") / 8.0
    return gx, gy, np.hypot(gx, gy)


def suggest_translation(primary, reference, state, max_shift=DEFAULT_MAX_SHIFT,
                        candidate_count=6, max_shift_x=None, max_shift_y=None):
    """Return the best integer ``(dx, dy)`` mapping primary onto reference.

    See the module docstring for the coordinate convention.  ``max_shift``
    bounds both axes; ``max_shift_x`` / ``max_shift_y`` optionally override
    the per-axis search radii.
    """
    radius = clamp_max_shift(max_shift)
    radius_x = clamp_max_shift(max_shift_x) if max_shift_x is not None else radius
    radius_y = clamp_max_shift(max_shift_y) if max_shift_y is not None else radius
    if primary.shape[:2] != reference.shape[:2]:
        raise InputError("对照图必须与原图同像素尺寸")
    height, width = primary.shape[:2]
    selected, usable = segmentation_masks(state, (width, height))
    if not usable.any():
        raise InputError("视域在扣除废区后没有可比较的像素，请先调整视域或废区")
    area_zero = int(np.count_nonzero(usable))

    gax, gay, mag_a = _features(primary)
    gbx, gby, mag_b = _features(reference)
    threshold_a = _edge_threshold(mag_a, usable)
    threshold_b = _edge_threshold(mag_b, selected)
    xs, ys = _edge_sites(usable, mag_a, threshold_a)
    edge_count = len(xs)

    shift_count = (2 * radius_x + 1) * (2 * radius_y + 1)
    if edge_count and shift_count * min(edge_count, EDGE_TARGET) <= EXHAUSTIVE_BUDGET:
        ranked = _exhaustive_rect(
            radius_x, radius_y, xs, ys, gax, gay, mag_a, gbx, gby, mag_b,
            threshold_b, usable, area_zero, width, height,
        )
        method = "exhaustive-edges"
    elif radius_x == radius_y:
        ranked = _pyramid_search(
            radius_x, xs, ys, gax, gay, mag_a, gbx, gby, mag_b,
            threshold_b, usable, area_zero, width, height, primary, reference,
        )
        method = "pyramid-edges"
    else:
        # Non-square per-axis radii (API-only): exhaustive rectangular scan.
        ranked = _exhaustive_rect(
            radius_x, radius_y, xs, ys, gax, gay, mag_a, gbx, gby, mag_b,
            threshold_b, usable, area_zero, width, height,
        )
        method = "exhaustive-edges-rect"

    if not ranked:
        raise InputError("最大偏移范围内没有足够的两图共有像素，请增大最大偏移或检查照片")

    best = ranked[0]
    runner_up = ranked[1] if len(ranked) > 1 else None
    margin = (
        (best["score"] - runner_up["score"]) / best["score"]
        if runner_up is not None and best["score"] > 0 else 1.0
    )
    near_ties = sum(
        1 for item in ranked[1:]
        if best["score"] > 0 and (best["score"] - item["score"]) / best["score"] <= NEAR_TIE_MARGIN
    )
    if best["edge_matches"] < 12 or best["edge_fraction"] < 0.06:
        confidence = "low"
    elif margin <= NEAR_TIE_MARGIN or near_ties >= 3:
        confidence = "ambiguous"
    elif margin >= 0.25:
        confidence = "high"
    else:
        confidence = "medium"

    at_limit = (
        (radius_x > 0 and abs(best["dx"]) == radius_x)
        or (radius_y > 0 and abs(best["dy"]) == radius_y)
    )
    candidates = ranked[:max(1, min(12, candidate_count))]
    missing_fraction = round(1 - best["area_px"] / max(1, area_zero), 4)
    record = {
        "dx": best["dx"],
        "dy": best["dy"],
        "x": best["dx"],
        "y": best["dy"],
        "shift": [best["dx"], best["dy"]],
        "shift_x": best["dx"],
        "shift_y": best["dy"],
        "offset_x": best["dx"],
        "offset_y": best["dy"],
        # Translation that must be applied to the PPL layer itself to align
        # it onto the XPL frame (the inverse of the coordinate mapping).
        "layer_shift": [-best["dx"], -best["dy"]],
        "layer_dx": -best["dx"],
        "layer_dy": -best["dy"],
        "render_dx": -best["dx"],
        "render_dy": -best["dy"],
        "max_shift": radius,
        "max_shift_x": radius_x,
        "max_shift_y": radius_y,
        "bounds": {
            "min_dx": -radius_x, "max_dx": radius_x,
            "min_dy": -radius_y, "max_dy": radius_y,
        },
        "method": method,
        "score": best["score"],
        "confidence": confidence,
        "margin": round(float(margin), 4),
        "near_ties": near_ties,
        "at_limit": bool(at_limit),
        "edge_matches": best["edge_matches"],
        "edge_sites": best["edge_sites"],
        "edge_fraction": best["edge_fraction"],
        "agreement": best["agreement"],
        "area_px": best["area_px"],
        "field_area_px": area_zero,
        "area_fraction": best["area_fraction"],
        "missing_fraction": missing_fraction,
        "edge_sites_total": int(edge_count),
        "candidates": candidates,
        "mapping": "reference_xy = primary_xy + (dx, dy)",
    }
    # Nested aliases for clients expecting a named suggestion/best object.
    nested = {
        "dx": record["dx"], "dy": record["dy"], "x": record["x"], "y": record["y"],
        "shift": record["shift"], "shift_x": record["shift_x"], "shift_y": record["shift_y"],
        "offset_x": record["offset_x"], "offset_y": record["offset_y"],
        "layer_dx": record["layer_dx"], "layer_dy": record["layer_dy"],
        "score": record["score"], "confidence": record["confidence"],
        "edge_matches": record["edge_matches"], "area_px": record["area_px"],
        "area_fraction": record["area_fraction"], "at_limit": record["at_limit"],
        "max_shift": radius,
    }
    record["suggestion"] = nested
    record["best"] = {**best, **nested}
    return record


# Descriptive aliases for callers/tests using alternative vocabulary.
estimate_translation = suggest_translation
suggest_alignment = suggest_translation
register_pair = suggest_translation
