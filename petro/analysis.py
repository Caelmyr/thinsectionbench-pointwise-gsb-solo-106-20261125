"""Measurement engines for fields, line profiles, components and optical texture."""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from scipy import ndimage

from .domain import InputError, field, segment
from .geometry import bilinear_values, line_coordinates, rectangle_slice_mask, segmentation_masks
from .raster import LUMA_WEIGHTS, encode_uint8, heat_renderer, luminance, normalized_array

CHANNELS = {"luminance", "red", "green", "blue", "saturation"}
COMPARISON_LIMIT = 2_000_000
TEXTURE_LIMIT = 3_000_000
MAX_COMPONENTS = 300
SPECTRAL_MIN_FRAMES = 3
SPECTRAL_MIN_SEPARATION = 4.0


@dataclass(frozen=True)
class ActiveField:
    image: np.ndarray
    selected: np.ndarray
    usable: np.ndarray
    x: int
    y: int

    @property
    def width(self):
        return self.image.shape[1]

    @property
    def height(self):
        return self.image.shape[0]

    @property
    def dimensions(self):
        return self.width, self.height

    def source_box(self):
        return {
            "x": self.x,
            "y": self.y,
            "width": self.width,
            "height": self.height,
        }


def active_field(image, state, maximum=None):
    dims = (image.shape[1], image.shape[0])
    rows, cols, selected = rectangle_slice_mask(field(state["field"]), dims)
    w = cols.stop - cols.start
    h = rows.stop - rows.start
    if not selected.any():
        raise InputError("视域中没有原图像素")
    if maximum is not None and w * h > maximum:
        raise InputError(f"这项分析需要小于 {maximum / 1_000_000:g} MP 的视域，请缩小视域")
    usable = selected.copy()
    for item in state["defects"]:
        hazard = field(item["shape"])
        u, v = hazard.axes
        xs = np.arange(cols.start, cols.stop, dtype=np.float32)[None, :] + (0.5 - hazard.origin.x)
        ys = np.arange(rows.start, rows.stop, dtype=np.float32)[:, None] + (0.5 - hazard.origin.y)
        local_u = xs * u.x + ys * u.y
        local_v = xs * v.x + ys * v.y
        overlaps = (
            (local_u >= 0) & (local_u < hazard.width)
            & (local_v >= 0) & (local_v < hazard.height)
        )
        usable &= ~overlaps
    if not usable.any():
        raise InputError("废区覆盖了视域中的全部像素")
    return ActiveField(image[rows, cols], selected, usable, cols.start, rows.start)


def color_channel(image, name):
    if name == "luminance":
        return luminance(image)
    if name == "saturation":
        largest = np.max(image, axis=-1).astype(np.float32)
        smallest = np.min(image, axis=-1).astype(np.float32)
        return 255 * np.divide(
            largest - smallest, largest, out=np.zeros_like(largest), where=largest > 0
        )
    return image[..., {"red": 0, "green": 1, "blue": 2}[name]].astype(np.float32)


def channel_stats(values):
    values = np.asarray(values)
    if not values.size:
        return None
    low, middle, high = np.percentile(values, [5, 50, 95])
    return {
        "min": float(values.min()),
        "max": float(values.max()),
        "mean": float(np.mean(values, dtype=np.float64)),
        "stddev": float(np.std(values, dtype=np.float64)),
        "p05": float(low),
        "median": float(middle),
        "p95": float(high),
    }


def _band_summary(values, valid, rows):
    size = values.shape[0] if rows else values.shape[1]
    chunks = np.array_split(np.arange(size), min(8, size))
    measured = []
    for indexes in chunks:
        index = slice(int(indexes[0]), int(indexes[-1]) + 1)
        selected = values[index] if rows else values[:, index]
        mask = valid[index] if rows else valid[:, index]
        samples = selected[mask]
        measured.append({
            "from": index.start,
            "to": index.stop,
            "count": int(samples.size),
            "mean": float(samples.mean()) if samples.size else None,
        })
    return measured


