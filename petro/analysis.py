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
    return _histogram_payload(patch, channel, bins, patch.usable)


def _histogram_payload(patch, channel, bins, chosen):
    values = color_channel(patch.image, channel)
    sample = values[chosen]
    if not sample.size:
        raise InputError("这一范围在当前视域没有可统计的像素")
    hist, edges = np.histogram(sample, bins=bins, range=(0, 256))
    return {
        "channel": channel,
        "histogram": [
            {"from": float(a), "to": float(b), "count": int(count)}
            for a, b, count in zip(edges, edges[1:], hist)
        ],
        "statistics": channel_stats(sample),
        "low_clipped": float(np.count_nonzero(sample <= 0) / sample.size),
        "high_clipped": float(np.count_nonzero(sample >= 255) / sample.size),
        "pixels": int(sample.size),
        "row_bands": _band_summary(values, chosen, True),
        "column_bands": _band_summary(values, chosen, False),
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


def _texture_axes(patch, sigma, minimum):
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
    return angle, coherence, usable


def tensor_texture(image, state, sigma, minimum, bins, minimum_pixels=200):
    patch = active_field(image, state, TEXTURE_LIMIT)
    angle, coherence, usable = _texture_axes(patch, sigma, minimum)
    measured = angle[usable]
    strengths = coherence[usable]
    histogram, edges = np.histogram(measured, bins=bins, range=(0, 180), weights=strengths)
    vector = complex(np.sum(np.exp(1j * np.deg2rad(2 * measured)) * strengths))
    weight_total = float(np.sum(strengths))
    directional_strength = abs(vector) / weight_total if weight_total > 0 else 0
    split = _texture_split(image, state, sigma, minimum, patch=patch, angle=angle,
                           coherence=coherence, usable=usable)
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
        **_pair_section(split, minimum, minimum_pixels),
    }
    hue = np.deg2rad(2 * angle)
    saturation = np.clip(coherence, 0, 1)
    red = 0.54 + 0.31 * saturation * np.cos(hue)
    green = 0.55 + 0.32 * saturation * np.cos(hue - 2.09)
    blue = 0.56 + 0.33 * saturation * np.cos(hue - 4.19)
    mapped = encode_uint8(255 * np.stack((red, green, blue), axis=2))
    mapped[~usable] = [22, 29, 38]
    return report, mapped


TEXTURE_TOLERANCE = 15.0
TEXTURE_MIN_SEPARATION = 20.0
TEXTURE_CANDIDATE_STEP = 1.0
TEXTURE_ANGLES = np.arange(0.0, 180.0, TEXTURE_CANDIDATE_STEP)
MAX_TEXTURE_PAIRS = 24
ALTERNATIVE_DISTINCTION = 10.0


@dataclass(frozen=True)
class TextureSplit:
    patch: ActiveField
    angle: np.ndarray
    coherence: np.ndarray
    included: np.ndarray
    doubled_angle: np.ndarray
    doubled_coherence: np.ndarray
    usable_pixels: int

    @property
    def count(self):
        return int(self.included.sum())


def axial_distance(a, b):
    delta = np.abs(a - b) % 180
    return np.minimum(delta, 180 - delta)


def _arc_counts(split, low, high, start_closed=True, end_closed=True):
    """Count included orientations on the axial arc, inclusive or exclusive at each end."""
    low = np.asarray(low, dtype=np.float64)
    high = np.asarray(high, dtype=np.float64)
    begin = np.mod(low, 180.0)
    width = np.mod(np.asarray(high, dtype=np.float64) - np.asarray(low, dtype=np.float64), 180.0)
    left = np.searchsorted(split.doubled_angle, begin, "left" if start_closed else "right")
    right = np.searchsorted(split.doubled_angle, begin + width,
                             "right" if end_closed else "left")
    return np.maximum(right - left, 0)


