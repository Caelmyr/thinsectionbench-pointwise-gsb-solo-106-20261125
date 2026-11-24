"""Anisotropic pixel-size calibration from several physical ruler segments.

Micrographs can have a different physical size per horizontal and per
vertical pixel. A single ruler, or several rulers lying along the same
direction family, cannot separate the two pixel sizes.  The solver here
only accepts an anisotropic fit when the recorded rulers span both axis
directions (a pair of mirrored diagonals does not count as two
directions) and when at least two rulers agree within the stated
tolerance. Otherwise the caller keeps the legacy single-ruler scale.
"""
from __future__ import annotations

import math

import numpy as np

from .domain import InputError, number, object_value, segment

MAX_RULERS = 4
RULER_MIN_PX = 1.0
MIN_SCALE = 1e-6
MAX_SCALE = 1_000_000.0
# Relative singular-value spread below which a design matrix is treated
# as rank-deficient (rulers all parallel, or all sharing one direction).
RANK_RATIO = 1e-3


def ruler_record(value, dimensions, index):
    value = object_value(value, f"标尺 {index + 1}")
    line = segment(value.get("line"), f"标尺 {index + 1}", dimensions)
    if line.length < RULER_MIN_PX:
        raise InputError(f"标尺 {index + 1}需至少跨 1 个像素")
    length = number(value.get("length_um"), f"标尺 {index + 1}实长 µm", 0.001, 1_000_000)
    return {
        "id": value.get("id") if isinstance(value.get("id"), str) else f"ruler-{index + 1}",
        "line": line.json(),
        "length_um": length,
    }


def ruler_records(value, dimensions):
    if not isinstance(value, list) or not 2 <= len(value) <= MAX_RULERS:
        raise InputError(f"多段标尺需提供 2–{MAX_RULERS} 段")
    return [ruler_record(item, dimensions, i) for i, item in enumerate(value)]


def _design(records):
    """Squared-length design: |dx|*sx and |dy|*sy enter quadratically.

    Linearizing the physical model ``L^2 = dx^2*sx^2 + dy^2*sy^2`` gives
    unbiased estimates of the squared pixel sizes even when the rulers
    have different lengths, unlike a least-squares fit on the linear
    distance equation which over-weights short rulers.
    """
    rows = []
    weights = []
    responses = []
    for item in records:
        start = item["line"]["start"]
        end = item["line"]["end"]
        dx = end["x"] - start["x"]
        dy = end["y"] - start["y"]
        rows.append((dx * dx, dy * dy))
        responses.append(item["length_um"] ** 2)
        # Longer rulers are placed more accurately: weight by physical length.
        weights.append(item["length_um"])
    return (
        np.asarray(rows, dtype=np.float64),
        np.asarray(weights, dtype=np.float64),
        np.asarray(responses, dtype=np.float64),
    )


def _direction_families(records):
    """Count direction families so mirrored diagonals count as one family."""
    families = []
    for item in records:
        start = item["line"]["start"]
        end = item["line"]["end"]
        dx = end["x"] - start["x"]
        dy = end["y"] - start["y"]
        angle = math.degrees(math.atan2(abs(dy), abs(dx)))
        # Horizontal ~0°, vertical ~90°, everything else diagonal by quadrant.
        bucket = 0 if angle < 22.5 else 2 if angle > 67.5 else 1
        if bucket not in families:
            families.append(bucket)
    return families


def _solve(rows, weights, target_squared):
    """Weighted least squares for the two squared physical pixel sizes."""
    sqrt_w = np.sqrt(weights)[:, None]
    design = rows * sqrt_w
    response = target_squared * np.sqrt(weights)
    try:
        result, *_ = np.linalg.lstsq(design, response, rcond=None)
    except np.linalg.LinAlgError as exc:
        raise InputError("标尺方向不足以分开横纵每像素长度") from exc
    qx, qy = float(result[0]), float(result[1])
    if not (math.isfinite(qx) and math.isfinite(qy)) or qx <= 0 or qy <= 0:
        raise InputError("标尺数据无法得到正的横纵每像素长度")
    sx, sy = math.sqrt(qx), math.sqrt(qy)
    if not (MIN_SCALE <= sx <= MAX_SCALE and MIN_SCALE <= sy <= MAX_SCALE):
        raise InputError("拟合得到的每像素长度超出合理范围")
    return sx, sy


