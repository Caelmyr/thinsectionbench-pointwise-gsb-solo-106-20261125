"""Image I/O, display transforms and rotated optical field extraction."""
from __future__ import annotations

import hashlib
import io
import math
import warnings
from dataclasses import dataclass

import numpy as np
from PIL import Image, ImageCms, ImageOps, UnidentifiedImageError
from scipy import ndimage
from scipy.ndimage import gaussian_filter

from .domain import InputError, field
from .geometry import PIXEL_BUDGET, segmentation_masks

MAX_IMAGE_BYTES = 22 * 1024 * 1024
MAX_SIDE = 6000
MIN_SIDE = 16
ALLOWED_FORMATS = {"PNG", "JPEG", "TIFF", "WEBP", "BMP"}
LUMA_WEIGHTS = np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)
_UNIFORM_COLORS = np.array([
    [14, 29, 47],
    [31, 77, 113],
    [55, 126, 145],
    [108, 164, 136],
    [215, 191, 101],
    [250, 233, 158],
], dtype=np.float32)
_FOCUS_COLORS = np.array([
    [27, 33, 52],
    [69, 64, 111],
    [96, 108, 152],
    [59, 163, 169],
    [134, 208, 162],
    [248, 235, 152],
], dtype=np.float32)


@dataclass(frozen=True)
class Decoded:
    width: int
    height: int
    original_format: str
    png: bytes
    sha256: str


def _size_check(dimensions):
    w, h = dimensions
    if min(w, h) < MIN_SIDE:
        raise InputError(f"图像宽和高须至少 {MIN_SIDE} 像素")
    if max(w, h) > MAX_SIDE or w * h > PIXEL_BUDGET:
        raise InputError(f"图像尺寸至多 {MAX_SIDE} px / {PIXEL_BUDGET // 1000000} MP")


def encode_png(array):
    output = io.BytesIO()
    image = array if isinstance(array, Image.Image) else Image.fromarray(array)
    image.save(output, format="PNG", compress_level=4)
    return output.getvalue()


def rgb_image(source):
    transparency = source.mode in {"RGBA", "LA", "PA"} or "transparency" in source.info
    if transparency:
        layer = source.convert("RGBA")
        background = Image.new("RGBA", source.size, (17, 20, 24, 255))
        background.alpha_composite(layer)
        return background.convert("RGB")
    high_depth = source.mode in {"I", "I;16B", "I;16L", "I;16", "I;16N", "F"}
    if high_depth:
        data = np.array(source, dtype=np.float64)
        finite = np.isfinite(data)
        if not np.any(finite):
            raise InputError("图像没有有限灰阶像素")
        floor, ceiling = np.percentile(data[finite], [0.2, 99.8])
        if ceiling <= floor:
            denominator = max(255.0, float(floor))
            grey = np.where(finite, data * 255 / denominator, 0)
        else:
            grey = np.where(finite, 255 * (data - floor) / (ceiling - floor), 0)
        return Image.fromarray(np.uint8(np.rint(np.clip(grey, 0, 255)))).convert("RGB")
    color_profile = source.info.get("icc_profile", b"")
    if color_profile and len(color_profile) < 4 * 1024 * 1024:
        try:
            return ImageCms.profileToProfile(
                source.convert("RGB") if source.mode not in {"RGB", "CMYK", "LAB", "L"} else source,
                ImageCms.getOpenProfile(io.BytesIO(color_profile)),
                ImageCms.createProfile("sRGB"),
                outputMode="RGB",
            )
        except (ImageCms.PyCMSError, OSError, ValueError, TypeError):
            pass
    return source.convert("RGB")


