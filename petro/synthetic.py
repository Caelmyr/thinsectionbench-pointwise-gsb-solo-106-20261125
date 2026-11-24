"""Reproducible synthetic thin-section imagery for the practice study."""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from scipy import ndimage
from scipy.spatial import cKDTree

from .domain import Field, Segment, Vec, default_state
from .raster import encode_png, encode_uint8

WIDTH = 420
HEIGHT = 320
DEMO_NAME = "斜切片 · 花岗质练习"
DEMO_ID = "8cff3fa1edc2"
SEQUENCE_ANGLES = (0, 15, 35, 55, 75)
_REGION = Field(Vec(78, 32), 240, 168, 17)
_SOURCES = (
    ("ref000000001", "单偏光对照", "crosscheck", None),
    ("face9a000000", "载物台 0°", "rotation", 0),
    ("face9a000015", "载物台 15°", "rotation", 15),
    ("face9a000035", "载物台 35°", "rotation", 35),
    ("face9a000055", "载物台 55°", "rotation", 55),
    ("face9a000075", "载物台 75°", "rotation", 75),
)
_MINERAL_TYPES = ("quartz", "feldspar", "mica", "amphibole", "opaque", "other")
_XPL = np.array([
    [197, 204, 201], [235, 199, 132], [187, 137, 202],
    [115, 157, 178], [73, 81, 96], [191, 206, 144],
], dtype=np.float32)
_PPL = np.array([
    [189, 173, 152], [211, 182, 147], [147, 126, 85],
    [111, 129, 99], [69, 64, 58], [176, 160, 116],
], dtype=np.float32)


@dataclass(frozen=True)
class Surface:
    xx: np.ndarray
    yy: np.ndarray
    index: np.ndarray
    centers: np.ndarray
    boundary: np.ndarray
    texture: np.ndarray
    minerals: np.ndarray
    orientation: np.ndarray
    lightfall: np.ndarray


def _grain_sites(rng):
    rows = np.arange(-18, HEIGHT + 42, 36)
    cols = np.arange(-24, WIDTH + 48, 38)
    locations = []
    for i, y in enumerate(rows):
        for x in cols:
            x_off = rng.uniform(-13, 13) + (i % 2) * 12
            y_off = rng.uniform(-12, 12)
            locations.append([x + x_off, y + y_off])
    return np.asarray(locations, dtype=np.float32)


def _surface():
    rng = np.random.default_rng(109)
    yy, xx = np.indices((HEIGHT, WIDTH), dtype=np.float32)
    centers = _grain_sites(rng)
    xi = xx + 3.9 * np.sin(yy / 25) + 1.7 * np.sin(yy / 5.3 + xx / 31)
    yi = yy + 3.2 * np.sin(xx / 21) + 1.8 * np.sin(xx / 6.1 - yy / 24)
    transform = np.array([[1.00, 0.2], [0.02, 1.22]], dtype=np.float32)
    coordinates = np.stack((xi, yi), axis=-1) @ transform
    distance, ids = cKDTree(centers @ transform).query(coordinates, k=2)
    index = ids[..., 0]
    gap = distance[..., 1] - distance[..., 0]
    boundary = np.exp(-gap.astype(np.float32) ** 1.45 / 4.3)
    noise = rng.normal(0, 1, xx.shape).astype(np.float32)
    micro = 9 * ndimage.gaussian_filter(noise, 0.48)
    meso = 8 * ndimage.gaussian_filter(noise, 2.1)
    minerals = rng.choice(6, len(centers), p=[0.28, 0.35, 0.13, 0.12, 0.07, 0.05])
    orientation = rng.uniform(0, np.pi, len(centers))
    x_zone = (xx - WIDTH / 2) / WIDTH
    y_zone = (yy - HEIGHT / 2) / HEIGHT
    light = 1.15 - 0.62 * (x_zone * x_zone + y_zone * y_zone) + 0.14 * x_zone
    return Surface(xx, yy, index, centers, boundary, micro + meso, minerals, orientation, light)


