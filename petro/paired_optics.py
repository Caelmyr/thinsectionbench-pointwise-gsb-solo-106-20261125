"""Color change and intensity residuals for two images of the same thin-section field."""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from scipy import ndimage

from .analysis import active_field, channel_stats
from .domain import InputError, field
from .geometry import local_array_coordinates
from .raster import encode_uint8, luminance, map_colors

PAIR_BUDGET = 1_600_000
_XYZ_MATRIX = np.float32([
    [0.4124564, 0.3575761, 0.1804375],
    [0.2126729, 0.7151522, 0.0721750],
    [0.0193339, 0.1191920, 0.9503041],
])
_REFERENCE_WHITE = np.array([0.95047, 1.00000, 1.08883], dtype=np.float32)


@dataclass(frozen=True)
class PairField:
    patch: object
    reference: np.ndarray
    accepted: np.ndarray
    usable: np.ndarray
    clipped: np.ndarray
    source_luminance: np.ndarray
    reference_luminance: np.ndarray

    @property
    def absolute_count(self):
        return int(np.count_nonzero(self.usable))

    @property
    def count(self):
        return int(np.count_nonzero(self.accepted))


def linear_light(rgb):
    srgb = rgb.astype(np.float32) / 255
    return np.where(srgb <= 0.04045, srgb / 12.92, ((srgb + 0.055) / 1.055) ** 2.4)


def cie_lab(rgb):
    xyz = linear_light(rgb) @ _XYZ_MATRIX.T
    normal = xyz / _REFERENCE_WHITE
    delta = 6 / 29
    transformed = np.where(
        normal > delta ** 3,
        np.cbrt(normal),
        normal / (3 * delta ** 2) + 4 / 29,
    )
    x, y, z = np.moveaxis(transformed, -1, 0)
    return np.stack((116 * y - 16, 500 * (x - y), 200 * (y - z)), axis=-1)


def _clipped(first, second, low, high):
    if low == 0 and high == 255:
        return np.zeros(first.shape, dtype=bool)
    return (
        (first < low) | (first > high)
        | (second < low) | (second > high)
    )


def prepare_pair(first, second, state, lower, upper):
    if first.shape != second.shape:
        raise InputError("两张偏光原图的坐标范围不一致")
    if lower >= upper:
        raise InputError("阴影排除值须小于高光排除值")
    view = active_field(first, state, PAIR_BUDGET)
    region = second[view.y:view.y + view.height, view.x:view.x + view.width]
    a = luminance(view.image)
    b = luminance(region)
    rejects = _clipped(a, b, lower, upper)
    included = view.usable & ~rejects
    if not included.any():
        raise InputError("去掉废区和曝光端点后没有可比较像素")
    return PairField(view, region, included, view.usable, rejects, a, b)


def _contrast_measures(pair, mode):
    if mode == "brightness":
        delta = pair.reference_luminance - pair.source_luminance
        magnitude = np.abs(delta)
        before, after = pair.source_luminance, pair.reference_luminance
        secondary = None
    else:
        before = cie_lab(pair.patch.image)
        after = cie_lab(pair.reference)
        separation = after - before
        if mode == "color":
            magnitude = np.linalg.norm(separation, axis=-1)
            delta = separation[..., 0]
            secondary = separation[..., 1:3]
        else:
            c_before = np.hypot(before[..., 1], before[..., 2])
            c_after = np.hypot(after[..., 1], after[..., 2])
            magnitude = np.abs(c_after - c_before)
            delta = c_after - c_before
            secondary = None
    return delta, magnitude, before, after, secondary


def _gradient_by_axis(data, accepted):
    gx = ndimage.sobel(data, axis=1, mode="nearest") / 8
    gy = ndimage.sobel(data, axis=0, mode="nearest") / 8
    valid = ndimage.binary_erosion(accepted, border_value=0)
    return gx, gy, valid


def _matching_gradient(pair):
    a_dx, a_dy, valid = _gradient_by_axis(pair.source_luminance, pair.accepted)
    b_dx, b_dy, _ = _gradient_by_axis(pair.reference_luminance, pair.accepted)
    a2 = a_dx * a_dx + a_dy * a_dy
    b2 = b_dx * b_dx + b_dy * b_dy
    high_signal = valid & (a2 > 4) & (b2 > 4)
    if not high_signal.any():
        return {"supported":0, "median_cosine":None, "opposite_percent":None}
    cosine = ((a_dx * b_dx + a_dy * b_dy)[high_signal] / np.sqrt(a2[high_signal] * b2[high_signal]))
    cosine = np.clip(cosine, -1, 1)
    return {
        "supported": int(high_signal.sum()),
        "median_cosine": float(np.median(cosine)),
        "opposite_percent": 100 * float(np.count_nonzero(cosine < -0.5)) / cosine.size,
    }


