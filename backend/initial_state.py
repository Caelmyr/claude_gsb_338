"""Initial-state import: parse JSON / CSV individuals and validate per domain.

The engines normally seed their population randomly from density / count
config, which makes a user-chosen initial layout impossible to reproduce.
This module implements *deterministic initial layouts*: an ordered list of
individual dicts (``{"id", "type", "state", "x", "y", ...}``) that fully
replaces the random population of an engine.

Two layers live here:

1. :func:`parse_layout_text` — turn pasted / uploaded JSON or CSV text into a
   raw row list (structural parsing only; never raises on content).
2. :func:`validate_layout` — model-specific, row-by-row validation: format,
   types, coordinate bounds and cross-row contradictions (duplicate cells,
   overlapping vehicles, vehicles off the ring road, infected/imported counts
   exceeding the population).  Every error names the offending row and field.

On success the rows are *canonicalised*: ids missing in the source are
assigned deterministically, enum aliases (``I`` / ``感染`` / ``1`` ...) are
mapped to engine state tokens and optional numeric fields get model defaults.
Feeding the canonical layout to an engine therefore yields exactly the same
individuals every time — which is what makes the config-page preview and the
real run byte-for-byte identical.
"""

from __future__ import annotations

import csv
import io
import json
import math
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from . import catalog
from .engine import make_engine

# Vehicle length used by the IDM ABM (mirrors TrafficABM.defaults()["length"],
# which is not catalog-exposed).  Kept here for the overlap contradiction check.
TRAFFIC_ABM_VEHICLE_LENGTH = 5.0

# --------------------------------------------------------------------------- #
# Result types
# --------------------------------------------------------------------------- #
@dataclass
class ParseResult:
    ok: bool
    rows: List[Dict[str, Any]]
    error: str = ""


@dataclass
class ValidationResult:
    ok: bool
    errors: List[str]
    warnings: List[str]
    layout: List[Dict[str, Any]]
    counts: Dict[str, int]
    config_updates: Dict[str, Any]


# --------------------------------------------------------------------------- #
# Field specifications per (domain, model)
# --------------------------------------------------------------------------- #
# A field spec is: key, CSV/JSON header aliases (lower-cased, stripped),
# value kind ("int" | "float" | "enum"), required?, per-row default or None,
# enum alias table (maps any token -> canonical token).

X = "x"
Y = "y"

# SIR states accept English tokens, Chinese labels and the engine's int codes.
_SIR_ALIASES = {
    "susceptible": "susceptible", "s": "susceptible", "0": "susceptible",
    "易感": "susceptible", "易感者": "susceptible", "susc": "susceptible",
    "infected": "infected", "i": "infected", "1": "infected",
    "感染": "infected", "感染者": "infected", "inf": "infected",
    "recovered": "recovered", "r": "recovered", "2": "recovered",
    "康复": "recovered", "康复者": "recovered", "免疫": "recovered",
    "removed": "recovered", "rec": "recovered",
}
_TRAFFIC_STATE_ALIASES = {
    "moving": "moving", "m": "moving", "行驶": "moving", "运动": "moving",
    "stopped": "stopped", "s": "stopped", "停车": "stopped", "停止": "stopped",
}
_VEHICLE_ALIASES = {"vehicle": "vehicle", "car": "vehicle", "v": "vehicle",
                    "车辆": "vehicle", "车": "vehicle"}
_RABBIT_ALIASES = {"rabbit": "rabbit", "rabbits": "rabbit", "r": "rabbit",
                   "兔子": "rabbit", "兔": "rabbit"}
_FOX_ALIASES = {"fox": "fox", "foxes": "fox", "f": "fox",
                "狐狸": "fox", "狐": "fox"}
_BOID_ALIASES = {"boid": "boid", "boids": "boid", "bird": "boid",
                 "birds": "boid", "b": "boid", "鸟": "boid", "鸟群": "boid"}
_PRED_ALIASES = {"predator": "predator", "predators": "predator", "p": "predator",
                 "捕食者": "predator", "掠食者": "predator"}
_PERSON_ALIASES = {"person": "person", "people": "person", "human": "person",
                   "p": "person", "人": "person", "居民": "person"}


