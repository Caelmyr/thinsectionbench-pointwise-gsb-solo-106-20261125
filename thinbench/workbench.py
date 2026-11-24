"""Application-facing operations for a single microscope study."""
from __future__ import annotations

import base64
import json
from dataclasses import dataclass

from petro import analysis
from petro.alignment import (
    DEFAULT_MAX_SHIFT, adopted_shift, counterpart, suggest_translation,
)
from petro.domain import MissingError, InputError, PALETTE, id_value, number, one_of, whole, flag, point
from petro.edgesweep import edge_sweep
from petro.geometry import field_summary, grain_metrics, manual_tally, physical_segment, rose_for_grains, sampled_pixel, stage_guide
from petro.grain_optics import inspect_grain
from petro.paired_optics import paired_optics
from petro.raster import (
    aligned_blend, aligned_difference, encode_png, rectified_crop, tonemap,
)

from .cache import RenderCache, SourceCache


@dataclass(frozen=True)
class Options:
    values: dict

    def choice(self, key, permitted, default, name):
        return one_of(self.values.get(key, default), permitted, name)

    def n(self, key, default, low, high, name):
        return number(self.values.get(key, default), name, low, high)

    def i(self, key, default, low, high, name):
        return whole(self.values.get(key, default), name, low, high)

    def boolean(self, key, default, name):
        return flag(self.values.get(key, default), name)

    def channel(self):
        return self.choice("channel", analysis.CHANNELS, "luminance", "采样通道")

    def _json(self):
        return json.dumps(self.values, sort_keys=True, separators=(",", ":"), allow_nan=False)