def histogram(image, state, channel="luminance", bins=64):
    patch = active_field(image, state)
    values = color_channel(patch.image, channel)
    chosen = values[patch.usable]
    hist, edges = np.histogram(chosen, bins=bins, range=(0, 256))
    return {
        "channel": channel,
        "histogram": [
            {"from": float(a), "to": float(b), "count": int(count)}
            for a, b, count in zip(edges, edges[1:], hist)
        ],
        "statistics": channel_stats(chosen),
        "low_clipped": float(np.count_nonzero(chosen <= 0) / chosen.size),
        "high_clipped": float(np.count_nonzero(chosen >= 255) / chosen.size),
        "pixels": int(chosen.size),
        "row_bands": _band_summary(values, patch.usable, True),
        "column_bands": _band_summary(values, patch.usable, False),
        "source_box": patch.source_box(),
    }


def valid_at(state, x, y):
    base = field(state["field"])
    u, v = base.axes
    dx = x - base.origin.x
    dy = y - base.origin.y
    local_u = u.x * dx + u.y * dy
    local_v = v.x * dx + v.y * dy
    valid = (0 <= local_u) & (local_u < base.width) & (0 <= local_v) & (local_v < base.height)
    for item in state["defects"]:
        hazard = field(item["shape"])
        hu, hv = hazard.axes
        qx = x - hazard.origin.x
        qy = y - hazard.origin.y
        col = hu.x * qx + hu.y * qy
        row = hv.x * qx + hv.y * qy
        valid &= ~((0 <= col) & (col < hazard.width) & (0 <= row) & (row < hazard.height))
    return valid


def smooth_profile(values, valid, radius):
    if radius == 0:
        return values
    weights = valid.astype(np.float64)
    size = 2 * radius + 1
    total = ndimage.uniform_filter1d(values * weights, size=size, mode="constant")
    count = ndimage.uniform_filter1d(weights, size=size, mode="constant")
    return np.divide(total, count, out=values.copy(), where=count > 1e-9)


def profile(image, state, channel, spacing=1.0, smoothing=0, mask=True):
    line = segment(state["transect"])
    t, x, y = line_coordinates(line, spacing)
    rgb = bilinear_values(image, x, y)
    if channel == "luminance":
        values = rgb @ LUMA_WEIGHTS
    else:
        values = color_channel(rgb, channel)
    valid = valid_at(state, x, y) if mask else np.ones(len(t), dtype=bool)
    values = smooth_profile(values, valid, smoothing)
    scale = state["scale"]
    factor = scale["um_per_px"] if scale else None
    samples = []
    for f, px, py, value, accepted in zip(t, x, y, values, valid):
        distance = float(f * line.length)
        samples.append({
            "x": float(px),
            "y": float(py),
            "distance_px": distance,
            "distance_um": distance * factor if factor is not None else None,
            "value": float(value) if accepted else None,
            "masked": not bool(accepted),
        })
    return {
        "channel": channel,
        "spacing_px": line.length / (len(t) - 1),
        "length_px": line.length,
        "length_um": line.length * factor if factor is not None else None,
        "angle": line.bearing,
        "valid_samples": int(np.count_nonzero(valid)),
        "samples": samples,
    }


def _component_contour(component):
    # Four-neighbor exposed edges avoid a diagonal area inflation.
    occupied = np.pad(component, ((1, 1), (1, 1)))
    core = occupied[1:-1, 1:-1]
    return int(
        np.count_nonzero(core & ~occupied[:-2, 1:-1])
        + np.count_nonzero(core & ~occupied[2:, 1:-1])
        + np.count_nonzero(core & ~occupied[1:-1, :-2])
        + np.count_nonzero(core & ~occupied[1:-1, 2:])
    )


