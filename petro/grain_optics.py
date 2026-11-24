"""Repeatable intensity summaries inside annotated and masked mineral grains."""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from scipy import ndimage

from .analysis import channel_stats, color_channel
from .domain import InputError, Vec, field, polygon
from .geometry import local_array_coordinates, roi_slices
from .raster import encode_uint8

GRAIN_BUDGET = 2_500_000
RADIAL_LIMIT = 9
PHASE_FEATURES = ("luminance", "saturation", "red", "green", "blue")


@dataclass(frozen=True)
class GrainWindow:
    image: np.ndarray
    outline: np.ndarray
    in_field: np.ndarray
    valid: np.ndarray
    distance: np.ndarray
    rows: slice
    cols: slice

    def coordinates(self, half=True):
        add = 0.5 if half else 0.0
        cols = np.arange(self.cols.start, self.cols.stop, dtype=np.float32)[None, :] + add
        rows = np.arange(self.rows.start, self.rows.stop, dtype=np.float32)[:, None] + add
        return np.broadcast_arrays(cols, rows)

    def box(self):
        return {
            "x": self.cols.start,
            "y": self.rows.start,
            "width": self.cols.stop - self.cols.start,
            "height": self.rows.stop - self.rows.start,
        }


def _bounds(vertices, dimensions, extra=4):
    w, h = dimensions
    a = max(0, math.floor(min(vertex.x for vertex in vertices)) - extra)
    b = min(w, math.ceil(max(vertex.x for vertex in vertices)) + extra)
    c = max(0, math.floor(min(vertex.y for vertex in vertices)) - extra)
    d = min(h, math.ceil(max(vertex.y for vertex in vertices)) + extra)
    if (b - a) * (d - c) > GRAIN_BUDGET:
        raise InputError("矿粒要分析的图像范围过大")
    return slice(c, d), slice(a, b)


def _interior(vertices, rows, cols):
    xs = np.arange(cols.start, cols.stop, dtype=np.float64)[None, :] + 0.5
    ys = np.arange(rows.start, rows.stop, dtype=np.float64)[:, None] + 0.5
    result = np.zeros((len(ys), xs.size), dtype=bool)
    for a, b in zip(vertices, vertices[1:] + vertices[:1]):
        rise = b.y - a.y
        if abs(rise) < 1e-10:
            continue
        crosses = ((a.y > ys) != (b.y > ys)) & (xs < a.x + (ys - a.y) * (b.x - a.x) / rise)
        result ^= crosses
    return result


def _rect_raster(shape, rows, cols):
    u, v = local_array_coordinates(shape, rows, cols)
    return (u >= 0) & (u < shape.width) & (v >= 0) & (v < shape.height)


def _masks(state, vertices, dimensions):
    rows, cols = _bounds(vertices, dimensions)
    outline = _interior(vertices, rows, cols)
    if not outline.any():
        raise InputError("这个矿粒轮廓内没有像素中心")
    in_field = outline & _rect_raster(field(state["field"]), rows, cols)
    valid = in_field.copy()
    for item in state["defects"]:
        box = field(item["shape"])
        horizontal, vertical = roi_slices(box, dimensions)[::-1]
        if horizontal.stop <= cols.start or horizontal.start >= cols.stop:
            continue
        if vertical.stop <= rows.start or vertical.start >= rows.stop:
            continue
        valid &= ~_rect_raster(box, rows, cols)
    return rows, cols, outline, in_field, valid


def grain_window(image, state, grain):
    dimensions = image.shape[1], image.shape[0]
    vertices = polygon(grain["vertices"], dimensions)
    rows, cols, outline, in_field, valid = _masks(state, vertices, dimensions)
    distance = ndimage.distance_transform_edt(np.pad(outline, 1))[1:-1, 1:-1]
    return GrainWindow(image[rows, cols], outline, in_field, valid, distance, rows, cols)


def _radius_zones(window, bands):
    largest = float(window.distance.max())
    if largest <= 0:
        raise InputError("矿粒轮廓不足以分隔内缘")
    boundaries = np.linspace(0, largest, min(RADIAL_LIMIT, bands) + 1)
    labels = np.clip(np.searchsorted(boundaries, window.distance, side="right") - 1, 0, len(boundaries) - 2)
    labels[~window.outline] = -1
    return labels, boundaries


def _quadrants(window, centroid, angle):
    xx, yy = window.coordinates()
    theta = math.radians(angle)
    cos, sin = math.cos(theta), math.sin(theta)
    du = xx - centroid["x"]
    dv = yy - centroid["y"]
    projection_u = du * cos + dv * sin
    projection_v = -du * sin + dv * cos
    quadrants = np.where(projection_v >= 0, 2, 0) + np.where(projection_u >= 0, 1, 0)
    quadrants[~window.outline] = -1
    return quadrants


def _patch_summary(data, raw, allowed, physical):
    area = int(np.count_nonzero(raw))
    sampled = int(np.count_nonzero(allowed))
    selected = data[allowed]
    return {
        "outline_pixels": area,
        "sampled_pixels": sampled,
        "selected_um2": sampled * physical ** 2 if physical is not None else None,
        "coverage_percent": sampled * 100 / area if area else None,
        "stats": channel_stats(selected),
    }


