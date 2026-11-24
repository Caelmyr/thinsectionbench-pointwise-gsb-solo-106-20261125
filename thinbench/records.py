"""Validation shared by portable study imports and their normalized image assets."""
from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass

from PIL import Image, UnidentifiedImageError

from petro.domain import (
    InputError, MAX_POINTS, MAX_REGIONS, array_value, defect, field, flag, grain, grid,
    id_value, mineral_point, number, object_value, one_of, scale, segment, text_value,
    view, whole,
)
from petro.geometry import PIXEL_BUDGET, grain_metrics
from petro.raster import ALLOWED_FORMATS, MAX_SIDE, MIN_SIDE

MAX_ARCHIVE_ASSETS = 14
MAX_ARCHIVE_RAW_ASSET = 39 * 1024 * 1024
MAX_ARCHIVE_BYTES = 110 * 1024 * 1024
MAX_ARCHIVE_JSON = 3 * 1024 * 1024
ARCHIVE_VERSION = 1
MANIFEST_PATH = "observation.json"
ASSET_DIRECTORY = "images/"
KINDS = {"primary", "crosscheck", "rotation"}


@dataclass(frozen=True)
class ArchiveAsset:
    identifier: str
    kind: str
    label: str
    angle: float | None
    dimensions: tuple
    original_format: str
    sha256: str
    byte_count: int

    @property
    def path(self):
        return f"{ASSET_DIRECTORY}{self.identifier}.png"

    def json(self):
        return {
            "id": self.identifier,
            "kind": self.kind,
            "name": self.label,
            "angle": self.angle,
            "width": self.dimensions[0],
            "height": self.dimensions[1],
            "original_format": self.original_format,
            "sha256": self.sha256,
            "byte_count": self.byte_count,
            "file": self.path,
        }


@dataclass(frozen=True)
class ArchiveRecord:
    metadata: dict
    dimensions: tuple
    state: dict
    synthetic: bool
    assets: list[ArchiveAsset]
    source_revision: int


def dimensions_value(entry):
    w = whole(entry.get("width"), "工作原图宽", MIN_SIDE, MAX_SIDE)
    h = whole(entry.get("height"), "工作原图高", MIN_SIDE, MAX_SIDE)
    if w * h > PIXEL_BUDGET:
        raise InputError("观察包图像像素超限")
    return w, h


def sha_value(raw):
    if not isinstance(raw, str) or len(raw) != 64:
        raise InputError("观察包照片缺少有效校验和")
    if any(char not in "0123456789abcdef" for char in raw):
        raise InputError("观察包照片校验和无效")
    return raw


def _ids_unique(collection, category):
    identifiers = {item["id"] for item in collection}
    if len(identifiers) != len(collection):
        raise InputError(f"观察包里有重复的{category}编号")


def _records(record, key, limit, label, parser):
    raw = array_value(record.get(key), label, limit)
    result = []
    for item in raw:
        item = object_value(item, label)
        entry = parser(item, id_value(item.get("id")))
        result.append(entry)
    _ids_unique(result, label)
    return result


def clean_state(payload, dimensions):
    payload = object_value(payload, "观察记录")
    bounds = {
        "field": field(payload.get("field"), dimensions=dimensions).json(),
        "grid": grid(payload.get("grid")),
        "view": view(payload.get("view")),
        "transect": segment(payload.get("transect"), dimensions=dimensions).json(),
        "scale": scale(payload["scale"], dimensions) if payload.get("scale") is not None else None,
    }

    def validated_grain(item, identifier):
        clean = grain(item, dimensions, identifier)
        grain_metrics(clean["vertices"])
        return clean

    bounds["defects"] = _records(
        payload, "defects", MAX_REGIONS, "废区", lambda item, i: defect(item, dimensions, i),
    )
    bounds["points"] = _records(
        payload, "points", MAX_POINTS, "点数", lambda item, i: mineral_point(item, dimensions, i),
    )
    bounds["grains"] = _records(payload, "grains", 160, "矿粒", validated_grain)
    return bounds


