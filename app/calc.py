# -*- coding: utf-8 -*-
"""陶瓷窑车层架载荷追踪台 —— 核心计算引擎（与 Flask/前端无关，可独立测试）。

物理模型
========
窑车自下而上由若干层组成。第 i 层包含：
  * 一块矩形棚板 board(x, y, w, d, weight)，其上放置坯件(wareWeight)；
  * 若干立柱 posts，立柱的*顶端*托住本层棚板，*底端*落在第 i-1 层棚板上；
    最底层(i=0)立柱的底端落在窑车台面（car 矩形）内。

载荷自上而下传递：
  上层立柱的底端以“点载荷”形式落在下层棚板上。某层棚板承受的总载荷 =
      棚板自重 + 坯件重量 + 所有落在本板上的上层立柱点载荷
  再按“刚性棚板、有效立柱均摊”的工程简化模型，平均分配给所有
  *非悬空* 的立柱；悬空（底端掉在下层棚板/窑车台面之外）的立柱不承载。
  若整层没有一根有效立柱，则该层总载荷无法下传（记为丢失载荷）。

判定：
  hang     悬空：立柱底端坐标不在承托面（下层棚板或窑车台面）矩形内；
  overlap  落点重叠：同层两根及以上立柱底端坐标距离 <= TOL_MM，即踩在同一落点；
                     重叠点对下层棚板的点载荷为各立柱载荷之和；
  overload 超载：立柱实际分配载荷 > 其容量 capacity；
  failed   失效（推演）：调用方通过 failed 参数显式标记的非悬空立柱，视为不承载，
                     整架按同一规则重新分摊；与悬空区分标记，不修改输入数据。
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple

TOL_MM = 10.0  # 落点重叠判定距离（mm）

# 加柱建议：候选网格点试算规模上限（防止间距过小导致穷举爆炸）
MAX_SUGGEST_GRID = 4096
# 加柱建议：最多返回的方案数
MAX_SUGGEST_PLANS = 5


# --------------------------------------------------------------------------
# 工具函数
# --------------------------------------------------------------------------
def _num(v: Any, default: float = 0.0) -> float:
    """宽容地把输入转成 float；非法/None 用 default。"""
    try:
        if v is None or v == "":
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def r2(v: float) -> float:
    """输出统一保留两位小数，去掉 -0。"""
    out = round(float(v) + 0.0, 2)
    return 0.0 if out == 0 else out


def in_rect(x: float, y: float, rect: Dict[str, float]) -> bool:
    return (
        rect["x"] <= x <= rect["x"] + rect["w"]
        and rect["y"] <= y <= rect["y"] + rect["d"]
    )


def _post_label(layer_name: str, pid: str) -> str:
    return f"{layer_name}·{pid}"


# --------------------------------------------------------------------------
# 输入规范化
# --------------------------------------------------------------------------
def normalize(state: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    state = state or {}
    car_in = state.get("car") or {}
    car = {
        "name": str(car_in.get("name") or "窑车台面"),
        "w": max(0.0, _num(car_in.get("w"), 1200.0)),
        "d": max(0.0, _num(car_in.get("d"), 800.0)),
    }

    layers: List[Dict[str, Any]] = []
    for idx, raw in enumerate(state.get("layers") or []):
        raw = raw or {}
        b = raw.get("board") or {}
        posts = []
        for j, p in enumerate(raw.get("posts") or []):
            p = p or {}
            posts.append(
                {
                    "id": str(p.get("id") or f"P{j + 1}"),
                    "x": _num(p.get("x")),
                    "y": _num(p.get("y")),
                    "capacity": _num(p.get("capacity"), 0.0),
                }
            )
        layers.append(
            {
                "id": str(raw.get("id") or f"L{idx}"),
                "name": str(raw.get("name") or f"第{idx + 1}层"),
                "height": max(0.0, _num(raw.get("height"), 300.0)),
                "board": {
                    "x": _num(b.get("x")),
                    "y": _num(b.get("y")),
                    "w": max(0.0, _num(b.get("w"))),
                    "d": max(0.0, _num(b.get("d"))),
                    "weight": max(0.0, _num(b.get("weight"))),
                },
                "wareWeight": max(0.0, _num(raw.get("wareWeight"))),
                "posts": posts,
            }
        )
    return {"car": car, "layers": layers}


# --------------------------------------------------------------------------
# 重叠分组（按平面距离 <= TOL_MM；加柱建议会对数千个候选点反复重算，
# 用网格空间哈希把 O(n^2) 降到接近 O(n)）
# --------------------------------------------------------------------------
def _overlap_groups(points: List[Dict[str, Any]]) -> List[List[int]]:
    n = len(points)
    parent = list(range(n))

    def find(a: int) -> int:
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    # 边长 TOL_MM 的空间哈希桶：只需检查同桶与 8 个相邻桶
    cell: Dict[Tuple[int, int], List[int]] = {}
    for i, p in enumerate(points):
        key = (int(math.floor(p["x"] / TOL_MM)), int(math.floor(p["y"] / TOL_MM)))
        cell.setdefault(key, []).append(i)

    def near_cells(cx: int, cy: int):
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                yield cell.get((cx + dx, cy + dy))

    for i, p in enumerate(points):
        cx = int(math.floor(p["x"] / TOL_MM))
        cy = int(math.floor(p["y"] / TOL_MM))
        for bucket in near_cells(cx, cy):
            if not bucket:
                continue
            for j in bucket:
                if j <= i:
                    continue
                q = points[j]
                if math.hypot(p["x"] - q["x"], p["y"] - q["y"]) <= TOL_MM:
                    union(i, j)

    groups: Dict[int, List[int]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    return [g for g in groups.values() if len(g) >= 2]


# --------------------------------------------------------------------------
# 主计算
# --------------------------------------------------------------------------
def calculate(
    raw_state: Optional[Dict[str, Any]],
    failed: Optional[List[Any]] = None,
) -> Dict[str, Any]:
    """自顶层向下逐层分摊、累加载荷，返回完整结果（供前端渲染与持久化）。

    failed：失效推演标记，可迭代元素为 {"layer": i, "post": "P1"} 或 (i, "P1")。
    被标记的*非悬空*立柱视为不承载（输出 failed=True，与悬空区分），整架按
    同一规则重新分摊；输入数据本身不被修改。
    """
    return _calculate(normalize(raw_state), failed)


def _calculate(
    state: Dict[str, Any],
    failed: Optional[List[Any]] = None,
) -> Dict[str, Any]:
    """与 calculate 相同，但入参必须是 normalize() 后的架体（供批量试算复用）。"""
    failed_set = _normalize_failed(failed)
    car = state["car"]
    layers = state["layers"]
    n = len(layers)

    car_rect = {"x": 0.0, "y": 0.0, "w": car["w"], "d": car["d"]}
    issues: List[Dict[str, Any]] = []

    # incoming[i]：由第 i+1 层立柱底端施加在第 i 层棚板上的点载荷。
    # 每个点载荷 {"x","y","items":[{"chain","amount"}]}，sum(items)=总点载荷。
    incoming: List[List[Dict[str, Any]]] = [[] for _ in range(n)]

    result_layers: List[Dict[str, Any]] = []

    total_own = 0.0
    total_lost = 0.0

    for i in range(n - 1, -1, -1):
        layer = layers[i]
        board = layer["board"]
        name = layer["name"]
        bw, ww = board["weight"], layer["wareWeight"]
        total_own += bw + ww

        points = incoming[i]
        load_in = sum(sum(it["amount"] for it in p["items"]) for p in points)
        board_total = bw + ww + load_in

        # 承托面：底层为窑车台面，其余为下一层棚板
        if i == 0:
            support_kind, support_rect, support_name = (
                "car",
                car_rect,
                car["name"],
            )
        else:
            lb = layers[i - 1]["board"]
            support_kind = "board"
            support_rect = {"x": lb["x"], "y": lb["y"], "w": lb["w"], "d": lb["d"]}
            support_name = layers[i - 1]["name"] + "棚板"

        posts_out: List[Dict[str, Any]] = []
        supported_idx: List[int] = []
        hang_n = 0
        fail_n = 0
        for p in layer["posts"]:
            hanging = not in_rect(p["x"], p["y"], support_rect)
            is_failed = not hanging and (i, p["id"]) in failed_set
            po = {
                "id": p["id"],
                "x": r2(p["x"]),
                "y": r2(p["y"]),
                "capacity": r2(p["capacity"]),
                "load": 0.0,
                "hanging": hanging,
                "failed": is_failed,
                "overlap": False,
                "overloaded": False,
                "overlapWith": [],
                "sources": [],
            }
            posts_out.append(po)
            if hanging:
                hang_n += 1
                issues.append(
                    {
                        "level": "error",
                        "type": "hang",
                        "layer": i,
                        "post": p["id"],
                        "message": (
                            f"{name} 立柱 {p['id']}（{r2(p['x'])}, {r2(p['y'])}）"
                            f"底端悬空，未落在{support_name}范围内，不承载"
                        ),
                    }
                )
            elif is_failed:
                fail_n += 1
                issues.append(
                    {
                        "level": "info",
                        "type": "failed",
                        "layer": i,
                        "post": p["id"],
                        "message": (
                            f"{name} 立柱 {p['id']} 被标记为失效（推演），"
                            f"按不承载参与整架重新分摊"
                        ),
                    }
                )
            else:
                supported_idx.append(len(posts_out) - 1)

        c = len(supported_idx)
        unsupported = c == 0
        lost = 0.0
        if unsupported and layer["posts"]:
            lost = board_total
            total_lost += lost
            if fail_n:
                reason = f"均不承载（悬空 {hang_n} 根、失效 {fail_n} 根）"
            else:
                reason = "均悬空"
            issues.append(
                {
                    "level": "error",
                    "type": "unsupported",
                    "layer": i,
                    "post": None,
                    "message": (
                        f"{name}全部 {len(layer['posts'])} 根立柱{reason}，"
                        f"棚板载荷 {r2(board_total)} kg 无有效支撑、无法下传"
                    ),
                }
            )
        elif unsupported:
            # 没有录入立柱（如留空的顶层）：载荷同样无支撑
            lost = board_total
            total_lost += lost
            if board_total > 0:
                issues.append(
                    {
                        "level": "warn",
                        "type": "unsupported",
                        "layer": i,
                        "post": None,
                        "message": f"{name}未录入立柱，载荷 {r2(board_total)} kg 无支撑",
                    }
                )

        # 有效立柱均摊棚板总载荷，并构建载荷来源
        if c > 0:
            share = board_total / c
            for k in supported_idx:
                po = posts_out[k]
                po["load"] = r2(share)
                po["overloaded"] = p_capacity_over(po, share)
                sources: List[Dict[str, Any]] = []
                if bw > 0:
                    sources.append({"label": "本层棚板自重", "amount": r2(bw / c)})
                if ww > 0:
                    sources.append({"label": "本层坯件重量", "amount": r2(ww / c)})
                for pt in points:
                    for it in pt["items"]:
                        sources.append(
                            {"label": it["chain"], "amount": r2(it["amount"] / c)}
                        )
                po["sources"] = _merge_sources(sources)

        # 落点重叠（仅在有效立柱之间判定）
        overlap_sets = _overlap_groups(
            [{"x": posts_out[k]["x"], "y": posts_out[k]["y"]} for k in supported_idx]
        )
        for gi, group in enumerate(overlap_sets):
            members = [supported_idx[g] for g in group]
            ids = [posts_out[m]["id"] for m in members]
            combined = r2(sum(posts_out[m]["load"] for m in members))
            for m in members:
                po = posts_out[m]
                po["overlap"] = True
                po["overlapWith"] = [pid for pid in ids if pid != po["id"]]
            issues.append(
                {
                    "level": "warn",
                    "type": "overlap",
                    "layer": i,
                    "post": ids[0],
                    "message": (
                        f"{name} 立柱 {'、'.join(ids)} 底端落在同一坐标"
                        f"（{posts_out[members[0]]['x']}, {posts_out[members[0]]['y']}），"
                        f"对{support_name}的合并点载荷 {combined} kg"
                    ),
                }
            )

        # 超载告警
        for po in posts_out:
            if po["overloaded"]:
                issues.append(
                    {
                        "level": "error",
                        "type": "overload",
                        "layer": i,
                        "post": po["id"],
                        "message": (
                            f"{name} 立柱 {po['id']} 超载："
                            f"载荷 {po['load']} kg > 容量 {po['capacity']} kg"
                        ),
                    }
                )

        # 本层有效立柱以“点载荷”施加到下层棚板；重叠落点合并为一个点
        if i > 0 and not unsupported:
            grouped_members = set()
            for group in overlap_sets:
                members = [supported_idx[g] for g in group]
                grouped_members.update(members)
                rep = posts_out[members[0]]
                items: List[Dict[str, Any]] = []
                for m in members:
                    items.extend(_prop_items(layer["name"], posts_out[m], bw, ww, points, c))
                incoming[i - 1].append(
                    {
                        "x": rep["x"],
                        "y": rep["y"],
                        "items": [
                            {"chain": k, "amount": r2(v)}
                            for k, v in _sum_items(items).items()
                        ],
                    }
                )
            for k in supported_idx:
                if k in grouped_members:
                    continue
                po = posts_out[k]
                items = _prop_items(layer["name"], po, bw, ww, points, c)
                incoming[i - 1].append(
                    {
                        "x": po["x"],
                        "y": po["y"],
                        "items": [
                            {"chain": k2, "amount": r2(v2)}
                            for k2, v2 in _sum_items(items).items()
                        ],
                    }
                )

        carried = r2(sum(po["load"] for po in posts_out))
        result_layers.append(
            {
                "index": i,
                "id": layer["id"],
                "name": name,
                "height": r2(layer["height"]),
                "board": {
                    "x": r2(board["x"]),
                    "y": r2(board["y"]),
                    "w": r2(board["w"]),
                    "d": r2(board["d"]),
                    "weight": r2(board["weight"]),
                },
                "wareWeight": r2(ww),
                "supportKind": support_kind,
                "supportName": support_name,
                "supportRect": {k: r2(v) for k, v in support_rect.items()},
                "loadOwn": r2(bw + ww),
                "loadIncoming": r2(load_in),
                "loadTotal": r2(board_total),
                "loadCarried": carried,
                "lostLoad": r2(lost),
                "supportedCount": c,
                "hangingCount": hang_n,
                "failedCount": fail_n,
                "posts": posts_out,
            }
        )

    result_layers.reverse()  # 恢复自下而上

    # 输入校验类提示
    for i, layer in enumerate(layers):
        if layer["board"]["w"] <= 0 or layer["board"]["d"] <= 0:
            issues.append(
                {
                    "level": "warn",
                    "type": "invalid",
                    "layer": i,
                    "post": None,
                    "message": f"{layer['name']}棚板宽/深为 0，请检查棚板尺寸",
                }
            )
        ids = [p["id"] for p in layer["posts"]]
        if len(ids) != len(set(ids)):
            issues.append(
                {
                    "level": "warn",
                    "type": "invalid",
                    "layer": i,
                    "post": None,
                    "message": f"{layer['name']}存在重复立柱编号，请修正",
                }
            )

    order = {"overload": 0, "hang": 1, "unsupported": 2, "failed": 3, "overlap": 4, "invalid": 5}
    issues.sort(key=lambda e: (order.get(e["type"], 9), e["layer"], str(e["post"])))

    bottom_carried = (
        result_layers[0]["loadCarried"] if result_layers else 0.0
    )
    return {
        "car": {"name": car["name"], "w": r2(car["w"]), "d": r2(car["d"])},
        "layers": result_layers,
        "issues": issues,
        "totals": {
            "ownWeight": r2(total_own),
            "carriedAtBottom": bottom_carried,
            "lostLoad": r2(total_lost),
            "postCount": sum(len(l["posts"]) for l in result_layers),
            "failedCount": sum(l["failedCount"] for l in result_layers),
            "layerCount": n,
        },
        "toleranceMm": TOL_MM,
    }


def p_capacity_over(po: Dict[str, Any], load: float) -> bool:
    return po["capacity"] > 0 and load > po["capacity"] + 1e-9


def _merge_sources(sources: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    agg: Dict[str, float] = {}
    for s in sources:
        agg[s["label"]] = agg.get(s["label"], 0.0) + s["amount"]
    return [{"label": k, "amount": r2(v)} for k, v in agg.items()]


def _sum_items(items: List[Dict[str, Any]]) -> Dict[str, float]:
    agg: Dict[str, float] = {}
    for it in items:
        agg[it["chain"]] = agg.get(it["chain"], 0.0) + it["amount"]
    return agg


def _prop_items(
    layer_name: str,
    po: Dict[str, Any],
    bw: float,
    ww: float,
    points: List[Dict[str, Any]],
    c: int,
) -> List[Dict[str, Any]]:
    """生成本层立柱底端施加到下层棚板的点载荷来源链。"""
    label = _post_label(layer_name, po["id"])
    items: List[Dict[str, Any]] = []
    own = (bw + ww) / c
    if own > 0:
        items.append({"chain": label, "amount": own})
    for pt in points:
        for it in pt["items"]:
            items.append(
                {"chain": f"{it['chain']} → {label}", "amount": it["amount"] / c}
            )
    return items


# --------------------------------------------------------------------------
# 立柱失效推演（What-if）：基准 vs 推演并排对比
# --------------------------------------------------------------------------
def _normalize_failed(failed: Optional[List[Any]]) -> set:
    """把失效标记规范化为 {(layer_index, post_id)}；非法项静默忽略。"""
    out = set()
    for item in failed or []:
        li: Any = None
        pid: Any = None
        if isinstance(item, dict):
            li, pid = item.get("layer"), item.get("post")
        elif isinstance(item, (list, tuple)) and len(item) == 2:
            li, pid = item
        try:
            li = int(li)
        except (TypeError, ValueError):
            continue
        if pid is None or str(pid) == "":
            continue
        out.add((li, str(pid)))
    return out


def simulate(
    raw_state: Optional[Dict[str, Any]],
    failed: Optional[List[Any]] = None,
) -> Dict[str, Any]:
    """以当前输入为基准，将指定立柱标记为失效后整架重算，返回基准/推演/差值。

    不修改、不持久化任何输入；失效标记仅作用于本次调用。
    """
    baseline = calculate(raw_state)
    simulation = calculate(raw_state, failed=failed)
    applied = [
        {"layer": L["index"], "layerName": L["name"], "post": p["id"]}
        for L in simulation["layers"]
        for p in L["posts"]
        if p.get("failed")
    ]
    return {
        "baseline": baseline,
        "simulation": simulation,
        "failed": applied,
        "diff": _diff_results(baseline, simulation),
    }


def _diff_results(base: Dict[str, Any], sim: Dict[str, Any]) -> Dict[str, Any]:
    """基准 vs 推演：台面载荷/丢失载荷对比、每根立柱载荷差值、新增超载或无支撑。"""

    def issue_key(e: Dict[str, Any]) -> Tuple[str, int, str]:
        return (e["type"], e["layer"], str(e["post"]))

    base_keys = {issue_key(e) for e in base["issues"]}
    new_issues = [
        e
        for e in sim["issues"]
        if e["type"] in ("overload", "unsupported") and issue_key(e) not in base_keys
    ]

    base_posts = {
        (L["index"], p["id"]): p for L in base["layers"] for p in L["posts"]
    }
    posts: List[Dict[str, Any]] = []
    for L in sim["layers"]:
        for p in L["posts"]:
            b = base_posts.get((L["index"], p["id"])) or {}
            base_load = b.get("load", 0.0)
            posts.append(
                {
                    "layer": L["index"],
                    "layerName": L["name"],
                    "post": p["id"],
                    "base": base_load,
                    "sim": p["load"],
                    "delta": r2(p["load"] - base_load),
                    "failed": bool(p.get("failed")),
                    "hanging": bool(p.get("hanging")),
                    "overloaded": bool(p.get("overloaded")),
                }
            )

    def pair(key: str) -> Dict[str, float]:
        b = base["totals"][key]
        s = sim["totals"][key]
        return {"base": b, "sim": s, "delta": r2(s - b)}

    return {
        "carriedAtBottom": pair("carriedAtBottom"),
        "lostLoad": pair("lostLoad"),
        "posts": posts,
        "newIssues": new_issues,
    }


# --------------------------------------------------------------------------
# 减载配额试算（Load Shedding Quota）
#
# 几何/立柱不变、只允许下调各层坯件重量 w_i（0 ≤ r_i ≤ w_i，r_i 为本层减量）时，
# 第 i 层棚板总载荷关于 r 是线性的：
#
#     T_i(r) = T0_i + Σ_{k∈run(i)} (w_k - r_k)
#
# 其中 T0_i 为全部坯件清零时第 i 层棚板总载荷（板自重 + 上层结构传递），
# run(i) 为与第 i 层经“每层至少一根有效柱”连通的最高层到 i 的连续层段；
# 某层 c_i=0（有效柱为 0，含全悬空/无柱）时载荷下传中断，层段在此截断。
#
# 本层有效柱均摊，最紧约束来自容量最小的有限容量柱：
#
#     T_i(r) ≤ c_i · capmin_i
#
# 即每个有限容量层给出一个“后缀减量和”下界：
#
#     Σ_{k=run(i)..i} r_k ≥ d_i
#
# 这是嵌套的覆盖型线性规划：层段内自顶向下，d_i 单调增长；最小总减量
# R*_i = max(d_i, R*_{i-1})（跨 c=0 层重置），再按“优先减本层、不足部分
# 由近及远向上层回补”分配各 r_k 即可达到该下界。
# --------------------------------------------------------------------------
def _to_units(v: float) -> int:
    """把 kg 重量/容量就近换算为 0.01 kg 整数单位（消除二进制浮点尾差）。"""
    return int(round(max(0.0, float(v)) * 100.0 + 1e-9))


def _shed_constants(
    norm: Dict[str, Any]
) -> Tuple[List[int], List[Optional[float]], List[float]]:
    """与坯件重量无关的整架常量：
    返回 (c, capmin, t0)：
      c[i]      第 i 层有效（非悬空）立柱数
      capmin[i] 第 i 层有效柱中最小的正容量（无有限容量柱时为 None，保留精确原值）
      t0[i]     全部坯件清零时第 i 层棚板总载荷（板自重 + 上层立柱传递）
    """
    layers = norm["layers"]
    n = len(layers)
    support_rects = [_support_rect_for(norm, i) for i in range(n)]
    c = [0] * n
    capmin: List[Optional[float]] = [None] * n
    for i, L in enumerate(layers):
        caps: List[float] = []
        rect = support_rects[i]
        for p in L["posts"]:
            if in_rect(p["x"], p["y"], rect):
                c[i] += 1
                if p["capacity"] > 0:
                    caps.append(p["capacity"])
        if caps:
            capmin[i] = min(caps)

    t0 = [0.0] * n
    for i in range(n - 1, -1, -1):
        # 处理第 i 层时，t0[i] 已累计第 i+1 层（及其上层）在坯件清零下的下传载荷
        total = layers[i]["board"]["weight"] + t0[i]
        if i > 0 and c[i] > 0:
            t0[i - 1] += total  # 有效柱均摊且全部下传；c=0 层载荷丢失、不传递
        t0[i] = total
    return c, capmin, t0


def _segment_tops(c: List[int]) -> List[int]:
    """seg_top[i]：与第 i 层连通的最高层索引（载荷自上而下传递，c=0 层截断）。

    第 i 层棚板总载荷受第 i..seg_top[i] 层坯件重量影响；同层段内层数越大越靠上。
    """
    seg_top = [0] * len(c)
    top = len(c) - 1
    for i in range(len(c) - 1, -1, -1):
        if c[i] == 0:
            top = i - 1
        seg_top[i] = top
    return seg_top


def _shed_greedy_units(d_units: List[int], wu: List[int], c: List[int],
                       seg_top: List[int]) -> List[int]:
    """在 0.01 kg 整数网格上自顶向下分配各层减量单位（嵌套后缀覆盖约束贪心）。

    d_units[i]：第 i 层要求的后缀减量单位下界（仅有限容量层 > 0）；
    wu[i]：第 i 层可减的最大单位数。每层先减本层坯件，不足由近及远向上层
    回补；返回各层减量单位 r_alloc_u。
    """
    n = len(wu)
    r_alloc_u = [0] * n
    for i in range(n - 1, -1, -1):
        if c[i] == 0 or d_units[i] <= 0:
            continue
        r_upper = sum(r_alloc_u[i + 1:seg_top[i] + 1])
        if d_units[i] <= r_upper:
            continue  # 上层已分配的减量已覆盖本层约束
        need = d_units[i] - r_upper
        take = min(need, wu[i] - r_alloc_u[i])
        r_alloc_u[i] += take
        need -= take
        j = i + 1  # 本层额度不足：由近及远向上层回补（均在同一连通层段内）
        while need > 0 and j <= seg_top[i]:
            take = min(need, wu[j] - r_alloc_u[j])
            r_alloc_u[j] += take
            need -= take
            j += 1
    return r_alloc_u


def _overload_posts(res: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [
        {
            "layer": L["index"],
            "layerName": L["name"],
            "post": p["id"],
            "load": p["load"],
            "capacity": p["capacity"],
        }
        for L in res["layers"]
        for p in L["posts"]
        if p.get("overloaded")
    ]


def load_shed(raw_state: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """减载配额试算：只调整各层坯件重量，求消除所有有限容量立柱超载的最小总减量。

    几何（台面/棚板/立柱坐标与容量）完全不动；每层减量 r_i ∈ [0, w_i]。
    多层载荷按现有“刚性棚板、有效柱均摊、悬空不承载、无有效柱则丢失”的
    整架规则共同下传，因此超载约束是嵌套的后缀减量约束，存在闭式最优解。

    求解先用精确浮点求出连续最优的各层后缀减量需求，再把需求向上取整到
    0.01 kg 网格（页面录入/展示精度），在整数网格上自顶向下分配，最后用同一套
    _calculate 重新整架校验；容量若不是 0.01 kg 的整数倍，取整只会留出安全
    余量，不会造成误判。方案在 0.01 kg 网格上总减量最小（任一被减层回调
    0.01 kg 都会再次超载）。

    只计算、不修改输入、不写盘：返回每层原重量/建议重量/减量与减载前后的
    超载数、丢失载荷；求得方案后用同一套 _calculate 重新整架校验。
    若全部坯件清空仍有超载（棚板自重等结构载荷已超过容量），feasible=False，
    并给出清空后的剩余超载柱清单。
    """
    norm = normalize(raw_state)
    layers = norm["layers"]
    n = len(layers)
    baseline = _calculate(norm)

    if n == 0:
        return {
            "feasible": True,
            "totalReduction": 0.0,
            "layers": [],
            "baseline": baseline,
            "trial": baseline,
            "remainingOverloads": [],
            "reason": None,
        }

    c, capmin, t0 = _shed_constants(norm)
    seg_top = _segment_tops(c)
    w = [max(0.0, L["wareWeight"]) for L in layers]
    wu = [_to_units(v) for v in w]  # 各层坯件重量（0.01 kg 整数单位）

    # 每个有限容量层连续意义上所需的后缀减量（kg）
    d = [0.0] * n
    for i in range(n):
        if c[i] > 0 and capmin[i] is not None:
            run_ware = sum(w[i:seg_top[i] + 1])
            need = t0[i] + run_ware - c[i] * capmin[i]
            d[i] = need if need > 1e-9 else 0.0

    # 极端工况：全部坯件清空（最大可能减载）后整架重算，仍超载即无解。
    # 这是“是否有解”的唯一权威判据（容量可能不是 0.01 整数倍，必须按精确值判）。
    cleared_norm = _clone_norm(norm)
    for L in cleared_norm["layers"]:
        L["wareWeight"] = 0.0
    cleared = _calculate(cleared_norm)
    remaining_cleared = _overload_posts(cleared)
    if remaining_cleared:
        return _shed_result(
            feasible=False,
            layers=layers,
            original=w,
            suggested=[0.0] * n,
            reductions=w,
            baseline=baseline,
            trial=cleared,
            remaining=remaining_cleared,
            reason=(
                f"即使全部坯件清空（共减载 {r2(sum(w))} kg），仍有 "
                f"{len(remaining_cleared)} 根有限容量立柱超载；仅靠减载无法消除，"
                "请加柱、更换大容量柱或减轻棚板自重后再试"
            ),
        )

    # 连续需求向上取整到 0.01 kg 整单位，再在整数网格上贪心分配各层减量
    d_units = [0] * n
    for i in range(n):
        if d[i] > 0.0:
            d_units[i] = int(math.ceil(d[i] * 100.0 - 1e-9))
    r_alloc_u = _shed_greedy_units(d_units, wu, c, seg_top)

    # 建议重量即整数网格上的原重量 − 减量（0.01 kg），本层无减量则原样返回
    def _build_trial(alloc_u: List[int]) -> Dict[str, Any]:
        trial_norm = _clone_norm(norm)
        for k, L in enumerate(trial_norm["layers"]):
            L["wareWeight"] = w[k] if alloc_u[k] == 0 else (wu[k] - alloc_u[k]) / 100.0
        return _calculate(trial_norm)

    trial = _build_trial(r_alloc_u)
    remaining = _overload_posts(trial)
    if remaining:
        # 极小概率（浮点/非网格容量边界）：取整方案被权威整架校验判超载。
        # 清空已证明有解 → 自超载层起向其连通上层补 1 个网格单位并重算，直到通过。
        for _ in range(sum(wu) + 1):
            over_layers = {p["layer"] for p in remaining}
            bumped = False
            for li in sorted(over_layers):
                for j in range(li, seg_top[li] + 1):
                    if r_alloc_u[j] < wu[j]:
                        r_alloc_u[j] += 1
                        bumped = True
                        break
                if bumped:
                    break
            if not bumped:
                break
            trial = _build_trial(r_alloc_u)
            remaining = _overload_posts(trial)
            if not remaining:
                break
        if remaining:
            # 理论不可达（清空已无超载）；安全兜底按无解返回
            return _shed_result(
                feasible=False,
                layers=layers,
                original=w,
                suggested=[0.0] * n,
                reductions=w,
                baseline=baseline,
                trial=cleared,
                remaining=remaining_cleared,
                reason=(
                    f"试算方案重新整架校验时仍有 {len(remaining)} 根立柱超载，"
                    "已按全部坯件清空返回剩余超载柱，请加柱或更换大容量柱"
                ),
            )

    suggested = [
        w[i] if r_alloc_u[i] == 0 else (wu[i] - r_alloc_u[i]) / 100.0 for i in range(n)
    ]
    return _shed_result(
        feasible=True,
        layers=layers,
        original=w,
        suggested=suggested,
        reductions=[w[i] - suggested[i] for i in range(n)],
        baseline=baseline,
        trial=trial,
        remaining=[],
        reason=None,
    )


def _clone_norm(norm: Dict[str, Any]) -> Dict[str, Any]:
    """深拷贝 normalize() 后的架体（结构简单，手写避免引入 copy 外的依赖约定）。"""
    return {
        "car": dict(norm["car"]),
        "layers": [
            {
                "id": L["id"],
                "name": L["name"],
                "height": L["height"],
                "board": dict(L["board"]),
                "wareWeight": L["wareWeight"],
                "posts": [dict(p) for p in L["posts"]],
            }
            for L in norm["layers"]
        ],
    }


def _shed_result(
    *,
    feasible: bool,
    layers: List[Dict[str, Any]],
    original: List[float],
    suggested: List[float],
    reductions: List[float],
    baseline: Dict[str, Any],
    trial: Dict[str, Any],
    remaining: List[Dict[str, Any]],
    reason: Optional[str],
) -> Dict[str, Any]:
    before = {L["index"]: L for L in baseline["layers"]}
    after = {L["index"]: L for L in trial["layers"]}
    rows: List[Dict[str, Any]] = []
    for i, L in enumerate(layers):
        bL, tL = before[i], after[i]
        over_before = sum(1 for p in bL["posts"] if p.get("overloaded"))
        over_after = sum(1 for p in tL["posts"] if p.get("overloaded"))
        reduction = reductions[i]
        rows.append(
            {
                "index": i,
                "id": L["id"],
                "name": L["name"],
                "wareOriginal": r2(original[i]),
                "wareSuggested": r2(suggested[i]),
                "reduction": r2(reduction),
                "changed": reduction > 1e-9,
                "supportedCount": bL["supportedCount"],
                "overloadBefore": over_before,
                "overloadAfter": over_after,
                "lostBefore": bL["lostLoad"],
                "lostAfter": tL["lostLoad"],
            }
        )
    return {
        "feasible": feasible,
        "totalReduction": r2(sum(reductions)),
        "layers": rows,
        "metrics": {
            "overloadCount": {
                "before": _count_overloaded(baseline),
                "after": _count_overloaded(trial),
            },
            "lostLoad": {
                "before": baseline["totals"]["lostLoad"],
                "after": trial["totals"]["lostLoad"],
            },
            "carriedAtBottom": {
                "before": baseline["totals"]["carriedAtBottom"],
                "after": trial["totals"]["carriedAtBottom"],
            },
        },
        "baseline": baseline,
        "trial": trial,
        "remainingOverloads": remaining,
        "reason": reason,
    }


# --------------------------------------------------------------------------
# 加柱建议（Suggest Post）：在目标层候选网格上逐点试算新增一根立柱
# --------------------------------------------------------------------------
def _support_rect_for(norm: Dict[str, Any], i: int) -> Dict[str, float]:
    """第 i 层立柱底端的承托面矩形（底层为窑车台面，其余为下一层棚板）。"""
    if i == 0:
        car = norm["car"]
        return {"x": 0.0, "y": 0.0, "w": car["w"], "d": car["d"]}
    lb = norm["layers"][i - 1]["board"]
    return {"x": lb["x"], "y": lb["y"], "w": lb["w"], "d": lb["d"]}


def _fast_trial_scores(
    norm: Dict[str, Any],
    li: int,
    cap: float,
    feasible: List[Tuple[float, float]],
    tol: float = TOL_MM,
) -> List[Tuple[float, int, Optional[float], float, float]]:
    """加柱建议批量快路：对每个可行点试算新柱，只产出排序所需打分。

    与 _calculate 同一物理规则（刚性棚板、有效柱均摊、悬空不承载、重叠点
    合并下传），但不生成来源链 / 告警文案 / 逐值 round，也不构建对外结果
    结构——逐点试算量可达数千次，这里省去绝大部分开销。返回
    (lostLoad, overloadCount, maxUtilization(None=全部不限容量), x, y)。
    """
    layers = norm["layers"]
    n = len(layers)
    car = norm["car"]

    # 各层承托面矩形与基准“有效柱”（非悬空）坐标/容量
    support_rects = [_support_rect_for(norm, i) for i in range(n)]
    base_xy: List[List[Tuple[float, float]]] = []
    base_caps: List[List[float]] = []
    for i, L in enumerate(layers):
        pairs: List[Tuple[float, float]] = []
        cs: List[float] = []
        rect = support_rects[i]
        for p in L["posts"]:
            if in_rect(p["x"], p["y"], rect):
                pairs.append((p["x"], p["y"]))
                cs.append(p["capacity"])
        base_xy.append(pairs)
        base_caps.append(cs)

    scores: List[Tuple[float, int, Optional[float], float, float]] = []
    for x, y in feasible:
        xy = [list(pairs) for pairs in base_xy]
        caps = [list(cs) for cs in base_caps]
        xy[li].append((x, y))
        caps[li].append(cap)

        # incoming_items[i]：落到第 i 层棚板上的点载荷（未合并），
        # 每个点 (x, y, load)；先求本层总载荷，重叠点合并后再下传。
        incoming_items: List[List[Tuple[float, float, float]]] = [
            [] for _ in range(n)
        ]
        total_lost = 0.0
        overload_n = 0
        max_util: Optional[float] = None

        for i in range(n - 1, -1, -1):
            L = layers[i]
            b = L["board"]
            board_total = b["weight"] + L["wareWeight"] + sum(
                ld for _, _, ld in incoming_items[i]
            )
            c = len(xy[i])
            if c == 0:
                total_lost += board_total
                continue  # 无有效柱：载荷丢失，不向下传递
            share = board_total / c
            for cp in caps[i]:
                if cp > 0:
                    if share > cp + 1e-9:
                        overload_n += 1
                    u = share / cp
                    if max_util is None or u > max_util:
                        max_util = u

            # 重叠落点（距离 <= tol）合并为一个点载荷，下传到第 i-1 层
            if i > 0:
                cell = max(tol, 1e-9)
                merged: Dict[Tuple[int, int], Tuple[float, float, float]] = {}

                def find_bucket(px: float, py: float):
                    gx = int(math.floor(px / cell))
                    gy = int(math.floor(py / cell))
                    for dx in (-1, 0, 1):
                        for dy in (-1, 0, 1):
                            kk = (gx + dx, gy + dy)
                            if kk in merged:
                                mx, my, _ = merged[kk]
                                if math.hypot(px - mx, py - my) <= tol:
                                    return kk
                    return None

                for px, py in xy[i]:
                    key = find_bucket(px, py)
                    if key is None:
                        key = (int(math.floor(px / cell)), int(math.floor(py / cell)))
                        merged[key] = (px, py, share)
                    else:
                        mx, my, mload = merged[key]
                        merged[key] = (mx, my, mload + share)
                for mx, my, mload in merged.values():
                    incoming_items[i - 1].append((mx, my, mload))

        scores.append((total_lost, overload_n, max_util, x, y))
    return scores


def _issue_key(e: Dict[str, Any]) -> Tuple[str, int, str]:
    return (e["type"], e["layer"], str(e["post"]))


def _count_overloaded(res: Dict[str, Any]) -> int:
    return sum(
        1
        for L in res["layers"]
        for p in L["posts"]
        if p.get("overloaded")
    )


def _max_utilization(res: Dict[str, Any]) -> Optional[float]:
    """整架最高利用率 = max(load/capacity)；容量为 0（不限容量）的立柱不参与。

    所有立柱均不限容量时返回 None（排序时排在最后）。
    """
    best: Optional[float] = None
    for L in res["layers"]:
        for p in L["posts"]:
            if p.get("capacity", 0.0) > 0:
                u = p["load"] / p["capacity"]
                if best is None or u > best:
                    best = u
    return best


def _layer_rows(base: Dict[str, Any], trial: Dict[str, Any]) -> List[Dict[str, Any]]:
    """逐层载荷对比：本层总载荷 / 丢失载荷 / 下传载荷 / 超载柱数。"""
    rows: List[Dict[str, Any]] = []
    for bL, tL in zip(base["layers"], trial["layers"]):
        b_ov = sum(1 for p in bL["posts"] if p.get("overloaded"))
        t_ov = sum(1 for p in tL["posts"] if p.get("overloaded"))
        rows.append(
            {
                "layer": tL["index"],
                "name": tL["name"],
                "loadTotal": {"base": bL["loadTotal"], "trial": tL["loadTotal"]},
                "lostLoad": {"base": bL["lostLoad"], "trial": tL["lostLoad"]},
                "loadCarried": {"base": bL["loadCarried"], "trial": tL["loadCarried"]},
                "overloadCount": {"base": b_ov, "trial": t_ov},
            }
        )
    return rows


def _issue_changes(base: Dict[str, Any], trial: Dict[str, Any]) -> Dict[str, Any]:
    """新增 / 消除告警（以 (type, layer, post) 为键对比）。"""
    base_map = {_issue_key(e): e for e in base["issues"]}
    trial_map = {_issue_key(e): e for e in trial["issues"]}
    added = [trial_map[k] for k in trial_map.keys() - base_map.keys()]
    removed = [base_map[k] for k in base_map.keys() - trial_map.keys()]

    def _order(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        order = {
            "overload": 0, "hang": 1, "unsupported": 2,
            "failed": 3, "overlap": 4, "invalid": 5,
        }
        return sorted(items, key=lambda e: (order.get(e["type"], 9), e["layer"], str(e["post"])))

    return {"added": _order(added), "removed": _order(removed)}


def suggest_posts(
    raw_state: Optional[Dict[str, Any]],
    layer: Any,
    spacing: Any,
    capacity: Any,
) -> Dict[str, Any]:
    """加柱建议：在目标层的承托面内按候选网格逐点试算新增一根立柱。

    参数
    ----
    raw_state : 当前架体输入（不被修改、不被持久化）
    layer     : 目标层索引（自下而上，0 为底层）
    spacing   : 候选网格间距 mm（>0）
    capacity  : 新柱容量 kg（0 表示不限容量，不计利用率）

    可行点条件：候选点在承托面矩形内，且与目标层*现有有效立柱*（非悬空）
    底端平面距离严格大于 TOL_MM（避免与现有落点重叠）。

    排序（全部升序）：丢失载荷 → 超载数 → 最高利用率（无容量限制柱的方案
    排最后）→ 坐标（x、y）；仅返回前 MAX_SUGGEST_PLANS 个方案。

    只计算、不写盘：每个方案附带试算后的完整结果与逐层/告警对比，由前端
    预览并经用户确认后，才通过原有 PUT /api/state 写入架体。
    """
    norm = normalize(raw_state)
    layers = norm["layers"]
    n = len(layers)

    try:
        li = int(layer)
    except (TypeError, ValueError):
        raise ValueError("目标层索引必须是整数")
    if not 0 <= li < n:
        raise ValueError(f"目标层 {layer!r} 不存在：当前共 {n} 层（索引 0~{n - 1}）")

    try:
        gap = float(spacing)
    except (TypeError, ValueError):
        raise ValueError("候选网格间距必须是数值（mm）")
    if not math.isfinite(gap) or gap <= 0:
        raise ValueError("候选网格间距必须大于 0")

    try:
        cap = float(capacity)
    except (TypeError, ValueError):
        raise ValueError("新柱容量必须是数值（kg，0=不限容量）")
    if not math.isfinite(cap) or cap < 0:
        raise ValueError("新柱容量不能为负；0 表示不限容量")

    rect = _support_rect_for(norm, li)
    if rect["w"] <= 0 or rect["d"] <= 0:
        return {
            "layer": li,
            "layerName": layers[li]["name"],
            "spacing": r2(gap),
            "capacity": r2(cap),
            "gridCount": 0,
            "feasibleCount": 0,
            "plans": [],
            "baseline": None,
            "reason": (
                f"{layers[li]['name']}的承托面宽或深为 0"
                f"（{r2(rect['w'])}×{r2(rect['d'])} mm），没有可落柱的位置，"
                "请先修正承托面尺寸"
            ),
        }

    baseline = _calculate(norm)
    # 目标层现有*有效立柱*（非悬空）坐标；用 normalize 后的未截断坐标判距，
    # 避免边界点因 r2 两位小数被误纳/误排
    existing_effective = [
        (p["x"], p["y"])
        for p in layers[li]["posts"]
        if in_rect(p["x"], p["y"], rect)
    ]

    x0, y0 = rect["x"], rect["y"]
    x1, y1 = x0 + rect["w"], y0 + rect["d"]
    nx = int(math.floor((x1 - x0) / gap)) + 1
    ny = int(math.floor((y1 - y0) / gap)) + 1
    grid_count = nx * ny
    if grid_count > MAX_SUGGEST_GRID:
        return {
            "layer": li,
            "layerName": layers[li]["name"],
            "spacing": r2(gap),
            "capacity": r2(cap),
            "gridCount": grid_count,
            "feasibleCount": 0,
            "plans": [],
            "baseline": baseline,
            "reason": (
                f"候选网格过密：间距 {r2(gap)} mm 在承托面"
                f"（{r2(rect['w'])}×{r2(rect['d'])} mm）上产生 {grid_count} 个候选点，"
                f"超过试算上限 {MAX_SUGGEST_GRID} 个，请加大候选网格间距后重试"
            ),
        }

    # 新柱编号：取目标层未占用的 P 系列编号
    used_ids = {p["id"] for p in layers[li]["posts"]}
    new_id = None
    k = 1
    while new_id is None:
        cand = f"P{k}"
        if cand not in used_ids:
            new_id = cand
        k += 1

    feasible: List[Tuple[float, float]] = []
    for ix in range(nx):
        x = x0 + ix * gap
        if x > x1 + 1e-9:
            continue
        for iy in range(ny):
            y = y0 + iy * gap
            if y > y1 + 1e-9:
                continue
            # 与现有有效柱距离必须严格大于重叠阈值 TOL_MM
            too_close = any(
                math.hypot(x - ex, y - ey) <= TOL_MM
                for ex, ey in existing_effective
            )
            if not too_close:
                feasible.append((x, y))

    if not feasible:
        reason = (
            f"{layers[li]['name']}承托面内的 {grid_count} 个网格候选点，"
            f"全部与现有有效立柱落点距离 ≤ {TOL_MM:g} mm（重叠阈值），"
            "没有可新增立柱的位置；可减小现有立柱密度后再试"
        ) if grid_count else f"{layers[li]['name']}承托面内没有候选网格点"
        return {
            "layer": li,
            "layerName": layers[li]["name"],
            "spacing": r2(gap),
            "capacity": r2(cap),
            "gridCount": grid_count,
            "feasibleCount": 0,
            "plans": [],
            "baseline": baseline,
            "reason": reason,
        }

    base_lost = baseline["totals"]["lostLoad"]
    base_over = _count_overloaded(baseline)
    base_util = _max_utilization(baseline)

    # 快路：逐点只算排序打分（不含来源链/告警文案），避免数千次完整重算。
    # 快路直接给出精确的载荷/利用率（不做 r2 两位截断），可直接用于排序；
    # 仅对最终入选的前 5 个点再跑完整 _calculate 产出对外展示结果。
    fast_sorted = sorted(
        _fast_trial_scores(norm, li, cap, feasible),
        key=lambda t: (
            t[0], t[1], t[2] if t[2] is not None else math.inf, t[3], t[4]
        ),
    )
    chosen_pts = fast_sorted[:MAX_SUGGEST_PLANS]

    # 入选的少数点：在目标层临时追加新柱，用完整 _calculate 产出对比/预览结果
    posts_list = layers[li]["posts"]
    trial_post = {"id": new_id, "x": 0.0, "y": 0.0, "capacity": cap}
    posts_list.append(trial_post)
    scored: List[Tuple[float, int, float, float, float, Dict[str, Any]]] = []
    try:
        for _, _, _, x, y in chosen_pts:
            trial_post["x"], trial_post["y"] = x, y
            trial = _calculate(norm)

            lost = trial["totals"]["lostLoad"]
            over = _count_overloaded(trial)
            util = _max_utilization(trial)  # None=无容量受限柱，排序置后
            scored.append(
                (lost, over, util if util is not None else math.inf, x, y, trial)
            )
    finally:
        posts_list.pop()

    scored.sort(key=lambda t: (t[0], t[1], t[2], t[3], t[4]))

    plans: List[Dict[str, Any]] = []
    for rank, (lost, over, util, x, y, trial) in enumerate(
        scored[:MAX_SUGGEST_PLANS]
    ):
        new_post = next(
            p for p in trial["layers"][li]["posts"] if p["id"] == new_id
        )
        util_val = None if util == math.inf else util
        changes = _issue_changes(baseline, trial)
        plans.append(
            {
                "rank": rank + 1,
                "post": {
                    "id": new_id,
                    "x": r2(x),
                    "y": r2(y),
                    "capacity": r2(cap),
                    "load": new_post["load"],
                    "overloaded": bool(new_post["overloaded"]),
                },
                "metrics": {
                    "lostLoad": r2(lost),
                    "overloadCount": over,
                    "maxUtilization": (
                        None if util_val is None else round(util_val, 4)
                    ),
                    "carriedAtBottom": trial["totals"]["carriedAtBottom"],
                },
                "layers": _layer_rows(baseline, trial),
                "issues": changes,
                "result": trial,
            }
        )

    return {
        "layer": li,
        "layerName": layers[li]["name"],
        "spacing": r2(gap),
        "capacity": r2(cap),
        "gridCount": grid_count,
        "feasibleCount": len(feasible),
        "baselineMetrics": {
            "lostLoad": base_lost,
            "overloadCount": base_over,
            "maxUtilization": None if base_util is None else round(base_util, 4),
            "carriedAtBottom": baseline["totals"]["carriedAtBottom"],
        },
        "plans": plans,
        "reason": None,
        "toleranceMm": TOL_MM,
    }


# --------------------------------------------------------------------------
# 层架抗倾覆试算（Overturning Check）
#
# 只依赖架体几何与重量（与载荷分摊/容量无关）：
#   * 第 i 层棚板与坯件视为同一质点：水平位置取棚板中心
#     (x + w/2, y + d/2)，重心高度取该层*累计高度*
#     H_i = Σ_{k=0..i} height_k，重量取 board.weight + wareWeight；
#   * 整架重心 G 为各层质点按重量的加权平均（Gx/Gy/Gz）；
#   * 支撑多边形取底层（i=0）*非悬空*（底端落在窑车台面矩形内）立柱
#     落点的凸包，凸包面积必须非零（至少 3 个不共线落点）；
#   * 水平加速度 (ax, ay)（单位 g）下，重心水平投影平移
#     (ax·Gz, ay·Gz)（惯性力 m·a，等效偏心 a/g·h，a 以 g 计故系数为 1），
#     投影点到凸包边界取*带符号最短距离*（内正外负、边界为 0）：
#       d > 0 稳定（距边界尚有裕量），d ≈ 0 临界（恰在边界上），
#       d < 0 倾覆（投影越出支撑多边形）。
# 任一层无有效承托（没有落在承托面内的非悬空立柱）、落点凸包面积为 0、
# 整架重量为 0 或根本没有层时，明确返回 evaluable=False 与原因，不做判定。
# --------------------------------------------------------------------------
OVERTURN_EPS_MM = 1e-6  # 边界临界判定容差（mm）


def _convex_hull(points: List[Tuple[float, float]]) -> List[Tuple[float, float]]:
    """Andrew 单调链凸包；返回逆时针、剔除共线点的顶点列表（输入坐标不去精度）。"""
    pts = sorted(set(points))
    if len(pts) <= 2:
        return pts

    def cross(o: Tuple[float, float], a: Tuple[float, float],
              b: Tuple[float, float]) -> float:
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower: List[Tuple[float, float]] = []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    upper: List[Tuple[float, float]] = []
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


def _polygon_area2(hull: List[Tuple[float, float]]) -> float:
    """多边形有向面积的 2 倍（鞋带公式）。"""
    s = 0.0
    for i, (x0, y0) in enumerate(hull):
        x1, y1 = hull[(i + 1) % len(hull)]
        s += x0 * y1 - x1 * y0
    return s


def _signed_distance_to_polygon(
    x: float, y: float, hull: List[Tuple[float, float]]
) -> float:
    """点到凸多边形边界的带符号最短距离：内正、外负、边界 0。

    凸包顶点按逆时针排列：点在所有边的左侧（叉积 ≥ 0）即在内侧；
    几何最短距离取到各边*线段*（含端点）的最小距离。
    """
    inside = True
    best = math.inf
    for i, (ax0, ay0) in enumerate(hull):
        bx0, by0 = hull[(i + 1) % len(hull)]
        ex, ey = bx0 - ax0, by0 - ay0
        cross = ex * (y - ay0) - ey * (x - ax0)
        edge_len = math.hypot(ex, ey)
        if cross < -OVERTURN_EPS_MM * max(1.0, edge_len):
            inside = False
        den = ex * ex + ey * ey
        t = ((x - ax0) * ex + (y - ay0) * ey) / den if den > 0 else 0.0
        t = max(0.0, min(1.0, t))
        d = math.hypot(x - (ax0 + t * ex), y - (ay0 + t * ey))
        if d < best:
            best = d
    return best if inside else -best


def _parse_accel_g(ax: Any, ay: Any) -> Tuple[float, float]:
    """把水平加速度输入解析为有限 float（单位 g）；非法值抛 ValueError。"""
    try:
        axv = float(ax)
    except (TypeError, ValueError):
        raise ValueError("水平加速度 x 分量必须是数值（单位 g）")
    try:
        ayv = float(ay)
    except (TypeError, ValueError):
        raise ValueError("水平加速度 y 分量必须是数值（单位 g）")
    if not math.isfinite(axv):
        raise ValueError("水平加速度 x 分量必须是有限数值（单位 g）")
    if not math.isfinite(ayv):
        raise ValueError("水平加速度 y 分量必须是有限数值（单位 g）")
    return axv, ayv


def overturn_check(
    raw_state: Optional[Dict[str, Any]], ax: Any, ay: Any
) -> Dict[str, Any]:
    """层架抗倾覆试算：整架重心 + 底层落点凸包 + 水平加速度等效偏心。

    参数
    ----
    raw_state : 当前架体输入（不被修改、不被持久化）
    ax, ay    : 水平加速度分量（单位 g，可正可负）；非有限数值抛 ValueError

    返回
    ----
    evaluable=True 时给出整架总重、整架重心 cg(x,y,z)、平移量
    shift=(ax·Gz, ay·Gz)、平移后投影 projection、底层落点凸包 polygon、
    投影到支撑边界的带符号最短距离 margin(mm) 与判定
    verdict ∈ {stable, critical, overturn}，以及各层质点明细。
    evaluable=False 时 reason 说明无法评估的原因（无层 / 有层无有效承托 /
    落点不足以形成非零面积凸包 / 整架重量为 0）。
    """
    axv, ayv = _parse_accel_g(ax, ay)

    norm = normalize(raw_state)
    layers = norm["layers"]
    n = len(layers)

    layer_rows: List[Dict[str, Any]] = []
    unsupported: List[Dict[str, Any]] = []
    bottom_points: List[Tuple[float, float, str]] = []
    cum_z = 0.0
    total_weight = 0.0
    sum_x = sum_y = sum_z = 0.0

    for i, L in enumerate(layers):
        rect = _support_rect_for(norm, i)
        valid_posts = [p for p in L["posts"] if in_rect(p["x"], p["y"], rect)]
        if not valid_posts:
            unsupported.append(
                {
                    "index": i,
                    "name": L["name"],
                    "postCount": len(L["posts"]),
                    "validCount": 0,
                }
            )
        if i == 0:
            bottom_points = [(p["x"], p["y"], p["id"]) for p in valid_posts]

        b = L["board"]
        weight = b["weight"] + L["wareWeight"]
        cum_z += L["height"]
        cx = b["x"] + b["w"] / 2.0
        cy = b["y"] + b["d"] / 2.0
        total_weight += weight
        sum_x += weight * cx
        sum_y += weight * cy
        sum_z += weight * cum_z
        layer_rows.append(
            {
                "index": i,
                "name": L["name"],
                "height": r2(L["height"]),
                "z": r2(cum_z),
                "cx": r2(cx),
                "cy": r2(cy),
                "weight": r2(weight),
                "supportedCount": len(valid_posts),
            }
        )

    def fail(reason: str) -> Dict[str, Any]:
        return {
            "evaluable": False,
            "reason": reason,
            "ax": round(axv, 6),
            "ay": round(ayv, 6),
            "totalWeight": r2(total_weight),
            "cg": None,
            "shift": None,
            "projection": None,
            "polygon": None,
            "supportPoints": None,
            "supportPointCount": len(bottom_points),
            "margin": None,
            "verdict": None,
            "unsupportedLayers": unsupported,
            "layers": layer_rows,
        }

    if n == 0:
        return fail("当前架体没有任何层，无法评估抗倾覆稳定性")

    if unsupported:
        names = "、".join(
            f"{u['name']}（录入 {u['postCount']} 根立柱，有效承托 0 根）"
            for u in unsupported
        )
        return fail(
            f"以下 {len(unsupported)} 层无有效承托（立柱均悬空或未录入），"
            f"整架传力不完整，无法评估抗倾覆：{names}"
        )

    # 底层非悬空立柱落点凸包（坐标完全相同的落点去重）
    hull_pts = [(x, y) for x, y, _ in bottom_points]
    unique_n = len(set(hull_pts))
    hull = _convex_hull(hull_pts)
    hull_area = abs(_polygon_area2(hull)) / 2.0
    if len(hull) < 3 or hull_area <= OVERTURN_EPS_MM:
        return fail(
            f"底层非悬空立柱落点 {len(bottom_points)} 个（去重后 {unique_n} 个），"
            "不足以形成非零面积的支撑多边形（至少需要 3 个不共线落点），"
            "无法评估抗倾覆"
        )

    if total_weight <= 0.0:
        return fail(
            "整架棚板自重与坯件重量合计为 0，无法按重量求整架重心，"
            "不能评估抗倾覆"
        )

    gx, gy, gz = sum_x / total_weight, sum_y / total_weight, sum_z / total_weight
    dx, dy = axv * gz, ayv * gz
    px, py = gx + dx, gy + dy
    margin = _signed_distance_to_polygon(px, py, hull)
    if margin > OVERTURN_EPS_MM:
        verdict = "stable"
    elif margin < -OVERTURN_EPS_MM:
        verdict = "overturn"
    else:
        verdict = "critical"

    # 完全重合的落点合并展示（重叠阈值内但非完全重合的点各自保留，凸包不受影响）
    sp_map: Dict[Tuple[float, float], List[str]] = {}
    for x, y, pid in bottom_points:
        sp_map.setdefault((x, y), []).append(pid)
    support_points = [
        {"x": r2(x), "y": r2(y), "posts": ids}
        for (x, y), ids in sorted(sp_map.items())
    ]

    return {
        "evaluable": True,
        "reason": None,
        "ax": round(axv, 6),
        "ay": round(ayv, 6),
        "totalWeight": r2(total_weight),
        "cg": {"x": r2(gx), "y": r2(gy), "z": r2(gz)},
        "shift": {"x": r2(dx), "y": r2(dy)},
        "projection": {"x": r2(px), "y": r2(py)},
        "polygon": [{"x": r2(x), "y": r2(y)} for x, y in hull],
        "supportPoints": support_points,
        "supportPointCount": len(bottom_points),
        "margin": r2(margin),
        "verdict": verdict,
        "unsupportedLayers": [],
        "layers": layer_rows,
    }


# --------------------------------------------------------------------------
# 逐层装货顺序规划（Loading Sequence Planning）
#
# 与抗倾覆试算同一物理模型（各层板+坯为质点、底层落点凸包为支撑多边形），
# 但装货场景改为：
#   * 架体几何不变，全部棚板自重始终保留；
#   * 先把所有层坯件重量置零，再把坯件重量大于零的层（候选层，至多
#     MAX_LOAD_SEQ_LAYERS=12 个）按“整层坯件一次加入”的方式逐层装货；
#   * 对每个已装子集（含坯件全部置零的初始状态）求抗倾覆带符号裕量；
#   * 用位掩码动态规划求使「全过程最小裕量」最大的加入顺序；最优值相同
#     时按层序号字典序（自下而上）选取，结果确定。
#
# 若最优顺序的全过程最小裕量仍非正（<= OVERTURN_EPS_MM），标记
# allStable=False（无法全程稳定）。前置条件不足（无有效承托 / 底层支撑
# 凸包退化 / 初始棚板总重为零 / 无层 / 候选层超过 12 / 没有可装层）时
# 明确返回 planable=False 与原因，不做规划。
# --------------------------------------------------------------------------
MAX_LOAD_SEQ_LAYERS = 12


def loading_sequence(
    raw_state: Optional[Dict[str, Any]], ax: Any, ay: Any
) -> Dict[str, Any]:
    """逐层装货顺序规划：最大化全过程最小抗倾覆裕量的整层装货顺序。

    参数
    ----
    raw_state : 当前架体输入（不被修改、不被持久化）
    ax, ay    : 水平加速度分量（单位 g，可正可负）；非有限数值抛 ValueError

    返回
    ----
    planable=True 时给出候选层清单、初始（坯件全零）裕量/判定、动态规划求得
    的最优顺序 steps（每步新增层、累计坯件重量、总重、重心、投影、带符号
    裕量与稳定判定）、最优顺序（层索引）、全过程最小裕量 minMargin 与
    allStable（每步裕量均 > EPS 才为 True）。
    planable=False 时 reason 说明原因：无层 / 任一层无有效承托 / 凸包退化 /
    初始棚板总重为 0 / 候选层超过 12 / 没有坯件重量大于零的层。
    """
    axv, ayv = _parse_accel_g(ax, ay)

    norm = normalize(raw_state)
    layers = norm["layers"]
    n = len(layers)

    candidate_idx = [i for i, L in enumerate(layers) if L["wareWeight"] > 0.0]
    m = len(candidate_idx)

    # 与 overturn_check 一致的几何/重量前置检查（坯件全部保留时评估承托与凸包；
    # 承托有效性只取决于几何，与重量无关）
    unsupported: List[Dict[str, Any]] = []
    bottom_points: List[Tuple[float, float, str]] = []
    for i, L in enumerate(layers):
        rect = _support_rect_for(norm, i)
        valid_posts = [p for p in L["posts"] if in_rect(p["x"], p["y"], rect)]
        if not valid_posts:
            unsupported.append(
                {
                    "index": i,
                    "name": L["name"],
                    "postCount": len(L["posts"]),
                    "validCount": 0,
                }
            )
        if i == 0:
            bottom_points = [(p["x"], p["y"], p["id"]) for p in valid_posts]

    candidate_rows = [
        {
            "index": i,
            "id": layers[i]["id"],
            "name": layers[i]["name"],
            "wareWeight": r2(layers[i]["wareWeight"]),
        }
        for i in candidate_idx
    ]

    def fail(reason: str) -> Dict[str, Any]:
        return {
            "planable": False,
            "reason": reason,
            "ax": round(axv, 6),
            "ay": round(ayv, 6),
            "candidateCount": m,
            "candidateLimit": MAX_LOAD_SEQ_LAYERS,
            "candidates": candidate_rows,
            "unsupportedLayers": unsupported,
            "polygon": None,
            "order": None,
            "steps": None,
            "initial": None,
            "minMargin": None,
            "allStable": False,
        }

    if n == 0:
        return fail("当前架体没有任何层，无法规划逐层装货顺序")

    if unsupported:
        names = "、".join(
            f"{u['name']}（录入 {u['postCount']} 根立柱，有效承托 0 根）"
            for u in unsupported
        )
        return fail(
            f"以下 {len(unsupported)} 层无有效承托（立柱均悬空或未录入），"
            f"整架传力不完整，无法规划逐层装货顺序：{names}"
        )

    hull = _convex_hull([(x, y) for x, y, _ in bottom_points])
    unique_n = len({(x, y) for x, y, _ in bottom_points})
    hull_area = abs(_polygon_area2(hull)) / 2.0
    if len(hull) < 3 or hull_area <= OVERTURN_EPS_MM:
        return fail(
            f"底层非悬空立柱落点 {len(bottom_points)} 个（去重后 {unique_n} 个），"
            "不足以形成非零面积的支撑多边形（至少需要 3 个不共线落点），"
            "无法规划逐层装货顺序"
        )

    # 初始（坯件全部置零）时必须仍有重量（棚板自重）才能求重心
    board_weight_total = sum(L["board"]["weight"] for L in layers)
    if board_weight_total <= 0.0:
        return fail(
            "初始棚板总重为 0：坯件全部置零后整架没有重量，无法求重心，"
            "不能规划逐层装货顺序（请先录入棚板自重）"
        )

    if m > MAX_LOAD_SEQ_LAYERS:
        return fail(
            f"共有 {m} 个坯件重量大于零的层，超过逐层装货顺序规划上限 "
            f"{MAX_LOAD_SEQ_LAYERS} 层，请缩减规划范围（将部分层坯件重量置零）"
            "后重试"
        )

    if m == 0:
        return fail(
            "当前没有坯件重量大于零的层，所有层均已处于无坯件状态，"
            "无需规划装货顺序"
        )

    # ---- 各层质点常量（与 overturn_check 完全一致） ----
    z_at = [0.0] * n           # 累计高度 H_i
    cx_at = [0.0] * n          # 棚板中心 x
    cy_at = [0.0] * n          # 棚板中心 y
    bw_at = [0.0] * n          # 棚板自重
    ww_at = [0.0] * n          # 坯件重量
    cum_z = 0.0
    for i, L in enumerate(layers):
        b = L["board"]
        cum_z += L["height"]
        z_at[i] = cum_z
        cx_at[i] = b["x"] + b["w"] / 2.0
        cy_at[i] = b["y"] + b["d"] / 2.0
        bw_at[i] = b["weight"]
        ww_at[i] = L["wareWeight"]

    # 坯件全零时的一次矩常量（所有子集共享）
    base_w = board_weight_total
    base_sx = sum(bw_at[i] * cx_at[i] for i in range(n))
    base_sy = sum(bw_at[i] * cy_at[i] for i in range(n))
    base_sz = sum(bw_at[i] * z_at[i] for i in range(n))

    # 每个候选层坯件加入后对一次矩与重量的增量
    dW = [ww_at[i] for i in candidate_idx]
    dSX = [ww_at[i] * cx_at[i] for i in candidate_idx]
    dSY = [ww_at[i] * cy_at[i] for i in candidate_idx]
    dSZ = [ww_at[i] * z_at[i] for i in candidate_idx]

    def subset_margin(mask: int) -> Tuple[float, float, float, float, float, float]:
        """已装候选层集合 mask 下的（带符号裕量, 总重, Gx, Gy, Gz, 累计坯件重）。

        各量按固定顺序（候选层序号升序）累加，保证同一子集结果逐位确定。
        """
        tw = base_w
        sx, sy, sz = base_sx, base_sy, base_sz
        ware_sum = 0.0
        for k in range(m):
            if mask & (1 << k):
                tw += dW[k]
                sx += dSX[k]
                sy += dSY[k]
                sz += dSZ[k]
                ware_sum += dW[k]
        gx, gy, gz = sx / tw, sy / tw, sz / tw
        px, py = gx + axv * gz, gy + ayv * gz
        margin = _signed_distance_to_polygon(px, py, hull)
        return margin, tw, gx, gy, gz, ware_sum

    # 每个子集只算一次（DP 中同一掩码会被多个前驱访问；至多 2^12=4096 个子集）
    margin_cache: List[Tuple[float, float, float, float, float, float]] = [
        (0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    ] * (1 << m)
    cache_done = [False] * (1 << m)

    def cached_subset(mask: int):
        if not cache_done[mask]:
            margin_cache[mask] = subset_margin(mask)
            cache_done[mask] = True
        return margin_cache[mask]

    def verdict_of(margin: float) -> str:
        if margin > OVERTURN_EPS_MM:
            return "stable"
        if margin < -OVERTURN_EPS_MM:
            return "overturn"
        return "critical"

    # ---- 位掩码动态规划：最大化全过程（含初始状态）最小裕量 ----
    # best[mask] = (minMargin, order_tuple)：从初始状态装成 mask 这一过程中
    # 初始裕量与各步裕量的最小值；同分按层序号字典序（层索引元组字典序）。
    # 掩码按数值递增枚举即保证子集先于超集被处理。
    init_margin = cached_subset(0)[0]
    best: Dict[int, Tuple[float, Tuple[int, ...]]] = {0: (init_margin, ())}
    for mask in range(1, 1 << m):
        best_val = -math.inf
        best_order: Optional[Tuple[int, ...]] = None
        margin = cached_subset(mask)[0]
        bits = mask
        while bits:
            lb = bits & -bits
            k = lb.bit_length() - 1
            prev = mask ^ lb
            prev_val, prev_order = best[prev]
            val = min(prev_val, margin)
            order = prev_order + (candidate_idx[k],)
            if val > best_val or (val == best_val and
                                 (best_order is None or order < best_order)):
                best_val = val
                best_order = order
            bits ^= lb
        best[mask] = (best_val, best_order)  # type: ignore[assignment]

    full_mask = (1 << m) - 1
    min_margin, order_idx = best[full_mask]

    # ---- 逐步明细（初始坯件全零 + 每加入一层） ----
    def step_row(
        step_no: int,
        added: Optional[int],
        margin: float,
        tw: float,
        gx: float,
        gy: float,
        gz: float,
        ware_sum: float,
    ) -> Dict[str, Any]:
        v = verdict_of(margin)
        return {
            "step": step_no,
            "addedIndex": added,
            "addedId": layers[added]["id"] if added is not None else None,
            "addedName": layers[added]["name"] if added is not None else None,
            "addedWareWeight": r2(ww_at[added]) if added is not None else 0.0,
            "cumulativeWareWeight": r2(ware_sum),
            "totalWeight": r2(tw),
            "cg": {"x": r2(gx), "y": r2(gy), "z": r2(gz)},
            "projection": {
                "x": r2(gx + axv * gz),
                "y": r2(gy + ayv * gz),
            },
            "margin": r2(margin),
            "verdict": v,
            "stable": v == "stable",
        }

    m0, tw0, gx0, gy0, gz0, _ = cached_subset(0)
    steps = [step_row(0, None, m0, tw0, gx0, gy0, gz0, 0.0)]
    mask = 0
    for step_no, li in enumerate(order_idx, start=1):
        k = candidate_idx.index(li)
        mask |= 1 << k
        mv, tw, gx, gy, gz, ware_sum = cached_subset(mask)
        steps.append(step_row(step_no, li, mv, tw, gx, gy, gz, ware_sum))

    all_stable = all(s["stable"] for s in steps)

    sp_map: Dict[Tuple[float, float], List[str]] = {}
    for x, y, pid in bottom_points:
        sp_map.setdefault((x, y), []).append(pid)
    support_points = [
        {"x": r2(x), "y": r2(y), "posts": ids}
        for (x, y), ids in sorted(sp_map.items())
    ]

    return {
        "planable": True,
        "reason": None,
        "ax": round(axv, 6),
        "ay": round(ayv, 6),
        "candidateCount": m,
        "candidateLimit": MAX_LOAD_SEQ_LAYERS,
        "candidates": candidate_rows,
        "unsupportedLayers": [],
        "polygon": [{"x": r2(x), "y": r2(y)} for x, y in hull],
        "supportPoints": support_points,
        "supportPointCount": len(bottom_points),
        "order": list(order_idx),
        "steps": steps,
        "initial": steps[0],
        "minMargin": r2(min_margin),
        "minMarginStep": min(
            steps, key=lambda s: (s["margin"], s["step"])
        )["step"],
        "allStable": all_stable,
    }


# --------------------------------------------------------------------------
# 连续窑门通行试算（Kiln Door Passage Trial）
#
# 架体（窑车）载着层架沿直线依次通过若干道窑门，每道门只允许在进入该门之前
# 的一段行程内做不超过 maxShift mm 的横向平移（横移量）：
#   * 横向外廓只取*窑车台面与各层棚板*的 X 向跨度：台面 z=0 上 [0, car.w]；
#     第 i 层棚板位于累计高度 H_i=Σ_{k≤i} height_k，横向 [board.x, board.x+w]。
#   * 架体最高点 totalHeight = Σ height（累加层高），与棚板自重/坯件无关。
#   * 第 j 道门开口横向 [c_j-w_j/2, c_j+w_j/2]、净高 h_j。
#     - h_j < totalHeight：高度不足，直接判该门阻断（不再给横移区间）；
#     - 否则本门可通过的横移闭区间 = 所有外廓件区间之交：
#         s ∈ [max_k(openL-left_k), min_k(openR-right_k)]，
#       该区间自身为空即单门宽度不足（即使别的门都能过也阻断）。
#   * 可达性按门序递推：首门前位置为 {0}；进入第 j 道门前把上一道门后的
#     可达闭区间按本段 maxShift 扩张 [lo-m, hi+m]，再与本门可通过区间求交；
#     首次交集为空即在该门阻断（"各门单独能过但序列不可达"同样判阻断）。
#   * 全程可达时回溯一条确定的位置序列：末门取可达区间中最接近 0 的横移，
#     自后向前把上一道门位置取为可达区间内最接近下一门位置的值（clamp），
#     逐门给出实际横移与左/右/顶部净空。
# 整个过程只读、不修改输入、不写盘。
# --------------------------------------------------------------------------
DOOR_PASSAGE_EPS = 1e-9  # 闭区间接触（净空 0）即可通过的判定容差（mm）


def _parse_door_params(raw_doors: Any) -> List[Dict[str, float]]:
    """把窑门录入解析为有序列表；非数值/越界值抛 ValueError（API → 400）。"""
    if not isinstance(raw_doors, list):
        raise ValueError("窑门参数 doors 必须是数组")
    fields = (
        ("center", "开口横向中心"),
        ("width", "开口宽度"),
        ("height", "开口高度"),
        ("maxShift", "本段最大横移量"),
    )
    out: List[Dict[str, float]] = []
    for i, d in enumerate(raw_doors):
        label = f"第 {i + 1} 道门"
        if not isinstance(d, dict):
            raise ValueError(f"{label}的参数必须是对象")
        vals: Dict[str, float] = {}
        for key, name in fields:
            v = d.get(key)
            try:
                fv = float(v)
            except (TypeError, ValueError):
                raise ValueError(f"{label}{name}必须是数值（mm）")
            if not math.isfinite(fv):
                raise ValueError(f"{label}{name}必须是有限数值（mm）")
            vals[key] = fv
        if vals["width"] <= 0:
            raise ValueError(f"{label}开口宽度必须大于 0")
        if vals["height"] <= 0:
            raise ValueError(f"{label}开口高度必须大于 0")
        if vals["maxShift"] < 0:
            raise ValueError(f"{label}本段最大横移量不能为负")
        out.append({"index": i, **vals})
    return out


def _rack_profile(norm: Dict[str, Any]) -> Tuple[float, float, float,
                                                 List[Dict[str, Any]]]:
    """求架体横向外廓与最高点：返回 (总高, 外廓左, 外廓右, 外廓件列表)。

    外廓件只含窑车台面与各层棚板（X 向跨度为正的才计入）；立柱不参与。
    """
    car = norm["car"]
    slabs: List[Dict[str, Any]] = []
    if car["w"] > 0:
        slabs.append(
            {
                "kind": "car",
                "name": car["name"],
                "z": 0.0,
                "left": 0.0,
                "right": car["w"],
                "width": car["w"],
            }
        )
    cum_z = 0.0
    for L in norm["layers"]:
        cum_z += L["height"]
        b = L["board"]
        if b["w"] > 0:
            slabs.append(
                {
                    "kind": "board",
                    "name": L["name"],
                    "z": cum_z,
                    "left": b["x"],
                    "right": b["x"] + b["w"],
                    "width": b["w"],
                }
            )
    if slabs:
        env_left = min(s["left"] for s in slabs)
        env_right = max(s["right"] for s in slabs)
    else:
        env_left = env_right = 0.0
    return cum_z, env_left, env_right, slabs


def _iv_json(lo: float, hi: float) -> Optional[Dict[str, Optional[float]]]:
    """闭区间输出：±∞ 端记为 null（无界）。"""
    return {
        "lo": None if lo == -math.inf else r2(lo),
        "hi": None if hi == math.inf else r2(hi),
    }


def door_passage(
    raw_state: Optional[Dict[str, Any]], raw_doors: Any
) -> Dict[str, Any]:
    """连续窑门通行试算：按门序做横移可达闭区间递推并回溯一条位置序列。

    参数
    ----
    raw_state : 当前架体输入（不被修改、不被持久化）
    raw_doors : 按行进顺序排列的窑门参数列表，每项
                {"center","width","height","maxShift"}（单位 mm）；
                非法值抛 ValueError（Flask 层转 400）。

    返回
    ----
    evaluable=False（无层）时给出 reason；否则给出架体外廓 profile、逐门
    判定（本门区间/扩张区间/可达区间/阻断原因与冲突量），passable 为真时
    附回溯的 positions（每门选定横移、实际横移与左/右/顶部净空）。
    """
    doors_in = _parse_door_params(raw_doors)
    norm = normalize(raw_state)
    n = len(norm["layers"])
    total_h, env_l, env_r, slabs = _rack_profile(norm)

    profile = {
        "totalHeight": r2(total_h),
        "envelope": {
            "left": r2(env_l),
            "right": r2(env_r),
            "width": r2(env_r - env_l),
        },
        "slabs": [
            {
                "kind": s["kind"],
                "name": s["name"],
                "z": r2(s["z"]),
                "left": r2(s["left"]),
                "right": r2(s["right"]),
                "width": r2(s["width"]),
            }
            for s in slabs
        ],
    }

    def echo(d: Dict[str, float]) -> Dict[str, Any]:
        return {
            "index": d["index"],
            "center": r2(d["center"]),
            "width": r2(d["width"]),
            "height": r2(d["height"]),
            "maxShift": r2(d["maxShift"]),
            "opening": {
                "left": r2(d["center"] - d["width"] / 2.0),
                "right": r2(d["center"] + d["width"] / 2.0),
            },
        }

    if n == 0:
        return {
            "evaluable": False,
            "reason": "当前架体没有任何层，无法确定横向外廓与累计层高，不能进行窑门通行试算",
            "profile": profile,
            "doorCount": len(doors_in),
            "passable": False,
            "blockedAt": None,
            "blockReason": None,
            "doors": [echo(d) for d in doors_in],
            "positions": None,
            "note": None,
        }

    m = len(doors_in)
    rows: List[Dict[str, Any]] = []
    reach_ivs: List[Tuple[float, float]] = []  # 各门可达区间（精确浮点，供回溯）
    prev_lo = prev_hi = 0.0  # 首门之前的位置集合 {0}
    blocked_at: Optional[int] = None
    block_reason: Optional[str] = None

    for d in doors_in:
        i = d["index"]
        open_l = d["center"] - d["width"] / 2.0
        open_r = d["center"] + d["width"] / 2.0
        row: Dict[str, Any] = {
            **echo(d),
            "evaluated": True,
            "heightOk": True,
            "interval": None,
            "intervalEmpty": False,
            "reachExpanded": None,
            "reachable": None,
            "passable": False,
            "blockReason": None,
            "conflict": None,
            "chosenShift": None,
            "move": None,
            "clearance": None,
        }
        rows.append(row)

        if blocked_at is not None:
            # 阻断门之后不再评估（递推已无法继续）
            row["evaluated"] = False
            continue

        # 进入本门前，把上一可达区间按本段最大横移量扩张
        ex_lo = prev_lo - d["maxShift"]
        ex_hi = prev_hi + d["maxShift"]
        row["reachExpanded"] = _iv_json(ex_lo, ex_hi)

        # 高度不足：直接阻断，不再给可通过横移区间
        if d["height"] + DOOR_PASSAGE_EPS < total_h:
            blocked_at = i
            block_reason = "height"
            row["heightOk"] = False
            row["blockReason"] = "height"
            row["conflict"] = {
                "doorHeight": r2(d["height"]),
                "totalHeight": r2(total_h),
                "shortage": r2(total_h - d["height"]),
            }
            reach_ivs.append((math.inf, -math.inf))  # 空区间占位，回溯不会用到
            continue

        # 本门可通过横移闭区间：所有外廓件平移后必须落在开口内
        lo = max((open_l - s["left"] for s in slabs), default=-math.inf)
        hi = min((open_r - s["right"] for s in slabs), default=math.inf)
        row["interval"] = _iv_json(lo, hi)
        interval_empty = lo > hi + DOOR_PASSAGE_EPS

        if interval_empty:
            # 单门宽度不足（本门区间自身为空）
            blocked_at = i
            block_reason = "width"
            row["intervalEmpty"] = True
            row["blockReason"] = "width"
            gap = lo - hi
            row["conflict"] = {
                "doorWidth": r2(d["width"]),
                "envelopeWidth": r2(env_r - env_l),
                "shortage": r2(gap),
                "interval": _iv_json(lo, hi),
            }
            reach_ivs.append((math.inf, -math.inf))  # 空区间占位，回溯不会用到
            continue

        reach_lo = max(ex_lo, lo)
        reach_hi = min(ex_hi, hi)
        if reach_lo > reach_hi + DOOR_PASSAGE_EPS:
            # 本门单独能过，但按本段横移量扩张后的可达区间与本门区间无交集
            blocked_at = i
            block_reason = "reach"
            row["blockReason"] = "reach"
            if lo > ex_hi:
                # 本门区间整体在扩张区间右侧：需要继续向右，但本段横移量不够
                side = "right"
            else:
                # 本门区间整体在扩张区间左侧：需要继续向左，但本段横移量不够
                side = "left"
            gap = reach_lo - reach_hi
            row["conflict"] = {
                "side": side,
                "shortage": r2(gap),
                "reachExpanded": _iv_json(ex_lo, ex_hi),
                "doorInterval": _iv_json(lo, hi),
            }
            reach_ivs.append((math.inf, -math.inf))  # 空区间占位，回溯不会用到
            continue

        row["reachable"] = _iv_json(reach_lo, reach_hi)
        row["passable"] = True
        prev_lo, prev_hi = reach_lo, reach_hi
        reach_ivs.append((reach_lo, reach_hi))

    # ---- 回溯一条满足各段横移限制的位置序列 ----
    positions: Optional[List[Dict[str, Any]]] = None
    if blocked_at is None and m > 0:
        def clamp(v: float, lo: float, hi: float) -> float:
            return min(max(v, lo), hi)

        chosen: List[float] = [0.0] * m
        cur = clamp(0.0, *reach_ivs[m - 1])
        chosen[m - 1] = cur
        for k in range(m - 2, -1, -1):
            cur = clamp(cur, *reach_ivs[k])
            chosen[k] = cur

        positions = []
        prev_shift = 0.0  # 首门从横移 0 出发
        for k, row in enumerate(rows):
            shift = chosen[k]
            move = shift - prev_shift
            assert abs(move) <= doors_in[k]["maxShift"] + 1e-7
            rlo, rhi = reach_ivs[k]
            assert rlo - 1e-7 <= shift <= rhi + 1e-7
            dd = doors_in[k]
            open_lo = dd["center"] - dd["width"] / 2.0
            open_hi = dd["center"] + dd["width"] / 2.0
            row["chosenShift"] = r2(shift)
            row["move"] = r2(move)
            row["clearance"] = {
                "left": r2(max(0.0, (env_l + shift) - open_lo)),
                "right": r2(max(0.0, open_hi - (env_r + shift))),
                "top": r2(max(0.0, dd["height"] - total_h)),
            }
            positions.append(
                {
                    "door": k,
                    "shift": r2(shift),
                    "move": r2(move),
                    "maxShift": r2(doors_in[k]["maxShift"]),
                    "clearance": row["clearance"],
                }
            )
            prev_shift = shift

    note = None
    if m == 0:
        note = "未录入窑门，按全程可达处理（没有需要通过的门）"

    return {
        "evaluable": True,
        "reason": None,
        "profile": profile,
        "doorCount": m,
        "passable": blocked_at is None,
        "blockedAt": blocked_at,
        "blockReason": block_reason,
        "doors": rows,
        "positions": positions,
        "note": note,
    }


# --------------------------------------------------------------------------
# 轮组承载试算（Wheel Group Load Trial）
#
# 台面由矩形布置的四个车轮承托（刚性台面、四点刚体反力模型）：
#   * 轮位由台面内的两条 X 坐标线（左轮 xL、右轮 xR，xL < xR）与两条 Y
#     坐标线（前轮 yF、后轮 yR，yF < yR）张成轮组矩形；
#   * 四轮位置：左前 FL=(xL,yF)、右前 FR=(xR,yF)、左后 RL=(xL,yR)、
#     右后 RR=(xR,yR)，各自有独立额定载荷；
#   * 台面受到的点载荷取*底层非悬空立柱*的落点与实载（悬空柱不承载，
#     容量为 0 只表示立柱不限容量，不影响其是否参与）。
#
# 单个点载荷 P 落在 (x, y) 时，按双向杠杆（双线性）比例分到四轮：
#   X 向：wL=(xR-x)/(xR-xL) 给左轮列、wR=(x-xL)/(xR-xL) 给右轮列；
#   Y 向：wF=(yR-y)/(yR-yF) 给前轮行、wB=(y-yF)/(yR-yF) 给后轮行。
#   即 R_FL=P·wL·wF、R_FR=P·wR·wF、R_RL=P·wL·wB、R_RR=P·wR·wB，
# 该组系数对平面任意位置恒和为 1，故点载荷越出轮组矩形时只是某个系数
# 变负（杠杆外推），**绝不能把负反力截为零**——否则竖向力平衡与绕 X/Y
# 轴力矩平衡都会被破坏。
#
# 输出核对（反力合计 == 点载荷合计；绕前轮轴 X 力矩、绕左轮轴 Y 力矩
# 分别与点载荷一次矩一致），并逐轮给出利用率、超载与负反力风险。
# 轮距不正（坐标相等/顺序颠倒）、轮位越出台面矩形、额定载荷非正，
# 或没有底层 / 底层没有正载荷时，明确报参数错误或无法试算。
# 整个过程只读、不修改输入、不写盘。
# --------------------------------------------------------------------------
WHEEL_TRIAL_EPS = 1e-9  # 正载荷判定容差（kg）


def _parse_wheel_coord(value: Any, name: str) -> float:
    """解析轮位坐标（mm）；非有限数值抛 ValueError（API → 400）。"""
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name}必须是数值（mm）")
    if not math.isfinite(v):
        raise ValueError(f"{name}必须是有限数值（mm）")
    return v


def _parse_wheel_params(raw: Dict[str, Any]) -> Tuple[float, float, float, float,
                                                      Dict[str, float]]:
    """解析轮组输入：xLeft/xRight/yFront/yRear 与四轮额定载荷。

    额定载荷支持两种形式（二选一，xLeft 等扁平字段优先）：
      扁平：ratedFL / ratedFR / ratedRL / ratedRR（缺省键按 None→400）；
      分组：wheels={"FL":{rating},"FR":...,"RL":...,"RR":...}。
    坐标既可用 xLeft/xRight/yFront/yRear，也可放在 wheels 各项的 x/y。
    """
    wheels_in = raw.get("wheels")
    wheel_map: Dict[str, Dict[str, Any]] = {}
    if isinstance(wheels_in, dict):
        for key in ("FL", "FR", "RL", "RR"):
            w = wheels_in.get(key)
            if isinstance(w, dict):
                wheel_map[key] = w
    elif wheels_in is not None:
        raise ValueError("轮组参数 wheels 必须是对象（含 FL/FR/RL/RR 四轮）")

    def coord(field: str, wx_key: str, name: str) -> float:
        if raw.get(field) is not None:
            return _parse_wheel_coord(raw.get(field), name)
        if wx_key and wheel_map and wx_key in wheel_map:
            return _parse_wheel_coord(wheel_map[wx_key].get("x" if field[0] == "x" else "y"), name)
        raise ValueError(f"缺少{name}参数")

    x_left = coord("xLeft", "FL", "左轮位 X 坐标 xLeft")
    x_right = coord("xRight", "FR", "右轮位 X 坐标 xRight")
    y_front = coord("yFront", "FL", "前轮位 Y 坐标 yFront")
    y_rear = coord("yRear", "RL", "后轮位 Y 坐标 yRear")

    def rating(key: str, label: str) -> float:
        if raw.get(key) is not None:
            v = raw.get(key)
        elif key[5:] in wheel_map:
            v = wheel_map[key[5:]].get("rating")
        else:
            raise ValueError(f"缺少{label}额定载荷参数 {key}")
        try:
            fv = float(v)
        except (TypeError, ValueError):
            raise ValueError(f"{label}额定载荷必须是数值（kg）")
        if not math.isfinite(fv):
            raise ValueError(f"{label}额定载荷必须是有限数值（kg）")
        if fv <= 0:
            raise ValueError(f"{label}额定载荷必须大于 0（收到 {r2(fv)} kg）")
        return fv

    ratings = {
        "FL": rating("ratedFL", "左前轮"),
        "FR": rating("ratedFR", "右前轮"),
        "RL": rating("ratedRL", "左后轮"),
        "RR": rating("ratedRR", "右后轮"),
    }
    return x_left, x_right, y_front, y_rear, ratings


def wheel_load_trial(raw_state: Optional[Dict[str, Any]],
                     params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """轮组承载试算：底层非悬空立柱点载荷按矩形四轮双向杠杆比例分配反力。

    参数
    ----
    raw_state : 当前架体输入（不被修改、不被持久化）
    params    : 轮组参数，支持
                {"xLeft","xRight","yFront","yRear",
                 "ratedFL","ratedFR","ratedRL","ratedRR"}
                或 {"wheels": {"FL": {"x","y","rating"}, ...}}；
                轮距不正 / 轮位越出台面 / 额定载荷非正抛 ValueError（API → 400）。

    返回
    ----
    evaluable=True 时给出四轮位置与逐轮反力、利用率、超载/负反力标记，
    底层非悬空立柱点载荷明细（含外推/重叠标记），以及力平衡与绕 X/Y 轴
    力矩平衡核对（反力侧 − 点载荷侧残差）。evaluable=False 时 reason
    说明无法试算的原因（没有底层 / 底层没有正载荷）。
    """
    params = params if isinstance(params, dict) else {}
    x_left, x_right, y_front, y_rear, ratings = _parse_wheel_params(params)
    norm = normalize(raw_state)
    car = norm["car"]

    base = {
        "xLeft": r2(x_left), "xRight": r2(x_right),
        "yFront": r2(y_front), "yRear": r2(y_rear),
        "trackX": r2(x_right - x_left),
        "wheelbaseY": r2(y_rear - y_front),
        "ratings": {k: r2(v) for k, v in ratings.items()},
        "car": {"name": car["name"], "w": r2(car["w"]), "d": r2(car["d"])},
    }

    def fail(reason: str) -> Dict[str, Any]:
        return {
            "evaluable": False,
            "reason": reason,
            **base,
            "totalLoad": 0.0,
            "pointLoads": [],
            "pointLoadCount": 0,
            "overhangCount": 0,
            "wheels": None,
            "checks": None,
            "risks": [],
        }

    # ---- 轮距与台面范围校验 ----
    if x_right <= x_left:
        raise ValueError(
            f"左右轮位 X 坐标必须严格满足 xLeft < xRight：收到 xLeft={r2(x_left)}、"
            f"xRight={r2(x_right)}（轮距 {r2(x_right - x_left)} mm），轮距不正"
        )
    if y_rear <= y_front:
        raise ValueError(
            f"前后轮位 Y 坐标必须严格满足 yFront < yRear：收到 yFront={r2(y_front)}、"
            f"yRear={r2(y_rear)}（轴距 {r2(y_rear - y_front)} mm），轮距不正"
        )
    car_rect = {"x": 0.0, "y": 0.0, "w": car["w"], "d": car["d"]}
    for name, x, y in (
        ("左前轮 FL", x_left, y_front),
        ("右前轮 FR", x_right, y_front),
        ("左后轮 RL", x_left, y_rear),
        ("右后轮 RR", x_right, y_rear),
    ):
        if not in_rect(x, y, car_rect):
            raise ValueError(
                f"{name}轮位（{r2(x)}, {r2(y)}）越出{car['name']}台面矩形"
                f"（0~{r2(car['w'])} × 0~{r2(car['d'])} mm）"
            )

    layers = norm["layers"]
    if not layers:
        return fail("当前架体没有任何层，不存在底层立柱点载荷，无法进行轮组承载试算")
    bottom = layers[0]
    bottom_name = bottom["name"]

    # ---- 底层非悬空立柱落点与实载（经整架计算得到，保证与页面结果一致）----
    bottom_result = _calculate(norm)["layers"][0]
    posts_out = bottom_result["posts"]
    point_loads: List[Dict[str, Any]] = []
    groups: Dict[Tuple[float, float], List[Dict[str, Any]]] = {}
    for po in posts_out:
        if po["hanging"]:
            continue
        x, y, load = float(po["x"]), float(po["y"]), float(po["load"])
        groups.setdefault((x, y), []).append(po)
        point_loads.append(
            {
                "post": po["id"],
                "x": r2(x),
                "y": r2(y),
                "load": r2(load),
                "overlap": bool(po["overlap"]),
                "overlapWith": list(po.get("overlapWith") or []),
                "capacity": po["capacity"],
                "outsideWheelRect": not (
                    x_left <= x <= x_right and y_front <= y <= y_rear
                ),
            }
        )

    total_load = sum(p["load"] for p in point_loads)
    overhang_n = sum(1 for p in point_loads if p["outsideWheelRect"])
    base.update(
        totalLoad=r2(total_load),
        pointLoads=point_loads,
        pointLoadCount=len(point_loads),
        overhangCount=overhang_n,
    )
    if total_load <= WHEEL_TRIAL_EPS:
        return fail(
            f"{bottom_name}非悬空立柱的实载合计为 0（无正载荷），台面没有需要四个车轮"
            "承担的载荷，无法进行轮组承载试算；请先在架体录入棚板自重或坯件重量"
        )

    # ---- 双向杠杆（双线性）反力分配：允许外推，不截断负反力 ----
    dx = x_right - x_left
    dy = y_rear - y_front
    react = {"FL": 0.0, "FR": 0.0, "RL": 0.0, "RR": 0.0}
    # 完全相同坐标的落点合并为一个点载荷参与分配；TOL_MM 内但不重合的点
    # 各自仍是独立点载荷（与点载荷明细一一对应）。
    merged: List[Tuple[float, float, float]] = []
    for (x, y), members in groups.items():
        merged.append((x, y, sum(float(m["load"]) for m in members)))
    # 点载荷侧一次矩（取前轮轴 y=yFront、左轮轴 x=xLeft 为矩轴）
    moment_x_loads = sum(P * (y - y_front) for x, y, P in merged)  # 绕 X（前轮轴）
    moment_y_loads = sum(P * (x - x_left) for x, y, P in merged)  # 绕 Y（左轮轴）
    for x, y, P in merged:
        wl = (x_right - x) / dx
        wr = (x - x_left) / dx
        wf = (y_rear - y) / dy
        wb = (y - y_front) / dy
        react["FL"] += P * wl * wf
        react["FR"] += P * wr * wf
        react["RL"] += P * wl * wb
        react["RR"] += P * wr * wb

    wheel_pos = {
        "FL": (x_left, y_front),
        "FR": (x_right, y_front),
        "RL": (x_left, y_rear),
        "RR": (x_right, y_rear),
    }
    wheel_labels = {
        "FL": "左前轮", "FR": "右前轮", "RL": "左后轮", "RR": "右后轮",
    }
    wheels: List[Dict[str, Any]] = []
    risks: List[Dict[str, Any]] = []
    for key in ("FL", "FR", "RL", "RR"):
        wx, wy = wheel_pos[key]
        r = react[key]
        rating = ratings[key]
        overloaded = r > rating + 1e-9
        negative = r < -WHEEL_TRIAL_EPS
        util = r / rating if rating > 0 else None
        wheels.append(
            {
                "key": key,
                "name": wheel_labels[key],
                "x": r2(wx),
                "y": r2(wy),
                "reaction": r2(r),
                "rating": r2(rating),
                "utilization": round(util, 4) if util is not None else None,
                "overloaded": overloaded,
                "negative": negative,
            }
        )
        if overloaded:
            risks.append(
                {
                    "level": "error",
                    "type": "wheel-overload",
                    "wheel": key,
                    "message": (
                        f"{wheel_labels[key]} {key}（{r2(wx)}, {r2(wy)}）超额受力："
                        f"反力 {r2(r)} kg > 额定载荷 {r2(rating)} kg，"
                        f"利用率 {round(util * 100, 1)}%"
                    ),
                }
            )
        if negative:
            risks.append(
                {
                    "level": "error",
                    "type": "negative-reaction",
                    "wheel": key,
                    "message": (
                        f"{wheel_labels[key]} {key}（{r2(wx)}, {r2(wy)}）出现负反力 "
                        f"{r2(r)} kg：点载荷合力相对四轮矩形外偏，该轮需要向下"
                        "拉住台面才能平衡（刚体杠杆外推结果，未截断为零），实际"
                        "车轮只能承压，存在台面上翘/脱轮风险"
                    ),
                }
            )

    if overhang_n:
        risks.append(
            {
                "level": "warn",
                "type": "extrapolation",
                "wheel": None,
                "message": (
                    f"{bottom_name}有 {overhang_n} 个非悬空立柱落点越出四轮矩形"
                    "（已按杠杆比例外推分配，负反力不截零）；相关车轮可能出现负反力，"
                    "请结合上方逐轮结果核对"
                ),
            }
        )

    # ---- 平衡核对：反力侧 − 点载荷侧（大数量级用相对容差，避免二进制尾差误报）----
    reaction_sum = sum(react.values())
    moment_x_react = (
        (react["RL"] + react["RR"]) * dy      # 绕 X（前轮轴 y=yFront）
    )
    moment_y_react = (
        (react["FR"] + react["RR"]) * dx      # 绕 Y（左轮轴 x=xLeft）
    )

    def _balanced(a: float, b: float) -> bool:
        return abs(a - b) <= 1e-7 * max(1.0, abs(a), abs(b))

    checks = {
        "force": {
            "reactionSum": r2(reaction_sum),
            "loadSum": r2(total_load),
            "residual": r2(reaction_sum - total_load),
            "ok": _balanced(reaction_sum, total_load),
        },
        "momentX": {
            # 绕 X 轴（沿左右方向、过前轮 y=yFront）：力臂为 y−yFront
            "axis": "过前轮 y=yFront 的 X 向轴",
            "reactionMoment": r2(moment_x_react),
            "loadMoment": r2(moment_x_loads),
            "residual": r2(moment_x_react - moment_x_loads),
            "ok": _balanced(moment_x_react, moment_x_loads),
        },
        "momentY": {
            # 绕 Y 轴（沿前后方向、过左轮 x=xLeft）：力臂为 x−xLeft
            "axis": "过左轮 x=xLeft 的 Y 向轴",
            "reactionMoment": r2(moment_y_react),
            "loadMoment": r2(moment_y_loads),
            "residual": r2(moment_y_react - moment_y_loads),
            "ok": _balanced(moment_y_react, moment_y_loads),
        },
        "allOk": False,
    }
    checks["allOk"] = (
        checks["force"]["ok"] and checks["momentX"]["ok"] and checks["momentY"]["ok"]
    )

    return {
        "evaluable": True,
        "reason": None,
        **base,
        "wheels": wheels,
        "checks": checks,
        "risks": risks,
    }
