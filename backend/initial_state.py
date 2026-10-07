"""Initial-state import: parse JSON / CSV, validate per domain, normalize.

A scene may carry ``config["initial_state"]`` — a normalized list of individual
dicts that *fully determines* the step-0 population, replacing the engines'
random density/count based initialization.  This module is the single place
that turns user-supplied text into that normalized list:

* :func:`validate_import`  — parse raw JSON / CSV text, validate every record
  against the domain/model schema (types, enums, coordinate bounds) and run
  cross-record consistency checks (duplicate cells, off-road positions,
  infected count exceeding the population, vehicle overlap, capacity).
  Every error names the offending record (1-based) and field.
* :func:`validate_stored`  — re-validate an already-normalized list stored in
  a scene (used by :func:`backend.models.validate_scene` so a scene whose
  parameters changed after import can no longer be saved inconsistently).
* :func:`template` / :func:`schema_doc` — per-model file templates and field
  documentation so users can discover the expected format.

The normalized output is exactly what the engines consume in ``_init`` and
what the preview endpoint turns into the engine's step-0 snapshot, so the
distribution previewed on the config page is identical to the initial layout
of any run created from the same scene.
"""

from __future__ import annotations

import csv
import io
import json
import math
from typing import Any, Dict, List, Optional, Tuple

# Upper bound on imported records — guards the validate endpoint against
# multi-megabyte payloads; the largest built-in population is 5000.
MAX_RECORDS = 20000

# A bound is either a number or ("cfg", key[, offset]) resolved against the
# merged scene config (e.g. traffic lane count -> y < lanes).
Bound = Any
Indexed = List[Tuple[int, Dict[str, Any]]]  # (original 0-based index, record)


def _field(name: str, label: str, kind: str, required: bool = False,
           default: Any = None, lo: Bound = None, hi: Bound = None,
           options: Optional[List[str]] = None, hint: str = "") -> Dict[str, Any]:
    spec: Dict[str, Any] = {"name": name, "label": label, "kind": kind,
                            "required": required, "hint": hint}
    if default is not None:
        spec["default"] = default
    if lo is not None:
        spec["lo"] = lo
    if hi is not None:
        spec["hi"] = hi
    if options is not None:
        spec["options"] = list(options)
    return spec


# --------------------------------------------------------------------------- #
# Cross-record consistency checks (run only when every record is valid)
# --------------------------------------------------------------------------- #
def _cross_unique_cells(indexed: Indexed, errors: List[Dict[str, Any]]) -> None:
    """Flag any two records occupying the same lattice cell."""
    seen: Dict[Tuple[Any, Any], int] = {}
    for idx, rec in indexed:
        key = (rec["x"], rec["y"])
        if key in seen:
            errors.append({
                "index": idx, "field": "x",
                "message": f"第 {seen[key] + 1} 条与第 {idx + 1} 条重复占用同一格 "
                           f"(x={key[0]}, y={key[1]})"})
        else:
            seen[key] = idx


def _cross_traffic_ca(indexed: Indexed, cfg: Dict[str, Any],
                      errors: List[Dict[str, Any]], warnings: List[str]) -> None:
    lanes, length = int(cfg["lanes"]), int(cfg["length"])
    cap = lanes * length
    if len(indexed) > cap:
        errors.append({"index": None, "field": None,
                       "message": f"车辆数（{len(indexed)}）超过道路容量 "
                                  f"{lanes} 车道 × {length} 格 = {cap}"})
    _cross_unique_cells(indexed, errors)
    warnings.append("已提供初始态导入，参数 density（初始车辆密度）将被忽略，以导入为准")