def _median_rgb(data, valid):
    if not np.any(valid):
        return None
    rgb = np.median(data[valid], axis=0)
    return [round(float(value), 3) for value in rgb]


def _region_density(window, values, zones, radial, bins, physical):
    result = []
    for i in range(len(bins) - 1):
        raw = zones == i
        accepted = raw & window.valid
        record = _patch_summary(values, raw, accepted, physical)
        record.update({
            "shell": i + 1,
            "from_px": float(bins[i]),
            "to_px": float(bins[i + 1]),
            "reference_rgb": _median_rgb(window.image, accepted),
            "maximum_clearance": float(np.max(radial[accepted])) if accepted.any() else None,
        })
        result.append(record)
    return result


def _balance_report(window, values, grain, physical):
    geometry = grain["metrics"]
    angle = geometry["long_axis_deg"]
    quadrants = _quadrants(window, geometry["centroid"], angle)
    areas = []
    labels = ("-U / -V", "+U / -V", "-U / +V", "+U / +V")
    for i, label in enumerate(labels):
        outer = quadrants == i
        usable = window.valid & outer
        stats = _patch_summary(values, outer, usable, physical)
        stats["quadrant"] = label
        areas.append(stats)
    finite = [item["stats"]["median"] for item in areas if item["stats"]]
    return {
        "bearing_deg": angle,
        "quadrants": areas,
        "median_span": max(finite) - min(finite) if len(finite) >= 2 else None,
    }


def _representative_pixel(values, valid, window):
    if not valid.any():
        return None
    median = float(np.median(values[valid]))
    y, x = np.nonzero(valid)
    i = int(np.argmin(np.abs(values[y, x] - median)))
    return {
        "source_x": float(x[i] + window.cols.start + 0.5),
        "source_y": float(y[i] + window.rows.start + 0.5),
        "value": float(values[y[i], x[i]]),
    }


def _contrast_reading(inner, outside, minimum=5):
    under = inner["sampled_pixels"]
    near = outside["sampled_pixels"]
    if under < minimum or near < minimum:
        return None
    mid_inner = inner["stats"]["median"]
    mid_outer = outside["stats"]["median"]
    combined = mid_inner + mid_outer
    return {
        "inner_minus_outer": mid_inner - mid_outer,
        "relative_contrast": (mid_inner - mid_outer) / combined if combined > 1e-7 else None,
        "inner_mean": inner["stats"]["mean"],
        "rim_mean": outside["stats"]["mean"],
    }


def _joint_mask(first, second, valid):
    data = (second - first)[valid]
    above = int(np.count_nonzero(data > 3))
    below = int(np.count_nonzero(data < -3))
    return {
        "difference": channel_stats(data),
        "brighter": above,
        "darker": below,
        "similar": int(data.size) - above - below,
    }


def _color_sections(window, sector, gradient):
    contour = window.outline & ~ndimage.binary_erosion(window.outline, border_value=0)
    steel = np.array([36, 52, 65], dtype=np.float32)
    muted = np.array([101, 66, 68], dtype=np.float32)
    target = window.image.astype(np.float32) * 0.45 + steel * 0.30
    keep = window.valid
    values = np.linspace(0.0, 1.0, gradient)
    stops = np.stack((184 - 92 * values, 155 + 55 * values, 86 + 88 * values), axis=1)
    indexed = stops[np.maximum(sector, 0)]
    target[keep] = window.image[keep] * 0.57 + indexed[keep] * 0.43
    rejected = window.outline & ~keep
    target[rejected] = 0.36 * window.image[rejected] + 0.64 * muted
    target[contour] = (226, 232, 194)
    return encode_uint8(target)


def inspect_grain(image, state, grain, channel, rings, reference=None):
    window = grain_window(image, state, grain)
    calibration = state["scale"]
    units = calibration["um_per_px"] if calibration is not None else None
    values = color_channel(window.image, channel)
    indices, bands = _radius_zones(window, rings)
    parts = _region_density(window, values, indices, window.distance, bands, units)
    name = grain["name"]
    result = {
        "id": grain["id"],
        "name": name,
        "class": grain["mineral"],
        "channel": channel,
        "source_box": window.box(),
        "outline_pixels": int(np.count_nonzero(window.outline)),
        "inside_field": int(np.count_nonzero(window.in_field)),
        "masked_inside_field": int(np.count_nonzero(window.in_field & ~window.valid)),
        "region": _patch_summary(values, window.outline, window.valid, units),
        "shells": parts,
        "shell_contrast": _contrast_reading(parts[-1], parts[0]),
        "axes": _balance_report(window, values, grain, units),
        "source_pixel": _representative_pixel(values, window.valid, window),
        "comparison": None,
    }
    if reference is not None:
        alternate = color_channel(reference[window.rows, window.cols], channel)
        result["comparison"] = _joint_mask(values, alternate, window.valid)
    return result, _color_sections(window, indices, len(bands) - 1)