def _residuals(records, sx, sy):
    output = []
    for item in records:
        start = item["line"]["start"]
        end = item["line"]["end"]
        dx = end["x"] - start["x"]
        dy = end["y"] - start["y"]
        predicted = math.hypot(dx * sx, dy * sy)
        actual = item["length_um"]
        difference = predicted - actual
        output.append({
            "predicted_um": float(predicted),
            "actual_um": float(actual),
            "error_um": float(difference),
            "abs_error_um": float(abs(difference)),
            "relative_error": float(abs(difference) / actual),
            "pixel_dx": float(abs(dx)),
            "pixel_dy": float(abs(dy)),
        })
    return output


def _has_two_axes(rows):
    """Two independent directions are required to separate sx and sy."""
    singular = np.linalg.svd(rows, compute_uv=False)
    if singular[0] <= 0:
        return False
    return singular[-1] > RANK_RATIO * singular[0]


def _isotropic_factor(records):
    """Single µm/px value from the same squared-length least squares."""
    total_px2 = total_um2 = 0.0
    for item in records:
        start = item["line"]["start"]
        end = item["line"]["end"]
        px2 = (end["x"] - start["x"]) ** 2 + (end["y"] - start["y"]) ** 2
        total_px2 += px2
        total_um2 += item["length_um"] ** 2
    return math.sqrt(total_um2 / total_px2) if total_px2 > 0 else None


def fit_calibration(records, tolerance_um):
    """Enumerate rank-sufficient ruler subsets and rank the resulting fits.

    Every candidate must separate the horizontal and vertical pixel
    sizes. Candidates are ranked by the number of rulers whose residual
    is inside ``tolerance_um`` and, on ties, by the length-weighted RMS
    deviation so long-ruler agreement decides between equal fits.
    """
    tolerance = number(tolerance_um, "可接受实长误差 µm", 0.0, 1_000_000)
    rows, weights, target_squared = _design(records)
    full_rank = _has_two_axes(rows)
    families = _direction_families(records)

    candidates = []
    seen = set()

    def add_candidate(indexes):
        key = tuple(sorted(indexes))
        if key in seen:
            return
        subset_rows = rows[list(key)]
        if not _has_two_axes(subset_rows):
            return
        seen.add(key)
        sx, sy = _solve(subset_rows, weights[list(key)], target_squared[list(key)])
        residuals = _residuals(records, sx, sy)
        within = [bool(r["abs_error_um"] <= tolerance) for r in residuals]
        weighted_rms = math.sqrt(
            sum(w * r["abs_error_um"] ** 2 for w, r in zip(weights, residuals)) / sum(weights)
        )
        # Longest ruler deviation as a secondary, transparent tie-breaker.
        longest = max(
            (r["abs_error_um"] for r in residuals),
            default=0.0,
        )
        candidates.append({
            "subset": list(key),
            "um_per_px_x": float(sx),
            "um_per_px_y": float(sy),
            "residuals": residuals,
            "within_tolerance": within,
            "within_count": int(sum(within)),
            "weighted_rms_um": float(weighted_rms),
            "longest_ruler_error_um": float(longest),
            "anisotropy": float(max(sx, sy) / min(sx, sy)),
        })

    for size in (2, 3):
        if len(records) < size:
            continue
        for combo in _combinations(range(len(records)), size):
            add_candidate(combo)
    add_candidate(tuple(range(len(records))))

    candidates.sort(key=lambda item: (
        -item["within_count"], item["weighted_rms_um"], item["longest_ruler_error_um"],
    ))

    iso_factor = _isotropic_factor(records)
    iso_residuals = _residuals(records, iso_factor, iso_factor)
    iso_within = [bool(r["abs_error_um"] <= tolerance) for r in iso_residuals]
    isotropic = {
        "um_per_px": float(iso_factor),
        "residuals": iso_residuals,
        "within_tolerance": iso_within,
        "within_count": int(sum(iso_within)),
        "weighted_rms_um": float(math.sqrt(
            sum(w * r["abs_error_um"] ** 2 for w, r in zip(weights, iso_residuals))
            / sum(weights)
        )),
    }

    accepted = bool(candidates) and candidates[0]["within_count"] >= 2
    recommendation = candidates[0] if accepted else None
    if not accepted:
        if not full_rank:
            message = "标尺只覆盖同一方向族（仅正负镜像的斜线不算横纵两类），无法分开横纵比例，已保留原标尺模式。"
        else:
            message = "没有任何一组拟合能让至少两段标尺落在误差内，已保留原标尺模式。"
    else:
        message = (
            f"建议采用横 {recommendation['um_per_px_x']:.4f}、纵 {recommendation['um_per_px_y']:.4f} µm/px，"
            f"{recommendation['within_count']} 段标尺落在误差内。"
        )

    return {
        "accepted": accepted,
        "full_rank": bool(full_rank),
        "direction_families": [int(item) for item in families],
        "tolerance_um": float(tolerance),
        "candidates": candidates,
        "recommendation": recommendation,
        "isotropic": isotropic,
        "message": message,
    }