def _cross_traffic_abm(indexed: Indexed, cfg: Dict[str, Any],
                       errors: List[Dict[str, Any]], warnings: List[str]) -> None:
    road = float(cfg["road_length"])
    veh = float(cfg["length"])
    n = len(indexed)
    if n * veh > road + 1e-9:
        errors.append({"index": None, "field": None,
                       "message": f"车辆总长度（{n} 辆 × {veh} m）超过道路长度 "
                                  f"（{road} m），无法全部容纳"})
    pts = sorted(indexed, key=lambda t: t[1]["x"])
    for i in range(n):
        if n < 2:
            break
        ia, ra = pts[i]
        ib, rb = pts[(i + 1) % n]
        gap = (rb["x"] - ra["x"]) % road
        if gap < veh - 1e-9:
            errors.append({"index": ib, "field": "x",
                           "message": f"第 {ia + 1} 条与第 {ib + 1} 条车辆位置重叠："
                                      f"环形间距 {gap:.2f} m 小于车长 {veh} m"})
    warnings.append("已提供初始态导入，参数 n（车辆数）将被忽略，以导入为准")


def _cross_ecology_ca(indexed: Indexed, cfg: Dict[str, Any],
                      errors: List[Dict[str, Any]], warnings: List[str]) -> None:
    w, h = int(cfg["width"]), int(cfg["height"])
    if len(indexed) > w * h:
        errors.append({"index": None, "field": None,
                       "message": f"动物数量（{len(indexed)}）超过网格容量 "
                                  f"{w}×{h} = {w * h}"})
    _cross_unique_cells(indexed, errors)
    warnings.append("已提供初始态导入，参数 n_rabbits、n_foxes 将被忽略，以导入为准")
    warnings.append("草地基底不属于个体，仍由随机种子生成（seed 固定时可复现）")


def _cross_ecology_abm(indexed: Indexed, cfg: Dict[str, Any],
                       errors: List[Dict[str, Any]], warnings: List[str]) -> None:
    warnings.append("已提供初始态导入，参数 n_boids、n_predators 将被忽略，以导入为准")


def _cross_epidemic_ca(indexed: Indexed, cfg: Dict[str, Any],
                       errors: List[Dict[str, Any]], warnings: List[str]) -> None:
    w, h = int(cfg["width"]), int(cfg["height"])
    total = w * h
    infected = sum(1 for _, r in indexed if r["state"] == "infected")
    if infected > total:
        errors.append({"index": None, "field": "state",
                       "message": f"感染者数量（{infected}）超过网格总人数 "
                                  f"{w}×{h} = {total}——感染人数不能超过总数"})
    if len(indexed) > total:
        errors.append({"index": None, "field": None,
                       "message": f"记录数（{len(indexed)}）超过网格总格数 "
                                  f"{w}×{h} = {total}"})
    _cross_unique_cells(indexed, errors)
    warnings.append("未列出的格子默认为易感（susceptible）")
    warnings.append("已提供初始态导入，参数 initial_infected、vaccination_rate "
                    "将被忽略，以导入为准")


def _cross_epidemic_abm(indexed: Indexed, cfg: Dict[str, Any],
                        errors: List[Dict[str, Any]], warnings: List[str]) -> None:
    n = int(cfg["n"])
    infected = sum(1 for _, r in indexed if r["state"] == "infected")
    if infected > n:
        errors.append({"index": None, "field": "state",
                       "message": f"感染者数量（{infected}）超过配置的人口规模 "
                                  f"n（{n}）——感染人数不能超过总数"})
    if len(indexed) > n:
        errors.append({"index": None, "field": None,
                       "message": f"导入个体数（{len(indexed)}）超过配置的人口规模 "
                                  f"n（{n}）"})
    warnings.append("已提供初始态导入，参数 n、initial_infected、vaccination_rate "
                    "将被忽略，以导入为准")