def _label_shapes(labels, patch, minimum, calibration):
    sizes = np.bincount(labels.reshape(-1))
    objects = ndimage.find_objects(labels)
    order = sorted(
        (identifier for identifier in range(1, len(sizes)) if sizes[identifier] >= minimum),
        key=lambda identifier: (-sizes[identifier], identifier),
    )
    keep = np.zeros(len(sizes), dtype=bool)
    keep[order] = True
    boundaries = patch.usable & ~ndimage.binary_erosion(patch.usable, border_value=0)
    entries = []
    for identifier in order[:MAX_COMPONENTS]:
        rows, cols = objects[identifier - 1]
        component = labels[rows, cols] == identifier
        ys, xs = np.nonzero(component)
        area = int(sizes[identifier])
        edge = bool(np.any(component & boundaries[rows, cols]))
        perimeter = _component_contour(component)
        width, height = cols.stop - cols.start, rows.stop - rows.start
        stats = {
            "label": identifier,
            "area_px2": area,
            "perimeter_px": perimeter,
            "area_um2": area * calibration ** 2 if calibration else None,
            "centroid": {
                "x": float(xs.mean() + cols.start + patch.x + 0.5),
                "y": float(ys.mean() + rows.start + patch.y + 0.5),
            },
            "bbox": {
                "x": cols.start + patch.x,
                "y": rows.start + patch.y,
                "width": width,
                "height": height,
            },
            "compactness": float(4 * math.pi * area / perimeter ** 2) if perimeter else None,
            "on_edge": edge,
        }
        entries.append(stats)
    return entries, keep[labels], len(order)


def threshold_components(image, state, channel, limit, polarity, minimum, cleanup, connectivity):
    patch = active_field(image, state, TEXTURE_LIMIT)
    values = color_channel(patch.image, channel)
    selected = values <= limit if polarity == "dark" else values >= limit
    selected &= patch.usable
    kernel = ndimage.generate_binary_structure(2, connectivity)
    if cleanup:
        selected = ndimage.binary_opening(selected, structure=kernel, iterations=cleanup)
        selected &= patch.usable
    labels, n = ndimage.label(selected, structure=kernel)
    scale = state["scale"]
    entries, kept, count = _label_shapes(labels, patch, minimum, scale["um_per_px"] if scale else None)
    valid_count = int(np.count_nonzero(patch.usable))
    selected_count = int(np.count_nonzero(kept))
    report = {
        "source_box": patch.source_box(),
        "channel": channel,
        "threshold": limit,
        "polarity": polarity,
        "component_count": count,
        "removed_components": n - count,
        "reported_components": len(entries),
        "usable_pixels": valid_count,
        "selected_pixels": selected_count,
        "coverage_percent": selected_count * 100 / valid_count,
        "components": entries,
    }
    return report, kept, patch


def component_image(patch, selected):
    dark = patch.image.astype(np.float32) * 0.48
    result = dark.copy()
    result[~patch.selected] = [17, 24, 30]
    result[patch.selected & ~patch.usable] = [87, 47, 52]
    result[selected] = 0.4 * patch.image[selected] + 0.6 * np.array([98, 217, 178], dtype=np.float32)
    return encode_uint8(result)


def _supported_fraction(valid, sigma):
    return ndimage.gaussian_filter(valid.astype(np.float32), sigma, mode="constant")


def _weighted_smooth(values, valid, sigma):
    nearby = _supported_fraction(valid, sigma)
    total = ndimage.gaussian_filter(values * valid, sigma, mode="constant")
    return np.divide(total, nearby, out=np.zeros_like(total), where=nearby > 1e-5), nearby


def focus_map(image, state, sigma):
    patch = active_field(image, state, TEXTURE_LIMIT)
    gray = luminance(patch.image)
    smooth = ndimage.gaussian_filter(gray, 0.65, mode="nearest")
    dx = ndimage.sobel(smooth, axis=1, mode="nearest") / 8
    dy = ndimage.sobel(smooth, axis=0, mode="nearest") / 8
    support = ndimage.binary_erosion(patch.usable, border_value=0)
    energy, valid_fraction = _weighted_smooth(dx * dx + dy * dy, support, sigma)
    interpreted = patch.usable & (valid_fraction >= 0.5)
    visible = np.sqrt(np.maximum(energy, 0))
    colors, ceiling = heat_renderer(visible, interpreted)
    report = {
        "source_box": patch.source_box(),
        "window_sigma_px": sigma,
        "gradient": channel_stats(visible[interpreted]),
        "interpreted_pixels": int(np.count_nonzero(interpreted)),
        "display_ceiling": ceiling,
        "rows": _band_summary(visible, interpreted, True),
        "columns": _band_summary(visible, interpreted, False),
    }
    return report, colors


