"""探方剖面：初版登记、复测返工、几何校验与持久化。"""

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture()
def client(tmp_path):
    app = create_app(db_path=str(tmp_path / "profile.db"))
    with TestClient(app) as c:
        yield c


def _site(client):
    return client.post("/sites", json={"name": "剖面遗址"}).json()["record"]


def _strata(client, site_id, trench, layer_nos):
    out = {}
    for layer_no in layer_nos:
        r = client.post(
            f"/sites/{site_id}/strata",
            json={
                "trench": trench,
                "layer_no": layer_no,
                "intervals": [{"year_from": -5000, "year_to": -4000}],
            },
        )
        assert r.status_code == 201, r.text
        out[layer_no] = r.json()["record"]["id"]
    return out


def _linear(x0, y0, x1, y1):
    return [{"x": x0, "y": y0}, {"x": x1, "y": y1}]


def _section(client, site_id, trench, layers, x_min=0, x_max=1000):
    return client.post(
        f"/sites/{site_id}/sections",
        json={
            "trench": trench,
            "x_min": x_min,
            "x_max": x_max,
            "layers": layers,
        },
    )


def _rev(client, site_id, trench, payload):
    return client.post(f"/sites/{site_id}/sections/{trench}/revisions", json=payload)


def _exact(points):
    return [[x, y] for x, y in points]


