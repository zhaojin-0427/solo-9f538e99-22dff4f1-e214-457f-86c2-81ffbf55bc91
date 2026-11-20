"""请求/响应模型。

年份用整数：公元后为正整数，公元前用负整数（如 -221 表示公元前 221 年）。
年代段为整年闭区间 [year_from, year_to]，一个层位可给多个区间，区间之间允许空档。
"""

from __future__ import annotations

from typing import Optional, Union

from pydantic import BaseModel, Field, field_validator, model_validator


class IntervalIn(BaseModel):
    year_from: int = Field(..., description="区间起始年（含），公元前用负数")
    year_to: int = Field(..., description="区间结束年（含），公元前用负数")

    @field_validator("year_to")
    @classmethod
    def _check_order(cls, value: int, info) -> int:
        start = info.data.get("year_from")
        if start is not None and start > value:
            raise ValueError("year_from 不得晚于 year_to（闭区间整年）")
        return value


class SiteCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=120, description="遗址名称")


class SiteOut(BaseModel):
    id: int
    name: str
    created: str


class StratumCreate(BaseModel):
    trench: str = Field(..., min_length=1, max_length=60, description="探方编号")
    layer_no: str = Field(..., min_length=1, max_length=60, description="层位编号（探方内唯一）")
    intervals: list[IntervalIn] = Field(
        ..., min_length=1, description="可能的沉积年代段，可多段、可有空档"
    )
    note: Optional[str] = Field(None, max_length=400)


class StratumOut(BaseModel):
    id: int
    site_id: int
    trench: str
    layer_no: str
    note: Optional[str]
    intervals: list[IntervalIn]
    created: str


class CorrelationCreate(BaseModel):
    stratum_a_id: int = Field(..., description="待对接层位 A")
    stratum_b_id: int = Field(..., description="待对接层位 B（须与 A 不同探方）")


class CorrelationOut(BaseModel):
    id: int
    site_id: int
    stratum_a_id: int
    stratum_b_id: int
    created: str


class SuperpositionCreate(BaseModel):
    lower_id: int = Field(..., description="下层层位 id")
    upper_id: int = Field(..., description="上层层位 id（不得与下层相同）")


class SuperpositionOut(BaseModel):
    id: int
    site_id: int
    lower_id: int
    upper_id: int
    created: str


class WitnessOut(BaseModel):
    kind: str
    record_id: int
    detail: str


class ContradictionOut(BaseModel):
    reason: str = Field(..., description="EMPTY_DOMAIN | GROUP_CYCLE | ORDER_CONFLICT")
    message: str
    witness: list[WitnessOut]


class GroupEdgeOut(BaseModel):
    from_group: int
    to_group: int
    edge_ids: list[int]


class GroupOut(BaseModel):
    group_id: int
    member_ids: list[int]
    domain: list[list[int]] = Field(..., description="组内共用的原始可能年代域（交集，含空档）")
    feasible_years: list[list[int]] = Field(
        ..., description="与所有其他层位约束可同时成立的全部年代段（含空档）"
    )
    earliest_year: Optional[int]
    latest_year: Optional[int]


class SolutionOut(BaseModel):
    feasible: bool
    version: int = Field(..., description="当前已完成提交的序号，每次成功写入 +1")
    groups: list[GroupOut]
    group_edges: list[GroupEdgeOut]
    group_of: dict[str, int]
    contradiction: Optional[ContradictionOut] = None
    earliest_assignment: Optional[dict[str, int]] = None
    latest_assignment: Optional[dict[str, int]] = None


class WriteResult(BaseModel):
    """所有写接口统一返回：操作对象 + 写入后的整站求解结果。"""

    record: dict
    solution: SolutionOut


# ---------------------------------------------------------------------------
# 探方剖面
# ---------------------------------------------------------------------------

_POINT_X_KEYS = (
    "x",
    "x_mm",
    "distance",
    "dist",
    "offset",
    "horizontal",
    "横距",
)
_POINT_Y_KEYS = (
    "y",
    "y_mm",
    "elevation",
    "elev",
    "elevation_mm",
    "height",
    "vertical",
    "高程",
)