def tensor_texture(image, state, sigma, minimum, bins):
    patch = active_field(image, state, TEXTURE_LIMIT)
    gray = luminance(patch.image) / 255
    smooth = ndimage.gaussian_filter(gray, 0.8, mode="nearest")
    dx = ndimage.sobel(smooth, axis=1, mode="nearest") / 8
    dy = ndimage.sobel(smooth, axis=0, mode="nearest") / 8
    support = ndimage.binary_erosion(patch.usable, border_value=0)
    xx, denominator = _weighted_smooth(dx * dx, support, sigma)
    yy, _ = _weighted_smooth(dy * dy, support, sigma)
    xy, _ = _weighted_smooth(dx * dy, support, sigma)
    trace = xx + yy
    difference = xx - yy
    anisotropy = np.hypot(difference, 2 * xy)
    coherence = np.divide(anisotropy, trace, out=np.zeros_like(trace), where=trace > 1e-8)
    angle = (np.degrees(np.arctan2(2 * xy, difference)) / 2 + 90) % 180
    usable = patch.usable & (denominator >= 0.5) & (coherence >= minimum) & (trace > 1e-6)
    measured = angle[usable]
    strengths = coherence[usable]
    histogram, edges = np.histogram(measured, bins=bins, range=(0, 180), weights=strengths)
    vector = complex(np.sum(np.exp(1j * np.deg2rad(2 * measured)) * strengths))
    weight_total = float(np.sum(strengths))
    directional_strength = abs(vector) / weight_total if weight_total > 0 else 0
    report = {
        "source_box": patch.source_box(),
        "window_sigma_px": sigma,
        "min_coherence": minimum,
        "acceptance_percent": 100 * measured.size / np.count_nonzero(patch.usable),
        "sample_count": int(measured.size),
        "preferred_deg": math.degrees(np.angle(vector) / 2) % 180 if directional_strength > 1e-7 else None,
        "strength": directional_strength,
        "bins": [
            {"from": float(a), "to": float(b), "weight": float(w)}
            for a, b, w in zip(edges, edges[1:], histogram)
        ],
    }
    hue = np.deg2rad(2 * angle)
    saturation = np.clip(coherence, 0, 1)
    red = 0.54 + 0.31 * saturation * np.cos(hue)
    green = 0.55 + 0.32 * saturation * np.cos(hue - 2.09)
    blue = 0.56 + 0.33 * saturation * np.cos(hue - 4.19)
    mapped = encode_uint8(255 * np.stack((red, green, blue), axis=2))
    mapped[~usable] = [22, 29, 38]
    return report, mapped


def compare_optics(first, second, state):
    patch = active_field(first, state, COMPARISON_LIMIT)
    other = second[patch.y:patch.y + patch.height, patch.x:patch.x + patch.width]
    first_gray = luminance(patch.image)
    second_gray = luminance(other)
    valid = patch.usable
    a, b = first_gray[valid], second_gray[valid]
    a_centered = a - a.mean()
    b_centered = b - b.mean()
    denominator = float(np.linalg.norm(a_centered) * np.linalg.norm(b_centered))
    delta = b - a
    return {
        "source_box": patch.source_box(),
        "pixels": int(delta.size),
        "original": channel_stats(a),
        "reference": channel_stats(b),
        "difference": channel_stats(delta),
        "mean_absolute_difference": float(np.mean(np.abs(delta), dtype=np.float64)),
        "brighter_percent": 100 * np.count_nonzero(delta > 2) / delta.size,
        "darker_percent": 100 * np.count_nonzero(delta < -2) / delta.size,
        "correlation": float(np.dot(a_centered, b_centered) / denominator) if denominator > 1e-8 else None,
    }