# --------------------------------------------------------------------------- #
# Per-record hooks (checks that need more than one field of the same record)
# --------------------------------------------------------------------------- #
def _hook_ecology_abm(rec: Dict[str, Any], cfg: Dict[str, Any],
                      idx: int) -> List[Dict[str, Any]]:
    limit = (float(cfg["max_speed"]) if rec["type"] == "boid"
             else float(cfg["pred_speed"]))
    speed = math.hypot(rec.get("vx", 0.0), rec.get("vy", 0.0))
    if speed > limit + 1e-9:
        return [{"index": idx, "field": "vx",
                 "message": f"第 {idx + 1} 条：初速度 {speed:.2f} 超过类型 "
                            f"{rec['type']} 的速度上限 {limit}"}]
    return []


# --------------------------------------------------------------------------- #
# Normalizers: validated record -> engine-ready individual dict
# --------------------------------------------------------------------------- #
def _norm_traffic_ca(rec: Dict[str, Any], cfg: Dict[str, Any]) -> Dict[str, Any]:
    return {"id": rec["id"], "type": "vehicle", "state": rec["state"],
            "x": rec["x"], "y": rec["y"], "v": rec["v"]}


def _norm_traffic_abm(rec: Dict[str, Any], cfg: Dict[str, Any]) -> Dict[str, Any]:
    return {"id": rec["id"], "type": "vehicle", "state": rec["state"],
            "x": rec["x"], "y": 0.0, "v": rec["v"]}


def _norm_ecology_ca(rec: Dict[str, Any], cfg: Dict[str, Any]) -> Dict[str, Any]:
    return {"id": rec["id"], "type": rec["type"], "state": rec["type"],
            "x": rec["x"], "y": rec["y"], "energy": rec["energy"], "age": 0}


def _norm_ecology_abm(rec: Dict[str, Any], cfg: Dict[str, Any]) -> Dict[str, Any]:
    return {"id": rec["id"], "type": rec["type"], "state": rec["type"],
            "x": rec["x"], "y": rec["y"], "vx": rec["vx"], "vy": rec["vy"]}


def _norm_epidemic_ca(rec: Dict[str, Any], cfg: Dict[str, Any]) -> Dict[str, Any]:
    return {"x": rec["x"], "y": rec["y"], "type": "person",
            "state": rec["state"], "days": rec["days"]}


def _norm_epidemic_abm(rec: Dict[str, Any], cfg: Dict[str, Any]) -> Dict[str, Any]:
    return {"id": rec["id"], "type": "person", "state": rec["state"],
            "x": rec["x"], "y": rec["y"], "heading": rec["heading"],
            "days": rec["days"]}


# --------------------------------------------------------------------------- #
# Schemas
# --------------------------------------------------------------------------- #
def _energy_default(cfg: Dict[str, Any], rec: Dict[str, Any]) -> float:
    key = "rabbit_repro" if rec.get("type") == "rabbit" else "fox_repro"
    return float(cfg.get(key, 8)) / 2


def _traffic_state_default(cfg: Dict[str, Any], rec: Dict[str, Any]) -> str:
    return "moving" if float(rec.get("v") or 0) > 0 else "stopped"


_OFF_ROAD = "（超出道路范围）"