def _unwrap_line(value):
    """折线可能被包在 ``{"points": [...]}`` / ``{"line": [...]}`` 中。"""
    if isinstance(value, dict) and not all(
        isinstance(k, (int, float)) for k in value
    ):
        for key in ("points", "line", "polyline", "points_mm", "vertices"):
            if key in value:
                return value[key]
    return value


def _as_point_dicts(value):
    value = _unwrap_line(value)
    """允许测点写成多种等价形式。

    * ``{"x": .., "y": ..}``（字段可用常见别名）；
    * ``[x, y]``；
    * 扁平数值序列 ``[x1, y1, x2, y2, ...]``；
    * ``{横距: 高程}`` 的映射。
    """
    if value is None:
        return None
    if isinstance(value, dict):
        value = sorted(
            ({"x": x, "y": y} for x, y in value.items()),
            key=lambda point: int(point["x"]),
        )
    elif isinstance(value, (list, tuple)) and value and all(
        isinstance(item, (int, float)) and not isinstance(item, bool)
        for item in value
    ):
        if len(value) % 2 != 0:
            raise ValueError("扁平测点序列长度必须为偶数（x, y 成对）")
        value = [
            {"x": value[i], "y": value[i + 1]} for i in range(0, len(value), 2)
        ]
    out = []
    for point in value:
        if isinstance(point, (list, tuple)):
            if len(point) != 2:
                raise ValueError("测点必须是 {x, y} 或 [x, y]")
            out.append({"x": point[0], "y": point[1]})
            continue
        if not isinstance(point, dict):
            raise ValueError("测点必须是 {x, y} 或 [x, y]")
        normalized = dict(point)
        if "x" not in normalized:
            for key in _POINT_X_KEYS:
                if key in normalized:
                    normalized["x"] = normalized[key]
                    break
        if "y" not in normalized:
            for key in _POINT_Y_KEYS:
                if key in normalized:
                    normalized["y"] = normalized[key]
                    break
        out.append(normalized)
    return out


def _layer_key_id(key):
    try:
        return int(key)
    except (TypeError, ValueError):
        raise ValueError(f"layers 映射的键必须是层位 ID，收到 {key!r}") from None


def _as_layer_list(value):
    """允许 layers 写成列表，或 {层位ID: 定义} 的映射。"""
    if value is None or isinstance(value, list):
        return value
    if isinstance(value, dict):
        out = []
        for key, layer in value.items():
            if not isinstance(layer, dict):
                raise ValueError("layers 映射的值必须是对象")
            item = dict(layer)
            item.setdefault("stratum_id", _layer_key_id(key))
            out.append(item)
        return out
    raise ValueError("layers 必须是列表或映射")


class ProfilePointIn(BaseModel):
    x: int = Field(..., description="横距（整数毫米）")
    y: int = Field(..., description="纵距/高程（整数毫米）")