class Bench:
    def __init__(self, store):
        self.store = store
        self.pixels = SourceCache()
        self.rendered = RenderCache()

    def primary_record(self, study):
        return next(item for item in study["assets"] if item["kind"] == "primary")

    def reference_record(self, study):
        selected = next((item for item in study["assets"] if item["kind"] == "crosscheck"), None)
        if selected is None:
            raise InputError("请先加入一张同视域偏光对照图")
        return selected

    def pixels_for(self, study, item=None):
        item = item or self.primary_record(study)
        path = self.store.asset_path(study["id"], item["id"])
        try:
            return self.pixels.array(study["id"], item, path)
        except FileNotFoundError as exc:
            raise MissingError("原始图像文件不存在，请重新打开工作") from exc

    @staticmethod
    def dimensions(study):
        return study["width"], study["height"]

    def document(self, study_id):
        study = self.store.study(study_id)
        state = study["state"]
        dims = self.dimensions(study)
        study["ruler"] = physical_segment(state["transect"], state["scale"])
        study["tally"] = manual_tally(state["points"])
        study["guide"] = stage_guide(state["field"], state["grid"], dims)
        study["rose"] = rose_for_grains(state["grains"], 12)
        return study

    def survey_report(self, study, _options):
        result = field_summary(study["state"], self.dimensions(study))
        scale = study["state"]["scale"]
        result["physical_area_um2"] = result["usable_pixels"] * scale["um_per_px"] ** 2 if scale else None
        return result, None

    def histogram_report(self, study, options):
        return analysis.histogram(
            self.pixels_for(study), study["state"], options.channel(),
            options.i("bins", 64, 8, 128, "箱数"),
        ), None

    def profile_report(self, study, options):
        return analysis.profile(
            self.pixels_for(study), study["state"], options.channel(),
            options.n("spacing", 1, 0.25, 20, "采样间隔"),
            options.i("smoothing", 0, 0, 30, "平滑半径"),
            options.boolean("mask", True, "应用废区"),
        ), None

    def components_report(self, study, options):
        result, kept, patch = analysis.threshold_components(
            self.pixels_for(study), study["state"], options.channel(),
            options.n("threshold", 88, 0, 255, "阈值"),
            options.choice("polarity", {"dark", "bright"}, "dark", "阈值方向"),
            options.i("minimum", 20, 1, 1_000_000, "最小面积"),
            options.i("cleanup", 1, 0, 5, "去噪"),
            options.i("connectivity", 2, 1, 2, "连通方式"),
        )
        return result, analysis.component_image(patch, kept)

    def focus_report(self, study, options):
        return analysis.focus_map(
            self.pixels_for(study), study["state"],
            options.n("sigma", 3.0, 1, 24, "视窗半径"),
        )

    def texture_report(self, study, options):
        return analysis.tensor_texture(
            self.pixels_for(study), study["state"],
            options.n("sigma", 4.0, 1, 24, "平滑半径"),
            options.n("coherence", 0.45, 0, 1, "最小一致性"),
            options.i("bins", 12, 4, 36, "方向分箱"),
        )

    def compare_report(self, study, _options):
        base = self.pixels_for(study)
        overlay = self.pixels_for(study, self.reference_record(study))
        return analysis.compare_optics(base, overlay, study["state"]), None

    def align_report(self, study, options):
        primary = self.pixels_for(study)
        reference = self.pixels_for(study, self.reference_record(study))
        saved_limit = study["state"].get("align_limit", DEFAULT_MAX_SHIFT)
        values = options.values
        raw_limit = next((
            values[key] for key in
            ("max_shift", "max_offset", "align_limit", "limit", "range", "radius", "max")
            if key in values
        ), saved_limit)
        raw_x = next((values[key] for key in ("max_shift_x", "max_dx", "range_x", "limit_x")
                      if key in values), None)
        raw_y = next((values[key] for key in ("max_shift_y", "max_dy", "range_y", "limit_y")
                      if key in values), None)
        result = suggest_translation(
            primary, reference, study["state"], raw_limit,
            max_shift_x=raw_x, max_shift_y=raw_y,
        )
        dx, dy, adopted = adopted_shift(study["state"])
        result["adopted"] = {"dx": dx, "dy": dy, "active": adopted}
        return result, None

    def paired_report(self, study, options):
        lower = options.i("lower", 2, 0, 254, "曝光下界")
        upper = options.i("upper", 253, 1, 255, "曝光上界")
        if lower >= upper:
            raise InputError("曝光上界应大于下界")
        return paired_optics(
            self.pixels_for(study),
            self.pixels_for(study, self.reference_record(study)),
            study["state"],
            {
                "lower": lower, "upper": upper,
                "mode": options.choice("mode", {"brightness", "color", "chroma"}, "brightness", "差异尺度"),
                "limit": options.n("limit", 12, 0, 255, "明显变化阈值"),
                "bands": options.i("bands", 4, 2, 12, "视域分带数"),
                "signed": options.boolean("signed", False, "显示正负"),
            },
        )

    def grain_optics_report(self, study, options):
        identifier = id_value(options.values.get("grain_id"))
        grain = next((item for item in study["state"]["grains"] if item["id"] == identifier), None)
        if grain is None:
            raise MissingError("所选矿粒已不存在")
        grain = {**grain, "metrics": grain_metrics(grain["vertices"])}
        reference = None
        if options.boolean("compare", False, "使用偏光对照"):
            reference = self.pixels_for(study, self.reference_record(study))
        return inspect_grain(
            self.pixels_for(study), study["state"], grain, options.channel(),
            options.i("rings", 4, 2, 8, "由内至外分带数"), reference,
        )

    def edge_sweep_report(self, study, options):
        return edge_sweep(self.pixels_for(study), study["state"], {
            "channel": options.channel(),
            "width": options.i("width", 7, 1, 21, "测带宽度"),
            "spacing": options.n("spacing", 1, 0.5, 8, "采样间隔"),
            "smoothing": options.n("smoothing", 1, 0, 8, "平滑半径"),
            "min_support": options.n("min_support", 0.6, 0.1, 1, "最低有效比例"),
            "baseline": options.n("baseline", 8, 3, 48, "边界前后读数距离"),
            "polarity": options.choice("polarity", {"both", "rising", "falling"}, "both", "变化方向"),
            "minimum": options.n("minimum", 10, 0.1, 255, "最小亮度变化"),
            "separation": options.n("separation", 6, 1, 48, "边界间隔"),
        })

    def sequence_report(self, study, options):
        selection = options.choice("scope", {"field", "grain"}, "field", "检测范围")
        records = [item for item in study["assets"] if item["kind"] == "rotation"]
        paths = [(item, self.store.asset_path(study["id"], item["id"])) for item in records]
        return analysis.density_sequence(
            paths, self.pixels_for(study), study["state"], selection,
        ), None

    def lighting_report(self, study, options):
        return analysis.grade_lighting(
            self.pixels_for(study), study["state"], options.i("tiles", 4, 2, 10, "分格数"),
        ), None

    def pixel_report(self, study, options):
        dimensions = self.dimensions(study)
        coordinates = point(options.values, dimensions=dimensions)
        primary_image = self.pixels_for(study)
        x, y = int(coordinates.x), int(coordinates.y)
        result = sampled_pixel(primary_image, coordinates.json())
        wants_compare = options.boolean("compare", False, "偏光对照")
        if wants_compare:
            # reference_record keeps the original 400 when no crosscheck exists.
            reference_image = self.pixels_for(study, self.reference_record(study))
            dx, dy, adopted = adopted_shift(study["state"])
            height, width = reference_image.shape[:2]
            mapping = counterpart(x, y, dx, dy, width, height)
            result["alignment"] = {"dx": dx, "dy": dy, "adopted": adopted}
            if mapping["in_frame"]:
                rx, ry = mapping["reference_x"], mapping["reference_y"]
                reference_pixel = sampled_pixel(reference_image, {"x": rx, "y": ry})
                result["counterpart"] = {**mapping, **reference_pixel}
                result["comparison"] = analysis.analyze_pixel_pair(
                    primary_image, reference_image, x, y, rx, ry,
                )
            else:
                # No corresponding original location: report missing instead
                # of clamping, wrapping or filling with an edge pixel.
                result["counterpart"] = {**mapping, "r": None, "g": None, "b": None}
                result["comparison"] = None
                result["missing_reference"] = True
        if study["state"]["scale"]:
            factor = study["state"]["scale"]["um_per_px"]
            result["x_um"] = coordinates.x * factor
            result["y_um"] = coordinates.y * factor
        return result, None

    def run(self, study, operation, arguments):
        callback = ANALYSES.get(operation)
        if callback is None:
            raise MissingError("所选分析不存在")
        return callback(self, study, Options(arguments))

    def analyze(self, study, operation, arguments):
        record, preview = self.run(study, operation, arguments)
        result = {"revision": study["revision"], "analysis": operation, "result": record}
        if preview is not None:
            result["preview"] = "data:image/png;base64," + base64.b64encode(encode_png(preview)).decode("ascii")
        return result

    def view_pixels(self, study, mode, mixing=0.5):
        source = self.pixels_for(study)
        if mode == "primary":
            return tonemap(source, study["state"])
        if mode == "crop":
            mapped = tonemap(source, study["state"])
            return rectified_crop(mapped, study["state"]["field"])
        reference = self.pixels_for(study, self.reference_record(study))
        if mode == "reference":
            return reference
        dx, dy, _adopted = adopted_shift(study["state"])
        if mode == "mix":
            return aligned_blend(source, reference, mixing, dx, dy)
        if mode == "difference":
            return aligned_difference(source, reference, dx, dy)
        raise InputError("预览方式无效")

    def preview(self, study, mode, mixing):
        return self.rendered.rendered(
            study["id"], study["revision"], mode, (mixing,),
            lambda: encode_png(self.view_pixels(study, mode, mixing)),
        )

    def purge(self, study_id):
        self.pixels.remove_prefix(("source", study_id))
        self.rendered.remove_prefix(("render", study_id))

    def palette(self):
        return PALETTE


ANALYSES = {
    "survey": Bench.survey_report,
    "histogram": Bench.histogram_report,
    "profile": Bench.profile_report,
    "components": Bench.components_report,
    "focus": Bench.focus_report,
    "texture": Bench.texture_report,
    "compare": Bench.compare_report,
    "align": Bench.align_report,
    "alignment": Bench.align_report,
    "align-shift": Bench.align_report,
    "registration": Bench.align_report,
    "shift": Bench.align_report,
    "offset": Bench.align_report,
    "translation": Bench.align_report,
    "paired": Bench.paired_report,
    "grain-optics": Bench.grain_optics_report,
    "edge-sweep": Bench.edge_sweep_report,
    "sequence": Bench.sequence_report,
    "lighting": Bench.lighting_report,
    "pixel": Bench.pixel_report,
}