_SCHEMAS: Dict[Tuple[str, str], Dict[str, Any]] = {
    ("traffic", "ca"): {
        "fields": [
            _field("id", "个体 ID", "str"),
            _field("type", "类型", "choice", default="vehicle",
                   options=["vehicle"]),
            _field("x", "位置 x（格）", "int", required=True,
                   lo=0, hi=("cfg", "length", -1), hint=_OFF_ROAD),
            _field("y", "车道 y", "int", required=True,
                   lo=0, hi=("cfg", "lanes", -1), hint=_OFF_ROAD),
            _field("v", "速度 v", "int", default=0, lo=0, hi=("cfg", "vmax")),
            _field("state", "状态", "choice", default=_traffic_state_default,
                   options=["moving", "stopped"]),
        ],
        "id_prefix": lambda rec: "v",
        "sort_by": None,
        "cross": _cross_traffic_ca,
        "normalize": _norm_traffic_ca,
    },
    ("traffic", "abm"): {
        "fields": [
            _field("id", "个体 ID", "str"),
            _field("type", "类型", "choice", default="vehicle",
                   options=["vehicle"]),
            _field("x", "位置 x（米）", "float", required=True,
                   lo=0, hi=("cfg", "road_length"), hint=_OFF_ROAD),
            _field("v", "速度 v", "float", default=0.0, lo=0, hi=("cfg", "v0")),
            _field("state", "状态", "choice", default=_traffic_state_default,
                   options=["moving", "stopped"]),
        ],
        "id_prefix": lambda rec: "v",
        "sort_by": "x",
        "cross": _cross_traffic_abm,
        "normalize": _norm_traffic_abm,
    },
    ("ecology", "ca"): {
        "fields": [
            _field("id", "个体 ID", "str"),
            _field("type", "类型", "choice", required=True,
                   options=["rabbit", "fox"]),
            _field("x", "位置 x（格）", "int", required=True,
                   lo=0, hi=("cfg", "width", -1)),
            _field("y", "位置 y（格）", "int", required=True,
                   lo=0, hi=("cfg", "height", -1)),
            _field("energy", "初始能量", "float", default=_energy_default,
                   lo=1e-9),
        ],
        "id_prefix": lambda rec: "rb" if rec["type"] == "rabbit" else "fx",
        "sort_by": None,
        "cross": _cross_ecology_ca,
        "normalize": _norm_ecology_ca,
    },
    ("ecology", "abm"): {
        "fields": [
            _field("id", "个体 ID", "str"),
            _field("type", "类型", "choice", required=True,
                   options=["boid", "predator"]),
            _field("x", "位置 x", "float", required=True, lo=0, hi=("cfg", "width")),
            _field("y", "位置 y", "float", required=True, lo=0, hi=("cfg", "height")),
            _field("vx", "初速度 vx", "float", default=0.0),
            _field("vy", "初速度 vy", "float", default=0.0),
        ],
        "id_prefix": lambda rec: "b" if rec["type"] == "boid" else "p",
        "sort_by": None,
        "record_hook": _hook_ecology_abm,
        "cross": _cross_ecology_abm,
        "normalize": _norm_ecology_abm,
    },
    ("epidemic", "ca"): {
        "fields": [
            _field("x", "位置 x（格）", "int", required=True,
                   lo=0, hi=("cfg", "width", -1)),
            _field("y", "位置 y（格）", "int", required=True,
                   lo=0, hi=("cfg", "height", -1)),
            _field("state", "状态", "choice", required=True,
                   options=["susceptible", "infected", "recovered"]),
            _field("days", "已感染天数", "int", default=0, lo=0),
        ],
        "id_prefix": None,
        "sort_by": None,
        "cross": _cross_epidemic_ca,
        "normalize": _norm_epidemic_ca,
    },
    ("epidemic", "abm"): {
        "fields": [
            _field("id", "个体 ID", "str"),
            _field("type", "类型", "choice", default="person",
                   options=["person"]),
            _field("x", "位置 x", "float", required=True, lo=0, hi=("cfg", "width")),
            _field("y", "位置 y", "float", required=True, lo=0, hi=("cfg", "height")),
            _field("state", "状态", "choice", default="susceptible",
                   options=["susceptible", "infected", "recovered"]),
            _field("heading", "初始朝向（弧度）", "float", default=0.0),
            _field("days", "已感染天数", "int", default=0, lo=0),
        ],
        "id_prefix": lambda rec: "p",
        "sort_by": None,
        "cross": _cross_epidemic_abm,
        "normalize": _norm_epidemic_abm,
    },
}