def _spec(key: str, aliases: Tuple[str, ...], kind: str,
          required: bool, default: Any = None,
          enums: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    return {"key": key, "aliases": set(aliases) | {key}, "kind": kind,
            "required": required, "default": default, "enums": enums}


# Canonical per-model field lists.  Their order also defines the CSV template.
SPECS: Dict[str, List[Dict[str, Any]]] = {
    "traffic/ca": [
        _spec("x", ("x", "位置", "格", "pos", "position"), "int", True),
        _spec("y", ("y", "车道", "lane", "车道号"), "int", True),
        _spec("type", ("type", "类型"), "enum", False, "vehicle", _VEHICLE_ALIASES),
        _spec("state", ("state", "状态", "车速状态"), "enum", False,
              "stopped", _TRAFFIC_STATE_ALIASES),
        _spec("v", ("v", "速度", "speed", "车速"), "int", False, 0),
    ],
    "traffic/abm": [
        _spec("x", ("x", "位置", "pos", "position", "米", "里程"), "float", True),
        _spec("y", ("y",), "float", False, 0.0),
        _spec("type", ("type", "类型"), "enum", False, "vehicle", _VEHICLE_ALIASES),
        _spec("state", ("state", "状态"), "enum", False,
              "stopped", _TRAFFIC_STATE_ALIASES),
        _spec("v", ("v", "速度", "speed", "车速"), "float", False, 0.0),
    ],
    "ecology/ca": [
        _spec("x", ("x",), "int", True),
        _spec("y", ("y",), "int", True),
        _spec("type", ("type", "类型", "物种"), "enum", True,
              None, {**_RABBIT_ALIASES, **_FOX_ALIASES}),
        _spec("state", ("state", "状态"), "enum", False, None,
              {**_RABBIT_ALIASES, **_FOX_ALIASES}),
        _spec("energy", ("energy", "能量"), "int", False, None),
        _spec("age", ("age", "年龄"), "int", False, 0),
    ],
    "ecology/abm": [
        _spec("x", ("x",), "float", True),
        _spec("y", ("y",), "float", True),
        _spec("type", ("type", "类型", "物种"), "enum", True,
              None, {**_BOID_ALIASES, **_PRED_ALIASES}),
        _spec("state", ("state", "状态"), "enum", False, None,
              {**_BOID_ALIASES, **_PRED_ALIASES}),
        _spec("vx", ("vx",), "float", False, 0.0),
        _spec("vy", ("vy",), "float", False, 0.0),
    ],
    "epidemic/ca": [
        _spec("x", ("x",), "int", True),
        _spec("y", ("y",), "int", True),
        _spec("type", ("type", "类型"), "enum", False, "person", _PERSON_ALIASES),
        _spec("state", ("state", "状态", "感染状态"), "enum", False,
              "susceptible", _SIR_ALIASES),
        _spec("days", ("days", "感染天数"), "int", False, 0),
    ],
    "epidemic/abm": [
        _spec("x", ("x",), "float", True),
        _spec("y", ("y",), "float", True),
        _spec("type", ("type", "类型"), "enum", False, "person", _PERSON_ALIASES),
        _spec("state", ("state", "状态", "感染状态"), "enum", False,
              "susceptible", _SIR_ALIASES),
        _spec("days", ("days", "感染天数"), "int", False, 0),
        _spec("heading", ("heading", "朝向", "方向"), "float", False, None),
        _spec("hx", ("hx", "home_x"), "float", False, None),
        _spec("hy", ("hy", "home_y"), "float", False, None),
        _spec("hr", ("hr", "home_r"), "float", False, None),
    ],
}

# Free-text hints shown above the import box, per model.
HINTS: Dict[str, str] = {
    "traffic/ca": "每行一辆车：x=格位置（0..长度-1），y=车道号（0..车道数-1），每格至多一辆车。",
    "traffic/abm": "每行一辆车：x=沿环形道路的米数（0..道路长度），车辆按车头时距排列，不能重叠或越过环道端点。",
    "ecology/ca": "每行一只动物：x/y 为整数格坐标，type=rabbit(兔子) 或 fox(狐狸)，每格至多一只。",
    "ecology/abm": "每行一个个体：type=boid(鸟) 或 predator(捕食者)，x/y 为连续坐标，同一坐标不可重复。",
    "epidemic/ca": "每行一个栅格上的人：x/y 为整数格坐标，每格一人；state=S/I/R（可用“易感/感染/康复”）。未列出的格默认是易感者。",
    "epidemic/abm": "每行一个人：x/y 为连续坐标，state=S/I/R；列表即全部人口，行数即人口规模。",
}


def known_model(domain: str, model: str) -> bool:
    return catalog.known_model(domain, model)


# --------------------------------------------------------------------------- #
# Merged config (same precedence the run manager uses)
# --------------------------------------------------------------------------- #
def resolved_config(domain: str, model: str,
                    config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    merged = catalog.model_defaults(domain, model)
    merged.update(config or {})
    return merged


# --------------------------------------------------------------------------- #
# Parsing (JSON / CSV -> raw rows)
# --------------------------------------------------------------------------- #
def parse_layout_text(text: str, fmt: str) -> ParseResult:
    """Parse ``text`` (``"json"`` or ``"csv"``) into a raw row list.

    JSON accepts either a bare array or ``{"individuals": [...]}``.  Parsing
    errors are reported as a single document error; per-row value problems are
    left to :func:`validate_layout`.
    """
    text = (text or "").strip()
    if not text:
        return ParseResult(False, [], "导入内容为空")
    if fmt == "json":
        return _parse_json(text)
    if fmt == "csv":
        return _parse_csv(text)
    return ParseResult(False, [], f"未知格式: {fmt}")


def _parse_json(text: str) -> ParseResult:
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as exc:
        return ParseResult(False, [], f"JSON 解析失败：第 {exc.lineno} 行第 {exc.colno} 列：{exc.msg}")
    if isinstance(doc, dict) and "individuals" in doc:
        doc = doc["individuals"]
    if not isinstance(doc, list):
        return ParseResult(False, [], "JSON 顶层必须是个体数组，或含 individuals 数组的对象")
    rows: List[Dict[str, Any]] = []
    for i, item in enumerate(doc):
        if not isinstance(item, dict):
            return ParseResult(False, [], f"第 {i + 1} 行不是对象（个体必须是 JSON 对象）")
        rows.append(dict(item))
    return ParseResult(True, rows)


def _parse_csv(text: str) -> ParseResult:
    if text.startswith("﻿"):
        text = text[1:]
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        return ParseResult(False, [], "CSV 缺少表头行")
    header = [h.strip() for h in header]
    if not any(header):
        return ParseResult(False, [], "CSV 表头为空")
    seen: Dict[str, int] = {}
    for name in header:
        seen[name.lower()] = seen.get(name.lower(), 0) + 1
    dupes = sorted(n for n, c in seen.items() if c > 1)
    if dupes:
        return ParseResult(False, [], f"CSV 表头存在重复列: {', '.join(dupes)}")
    rows: List[Dict[str, Any]] = []
    for line_no, raw in enumerate(reader, start=2):
        if not any((c or "").strip() for c in raw):
            continue  # blank line
        if len(raw) > len(header):
            return ParseResult(False, [],
                               f"第 {line_no} 行列数（{len(raw)}）多于表头列数（{len(header)}）")
        row = {}
        for name, val in zip(header, raw):
            row[name] = val
        row["__line__"] = line_no
        rows.append(row)
    if not rows:
        return ParseResult(False, [], "CSV 只有表头，没有任何个体行")
    return ParseResult(True, rows)


# --------------------------------------------------------------------------- #
# Value coercion helpers
# --------------------------------------------------------------------------- #
def _is_bool(v: Any) -> bool:
    return isinstance(v, bool)


def _coerce_int(v: Any) -> Optional[int]:
    if _is_bool(v):
        raise ValueError("不是整数（布尔值）")
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        if math.isfinite(v) and v.is_integer():
            return int(v)
        raise ValueError("不是整数")
    if isinstance(v, str):
        s = v.strip()
        if s == "":
            return None
        try:
            f = float(s)
        except ValueError:
            raise ValueError("不是整数")
        if math.isfinite(f) and f.is_integer():
            return int(f)
        raise ValueError("不是整数")
    raise ValueError("不是整数")


def _coerce_float(v: Any) -> Optional[float]:
    if _is_bool(v):
        raise ValueError("不是数字（布尔值）")
    if isinstance(v, (int, float)):
        f = float(v)
    elif isinstance(v, str):
        s = v.strip()
        if s == "":
            return None
        try:
            f = float(s)
        except ValueError:
            raise ValueError("不是数字")
    else:
        raise ValueError("不是数字")
    if not math.isfinite(f):
        raise ValueError("必须是有限数值（不能是 NaN / Infinity）")
    return f


def _enum_value(v: Any, aliases: Dict[str, str],
                label: str) -> Tuple[Optional[str], Optional[str]]:
    """Return (canonical, error); blank -> (None, None) so defaults can apply."""
    if v is None:
        return None, None
    if isinstance(v, str) and v.strip() == "":
        return None, None
    if isinstance(v, (int, float)) and not _is_bool(v):
        key = str(int(v)) if float(v).is_integer() else str(v)
    else:
        key = str(v).strip().lower()
    if key in aliases:
        return aliases[key], None
    allowed = sorted({c for c in aliases.values()})
    return None, f"{label} 取值非法（收到：{v}），允许: {', '.join(allowed)}"


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #
def validate_layout(domain: str, model: str,
                    raw_rows: List[Dict[str, Any]],
                    config: Optional[Dict[str, Any]] = None) -> ValidationResult:
    """Validate raw parsed rows against the rules of ``domain/model``.

    All errors found are collected in one pass; each message starts with the
    1-based row number (CSV rows additionally carry the physical file line).
    """
    key = f"{domain}/{model}"
    cfg = resolved_config(domain, model, config)
    errors: List[str] = []
    warnings: List[str] = []
    layout: List[Optional[Dict[str, Any]]] = [None] * len(raw_rows)
    specs = SPECS[key]
    header_alias = {"id": "id", "编号": "id", "个体id": "id", "个体编号": "id"}
    for sp in specs:
        header_alias.update({a: sp["key"] for a in sp["aliases"]})
        header_alias[sp["key"]] = sp["key"]

    used_ids: Dict[str, int] = {}

    for i, raw in enumerate(raw_rows):
        loc = _row_locator(i, raw)
        row_errs: List[str] = []
        row: Dict[str, Any] = {}

        # Map CSV / alias headers to canonical keys (JSON mostly already is).
        mapped: Dict[str, Any] = {}
        unknown: List[str] = []
        for k, v in raw.items():
            if k == "__line__":
                continue
            canon = header_alias.get(str(k).strip().lower())
            if canon is None:
                unknown.append(str(k))
            else:
                mapped[canon] = v
        if unknown:
            row_errs.append(f"未知字段: {', '.join(unknown)}（该模型字段见下方模板）")

        for sp in specs:
            k = sp["key"]
            val = mapped.get(k)
            blank = val is None or (isinstance(val, str) and val.strip() == "")
            if blank:
                if sp["required"]:
                    row_errs.append(f"字段 {k} 缺失")
                continue
            try:
                if sp["kind"] == "int":
                    row[k] = _coerce_int(val)
                elif sp["kind"] == "float":
                    row[k] = _coerce_float(val)
                else:
                    canon, err = _enum_value(val, sp["enums"], k)
                    if err:
                        row_errs.append(err)
                    else:
                        row[k] = canon
            except ValueError as exc:
                row_errs.append(f"字段 {k} {exc}（收到：{val!r}）".replace("'", ""))

        # Per-model semantic checks fill in defaults only when no error so far,
        # but we keep best-effort values to run cross-row checks afterwards.
        _model_row_checks(key, cfg, row, row_errs)

        raw_id = mapped.get("id")
        if isinstance(raw_id, str) and raw_id.strip() == "":
            raw_id = None
        rid = str(raw_id).strip() if raw_id is not None else None
        if rid is not None:
            row["id"] = rid
            if rid in used_ids:
                row_errs.append(f"id 重复：{rid}（已在第 {used_ids[rid] + 1} 行出现）")
            else:
                used_ids[rid] = i

        if row_errs:
            errors.extend(f"{loc}：{e}" for e in row_errs)
        else:
            layout[i] = row

    # ---- cross-row / global checks ---------------------------------------- #
    good_idx = [i for i, r in enumerate(layout) if r is not None]
    _model_cross_checks(key, cfg, layout, good_idx, errors, warnings)

    if errors:
        return ValidationResult(False, errors, warnings, [], {}, {})

    # ---- canonicalisation (deterministic ids / defaults) ------------------ #
    canonical = _canonicalise(key, cfg, [layout[i] for i in good_idx])
    # An explicitly supplied id may collide with an id the engine assigns by
    # convention (epidemic CA ids derive from cell coordinates); reject those.
    seen_final: Dict[str, int] = {}
    for k, row in enumerate(canonical):
        rid = row["id"]
        if rid in seen_final:
            errors.append(f"第 {good_idx[k] + 1} 行：id {rid} 与第 "
                          f"{good_idx[seen_final[rid]] + 1} 行冲突（与引擎按位置分配的 id 撞号，请更换 id 或留空）")
        else:
            seen_final[rid] = k
    if errors:
        return ValidationResult(False, errors, warnings, [], {}, {})
    counts, updates, warns = _summary_and_sync(key, cfg, canonical)
    warnings.extend(warns)
    return ValidationResult(True, [], warnings, canonical, counts, updates)


def _row_locator(i: int, raw: Dict[str, Any]) -> str:
    line = raw.get("__line__")
    return f"第 {line} 行（第 {i + 1} 条）" if line else f"第 {i + 1} 行"


# --------------------------------------------------------------------------- #
# Per-model row-level checks
# --------------------------------------------------------------------------- #
def _model_row_checks(key: str, cfg: Dict[str, Any],
                      row: Dict[str, Any], errs: List[str]) -> None:
    if key == "traffic/ca":
        _check_ca_xy(row, int(cfg["length"]), int(cfg["lanes"]), errs,
                     xname="x", yname="y", xlabel="格位置", ylabel="车道")
        if "v" in row and (row["v"] < 0 or row["v"] > int(cfg["vmax"])):
            errs.append(f"速度 v={row['v']} 越界，允许 0..{int(cfg['vmax'])}（最高速度）")
        if row.get("type") not in (None, "vehicle"):
            errs.append(f"类型 type={row.get('type')} 非法，交通 CA 只能是 vehicle（车辆）")
        if row.get("state") not in (None, "moving", "stopped"):
            errs.append(f"状态 state={row.get('state')} 非法，允许 moving/stopped（行驶/停车）")

    elif key == "traffic/abm":
        L = float(cfg["road_length"])
        if "x" in row and not (0.0 <= row["x"] < L):
            errs.append(f"坐标越界 x={row['x']}：车辆必须落在环形道路上，允许 [0, {L:g}) 米")
        if "y" in row and abs(row["y"]) > 1e-9:
            errs.append(f"坐标越界 y={row['y']}：环形道路只有一条车道，y 必须为 0（车辆落到道路之外）")
        if "v" in row and not (0.0 <= row["v"] <= float(cfg["v0"]) + 1e-9):
            errs.append(f"速度 v={row['v']} 越界，允许 0..{cfg['v0']:g}（期望速度 v0）")
        if row.get("type") not in (None, "vehicle"):
            errs.append(f"类型 type={row.get('type')} 非法，交通模型只能是 vehicle（车辆）")
        if row.get("state") not in (None, "moving", "stopped"):
            errs.append(f"状态 state={row.get('state')} 非法，允许 moving/stopped（行驶/停车）")

    elif key == "ecology/ca":
        _check_ca_xy(row, int(cfg["width"]), int(cfg["height"]), errs)
        typ = row.get("type")
        if typ not in (None, "rabbit", "fox"):
            errs.append(f"类型 type={typ} 非法，允许 rabbit(兔子)/fox(狐狸)")
        if row.get("state") not in (None, typ):
            errs.append(f"状态 state={row.get('state')} 与类型 type={typ} 矛盾")
        if "energy" in row and row["energy"] < 0:
            errs.append(f"能量 energy={row['energy']} 不能为负")
        if "age" in row and row["age"] < 0:
            errs.append(f"年龄 age={row['age']} 不能为负")

    elif key == "ecology/abm":
        _check_abm_xy(row, float(cfg["width"]), float(cfg["height"]), errs)
        typ = row.get("type")
        if typ not in (None, "boid", "predator"):
            errs.append(f"类型 type={typ} 非法，允许 boid(鸟)/predator(捕食者)")
        if row.get("state") not in (None, typ):
            errs.append(f"状态 state={row.get('state')} 与类型 type={typ} 矛盾")

    elif key == "epidemic/ca":
        _check_ca_xy(row, int(cfg["width"]), int(cfg["height"]), errs)
        if row.get("type") not in (None, "person"):
            errs.append(f"类型 type={row.get('type')} 非法，传染病模型只能是 person（人）")
        if row.get("state") not in (None, "susceptible", "infected", "recovered"):
            errs.append(f"状态 state={row.get('state')} 非法，允许 S/I/R（易感/感染/康复）")
        if "days" in row and row["days"] < 0:
            errs.append(f"感染天数 days={row['days']} 不能为负")

    elif key == "epidemic/abm":
        _check_abm_xy(row, float(cfg["width"]), float(cfg["height"]), errs)
        if row.get("type") not in (None, "person"):
            errs.append(f"类型 type={row.get('type')} 非法，传染病模型只能是 person（人）")
        if row.get("state") not in (None, "susceptible", "infected", "recovered"):
            errs.append(f"状态 state={row.get('state')} 非法，允许 S/I/R（易感/感染/康复）")
        if "days" in row and row["days"] < 0:
            errs.append(f"感染天数 days={row['days']} 不能为负")
        for hk in ("hx", "hy", "hr"):
            if hk in row and row[hk] is not None:
                pass  # finite-ness already checked; home points may be off-world relative coords


def _check_ca_xy(row: Dict[str, Any], w: int, h: int, errs: List[str],
                 xname: str = "x", yname: str = "y",
                 xlabel: str = "x", ylabel: str = "y") -> None:
    if xname in row and not (0 <= row[xname] < w):
        errs.append(f"坐标越界 {xname}={row[xname]}，{xlabel}允许 0..{w - 1}")
    if yname in row and not (0 <= row[yname] < h):
        errs.append(f"坐标越界 {yname}={row[yname]}，{ylabel}允许 0..{h - 1}")


def _check_abm_xy(row: Dict[str, Any], w: float, h: float,
                  errs: List[str]) -> None:
    if "x" in row and not (0.0 <= row["x"] < w):
        errs.append(f"坐标越界 x={row['x']}，世界范围 [0, {w:g})")
    if "y" in row and not (0.0 <= row["y"] < h):
        errs.append(f"坐标越界 y={row['y']}，世界范围 [0, {h:g})")


# --------------------------------------------------------------------------- #
# Per-model cross-row checks (contradictions)
# --------------------------------------------------------------------------- #
def _model_cross_checks(key: str, cfg: Dict[str, Any],
                        rows: List[Optional[Dict[str, Any]]],
                        good: List[int],
                        errs: List[str], warnings: List[str]) -> None:
    if key in ("traffic/ca", "ecology/ca", "epidemic/ca"):
        cells: Dict[Tuple[int, int], int] = {}
        for i in good:
            r = rows[i]
            cell = (r["x"], r["y"])
            if cell in cells:
                errs.append(f"第 {i + 1} 行与第 {cells[cell] + 1} 行占据同一格 (x={cell[0]}, y={cell[1]})")
            else:
                cells[cell] = i
        if not good:
            errs.append("个体列表为空：至少需要导入一个个体")
        if key == "ecology/ca":
            cap = int(cfg["width"]) * int(cfg["height"])
            if len(good) > cap:
                errs.append(f"动物总数 {len(good)} 超过栅格容量 {cap}（width×height）")
        if key == "traffic/ca":
            cap = int(cfg["length"]) * int(cfg["lanes"])
            if len(good) > cap:
                errs.append(f"车辆总数 {len(good)} 超过道路容量 {cap}（长度×车道数）")

    elif key in ("ecology/abm", "epidemic/abm"):
        seen: Dict[Tuple[Any, Any], int] = {}
        for i in good:
            r = rows[i]
            pos = (round(r["x"], 9), round(r["y"], 9))
            if pos in seen:
                errs.append(f"第 {i + 1} 行与第 {seen[pos] + 1} 行坐标完全相同 (x={r['x']}, y={r['y']})，重复占据同一位置")
            else:
                seen[pos] = i
        if key == "epidemic/abm":
            # "Infected > total" cannot occur with valid enums; counts are
            # reported back to the UI instead.  Empty populations are rejected
            # here because an SIR run without people is meaningless.
            if len(good) == 0:
                errs.append("个体列表为空：传染病 ABM 至少需要一个人")
        elif key == "ecology/abm" and len(good) == 0:
            errs.append("个体列表为空：至少需要导入一只鸟或捕食者")

    elif key == "traffic/abm":
        L = float(cfg["road_length"])
        veh_len = TRAFFIC_ABM_VEHICLE_LENGTH
        order = sorted(good, key=lambda i: rows[i]["x"])
        n_cars = len(order)
        for k, i in enumerate(order):
            j = order[(k + 1) % n_cars]
            a, b = rows[i], rows[j]
            # Forward centre-to-centre gap along the ring (the only physical
            # gap when two cars share a ring; == 0 means a duplicated cell).
            centre_gap = (b["x"] - a["x"]) % L
            if n_cars > 1 and centre_gap < veh_len:
                errs.append(
                    f"第 {i + 1} 行 (x={a['x']:g}) 与前车第 {j + 1} 行 (x={b['x']:g}) "
                    f"在环形道路上重叠（间距 {centre_gap:g}m 小于车长 {veh_len:g}m，"
                    f"车辆落到道路之外/相互穿透）")
        if n_cars * veh_len > L:
            errs.append(f"车辆总长度 {n_cars * veh_len:g}m 超过环形道路长度 {L:g}m，车辆必然重叠")


# --------------------------------------------------------------------------- #
# Canonicalisation + counts / config sync
# --------------------------------------------------------------------------- #
def _canonicalise(key: str, cfg: Dict[str, Any],
                  rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Fill defaults and assign deterministic ids in import order."""
    out: List[Dict[str, Any]] = []
    counters: Dict[str, int] = {}
    w = int(cfg.get("width", 0))
    movement = cfg.get("movement", "random_walk")

    for i, r in enumerate(rows):
        c: Dict[str, Any] = {}

        if key == "traffic/ca":
            n = counters.get("v", 0); counters["v"] = n + 1
            c = {"id": r.get("id") or f"v{n:04d}", "type": "vehicle",
                 "state": r.get("state", "stopped"),
                 "x": int(r["x"]), "y": int(r["y"]), "v": int(r.get("v", 0))}

        elif key == "traffic/abm":
            n = counters.get("v", 0); counters["v"] = n + 1
            c = {"id": r.get("id") or f"v{n:04d}", "type": "vehicle",
                 "state": r.get("state", "stopped"),
                 "x": round(float(r["x"]), 2), "y": 0.0,
                 "v": float(r.get("v", 0.0))}

        elif key == "ecology/ca":
            typ = r["type"]
            n = counters.get(typ, 0); counters[typ] = n + 1
            prefix = "rb" if typ == "rabbit" else "fx"
            default_energy = (int(cfg["rabbit_repro"]) if typ == "rabbit"
                              else int(cfg["fox_repro"])) // 2
            energy = r.get("energy")
            if energy is None:
                energy = default_energy
            c = {"id": r.get("id") or f"{prefix}{n:05d}", "type": typ,
                 "state": typ, "x": int(r["x"]), "y": int(r["y"]),
                 "energy": int(energy), "age": int(r.get("age", 0))}

        elif key == "ecology/abm":
            typ = r["type"]
            n = counters.get(typ, 0); counters[typ] = n + 1
            prefix = "b" if typ == "boid" else "p"
            c = {"id": r.get("id") or f"{prefix}{n:04d}", "type": typ,
                 "state": typ, "x": float(r["x"]), "y": float(r["y"]),
                 "vx": float(r.get("vx", 0.0)), "vy": float(r.get("vy", 0.0))}

        elif key == "epidemic/ca":
            x, y = int(r["x"]), int(r["y"])
            default_id = f"p{y * w + x:05d}"
            c = {"id": r.get("id") or default_id, "type": "person",
                 "state": r.get("state", "susceptible"),
                 "x": x, "y": y, "days": int(r.get("days", 0))}

        elif key == "epidemic/abm":
            state = r.get("state", "susceptible")
            heading = r.get("heading")
            if heading is None:
                # Deterministic spread of headings (independent of the RNG),
                # so the preview and the run agree exactly.
                heading = (i * 2.39996323) % (2 * math.pi)
            c = {"id": r.get("id") or f"p{i:05d}", "type": "person",
                 "state": state, "x": float(r["x"]), "y": float(r["y"]),
                 "heading": float(heading), "days": int(r.get("days", 0))}
            if movement == "home_range":
                c["hx"] = float(r["hx"]) if r.get("hx") is not None else float(r["x"])
                c["hy"] = float(r["hy"]) if r.get("hy") is not None else float(r["y"])
                c["hr"] = float(r["hr"]) if r.get("hr") is not None else 50.0

        out.append(c)
    return out


def _summary_and_sync(key: str, cfg: Dict[str, Any],
                      rows: List[Dict[str, Any]]
                      ) -> Tuple[Dict[str, int], Dict[str, Any], List[str]]:
    """Return counts, config overrides implied by the layout, and warnings.

    When a deterministic layout exists it *is* the population: count/density
    config params are overridden to match it so every later code path (reports,
    experiments, resets) sees consistent numbers.  A mismatch with what the
    parameter form currently holds is surfaced as a warning, and the frontend
    syncs the corresponding input.
    """
    warns: List[str] = []
    counts: Dict[str, int] = {"total": len(rows)}
    updates: Dict[str, Any] = {}
    n = len(rows)

    if key == "traffic/ca":
        L, lanes = int(cfg["length"]), int(cfg["lanes"])
        density = round(n / (L * lanes), 4) if L * lanes else 0.0
        if abs(float(cfg.get("density", -1)) - density) > 1e-9:
            warns.append(f"导入 {n} 辆车，密度参数已按布局同步为 {density:g}（随机密度不再生效）")
        updates = {"density": density}
        counts["vehicles"] = n
        counts["stopped"] = sum(1 for r in rows if r["state"] == "stopped")

    elif key == "traffic/abm":
        if int(cfg.get("n", -1)) != n:
            warns.append(f"导入 {n} 辆车，车辆数参数 n 已同步为 {n}（随机生成不再生效）")
        updates = {"n": n}
        counts["vehicles"] = n
        counts["stopped"] = sum(1 for r in rows if r["state"] == "stopped")

    elif key == "ecology/ca":
        nr = sum(1 for r in rows if r["type"] == "rabbit")
        nf = n - nr
        if int(cfg.get("n_rabbits", -1)) != nr:
            warns.append(f"初始兔子数已按布局同步为 {nr}")
        if int(cfg.get("n_foxes", -1)) != nf:
            warns.append(f"初始狐狸数已按布局同步为 {nf}")
        updates = {"n_rabbits": nr, "n_foxes": nf}
        counts["rabbits"] = nr
        counts["foxes"] = nf

    elif key == "ecology/abm":
        nb = sum(1 for r in rows if r["type"] == "boid")
        npr = n - nb
        if int(cfg.get("n_boids", -1)) != nb:
            warns.append(f"鸟群个体数已按布局同步为 {nb}")
        if int(cfg.get("n_predators", -1)) != npr:
            warns.append(f"捕食者数量已按布局同步为 {npr}")
        updates = {"n_boids": nb, "n_predators": npr}
        counts["boids"] = nb
        counts["predators"] = npr

    elif key == "epidemic/ca":
        ni = sum(1 for r in rows if r["state"] == "infected")
        nr = sum(1 for r in rows if r["state"] == "recovered")
        total = int(cfg["width"]) * int(cfg["height"])
        if ni > total:
            warns.append(f"感染人数 {ni} 超过栅格总人数 {total}")
        if int(cfg.get("initial_infected", -1)) != ni:
            warns.append(f"初始感染者已按布局同步为 {ni}")
        updates = {"initial_infected": ni, "vaccination_rate": 0.0}
        counts.update({"susceptible": total - ni - nr, "infected": ni,
                       "recovered": nr, "total_population": total})

    elif key == "epidemic/abm":
        ni = sum(1 for r in rows if r["state"] == "infected")
        nr2 = sum(1 for r in rows if r["state"] == "recovered")
        ns = n - ni - nr2
        if int(cfg.get("n", -1)) != n:
            warns.append(f"人口规模 n 已按布局同步为 {n}（随机生成不再生效）")
        if int(cfg.get("initial_infected", -1)) != ni:
            warns.append(f"初始感染者已按布局同步为 {ni}")
        updates = {"n": n, "initial_infected": ni, "vaccination_rate": 0.0}
        counts.update({"susceptible": ns, "infected": ni, "recovered": nr2})

    return counts, updates, warns


# --------------------------------------------------------------------------- #
# Preview — build the exact engine the run will use
# --------------------------------------------------------------------------- #
def build_preview(domain: str, model: str,
                  config: Optional[Dict[str, Any]],
                  layout: List[Dict[str, Any]],
                  seed: Optional[int] = None) -> Dict[str, Any]:
    """Instantiate the engine with a validated canonical layout.

    The returned snapshot is produced by the same factory and config
    resolution :mod:`backend.run_manager` uses, so the distribution shown on
    the config page is identical to a freshly created run's step-0 snapshot.
    """
    cfg = resolved_config(domain, model, config)
    if seed is None:
        seed = int(cfg.get("seed", 0) or 0)
    engine = make_engine(domain, model, config=cfg, seed=seed,
                         initial_state=layout)
    return engine.snapshot()


# --------------------------------------------------------------------------- #
# Templates + field metadata for the UI
# --------------------------------------------------------------------------- #
def template_text(domain: str, model: str, fmt: str) -> str:
    """Return an importable example document for ``domain/model``."""
    key = f"{domain}/{model}"
    if fmt == "csv":
        return _TEMPLATE_CSV[key]
    return _TEMPLATE_JSON[key]


_TEMPLATE_CSV = {
    "traffic/ca": ("x,y,type,state,v\n"
                   "0,0,vehicle,stopped,0\n"
                   "5,0,vehicle,moving,2\n"
                   "12,1,vehicle,stopped,0\n"),
    "traffic/abm": ("x,type,state,v\n"
                    "0.0,vehicle,stopped,0\n"
                    "25.0,vehicle,moving,12.5\n"
                    "48.5,vehicle,moving,18.0\n"),
    "ecology/ca": ("x,y,type,energy,age\n"
                   "3,4,rabbit,4,0\n"
                   "10,10,rabbit,4,1\n"
                   "7,8,fox,6,0\n"),
    "ecology/abm": ("x,y,type,vx,vy\n"
                    "20,30,boid,1,0\n"
                    "120,80,boid,-1,0.5\n"
                    "200,200,predator,0,0\n"),
    "epidemic/ca": ("x,y,state,days\n"
                    "10,10,infected,0\n"
                    "11,10,infected,0\n"
                    "30,30,recovered,3\n"),
    "epidemic/abm": ("x,y,state,days\n"
                     "50.0,50.0,infected,0\n"
                     "52.5,50.0,susceptible,0\n"
                     "200.0,120.0,recovered,5\n"),
}

_TEMPLATE_JSON = {
    "traffic/ca": json.dumps({"individuals": [
        {"x": 0, "y": 0, "type": "vehicle", "state": "stopped", "v": 0},
        {"x": 5, "y": 0, "type": "vehicle", "state": "moving", "v": 2},
        {"x": 12, "y": 1, "type": "vehicle", "state": "stopped", "v": 0}]},
        ensure_ascii=False, indent=2) + "\n",
    "traffic/abm": json.dumps({"individuals": [
        {"x": 0.0, "type": "vehicle", "state": "stopped", "v": 0},
        {"x": 25.0, "type": "vehicle", "state": "moving", "v": 12.5},
        {"x": 48.5, "type": "vehicle", "state": "moving", "v": 18.0}]},
        ensure_ascii=False, indent=2) + "\n",
    "ecology/ca": json.dumps({"individuals": [
        {"x": 3, "y": 4, "type": "rabbit", "energy": 4, "age": 0},
        {"x": 10, "y": 10, "type": "rabbit", "energy": 4, "age": 1},
        {"x": 7, "y": 8, "type": "fox", "energy": 6, "age": 0}]},
        ensure_ascii=False, indent=2) + "\n",
    "ecology/abm": json.dumps({"individuals": [
        {"x": 20, "y": 30, "type": "boid", "vx": 1, "vy": 0},
        {"x": 120, "y": 80, "type": "boid", "vx": -1, "vy": 0.5},
        {"x": 200, "y": 200, "type": "predator", "vx": 0, "vy": 0}]},
        ensure_ascii=False, indent=2) + "\n",
    "epidemic/ca": json.dumps({"individuals": [
        {"x": 10, "y": 10, "state": "infected", "days": 0},
        {"x": 11, "y": 10, "state": "infected", "days": 0},
        {"x": 30, "y": 30, "state": "recovered", "days": 3}]},
        ensure_ascii=False, indent=2) + "\n",
    "epidemic/abm": json.dumps({"individuals": [
        {"x": 50.0, "y": 50.0, "state": "infected", "days": 0},
        {"x": 52.5, "y": 50.0, "state": "susceptible", "days": 0},
        {"x": 200.0, "y": 120.0, "state": "recovered", "days": 5}]},
        ensure_ascii=False, indent=2) + "\n",
}


def field_metadata(domain: str, model: str) -> List[Dict[str, Any]]:
    """Serializable per-field description for the frontend."""
    out = []
    for sp in SPECS[f"{domain}/{model}"]:
        allowed = sorted({c for c in (sp["enums"] or {}).values()})
        out.append({"key": sp["key"], "required": sp["required"],
                    "kind": sp["kind"], "default": sp["default"],
                    "allowed": allowed})
    return out
