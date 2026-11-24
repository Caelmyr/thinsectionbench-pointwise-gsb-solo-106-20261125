"""Measure an intensity transition along the existing annotated transect."""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from scipy import ndimage

from .analysis import channel_stats, color_channel, valid_at
from .domain import InputError, Vec, segment
from .geometry import bilinear_values, line_coordinates
from .raster import encode_uint8, luminance

MAX_TAPS = 41
MAX_SAMPLES = 8192
MIN_RUN = 5


@dataclass(frozen=True)
class Sweep:
    axis: object
    spacing: float
    distance: np.ndarray
    original: np.ndarray
    blurred: np.ndarray
    accepted: np.ndarray
    support: np.ndarray
    lane_values: np.ndarray
    lane_valid: np.ndarray
    lane_offsets: np.ndarray


def _pixels_for_channel(image, channel):
    return luminance(image) if channel == "luminance" else color_channel(image, channel)


def _sample_lane(gray, line, dx, dy, state):
    w, h = gray.shape[1], gray.shape[0]
    x, y = line[0] + dx, line[1] + dy
    values = bilinear_values(gray, x, y)
    valid_bounds = (x >= 0.5) & (x <= w - 0.5) & (y >= 0.5) & (y <= h - 0.5)
    return values, valid_at(state, x, y) & valid_bounds


def _lane_matrix(gray, state, axis, spacing, width):
    t, xx, yy = line_coordinates(axis, spacing)
    if len(t) > MAX_SAMPLES:
        raise InputError("过长的测线请使用较大的采样间隔")
    vector = (axis.end - axis.start) * (1 / axis.length)
    offsets = np.linspace(-width / 2, width / 2, min(MAX_TAPS, max(1, int(math.ceil(width)) + 1)))
    channel = []
    occupancy = []
    for offset in offsets:
        over, allowed = _sample_lane(gray, (xx, yy), -vector.y * offset, vector.x * offset, state)
        channel.append(over)
        occupancy.append(allowed)
    return t * axis.length, np.asarray(channel), np.asarray(occupancy), offsets


def _combine(values, valid, band):
    counts = np.count_nonzero(valid, axis=0)
    minimum = max(1, math.ceil(values.shape[0] * band))
    accepted = counts >= minimum
    selected = np.full(values.shape[1], np.nan)
    for i in np.flatnonzero(accepted):
        target = values[:, i]
        selected[i] = float(np.median(target[valid[:, i]]))
    return selected, accepted, counts


def _runs(mask):
    edges = np.diff(np.r_[False, mask, False].astype(np.int8))
    return list(zip(np.flatnonzero(edges > 0), np.flatnonzero(edges < 0)))


def _smooth_unbroken(values, accepted, smoothing):
    result = values.copy()
    if smoothing > 0:
        for start, stop in _runs(accepted):
            if stop - start >= MIN_RUN:
                result[start:stop] = ndimage.gaussian_filter1d(
                    values[start:stop], sigma=smoothing, mode="nearest",
                )
    return result


def make_sweep(image, state, channel, width, spacing, smoothing, min_support):
    line = segment(state["transect"])
    image_channel = _pixels_for_channel(image, channel)
    distance, lanes, validity, offsets = _lane_matrix(image_channel, state, line, spacing, width)
    measured, accepted, counts = _combine(lanes, validity, min_support)
    softened = _smooth_unbroken(measured, accepted, smoothing)
    return Sweep(
        line, line.length / (len(distance) - 1), distance, measured,
        softened, accepted, counts, lanes, validity, offsets,
    )


def _significant_runs(sweep, min_baseline):
    for start, stop in _runs(sweep.accepted):
        if stop - start < max(MIN_RUN, min_baseline * 2 + 1):
            continue
        selected = sweep.blurred[start:stop]
        if not np.isfinite(selected).all():
            continue
        yield start, stop, selected


def _local_candidates(signal, polarity, baseline, min_change, separation, span):
    gradient = np.gradient(signal)
    if polarity == "rising":
        matching = gradient
    elif polarity == "falling":
        matching = -gradient
    else:
        matching = np.abs(gradient)
    possible = []
    interior = range(baseline, max(baseline, len(signal) - baseline))
    for index in interior:
        rate = matching[index]
        if rate <= 0 or rate < max(matching[index - 1], matching[index + 1]):
            continue
        lead = signal[max(0, index - span):index - 1]
        trail = signal[index + 2:min(len(signal), index + span + 1)]
        if min(lead.size, trail.size) < 2:
            continue
        before = float(np.median(lead))
        after = float(np.median(trail))
        delta = after - before
        if abs(delta) < min_change or np.sign(delta) != np.sign(gradient[index]):
            continue
        possible.append((index, rate, before, after))
    picked = []
    for item in sorted(possible, key=lambda item: (-item[1], item[0])):
        if all(abs(item[0] - have[0]) >= separation for have in picked):
            picked.append(item)
    return sorted(picked, key=lambda item: item[0])