class ProfileLayerIn(BaseModel):
    stratum_id: Optional[int] = Field(None, description="已登记的层位 ID")
    layer_id: Optional[int] = Field(None, description="stratum_id 的别名")
    id: Optional[int] = Field(None, description="stratum_id 的别名")
    top: Optional[list[ProfilePointIn]] = Field(
        None, description="该层顶线折线，首末横距须为剖面两端"
    )
    bottom: Optional[list[ProfilePointIn]] = Field(
        None, description="该层底线折线，首末横距须为剖面两端"
    )
    top_points: Optional[list[ProfilePointIn]] = Field(
        None, description="top 的别名"
    )
    bottom_points: Optional[list[ProfilePointIn]] = Field(
        None, description="bottom 的别名"
    )
    top_line: Optional[list[ProfilePointIn]] = Field(
        None, description="top 的别名"
    )
    bottom_line: Optional[list[ProfilePointIn]] = Field(
        None, description="bottom 的别名"
    )
    x_min: Optional[int] = Field(
        None, description="该层横距起点；给出时须与剖面及其他各层一致"
    )
    x_max: Optional[int] = Field(
        None, description="该层横距终点；给出时须与剖面及其他各层一致"
    )
    x_range: Optional[list[int]] = Field(
        None, min_length=2, max_length=2, description="[x_min, x_max] 的等价写法"
    )

    _normalize_points = field_validator(
        "top",
        "bottom",
        "top_points",
        "bottom_points",
        "top_line",
        "bottom_line",
        mode="before",
    )(classmethod(lambda cls, v: _as_point_dicts(v)))

    @model_validator(mode="after")
    def _merge_aliases(self):
        if self.stratum_id is None:
            for candidate in (self.layer_id, self.id):
                if candidate is not None:
                    self.stratum_id = candidate
                    break
        if self.stratum_id is None:
            raise ValueError("每层必须给出 stratum_id（或 layer_id）")
        top_aliases = [v for v in (self.top, self.top_points, self.top_line) if v is not None]
        bottom_aliases = [
            v for v in (self.bottom, self.bottom_points, self.bottom_line) if v is not None
        ]
        if len(top_aliases) > 1:
            raise ValueError("顶线只能给出一次（top/top_points/top_line 选一）")
        if len(bottom_aliases) > 1:
            raise ValueError("底线只能给出一次（bottom/bottom_points/bottom_line 选一）")
        self.top = top_aliases[0] if top_aliases else None
        self.bottom = bottom_aliases[0] if bottom_aliases else None
        if self.top is None or self.bottom is None:
            raise ValueError("每层必须给出 top 与 bottom 折线")
        if len(self.top) < 2 or len(self.bottom) < 2:
            raise ValueError("顶线与底线至少各两个测点")
        if self.x_range is not None:
            if self.x_min is None:
                self.x_min = self.x_range[0]
            if self.x_max is None:
                self.x_max = self.x_range[1]
        if (self.x_min is None) != (self.x_max is None):
            raise ValueError("x_min 与 x_max 必须同时给出")
        return self


class SectionCreate(BaseModel):
    """登记探方初版剖面：同一探方各层共用同一横距范围。

    兼容多种写法：``x_min/x_max``（或 ``x_range``）为剖面横距范围；每层给
    ``stratum_id`` 与顶、底折线（``top``/``bottom`` 或 ``top_points``/``bottom_points``，
    测点可写 ``{x, y}`` 或 ``[x, y]``）。若省略剖面范围，则从各层一致的范围推断。
    """

    site_id: Optional[int] = Field(
        None, description="遗址 ID（使用 /sites/{site_id}/... 路径时可省略）"
    )
    trench: Optional[str] = Field(
        None,
        min_length=1,
        max_length=60,
        description="探方编号（使用 /sections 路径时必填；使用 /sections/{trench} 时可省略）",
    )
    x_min: Optional[int] = Field(None, description="剖面横距起点（整数毫米）")
    x_max: Optional[int] = Field(None, description="剖面横距终点（整数毫米，须大于起点）")
    x_range: Optional[list[int]] = Field(
        None, min_length=2, max_length=2, description="[x_min, x_max] 的等价写法"
    )
    x_start: Optional[int] = Field(None, description="x_min 的别名")
    x_end: Optional[int] = Field(None, description="x_max 的别名")
    start_x: Optional[int] = Field(None, description="x_min 的别名")
    end_x: Optional[int] = Field(None, description="x_max 的别名")
    from_x: Optional[int] = Field(None, description="x_min 的别名")
    to_x: Optional[int] = Field(None, description="x_max 的别名")
    layers: Optional[list[ProfileLayerIn]] = Field(
        None, description="各层顶、底折线；层位 ID 须属于本遗址"
    )
    strata: Optional[list[ProfileLayerIn]] = Field(
        None, description="layers 的别名"
    )
    layer_list: Optional[list[ProfileLayerIn]] = Field(
        None, description="layers 的别名"
    )

    _normalize_layers = field_validator(
        "layers", "strata", "layer_list", mode="before"
    )(classmethod(lambda cls, v: _as_layer_list(v)))

    @model_validator(mode="after")
    def _merge_layers(self):
        candidates = [
            v for v in (self.layers, self.strata, self.layer_list) if v is not None
        ]
        if len(candidates) > 1:
            raise ValueError("各层只能给出一次（layers/strata/layer_list 选一）")
        if not candidates:
            raise ValueError("必须给出 layers（各层顶、底折线）")
        if len(candidates[0]) < 1:
            raise ValueError("至少登记一层")
        self.layers = candidates[0]
        return self

    @model_validator(mode="after")
    def _fill_range(self):
        if self.x_min is None:
            for candidate in (self.x_start, self.start_x, self.from_x):
                if candidate is not None:
                    self.x_min = candidate
                    break
        if self.x_max is None:
            for candidate in (self.x_end, self.end_x, self.to_x):
                if candidate is not None:
                    self.x_max = candidate
                    break
        if self.x_range is not None:
            if len(self.x_range) != 2:
                raise ValueError("x_range 必须恰有两个整数")
            if self.x_min is None:
                self.x_min = self.x_range[0]
            if self.x_max is None:
                self.x_max = self.x_range[1]
        if self.x_min is None or self.x_max is None:
            candidates = set()
            for layer in self.layers:
                if layer.x_min is not None:
                    candidates.add((layer.x_min, layer.x_max))
                else:
                    xs = {p.x for p in layer.top} | {p.x for p in layer.bottom}
                    candidates.add((min(xs), max(xs)))
            if len(candidates) == 1:
                self.x_min, self.x_max = candidates.pop()
        if self.x_min is None or self.x_max is None:
            raise ValueError("必须提供 x_min 与 x_max（或 x_range，或各层一致的范围）")
        return self