def _asset_entry(item, dimensions):
    item = object_value(item, "照片")
    identifier = id_value(item.get("id"))
    kind = one_of(item.get("kind"), KINDS, "照片类型")
    angle = number(item.get("angle"), "载物台角度", 0, 360) % 360 if kind == "rotation" else None
    label = text_value(item.get("name"), "照片标签", 95)
    expected_path = f"{ASSET_DIRECTORY}{identifier}.png"
    if item.get("file") != expected_path:
        raise InputError("观察包照片路径与编号不一致")
    size = dimensions_value(item)
    if size != dimensions:
        raise InputError("观察包照片不是同一像素尺寸")
    fmt = one_of(item.get("original_format"), ALLOWED_FORMATS, "原始格式")
    sha = sha_value(item.get("sha256"))
    byte_count = whole(item.get("byte_count"), "观察包照片大小", 40, MAX_ARCHIVE_RAW_ASSET)
    return ArchiveAsset(identifier, kind, label, angle, size, fmt, sha, byte_count)


def validate_manifest(manifest):
    manifest = object_value(manifest, "观察包清单")
    if manifest.get("format") != "thin-section-bench" or manifest.get("version") != ARCHIVE_VERSION:
        raise InputError("这份文件不是 Thin Section Bench v1 观察包")
    study = object_value(manifest.get("study"), "观察工作")
    dimensions = dimensions_value(study)
    metadata = {
        "title": text_value(study.get("title"), "工作名称", 95),
        "specimen": text_value(study.get("specimen", ""), "样品编号", 80, optional=True),
        "notes": text_value(study.get("notes", ""), "岩片备注", 1800, optional=True),
    }
    state = clean_state(manifest.get("state"), dimensions)
    synthetic = flag(study.get("synthetic"), "合成图标记")
    candidates = array_value(manifest.get("assets"), "照片清单", MAX_ARCHIVE_ASSETS, 1)
    assets = [_asset_entry(item, dimensions) for item in candidates]
    if len({item.identifier for item in assets}) != len(assets):
        raise InputError("观察包照片编号重复")
    kinds = [item.kind for item in assets]
    if kinds.count("primary") != 1 or kinds.count("crosscheck") > 1:
        raise InputError("观察包只能含一张交偏光原图和一张偏光对照")
    angles = sorted(item.angle for item in assets if item.kind == "rotation")
    if any(b - a <= 1e-6 for a, b in zip(angles, angles[1:])):
        raise InputError("观察包偏光序列存在重复角度")
    if sum(item.byte_count for item in assets) > MAX_ARCHIVE_BYTES:
        raise InputError("观察包内照片总容量超过上限")
    revision = whole(study.get("revision"), "来源工作版本", 1, 2 ** 53 - 1)
    return ArchiveRecord(metadata, dimensions, state, synthetic, assets, revision)


def validate_normalized_png(content, asset):
    if len(content) != asset.byte_count or hashlib.sha256(content).hexdigest() != asset.sha256:
        raise InputError(f"观察包照片 {asset.label} 校验失败")
    try:
        with Image.open(io.BytesIO(content)) as opened:
            if opened.mode != "RGB" or opened.format != "PNG" or opened.size != asset.dimensions:
                raise InputError("观察包照片须为同尺寸的标准化 RGB PNG")
            opened.load()
    except (OSError, UnidentifiedImageError, Image.DecompressionBombError) as exc:
        raise InputError("观察包照片无法解码") from exc


def archive_asset(record):
    return ArchiveAsset(
        record["id"], record["kind"], record["name"], record["angle"],
        (record["width"], record["height"]), record["original_format"],
        record["sha256"], record["byte_count"],
    )


def manifest_for(study):
    assets = [archive_asset(item).json() for item in study["assets"]]
    identity = {
        key: study[key]
        for key in ("title", "specimen", "notes", "synthetic", "created",
                    "modified", "revision", "width", "height")
    }
    state = {**study["state"], "grains": [
        {key: item[key] for key in ("id", "name", "mineral", "vertices")}
        for item in study["state"]["grains"]
    ]}
    return {
        "format": "thin-section-bench",
        "version": ARCHIVE_VERSION,
        "study": identity,
        "state": state,
        "assets": assets,
    }