def _position(sweep, index, more=0.0):
    distance = float((index + more) * sweep.spacing)
    along = distance / sweep.axis.length
    source = sweep.axis.at(along)
    return distance, source.json()


def _fraction_position(profile, index, desired, direction, left, right):
    previous = (profile[index] - desired) * direction
    step = -1 if previous > 0 else 1
    j = index
    while left <= j + step < right:
        k = j + step
        now = (profile[k] - desired) * direction
        if (previous <= 0 <= now) or (now <= 0 <= previous):
            if abs(now - previous) < 1e-9:
                return float(j)
            return j - previous * step / (now - previous)
        j, previous = k, now
    return None


def _normalize_edge(sweep, index, before, after, channel, margins, scale):
    lo, hi = margins
    delta = after - before
    direction = 1 if delta > 0 else -1
    target = [before + delta * fraction for fraction in (0.1, 0.5, 0.9)]
    positions = [
        _fraction_position(sweep.blurred, index, threshold, direction, lo, hi)
        for threshold in target
    ]
    widths_ok = all(value is not None for value in positions) and positions[0] <= positions[1] <= positions[2]
    half = positions[1]
    chosen = half if half is not None else index
    distance, absolute = _position(sweep, chosen)
    response = float(abs(sweep.blurred[index + 1] - sweep.blurred[index - 1]) / (2 * sweep.spacing))
    edge_width = (positions[2] - positions[0]) * sweep.spacing if widths_ok else None
    return {
        "kind": "rising" if delta > 0 else "falling",
        "channel": channel,
        "x": absolute["x"],
        "y": absolute["y"],
        "distance_px": distance,
        "distance_um": distance * scale if scale else None,
        "rise_px": edge_width,
        "rise_um": edge_width * scale if edge_width is not None and scale else None,
        "response_per_px": response,
        "difference": delta,
        "before": before,
        "after": after,
        "support": int(sweep.support[index]),
    }


def transitions(sweep, state, options):
    scale = state["scale"]["um_per_px"] if state["scale"] else None
    baseline = max(3, round(options["baseline"] / sweep.spacing))
    separation = max(1, round(options["separation"] / sweep.spacing))
    records = []
    for start, stop, measured in _significant_runs(sweep, baseline):
        candidates = _local_candidates(
            measured, options["polarity"], baseline, options["minimum"],
            separation, max(baseline + 2, 5),
        )
        for relative, _, before, after in candidates:
            absolute = start + relative
            limits = max(start, absolute - baseline - 2), min(stop, absolute + baseline + 3)
            records.append(_normalize_edge(
                sweep, absolute, before, after, options["channel"], limits, scale,
            ))
    return records[:160], len(records)


def sweep_view(sweep):
    levels = np.clip(sweep.lane_values / 255, 0, 1)
    r = 45 + 170 * levels
    g = 62 + 170 * levels
    b = 72 + 147 * levels
    image = encode_uint8(np.stack((r, g, b), axis=2))
    image[~sweep.lane_valid] = [96, 58, 58]
    from PIL import Image
    picture = Image.fromarray(image)
    h = max(36, min(160, picture.height * 9))
    size = min(1400, max(360, picture.width)), h
    return np.asarray(picture.resize(size, Image.Resampling.NEAREST))


def _serial_profile(sweep):
    accepted = sweep.accepted
    output = []
    for i, distance in enumerate(sweep.distance):
        valid = bool(accepted[i])
        output.append({
            "distance_px": float(distance),
            "median": float(sweep.original[i]) if valid else None,
            "filtered": float(sweep.blurred[i]) if valid else None,
            "lanes": int(sweep.support[i]),
        })
    return output


def edge_sweep(image, state, options):
    sweep = make_sweep(
        image, state, options["channel"], options["width"], options["spacing"],
        options["smoothing"], options["min_support"],
    )
    records, observed = transitions(sweep, state, options)
    widths = [item["rise_px"] for item in records if item["rise_px"] is not None]
    supporters = int(np.count_nonzero(sweep.accepted))
    report = {
        "sampling_pitch_px": sweep.spacing,
        "sample_count": int(sweep.distance.size),
        "accepted_count": supporters,
        "full_lane_count": int(sweep.lane_offsets.size),
        "length_px": sweep.axis.length,
        "usable_percent": 100 * supporters / sweep.distance.size,
        "edges_detected": observed,
        "edges": records,
        "width_stats": channel_stats(widths),
        "sweep": _serial_profile(sweep),
    }
    return report, sweep_view(sweep)