# Two valid example rows per model, used by the template endpoint.
_EXAMPLES: Dict[Tuple[str, str], List[Dict[str, Any]]] = {
    ("traffic", "ca"): [
        {"id": "v0000", "type": "vehicle", "x": 0, "y": 0, "v": 0,
         "state": "stopped"},
        {"id": "v0001", "type": "vehicle", "x": 12, "y": 1, "v": 3,
         "state": "moving"},
    ],
    ("traffic", "abm"): [
        {"id": "v0000", "type": "vehicle", "x": 0.0, "v": 0.0,
         "state": "stopped"},
        {"id": "v0001", "type": "vehicle", "x": 250.5, "v": 20.0,
         "state": "moving"},
    ],
    ("ecology", "ca"): [
        {"id": "rb0000", "type": "rabbit", "x": 5, "y": 5, "energy": 4},
        {"id": "fx0001", "type": "fox", "x": 20, "y": 20, "energy": 6},
    ],
    ("ecology", "abm"): [
        {"id": "b0000", "type": "boid", "x": 100.0, "y": 150.0,
         "vx": 1.0, "vy": 0.0},
        {"id": "p0001", "type": "predator", "x": 300.0, "y": 300.0,
         "vx": 0.0, "vy": 2.0},
    ],
    ("epidemic", "ca"): [
        {"x": 10, "y": 10, "state": "infected", "days": 0},
        {"x": 20, "y": 20, "state": "recovered", "days": 0},
    ],
    ("epidemic", "abm"): [
        {"id": "p0000", "type": "person", "x": 100.0, "y": 100.0,
         "state": "infected", "heading": 0.0, "days": 0},
        {"id": "p0001", "type": "person", "x": 200.0, "y": 300.0,
         "state": "susceptible", "heading": 1.5, "days": 0},
    ],
}

# CSV header aliases (Chinese headers map to canonical field names).
_CSV_ALIASES = {"状态": "state", "类型": "type", "能量": "energy",
                "速度": "v", "天数": "days", "朝向": "heading"}


def known(domain: str, model: str) -> bool:
    return (domain, model) in _SCHEMAS


def _full_config(domain: str, model: str, config: Dict[str, Any]) -> Dict[str, Any]:
    """Merge the engine's own defaults under the supplied config.

    Some bounds the validator needs (e.g. traffic-ABM vehicle ``length``)
    live only in the engine defaults, not in the catalog params — the engine
    sees ``{**engine.defaults(), **config}`` so the validator must too.
    """
    from .engine import engine_defaults  # local import: no cycle at load time
    return {**engine_defaults(domain, model), **(config or {})}


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
def _err(index: Optional[int], field: Optional[str], message: str) -> Dict[str, Any]:
    return {"index": index, "field": field, "message": message}


def _parse_json(text: str) -> Tuple[Indexed, List[Dict[str, Any]], List[str]]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return [], [_err(None, None, f"JSON 解析失败：第 {exc.lineno} 行第 "
                                     f"{exc.colno} 列：{exc.msg}")], []
    if isinstance(data, dict):
        if isinstance(data.get("individuals"), list):
            data = data["individuals"]
        else:
            return [], [_err(None, None, "JSON 顶层应为个体数组，或包含 "
                                         "\"individuals\" 数组的对象")], []
    if not isinstance(data, list):
        return [], [_err(None, None, "JSON 顶层应为个体数组")], []
    indexed: Indexed = []
    errors: List[Dict[str, Any]] = []
    for i, item in enumerate(data):
        if not isinstance(item, dict):
            errors.append(_err(i, None, f"第 {i + 1} 条应为对象，"
                                        f"得到 {type(item).__name__}"))
            continue
        indexed.append((i, item))
    return indexed, errors, []