class RevisionPointIn(BaseModel):
    x: int = Field(..., description="新测点横距（整数毫米，严格在两切口之间）")
    y: int = Field(..., description="新测点高程（整数毫米）")


class LayerRevisionIn(BaseModel):
    stratum_id: Optional[int] = Field(None, description="本次需要返工的层位 ID")
    layer_id: Optional[int] = Field(None, description="stratum_id 的别名")
    id: Optional[int] = Field(None, description="stratum_id 的别名")
    stratum: Optional[int] = Field(None, description="stratum_id 的别名")
    stratumId: Optional[int] = Field(None, description="stratum_id 的别名")
    top_points: list[RevisionPointIn] = Field(
        default_factory=list, description="顶线在切口之间新增的测点（空则直连）"
    )
    bottom_points: list[RevisionPointIn] = Field(
        default_factory=list, description="底线在切口之间新增的测点（空则直连）"
    )
    top: Optional[list[RevisionPointIn]] = Field(
        None, description="top_points 的别名"
    )
    bottom: Optional[list[RevisionPointIn]] = Field(
        None, description="bottom_points 的别名"
    )
    top_line: Optional[list[RevisionPointIn]] = Field(
        None, description="top_points 的别名"
    )
    bottom_line: Optional[list[RevisionPointIn]] = Field(
        None, description="bottom_points 的别名"
    )
    new_top_points: Optional[list[RevisionPointIn]] = Field(
        None, description="top_points 的别名"
    )
    new_bottom_points: Optional[list[RevisionPointIn]] = Field(
        None, description="bottom_points 的别名"
    )
    points: Optional[dict[str, list[RevisionPointIn]]] = Field(
        None, description='可写 {"top": [...], "bottom": [...]}'
    )

    _normalize_points = field_validator(
        "top",
        "bottom",
        "top_points",
        "bottom_points",
        "top_line",
        "bottom_line",
        "new_top_points",
        "new_bottom_points",
        mode="before",
    )(classmethod(lambda cls, v: _as_point_dicts(v)))

    @field_validator("points", mode="before")
    @classmethod
    def _normalize_point_map(cls, value):
        if value is None:
            return None
        if not isinstance(value, dict):
            raise ValueError('points 必须是 {"top": [...], "bottom": [...]}')
        out = {}
        for side, points in value.items():
            if side not in ("top", "bottom"):
                raise ValueError("points 只能包含 top 与 bottom")
            if points is None:
                continue
            out[side] = _as_point_dicts(points)
        return out

    @model_validator(mode="after")
    def _merge_aliases(self):
        if self.stratum_id is None:
            for candidate in (
                self.layer_id,
                self.id,
                self.stratum,
                self.stratumId,
            ):
                if candidate is not None:
                    self.stratum_id = candidate
                    break
        if self.stratum_id is None:
            raise ValueError("每层必须给出 stratum_id（或 layer_id）")
        top_aliases = [
            v
            for v in (self.top, self.top_line, self.new_top_points)
            if v is not None
        ]
        bottom_aliases = [
            v
            for v in (self.bottom, self.bottom_line, self.new_bottom_points)
            if v is not None
        ]
        if self.points:
            if "top" in self.points:
                top_aliases.append(self.points["top"])
            if "bottom" in self.points:
                bottom_aliases.append(self.points["bottom"])
        if len(top_aliases) > 1 or (top_aliases and self.top_points):
            raise ValueError("顶线新测点只能给出一次")
        if len(bottom_aliases) > 1 or (bottom_aliases and self.bottom_points):
            raise ValueError("底线新测点只能给出一次")
        if top_aliases:
            self.top_points = top_aliases[0]
        if bottom_aliases:
            self.bottom_points = bottom_aliases[0]
        return self


