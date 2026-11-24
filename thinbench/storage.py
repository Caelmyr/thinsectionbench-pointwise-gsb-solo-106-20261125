"""Study catalog, optimistic edits and immutable image assets."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import sqlite3
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from petro.domain import (
    ConflictError, InputError, MissingError, MAX_POINTS, MAX_REGIONS, default_state,
    defect, field, grain, grid, id_value, mineral_point, number, object_value, one_of,
    scale, segment, text_value, view, whole,
)
from petro.geometry import grain_metrics
from petro.raster import decode_upload
from petro.synthetic import DEMO_ID, DEMO_NAME, HEIGHT, WIDTH, build_practice

SCHEMA_VERSION = 1
MAX_STUDIES = 50
MAX_GRAINS = 160
MAX_ROTATION_FRAMES = 12
MAX_CHECKPOINTS = 40
DEMO_PURPOSE = "为偏光岩片判读工作流制作的合成练习。"
ROOT_SCHEMA = """
CREATE TABLE IF NOT EXISTS study (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    specimen TEXT NOT NULL,
    notes TEXT NOT NULL,
    synthetic INTEGER NOT NULL,
    created TEXT NOT NULL,
    modified TEXT NOT NULL,
    revision INTEGER NOT NULL,
    width INTEGER NOT NULL,
    height INTEGER NOT NULL,
    state_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS study_recent ON study(modified DESC);
CREATE TABLE IF NOT EXISTS asset (
    id TEXT NOT NULL,
    study_id TEXT NOT NULL REFERENCES study(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    angle REAL,
    width INTEGER NOT NULL,
    height INTEGER NOT NULL,
    original_format TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    byte_count INTEGER NOT NULL,
    created TEXT NOT NULL,
    PRIMARY KEY (study_id,id)
);
CREATE UNIQUE INDEX IF NOT EXISTS one_primary ON asset(study_id) WHERE kind='primary';
CREATE UNIQUE INDEX IF NOT EXISTS one_reference ON asset(study_id) WHERE kind='crosscheck';
CREATE UNIQUE INDEX IF NOT EXISTS one_angle ON asset(study_id,angle) WHERE kind='rotation';
CREATE TABLE IF NOT EXISTS checkpoint (
    id TEXT PRIMARY KEY,
    study_id TEXT NOT NULL REFERENCES study(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    revision INTEGER NOT NULL,
    created TEXT NOT NULL,
    state_json TEXT NOT NULL,
    metadata_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS study_checkpoint ON checkpoint(study_id,created DESC);
"""


def timestamp():
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def serial(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def short_id():
    return uuid.uuid4().hex[:16]


def _edit_revision(payload):
    payload = object_value(payload, "编辑请求")
    return whole(payload.get("revision"), "工作版本", 1, 2 ** 53 - 1)


def _clean_metadata(payload):
    return {
        "title": text_value(payload.get("title"), "工作名称", 95),
        "specimen": text_value(payload.get("specimen", ""), "样品编号", 80, optional=True),
        "notes": text_value(payload.get("notes", ""), "岩片备注", 1800, optional=True),
    }


def _list_insert(items, record, maximum, label):
    if len(items) >= maximum:
        raise InputError(f"{label}至多保存 {maximum} 项")
    items.append(record)


def _list_remove(items, identifier, label):
    index = next((i for i, item in enumerate(items) if item["id"] == identifier), None)
    if index is None:
        raise MissingError(f"所选{label}不存在")
    return items.pop(index)


def _list_replace(items, identifier, record, label):
    for index, item in enumerate(items):
        if item["id"] == identifier:
            items[index] = record
            return
    raise MissingError(f"所选{label}不存在")


def _state_with_metrics(state):
    result = copy.deepcopy(state)
    factor = result["scale"]["um_per_px"] if result["scale"] else None
    for item in result["grains"]:
        item["metrics"] = grain_metrics(item["vertices"], factor)
    return result


class StudyStore:
    def __init__(self, directory):
        self.root = Path(directory).expanduser().resolve()
        self.root.mkdir(mode=0o750, parents=True, exist_ok=True)
        self.asset_root = self.root / "assets"
        self.asset_root.mkdir(mode=0o750, exist_ok=True)
        self.database = self.root / "catalog.sqlite"
        self.schema_lock = threading.Lock()

    @contextmanager
    def connection(self, immediate=False):
        conn = sqlite3.connect(self.database, timeout=12, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield conn
        except BaseException:
            conn.rollback()
            raise
        else:
            conn.commit()
        finally:
            conn.close()

    def initialize(self):
        with self.schema_lock, sqlite3.connect(self.database, timeout=12) as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise RuntimeError("Catalog schema is newer than this app")
            conn.execute("PRAGMA journal_mode=WAL")
            if version == 0:
                with conn:
                    conn.executescript(ROOT_SCHEMA)
                    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    def healthy(self):
        with self.connection() as conn:
            return conn.execute("SELECT 1").fetchone()[0] == 1

    def asset_path(self, study_id, item_id):
        return self.asset_root / id_value(study_id) / f"{id_value(item_id)}.png"

    def _asset_write(self, study_id, item_id, content):
        dest = self.asset_path(study_id, item_id)
        dest.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(prefix=".scan-", dir=dest.parent)
        staging = Path(name)
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            staging.replace(dest)
            directory_fd = os.open(dest.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            staging.unlink(missing_ok=True)
        return dest

    @staticmethod
    def _asset_rows(conn, study_id):
        result = conn.execute(
            "SELECT * FROM asset WHERE study_id=? ORDER BY kind,angle,created,id",
            (study_id,),
        ).fetchall()
        return [dict(row) for row in result]

    @staticmethod
    def _row(conn, study_id):
        row = conn.execute("SELECT * FROM study WHERE id=?", (id_value(study_id),)).fetchone()
        if row is None:
            raise MissingError("这份岩片工作不存在")
        return row

    @staticmethod
    def _require_revision(row, revision):
        if row["revision"] != revision:
            raise ConflictError(f"当前版本已更新到 r{row['revision']}，请刷新后再修改")

    @staticmethod
    def _catalog_record(row):
        return {
            "id": row["id"],
            "title": row["title"],
            "specimen": row["specimen"],
            "width": row["width"],
            "height": row["height"],
            "revision": row["revision"],
            "created": row["created"],
            "modified": row["modified"],
            "synthetic": bool(row["synthetic"]),
        }

    def _insert_study(self, conn, study_id, metadata, dimensions, state, synthetic):
        current = timestamp()
        columns = (
            "id,title,specimen,notes,synthetic,created,modified,revision,width,height,state_json"
        )
        values = (
            study_id, metadata["title"], metadata["specimen"], metadata["notes"],
            int(synthetic), current, current, 1, *dimensions, serial(state),
        )
        conn.execute(f"INSERT INTO study({columns}) VALUES (?,?,?,?,?,?,?,?,?,?,?)", values)

    def _insert_asset(self, conn, study_id, item_id, kind, name, angle, dimensions, fmt, png):
        path = self._asset_write(study_id, item_id, png)
        try:
            data = (
                item_id, study_id, kind, name, angle, *dimensions,
                fmt, hashlib.sha256(png).hexdigest(), len(png), timestamp(),
            )
            conn.execute("INSERT INTO asset VALUES (?,?,?,?,?,?,?,?,?,?,?)", data)
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return path

    def create(self, metadata, upload, original_name):
        decoded = decode_upload(upload)
        state = default_state((decoded.width, decoded.height))
        identifier = short_id()
        file_id = short_id()
        title = text_value(original_name[:95] or "原照片", "原图文件名", 95)
        written = None
        try:
            with self.connection(immediate=True) as conn:
                count = conn.execute("SELECT count(*) FROM study").fetchone()[0]
                if count >= MAX_STUDIES:
                    raise InputError(f"工作柜已满，请先导出并移除一份工作（上限 {MAX_STUDIES}）")
                self._insert_study(conn, identifier, metadata, (decoded.width, decoded.height), state, False)
                written = self._insert_asset(
                    conn, identifier, file_id, "primary", title, None,
                    (decoded.width, decoded.height), decoded.original_format, decoded.png,
                )
        except BaseException:
            if written is not None:
                written.unlink(missing_ok=True)
            raise
        return identifier

    def import_observation(self, record, blobs):
        identifier = short_id()
        written = []
        try:
            with self.connection(immediate=True) as conn:
                count = conn.execute("SELECT count(*) FROM study").fetchone()[0]
                if count >= MAX_STUDIES:
                    raise InputError(f"工作柜已满（上限 {MAX_STUDIES}）")
                self._insert_study(
                    conn, identifier, record.metadata, record.dimensions, record.state, record.synthetic,
                )
                for asset in record.assets:
                    written.append(self._insert_asset(
                        conn, identifier, asset.identifier, asset.kind, asset.label, asset.angle,
                        asset.dimensions, asset.original_format, blobs[asset.identifier],
                    ))
        except BaseException:
            for path in written:
                path.unlink(missing_ok=True)
            raise
        return identifier

    def ensure_practice(self):
        with self.connection() as conn:
            previous = conn.execute("SELECT 1 FROM study WHERE id=?", (DEMO_ID,)).fetchone()
            if previous:
                return DEMO_ID
        first, state, attached = build_practice()
        dimensions = WIDTH, HEIGHT
        metadata = {"title": DEMO_NAME, "specimen": "DEMO-109", "notes": DEMO_PURPOSE}
        written = []
        try:
            with self.connection(immediate=True) as conn:
                previous = conn.execute("SELECT 1 FROM study WHERE id=?", (DEMO_ID,)).fetchone()
                if previous:
                    return DEMO_ID
                self._insert_study(conn, DEMO_ID, metadata, dimensions, state, True)
                written.append(self._insert_asset(
                    conn, DEMO_ID, "acf000000010", "primary", "交偏光原图", None,
                    dimensions, "PNG", first,
                ))
                for item in attached:
                    written.append(self._insert_asset(
                        conn, DEMO_ID, item["id"], item["kind"], item["name"], item["angle"],
                        dimensions, "PNG", item["data"],
                    ))
        except BaseException:
            for path in written:
                path.unlink(missing_ok=True)
            raise
        return DEMO_ID

    def listing(self):
        with self.connection() as conn:
            rows = conn.execute("SELECT * FROM study ORDER BY modified DESC,id").fetchall()
            return [self._catalog_record(row) for row in rows]

    def study(self, study_id, metrics=True):
        with self.connection() as conn:
            row = self._row(conn, study_id)
            assets = self._asset_rows(conn, study_id)
        state = json.loads(row["state_json"])
        result = self._catalog_record(row)
        result["notes"] = row["notes"]
        result["state"] = _state_with_metrics(state) if metrics else state
        result["assets"] = assets
        return result

    def rename(self, study_id, payload):
        revision = _edit_revision(payload)
        entry = _clean_metadata(payload)
        with self.connection(immediate=True) as conn:
            row = self._row(conn, study_id)
            self._require_revision(row, revision)
            conn.execute(
                "UPDATE study SET title=?,specimen=?,notes=?,modified=?,revision=? WHERE id=?",
                (entry["title"], entry["specimen"], entry["notes"], timestamp(), revision + 1, study_id),
            )

    def remove_study(self, study_id, revision, typed_title):
        revision = whole(revision, "工作版本", 1, 2 ** 53 - 1)
        with self.connection(immediate=True) as conn:
            row = self._row(conn, study_id)
            self._require_revision(row, revision)
            if typed_title != row["title"]:
                raise InputError("请核对要移除的工作名称")
            assets = self._asset_rows(conn, study_id)
            conn.execute("DELETE FROM study WHERE id=?", (study_id,))
        for item in assets:
            self.asset_path(study_id, item["id"]).unlink(missing_ok=True)

    def asset(self, study_id, identifier):
        with self.connection() as conn:
            self._row(conn, study_id)
            row = conn.execute("SELECT * FROM asset WHERE study_id=? AND id=?",
                               (study_id, id_value(identifier))).fetchone()
        if row is None:
            raise MissingError("这张偏光图不存在")
        return dict(row), self.asset_path(study_id, identifier)

    def attach(self, study_id, revision, options, upload):
        rev = whole(revision, "工作版本", 1, 2 ** 53 - 1)
        kind = one_of(options.get("kind"), {"crosscheck", "rotation"}, "对照照片类型")
        title = text_value(options.get("name"), "照片标签", 95)
        angle = number(options.get("angle"), "载物台角度", 0, 360) % 360 if kind == "rotation" else None
        decoded = decode_upload(upload)
        identifier = short_id()
        dims = decoded.width, decoded.height
        written = None
        try:
            with self.connection(immediate=True) as conn:
                row = self._row(conn, study_id)
                self._require_revision(row, rev)
                if dims != (row["width"], row["height"]):
                    raise InputError("只接收同一视域、同像素大小的照片")
                previous = conn.execute("SELECT * FROM asset WHERE study_id=? AND kind=?",
                                        (study_id, kind)).fetchall()
                if kind == "crosscheck" and previous:
                    raise InputError("移除现有偏光对照后再加入另一张")
                if kind == "rotation":
                    if len(previous) >= MAX_ROTATION_FRAMES:
                        raise InputError(f"偏光序列至多 {MAX_ROTATION_FRAMES} 张")
                    if any(abs(item["angle"] - angle) <= 1e-6 for item in previous):
                        raise InputError("这个角度已有照片")
                written = self._insert_asset(
                    conn, study_id, identifier, kind, title, angle, dims, decoded.original_format, decoded.png,
                )
                conn.execute("UPDATE study SET modified=?,revision=? WHERE id=?", (timestamp(), rev + 1, study_id))
        except BaseException:
            if written is not None:
                written.unlink(missing_ok=True)
            raise
        return identifier

    def remove_asset(self, study_id, revision, identifier):
        rev = whole(revision, "工作版本", 1, 2 ** 53 - 1)
        with self.connection(immediate=True) as conn:
            row = self._row(conn, study_id)
            self._require_revision(row, rev)
            deleted = conn.execute(
                "DELETE FROM asset WHERE study_id=? AND id=? AND kind<>'primary'",
                (study_id, id_value(identifier)),
            )
            if deleted.rowcount != 1:
                raise MissingError("对照照片不存在")
            conn.execute("UPDATE study SET modified=?,revision=? WHERE id=?", (timestamp(), rev + 1, study_id))
        self.asset_path(study_id, identifier).unlink(missing_ok=True)

    def update_state(self, study_id, payload):
        revision = _edit_revision(payload)
        kind = one_of(payload.get("kind"), EDITS, "编辑种类")
        data = payload.get("value")
        identifier = payload.get("id")
        with self.connection(immediate=True) as conn:
            row = self._row(conn, study_id)
            self._require_revision(row, revision)
            dimensions = row["width"], row["height"]
            state = json.loads(row["state_json"])
            EDITS[kind](state, data, dimensions, identifier)
            conn.execute(
                "UPDATE study SET state_json=?,modified=?,revision=? WHERE id=?",
                (serial(state), timestamp(), revision + 1, study_id),
            )

    def checkpoint_list(self, study_id):
        with self.connection() as conn:
            self._row(conn, study_id)
            snapshots = conn.execute(
                "SELECT id,title,created,revision FROM checkpoint WHERE study_id=? ORDER BY created DESC,id",
                (study_id,),
            ).fetchall()
            return [dict(item) for item in snapshots]

    def make_checkpoint(self, study_id, payload):
        revision = _edit_revision(payload)
        name = text_value(payload.get("name"), "存档名称", 95)
        identifier = short_id()
        with self.connection(immediate=True) as conn:
            row = self._row(conn, study_id)
            self._require_revision(row, revision)
            if conn.execute("SELECT count(*) FROM checkpoint WHERE study_id=?", (study_id,)).fetchone()[0] >= MAX_CHECKPOINTS:
                raise InputError(f"每份工作至多保留 {MAX_CHECKPOINTS} 个观察存档")
            metadata = {key: row[key] for key in ("title", "specimen", "notes")}
            conn.execute("INSERT INTO checkpoint VALUES (?,?,?,?,?,?,?)", (
                identifier, study_id, name, revision, timestamp(), row["state_json"], serial(metadata),
            ))
        return identifier

    def restore_checkpoint(self, study_id, payload):
        revision = _edit_revision(payload)
        identifier = id_value(payload.get("id"))
        with self.connection(immediate=True) as conn:
            row = self._row(conn, study_id)
            self._require_revision(row, revision)
            snap = conn.execute(
                "SELECT * FROM checkpoint WHERE study_id=? AND id=?", (study_id, identifier),
            ).fetchone()
            if snap is None:
                raise MissingError("观察存档不存在")
            metadata = json.loads(snap["metadata_json"])
            conn.execute(
                "UPDATE study SET state_json=?,title=?,specimen=?,notes=?,modified=?,revision=? WHERE id=?",
                (snap["state_json"], metadata["title"], metadata["specimen"], metadata["notes"],
                 timestamp(), revision + 1, study_id),
            )

    def remove_checkpoint(self, study_id, identifier):
        with self.connection(immediate=True) as conn:
            self._row(conn, study_id)
            cursor = conn.execute("DELETE FROM checkpoint WHERE study_id=? AND id=?",
                                  (study_id, id_value(identifier)))
            if not cursor.rowcount:
                raise MissingError("观察存档不存在")

    def maintenance(self):
        now = time.time()
        with self.connection(immediate=True) as conn:
            assets = {(row["study_id"], row["id"]) for row in conn.execute("SELECT study_id,id FROM asset")}
            removed = 0
            for child in self.asset_root.iterdir():
                if child.is_symlink() or not child.is_dir():
                    continue
                for item in child.iterdir():
                    if item.is_symlink() or not item.is_file():
                        continue
                    orphans = item.suffix == ".png" and (child.name, item.stem) not in assets
                    interrupted = item.name.startswith(".scan-")
                    if (orphans or interrupted) and now - item.stat().st_mtime > 1800:
                        item.unlink(missing_ok=True)
                        removed += 1
            return removed


def _change_field(state, value, dimensions, _identifier):
    state["field"] = field(value, dimensions=dimensions).json()


def _change_grid(state, value, _dimensions, _identifier):
    state["grid"] = grid(value)


def _change_tone(state, value, _dimensions, _identifier):
    state["view"] = view(value)


def _change_line(state, value, dimensions, _identifier):
    state["transect"] = segment(value, dimensions=dimensions).json()


def _change_scale(state, value, dimensions, _identifier):
    state["scale"] = scale(value, dimensions) if value is not None else None


def _edit_defect(state, value, dimensions, identifier):
    identifier = id_value(identifier) if identifier else None
    if value is None:
        _list_remove(state["defects"], identifier, "废区")
        return
    record = defect(value, dimensions, identifier or short_id())
    if identifier:
        _list_replace(state["defects"], identifier, record, "废区")
    else:
        _list_insert(state["defects"], record, MAX_REGIONS, "废区")


def _edit_point(state, value, dimensions, identifier):
    identifier = id_value(identifier) if identifier else None
    if value is None:
        _list_remove(state["points"], identifier, "点数记录")
        return
    record = mineral_point(value, dimensions, identifier or short_id())
    if identifier:
        _list_replace(state["points"], identifier, record, "点数记录")
    else:
        _list_insert(state["points"], record, MAX_POINTS, "点数记录")


def _edit_grain(state, value, dimensions, identifier):
    identifier = id_value(identifier) if identifier else None
    if value is None:
        _list_remove(state["grains"], identifier, "矿粒")
        return
    record = grain(value, dimensions, identifier or short_id())
    grain_metrics(record["vertices"])
    if identifier:
        _list_replace(state["grains"], identifier, record, "矿粒")
    else:
        _list_insert(state["grains"], record, MAX_GRAINS, "矿粒")


EDITS = {
    "field": _change_field,
    "grid": _change_grid,
    "tone": _change_tone,
    "transect": _change_line,
    "scale": _change_scale,
    "defect": _edit_defect,
    "point": _edit_point,
    "grain": _edit_grain,
}