def density_sequence(paths, reference_image, state, mode):
    if not paths:
        raise InputError("请先加入同视域的偏光序列")
    patch = active_field(reference_image, state, COMPARISON_LIMIT)
    if mode == "grain":
        if not state["grains"]:
            raise InputError("请先在矿粒台圈出要测的轮廓")
        from PIL import Image, ImageDraw
        vertices = state["grains"][-1]["vertices"]
        bitmap = Image.new("1", patch.dimensions)
        draw = ImageDraw.Draw(bitmap)
        draw.polygon([(p["x"] - patch.x - 0.5, p["y"] - patch.y - 0.5)
                      for p in vertices], fill=1)
        included = patch.usable & np.asarray(bitmap, dtype=bool)
        scope = state["grains"][-1]["name"]
    else:
        included = patch.usable
        scope = "当前可用视域"
    if not included.any():
        raise InputError("所选矿粒在当前视域中没有可用像素")
    entries = []
    for item, path in paths:
        data = normalized_array(path)
        local = data[patch.y:patch.y + patch.height, patch.x:patch.x + patch.width]
        gray = luminance(local)[included]
        stats = channel_stats(gray)
        entries.append({
            "name": item["name"],
            "id": item["id"],
            "angle": item["angle"],
            "samples": stats,
            "clipped_dark": int(np.count_nonzero(gray <= 0)),
            "clipped_light": int(np.count_nonzero(gray >= 255)),
        })
    entries.sort(key=lambda entry: (entry["angle"], entry["id"]))
    means = np.array([entry["samples"]["mean"] for entry in entries])
    return {
        "scope": scope,
        "pixels_per_frame": int(np.count_nonzero(included)),
        "source_box": patch.source_box(),
        "values": entries,
        "range": float(means.max() - means.min()),
        "darkest_recorded": min(entries, key=lambda entry: entry["samples"]["mean"])["angle"],
        "brightest_recorded": max(entries, key=lambda entry: entry["samples"]["mean"])["angle"],
    }


def grade_lighting(image, state, tiles=4):
    patch = active_field(image, state, TEXTURE_LIMIT)
    gray = luminance(patch.image)
    rows = np.array_split(np.arange(patch.height), min(patch.height, tiles))
    columns = np.array_split(np.arange(patch.width), min(patch.width, tiles))
    entries = []
    for r, ys in enumerate(rows):
        for c, xs in enumerate(columns):
            y = slice(int(ys[0]), int(ys[-1]) + 1)
            x = slice(int(xs[0]), int(xs[-1]) + 1)
            valid = patch.usable[y, x]
            samples = gray[y, x][valid]
            entry = {
                "column": c,
                "row": r,
                "x": patch.x + x.start,
                "y": patch.y + y.start,
                "width": x.stop - x.start,
                "height": y.stop - y.start,
                "pixels": int(samples.size),
                "coverage_percent": 100 * samples.size / valid.size,
                "median": float(np.median(samples)) if samples.size else None,
            }
            entries.append(entry)
    usable = [entry["median"] for entry in entries if entry["coverage_percent"] >= 40]
    return {
        "source_box": patch.source_box(),
        "tiles": entries,
        "comparable": len(usable),
        "spread": max(usable) - min(usable) if len(usable) >= 2 else None,
    }


def analyze_pixel_pair(first, second, x, y, second_x=None, second_y=None):
    rx = int(x) if second_x is None else int(second_x)
    ry = int(y) if second_y is None else int(second_y)
    a = first[int(y), int(x)].astype(np.int16)
    b = second[ry, rx].astype(np.int16)
    return {
        "primary": a.tolist(),
        "reference": b.tolist(),
        "delta": (b - a).tolist(),
        "luminance_delta": float(np.dot(b - a, LUMA_WEIGHTS)),
        "x": int(x),
        "y": int(y),
        "reference_x": rx,
        "reference_y": ry,
        "in_frame": True,
    }
