"""探方剖面的装配、校验、返工与序列化。

所有写操作都在调用方的事务中完成；本模块只做纯计算与仓储调用，
任何校验失败都抛 :class:`~app.profile_geometry.GeometryError`，由路由层
统一转成拒绝响应并回滚——原图与版号保持原样。
"""

from __future__ import annotations

import sqlite3
from fractions import Fraction
from typing import Any, Iterable

from . import db
from .profile_geometry import (
    GeometryError,
    LayerGeometry,
    Polyline,
    as_fraction,
    fraction_to_exact,
    fraction_to_number,
    fraction_to_pair,
    normalize_new_points,
    splice_polyline,
    validate_layers,
)
from .schemas import ProfileLayerIn, RevisionCreate


# ---------------------------------------------------------------------------
# 仓储 <-> 计算结构
# ---------------------------------------------------------------------------

def _points_to_storage(poly: Polyline) -> list[tuple[int, str, str]]:
    return [
        (x, str(y.numerator), str(y.denominator))
        for x, y in poly
    ]


def _points_from_storage(
    points: Iterable[tuple[int, str, str]]
) -> Polyline:
    return [(x, Fraction(int(num), int(den))) for x, num, den in points]


def _new_polyline_points(points) -> Polyline:
    return [(p.x, as_fraction(p.y)) for p in points]


def _assemble_layers(rows: list[tuple[int, Polyline, Polyline]]) -> dict[int, LayerGeometry]:
    return {sid: (top, bottom) for sid, top, bottom in rows}


def load_section_view(conn: sqlite3.Connection, section: sqlite3.Row) -> dict[str, Any]:
    """读取剖面并装配为对外结构（整数近似 + 精确有理数两种表示）。"""
    layer_rows = db.list_section_layers(conn, section["id"])
    layers: list[dict[str, Any]] = []
    layer_map: dict[str, dict[str, Any]] = {}
    exact_map: dict[str, dict[str, list[list[str]]]] = {}
    for layer in layer_rows:
        stratum = db.get_stratum(conn, layer["stratum_id"])
        if stratum is None:  # 外键保证不会发生；防御性处理
            continue
        top = _points_from_storage(
            db.get_section_points(conn, layer["id"], "top")
        )
        bottom = _points_from_storage(
            db.get_section_points(conn, layer["id"], "bottom")
        )
        top_exact = [[str(x), fraction_to_exact(y)] for x, y in top]
        bottom_exact = [[str(x), fraction_to_exact(y)] for x, y in bottom]
        entry = {
            "stratum_id": layer["stratum_id"],
            "trench": stratum["trench"],
            "layer_no": stratum["layer_no"],
            "x_min": section["x_min"],
            "x_max": section["x_max"],
            "top": [[x, fraction_to_number(y)] for x, y in top],
            "bottom": [[x, fraction_to_number(y)] for x, y in bottom],
            "top_exact": top_exact,
            "bottom_exact": bottom_exact,
            "top_fraction": [[x, fraction_to_pair(y)] for x, y in top],
            "bottom_fraction": [[x, fraction_to_pair(y)] for x, y in bottom],
            "exact": {"top": top_exact, "bottom": bottom_exact},
        }
        layers.append(entry)
        layer_map[str(layer["stratum_id"])] = entry
        exact_map[str(layer["stratum_id"])] = entry["exact"]
    return {
        "id": section["id"],
        "site_id": section["site_id"],
        "trench": section["trench"],
        "version": section["version"],
        "current_version": section["version"],
        "x_min": section["x_min"],
        "x_max": section["x_max"],
        "range": [section["x_min"], section["x_max"]],
        "layers": layers,
        "layer_map": layer_map,
        "exact": exact_map,
    }


def serialize_section_summary(conn: sqlite3.Connection, section: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": section["id"],
        "site_id": section["site_id"],
        "trench": section["trench"],
        "version": section["version"],
        "current_version": section["version"],
        "x_min": section["x_min"],
        "x_max": section["x_max"],
        "layer_ids": [
            r["stratum_id"]
            for r in db.list_section_layers(conn, section["id"])
        ],
    }


# ---------------------------------------------------------------------------
# 初版登记
# ---------------------------------------------------------------------------

def _validate_layer_inputs(
    conn: sqlite3.Connection,
    site_id: int,
    body_layers: list[ProfileLayerIn],
    x_min: int,
    x_max: int,
) -> list[tuple[int, Polyline, Polyline]]:
    if x_min >= x_max:
        raise GeometryError(
            "INVALID_RANGE",
            f"剖面横距范围无效：x_min={x_min} 必须小于 x_max={x_max}",
        )
    seen: set[int] = set()
    rows: list[tuple[int, Polyline, Polyline]] = []
    for layer in body_layers:
        sid = layer.stratum_id
        if sid in seen:
            raise GeometryError(
                "DUPLICATE_LAYER",
                f"层位 {sid} 在本次请求中重复出现",
                {"stratum_id": sid},
            )
        seen.add(sid)
        stratum = db.get_stratum(conn, sid)
        if stratum is None or stratum["site_id"] != site_id:
            raise GeometryError(
                "UNKNOWN_STRATUM",
                f"层位 {sid} 不属于遗址 {site_id} 或不存在",
                {"stratum_id": sid},
            )
        if layer.x_min is not None and (layer.x_min, layer.x_max) != (x_min, x_max):
            raise GeometryError(
                "RANGE_MISMATCH",
                f"层位 {sid} 的横距范围 [{layer.x_min}, {layer.x_max}] "
                f"与剖面 [{x_min}, {x_max}] 不一致",
                {"stratum_id": sid},
            )
        top = _new_polyline_points(layer.top)
        bottom = _new_polyline_points(layer.bottom)
        validate_layers({sid: (top, bottom)}, x_min, x_max)
        rows.append((sid, top, bottom))
    return rows