def _sectors(pair, state, signed, magnitude, bands, limit):
    region = pair.patch
    horizontal = slice(region.x, region.x + region.width)
    vertical = slice(region.y, region.y + region.height)
    geometry = field(state["field"])
    ux, vx = local_array_coordinates(geometry, vertical, horizontal)
    u_split = np.linspace(0, geometry.width, num=bands + 1)
    v_split = np.linspace(0, geometry.height, num=bands + 1)

    def estimate(source, breaks):
        counts = []
        for lower, upper in zip(breaks, breaks[1:]):
            local = (source >= lower) & (source < upper)
            total = int(np.count_nonzero(pair.usable & local))
            in_mask = pair.accepted & local
            n = int(np.count_nonzero(in_mask))
            counts.append({
                "from_px": float(lower),
                "to_px": float(upper),
                "unmasked": total,
                "comparable": n,
                "signed_median": float(np.median(signed[in_mask])) if n else None,
                "magnitude_median": float(np.median(magnitude[in_mask])) if n else None,
                "changed_percent": 100 * float(np.count_nonzero(magnitude[in_mask] >= limit)) / n if n else None,
            })
        return counts

    return {"along_u":estimate(ux, u_split), "along_v":estimate(vx, v_split)}


def _magnitude_preview(pair, magnitude, limit, cap):
    cap = max(limit + 0.001, cap)
    level = magnitude / cap
    shaded = map_colors(level)
    preview = 0.64 * shaded + 0.36 * pair.patch.image
    preview[~pair.usable] = [22, 32, 40]
    preview[pair.usable & pair.clipped] = [91, 59, 61]
    boundary = pair.accepted & (magnitude >= limit)
    edge = boundary & ~ndimage.binary_erosion(boundary, border_value=0)
    preview[edge] = 0.55 * preview[edge] + [105, 96, 60]
    return encode_uint8(preview)


def _signed_preview(pair, signed, cap):
    normalized = np.clip(np.abs(signed) / max(cap, 1e-6), 0, 1)
    warm = np.array([218, 148, 82], dtype=np.float32)
    cool = np.array([89, 172, 207], dtype=np.float32)
    base = 0.32 * pair.patch.image + np.array([10, 18, 29], dtype=np.float32)
    color = np.where((signed >= 0)[..., None], warm, cool)
    painted = base + (color - base) * normalized[..., None]
    painted[~pair.usable] = [22, 32, 40]
    painted[pair.usable & pair.clipped] = [91, 59, 61]
    return encode_uint8(painted)


def _median_lab(values, mask):
    if values.ndim < 3:
        return None
    return [float(value) for value in np.median(values[mask], axis=0)]


def _center_of_change(changed, mag, patch):
    n = int(np.count_nonzero(changed))
    if n == 0:
        return None
    y, x = np.nonzero(changed)
    weighted = mag[changed]
    factor = float(np.sum(weighted, dtype=np.float64))
    if factor <= 0.0:
        return None
    return {
        "x": float(np.dot(x.astype(np.float64), weighted) / factor + patch.x + 0.5),
        "y": float(np.dot(y.astype(np.float64), weighted) / factor + patch.y + 0.5),
    }


def _opposite_chroma(secondary, valid):
    if secondary is None:
        return None
    da = secondary[..., 0][valid]
    db = secondary[..., 1][valid]
    phase = np.arctan2(db, da)
    length = np.hypot(da, db)
    notice = length > 0.5
    if not notice.any():
        return {"sampled":0, "direction_deg":None, "consistency":None}
    direction = complex(np.sum(length[notice] * np.exp(1j * phase[notice])))
    magnitude = float(np.sum(length[notice]))
    return {
        "sampled": int(notice.sum()),
        "direction_deg": math.degrees(np.angle(direction)) % 360,
        "consistency": abs(direction) / magnitude,
    }


def paired_optics(first, second, state, settings):
    pair = prepare_pair(first, second, state, settings["lower"], settings["upper"])
    mode = settings["mode"]
    signed, magnitude, before, after, secondary = _contrast_measures(pair, mode)
    limit = settings["limit"]
    chosen = pair.accepted
    selected = magnitude[chosen]
    changed = (magnitude >= limit) & chosen
    cap = max(0.5, float(np.percentile(selected, 97)))
    view = _signed_preview(pair, signed, cap) if settings["signed"] else _magnitude_preview(pair, magnitude, limit, cap)
    report = {
        "mode": mode,
        "source_box": pair.patch.source_box(),
        "usable_pixels": pair.absolute_count,
        "paired_pixels": pair.count,
        "clipped_pixels": int(np.count_nonzero(pair.clipped & pair.usable)),
        "changed_pixels": int(np.count_nonzero(changed)),
        "changed_percent": 100 * float(changed.sum()) / pair.count,
        "visual_ceiling": cap,
        "threshold": limit,
        "signed_delta": channel_stats(signed[chosen]),
        "magnitude": channel_stats(selected),
        "primary_lab": _median_lab(before, chosen),
        "reference_lab": _median_lab(after, chosen),
        "signed_chroma": _opposite_chroma(secondary, chosen),
        "change_center": _center_of_change(changed, magnitude, pair.patch),
        "gradient": _matching_gradient(pair),
        "field_bands": _sectors(pair, state, signed, magnitude, settings["bands"], limit),
    }
    return report, view