def _parse_csv(text: str) -> Tuple[Indexed, List[Dict[str, Any]], List[str]]:
    try:
        reader = csv.DictReader(io.StringIO(text))
        raw_headers = reader.fieldnames
        rows = list(reader)
    except csv.Error as exc:
        return [], [_err(None, None, f"CSV 解析失败：{exc}")], []
    if not raw_headers:
        return [], [_err(None, None, "CSV 缺少表头（第一行应为字段名）")], []

    headers: List[str] = []
    for h in raw_headers:
        name = (h or "").strip().lower()
        name = _CSV_ALIASES.get((h or "").strip(), name)
        headers.append(name)
    if len(set(headers)) != len(headers):
        dup = sorted({h for h in headers if headers.count(h) > 1})
        return [], [_err(None, None, f"CSV 表头存在重复列：{'、'.join(dup)}")], []

    indexed = []
    warnings: List[str] = []
    skipped = 0
    for i, row in enumerate(rows):
        rec = {headers[j]: (v.strip() if isinstance(v, str) else v)
               for j, v in enumerate(row.values()) if j < len(headers)}
        if all(v in (None, "") for v in rec.values()):
            skipped += 1
            continue
        indexed.append((i, rec))
    if skipped:
        warnings.append(f"跳过了 {skipped} 行空行")
    return indexed, [], warnings


def parse_text(text: str, fmt: str) -> Tuple[str, Indexed,
                                             List[Dict[str, Any]], List[str]]:
    """Parse raw text into ``(format, indexed_records, errors, warnings)``."""
    fmt = (fmt or "auto").lower()
    if fmt == "auto":
        fmt = "json" if text.lstrip()[:1] in "[{" else "csv"
    if fmt == "json":
        indexed, errors, warnings = _parse_json(text)
    elif fmt == "csv":
        indexed, errors, warnings = _parse_csv(text)
    else:
        return fmt, [], [_err(None, None, f"未知格式：{fmt}（支持 json / csv）")], []
    return fmt, indexed, errors, warnings


# --------------------------------------------------------------------------- #
# Per-record validation
# --------------------------------------------------------------------------- #
def _resolve(bound: Bound, cfg: Dict[str, Any]) -> Optional[float]:
    if bound is None:
        return None
    if isinstance(bound, (int, float)):
        return float(bound)
    _, key, *rest = bound
    base = cfg.get(key)
    if base is None:
        return None
    return float(base) + (rest[0] if rest else 0)


def _coerce_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if math.isfinite(value) and value.is_integer() else None
    if isinstance(value, str):
        s = value.strip()
        try:
            return int(s)
        except ValueError:
            try:
                f = float(s)
            except ValueError:
                return None
            return int(f) if math.isfinite(f) and f.is_integer() else None
    return None