def _near_counts(split, centers):
    centers = np.atleast_1d(np.asarray(centers, dtype=np.float64))
    return _arc_counts(split, centers - TEXTURE_TOLERANCE, centers + TEXTURE_TOLERANCE)


def _assigned_counts(split, lowers, uppers):
    """Pixel counts of the two nearest-wins sets; pixels exactly on the midpoint stay out."""
    lowers = np.asarray(lowers, dtype=np.float64)
    uppers = np.asarray(uppers, dtype=np.float64)
    distance = _separation(lowers, uppers)
    wide = distance > 2 * TEXTURE_TOLERANCE + 1e-9
    counted = np.zeros((lowers.size, 2), dtype=np.int64)
    if np.any(wide):
        counted[wide, 0] = _near_counts(split, lowers[wide])
        counted[wide, 1] = _near_counts(split, uppers[wide])
    close = ~wide
    if np.any(close):
        low = lowers[close]
        high = uppers[close]
        forward = np.mod(high - low, 180.0) <= 90
        middle = np.where(forward, low + np.mod(high - low, 180.0) / 2,
                          high + np.mod(low - high, 180.0) / 2)
        counted[close, 0] = np.where(
            forward,
            _arc_counts(split, low - TEXTURE_TOLERANCE, middle, True, False),
            _arc_counts(split, middle, low + TEXTURE_TOLERANCE, False, True),
        )
        counted[close, 1] = np.where(
            forward,
            _arc_counts(split, middle, high + TEXTURE_TOLERANCE, False, True),
            _arc_counts(split, high - TEXTURE_TOLERANCE, middle, True, False),
        )
    return counted


def _group_counts(split):
    return _near_counts(split, TEXTURE_ANGLES)


def _histogram_peaks(split, minimum_hits):
    """Per-degree orientation modes, smoothing only enough to suppress isolated noise."""
    hits = np.histogram(split.angle[split.included], bins=180, range=(0, 180))[0]
    smoothed = ndimage.gaussian_filter1d(
        np.concatenate((hits, hits, hits)).astype(np.float64), 1.4, mode="wrap",
    )[180:360]
    peaks = [
        index for index in range(180)
        if smoothed[index] >= smoothed[(index - 1) % 180]
        and smoothed[index] > smoothed[(index + 1) % 180]
        and int(hits[index]) >= minimum_hits
    ]
    peaks.sort(key=lambda index: (-smoothed[index], index))
    return [float(index) for index in peaks]


REFINE_TOLERANCE = 4.0


def _refine_center(split, center):
    """Tighten a peak to the coherence-weighted circular mean of its immediate neighbourhood."""
    chosen = split.included & (
        axial_distance(split.angle, center) <= REFINE_TOLERANCE + 1e-9
    )
    if not np.any(chosen):
        return float(center)
    vector = np.sum(split.coherence[chosen] * np.exp(2j * np.deg2rad(split.angle[chosen])))
    if abs(vector) < 1e-12:
        return float(center)
    refined = math.degrees(np.angle(vector) / 2) % 180
    if axial_distance(refined, center) > REFINE_TOLERANCE:
        return float(center)
    return float(refined)


