"""Portable image evidence with bounded, transactional import."""
from __future__ import annotations

import json
import tempfile
import zipfile
from pathlib import PurePosixPath

from petro.domain import InputError

from .records import (
    MANIFEST_PATH, MAX_ARCHIVE_BYTES, MAX_ARCHIVE_JSON,
    manifest_for, validate_manifest, validate_normalized_png,
)

MAX_ENTRIES = 45
MAX_AUXILIARY = 6 * 1024 * 1024
COMPRESSION = {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}


def write_entry(package, name, data):
    metadata = zipfile.ZipInfo(name)
    metadata.compress_type = zipfile.ZIP_DEFLATED
    metadata.external_attr = 0o644 << 16
    package.writestr(metadata, data, compresslevel=5)


def create_archive(study, data_root, extras):
    manifest = manifest_for(study)
    original_assets = validate_manifest(manifest).assets
    output = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024, mode="w+b")
    try:
        with zipfile.ZipFile(output, "w", allowZip64=False) as package:
            serialized = json.dumps(manifest, indent=2, ensure_ascii=False, allow_nan=False).encode("utf-8") + b"\n"
            write_entry(package, MANIFEST_PATH, serialized)
            for asset in original_assets:
                source = data_root(study["id"], asset.identifier)
                content = source.read_bytes()
                validate_normalized_png(content, asset)
                metadata = zipfile.ZipInfo(asset.path)
                metadata.compress_type = zipfile.ZIP_STORED
                metadata.external_attr = 0o644 << 16
                package.writestr(metadata, content)
            for item, raw in extras():
                write_entry(package, item, raw)
        if output.tell() > MAX_ARCHIVE_BYTES:
            raise InputError("观察包超过 110 MiB，请单独下载照片和读数")
        output.seek(0)
        return output
    except BaseException:
        output.close()
        raise


def _file_entries(package):
    entries = package.infolist()
    if not 1 <= len(entries) <= MAX_ENTRIES:
        raise InputError("观察包文件数量无效")
    indexed = {}
    for item in entries:
        path = PurePosixPath(item.filename)
        if (
            item.filename in indexed or item.is_dir() or path.is_absolute()
            or ".." in path.parts or "\\" in item.filename
            or item.file_size < 0 or item.compress_size < 0
            or item.flag_bits & 1 or item.compress_type not in COMPRESSION
        ):
            raise InputError("观察包里有不支持的文件")
        indexed[item.filename] = item
    if sum(item.file_size for item in entries) > MAX_ARCHIVE_BYTES + MAX_AUXILIARY:
        raise InputError("观察包解压尺寸超出上限")
    return indexed


def _read_limited(package, item, limit):
    if item.file_size > limit:
        raise InputError("观察包内的文件超过大小上限")
    with package.open(item) as incoming:
        content = incoming.read(limit + 1)
    if len(content) != item.file_size or len(content) > limit:
        raise InputError("观察包内文件大小与清单不一致")
    return content


def _manifest(package, indexed):
    info = indexed.get(MANIFEST_PATH)
    if info is None:
        raise InputError("观察包缺少 observation.json")
    raw = _read_limited(package, info, MAX_ARCHIVE_JSON)
    try:
        return validate_manifest(json.loads(raw))
    except (ValueError, RecursionError) as exc:
        if isinstance(exc, InputError):
            raise
        raise InputError("观察包清单无法解析") from exc


def _validate_filenames(indexed, assets):
    photo_paths = {asset.path for asset in assets}
    expected_roots = {"readouts", "plates"}
    for name in indexed:
        if name == MANIFEST_PATH or name in photo_paths:
            continue
        path = PurePosixPath(name)
        if len(path.parts) != 2 or path.parts[0] not in expected_roots:
            raise InputError("观察包包含不在清单里的文件")
    if not photo_paths.issubset(indexed):
        raise InputError("观察包缺少清单中列出的照片")


def verify_package(stream):
    stream.seek(0, 2)
    if stream.tell() > MAX_ARCHIVE_BYTES:
        raise InputError("观察包超过 110 MiB")
    stream.seek(0)
    try:
        with zipfile.ZipFile(stream, "r") as package:
            indexed = _file_entries(package)
            record = _manifest(package, indexed)
            _validate_filenames(indexed, record.assets)
            blobs = {}
            for asset in record.assets:
                source = indexed[asset.path]
                content = _read_limited(package, source, asset.byte_count)
                validate_normalized_png(content, asset)
                blobs[asset.identifier] = content
    except (OSError, RuntimeError, NotImplementedError, zipfile.BadZipFile, EOFError) as exc:
        if isinstance(exc, InputError):
            raise
        raise InputError("观察包不是有效 ZIP 或内部数据损坏") from exc
    return record, blobs
