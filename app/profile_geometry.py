"""探方剖面折线的纯几何运算（整数毫米测点 + 精确有理数高程）。

坐标约定
--------
* 横距 ``x`` 一律为整数毫米，且同一剖面的所有层位共用同一个 ``[x_min, x_max]``。
* 纵距 ``y`` 的输入测点为整数毫米；但切口若落在两个测点之间，其高程由旧折线
  线性插值得到，可能是分数。内部一律使用 :class:`fractions.Fraction` 保存，
  绝不取整，因此任意一次返工之后都能精确重画。

一次返工只替换 ``[cut_x1, cut_x2]`` 区间：区间外的旧折线（含此前返工产生的
切口点）原样保留；区间内由两个切口处的旧线高程和新测点重新连接。若没有新测点，
两个切口直接相连。
"""

from __future__ import annotations

from bisect import bisect_left
from fractions import Fraction
from typing import Dict, Optional, Sequence, Tuple

Point = Tuple[int, Fraction]
Polyline = list[Point]
LayerGeometry = Tuple[Polyline, Polyline]


class GeometryError(Exception):
    """折线不满足剖面约束。``reason`` 为稳定的机器可读代码。"""

    def __init__(self, reason: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.details = details or {}


# ---------------------------------------------------------------------------
# 有理数 / 序列化
# ---------------------------------------------------------------------------

def as_fraction(value: int | Fraction) -> Fraction:
    return value if isinstance(value, Fraction) else Fraction(value)


def fraction_to_number(value: Fraction) -> int | float:
    """JSON 数值形式。整数保持整数；分数转 float（精确重画请用 ``*_exact``）。"""
    if value.denominator == 1:
        return value.numerator
    return float(value)


def fraction_to_exact(value: Fraction) -> str:
    """规范化的精确有理数字符串，如 ``"100"``、``"333/1000"``。"""
    if value.denominator == 1:
        return str(value.numerator)
    return f"{value.numerator}/{value.denominator}"


def fraction_to_pair(value: Fraction) -> list[int]:
    return [value.numerator, value.denominator]


# ---------------------------------------------------------------------------
# 折线基础
# ---------------------------------------------------------------------------

def polyline_xs(poly: Polyline) -> list[int]:
    return [x for x, _ in poly]


def evaluate(poly: Polyline, x: int | Fraction) -> Fraction:
    """旧折线在横距 ``x`` 处的精确高程（分段线性插值，不取整）。"""
    if not poly:
        raise GeometryError("EMPTY_POLYLINE", "折线为空，无法求高程")
    xs = polyline_xs(poly)
    if x < xs[0] or x > xs[-1]:
        raise GeometryError(
            "POINT_OUT_OF_RANGE",
            f"横距 {x} 超出折线范围 [{xs[0]}, {xs[-1]}]",
        )
    i = bisect_left(xs, x)
    if i < len(poly) and xs[i] == x:
        return poly[i][1]
    # xs[i-1] < x < xs[i]
    x0, y0 = poly[i - 1]
    x1, y1 = poly[i]
    return y0 + (y1 - y0) * Fraction(x - x0, x1 - x0)


def validate_polyline(
    poly: Polyline,
    x_min: int,
    x_max: int,
    label: str,
) -> None:
    """检查单条折线的结构；结构错误抛出 ``GeometryError``。"""
    if len(poly) < 2:
        raise GeometryError(
            "INVALID_POLYLINE",
            f"{label} 至少需要两个测点（横距范围的两个端点）",
            {"layer": label},
        )
    prev_x: Optional[int] = None
    for x, _y in poly:
        if not isinstance(x, int) or isinstance(x, bool):
            raise GeometryError(
                "INVALID_POLYLINE", f"{label} 的横距必须是整数毫米", {"layer": label}
            )
        if x < x_min or x > x_max:
            raise GeometryError(
                "INVALID_POLYLINE",
                f"{label} 的测点横距 {x} 超出范围 [{x_min}, {x_max}]",
                {"layer": label, "x": x},
            )
        if prev_x is not None and x <= prev_x:
            raise GeometryError(
                "INVALID_POLYLINE",
                f"{label} 的测点必须按横距严格递增（重复或逆序：{prev_x} -> {x}）",
                {"layer": label, "x": x},
            )
        prev_x = x
    if poly[0][0] != x_min or poly[-1][0] != x_max:
        raise GeometryError(
            "RANGE_MISMATCH",
            f"{label} 的横距范围必须与剖面一致，恰为 [{x_min}, {x_max}]",
            {"layer": label},
        )


def _positive_interval(
    left: int,
    right: int,
    d0: Fraction,
    d1: Fraction,
) -> Optional[tuple[Fraction, Fraction]]:
    """线性差值 d 在段内 ``d > 0`` 的开区间；不存在返回 None。

    ``d0``/``d1`` 是 d 在段两端的值，段内线性插值。
    """
    lo = Fraction(left)
    hi = Fraction(right)
    if d0 > 0 and d1 > 0:
        return (lo, hi)
    if d0 <= 0 and d1 <= 0:
        return None
    if d0 > 0:  # d1 <= 0
        if d1 == 0:
            return (lo, hi)
        root = lo + (hi - lo) * d0 / (d0 - d1)
        return (lo, root)
    # d0 <= 0 < d1
    if d0 == 0:
        return (lo, hi)
    root = lo + (hi - lo) * (-d0) / (d1 - d0)
    return (root, hi)


def _open_intersection(
    first: Optional[tuple[Fraction, Fraction]],
    second: Optional[tuple[Fraction, Fraction]],
) -> Optional[tuple[Fraction, Fraction]]:
    if first is None or second is None:
        return None
    lo = max(first[0], second[0])
    hi = min(first[1], second[1])
    return (lo, hi) if lo < hi else None


def _overlap_interval_on_segment(
    left: int,
    right: int,
    a_top: tuple[Fraction, Fraction],
    a_bottom: tuple[Fraction, Fraction],
    b_top: tuple[Fraction, Fraction],
    b_bottom: tuple[Fraction, Fraction],
) -> Optional[tuple[Fraction, Fraction]]:
    """段内两层“内部”真正重叠的横距开区间。

    层内部 = ``bottom(x) < y < top(x)``；两层内部相交当且仅当
    ``max(bottom_a, bottom_b) < min(top_a, top_b)``，等价于四个线性
    条件 ``bottom_a < top_a``、``bottom_a < top_b``、``bottom_b < top_a``、
    ``bottom_b < top_b`` 同时成立。四个开区间之交非空即有内部重叠。
    """
    ta0, ta1 = a_top
    ba0, ba1 = a_bottom
    tb0, tb1 = b_top
    bb0, bb1 = b_bottom

    interval: Optional[tuple[Fraction, Fraction]] = (
        Fraction(left),
        Fraction(right),
    )
    conditions = [
        (ta0 - ba0, ta1 - ba1),  # top_a > bottom_a
        (tb0 - bb0, tb1 - bb1),  # top_b > bottom_b
        (ta0 - bb0, ta1 - bb1),  # top_a > bottom_b
        (tb0 - ba0, tb1 - ba1),  # top_b > bottom_a
    ]
    for d0, d1 in conditions:
        interval = _open_intersection(
            interval, _positive_interval(left, right, d0, d1)
        )
        if interval is None:
            return None
    return interval


def _union_xs(*polys: Polyline) -> list[int]:
    xs: set[int] = set()
    for poly in polys:
        xs.update(x for x, _ in poly)
    return sorted(xs)


def validate_layers(
    layers: Dict[int, LayerGeometry],
    x_min: int,
    x_max: int,
) -> None:
    """校验整个剖面的新线形。

    * 每层顶线不得低于底线（可重合）；
    * 不同层允许共边，但两条带厚度的层位内部不得重叠；
    * 所有层位横距范围相同。
    """
    for sid, (top, bottom) in layers.items():
        validate_polyline(top, x_min, x_max, f"层位 {sid} 顶线")
        validate_polyline(bottom, x_min, x_max, f"层位 {sid} 底线")

    # 同层顶/底：差值为分段线性，只需在并集断点处检查。
    for sid, (top, bottom) in layers.items():
        for x in _union_xs(top, bottom):
            if evaluate(top, x) < evaluate(bottom, x):
                raise GeometryError(
                    "TOP_BELOW_BOTTOM",
                    f"层位 {sid} 的顶线在横距 {x} 处低于底线",
                    {
                        "layer": sid,
                        "x": x,
                        "top": fraction_to_exact(evaluate(top, x)),
                        "bottom": fraction_to_exact(evaluate(bottom, x)),
                    },
                )

    # 不同层：检查是否存在纵向内部重叠。
    ids = list(layers)
    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            sid_a, sid_b = ids[i], ids[j]
            top_a, bottom_a = layers[sid_a]
            top_b, bottom_b = layers[sid_b]
            xs = _union_xs(top_a, bottom_a, top_b, bottom_b)
            for left, right in zip(xs, xs[1:]):
                # 在 [left,right] 上四条线均线性；内部重叠当且仅当
                # max(bottom_a, bottom_b) < min(top_a, top_b)。
                hit = _overlap_interval_on_segment(
                    left,
                    right,
                    (evaluate(top_a, left), evaluate(top_a, right)),
                    (evaluate(bottom_a, left), evaluate(bottom_a, right)),
                    (evaluate(top_b, left), evaluate(top_b, right)),
                    (evaluate(bottom_b, left), evaluate(bottom_b, right)),
                )
                if hit is not None:
                    raise GeometryError(
                        "LAYER_OVERLAP",
                        f"层位 {sid_a} 与层位 {sid_b} 的内部在横距 "
                        f"[{left}, {right}] 区间重叠",
                        {
                            "layer_a": sid_a,
                            "layer_b": sid_b,
                            "x_from": fraction_to_exact(hit[0]),
                            "x_to": fraction_to_exact(hit[1]),
                        },
                    )


# ---------------------------------------------------------------------------
# 返工拼接
# ---------------------------------------------------------------------------

def normalize_new_points(
    points: Sequence[tuple[int, Fraction]],
    cut_x1: int,
    cut_x2: int,
    label: str,
) -> Polyline:
    """校验并排序切口之间的新测点。

    新测点必须**严格位于**两切口之间（横距为整数毫米）；切口处的高程完全由
    旧折线精确插值决定，不接受另行给出的切口值。区间之外的线形一律沿用原图。
    """
    normalized: Polyline = []
    for x, y in points:
        if not isinstance(x, int) or isinstance(x, bool):
            raise GeometryError(
                "INVALID_POLYLINE", f"{label} 的横距必须是整数毫米"
            )
        if x <= cut_x1 or x >= cut_x2:
            raise GeometryError(
                "POINT_OUT_OF_RANGE",
                f"{label} 的新测点横距 {x} 必须严格位于两切口 "
                f"({cut_x1}, {cut_x2}) 之间；区间外沿用原图",
                {"x": x, "cut_x1": cut_x1, "cut_x2": cut_x2},
            )
        normalized.append((x, as_fraction(y)))
    normalized.sort(key=lambda p: p[0])
    for (x0, _y), (x1, _y2) in zip(normalized, normalized[1:]):
        if x0 == x1:
            raise GeometryError(
                "DUPLICATE_POINT",
                f"{label} 在横距 {x0} 处有重复测点",
                {"x": x0},
            )
    return normalized


def splice_polyline(
    old: Polyline,
    cut_x1: int,
    cut_x2: int,
    new_points: Sequence[tuple[int, Fraction]],
) -> Polyline:
    """把旧折线在 ``[cut_x1, cut_x2]`` 内替换为新测点，切口接回旧线高程。"""
    y1 = evaluate(old, cut_x1)
    y2 = evaluate(old, cut_x2)
    left = [p for p in old if p[0] < cut_x1]
    right = [p for p in old if p[0] > cut_x2]
    middle: Polyline = [(cut_x1, y1)]
    middle.extend((x, as_fraction(y)) for x, y in new_points)
    middle.append((cut_x2, y2))
    return left + middle + right