def validate_new_section(
    conn: sqlite3.Connection,
    site_id: int,
    trench: str,
    x_min: int,
    x_max: int,
    layers: list[ProfileLayerIn],
) -> list[tuple[int, Polyline, Polyline]]:
    rows = _validate_layer_inputs(conn, site_id, layers, x_min, x_max)
    for sid, _top, _bottom in rows:
        stratum = db.get_stratum(conn, sid)
        if stratum["trench"] != trench:
            raise GeometryError(
                "TRENCH_MISMATCH",
                f"层位 {sid} 属于探方 {stratum['trench']}，不能登记到探方 {trench} 的剖面",
                {"stratum_id": sid, "trench": stratum["trench"]},
            )
        existing = db.list_section_layers_for_stratum(conn, sid)
        if existing is not None:
            raise GeometryError(
                "STRATUM_ALREADY_PLACED",
                f"层位 {sid} 已登记在剖面 {existing['section_id']} 中",
                {"stratum_id": sid, "section_id": existing["section_id"]},
            )
    # 整张剖面的跨层规则
    validate_layers(_assemble_layers(rows), x_min, x_max)
    return rows


def create_section(
    conn: sqlite3.Connection,
    site_id: int,
    trench: str,
    x_min: int,
    x_max: int,
    rows: list[tuple[int, Polyline, Polyline]],
) -> sqlite3.Row:
    return db.create_section(
        conn,
        site_id,
        trench,
        x_min,
        x_max,
        [
            (sid, _points_to_storage(top), _points_to_storage(bottom))
            for sid, top, bottom in rows
        ],
        version=1,
    )


# ---------------------------------------------------------------------------
# 复测返工
# ---------------------------------------------------------------------------

def apply_revision(
    conn: sqlite3.Connection,
    section: sqlite3.Row,
    body: RevisionCreate,
) -> list[tuple[int, Polyline, Polyline]]:
    """在事务内拼接新折线并返回待写入的整体行；不写库。

    只替换 ``[cut_x1, cut_x2]``：区间外保留当次复测前的线形（含此前返工
    产生的切口点）；两切口高程由旧线精确插值得到，新点为空时直连。
    """
    if body.version != section["version"]:
        raise GeometryError(
            "STALE_VERSION",
            f"版号已过期：所见版号 {body.version}，当前版号 {section['version']}，"
            "请重新读取剖面后再复测",
            {"expected": section["version"], "received": body.version},
        )

    x_min, x_max = section["x_min"], section["x_max"]
    cut_x1, cut_x2 = body.cut_x1, body.cut_x2
    if not (x_min <= cut_x1 < cut_x2 <= x_max):
        raise GeometryError(
            "CUT_OUT_OF_RANGE",
            f"切口 ({cut_x1}, {cut_x2}) 必须满足 {x_min} <= 左 < 右 <= {x_max}",
            {"x_min": x_min, "x_max": x_max, "cut_x1": cut_x1, "cut_x2": cut_x2},
        )
    x1, x2 = cut_x1, cut_x2

    layer_rows = db.list_section_layers(conn, section["id"])
    current: list[tuple[int, Polyline, Polyline]] = []
    for layer in layer_rows:
        current.append(
            (
                layer["stratum_id"],
                _points_from_storage(db.get_section_points(conn, layer["id"], "top")),
                _points_from_storage(db.get_section_points(conn, layer["id"], "bottom")),
            )
        )

    requested: dict[int, tuple[Polyline, Polyline]] = {}
    for layer in body.layers:
        sid = layer.stratum_id
        if sid in requested:
            raise GeometryError(
                "DUPLICATE_LAYER",
                f"层位 {sid} 在本次请求中重复出现",
                {"stratum_id": sid},
            )
        requested[sid] = (layer.top_points, layer.bottom_points)

    known = {sid for sid, _top, _bottom in current}
    for sid in requested:
        if sid not in known:
            raise GeometryError(
                "UNKNOWN_LAYER",
                f"层位 {sid} 不在本剖面的分层中",
                {"stratum_id": sid},
            )

    revised: list[tuple[int, Polyline, Polyline]] = []
    for sid, top, bottom in current:
        if sid in requested:
            raw_top, raw_bottom = requested[sid]
            new_top_points = normalize_new_points(
                _new_polyline_points(raw_top), x1, x2, f"层位 {sid} 顶线"
            )
            new_bottom_points = normalize_new_points(
                _new_polyline_points(raw_bottom), x1, x2, f"层位 {sid} 底线"
            )
            top = splice_polyline(top, x1, x2, new_top_points)
            bottom = splice_polyline(bottom, x1, x2, new_bottom_points)
        revised.append((sid, top, bottom))

    # 拼接后整张剖面重新校验；失败则整次拒绝。
    validate_layers(_assemble_layers(revised), x_min, x_max)
    return revised


def write_revision(
    conn: sqlite3.Connection,
    section: sqlite3.Row,
    rows: list[tuple[int, Polyline, Polyline]],
) -> sqlite3.Row:
    next_version = section["version"] + 1
    db.replace_section_geometry(
        conn,
        section["id"],
        [
            (sid, _points_to_storage(top), _points_to_storage(bottom))
            for sid, top, bottom in rows
        ],
        next_version,
    )
    db.bump_version(conn)
    return db.get_section_by_id(conn, section["id"])