def _interior_styles(surface):
    mineral = surface.minerals[surface.index]
    angle = surface.orientation[surface.index]
    center = surface.centers[surface.index]
    along = np.cos(angle) * (surface.xx - center[..., 0]) + np.sin(angle) * (surface.yy - center[..., 1])
    bands = np.sin(along * 0.93 + np.sin(along * 0.44)) + 0.55 * np.sin((surface.xx + surface.yy) * 0.38)
    thin_line = np.exp(-np.abs(np.sin(along * 0.51)) * 10)
    flecks = np.clip(surface.texture - 3, 0, 7)
    style = np.where(mineral == 1, bands * 10, 0)
    style += np.where(mineral == 2, -24 * thin_line + bands * 7, 0)
    style += np.where(mineral == 3, -10 * thin_line, 0)
    style += np.where(mineral == 4, -17 - flecks * 1.9, 0)
    style += np.where(mineral == 0, 6 * np.sin(along * 0.19), 0)
    return mineral, angle, style


def _paper_defects(surface, rgb, crossed):
    src_x = surface.xx + 0.5 - _REGION.origin.x
    src_y = surface.yy + 0.5 - _REGION.origin.y
    u, v = _REGION.axes
    xx = src_x * u.x + src_y * u.y
    yy = src_x * v.x + src_y * v.y
    dx = (xx - 196) / 24
    dy = (yy - 108) / 21
    radius = np.hypot(dx, dy)
    center = radius < 0.92
    rim = np.exp(-((radius - 0.94) * 18) ** 2)
    glow = np.exp(-((dx + 0.44) ** 2 + (dy + 0.51) ** 2) / 0.047)
    bubble_color = np.array([22, 42, 60] if crossed else [92, 77, 59], dtype=np.float32)
    rgb[center] = 0.30 * rgb[center] + 0.70 * bubble_color
    rgb += glow[..., None] * np.array([52, 66, 62], dtype=np.float32)
    rgb *= 1 - 0.66 * rim[..., None]
    ridge = 69.3 + 1.0 * np.sin(xx / 7.2) + 0.7 * np.sin(xx / 2.9)
    exposed = ((xx >= 7) & (xx <= 91)) * np.exp(-np.abs(yy - ridge) / 1.2)
    rgb = rgb * (1 - 0.79 * exposed[..., None])
    ridge = 30.2 + 0.8 * np.sin(xx / 5)
    scratch = ((xx >= 8) & (xx <= 33)) * np.exp(-np.abs(yy - ridge) / 0.9)
    rgb += scratch[..., None] * np.array([51, 37, 29], dtype=np.float32)
    return rgb


def _mount_edge(surface, result):
    line = 392 + 7 * np.sin(surface.yy / 46)
    gap = surface.xx - line
    bevel = 1 - np.exp(-np.maximum(gap, 0) / 7.0)
    embed = np.array([35, 47, 51], dtype=np.float32)
    result = result * (1.0 - bevel[..., None]) + embed * bevel[..., None]
    glare = np.exp(-((gap + 1.8) / 1.1) ** 2)
    result += glare[..., None] * np.array([25, 32, 34], dtype=np.float32)
    return result


