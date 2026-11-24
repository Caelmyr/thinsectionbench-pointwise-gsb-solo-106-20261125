"""Validation and geometry for decisions made on normalized source images."""
from __future__ import annotations

import math
import re
from dataclasses import dataclass

MAX_COORD = 16_384
MAX_POINTS = 600
MAX_REGIONS = 64
MAX_VERTICES = 128
PALETTE = {
    "quartz": {"label": "石英", "color": "#76c0d4", "group": "矿物"},
    "feldspar": {"label": "长石", "color": "#ddb25f", "group": "矿物"},
    "mica": {"label": "云母", "color": "#9a7acf", "group": "矿物"},
    "amphibole": {"label": "角闪石", "color": "#6fb994", "group": "矿物"},
    "opaque": {"label": "不透明矿物", "color": "#677687", "group": "矿物"},
    "other": {"label": "其他矿物", "color": "#c78391", "group": "矿物"},
    "pore": {"label": "孔隙", "color": "#367b94", "group": "空隙"},
    "undetermined": {"label": "无法判读", "color": "#a3a89d", "group": "待复核"},
}
DEFECTS = {
    "bubble": "气泡",
    "fracture": "装片裂痕",
    "resin": "封固剂边缘",
    "blur": "失焦",
    "dust": "表面异物",
}
_ID = re.compile(r"^[a-f0-9]{12,32}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class InputError(ValueError):
    """An operator-provided value failed validation."""


class ConflictError(InputError):
    """A stale edit would overwrite the current study."""


class MissingError(InputError):
    """The selected study or item does not exist."""


@dataclass(frozen=True)
class Vec:
    x: float
    y: float

    def __add__(self, other):
        return Vec(self.x + other.x, self.y + other.y)

    def __sub__(self, other):
        return Vec(self.x - other.x, self.y - other.y)

    def __mul__(self, scale):
        return Vec(self.x * scale, self.y * scale)

    def dot(self, other):
        return self.x * other.x + self.y * other.y

    def cross(self, other):
        return self.x * other.y - self.y * other.x

    def norm(self):
        return math.hypot(self.x, self.y)

    def json(self):
        return {"x": self.x, "y": self.y}


@dataclass(frozen=True)
class Field:
    """Source-space top-left origin with clockwise U and perpendicular V."""

    origin: Vec
    width: float
    height: float
    angle: float

    @property
    def axes(self):
        direction = math.radians(self.angle)
        u = Vec(math.cos(direction), math.sin(direction))
        return u, Vec(-u.y, u.x)

    @property
    def center(self):
        return self.to_source(Vec(self.width / 2, self.height / 2))

    def to_source(self, point):
        u, v = self.axes
        return self.origin + u * point.x + v * point.y

    def to_local(self, point):
        u, v = self.axes
        delta = point - self.origin
        return Vec(delta.dot(u), delta.dot(v))

    def corners(self):
        return tuple(
            self.to_source(point)
            for point in (Vec(0, 0), Vec(self.width, 0), Vec(self.width, self.height), Vec(0, self.height))
        )

    def contains(self, point, margin=0.0):
        local = self.to_local(point)
        return margin <= local.x <= self.width - margin and margin <= local.y <= self.height - margin

    def json(self):
        return {
            **self.origin.json(),
            "width": self.width,
            "height": self.height,
            "angle": self.angle,
        }


@dataclass(frozen=True)
class Segment:
    start: Vec
    end: Vec

    @property
    def length(self):
        return (self.end - self.start).norm()

    @property
    def bearing(self):
        delta = self.end - self.start
        return math.degrees(math.atan2(delta.y, delta.x)) % 180

    def at(self, fraction):
        return self.start + (self.end - self.start) * fraction

    def json(self):
        return {"start": self.start.json(), "end": self.end.json()}


def object_value(value, label):
    if not isinstance(value, dict):
        raise InputError(f"{label}应为对象")
    return value


def array_value(value, label, maximum, minimum=0):
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        raise InputError(f"{label}须有 {minimum}–{maximum} 项")
    return value


def text_value(value, label, limit=80, optional=False):
    if not isinstance(value, str):
        raise InputError(f"{label}应为文字")
    clean = _CONTROL.sub(" ", value).strip()
    if (not optional and not clean) or len(clean) > limit:
        raise InputError(f"{label}须为有效文字，最多 {limit} 个字符")
    return clean


def number(value, label, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InputError(f"{label}应为数值")
    if not minimum <= value <= maximum or not math.isfinite(value):
        raise InputError(f"{label}须在 {minimum:g} 和 {maximum:g} 之间")
    return float(value)


def whole(value, label, minimum, maximum):
    clean = number(value, label, minimum, maximum)
    if not clean.is_integer():
        raise InputError(f"{label}应为整数")
    return int(clean)


def one_of(value, permitted, label):
    if not isinstance(value, str) or value not in permitted:
        raise InputError(f"{label}无效")
    return value


def flag(value, label):
    if not isinstance(value, bool):
        raise InputError(f"{label}应为开关")
    return value


def id_value(value):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise InputError("记录编号格式无效")
    return value


def inside(value, width, height):
    return 0 <= value.x < width and 0 <= value.y < height


def point(value, label="像素位置", dimensions=None):
    value = object_value(value, label)
    xy = Vec(
        number(value.get("x"), f"{label} X", -MAX_COORD, MAX_COORD),
        number(value.get("y"), f"{label} Y", -MAX_COORD, MAX_COORD),
    )
    if dimensions and not inside(xy, *dimensions):
        raise InputError(f"{label}超出原图")
    return xy


def field(value, label="观察视域", dimensions=None):
    value = object_value(value, label)
    result = Field(
        point(value, f"{label}起点"),
        number(value.get("width"), f"{label}宽", 4, MAX_COORD),
        number(value.get("height"), f"{label}高", 4, MAX_COORD),
        number(value.get("angle"), f"{label}角", -180, 180),
    )
    if dimensions is not None:
        w, h = dimensions
        corners = result.corners()
        intersects = (
            max(p.x for p in corners) >= 0
            and min(p.x for p in corners) < w
            and max(p.y for p in corners) >= 0
            and min(p.y for p in corners) < h
        )
        if not intersects:
            raise InputError(f"{label}没有触及原图")
    return result


def segment(value, label="测线", dimensions=None):
    value = object_value(value, label)
    result = Segment(
        point(value.get("start"), f"{label}起点", dimensions),
        point(value.get("end"), f"{label}终点", dimensions),
    )
    if result.length < 1:
        raise InputError(f"{label}需至少跨 1 个像素")
    return result


def defect(value, dimensions, identifier):
    value = object_value(value, "废区")
    item = {
        "id": identifier,
        "kind": one_of(value.get("kind"), DEFECTS, "废区类别"),
        "name": text_value(value.get("name", ""), "废区说明", 80, optional=True),
        "shape": field(value.get("shape"), "废区", dimensions).json(),
    }
    return item


def mineral_point(value, dimensions, identifier):
    value = object_value(value, "点数记录")
    item = {
        "id": identifier,
        "mineral": one_of(value.get("mineral"), PALETTE, "矿物类别"),
        "note": text_value(value.get("note", ""), "判读说明", 160, optional=True),
        **point(value, "记录坐标", dimensions).json(),
    }
    return item


def polygon(value, dimensions=None):
    vertices = array_value(value, "轮廓顶点", MAX_VERTICES, 3)
    clean = [point(item, f"顶点 {i + 1}", dimensions) for i, item in enumerate(vertices)]
    if min((b - a).norm() for a, b in zip(clean, clean[1:] + clean[:1])) < 0.25:
        raise InputError("轮廓的相邻顶点距离过小")
    area = abs(sum(a.cross(b) for a, b in zip(clean, clean[1:] + clean[:1]))) / 2
    if area < 1:
        raise InputError("轮廓面积须至少为 1 像素平方")
    return clean


def grain(value, dimensions, identifier):
    value = object_value(value, "矿粒")
    return {
        "id": identifier,
        "name": text_value(value.get("name"), "矿粒标签"),
        "mineral": one_of(value.get("mineral"), PALETTE, "矿物类别"),
        "vertices": [p.json() for p in polygon(value.get("vertices"), dimensions)],
    }


def scale(value, dimensions):
    value = object_value(value, "尺度")
    line = segment(value.get("line"), "标尺", dimensions)
    length = number(value.get("length_um"), "实长 µm", 0.001, 1_000_000)
    return {"line": line.json(), "length_um": length, "um_per_px": length / line.length}


def grid(value):
    value = object_value(value, "载物台设置")
    return {
        "step": number(value.get("step"), "台阶间距 px", 4, 2000),
        "phase_u": one_of(value.get("phase_u"), {"early", "late"}, "U 轴停位"),
        "phase_v": one_of(value.get("phase_v"), {"early", "late"}, "V 轴停位"),
    }


def view(value):
    value = object_value(value, "成像")
    return {
        "tone": one_of(value.get("tone"), {"original", "levels", "mono", "detail", "heat"}, "观察模式"),
        "low": whole(value.get("low"), "暗端 %", 0, 49),
        "high": whole(value.get("high"), "亮端 %", 51, 100),
    }


def default_state(dimensions):
    w, h = dimensions
    inner_width = max(4, w * 0.72)
    inner_height = max(4, h * 0.70)
    crop = Field(Vec((w - inner_width) / 2, (h - inner_height) / 2), inner_width, inner_height, 0)
    line = Segment(
        Vec(max(0, (w - 1) * 0.15), max(0, (h - 1) * 0.57)),
        Vec(max(1, (w - 1) * 0.83), max(0, (h - 1) * 0.57)),
    )
    return {
        "field": crop.json(),
        "defects": [],
        "points": [],
        "grains": [],
        "scale": None,
        "transect": line.json(),
        "grid": {"step": max(4, round(min(w, h) / 8)), "phase_u": "early", "phase_v": "early"},
        "view": {"tone": "original", "low": 2, "high": 98},
    }