class RevisionCreate(BaseModel):
    """复测：提交所见版号、范围内两个整数毫米切口及各层在两切口之间的新测点。

    兼容写法：版号也可用 ``seen_version``/``base_version``/``expected_version``；
    切口也可用 ``cuts``/``cut``/``cut_range``/``x_range``；``layers`` 也可写成
    ``{层位ID: 新测点}`` 的映射。
    """

    site_id: Optional[int] = Field(
        None, description="遗址 ID（使用 /sites/{site_id}/... 路径时可省略）"
    )
    trench: Optional[str] = Field(
        None,
        min_length=1,
        max_length=60,
        description="探方编号（使用 /revisions 路径时必填；使用 /sections/{trench}/revisions 时可省略）",
    )
    version: Optional[int] = Field(None, description="复测时所见剖面版号，必须等于当前版号")
    seen_version: Optional[int] = Field(None, description="version 的别名")
    base_version: Optional[int] = Field(None, description="version 的别名")
    expected_version: Optional[int] = Field(None, description="version 的别名")
    cut_x1: Optional[int] = Field(None, description="左切口横距（整数毫米）")
    cut_x2: Optional[int] = Field(None, description="右切口横距（整数毫米，须大于左切口）")
    cuts: Optional[Union[list[int], dict[str, int]]] = Field(
        None, description="[cut_x1, cut_x2] 或 {\"x1\": .., \"x2\": ..} 的等价写法"
    )
    cut: Optional[Union[list[int], dict[str, int]]] = Field(
        None, description="cuts 的别名"
    )
    cut_range: Optional[Union[list[int], dict[str, int]]] = Field(
        None, description="cuts 的别名"
    )
    cut_x: Optional[Union[list[int], dict[str, int]]] = Field(
        None, description="cuts 的别名"
    )
    x1: Optional[int] = Field(None, description="cut_x1 的别名")
    x2: Optional[int] = Field(None, description="cut_x2 的别名")
    from_x: Optional[int] = Field(None, description="cut_x1 的别名")
    to_x: Optional[int] = Field(None, description="cut_x2 的别名")
    x_from: Optional[int] = Field(None, description="cut_x1 的别名")
    x_to: Optional[int] = Field(None, description="cut_x2 的别名")
    based_on_version: Optional[int] = Field(None, description="version 的别名")
    revision_of: Optional[int] = Field(None, description="version 的别名")
    version_seen: Optional[int] = Field(None, description="version 的别名")
    seen: Optional[int] = Field(None, description="version 的别名")
    layers: Optional[list[LayerRevisionIn]] = Field(
        None, description="本次返工涉及的层位及切口间新测点"
    )
    strata: Optional[list[LayerRevisionIn]] = Field(
        None, description="layers 的别名"
    )
    layer_list: Optional[list[LayerRevisionIn]] = Field(
        None, description="layers 的别名"
    )

    _normalize_layers = field_validator(
        "layers", "strata", "layer_list", mode="before"
    )(classmethod(lambda cls, v: _as_layer_list(v)))

    @model_validator(mode="after")
    def _merge_layers(self):
        candidates = [
            v for v in (self.layers, self.strata, self.layer_list) if v is not None
        ]
        if len(candidates) > 1:
            raise ValueError("各层只能给出一次（layers/strata/layer_list 选一）")
        if not candidates:
            raise ValueError("必须给出 layers（本次返工的层位）")
        self.layers = candidates[0]
        return self

    @model_validator(mode="after")
    def _fill_aliases(self):
        if self.version is None:
            for candidate in (
                self.seen_version,
                self.base_version,
                self.expected_version,
                self.based_on_version,
                self.revision_of,
                self.version_seen,
                self.seen,
            ):
                if candidate is not None:
                    self.version = candidate
                    break
        if self.version is None:
            raise ValueError("必须提供所见版号 version")
        for candidate in (self.cuts, self.cut, self.cut_range, self.cut_x):
            if candidate is None:
                continue
            if isinstance(candidate, dict):
                first = None
                second = None
                for key in ("x1", "cut_x1", "from", "start", "left"):
                    if key in candidate:
                        first = candidate[key]
                        break
                for key in ("x2", "cut_x2", "to", "end", "right"):
                    if key in candidate:
                        second = candidate[key]
                        break
                if first is None or second is None:
                    raise ValueError("切口对象必须包含 x1 与 x2")
            else:
                if len(candidate) != 2:
                    raise ValueError("切口范围必须恰有两个整数")
                first, second = candidate
            if self.cut_x1 is None:
                self.cut_x1 = first
            if self.cut_x2 is None:
                self.cut_x2 = second
        if self.cut_x1 is None:
            for candidate in (self.x1, self.from_x, self.x_from):
                if candidate is not None:
                    self.cut_x1 = candidate
                    break
        if self.cut_x2 is None:
            for candidate in (self.x2, self.to_x, self.x_to):
                if candidate is not None:
                    self.cut_x2 = candidate
                    break
        if self.cut_x1 is None or self.cut_x2 is None:
            raise ValueError("必须提供 cut_x1 与 cut_x2（或 cuts）")
        return self