def _coerce_float(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(float(value)) else None
    if isinstance(value, str):
        try:
            f = float(value.strip())
        except ValueError:
            return None
        return f if math.isfinite(f) else None
    return None


def _validate_record(schema: Dict[str, Any], raw: Dict[str, Any], idx: int,
                     cfg: Dict[str, Any]) -> Tuple[Dict[str, Any],
                                                   List[Dict[str, Any]]]:
    errors: List[Dict[str, Any]] = []
    out: Dict[str, Any] = {}
    n = idx + 1
    for spec in schema["fields"]:
        name = spec["name"]
        present = name in raw and raw[name] not in (None, "")
        if not present:
            if spec.get("required"):
                errors.append(_err(idx, name, f"第 {n} 条：缺少必填字段 "
                                              f"{name}（{spec['label']}）"))
            elif "default" in spec:
                default = spec["default"]
                out[name] = (default(cfg, out) if callable(default) else default)
            continue
        value = raw[name]
        kind = spec["kind"]
        if kind == "int":
            v = _coerce_int(value)
            if v is None:
                errors.append(_err(idx, name, f"第 {n} 条：字段 {name}"
                                              f"（{spec['label']}）应为整数，"
                                              f"得到 {value!r}"))
                continue
        elif kind == "float":
            v = _coerce_float(value)
            if v is None:
                errors.append(_err(idx, name, f"第 {n} 条：字段 {name}"
                                              f"（{spec['label']}）应为数值，"
                                              f"得到 {value!r}"))
                continue
        elif kind == "choice":
            v = str(value).strip()
            if v not in spec["options"]:
                errors.append(_err(idx, name, f"第 {n} 条：字段 {name}"
                                              f"（{spec['label']}）取值 {value!r} "
                                              f"非法，可选："
                                              f"{' / '.join(spec['options'])}"))
                continue
        else:  # str
            v = str(value).strip()
            if not v:
                errors.append(_err(idx, name, f"第 {n} 条：字段 {name}"
                                              f"（{spec['label']}）不能为空"))
                continue
        lo = _resolve(spec.get("lo"), cfg)
        hi = _resolve(spec.get("hi"), cfg)
        if lo is not None and v < lo or hi is not None and v > hi:
            lo_s = f"{lo:g}" if lo is not None else "-∞"
            hi_s = f"{hi:g}" if hi is not None else "+∞"
            errors.append(_err(idx, name, f"第 {n} 条：字段 {name}"
                                          f"（{spec['label']}）= {v} 越界，"
                                          f"合法范围 [{lo_s}, {hi_s}]{spec['hint']}"))
            continue
        out[name] = v
    hook = schema.get("record_hook")
    if hook and not errors:
        errors.extend(hook(out, cfg, idx))
    return out, errors


def _assign_ids(schema: Dict[str, Any], valid: Indexed,
                errors: List[Dict[str, Any]]) -> None:
    """Check user-supplied ids for duplicates; auto-assign the missing ones."""
    prefix_fn = schema.get("id_prefix")
    if prefix_fn is None:
        return
    seen: Dict[str, int] = {}
    for idx, rec in valid:
        if "id" in rec:
            if rec["id"] in seen:
                errors.append(_err(idx, "id", f"第 {seen[rec['id']] + 1} 条与第 "
                                              f"{idx + 1} 条的 id 重复："
                                              f"{rec['id']!r}"))
            else:
                seen[rec["id"]] = idx
    counter = 0
    for idx, rec in valid:
        if "id" in rec:
            continue
        while True:
            candidate = f"{prefix_fn(rec)}{counter:04d}"
            counter += 1
            if candidate not in seen:
                seen[candidate] = idx
                rec["id"] = candidate
                break


# --------------------------------------------------------------------------- #
# Core validation pipeline
# --------------------------------------------------------------------------- #
def _validate(domain: str, model: str, config: Dict[str, Any],
              indexed: Indexed, warnings: List[str]) -> Dict[str, Any]:
    schema = _SCHEMAS[(domain, model)]
    errors: List[Dict[str, Any]] = []

    known = {f["name"] for f in schema["fields"]}
    unknown = sorted({k for _, rec in indexed for k in rec} - known)
    if unknown:
        warnings.append(f"未知字段 {'、'.join(unknown)} 将被忽略")

    valid: Indexed = []
    for idx, raw in indexed:
        rec, errs = _validate_record(schema, raw, idx, config)
        errors.extend(errs)
        if not errs:
            valid.append((idx, rec))
    if errors:
        return {"ok": False, "errors": errors, "warnings": warnings,
                "individuals": []}

    _assign_ids(schema, valid, errors)
    schema["cross"](valid, config, errors, warnings)
    if errors:
        return {"ok": False, "errors": errors, "warnings": warnings,
                "individuals": []}

    if schema.get("sort_by"):
        valid.sort(key=lambda t: t[1][schema["sort_by"]])
    normalize = schema["normalize"]
    individuals = [normalize(rec, config) for _, rec in valid]
    return {"ok": True, "errors": [], "warnings": warnings,
            "individuals": individuals}


def _summary(individuals: List[Dict[str, Any]]) -> Dict[str, Any]:
    by_state: Dict[str, int] = {}
    by_type: Dict[str, int] = {}
    for a in individuals:
        by_state[str(a.get("state", "?"))] = by_state.get(str(a.get("state", "?")), 0) + 1
        by_type[str(a.get("type", "?"))] = by_type.get(str(a.get("type", "?")), 0) + 1
    return {"total": len(individuals), "by_state": by_state, "by_type": by_type}


def validate_import(domain: str, model: str, config: Dict[str, Any],
                    text: str, fmt: str = "auto") -> Dict[str, Any]:
    """Parse + validate + normalize raw import text.

    Returns ``{"ok", "format", "errors", "warnings", "individuals",
    "summary"}``; ``individuals``/``summary`` are only populated when ``ok``.
    """
    result: Dict[str, Any] = {"ok": False, "format": fmt, "errors": [],
                              "warnings": [], "individuals": [],
                              "summary": {"total": 0, "by_state": {}, "by_type": {}}}
    if not known(domain, model):
        result["errors"].append(_err(None, None, f"未知领域/模型：{domain}/{model}"))
        return result
    if not (text or "").strip():
        result["errors"].append(_err(None, None, "导入内容为空"))
        return result
    config = _full_config(domain, model, config)

    fmt, indexed, errors, warnings = parse_text(text, fmt)
    result["format"] = fmt
    result["warnings"] = warnings
    if errors:
        result["errors"] = errors
        return result
    if not indexed:
        result["errors"].append(_err(None, None, "导入内容不包含任何个体记录"))
        return result
    if len(indexed) > MAX_RECORDS:
        result["errors"].append(_err(None, None, f"记录数（{len(indexed)}）超过上限 "
                                                 f"{MAX_RECORDS}"))
        return result

    out = _validate(domain, model, config, indexed, warnings)
    result.update(out)
    if out["ok"]:
        result["summary"] = _summary(out["individuals"])
    return result


def validate_stored(domain: str, model: str, config: Dict[str, Any],
                    records: Any) -> List[str]:
    """Re-validate a normalized list stored in a scene; flat error strings."""
    if not isinstance(records, list) or not records:
        return ["初始态导入数据为空或格式错误（应为非空对象数组）"]
    if any(not isinstance(r, dict) for r in records):
        return ["初始态导入数据格式错误（每条记录应为对象）"]
    if len(records) > MAX_RECORDS:
        return [f"初始态导入记录数（{len(records)}）超过上限 {MAX_RECORDS}"]
    config = _full_config(domain, model, config)
    out = _validate(domain, model, config, list(enumerate(records)), [])
    return [f"初始态导入：{e['message']}" for e in out["errors"]]


# --------------------------------------------------------------------------- #
# Templates and field documentation
# --------------------------------------------------------------------------- #
def template(domain: str, model: str, fmt: str = "json") -> str:
    """Example import file content for a domain/model."""
    examples = _EXAMPLES[(domain, model)]
    if fmt == "csv":
        fields = [f["name"] for f in _SCHEMAS[(domain, model)]["fields"]]
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in examples:
            writer.writerow(row)
        return buf.getvalue()
    return json.dumps(examples, ensure_ascii=False, indent=2)


def schema_doc(domain: str, model: str, config: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Human-readable field documentation for the config page."""
    config = _full_config(domain, model, config)
    schema = _SCHEMAS[(domain, model)]
    doc = []
    for spec in schema["fields"]:
        parts = []
        if spec.get("required"):
            parts.append("必填")
        else:
            default = spec.get("default")
            if callable(default):
                parts.append("可选（缺省按模型规则推导）")
            elif "default" in spec:
                parts.append(f"可选（默认 {default}）")
            else:
                parts.append("可选（缺省自动分配）")
        if spec["kind"] == "choice":
            parts.append("取值：" + " / ".join(spec["options"]))
        lo = _resolve(spec.get("lo"), config)
        hi = _resolve(spec.get("hi"), config)
        if lo is not None or hi is not None:
            lo_s = f"{lo:g}" if lo is not None else "-∞"
            hi_s = f"{hi:g}" if hi is not None else "+∞"
            parts.append(f"范围 [{lo_s}, {hi_s}]")
        doc.append({"name": spec["name"], "label": spec["label"],
                    "kind": spec["kind"], "desc": "；".join(parts)})
    return doc