def _combinations(values, size):
    values = list(values)
    if size == 2:
        for i in range(len(values)):
            for j in range(i + 1, len(values)):
                yield (values[i], values[j])
    elif size == 3:
        for i in range(len(values)):
            for j in range(i + 1, len(values)):
                for k in range(j + 1, len(values)):
                    yield (values[i], values[j], values[k])


def scale2d(value, dimensions):
    """Validate and persist an accepted anisotropic calibration."""
    value = object_value(value, "横纵标尺")
    rulers = ruler_records(value.get("rulers"), dimensions)
    tolerance = number(value.get("tolerance_um"), "可接受实长误差 µm", 0.0, 1_000_000)
    report = fit_calibration(rulers, tolerance)
    if not report["accepted"]:
        raise InputError(report["message"])
    chosen = report["recommendation"]
    return {
        "mode": "anisotropic",
        "um_per_px_x": chosen["um_per_px_x"],
        "um_per_px_y": chosen["um_per_px_y"],
        "tolerance_um": tolerance,
        "rulers": rulers,
        "fit": {
            "within_count": chosen["within_count"],
            "weighted_rms_um": chosen["weighted_rms_um"],
        },
    }


def calibration_axes(scale):
    """Return (sx, sy) physical µm per pixel for either scale schema."""
    if scale is None:
        return None, None
    if scale.get("mode") == "anisotropic":
        return scale["um_per_px_x"], scale["um_per_px_y"]
    factor = scale["um_per_px"]
    return factor, factor


def isotropic_factor(scale):
    if scale is None:
        return None
    if scale.get("mode") == "anisotropic":
        return math.sqrt(scale["um_per_px_x"] * scale["um_per_px_y"])
    return scale["um_per_px"]


def area_factor(scale):
    if scale is None:
        return None
    if scale.get("mode") == "anisotropic":
        return scale["um_per_px_x"] * scale["um_per_px_y"]
    return scale["um_per_px"] ** 2


def segment_length_um(specification, scale):
    if scale is None:
        return None
    line = specification if "start" in specification else specification["line"]
    sx, sy = calibration_axes(scale)
    dx = line["end"]["x"] - line["start"]["x"]
    dy = line["end"]["y"] - line["start"]["y"]
    return math.hypot(dx * sx, dy * sy)