def draw_practice(surface, polarization=None):
    mineral, angle, interior = _interior_styles(surface)
    crossed = polarization is not None
    if crossed:
        rotation = math.radians(polarization)
        extinction = 0.21 + 0.79 * np.sin(2 * (rotation - angle)) ** 2
        result = _XPL[mineral] * extinction[..., None]
        weak_tint = np.sin(surface.xx * 0.078 + surface.yy * 0.033)
        result += interior[..., None] + weak_tint[..., None] * np.array([4, 7, 6], dtype=np.float32)
    else:
        pleochroism = 0.88 + 0.12 * np.cos(2 * angle)
        result = _PPL[mineral] * pleochroism[..., None] + interior[..., None] * 0.66
    result = (result + surface.texture[..., None]) * surface.lightfall[..., None]
    result *= 1 - surface.boundary[..., None] * 0.66
    result = _paper_defects(surface, result, crossed)
    result = _mount_edge(surface, result)
    blur = np.clip((surface.xx - 312) / 96, 0, 1) * np.clip((surface.yy - 168) / 114, 0, 1)
    softened = ndimage.gaussian_filter(result, sigma=(1.9, 1.9, 0), mode="nearest")
    return encode_uint8(result * (1 - blur[..., None]) + softened * blur[..., None])


def _source_point(local_x, local_y):
    return _REGION.to_source(Vec(local_x, local_y))


def _defect(item_id, label, kind, x0, y0, x1, y1):
    shape = Field(_source_point(x0, y0), x1 - x0, y1 - y0, _REGION.angle)
    return {"id": item_id, "kind": kind, "name": label, "shape": shape.json()}


def _seed_grain(surface):
    # Select a complete central silhouette from the deterministic Voronoi map.
    center_x, center_y = 135, 111
    identifier = surface.index[center_y, center_x]
    filled = surface.index == identifier
    edge = filled & ~ndimage.binary_erosion(filled)
    yy, xx = np.nonzero(edge)
    angle = np.mod(np.arctan2(yy - yy.mean(), xx - xx.mean()), 2 * np.pi)
    points = []
    for a, b in zip(np.linspace(0, 2 * np.pi, 13)[:-1], np.linspace(0, 2 * np.pi, 13)[1:]):
        included = (angle >= a) & (angle < b)
        if included.any():
            sample_x, sample_y = xx[included], yy[included]
            choose = int(np.argmax((sample_x - center_x) ** 2 + (sample_y - center_y) ** 2))
            points.append({"x": float(sample_x[choose] + 0.5), "y": float(sample_y[choose] + 0.5)})
    mineral = _MINERAL_TYPES[int(surface.minerals[identifier])]
    return {"id": "decafb010001", "name": "练习粒 01", "mineral": mineral, "vertices": points}


def _seed_marks(surface):
    marks = []
    for i, xy in enumerate(((26, 17), (44, 89), (106, 52), (142, 121), (208, 49), (218, 154))):
        position = _source_point(*xy)
        label = _MINERAL_TYPES[int(surface.minerals[surface.index[int(position.y), int(position.x)]])]
        marks.append({
            "id": f"dec0de{i:06d}",
            **position.json(),
            "mineral": label if i != 4 else "undetermined",
            "note": "合成练习示例记录",
        })
    return marks


def practice_state(surface):
    state = default_state((WIDTH, HEIGHT))
    state["field"] = _REGION.json()
    state["grid"]["step"] = 40
    state["defects"] = [
        _defect("decafe000001", "封固气泡", "bubble", 164, 84, 226, 136),
        _defect("decafe000002", "擦痕", "dust", 6, 26, 34, 34),
        _defect("decafe000003", "装片裂痕", "fracture", 6, 66, 94, 74),
    ]
    state["points"] = _seed_marks(surface)
    state["grains"] = [_seed_grain(surface)]
    state["transect"] = Segment(_source_point(8, 110), _source_point(228, 110)).json()
    state["scale"] = {
        "line": Segment(Vec(289, 277), Vec(369, 277)).json(),
        "length_um": 200,
        "um_per_px": 2.5,
    }
    return state


def build_practice():
    surface = _surface()
    primary = encode_png(draw_practice(surface, 35))
    attachments = []
    for identifier, name, kind, angle in _SOURCES:
        attachments.append({
            "id": identifier.replace("ref", "acf"),
            "name": name,
            "kind": kind,
            "angle": angle,
            "data": encode_png(draw_practice(surface, angle)),
        })
    return primary, practice_state(surface), attachments