def decode_upload(content):
    if not content:
        raise InputError("请选择图像")
    if len(content) > MAX_IMAGE_BYTES:
        raise InputError("单张原照须小于 22 MiB")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(content)) as opened:
                if opened.format not in ALLOWED_FORMATS:
                    raise InputError("请使用 PNG、JPEG、TIFF、WebP 或 BMP")
                file_format = opened.format
                _size_check(opened.size)
                normalized = ImageOps.exif_transpose(opened)
                rgb = rgb_image(normalized)
                _size_check(rgb.size)
                png = encode_png(rgb)
    except (OSError, UnidentifiedImageError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise InputError("图像无法解码或尺寸超限") from exc
    width, height = rgb.size
    return Decoded(width, height, file_format, png, hashlib.sha256(png).hexdigest())


def normalized_array(path):
    with Image.open(path) as opened:
        return np.asarray(opened.convert("RGB")).copy()


def luminance(array):
    return np.sum(array.astype(np.float32) * LUMA_WEIGHTS, axis=-1)


def encode_uint8(array):
    return np.clip(np.rint(array), 0, 255).astype(np.uint8)


def map_colors(values, palette=_UNIFORM_COLORS):
    scaled = np.clip(values, 0, 1) * (len(palette) - 1)
    lower = scaled.astype(np.int32)
    upper = np.minimum(lower + 1, len(palette) - 1)
    fraction = (scaled - lower)[..., None]
    return encode_uint8(palette[lower] * (1 - fraction) + palette[upper] * fraction)


def level_bounds(luma, valid, low, high):
    sample = luma[valid]
    if not len(sample):
        raise InputError("所选视域中没有可用像素")
    bottom, top = (float(value) for value in np.percentile(sample, [low, high]))
    if top - bottom < 1:
        bottom = max(0.0, min(bottom - 1, 254))
        top = min(255.0, max(top + 1, bottom + 1.0))
    return bottom, top


def tonemap(array, state):
    options = state["view"]
    mode = options["tone"]
    if mode == "original":
        return array
    luma = luminance(array)
    if mode == "mono":
        return np.repeat(encode_uint8(luma)[..., None], 3, axis=2)
    dims = array.shape[1], array.shape[0]
    _, valid = segmentation_masks(state, dims)
    bottom, top = level_bounds(luma, valid, options["low"], options["high"])
    if mode == "levels":
        remapped = np.clip((luma - bottom) * (255 / (top - bottom)), 0, 255)
        ratio = remapped / np.maximum(luma, 1)
        return encode_uint8(array.astype(np.float32) * ratio[..., None])
    if mode == "detail":
        smooth = gaussian_filter(luma, 2.1, mode="nearest")
        delta = (luma - smooth) * 1.15
        return encode_uint8(array.astype(np.float32) + delta[..., None])
    if mode == "heat":
        return map_colors((luma - bottom) / (top - bottom))
    raise InputError("观察模式无效")


def crop_affine_matrix(box, size):
    width, height = size
    u, v = box.axes
    us = box.width / width
    vs = box.height / height
    # Pillow evaluates affine coordinates at pixel centers.
    return u.x * us, v.x * vs, box.origin.x, u.y * us, v.y * vs, box.origin.y


def rectified_crop(array, specification, limit=2048):
    box = field(specification)
    factor = min(1.0, limit / max(box.width, box.height))
    w = max(1, int(round(box.width * factor)))
    h = max(1, int(round(box.height * factor)))
    if w * h > PIXEL_BUDGET:
        raise InputError("裁切预览范围过大")
    transform = crop_affine_matrix(box, (w, h))
    source = Image.fromarray(array)
    view = source.transform(
        (w, h), Image.Transform.AFFINE, transform,
        Image.Resampling.BICUBIC, fillcolor=(19, 24, 29),
    )
    return view


def blend_sources(first, second, proportion):
    if first.shape != second.shape:
        raise InputError("对照图必须与原图同像素尺寸")
    return encode_uint8(first * np.float32(1 - proportion) + second * np.float32(proportion))


def shifted_reference(reference, dx, dy):
    """Sample ``reference[y + dy, x + dx]`` over the whole primary frame.

    Returns the zero-filled aligned array and a boolean coverage mask.
    Positions outside the reference original are never wrapped or filled
    with edge pixels; callers render ``~valid`` as missing.
    """
    height, width = reference.shape[:2]
    aligned = np.zeros_like(reference)
    valid = np.zeros((height, width), dtype=bool)
    px0, px1 = max(0, -dx), min(width, width - dx)
    py0, py1 = max(0, -dy), min(height, height - dy)
    if px1 <= px0 or py1 <= py0:
        return aligned, valid
    qx0, qy0 = px0 + dx, py0 + dy
    chunk_h, chunk_w = py1 - py0, px1 - px0
    aligned[py0:py1, px0:px1] = reference[qy0:qy0 + chunk_h, qx0:qx0 + chunk_w]
    valid[py0:py1, px0:px1] = True
    return aligned, valid


def paint_missing(image, valid):
    """Diagonal amber hatch marking pixels with no counterpart original."""
    if valid.all():
        return image
    result = image.astype(np.float32, copy=True)
    missing = ~valid
    yy, xx = np.indices(valid.shape)
    stripes = ((xx + yy) // 9) % 2 == 0
    hatch = missing & stripes
    shade = missing & ~stripes
    result[shade] = 0.72 * result[shade] + 0.28 * np.array([30, 26, 34], dtype=np.float32)
    result[hatch] = 0.35 * result[hatch] + 0.65 * np.array([196, 148, 74], dtype=np.float32)
    # A crisp amber rim along the coverage boundary makes the strip explicit.
    # border_value=1 keeps the physical image edge out of this rim.
    core = ndimage.binary_erosion(valid, border_value=1)
    rim = valid & ~core
    result[rim] = np.array([239, 199, 116], dtype=np.float32)
    return encode_uint8(result)


def aligned_blend(first, second, proportion, dx=0, dy=0):
    """Blend a shifted reference; uncovered frame areas are marked missing."""
    if first.shape != second.shape:
        raise InputError("对照图必须与原图同像素尺寸")
    moved, valid = shifted_reference(second, int(dx), int(dy))
    blended = first * np.float32(1 - proportion) + moved * np.float32(proportion)
    result = encode_uint8(blended)
    return paint_missing(result, valid)


def aligned_difference(first, second, dx=0, dy=0):
    """Polarity-aware difference against the shifted reference."""
    if first.shape != second.shape:
        raise InputError("对照图必须与原图同像素尺寸")
    moved, valid = shifted_reference(second, int(dx), int(dy))
    delta = luminance(moved) - luminance(first)
    strength = np.minimum(np.abs(delta) / 96, 1.0)
    backdrop = np.array([29, 40, 48], dtype=np.float32)
    brighter = np.array([248, 189, 106], dtype=np.float32)
    darker = np.array([81, 170, 209], dtype=np.float32)
    color = np.where((delta >= 0)[..., None], brighter, darker)
    rendered = encode_uint8(backdrop + (color - backdrop) * strength[..., None])
    # Where no counterpart exists show the same explicit missing hatch rather
    # than a zero-difference backdrop, wrap-around or edge replication.
    return paint_missing(rendered, valid)


def difference_image(first, second):
    delta = luminance(second) - luminance(first)
    strength = np.minimum(np.abs(delta) / 96, 1.0)
    backdrop = np.array([29, 40, 48], dtype=np.float32)
    brighter = np.array([248, 189, 106], dtype=np.float32)
    darker = np.array([81, 170, 209], dtype=np.float32)
    color = np.where((delta >= 0)[..., None], brighter, darker)
    return encode_uint8(backdrop + (color - backdrop) * strength[..., None])


def heat_renderer(values, valid, ceiling=None):
    sample = values[valid]
    if not len(sample):
        raise InputError("热图没有可用像素")
    if ceiling is None:
        ceiling = float(np.percentile(sample, 95))
    if not math.isfinite(ceiling) or ceiling <= 0:
        ceiling = 1.0
    result = map_colors(values / ceiling, _FOCUS_COLORS)
    result[~valid] = np.array([20, 26, 35], dtype=np.uint8)
    return result, ceiling