def _candidate_pairs(split, counts, minimum_pixels):
    """Pairs of supported orientation modes separated by at least 20° (axial)."""
    peaks = _histogram_peaks(split, max(1, minimum_pixels // 20))
    centres = []
    for peak in peaks:
        if _near_counts(split, peak)[0] < minimum_pixels:
            continue
        centres.append(_refine_center(split, peak))
    firsts, seconds = [], []
    for i, first in enumerate(centres):
        for second in centres[i + 1:]:
            if axial_distance(first, second) < TEXTURE_MIN_SEPARATION - 1e-9:
                continue
            firsts.append(min(first, second))
            seconds.append(max(first, second))
    if not firsts:
        return np.empty((0, 2))
    return np.asarray([firsts, seconds], dtype=np.float64).T


def _separation(lower, upper):
    return axial_distance(np.asarray(lower, dtype=np.float64),
                          np.asarray(upper, dtype=np.float64))


def _pair_masks(split, lower, upper):
    """Exact assignment: within 15° of a direction, nearest wins, exact ties stay out."""
    d_lower = axial_distance(split.angle, lower)
    d_upper = axial_distance(split.angle, upper)
    near_lower = split.included & (d_lower <= TEXTURE_TOLERANCE + 1e-9)
    near_upper = split.included & (d_upper <= TEXTURE_TOLERANCE + 1e-9)
    tie = near_lower & near_upper & (np.abs(d_lower - d_upper) < 1e-9)
    first = near_lower & ~tie & (~near_upper | (d_lower < d_upper))
    second = near_upper & ~tie & (~near_lower | (d_upper < d_lower))
    return first, second


def _pair_entries(split, minimum_pixels):
    counts = _group_counts(split)
    pairs = _candidate_pairs(split, counts, minimum_pixels)
    if not len(pairs):
        return counts, []
    assigned = _assigned_counts(split, pairs[:, 0], pairs[:, 1])
    accepted = (assigned[:, 0] >= minimum_pixels) & (assigned[:, 1] >= minimum_pixels)
    if not np.any(accepted):
        return counts, []
    pairs = pairs[accepted]
    kept = assigned[accepted].sum(axis=1)
    total = split.count
    distance = _separation(pairs[:, 0], pairs[:, 1])
    balance = 2 * assigned[accepted].min(axis=1) / kept
    score = kept / total * distance / 90 * balance
    order = np.argsort(-score, kind="stable")
    entries = []
    for index in order[:MAX_TEXTURE_PAIRS * 2]:
        lower, upper = round(float(pairs[index, 0]), 2), round(float(pairs[index, 1]), 2)
        if any(
            axial_distance(lower, seen["first_deg"]) <= ALTERNATIVE_DISTINCTION
            and axial_distance(upper, seen["second_deg"]) <= ALTERNATIVE_DISTINCTION
            for seen in entries
        ):
            continue
        first, second = _pair_masks(split, lower, upper)
        count_a = int(first.sum())
        count_b = int(second.sum())
        if count_a < minimum_pixels or count_b < minimum_pixels:
            continue
        entries.append({
            "first_deg": lower,
            "second_deg": upper,
            "separation_deg": round(float(axial_distance(lower, upper)), 2),
            "first_pixels": count_a,
            "second_pixels": count_b,
            "first_coherence": round(float(split.coherence[first].mean()), 4),
            "second_coherence": round(float(split.coherence[second].mean()), 4),
            "kept_pixels": count_a + count_b,
            "kept_percent": round(100 * (count_a + count_b) / total, 2),
            "balance": round(2 * min(count_a, count_b) / (count_a + count_b), 4),
            "score": round(float(score[index]), 4),
        })
        if len(entries) > MAX_TEXTURE_PAIRS:
            break
    return counts, entries


def _texture_split(image, state, sigma, minimum, patch=None, angle=None,
                   coherence=None, usable=None):
    if patch is None:
        patch = active_field(image, state, TEXTURE_LIMIT)
        angle, coherence, usable = _texture_axes(patch, sigma, minimum)
    selected = np.sort(angle[usable])
    weights = coherence[usable][np.argsort(angle[usable], kind="stable")]
    doubled_angle = np.concatenate((selected, selected + 180))
    doubled_coherence = np.concatenate((
        [0.0], np.cumsum(np.concatenate((weights, weights)), dtype=np.float64),
    ))
    return TextureSplit(
        patch, angle, coherence, usable, doubled_angle, doubled_coherence,
        int(np.count_nonzero(patch.usable)),
    )


def _pair_section(split, minimum, minimum_pixels):
    counts, entries = _pair_entries(split, minimum_pixels)
    common = {
        "min_coherence": minimum,
        "tolerance_deg": TEXTURE_TOLERANCE,
        "minimum_separation_deg": TEXTURE_MIN_SEPARATION,
        "usable_pixels": split.usable_pixels,
        "texture_pixels": split.count,
        "minimum_pixels": minimum_pixels,
    }
    if not entries:
        return {**common, **_shortfall(split, counts, minimum_pixels)}
    chosen = entries[0]
    assigned = chosen["first_pixels"] + chosen["second_pixels"]
    return {
        **common,
        "available": True,
        "pair": chosen,
        "assigned_pixels": assigned,
        "unassigned_pixels": split.count - assigned,
        "assigned_percent": round(100 * assigned / split.count, 2) if split.count else 0.0,
        "alternatives": entries[1:4],
    }


def _shortfall(split, counts, minimum_pixels):
    accepted = split.count
    largest = int(counts.max()) if counts.size else 0
    peaks = _histogram_peaks(split, max(1, minimum_pixels // 20))
    supported = [peak for peak in peaks if _near_counts(split, peak)[0] >= minimum_pixels]
    separated = False
    for i, first in enumerate(supported):
        for second in supported[i + 1:]:
            if axial_distance(first, second) >= TEXTURE_MIN_SEPARATION - 1e-9:
                separated = True
    reasons = []
    if accepted < 2 * minimum_pixels:
        reasons.append(
            f"通过一致性门槛的纹理像素只有 {accepted} 个；两套各需 {minimum_pixels} 个，合计至少 {2 * minimum_pixels} 个"
        )
    if largest < minimum_pixels:
        reasons.append(
            f"最集中的一套方向（±{TEXTURE_TOLERANCE:g}°）只有 {largest} 个像素，不到每套最低 {minimum_pixels} 个"
        )
    if len(supported) < 2:
        reasons.append(
            f"只有 {len(supported)} 套方向在 ±{TEXTURE_TOLERANCE:g}° 内达到 {minimum_pixels} 个像素，凑不出两套"
        )
    elif not separated:
        reasons.append(
            f"虽有两套达标方向，但彼此相隔不到 {TEXTURE_MIN_SEPARATION:g}°，分不开"
        )
    if not reasons:
        reasons.append("没有找到同时满足方向间隔、每套像素数和一致性门槛的两套方向")
    return {
        "available": False,
        "largest_group_pixels": largest,
        "reasons": reasons,
    }


def texture_separation(image, state, sigma, minimum, minimum_pixels):
    split = _texture_split(image, state, sigma, minimum)
    return {
        "source_box": split.patch.source_box(),
        "window_sigma_px": sigma,
        **_pair_section(split, minimum, minimum_pixels),
    }


def texture_pair_histogram(image, state, sigma, minimum, first, second, selection,
                           channel="luminance", bins=64):
    split = _texture_split(image, state, sigma, minimum)
    one, two = _pair_masks(split, first, second)
    mask = one if selection == "first" else two
    if not np.any(mask):
        raise InputError("这一套方向在当前视域没有分到像素，请重新计算纹理方向")
    report = _histogram_payload(split.patch, channel, bins, mask)
    report["texture_scope"] = {
        "first_deg": float(first),
        "second_deg": float(second),
        "separation_deg": float(axial_distance(first, second)),
        "tolerance_deg": TEXTURE_TOLERANCE,
        "sigma": sigma,
        "min_coherence": minimum,
        "channel": channel,
        "bins": bins,
        "selection": selection,
        "pixels": int(np.count_nonzero(mask)),
        "first_pixels": int(np.count_nonzero(one)),
        "second_pixels": int(np.count_nonzero(two)),
    }
    return report


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


def analyze_pixel_pair(first, second, x, y):
    a = first[int(y), int(x)].astype(np.int16)
    b = second[int(y), int(x)].astype(np.int16)
    return {
        "primary": a.tolist(),
        "reference": b.tolist(),
        "delta": (b - a).tolist(),
        "luminance_delta": float(np.dot(b - a, LUMA_WEIGHTS)),
    }