def test_initial_section_roundtrip_and_exact_view(client):
    site = _site(client)
    sid = site["id"]
    ids = _strata(client, sid, "T1", ["L1", "L2"])
    r = _section(
        client,
        sid,
        "T1",
        [
            {
                "stratum_id": ids["L1"],
                "top": _linear(0, 200, 1000, 300),
                "bottom": _linear(0, 100, 1000, 200),
            },
            {
                "stratum_id": ids["L2"],
                "top": _linear(0, 100, 1000, 200),
                "bottom": _linear(0, 0, 1000, 100),
            },
        ],
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["version"] == 1
    assert body["x_min"] == 0 and body["x_max"] == 1000
    by_id = {layer["stratum_id"]: layer for layer in body["layers"]}
    assert _exact(by_id[ids["L1"]]["top_exact"]) == [["0", "200"], ["1000", "300"]]
    assert by_id[ids["L2"]]["bottom"] == [[0, 0], [1000, 100]]

    got = client.get(f"/sites/{sid}/sections/T1").json()
    assert got == body


def test_revision_keeps_outside_original_and_interpolates_cut(client):
    site = _site(client)
    sid = site["id"]
    ids = _strata(client, sid, "T1", ["L1"])
    # 顶线 0..1000 从 y=0 升到 y=1000：切口 333 处旧线高程 = 333
    r = _section(
        client,
        sid,
        "T1",
        [
            {
                "stratum_id": ids["L1"],
                "top": _linear(0, 0, 1000, 1000),
                "bottom": _linear(0, -1000, 1000, 0),
            }
        ],
    )
    assert r.status_code == 201, r.text

    rev = _rev(
        client,
        sid,
        "T1",
        {
            "version": 1,
            "cut_x1": 333,
            "cut_x2": 700,
            "layers": [
                {
                    "stratum_id": ids["L1"],
                    "top_points": [{"x": 500, "y": 900}],
                    "bottom_points": [],
                }
            ],
        },
    )
    assert rev.status_code == 201, rev.text
    body = rev.json()
    assert body["version"] == 2
    layer = body["layers"][0]
    top = layer["top_exact"]
    # 区间外保留原图端点；切口处接回旧线精确高程 333 与 700
    assert top[0] == ["0", "0"]
    assert ["333", "333"] in top
    assert ["500", "900"] in top
    assert top[-1] == ["1000", "1000"]
    idx = {int(x): y for x, y in top}
    assert idx[333] == "333"
    assert idx[700] == "700"
    # 底线没有新点 -> 两切口直连；切口高程仍来自旧线
    bottom = {int(x): y for x, y in layer["bottom_exact"]}
    assert bottom[333] == "-667"
    assert bottom[700] == "-300"
    # 区间内没有额外测点
    assert sorted(bottom) == [0, 333, 700, 1000]


def test_cut_between_points_is_not_rounded(client):
    site = _site(client)
    sid = site["id"]
    ids = _strata(client, sid, "T1", ["L1"])
    # 旧线两点 (0,0)-(3,1)：切口 x=1 的高程为 1/3，绝不取整
    r = _section(
        client,
        sid,
        "T1",
        [
            {
                "stratum_id": ids["L1"],
                "top": _linear(0, 10, 3, 11),
                "bottom": _linear(0, 0, 3, 1),
            }
        ],
        x_min=0,
        x_max=3,
    )
    assert r.status_code == 201, r.text
    rev = _rev(
        client,
        sid,
        "T1",
        {
            "version": 1,
            "cut_x1": 1,
            "cut_x2": 2,
            "layers": [{"stratum_id": ids["L1"], "top_points": [], "bottom_points": []}],
        },
    )
    assert rev.status_code == 201, rev.text
    exact = {int(x): y for x, y in rev.json()["layers"][0]["bottom_exact"]}
    assert exact[1] == "1/3"
    assert exact[2] == "2/3"
    approx = dict(rev.json()["layers"][0]["bottom"])
    assert approx[1] == pytest.approx(1 / 3)


def test_consecutive_revisions_preserve_previous_shape_outside_cut(client):
    site = _site(client)
    sid = site["id"]
    ids = _strata(client, sid, "T1", ["L1"])
    _section(
        client,
        sid,
        "T1",
        [
            {
                "stratum_id": ids["L1"],
                "top": _linear(0, 1000, 1000, 1000),
                "bottom": _linear(0, 0, 1000, 0),
            }
        ],
    )
    first = _rev(
        client,
        sid,
        "T1",
        {
            "version": 1,
            "cut_x1": 100,
            "cut_x2": 300,
            "layers": [
                {
                    "stratum_id": ids["L1"],
                    "top_points": [{"x": 200, "y": 900}],
                    "bottom_points": [],
                }
            ],
        },
    )
    assert first.status_code == 201, first.text
    before = first.json()["layers"][0]["top_exact"]

    second = _rev(
        client,
        sid,
        "T1",
        {
            "version": 2,
            "cut_x1": 600,
            "cut_x2": 800,
            "layers": [
                {
                    "stratum_id": ids["L1"],
                    "top_points": [{"x": 700, "y": 1200}],
                    "bottom_points": [],
                }
            ],
        },
    )
    assert second.status_code == 201, second.text
    after = second.json()["layers"][0]["top_exact"]
    # 300 之后到 600 之前（不含第二切口产生的点）必须与复测前的线形一致
    before_map = {int(x): y for x, y in before}
    after_map = {int(x): y for x, y in after}
    for x in before_map:
        if x <= 600:
            assert after_map[x] == before_map[x], x
    # 第一次返工产生的 200 测点仍在
    assert after_map[200] == "900"


def test_stale_version_rejected_and_previous_preserved(client):
    site = _site(client)
    sid = site["id"]
    ids = _strata(client, sid, "T1", ["L1"])
    _section(
        client,
        sid,
        "T1",
        [{"stratum_id": ids["L1"], "top": _linear(0, 10, 10, 10), "bottom": _linear(0, 0, 10, 0)}],
        x_min=0,
        x_max=10,
    )
    ok = _rev(
        client,
        sid,
        "T1",
        {
            "version": 1,
            "cut_x1": 2,
            "cut_x2": 4,
            "layers": [{"stratum_id": ids["L1"], "top_points": [], "bottom_points": []}],
        },
    )
    assert ok.status_code == 201 and ok.json()["version"] == 2
    before = client.get(f"/sites/{sid}/sections/T1").json()

    stale = _rev(
        client,
        sid,
        "T1",
        {
            "version": 1,
            "cut_x1": 5,
            "cut_x2": 6,
            "layers": [{"stratum_id": ids["L1"], "top_points": [], "bottom_points": []}],
        },
    )
    assert stale.status_code == 409
    assert stale.json()["reason"] == "STALE_VERSION"
    assert client.get(f"/sites/{sid}/sections/T1").json() == before


def test_cut_out_of_range_rejected(client):
    site = _site(client)
    sid = site["id"]
    ids = _strata(client, sid, "T1", ["L1"])
    _section(
        client,
        sid,
        "T1",
        [{"stratum_id": ids["L1"], "top": _linear(0, 10, 10, 10), "bottom": _linear(0, 0, 10, 0)}],
        x_min=0,
        x_max=10,
    )
    before = client.get(f"/sites/{sid}/sections/T1").json()
    for x1, x2 in ((-1, 5), (5, 11), (5, 5), (8, 3)):
        r = _rev(
            client,
            sid,
            "T1",
            {
                "version": 1,
                "cut_x1": x1,
                "cut_x2": x2,
                "layers": [{"stratum_id": ids["L1"], "top_points": [], "bottom_points": []}],
            },
        )
        assert r.status_code == 409, (x1, x2, r.text)
        assert r.json()["reason"] == "CUT_OUT_OF_RANGE"
    assert client.get(f"/sites/{sid}/sections/T1").json() == before


def test_new_points_outside_cut_rejected(client):
    site = _site(client)
    sid = site["id"]
    ids = _strata(client, sid, "T1", ["L1"])
    _section(
        client,
        sid,
        "T1",
        [{"stratum_id": ids["L1"], "top": _linear(0, 10, 10, 10), "bottom": _linear(0, 0, 10, 0)}],
        x_min=0,
        x_max=10,
    )
    r = _rev(
        client,
        sid,
        "T1",
        {
            "version": 1,
            "cut_x1": 3,
            "cut_x2": 6,
            "layers": [
                {"stratum_id": ids["L1"], "top_points": [{"x": 7, "y": 10}], "bottom_points": []}
            ],
        },
    )
    assert r.status_code == 409
    assert r.json()["reason"] == "POINT_OUT_OF_RANGE"


def test_top_below_bottom_rejected_and_preserved(client):
    site = _site(client)
    sid = site["id"]
    ids = _strata(client, sid, "T1", ["L1"])
    _section(
        client,
        sid,
        "T1",
        [{"stratum_id": ids["L1"], "top": _linear(0, 10, 10, 10), "bottom": _linear(0, 0, 10, 0)}],
        x_min=0,
        x_max=10,
    )
    before = client.get(f"/sites/{sid}/sections/T1").json()
    r = _rev(
        client,
        sid,
        "T1",
        {
            "version": 1,
            "cut_x1": 2,
            "cut_x2": 8,
            "layers": [
                {
                    "stratum_id": ids["L1"],
                    "top_points": [{"x": 5, "y": -5}],
                    "bottom_points": [],
                }
            ],
        },
    )
    assert r.status_code == 409
    assert r.json()["reason"] == "TOP_BELOW_BOTTOM"
    assert client.get(f"/sites/{sid}/sections/T1").json() == before


def test_layer_internal_overlap_allowed_shared_edge_but_not_overlap(client):
    site = _site(client)
    sid = site["id"]
    ids = _strata(client, sid, "T1", ["L1", "L2"])
    # 共边（L1 底 = L2 顶）合法
    r = _section(
        client,
        sid,
        "T1",
        [
            {"stratum_id": ids["L1"], "top": _linear(0, 100, 10, 100), "bottom": _linear(0, 50, 10, 50)},
            {"stratum_id": ids["L2"], "top": _linear(0, 50, 10, 50), "bottom": _linear(0, 0, 10, 0)},
        ],
        x_min=0,
        x_max=10,
    )
    assert r.status_code == 201, r.text

    site2 = client.post("/sites", json={"name": "重叠遗址"}).json()["record"]
    ids2 = _strata(client, site2["id"], "T1", ["L1", "L2"])
    r2 = _section(
        client,
        site2["id"],
        "T1",
        [
            {"stratum_id": ids2["L1"], "top": _linear(0, 100, 10, 100), "bottom": _linear(0, 40, 10, 40)},
            {"stratum_id": ids2["L2"], "top": _linear(0, 60, 10, 60), "bottom": _linear(0, 0, 10, 0)},
        ],
        x_min=0,
        x_max=10,
    )
    assert r2.status_code == 409
    assert r2.json()["reason"] == "LAYER_OVERLAP"


def test_revision_creating_overlap_rejected(client):
    site = _site(client)
    sid = site["id"]
    ids = _strata(client, sid, "T1", ["L1", "L2"])
    _section(
        client,
        sid,
        "T1",
        [
            {"stratum_id": ids["L1"], "top": _linear(0, 100, 10, 100), "bottom": _linear(0, 50, 10, 50)},
            {"stratum_id": ids["L2"], "top": _linear(0, 50, 10, 50), "bottom": _linear(0, 0, 10, 0)},
        ],
        x_min=0,
        x_max=10,
    )
    before = client.get(f"/sites/{sid}/sections/T1").json()
    r = _rev(
        client,
        sid,
        "T1",
        {
            "version": 1,
            "cut_x1": 2,
            "cut_x2": 8,
            "layers": [
                {
                    "stratum_id": ids["L2"],
                    "top_points": [{"x": 5, "y": 70}],
                    "bottom_points": [],
                }
            ],
        },
    )
    assert r.status_code == 409
    assert r.json()["reason"] == "LAYER_OVERLAP"
    assert client.get(f"/sites/{sid}/sections/T1").json() == before


def test_range_mismatch_and_trench_and_duplicate_layer(client):
    site = _site(client)
    sid = site["id"]
    ids = _strata(client, sid, "T1", ["L1"])
    other = _strata(client, sid, "T2", ["L1"])

    r = _section(
        client,
        sid,
        "T1",
        [{"stratum_id": ids["L1"], "top": _linear(0, 10, 5, 10), "bottom": _linear(0, 0, 5, 0)}],
        x_min=0,
        x_max=10,
    )
    assert r.status_code == 409
    assert r.json()["reason"] == "RANGE_MISMATCH"

    r = _section(
        client,
        sid,
        "T1",
        [{"stratum_id": other["L1"], "top": _linear(0, 10, 10, 10), "bottom": _linear(0, 0, 10, 0)}],
        x_min=0,
        x_max=10,
    )
    assert r.status_code == 409
    assert r.json()["reason"] == "TRENCH_MISMATCH"

    ok = _section(
        client,
        sid,
        "T1",
        [{"stratum_id": ids["L1"], "top": _linear(0, 10, 10, 10), "bottom": _linear(0, 0, 10, 0)}],
        x_min=0,
        x_max=10,
    )
    assert ok.status_code == 201

    dup = _section(
        client,
        sid,
        "T1",
        [{"stratum_id": ids["L1"], "top": _linear(0, 10, 10, 10), "bottom": _linear(0, 0, 10, 0)}],
        x_min=0,
        x_max=10,
    )
    assert dup.status_code == 409


def test_persistence_and_version_stable_after_restart(tmp_path):
    db_path = str(tmp_path / "persist.db")
    with TestClient(create_app(db_path=db_path)) as c1:
        site = _site(c1)
        sid = site["id"]
        ids = _strata(c1, sid, "T1", ["L1"])
        _section(
            c1,
            sid,
            "T1",
            [{"stratum_id": ids["L1"], "top": _linear(0, 10, 3, 13), "bottom": _linear(0, 0, 3, 3)}],
            x_min=0,
            x_max=3,
        )
        rev = _rev(
            c1,
            sid,
            "T1",
            {
                "version": 1,
                "cut_x1": 1,
                "cut_x2": 2,
                "layers": [{"stratum_id": ids["L1"], "top_points": [], "bottom_points": []}],
            },
        )
        assert rev.status_code == 201
        snapshot = c1.get(f"/sites/{sid}/sections/T1").json()

    with TestClient(create_app(db_path=db_path)) as c2:
        assert c2.get(f"/sites/{sid}/sections/T1").json() == snapshot


def test_write_failure_rejected_and_rolled_back(client, monkeypatch):
    site = _site(client)
    sid = site["id"]
    ids = _strata(client, sid, "T1", ["L1"])
    _section(
        client,
        sid,
        "T1",
        [{"stratum_id": ids["L1"], "top": _linear(0, 10, 10, 10), "bottom": _linear(0, 0, 10, 0)}],
        x_min=0,
        x_max=10,
    )
    before = client.get(f"/sites/{sid}/sections/T1").json()

    from app import db as db_module

    original = db_module.replace_section_geometry

    def boom(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(db_module, "replace_section_geometry", boom)
    r = _rev(
        client,
        sid,
        "T1",
        {
            "version": 1,
            "cut_x1": 2,
            "cut_x2": 8,
            "layers": [
                {"stratum_id": ids["L1"], "top_points": [{"x": 5, "y": 12}], "bottom_points": []}
            ],
        },
    )
    assert r.status_code == 409
    assert r.json()["reason"] == "WRITE_FAILED"
    monkeypatch.setattr(db_module, "replace_section_geometry", original)
    assert client.get(f"/sites/{sid}/sections/T1").json() == before


def test_revision_only_touches_listed_layers(client):
    site = _site(client)
    sid = site["id"]
    ids = _strata(client, sid, "T1", ["L1", "L2"])
    _section(
        client,
        sid,
        "T1",
        [
            {"stratum_id": ids["L1"], "top": _linear(0, 100, 10, 100), "bottom": _linear(0, 50, 10, 50)},
            {"stratum_id": ids["L2"], "top": _linear(0, 50, 10, 50), "bottom": _linear(0, 0, 10, 0)},
        ],
        x_min=0,
        x_max=10,
    )
    r = _rev(
        client,
        sid,
        "T1",
        {
            "version": 1,
            "cut_x1": 2,
            "cut_x2": 8,
            "layers": [
                {"stratum_id": ids["L1"], "top_points": [{"x": 5, "y": 90}], "bottom_points": []}
            ],
        },
    )
    assert r.status_code == 201, r.text
    by_id = {layer["stratum_id"]: layer for layer in r.json()["layers"]}
    assert by_id[ids["L1"]]["top_exact"] == [
        ["0", "100"],
        ["2", "100"],
        ["5", "90"],
        ["8", "100"],
        ["10", "100"],
    ]
    assert by_id[ids["L2"]]["top_exact"] == [["0", "50"], ["10", "50"]]
    assert by_id[ids["L2"]]["bottom_exact"] == [["0", "0"], ["10", "0"]]


def test_cut_endpoint_points_rejected(client):
    site = _site(client)
    sid = site["id"]
    ids = _strata(client, sid, "T1", ["L1"])
    _section(
        client,
        sid,
        "T1",
        [{"stratum_id": ids["L1"], "top": _linear(0, 10, 10, 20), "bottom": _linear(0, 0, 10, 10)}],
        x_min=0,
        x_max=10,
    )
    ok = _rev(
        client,
        sid,
        "T1",
        {
            "version": 1,
            "cut_x1": 2,
            "cut_x2": 8,
            "layers": [
                {
                    "stratum_id": ids["L1"],
                    "top_points": [{"x": 2, "y": 12}],
                    "bottom_points": [],
                }
            ],
        },
    )
    assert ok.status_code == 409
    assert ok.json()["reason"] == "POINT_OUT_OF_RANGE"
    bad = _rev(
        client,
        sid,
        "T1",
        {
            "version": 1,
            "cut_x1": 3,
            "cut_x2": 9,
            "layers": [
                {"stratum_id": ids["L1"], "top_points": [{"x": 3, "y": 99}], "bottom_points": []}
            ],
        },
    )
    assert bad.status_code == 409
    assert bad.json()["reason"] == "POINT_OUT_OF_RANGE"


def test_many_consecutive_revisions_stay_exact_and_persist(tmp_path):
    db_path = str(tmp_path / "many.db")
    from fractions import Fraction

    with TestClient(create_app(db_path=db_path)) as c:
        site = _site(c)
        sid = site["id"]
        ids = _strata(c, sid, "T1", ["L1"])
        _section(
            c,
            sid,
            "T1",
            [{"stratum_id": ids["L1"], "top": _linear(0, 30, 30, 30), "bottom": _linear(0, 0, 30, 30)}],
            x_min=0,
            x_max=30,
        )
        version = 1
        for i in range(1, 8):
            x1, x2 = 4 * i - 3, 4 * i
            r = _rev(
                c,
                sid,
                "T1",
                {
                    "version": version,
                    "cut_x1": x1,
                    "cut_x2": x2,
                    "layers": [
                        {
                            "stratum_id": ids["L1"],
                            "top_points": [{"x": x1 + 1, "y": 30 + i}],
                            "bottom_points": [],
                        }
                    ],
                },
            )
            assert r.status_code == 201, r.text
            version = r.json()["version"]

        snapshot = c.get(f"/sites/{sid}/sections/T1").json()
        bottom_exact = {int(x): Fraction(y) for x, y in snapshot["layers"][0]["bottom_exact"]}
        # 所有切口点的高程都与原直线 y=x 精确一致（分数形式仍为整数）
        for x, y in bottom_exact.items():
            assert y == x
        # 每个返工段的顶线新点都还在
        top_exact = {int(x): Fraction(y) for x, y in snapshot["layers"][0]["top_exact"]}
        for i in range(1, 8):
            assert top_exact[4 * i - 2] == 30 + i

    with TestClient(create_app(db_path=db_path)) as c2:
        assert c2.get(f"/sites/{sid}/sections/T1").json() == snapshot
        stale = _rev(
            c2,
            sid,
            "T1",
            {
                "version": 1,
                "cut_x1": 1,
                "cut_x2": 2,
                "layers": [{"stratum_id": ids["L1"], "top_points": [], "bottom_points": []}],
            },
        )
        assert stale.status_code == 409
        assert stale.json()["reason"] == "STALE_VERSION"
        assert c2.get(f"/sites/{sid}/sections/T1").json() == snapshot


def test_concurrent_revisions_same_version_only_one_wins(client):
    import concurrent.futures

    site = _site(client)
    sid = site["id"]
    ids = _strata(client, sid, "T1", ["L1"])
    _section(
        client,
        sid,
        "T1",
        [{"stratum_id": ids["L1"], "top": _linear(0, 10, 10, 10), "bottom": _linear(0, 0, 10, 0)}],
        x_min=0,
        x_max=10,
    )

    def submit(i):
        return _rev(
            client,
            sid,
            "T1",
            {
                "version": 1,
                "cut_x1": 1,
                "cut_x2": 9,
                "layers": [
                    {
                        "stratum_id": ids["L1"],
                        "top_points": [{"x": 5, "y": 100 + i}],
                        "bottom_points": [],
                    }
                ],
            },
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(submit, range(6)))
    statuses = [r.status_code for r in results]
    assert statuses.count(201) == 1, statuses
    assert statuses.count(409) == 5, statuses
    final = client.get(f"/sites/{sid}/sections/T1").json()
    assert final["version"] == 2
    winners = [i for i, r in enumerate(results) if r.status_code == 201]
    # 胜者写入的新点保留，其余全部被拒且未写入
    ys = {y for _x, y in final["layers"][0]["top_exact"]}
    assert str(100 + winners[0]) in ys
    for i in range(6):
        if i != winners[0]:
            assert str(100 + i) not in ys
