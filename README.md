# 考古地层对接 API（FastAPI + SQLite）

管理同一考古遗址最多 40 个层位的探方编号、层位编号与可能沉积年代段（整年闭区间，
可含空档），登记跨探方层位对接与“下层 → 上层”叠压关系，并自动完成：

- **分组**：直接或间接对接的层位视为同一次沉积，合并为连通组，全组共用一个年份；
- **成环检测**：合组后的叠压关系（含同组自环）不得成环；
- **年代约束**：下层沉积年严格早于上层；同组年份必须落在各成员年代段的交集内；
- **可行年代段求解**：对每个组给出可与其他全部层位约束同时成立的所有年份
  （保留空档的闭区间列表）；
- **撤销重算**：删除对接/叠压后按剩余关系重新分组、重新求解。

另提供**探方剖面**（层位顶、底折线的初版登记与局部复测返工，见下文
[探方剖面](#探方剖面初版与复测)）。

## 快速开始（docker compose）

```bash
docker compose up --build
# API:      http://localhost:8000
# Swagger:  http://localhost:8000/docs   （可直接在页面上录入数据）
# ReDoc:    http://localhost:8000/redoc
```

SQLite 数据文件保存在命名卷 `archaeo-data`（容器内 `/data/archaeo.db`），
`docker compose down` 不会删除；如需清空：`docker compose down -v`。

## 本地裸跑

```bash
python -m pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
# 数据库默认在 ./data/archaeo.db，可用环境变量 DB_PATH 覆盖
```

## 年代表示约定

- 年份均为整数；公元后用正整数，公元前用负整数（如 `-221` 表示公元前 221 年）。
- 每个层位给一个或多个闭区间 `[year_from, year_to]`，多段之间允许空档
  （如 `[-5000,-4000]` 与 `[-3000,-2000]`，中间一千年无沉积可能）。
- 同组年代域 = 组内各层位区间的交集；求解结果 `feasible_years` 同样是带空档的区间列表。

## 主要接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/sites` | 建遗址 |
| GET | `/sites` / `/sites/{id}` | 遗址列表/详情 |
| POST | `/sites/{id}/strata` | 录入层位（探方、层位号、年代段；每遗址上限 40） |
| GET | `/sites/{id}/strata` / `/strata/{id}` | 层位查询 |
| POST | `/sites/{id}/correlations` | 登记两探方层位对接 |
| GET | `/sites/{id}/correlations` | 对接列表 |
| DELETE | `/correlations/{id}` | 撤销对接，按剩余对接重算 |
| POST | `/sites/{id}/superpositions` | 登记叠压（下层 → 上层） |
| GET | `/sites/{id}/superpositions` | 叠压列表 |
| DELETE | `/superpositions/{id}` | 撤销叠压并重算 |
| GET | `/sites/{id}/solution` | 整站分组、组间边、各组全部可行年代段 |
| GET | `/strata/{id}/correlation-view` | 按原始层位查同组成员与可行年代段 |

### 探方剖面（初版与复测）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/sites/{id}/sections/{trench}` | 按遗址和探方登记初版剖面 |
| POST | `/sites/{id}/sections` | 同上，探方编号写在请求体 `trench` |
| GET | `/sites/{id}/sections` | 该遗址已登记剖面一览（含当前版号） |
| GET | `/sites/{id}/sections/{trench}` | 当前版号与各层可精确重画的顶、底折线 |
| GET | `/sites/{id}/sections/{trench}/version` | 只取当前版号 |
| GET | `/strata/{id}/profile` | 按原始层位 ID 查其剖面的版号与该层折线 |
| POST | `/sites/{id}/sections/{trench}/revisions` | 复测返工（局部替换） |
| POST | `/sites/{id}/revisions` | 同上，探方编号写在请求体 `trench` |

`/profiles`、`/trenches/{trench}/profile` 是 `/sections` 的同义路径
（如 `/profiles/{trench}/revision`）；它们不出现在 `/docs` 列表中但行为一致。
请求体也兼容若干常见字段别名（`top_points`/`top_line`/`points.top`、
`seen_version`/`base_version`、`cuts`/`cut`/`x1,x2`、测点 `[x, y]` 等）。

#### 初版登记

同一探方各层共用**同一个整数毫米横距范围** `[x_min, x_max]`；每层用既有**层位 ID**
（`stratum_id`）关联一个已登记层位，并给出顶线与底线的折线测点
（`{"x": 整数毫米, "y": 整数毫米}` 或 `[x, y]`）。两条折线的首末横距都必须恰好是
`x_min` 与 `x_max`，横距严格递增。

```bash
SID=1
curl -X POST localhost:8000/sites/$SID/sections/T1 -H 'Content-Type: application/json' -d '{
  "x_min": 0, "x_max": 4000,
  "layers": [
    {"stratum_id": 1,
     "top":    [{"x": 0, "y": 820}, {"x": 2000, "y": 860}, {"x": 4000, "y": 800}],
     "bottom": [{"x": 0, "y": 700}, {"x": 4000, "y": 720}]},
    {"stratum_id": 2,
     "top":    [{"x": 0, "y": 700}, {"x": 4000, "y": 720}],
     "bottom": [{"x": 0, "y": 540}, {"x": 4000, "y": 560}]}
  ]
}'
```

规则：**每层顶线不得低于其底线**（可重合）；**不同层允许共边**（例如上层底线与
下层顶线完全重合），但两层带厚度的内部不得重叠。共边之外的任何内部重叠都拒绝。

#### 复测返工

一次复测只返工**一段**：提交所见**版号**、范围内的两个**整数毫米切口**
`cut_x1 < cut_x2`，以及各层在两切口**之间**的新测点（可以为空）。

- 切口处的高程由**旧折线精确线性插值**得到；切口落在两个旧测点之间时**不取整**，
  以分数保存（接口同时返回 `top_exact` 等 `"分子/分母"` 精确值与浮点近似）。
- 两个切口之间没有新测点时，两切口**直接相连**。
- **替换区间之外**（含此前返工产生的切口点）原样保留当次复测前的线形。
- 只有请求中列出的层位会被返工，其余层位保持不变。

```bash
# 所见版号 1，返工 [1200, 2600] 段，仅第 1 层
curl -X POST localhost:8000/sites/$SID/sections/T1/revisions \
  -H 'Content-Type: application/json' -d '{
  "version": 1, "cut_x1": 1200, "cut_x2": 2600,
  "layers": [
    {"stratum_id": 1,
     "top_points":    [{"x": 1700, "y": 905}],
     "bottom_points": []}
  ]
}'
```

整次拒绝（`409`，库内原图与版号保持不变）的情形：

- 切口越界或顺序不对（`CUT_OUT_OF_RANGE`）、新测点落在切口之外
  （`POINT_OUT_OF_RANGE`）；
- 版号过期（`STALE_VERSION`）：请求版号不等于当前版号；
- 拼接后违反剖面规则（`TOP_BELOW_BOTTOM`、`LAYER_OVERLAP` 等）；
- 写入失败（`WRITE_FAILED`）。

成功时 `version` 加 1，响应即返回新图，`GET` 查询立即可见；重启后同一数据库
文件读到的版号与折线完全相同（折线以整数分子/分母精确落库）。


### 录入示例

```bash
SID=1
# 层位：探方 T1 第 3 层，两个可能年代段（中间有空档）
curl -X POST localhost:8000/sites/$SID/strata -H 'Content-Type: application/json' -d '{
  "trench": "T1", "layer_no": "L3",
  "intervals": [{"year_from": -5000, "year_to": -4000},
                {"year_from": -3000, "year_to": -2000}]
}'
# 叠压：下层 1 -> 上层 2
curl -X POST localhost:8000/sites/$SID/superpositions \
  -H 'Content-Type: application/json' -d '{"lower_id": 1, "upper_id": 2}'
# 跨探方对接：层位 1 与层位 3 属同一次沉积
curl -X POST localhost:8000/sites/$SID/correlations \
  -H 'Content-Type: application/json' -d '{"stratum_a_id": 1, "stratum_b_id": 3}'
```

## 无解时的行为

写入会在**同一事务**内先重算再提交。若新记录导致无解（`EMPTY_DOMAIN` /
`GROUP_CYCLE` / `ORDER_CONFLICT`），事务回滚，接口返回 `409`，响应体包含：

- `contradiction`：一组本身即矛盾的**原始记录见证**（层位年代段、对接、叠压，
  含可读原因与记录 id，便于定位）；
- `previous_solution`：回滚后库内已有对接与年代结论（保持可行、保持可用）。

## 一致性与并发

- 所有写操作经进程内同一把锁串行化，按**提交顺序**核算；每次成功写入使全局
  `version` 加 1，查询结果带 `version`，可据此确认读到的是哪一次已完成提交。
- 分组与年代结论**不物化落库**，每次查询及重启都由原始记录现场求解；
  原始记录由 SQLite 事务保证原子性，因此任何查询看到的分组/年代都来自同一次
  完整提交，不存在半写入的中间态。
- SQLite 启用 WAL 与外键约束，连接带 30s busy timeout。

## 求解算法（`app/solver.py`）

1. 并查集按对接关系求连通组；组域为成员年代域交集（支持空档的区间运算）。
2. 叠压边提升为组间边；Kahn 拓扑排序判环，DFS 三色标记提取具体环与见证。
3. DAG 上两次贪心：
   - 最早解 E：拓扑序每组取域内 `> max(E[前驱])` 的最小年份；
   - 最晚解 L：逆拓扑序每组取域内 `< min(L[后继])` 的最大年份。
   任意可行解 F 都满足 E ≤ F ≤ L，故组 g 的全部可同时成立年份恰为
   其年代域 ∩ [E[g], L[g]]（仍可为含空档的多段区间）。

## 测试

```bash
python -m pytest tests/ -q
```

覆盖区间工具、缺口域求解、成环/空域/次序冲突及矛盾见证、40 层上限、
无解拒绝与旧结论保留、撤销重算、并发写入串行化、重启后结论恢复；
以及剖面初版登记、切口落在测点之间的精确分数高程、连续复测区间外线形保持、
版号过期/切口越界/顶底颠倒/层间内部重叠/写入失败的整次拒绝与回滚、重启后
剖面版号与折线不变。