class ProfileLayerOut(BaseModel):
    stratum_id: int
    trench: str
    layer_no: str
    x_min: int
    x_max: int
    top: list[list] = Field(..., description="顶线折线 [[x, y], ...]；整数保持整数，分数为浮点近似")
    bottom: list[list] = Field(..., description="底线折线，同上")
    top_exact: list[list[str]] = Field(
        ..., description="顶线精确点 [[x, 分子/分母], ...]，可无损重画"
    )
    bottom_exact: list[list[str]] = Field(..., description="底线精确点，同上")
    top_fraction: list[list] = Field(
        ..., description="顶线精确点 [[x, [分子, 分母]], ...]"
    )
    bottom_fraction: list[list] = Field(
        ..., description="底线精确点，同上"
    )
    exact: dict[str, list[list[str]]] = Field(
        ..., description="{'top': 精确点, 'bottom': 精确点}"
    )


class SectionOut(BaseModel):
    id: int
    site_id: int
    trench: str
    version: int
    current_version: int
    x_min: int
    x_max: int
    range: list[int]
    layers: list[ProfileLayerOut]
    layer_map: dict[str, ProfileLayerOut]
    exact: dict[str, dict[str, list[list[str]]]]


class SectionSummaryOut(BaseModel):
    id: int
    site_id: int
    trench: str
    version: int
    current_version: int
    x_min: int
    x_max: int
    layer_ids: list[int]
