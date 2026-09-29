# -*- coding: utf-8 -*-
"""核心计算引擎单元测试：python -m pytest -q 或 python tests/test_calc.py"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.calc import (  # noqa: E402
    calculate,
    door_passage,
    load_shed,
    loading_sequence,
    normalize,
    overturn_check,
    simulate,
    suggest_posts,
    wheel_load_trial,
)

SAMPLE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data",
    "sample_state.json",
)


class CalcTests(unittest.TestCase):
    def test_simple_two_layers_even_share(self):
        state = {
            "car": {"w": 1000, "d": 1000},
            "layers": [
                {
                    "name": "底",
                    "height": 300,
                    "board": {"x": 0, "y": 0, "w": 1000, "d": 1000, "weight": 100},
                    "wareWeight": 100,
                    "posts": [
                        {"id": "A", "x": 100, "y": 100, "capacity": 0},
                        {"id": "B", "x": 900, "y": 900, "capacity": 0},
                    ],
                },
                {
                    "name": "顶",
                    "height": 300,
                    "board": {"x": 100, "y": 100, "w": 800, "d": 800, "weight": 80},
                    "wareWeight": 120,
                    "posts": [
                        {"id": "C", "x": 200, "y": 200, "capacity": 0},
                        {"id": "D", "x": 800, "y": 800, "capacity": 0},
                    ],
                },
            ],
        }
        r = calculate(state)
        bottom, top = r["layers"]
        # 顶层 (80+120)/2 = 100
        self.assertEqual([p["load"] for p in top["posts"]], [100.0, 100.0])
        # 底层 100+100 + 上层点载荷 200 = 400, /2 = 200
        self.assertEqual(bottom["loadIncoming"], 200.0)
        self.assertEqual(bottom["loadTotal"], 400.0)
        self.assertEqual([p["load"] for p in bottom["posts"]], [200.0, 200.0])
        self.assertEqual(r["totals"]["lostLoad"], 0.0)
        self.assertEqual(r["totals"]["carriedAtBottom"], 400.0)
        # 载荷来源链
        labels = [s["label"] for s in bottom["posts"][0]["sources"]]
        self.assertIn("顶·C", labels)
        self.assertEqual(sum(s["amount"] for s in bottom["posts"][0]["sources"]), 200.0)

    def test_hanging_post_carries_nothing_and_load_is_lost_from_chain(self):
        state = {
            "car": {"w": 1000, "d": 1000},
            "layers": [
                {
                    "name": "底",
                    "board": {"x": 200, "y": 200, "w": 600, "d": 600, "weight": 0},
                    "wareWeight": 0,
                    "posts": [
                        {"id": "A", "x": 300, "y": 300, "capacity": 0},
                    ],
                },
                {
                    "name": "顶",
                    "board": {"x": 200, "y": 200, "w": 600, "d": 600, "weight": 100},
                    "wareWeight": 100,
                    "posts": [
                        {"id": "H", "x": 900, "y": 900, "capacity": 0},  # 悬空：在下层棚板外、台面内
                    ],
                },
            ],
        }
        r = calculate(state)
        bottom, top = r["layers"]
        self.assertTrue(top["posts"][0]["hanging"])
        self.assertEqual(top["posts"][0]["load"], 0.0)
        self.assertEqual(top["lostLoad"], 200.0)
        self.assertEqual(bottom["loadIncoming"], 0.0)
        kinds = {i["type"] for i in r["issues"]}
        self.assertIn("hang", kinds)
        self.assertIn("unsupported", kinds)

    def test_overlap_and_combined_point_load(self):
        state = {
            "car": {"w": 1000, "d": 1000},
            "layers": [
                {
                    "name": "底",
                    "board": {"x": 0, "y": 0, "w": 1000, "d": 1000, "weight": 0},
                    "wareWeight": 0,
                    "posts": [
                        {"id": "A", "x": 500, "y": 500, "capacity": 0},
                    ],
                },
                {
                    "name": "顶",
                    "board": {"x": 0, "y": 0, "w": 1000, "d": 1000, "weight": 100},
                    "wareWeight": 0,
                    "posts": [
                        {"id": "X", "x": 500, "y": 500, "capacity": 0},
                        {"id": "Y", "x": 503, "y": 500, "capacity": 0},  # 3mm < 10mm
                    ],
                },
            ],
        }
        r = calculate(state)
        bottom, top = r["layers"]
        self.assertTrue(all(p["overlap"] for p in top["posts"]))
        # 两根柱各 50，合并点载荷 100 落在下层 (500,500)
        self.assertEqual(bottom["loadIncoming"], 100.0)
        self.assertEqual(len(bottom["posts"][0]["sources"]), 2)
        self.assertIn("overlap", {i["type"] for i in r["issues"]})

    def test_overload_flag(self):
        state = {
            "car": {"w": 1000, "d": 1000},
            "layers": [
                {
                    "name": "底",
                    "board": {"x": 0, "y": 0, "w": 1000, "d": 1000, "weight": 100},
                    "wareWeight": 0,
                    "posts": [
                        {"id": "A", "x": 100, "y": 100, "capacity": 40},
                        {"id": "B", "x": 900, "y": 900, "capacity": 400},
                    ],
                }
            ],
        }
        r = calculate(state)
        p = r["layers"][0]["posts"]
        self.assertTrue(p[0]["overloaded"])
        self.assertFalse(p[1]["overloaded"])

    def test_sample_data_covers_all_issue_types(self):
        with open(SAMPLE, encoding="utf-8") as f:
            state = json.load(f)
        r = calculate(state)
        kinds = {i["type"] for i in r["issues"]}
        self.assertIn("hang", kinds)
        self.assertIn("overlap", kinds)
        self.assertIn("overload", kinds)
        # 载荷守恒：总自重 == 传到台面的载荷 + 丢失
        self.assertAlmostEqual(
            r["totals"]["ownWeight"],
            r["totals"]["carriedAtBottom"] + r["totals"]["lostLoad"],
            places=1,
        )

    def test_tolerant_normalize(self):
        r = normalize({"layers": [{"board": {"w": "abc"}, "posts": [{"x": ""}]}]})
        self.assertEqual(r["layers"][0]["board"]["w"], 0.0)
        self.assertEqual(r["layers"][0]["posts"][0]["x"], 0.0)

    # ---------------- 立柱失效推演 ----------------
    def test_failed_post_redistributes_and_diff(self):
        state = {
            "car": {"w": 1000, "d": 1000},
            "layers": [
                {
                    "name": "底",
                    "board": {"x": 0, "y": 0, "w": 1000, "d": 1000, "weight": 100},
                    "wareWeight": 100,
                    "posts": [
                        {"id": "A", "x": 100, "y": 100, "capacity": 0},
                        {"id": "B", "x": 900, "y": 900, "capacity": 0},
                    ],
                }
            ],
        }
        r = simulate(state, failed=[{"layer": 0, "post": "A"}])
        base, sim, diff = r["baseline"], r["simulation"], r["diff"]
        # 基准不受影响：无失效标记、载荷照旧
        self.assertEqual([p["load"] for p in base["layers"][0]["posts"]], [100.0, 100.0])
        self.assertFalse(any(p.get("failed") for p in base["layers"][0]["posts"]))
        # 推演：A 失效不承载，B 承担全部 200
        pa, pb = sim["layers"][0]["posts"]
        self.assertTrue(pa["failed"])
        self.assertFalse(pa["hanging"])
        self.assertEqual(pa["load"], 0.0)
        self.assertEqual(pb["load"], 200.0)
        self.assertEqual(sim["layers"][0]["supportedCount"], 1)
        self.assertEqual(sim["layers"][0]["failedCount"], 1)
        self.assertEqual(sim["layers"][0]["hangingCount"], 0)
        # 失效产生 info 级提示，而非 hang
        kinds = {i["type"] for i in sim["issues"]}
        self.assertIn("failed", kinds)
        self.assertNotIn("hang", kinds)
        # 差值：A -100、B +100，台面载荷与丢失载荷不变
        d = {p["post"]: p for p in diff["posts"]}
        self.assertEqual(d["A"]["delta"], -100.0)
        self.assertEqual(d["B"]["delta"], 100.0)
        self.assertEqual(diff["carriedAtBottom"]["delta"], 0.0)
        self.assertEqual(diff["lostLoad"]["delta"], 0.0)
        self.assertEqual(diff["newIssues"], [])
        self.assertEqual(r["failed"], [{"layer": 0, "layerName": "底", "post": "A"}])

    def test_failed_only_support_causes_lost_load_and_new_issue(self):
        state = {
            "car": {"w": 1000, "d": 1000},
            "layers": [
                {
                    "name": "底",
                    "board": {"x": 0, "y": 0, "w": 1000, "d": 1000, "weight": 100},
                    "wareWeight": 100,
                    "posts": [{"id": "A", "x": 100, "y": 100, "capacity": 0}],
                }
            ],
        }
        r = simulate(state, failed=[{"layer": 0, "post": "A"}])
        sim, diff = r["simulation"], r["diff"]
        self.assertEqual(sim["totals"]["lostLoad"], 200.0)
        self.assertEqual(sim["totals"]["carriedAtBottom"], 0.0)
        self.assertEqual(diff["lostLoad"]["delta"], 200.0)
        self.assertEqual(diff["carriedAtBottom"]["delta"], -200.0)
        new_types = {(i["type"], i["layer"]) for i in diff["newIssues"]}
        self.assertIn(("unsupported", 0), new_types)

    def test_failed_post_introduces_overload_as_new_issue(self):
        state = {
            "car": {"w": 1000, "d": 1000},
            "layers": [
                {
                    "name": "底",
                    "board": {"x": 0, "y": 0, "w": 1000, "d": 1000, "weight": 100},
                    "wareWeight": 100,
                    "posts": [
                        {"id": "A", "x": 100, "y": 100, "capacity": 150},
                        {"id": "B", "x": 900, "y": 900, "capacity": 0},
                    ],
                }
            ],
        }
        r = simulate(state, failed=[{"layer": 0, "post": "B"}])
        sim, diff = r["simulation"], r["diff"]
        pa = sim["layers"][0]["posts"][0]
        self.assertTrue(pa["overloaded"])
        self.assertEqual(pa["load"], 200.0)
        self.assertEqual(
            [(i["type"], i["post"]) for i in diff["newIssues"]],
            [("overload", "A")],
        )
        # 基准中 A 未超载 → 不计入新增
        self.assertFalse(r["baseline"]["layers"][0]["posts"][0]["overloaded"])

    def test_failed_mark_on_hanging_post_is_ignored(self):
        state = {
            "car": {"w": 1000, "d": 1000},
            "layers": [
                {
                    "name": "底",
                    "board": {"x": 0, "y": 0, "w": 1000, "d": 1000, "weight": 100},
                    "wareWeight": 0,
                    "posts": [
                        {"id": "H", "x": 2000, "y": 2000, "capacity": 0},  # 悬空
                        {"id": "A", "x": 100, "y": 100, "capacity": 0},
                    ],
                }
            ],
        }
        r = simulate(state, failed=[{"layer": 0, "post": "H"}])
        self.assertEqual(r["failed"], [])
        ph = r["simulation"]["layers"][0]["posts"][0]
        self.assertTrue(ph["hanging"])
        self.assertFalse(ph["failed"])
        self.assertEqual(
            [p["load"] for p in r["simulation"]["layers"][0]["posts"]],
            [p["load"] for p in r["baseline"]["layers"][0]["posts"]],
        )

    def test_calculate_failed_param_directly(self):
        state = {
            "car": {"w": 1000, "d": 1000},
            "layers": [
                {
                    "name": "底",
                    "board": {"x": 0, "y": 0, "w": 1000, "d": 1000, "weight": 0},
                    "wareWeight": 90,
                    "posts": [
                        {"id": "A", "x": 100, "y": 100, "capacity": 0},
                        {"id": "B", "x": 200, "y": 200, "capacity": 0},
                        {"id": "C", "x": 300, "y": 300, "capacity": 0},
                    ],
                }
            ],
        }
        r = calculate(state, failed=[(0, "A"), {"layer": 0, "post": "C"}])
        loads = {p["id"]: p["load"] for p in r["layers"][0]["posts"]}
        self.assertEqual(loads, {"A": 0.0, "B": 90.0, "C": 0.0})
        self.assertEqual(r["totals"]["failedCount"], 2)

    # ---------------- 加柱建议 ----------------
    def _overload_state(self):
        # 单层 1000x1000：200 kg 由 2 根容量 40 的柱承担 → 2 根均超载
        return {
            "car": {"w": 1000, "d": 1000},
            "layers": [
                {
                    "name": "底",
                    "board": {"x": 0, "y": 0, "w": 1000, "d": 1000, "weight": 200},
                    "wareWeight": 0,
                    "posts": [
                        {"id": "A", "x": 100, "y": 100, "capacity": 40},
                        {"id": "B", "x": 900, "y": 900, "capacity": 40},
                    ],
                }
            ],
        }

    def test_suggest_removes_overload_and_sorts_by_coords(self):
        r = suggest_posts(self._overload_state(), layer=0, spacing=100, capacity=300)
        self.assertIsNone(r["reason"])
        # 网格 11x11=121，(100,100)、(900,900) 两个候选点距现有柱 0，剔除 → 119
        self.assertEqual(r["gridCount"], 121)
        self.assertEqual(r["feasibleCount"], 119)
        self.assertLessEqual(len(r["plans"]), 5)
        # 3 根柱均摊 200 → 每根 66.67；原柱容量 40 仍超载（2 根），
        # 新柱容量 300 不超载。丢失 0、超载 2、利用率 1.6667 对所有可行点相同 → 按坐标升序
        coords = [(p["post"]["x"], p["post"]["y"]) for p in r["plans"]]
        self.assertEqual(coords, sorted(coords))
        plan = r["plans"][0]
        self.assertEqual((plan["post"]["x"], plan["post"]["y"]), (0.0, 0.0))
        self.assertEqual(plan["post"]["id"], "P1")
        self.assertAlmostEqual(plan["post"]["load"], 66.67, places=1)
        self.assertEqual(plan["metrics"]["overloadCount"], 2)
        self.assertAlmostEqual(plan["metrics"]["maxUtilization"], 200 / 3 / 40, places=2)
        # 对比表：目标层 200 → 200（总载荷不变），超载数 2 → 2（新柱本身不超载）
        row = plan["layers"][0]
        self.assertEqual(row["loadTotal"]["trial"], 200.0)
        self.assertEqual(row["overloadCount"]["base"], 2)

    def test_suggest_feasible_only_inside_support_and_beyond_threshold(self):
        # 承托面 30x30，现有有效柱 (5,5)，间距 10
        state = {
            "car": {"w": 30, "d": 30},
            "layers": [
                {
                    "name": "底",
                    "board": {"x": 0, "y": 0, "w": 30, "d": 30, "weight": 0},
                    "wareWeight": 0,
                    "posts": [{"id": "A", "x": 5, "y": 5, "capacity": 0}],
                }
            ],
        }
        r = suggest_posts(state, 0, 10, 100)
        self.assertEqual(r["gridCount"], 16)  # 4x4 网格点
        # (0,0) 距 (5,5) 仅 7.07 ≤ 10 → 剔除；(10,0) 距 11.18 可行；共 12 个
        self.assertEqual(r["feasibleCount"], 12)
        for p in r["plans"]:
            x, y = p["post"]["x"], p["post"]["y"]
            self.assertTrue(0 <= x <= 30 and 0 <= y <= 30)
            self.assertGreater(((x - 5) ** 2 + (y - 5) ** 2) ** 0.5, 10.0)
        # 距离恰为阈值（10 mm）的点必须剔除（严格大于）
        state2 = {
            "car": {"w": 20, "d": 10},
            "layers": [
                {
                    "name": "底",
                    "board": {"x": 0, "y": 0, "w": 20, "d": 10, "weight": 0},
                    "wareWeight": 0,
                    "posts": [{"id": "A", "x": 0, "y": 0, "capacity": 0}],
                }
            ],
        }
        r2 = suggest_posts(state2, 0, 10, 0)
        pts = [(p["post"]["x"], p["post"]["y"]) for p in r2["plans"]]
        self.assertNotIn((10.0, 0.0), pts)  # 距离恰为 10 → 不可行
        self.assertEqual(r2["feasibleCount"], 3)

    def test_suggest_recovers_lost_load(self):
        # 上层唯一立柱悬空 → 该层 100 kg 全部丢失；在承托面内加柱可完全找回
        state = {
            "car": {"w": 100, "d": 100},
            "layers": [
                {
                    "name": "底",
                    "board": {"x": 0, "y": 0, "w": 100, "d": 100, "weight": 0},
                    "wareWeight": 0,
                    "posts": [{"id": "A", "x": 50, "y": 50, "capacity": 0}],
                },
                {
                    "name": "顶",
                    "board": {"x": 0, "y": 0, "w": 100, "d": 100, "weight": 0},
                    "wareWeight": 100,
                    "posts": [{"id": "H", "x": 500, "y": 500, "capacity": 0}],  # 悬空
                },
            ],
        }
        r = suggest_posts(state, 1, 50, 0)
        self.assertEqual(r["baselineMetrics"]["lostLoad"], 100.0)
        best = r["plans"][0]
        self.assertEqual(best["metrics"]["lostLoad"], 0.0)
        # 顶层 100 kg 经新柱传到下层，再由底层 A 柱传到台面 → 台面 100 kg
        self.assertEqual(best["metrics"]["carriedAtBottom"], 100.0)
        # 容量 0（不限容量）→ 最高利用率为 None（排序时置后）
        self.assertIsNone(best["metrics"]["maxUtilization"])
        # 原悬空柱 H 仍悬空（加柱不改变其状态），但整层恢复有效支撑 → 消除无支撑告警
        removed = [(i["type"], i["layer"]) for i in best["issues"]["removed"]]
        self.assertIn(("unsupported", 1), removed)
        self.assertNotIn(("hang", 1), removed)
        self.assertEqual(best["issues"]["added"], [])

    def test_suggest_no_feasible_point_gives_reason(self):
        state = {
            "car": {"w": 10, "d": 10},
            "layers": [
                {
                    "name": "底",
                    "board": {"x": 0, "y": 0, "w": 10, "d": 10, "weight": 0},
                    "wareWeight": 0,
                    "posts": [{"id": "A", "x": 5, "y": 5, "capacity": 0}],
                }
            ],
        }
        r = suggest_posts(state, 0, 10, 100)
        self.assertEqual(r["plans"], [])
        self.assertEqual(r["feasibleCount"], 0)
        self.assertIn("重叠阈值", r["reason"])

    def test_suggest_zero_size_support_gives_reason(self):
        state = {
            "car": {"w": 0, "d": 0},
            "layers": [
                {
                    "name": "底",
                    "board": {"x": 0, "y": 0, "w": 0, "d": 0, "weight": 0},
                    "wareWeight": 0,
                    "posts": [],
                }
            ],
        }
        r = suggest_posts(state, 0, 100, 100)
        self.assertEqual(r["plans"], [])
        self.assertIn("承托面", r["reason"])

    def test_suggest_grid_too_dense_gives_reason(self):
        state = {
            "car": {"w": 1000, "d": 1000},
            "layers": [
                {
                    "name": "底",
                    "board": {"x": 0, "y": 0, "w": 1000, "d": 1000, "weight": 0},
                    "wareWeight": 0,
                    "posts": [],
                }
            ],
        }
        r = suggest_posts(state, 0, 1, 100)
        self.assertEqual(r["plans"], [])
        self.assertGreater(r["gridCount"], 4096)
        self.assertIn("加大候选网格间距", r["reason"])

    def test_suggest_invalid_params_raise(self):
        state = self._overload_state()
        with self.assertRaises(ValueError):
            suggest_posts(state, 9, 100, 100)       # 层越界
        with self.assertRaises(ValueError):
            suggest_posts(state, 0, 0, 100)         # 间距 0
        with self.assertRaises(ValueError):
            suggest_posts(state, 0, -10, 100)       # 间距为负
        with self.assertRaises(ValueError):
            suggest_posts(state, 0, 100, -5)        # 容量为负
        with self.assertRaises(ValueError):
            suggest_posts(state, "x", 100, 100)     # 层非整数

    def test_suggest_does_not_mutate_input(self):
        state = self._overload_state()
        before = json.loads(json.dumps(state, ensure_ascii=False))
        suggest_posts(state, 0, 100, 300)
        self.assertEqual(state, before)

    # ---------------- 减载配额试算 ----------------
    def _two_layer_state(self, bottom_ware, bottom_cap, top_ware=400, top_cap=0, board=100):
        return {
            "car": {"w": 1000, "d": 1000},
            "layers": [
                {
                    "name": "底",
                    "board": {"x": 0, "y": 0, "w": 1000, "d": 1000, "weight": board},
                    "wareWeight": bottom_ware,
                    "posts": [
                        {"id": "A", "x": 100, "y": 100, "capacity": bottom_cap},
                        {"id": "B", "x": 900, "y": 900, "capacity": bottom_cap},
                    ],
                },
                {
                    "name": "顶",
                    "board": {"x": 0, "y": 0, "w": 1000, "d": 1000, "weight": board},
                    "wareWeight": top_ware,
                    "posts": [
                        {"id": "C", "x": 200, "y": 200, "capacity": top_cap},
                        {"id": "D", "x": 800, "y": 800, "capacity": top_cap},
                    ],
                },
            ],
        }

    def test_shed_single_layer_minimum_reduction(self):
        # board 100 + ware 100 = 200，2 柱容量 80：均摊 100 > 80 → 总载荷须 ≤160 → 减 40
        state = {
            "car": {"w": 1000, "d": 1000},
            "layers": [
                {
                    "name": "底",
                    "board": {"x": 0, "y": 0, "w": 1000, "d": 1000, "weight": 100},
                    "wareWeight": 100,
                    "posts": [
                        {"id": "A", "x": 100, "y": 100, "capacity": 80},
                        {"id": "B", "x": 900, "y": 900, "capacity": 80},
                    ],
                }
            ],
        }
        r = load_shed(state)
        self.assertTrue(r["feasible"])
        self.assertEqual(r["totalReduction"], 40.0)
        self.assertEqual(r["layers"][0]["wareOriginal"], 100.0)
        self.assertEqual(r["layers"][0]["wareSuggested"], 60.0)
        # 重新整架校验：超载 2 → 0
        self.assertEqual(r["metrics"]["overloadCount"], {"before": 2, "after": 0})
        self.assertFalse(
            any(p["overloaded"] for L in r["trial"]["layers"] for p in L["posts"])
        )
        # 不修改输入
        self.assertEqual(state["layers"][0]["wareWeight"], 100)

    def test_shed_prefers_same_layer_then_backfills_upper(self):
        # 顶层 2 柱不限容量；底层总载荷 = 100(板)+20(坯)+500(顶) = 620，
        # 容量上限 2×280=560 → 需后缀减 60；本层坯件仅 20 → 本层减 20、顶层补 40
        r = load_shed(self._two_layer_state(20, 280))
        self.assertTrue(r["feasible"])
        self.assertEqual(r["totalReduction"], 60.0)
        self.assertEqual(r["layers"][0]["reduction"], 20.0)
        self.assertEqual(r["layers"][1]["reduction"], 40.0)
        self.assertFalse(
            any(p["overloaded"] for L in r["trial"]["layers"] for p in L["posts"])
        )
        # 最小性：顶层减得比它自身（无容量约束）所需多，全部是为底层服务；60 是下界
        self.assertEqual(r["layers"][1]["wareSuggested"], 360.0)

    def test_shed_nested_constraints_minimum_total(self):
        # 顶层：100+400=500 > 2×200=400 → 顶层后缀须减 100
        # 底层：100+50+500=650 > 2×200=400 → 两后缀合计须减 250（覆盖顶层的 100）
        r = load_shed(self._two_layer_state(50, 200, top_cap=200))
        self.assertTrue(r["feasible"])
        self.assertEqual(r["totalReduction"], 250.0)  # 不是 100+250 的重复计算
        # 先减本层：底层坯件 50 全部减掉，其余 200 由顶层承担
        self.assertEqual(r["layers"][0]["reduction"], 50.0)
        self.assertEqual(r["layers"][1]["reduction"], 200.0)
        self.assertEqual(r["layers"][1]["wareSuggested"], 200.0)
        self.assertEqual(r["metrics"]["overloadCount"], {"before": 4, "after": 0})

    def test_shed_no_overload_returns_zero_plan(self):
        state = {
            "car": {"w": 1000, "d": 1000},
            "layers": [
                {
                    "name": "底",
                    "board": {"x": 0, "y": 0, "w": 1000, "d": 1000, "weight": 100},
                    "wareWeight": 100,
                    "posts": [{"id": "A", "x": 100, "y": 100, "capacity": 500}],
                }
            ],
        }
        r = load_shed(state)
        self.assertTrue(r["feasible"])
        self.assertEqual(r["totalReduction"], 0.0)
        self.assertFalse(r["layers"][0]["changed"])
        self.assertEqual(r["layers"][0]["wareSuggested"], 100.0)

    def test_shed_infeasible_when_board_alone_overloads(self):
        # 单层：板自重 200，2 柱容量 80 → 坯件清空后均摊仍 100 > 80，无解
        state = {
            "car": {"w": 1000, "d": 1000},
            "layers": [
                {
                    "name": "底",
                    "board": {"x": 0, "y": 0, "w": 1000, "d": 1000, "weight": 200},
                    "wareWeight": 100,
                    "posts": [
                        {"id": "A", "x": 100, "y": 100, "capacity": 80},
                        {"id": "B", "x": 900, "y": 900, "capacity": 80},
                    ],
                }
            ],
        }
        r = load_shed(state)
        self.assertFalse(r["feasible"])
        self.assertEqual(len(r["remainingOverloads"]), 2)
        self.assertEqual({p["post"] for p in r["remainingOverloads"]}, {"A", "B"})
        self.assertIn("清空", r["reason"])
        # 无解时给的是“全部清空”后的整架校验结果与逐层行
        self.assertEqual(r["metrics"]["overloadCount"]["after"], 2)
        self.assertEqual(r["layers"][0]["wareSuggested"], 0.0)
        self.assertEqual(r["layers"][0]["reduction"], 100.0)

    def test_shed_infeasible_upper_layer_board_overload(self):
        # 顶板自重 500、单柱容量 200：清空坯件后顶柱仍超载；底层柱不限容量
        state = {
            "car": {"w": 1000, "d": 1000},
            "layers": [
                {
                    "name": "底",
                    "board": {"x": 0, "y": 0, "w": 1000, "d": 1000, "weight": 0},
                    "wareWeight": 0,
                    "posts": [{"id": "A", "x": 500, "y": 500, "capacity": 0}],
                },
                {
                    "name": "顶",
                    "board": {"x": 0, "y": 0, "w": 1000, "d": 1000, "weight": 500},
                    "wareWeight": 100,
                    "posts": [{"id": "T", "x": 500, "y": 500, "capacity": 200}],
                },
            ],
        }
        r = load_shed(state)
        self.assertFalse(r["feasible"])
        self.assertTrue(any(p["layer"] == 1 for p in r["remainingOverloads"]))

    def test_shed_zero_support_layer_breaks_chain(self):
        # 中间层无立柱 → 顶层载荷在此丢失，不压到底层；
        # 底层 100+500=600 单柱容量 200 → 只需底层减 400；中/顶层坯件不动
        state = {
            "car": {"w": 1000, "d": 1000},
            "layers": [
                {
                    "name": "底",
                    "board": {"x": 0, "y": 0, "w": 1000, "d": 1000, "weight": 100},
                    "wareWeight": 500,
                    "posts": [{"id": "A", "x": 100, "y": 100, "capacity": 200}],
                },
                {
                    "name": "中",
                    "board": {"x": 0, "y": 0, "w": 1000, "d": 1000, "weight": 100},
                    "wareWeight": 500,
                    "posts": [],
                },
                {
                    "name": "顶",
                    "board": {"x": 0, "y": 0, "w": 1000, "d": 1000, "weight": 100},
                    "wareWeight": 500,
                    "posts": [{"id": "C", "x": 200, "y": 200, "capacity": 0}],
                },
            ],
        }
        r = load_shed(state)
        self.assertTrue(r["feasible"])
        self.assertEqual(r["layers"][0]["reduction"], 400.0)
        self.assertEqual(r["layers"][1]["reduction"], 0.0)
        self.assertEqual(r["layers"][2]["reduction"], 0.0)
        # 中/顶层丢失载荷不变（未减它们的坯件）
        self.assertEqual(r["layers"][1]["lostBefore"], r["layers"][1]["lostAfter"])

    def test_shed_three_layer_backfill_order(self):
        # 三层各 300 坯件、板自重 0；底层单柱容量 450：
        # 顶层 300 → 中层 600 → 底层 900 > 450，后缀须减 450；
        # 先减底层 300，再由近及远减中层 150，顶层不动
        state = {
            "car": {"w": 1000, "d": 1000},
            "layers": [
                {
                    "name": "底",
                    "board": {"x": 0, "y": 0, "w": 1000, "d": 1000, "weight": 0},
                    "wareWeight": 300,
                    "posts": [{"id": "A", "x": 500, "y": 500, "capacity": 450}],
                },
                {
                    "name": "中",
                    "board": {"x": 0, "y": 0, "w": 1000, "d": 1000, "weight": 0},
                    "wareWeight": 300,
                    "posts": [
                        {"id": "M", "x": 300, "y": 300, "capacity": 0},
                        {"id": "N", "x": 700, "y": 700, "capacity": 0},
                    ],
                },
                {
                    "name": "顶",
                    "board": {"x": 0, "y": 0, "w": 1000, "d": 1000, "weight": 0},
                    "wareWeight": 300,
                    "posts": [{"id": "T", "x": 500, "y": 500, "capacity": 0}],
                },
            ],
        }
        r = load_shed(state)
        self.assertTrue(r["feasible"])
        self.assertEqual(r["totalReduction"], 450.0)
        self.assertEqual(
            [L["reduction"] for L in r["layers"]], [300.0, 150.0, 0.0]
        )
        self.assertEqual(r["metrics"]["overloadCount"]["after"], 0)

    def test_shed_quantization_grid_minimum(self):
        # 连续最优减量 0.03 kg（恰好 3 个 0.01 单位）：建议重量 0.02；
        # 回调 0.01（0.03，均摊 50.015 > 容量 50.01）会重新超载 → 网格上总减量最小
        state = {
            "car": {"w": 1000, "d": 1000},
            "layers": [
                {
                    "name": "底",
                    "board": {"x": 0, "y": 0, "w": 1000, "d": 1000, "weight": 100},
                    "wareWeight": 0.05,
                    "posts": [
                        {"id": "A", "x": 100, "y": 100, "capacity": 50.01},
                        {"id": "B", "x": 900, "y": 900, "capacity": 50.01},
                    ],
                }
            ],
        }
        r = load_shed(state)
        self.assertTrue(r["feasible"])
        self.assertEqual(r["layers"][0]["wareSuggested"], 0.02)
        self.assertFalse(
            any(p["overloaded"] for L in r["trial"]["layers"] for p in L["posts"])
        )
        # 网格最小性：建议重量 +0.01（=0.03）后必再次超载
        probe = json.loads(json.dumps(state))
        probe["layers"][0]["wareWeight"] = 0.03
        self.assertTrue(
            any(
                p["overloaded"]
                for L in calculate(probe)["layers"]
                for p in L["posts"]
            )
        )

    def test_shed_fractional_need_rounds_up_to_whole_grid_unit(self):
        # 板自重 1000 + 坯件 200 = 1200，2 柱容量 599.99 → 上限 1199.98，
        # 连续最优减量 0.02 kg（恰好 2 个 0.01 网格单位）：建议 199.98；
        # 回调 0.01（199.99，均摊 599.995 > 599.99）会再次超载
        state = {
            "car": {"w": 1000, "d": 1000},
            "layers": [
                {
                    "name": "底",
                    "board": {"x": 0, "y": 0, "w": 1000, "d": 1000, "weight": 1000},
                    "wareWeight": 200.00,
                    "posts": [
                        {"id": "A", "x": 100, "y": 100, "capacity": 599.99},
                        {"id": "B", "x": 900, "y": 900, "capacity": 599.99},
                    ],
                }
            ],
        }
        r = load_shed(state)
        self.assertTrue(r["feasible"])
        self.assertEqual(r["layers"][0]["wareSuggested"], 199.98)
        self.assertEqual(r["layers"][0]["reduction"], 0.02)
        probe = json.loads(json.dumps(state))
        probe["layers"][0]["wareWeight"] = 199.99  # 均摊 599.995 > 599.99 → 再次超载
        self.assertTrue(
            any(p["overloaded"] for L in calculate(probe)["layers"] for p in L["posts"])
        )

    def test_shed_empty_rack(self):
        r = load_shed({"car": {"w": 1000, "d": 1000}, "layers": []})
        self.assertTrue(r["feasible"])
        self.assertEqual(r["totalReduction"], 0.0)
        self.assertEqual(r["layers"], [])

    def test_shed_sample_state_feasible_and_verified(self):
        with open(SAMPLE, encoding="utf-8") as f:
            state = json.load(f)
        r = load_shed(state)
        self.assertTrue(r["feasible"])
        # 方案经同一套 calculate 重新整架校验：零超载
        self.assertFalse(
            any(p["overloaded"] for L in r["trial"]["layers"] for p in L["posts"])
        )
        # 减量守恒：减载量 == 原坯件合计 − 建议坯件合计
        self.assertAlmostEqual(
            r["totalReduction"],
            sum(L["wareOriginal"] - L["wareSuggested"] for L in r["layers"]),
            places=6,
        )
        # 每层减量不超过其原坯件重量
        for L in r["layers"]:
            self.assertGreaterEqual(L["reduction"], 0.0)
            self.assertLessEqual(L["reduction"], L["wareOriginal"] + 1e-9)

    def test_shed_does_not_mutate_input(self):
        with open(SAMPLE, encoding="utf-8") as f:
            state = json.load(f)
        before = json.loads(json.dumps(state, ensure_ascii=False))
        load_shed(state)
        self.assertEqual(state, before)

    # ---------------- 层架抗倾覆试算 ----------------
    def _overturn_rack(self, posts=None, height=300, weight=100, ware=100):
        posts = (
            posts
            if posts is not None
            else [
                {"id": "A", "x": 0, "y": 0, "capacity": 0},
                {"id": "B", "x": 1000, "y": 0, "capacity": 0},
                {"id": "C", "x": 0, "y": 1000, "capacity": 0},
                {"id": "D", "x": 1000, "y": 1000, "capacity": 0},
            ]
        )
        return {
            "car": {"w": 1000, "d": 1000},
            "layers": [
                {
                    "name": "底",
                    "height": height,
                    "board": {"x": 0, "y": 0, "w": 1000, "d": 1000, "weight": weight},
                    "wareWeight": ware,
                    "posts": posts,
                }
            ],
        }

    def test_overturn_zero_accel_stable_centered(self):
        r = overturn_check(self._overturn_rack(), 0, 0)
        self.assertTrue(r["evaluable"])
        self.assertEqual(r["verdict"], "stable")
        # 重心在 (500, 500)，到四边（凸包即台面）裕量 500
        self.assertEqual(r["cg"], {"x": 500.0, "y": 500.0, "z": 300.0})
        self.assertEqual(r["shift"], {"x": 0.0, "y": 0.0})
        self.assertEqual(r["projection"], {"x": 500.0, "y": 500.0})
        self.assertEqual(r["margin"], 500.0)
        self.assertEqual(r["totalWeight"], 200.0)
        # 凸包为四角
        self.assertEqual(
            {(p["x"], p["y"]) for p in r["polygon"]},
            {(0.0, 0.0), (1000.0, 0.0), (0.0, 1000.0), (1000.0, 1000.0)},
        )

    def test_overturn_boundary_is_critical(self):
        # 重心高 300、x 向距边界 500：ax=500/300 时投影恰在右边界（带符号距离 0）
        crit = 500.0 / 300.0
        r = overturn_check(self._overturn_rack(), crit, 0)
        self.assertEqual(r["verdict"], "critical")
        self.assertEqual(r["margin"], 0.0)
        self.assertEqual(r["projection"]["x"], 1000.0)
        # 略小：稳定（裕量 20）；略大：倾覆（-10）
        self.assertEqual(overturn_check(self._overturn_rack(), 1.6, 0)["verdict"], "stable")
        self.assertEqual(overturn_check(self._overturn_rack(), 1.6, 0)["margin"], 20.0)
        self.assertEqual(overturn_check(self._overturn_rack(), 1.7, 0)["verdict"], "overturn")
        self.assertEqual(overturn_check(self._overturn_rack(), 1.7, 0)["margin"], -10.0)

    def test_overturn_negative_accel_shifts_opposite_direction(self):
        r = overturn_check(self._overturn_rack(), -1.6, 0)
        self.assertEqual(r["projection"]["x"], 20.0)
        self.assertEqual(r["verdict"], "stable")
        self.assertEqual(r["margin"], 20.0)
        r2res = overturn_check(self._overturn_rack(), 0, -1.7)
        self.assertEqual(r2res["projection"]["y"], -10.0)
        self.assertEqual(r2res["verdict"], "overturn")

    def test_overturn_multi_layer_weighted_cg_uses_cumulative_height(self):
        # 两层重量均为 200（板100+坯100），板中心分别为 (500,500) 与 (300,700)，
        # 层高各 300 → z 为 300、600；整架重心 = 二者均值
        state = {
            "car": {"w": 1000, "d": 1000},
            "layers": [
                {
                    "name": "底",
                    "height": 300,
                    "board": {"x": 0, "y": 0, "w": 1000, "d": 1000, "weight": 100},
                    "wareWeight": 100,
                    "posts": [
                        {"id": "A", "x": 0, "y": 0, "capacity": 0},
                        {"id": "B", "x": 1000, "y": 0, "capacity": 0},
                        {"id": "C", "x": 0, "y": 1000, "capacity": 0},
                        {"id": "D", "x": 1000, "y": 1000, "capacity": 0},
                    ],
                },
                {
                    "name": "顶",
                    "height": 300,
                    "board": {"x": 0, "y": 200, "w": 600, "d": 1000, "weight": 100},
                    "wareWeight": 100,
                    "posts": [
                        {"id": "E", "x": 100, "y": 300, "capacity": 0},
                        {"id": "F", "x": 900, "y": 300, "capacity": 0},
                        {"id": "G", "x": 100, "y": 900, "capacity": 0},
                        {"id": "H", "x": 900, "y": 900, "capacity": 0},
                    ],
                },
            ],
        }
        r = overturn_check(state, 0, 0)
        self.assertTrue(r["evaluable"])
        self.assertEqual(r["cg"]["x"], 400.0)
        self.assertEqual(r["cg"]["y"], 600.0)
        self.assertEqual(r["cg"]["z"], 450.0)
        self.assertEqual(r["totalWeight"], 400.0)
        self.assertEqual([L["z"] for L in r["layers"]], [300.0, 600.0])
        # 支撑多边形只取底层四点（与上层立柱无关）
        self.assertEqual(len(r["polygon"]), 4)

    def test_overturn_inner_support_points_not_hull_vertices(self):
        # 第五根柱落在台面中央：凸包仍是四角，但它计入 supportPoints
        posts = [
            {"id": "A", "x": 0, "y": 0, "capacity": 0},
            {"id": "B", "x": 1000, "y": 0, "capacity": 0},
            {"id": "C", "x": 0, "y": 1000, "capacity": 0},
            {"id": "D", "x": 1000, "y": 1000, "capacity": 0},
            {"id": "M", "x": 500, "y": 500, "capacity": 0},
        ]
        r = overturn_check(self._overturn_rack(posts), 0, 0)
        self.assertEqual(len(r["polygon"]), 4)
        self.assertEqual(r["supportPointCount"], 5)

    def test_overturn_hanging_bottom_posts_excluded_from_polygon(self):
        # 台面 1000x1000；五根柱中一根悬空，有效四点仍成四边形
        posts = [
            {"id": "A", "x": 0, "y": 0, "capacity": 0},
            {"id": "B", "x": 1000, "y": 0, "capacity": 0},
            {"id": "C", "x": 0, "y": 1000, "capacity": 0},
            {"id": "D", "x": 1000, "y": 1000, "capacity": 0},
            {"id": "H", "x": 2000, "y": 2000, "capacity": 0},
        ]
        r = overturn_check(self._overturn_rack(posts), 0, 0)
        self.assertTrue(r["evaluable"])
        self.assertEqual(r["supportPointCount"], 4)

    def test_overturn_collinear_support_not_evaluable(self):
        # 三根柱共线 → 凸包面积为 0
        posts = [
            {"id": "A", "x": 0, "y": 500, "capacity": 0},
            {"id": "B", "x": 500, "y": 500, "capacity": 0},
            {"id": "C", "x": 1000, "y": 500, "capacity": 0},
        ]
        r = overturn_check(self._overturn_rack(posts), 0, 0)
        self.assertFalse(r["evaluable"])
        self.assertIsNone(r["verdict"])
        self.assertIn("支撑多边形", r["reason"])

    def test_overturn_too_few_support_points_not_evaluable(self):
        posts = [
            {"id": "A", "x": 0, "y": 0, "capacity": 0},
            {"id": "B", "x": 1000, "y": 1000, "capacity": 0},
        ]
        r = overturn_check(self._overturn_rack(posts), 0, 0)
        self.assertFalse(r["evaluable"])
        self.assertIn("3 个不共线落点", r["reason"])

    def test_overturn_duplicate_points_dedupe(self):
        # 三点，其中两点完全重合（落点重叠）：去重后仅 2 个唯一点 → 无法评估
        posts = [
            {"id": "A", "x": 0, "y": 0, "capacity": 0},
            {"id": "B", "x": 1000, "y": 0, "capacity": 0},
            {"id": "C", "x": 1000, "y": 0, "capacity": 0},
        ]
        r = overturn_check(self._overturn_rack(posts), 0, 0)
        self.assertFalse(r["evaluable"])
        self.assertEqual(r["supportPointCount"], 3)

    def test_overturn_unsupported_layer_not_evaluable(self):
        with open(SAMPLE, encoding="utf-8") as f:
            state = json.load(f)
        state["layers"][1]["posts"] = []  # 第二层无立柱
        r = overturn_check(state, 0.1, 0)
        self.assertFalse(r["evaluable"])
        self.assertEqual([u["index"] for u in r["unsupportedLayers"]], [1])
        self.assertIn("无有效承托", r["reason"])

    def test_overturn_all_bottom_posts_hanging_not_evaluable(self):
        posts = [{"id": "H", "x": 5000, "y": 5000, "capacity": 0}]
        r = overturn_check(self._overturn_rack(posts), 0, 0)
        self.assertFalse(r["evaluable"])
        # 底层无有效承托（全部悬空）优先命中“无有效承托”判定
        self.assertEqual([u["index"] for u in r["unsupportedLayers"]], [0])
        self.assertIn("无有效承托", r["reason"])

    def test_overturn_empty_rack_not_evaluable(self):
        r = overturn_check({"car": {"w": 1000, "d": 1000}, "layers": []}, 0, 0)
        self.assertFalse(r["evaluable"])
        self.assertIn("没有任何层", r["reason"])

    def test_overturn_zero_weight_not_evaluable(self):
        r = overturn_check(self._overturn_rack(weight=0, ware=0), 0, 0)
        self.assertFalse(r["evaluable"])
        self.assertIn("合计为 0", r["reason"])

    def test_overturn_invalid_accel_raises(self):
        state = self._overturn_rack()
        with self.assertRaises(ValueError):
            overturn_check(state, "x", 0)
        with self.assertRaises(ValueError):
            overturn_check(state, 0, None)
        with self.assertRaises(ValueError):
            overturn_check(state, float("inf"), 0)
        with self.assertRaises(ValueError):
            overturn_check(state, 0, float("nan"))

    def test_overturn_sample_evaluable_and_does_not_mutate_input(self):
        with open(SAMPLE, encoding="utf-8") as f:
            state = json.load(f)
        before = json.loads(json.dumps(state, ensure_ascii=False))
        r = overturn_check(state, 0.1, 0)
        self.assertTrue(r["evaluable"])
        self.assertIn(r["verdict"], {"stable", "critical", "overturn"})
        self.assertEqual(state, before)
        # 静止工况必然稳定（重心在四角凸包内部）
        r0 = overturn_check(state, 0, 0)
        self.assertEqual(r0["verdict"], "stable")
        self.assertGreater(r0["margin"], 0)

    # ---------------- 逐层装货顺序规划 ----------------
    def _seq_rack(self, board_offsets, wares, board_weights=None, height=300):
        """n 层同尺寸（可整体平移 x）架体；四角有效柱，坯件重量逐层给定。"""
        board_weights = board_weights or [100] * len(wares)
        return {
            "car": {"w": 1000, "d": 1000},
            "layers": [
                {
                    "name": f"第{i + 1}层",
                    "height": height,
                    "board": {
                        "x": off, "y": 0, "w": 1000, "d": 1000,
                        "weight": bw,
                    },
                    "wareWeight": w,
                    "posts": [
                        {"id": "A", "x": 0, "y": 0, "capacity": 0},
                        {"id": "B", "x": 1000, "y": 0, "capacity": 0},
                        {"id": "C", "x": 0, "y": 1000, "capacity": 0},
                        {"id": "D", "x": 1000, "y": 1000, "capacity": 0},
                    ],
                }
                for i, (off, w, bw) in enumerate(
                    zip(board_offsets, wares, board_weights)
                )
            ],
        }

    def test_loading_sequence_two_layers_light_low_first(self):
        # 底层坯轻、顶层坯重且顶层棚板整体偏 +x：先装底层（低、居中）更安全
        state = self._seq_rack([0, 200], [100, 800])
        r = loading_sequence(state, 0.5, 0)
        self.assertTrue(r["planable"])
        self.assertEqual(r["candidateCount"], 2)
        # 与两种顺序的独立整架计算对照
        def margins(order):
            s = json.loads(json.dumps(state))
            for L in s["layers"]:
                L["wareWeight"] = 0
            vals = [overturn_check(s, 0.5, 0)["margin"]]
            for i in order:
                s["layers"][i]["wareWeight"] = state["layers"][i]["wareWeight"]
                vals.append(overturn_check(s, 0.5, 0)["margin"])
            return min(vals), vals

        m01, vals01 = margins((0, 1))
        m10, vals10 = margins((1, 0))
        self.assertGreater(m01, m10)  # 先轻后低确实更优
        self.assertEqual(r["order"], [0, 1])
        self.assertAlmostEqual(r["minMargin"], m01, places=9)
        # steps：初始（无坯）+ 每步新增一层
        self.assertEqual(len(r["steps"]), 3)
        self.assertIsNone(r["steps"][0]["addedIndex"])
        self.assertEqual([s["addedIndex"] for s in r["steps"][1:]], [0, 1])
        self.assertEqual(
            [s["cumulativeWareWeight"] for s in r["steps"]], [0.0, 100.0, 900.0]
        )
        for got, exp in zip(
            [s["margin"] for s in r["steps"]], vals01
        ):
            self.assertAlmostEqual(got, exp, places=9)
        self.assertTrue(r["allStable"])
        self.assertGreater(r["minMargin"], 0)

    def test_loading_sequence_lexicographic_tie(self):
        # 对称架体 + 轴对称载荷：任意顺序裕量相同 → 取层序号字典序 [0, 1, 2]
        state = self._seq_rack([0, 0, 0], [100, 100, 100])
        r = loading_sequence(state, 0.1, 0)
        self.assertTrue(r["planable"])
        self.assertEqual(r["order"], [0, 1, 2])
        self.assertTrue(all(s["stable"] for s in r["steps"]))

    def test_loading_sequence_nonpositive_margin_marks_unstable(self):
        # 较大水平加速度：即便最优顺序，过程中仍会倾覆 → allStable=False
        state = self._seq_rack([0, 200], [100, 800])
        r = loading_sequence(state, 1.2, 0)
        self.assertTrue(r["planable"])
        self.assertFalse(r["allStable"])
        self.assertLessEqual(r["minMargin"], 0)
        # minMarginStep 指向取得最小裕量的步
        self.assertEqual(
            r["minMarginStep"],
            min(r["steps"], key=lambda s: (s["margin"], s["step"]))["step"],
        )

    def test_loading_sequence_critical_is_not_stable(self):
        # 初始静止时稳定；选一个让某一中间状态恰好临界的加速度 → allStable=False
        # 单候选层：先求临界加速度（对加坯后的重心高度），再在该工况规划
        state = self._seq_rack([0], [200])
        # 板100、坯200：重心高 300，x 中心 500；临界 ax = 500/300
        r = loading_sequence(state, 500.0 / 300.0, 0)
        self.assertTrue(r["planable"])
        self.assertFalse(r["allStable"])
        self.assertEqual(r["minMargin"], 0.0)
        verdicts = {s["step"]: s["verdict"] for s in r["steps"]}
        self.assertEqual(verdicts[1], "critical")

    def test_loading_sequence_zero_ware_layers_not_candidates(self):
        state = self._seq_rack([0, 100, 0], [100, 0, 200])
        r = loading_sequence(state, 0.1, 0)
        self.assertTrue(r["planable"])
        self.assertEqual(r["candidateCount"], 2)
        self.assertEqual([c["index"] for c in r["candidates"]], [0, 2])
        self.assertEqual(set(r["order"]), {0, 2})
        # 中间层坯件始终为 0：每步总重只含板自重 + 已加坯件
        self.assertEqual(r["steps"][0]["totalWeight"], 300.0)

    def test_loading_sequence_no_candidate_not_planable(self):
        state = self._seq_rack([0, 100], [0, 0])
        r = loading_sequence(state, 0.1, 0)
        self.assertFalse(r["planable"])
        self.assertIsNone(r["order"])
        self.assertIn("没有坯件重量大于零的层", r["reason"])

    def test_loading_sequence_unsupported_not_planable(self):
        with open(SAMPLE, encoding="utf-8") as f:
            state = json.load(f)
        state["layers"][1]["posts"] = []
        r = loading_sequence(state, 0.1, 0)
        self.assertFalse(r["planable"])
        self.assertEqual([u["index"] for u in r["unsupportedLayers"]], [1])
        self.assertIn("无有效承托", r["reason"])

    def test_loading_sequence_degenerate_hull_not_planable(self):
        state = self._seq_rack([0], [100])
        state["layers"][0]["posts"] = [
            {"id": "A", "x": 0, "y": 500, "capacity": 0},
            {"id": "B", "x": 500, "y": 500, "capacity": 0},
            {"id": "C", "x": 1000, "y": 500, "capacity": 0},
        ]
        r = loading_sequence(state, 0.1, 0)
        self.assertFalse(r["planable"])
        self.assertIn("支撑多边形", r["reason"])

    def test_loading_sequence_zero_board_weight_not_planable(self):
        state = self._seq_rack([0, 100], [100, 100], board_weights=[0, 0])
        r = loading_sequence(state, 0.1, 0)
        self.assertFalse(r["planable"])
        self.assertIn("初始棚板总重为 0", r["reason"])

    def test_loading_sequence_empty_rack_not_planable(self):
        r = loading_sequence(
            {"car": {"w": 1000, "d": 1000}, "layers": []}, 0.1, 0
        )
        self.assertFalse(r["planable"])
        self.assertIn("没有任何层", r["reason"])

    def test_loading_sequence_over_limit_not_planable(self):
        posts = [
            {"id": "A", "x": 0, "y": 0, "capacity": 0},
            {"id": "B", "x": 1000, "y": 0, "capacity": 0},
            {"id": "C", "x": 0, "y": 1000, "capacity": 0},
            {"id": "D", "x": 1000, "y": 1000, "capacity": 0},
        ]
        state = {
            "car": {"w": 1000, "d": 1000},
            "layers": [
                {
                    "name": f"L{i}", "height": 300,
                    "board": {"x": 0, "y": 0, "w": 1000, "d": 1000, "weight": 100},
                    "wareWeight": 100, "posts": posts,
                }
                for i in range(13)
            ],
        }
        r = loading_sequence(state, 0.1, 0)
        self.assertFalse(r["planable"])
        self.assertEqual(r["candidateCount"], 13)
        self.assertEqual(r["candidateLimit"], 12)
        self.assertIn("12 层", r["reason"])
        self.assertIn("缩减", r["reason"])
        # 恰好 12 层：可规划
        state["layers"] = state["layers"][:12]
        r12 = loading_sequence(state, 0.1, 0)
        self.assertTrue(r12["planable"])
        self.assertEqual(len(r12["order"]), 12)

    def test_loading_sequence_invalid_accel_raises(self):
        state = self._seq_rack([0], [100])
        with self.assertRaises(ValueError):
            loading_sequence(state, "x", 0)
        with self.assertRaises(ValueError):
            loading_sequence(state, 0, float("nan"))

    def test_loading_sequence_does_not_mutate_input(self):
        with open(SAMPLE, encoding="utf-8") as f:
            state = json.load(f)
        before = json.loads(json.dumps(state, ensure_ascii=False))
        r = loading_sequence(state, 0.15, -0.1)
        self.assertTrue(r["planable"])
        self.assertEqual(state, before)
        # 终态（全部坯件加入）与 overturn_check 完全一致
        oc = overturn_check(state, 0.15, -0.1)
        self.assertEqual(r["steps"][-1]["margin"], oc["margin"])
        self.assertEqual(r["steps"][-1]["cg"], oc["cg"])
        self.assertEqual(r["steps"][-1]["projection"], oc["projection"])
        # 初态（坯件清零）与 overturn_check 清零工况一致
        cleared = json.loads(json.dumps(state))
        for L in cleared["layers"]:
            L["wareWeight"] = 0
        oc0 = overturn_check(cleared, 0.15, -0.1)
        self.assertEqual(r["steps"][0]["margin"], oc0["margin"])
        self.assertEqual(r["steps"][0]["totalWeight"], oc0["totalWeight"])


class DoorPassageTests(unittest.TestCase):
    """连续窑门通行试算：横向外廓 + 门序可达闭区间递推 + 回溯位置序列。"""

    def _rack(self, boards=None, car_w=1000, heights=None):
        """构造台面宽 car_w、各层棚板 (x,w)（d 给 100）的简化架体。"""
        boards = boards if boards is not None else [(0, 1000), (100, 800)]
        heights = heights if heights is not None else [300] * len(boards)
        return {
            "car": {"w": car_w, "d": 100},
            "layers": [
                {
                    "name": f"L{i}",
                    "height": heights[i],
                    "board": {"x": x, "y": 0, "w": w, "d": 100, "weight": 10},
                    "wareWeight": 0,
                    "posts": [],
                }
                for i, (x, w) in enumerate(boards)
            ],
        }

    def test_profile_uses_car_and_boards_sum_heights(self):
        state = self._rack(boards=[(0, 1000), (100, 800)], heights=[320, 300])
        r = door_passage(state, [])
        self.assertTrue(r["evaluable"])
        self.assertEqual(r["profile"]["totalHeight"], 620.0)
        self.assertEqual(r["profile"]["envelope"],
                         {"left": 0.0, "right": 1000.0, "width": 1000.0})
        kinds = [(s["kind"], s["z"], s["left"], s["right"])
                 for s in r["profile"]["slabs"]]
        self.assertEqual(kinds, [
            ("car", 0.0, 0.0, 1000.0),
            ("board", 320.0, 0.0, 1000.0),
            ("board", 620.0, 100.0, 900.0),
        ])

    def test_single_centered_door_passable_with_clearances(self):
        # 外廓 [0,1000]；门中心 500、宽 1100 → 开口 [-50,1050] → 区间 [-50,50]
        # 首段 m=100，首门前 {0}，可达 [-50,50]；回溯取最接近 0 → 0
        r = door_passage(
            self._rack(),
            [{"center": 500, "width": 1100, "height": 700, "maxShift": 100}],
        )
        self.assertTrue(r["passable"])
        d = r["doors"][0]
        self.assertEqual(d["interval"], {"lo": -50.0, "hi": 50.0})
        self.assertEqual(d["reachable"], {"lo": -50.0, "hi": 50.0})
        self.assertEqual(d["chosenShift"], 0.0)
        self.assertEqual(d["move"], 0.0)
        self.assertEqual(d["clearance"], {"left": 50.0, "right": 50.0, "top": 100.0})
        self.assertEqual(r["positions"][0]["shift"], 0.0)

    def test_height_shortage_blocks_directly(self):
        r = door_passage(
            self._rack(heights=[300, 300]),  # 总高 600
            [{"center": 500, "width": 2000, "height": 599, "maxShift": 100}],
        )
        self.assertFalse(r["passable"])
        self.assertEqual(r["blockedAt"], 0)
        self.assertEqual(r["blockReason"], "height")
        d = r["doors"][0]
        self.assertFalse(d["heightOk"])
        self.assertIsNone(d["interval"])
        self.assertEqual(d["conflict"]["shortage"], 1.0)

    def test_exact_height_and_touching_edge_count_as_passable(self):
        # 净高恰等于最高点、区间恰好接触（闭区间）即可通过
        r = door_passage(
            self._rack(),
            [{"center": 500, "width": 1000, "height": 600, "maxShift": 0}],
        )
        self.assertTrue(r["passable"])
        d = r["doors"][0]
        self.assertEqual(d["interval"], {"lo": 0.0, "hi": 0.0})
        self.assertEqual(d["reachable"], {"lo": 0.0, "hi": 0.0})
        self.assertEqual(d["clearance"]["top"], 0.0)
        self.assertEqual(d["clearance"]["left"], 0.0)
        self.assertEqual(d["clearance"]["right"], 0.0)

    def test_width_insufficient_blocks_even_alone(self):
        # 外廓宽 1000，门宽 900 → 区间下界比上界大 100，单门宽度不足
        r = door_passage(
            self._rack(),
            [{"center": 500, "width": 900, "height": 700, "maxShift": 100}],
        )
        self.assertFalse(r["passable"])
        self.assertEqual(r["blockedAt"], 0)
        self.assertEqual(r["blockReason"], "width")
        self.assertTrue(r["doors"][0]["intervalEmpty"])
        self.assertEqual(r["doors"][0]["conflict"]["shortage"], 100.0)

    def test_each_door_fine_but_sequence_unreachable(self):
        # 门1：中心 500、宽 1100，区间 [-50,50]，首段 m=0 → 只能停在 0。
        # 门2：中心 700、宽 1100（开口 150..1250），区间 [150,250]；
        # 本段 m=100 把 {0} 扩张为 [-100,100]，与 [150,250] 无交集 → 阻断。
        # 两门单独评估都有非空区间，但连续序列不存在。
        r = door_passage(
            self._rack(),
            [
                {"center": 500, "width": 1100, "height": 700, "maxShift": 0},
                {"center": 700, "width": 1100, "height": 700, "maxShift": 100},
            ],
        )
        self.assertTrue(r["doors"][0]["passable"])
        self.assertEqual(r["doors"][0]["reachable"], {"lo": 0.0, "hi": 0.0})
        self.assertFalse(r["passable"])
        self.assertEqual(r["blockedAt"], 1)
        self.assertEqual(r["blockReason"], "reach")
        c = r["doors"][1]["conflict"]
        self.assertEqual(c["doorInterval"], {"lo": 150.0, "hi": 250.0})
        self.assertEqual(c["reachExpanded"], {"lo": -100.0, "hi": 100.0})
        self.assertEqual(c["shortage"], 50.0)
        self.assertEqual(c["side"], "right")

    def test_backtrack_position_sequence_respects_segment_limits(self):
        # 门1 区间 [-50,50]、m=100 → 可达 [-50,50]
        # 门2 区间 [-130,-30]、m=100 → 扩张 [-150,150]，可达 [-130,-30]
        # 回溯：门2 取最接近 0 = -30；门1 取可达区间内最接近 -30 = -30；
        # 首门由 0 移动 -30（≤100），门2 移动 0。
        r = door_passage(
            self._rack(),
            [
                {"center": 500, "width": 1100, "height": 700, "maxShift": 100},
                {"center": 420, "width": 1100, "height": 700, "maxShift": 100},
            ],
        )
        self.assertTrue(r["passable"])
        self.assertEqual([p["shift"] for p in r["positions"]], [-30.0, -30.0])
        self.assertEqual([p["move"] for p in r["positions"]], [-30.0, 0.0])
        # 门2 净空：开口 [-130,970]；外廓移到 [-30,970] → 左 100、右 0
        self.assertEqual(r["positions"][1]["clearance"]["left"], 100.0)
        self.assertEqual(r["positions"][1]["clearance"]["right"], 0.0)

    def test_backtrack_forces_move_when_next_door_farther(self):
        # 门1 可达 [-50,50]（m=100）；门2 开口 [-100,940]（宽 1040）：
        # 区间 [-100,-60]，m=30 扩张为 [-80,80]，交集 [-80,-60]。
        # 门2 取最接近 0 = -60；门1 clamp(-60,-50,50)=-50；移动 -50 ≤ 100，
        # 门2 再移动 -10（|−10|≤30）。
        r = door_passage(
            self._rack(),
            [
                {"center": 500, "width": 1100, "height": 700, "maxShift": 100},
                {"center": 420, "width": 1040, "height": 700, "maxShift": 30},
            ],
        )
        self.assertTrue(r["passable"])
        self.assertEqual([p["shift"] for p in r["positions"]], [-50.0, -60.0])
        self.assertEqual([p["move"] for p in r["positions"]], [-50.0, -10.0])

    def test_multi_level_envelope_intersection_narrows_interval(self):
        # 台面/底板 [0,1000]，顶板 [200,700]；门开口 [100,900]：
        # 台面要求 s∈[100,-100] 已为空 → 阻断，换一扇足够宽但偏置的门：
        # 开口 [50,1050] → 台面 [50,50]，顶板 [50-200=-150, 1050-700=350]
        # 交集 [50,50]，说明受台面（更宽的外廓件）约束。
        r = door_passage(
            self._rack(boards=[(0, 1000), (200, 500)]),
            [{"center": 550, "width": 1000, "height": 700, "maxShift": 100}],
        )
        self.assertTrue(r["passable"])
        self.assertEqual(r["doors"][0]["reachable"], {"lo": 50.0, "hi": 50.0})
        self.assertEqual(r["doors"][0]["chosenShift"], 50.0)

    def test_first_door_reach_bounded_by_segment_shift(self):
        # 本门区间 [150,150]，但首段最大横移仅 30 → 扩张 [-30,30]，无交集
        r = door_passage(
            self._rack(),
            [{"center": 650, "width": 1000, "height": 700, "maxShift": 30}],
        )
        self.assertFalse(r["passable"])
        self.assertEqual(r["blockReason"], "reach")
        self.assertEqual(r["doors"][0]["conflict"]["shortage"], 120.0)

    def test_empty_doors_is_passable_with_note(self):
        r = door_passage(self._rack(), [])
        self.assertTrue(r["evaluable"])
        self.assertTrue(r["passable"])
        self.assertEqual(r["doorCount"], 0)
        self.assertIsNotNone(r["note"])
        self.assertIsNone(r["positions"])

    def test_empty_rack_not_evaluable(self):
        r = door_passage({"car": {"w": 1000}, "layers": []}, [])
        self.assertFalse(r["evaluable"])
        self.assertIn("没有任何层", r["reason"])

    def test_later_doors_marked_unevaluated_after_block(self):
        r = door_passage(
            self._rack(heights=[300, 300]),
            [
                {"center": 500, "width": 900, "height": 700, "maxShift": 0},   # 宽度阻断
                {"center": 500, "width": 2000, "height": 700, "maxShift": 0},
            ],
        )
        self.assertEqual(r["blockedAt"], 0)
        self.assertFalse(r["doors"][0]["passable"])
        self.assertFalse(r["doors"][1]["evaluated"])

    def test_invalid_params_raise(self):
        state = self._rack()
        with self.assertRaises(ValueError):
            door_passage(state, "x")
        with self.assertRaises(ValueError):
            door_passage(state, [{"center": "a", "width": 1, "height": 1, "maxShift": 0}])
        with self.assertRaises(ValueError):
            door_passage(state, [{"center": 0, "width": 0, "height": 1, "maxShift": 0}])
        with self.assertRaises(ValueError):
            door_passage(state, [{"center": 0, "width": 1, "height": 0, "maxShift": 0}])
        with self.assertRaises(ValueError):
            door_passage(state, [{"center": 0, "width": 1, "height": 1, "maxShift": -1}])
        with self.assertRaises(ValueError):
            door_passage(state, [{"center": 0, "width": 1, "height": float("inf"), "maxShift": 0}])

    def test_does_not_mutate_input(self):
        with open(SAMPLE, encoding="utf-8") as f:
            state = json.load(f)
        doors = [
            {"center": 650, "width": 1400, "height": 970, "maxShift": 100},
            {"center": 570, "width": 1400, "height": 960, "maxShift": 100},
        ]
        before = json.loads(json.dumps({"state": state, "doors": doors}, ensure_ascii=False))
        r = door_passage(state, doors)
        self.assertTrue(r["passable"])
        self.assertEqual({"state": state, "doors": doors}, before)

    def test_sample_state_passable(self):
        with open(SAMPLE, encoding="utf-8") as f:
            state = json.load(f)
        r = door_passage(
            state,
            [
                {"center": 650, "width": 1400, "height": 970, "maxShift": 100},
                {"center": 570, "width": 1400, "height": 960, "maxShift": 100},
            ],
        )
        self.assertTrue(r["passable"])
        self.assertEqual(r["profile"]["totalHeight"], 960.0)
        self.assertEqual([p["shift"] for p in r["positions"]], [-30.0, -30.0])


class WheelLoadTrialTests(unittest.TestCase):
    """轮组承载试算：底层非悬空立柱点载荷 + 矩形四轮双向杠杆分配。"""

    def _params(self, **kw):
        p = dict(
            xLeft=100, xRight=900, yFront=100, yRear=900,
            ratedFL=100, ratedFR=100, ratedRL=100, ratedRR=100,
        )
        p.update(kw)
        return p

    def _single(self, x=500, y=500, load=100, car=(1000, 1000)):
        # 单根柱承载 load（板自重 load、无坯件）
        return {
            "car": {"w": car[0], "d": car[1]},
            "layers": [
                {
                    "name": "底",
                    "board": {"x": 0, "y": 0, "w": car[0], "d": car[1], "weight": load},
                    "wareWeight": 0,
                    "posts": [{"id": "A", "x": x, "y": y, "capacity": 0}],
                }
            ],
        }

    def test_centered_point_even_fourth(self):
        r = wheel_load_trial(self._single(500, 500, 400), self._params())
        self.assertTrue(r["evaluable"])
        self.assertEqual([w["reaction"] for w in r["wheels"]], [100.0] * 4)
        for w in r["wheels"]:
            self.assertFalse(w["overloaded"])
            self.assertFalse(w["negative"])
            self.assertEqual(w["utilization"], 1.0)
        self.assertTrue(r["checks"]["allOk"])
        self.assertEqual(r["totalLoad"], 400.0)
        self.assertEqual(r["pointLoadCount"], 1)

    def test_offcenter_bilinear_distribution_and_checks(self):
        # 点 (700,300)：wR=0.75 wL=0.25；wF=0.75 wB=0.25
        r = wheel_load_trial(self._single(700, 300, 100), self._params())
        w = {q["key"]: q["reaction"] for q in r["wheels"]}
        self.assertAlmostEqual(w["FL"], 100 * 0.25 * 0.75, places=9)
        self.assertAlmostEqual(w["FR"], 100 * 0.75 * 0.75, places=9)
        self.assertAlmostEqual(w["RL"], 100 * 0.25 * 0.25, places=9)
        self.assertAlmostEqual(w["RR"], 100 * 0.75 * 0.25, places=9)
        c = r["checks"]
        self.assertTrue(c["allOk"])
        self.assertEqual(c["force"]["residual"], 0.0)
        # 绕前轮轴：点载荷 100·(300−100)=20000
        self.assertEqual(c["momentX"]["loadMoment"], 20000.0)
        # 绕左轮轴：100·(700−100)=60000
        self.assertEqual(c["momentY"]["loadMoment"], 60000.0)

    def test_corner_point_loads_only_one_wheel(self):
        # 点恰落在左前轮位 → FL=100，其余 0（不视为负反力）
        r = wheel_load_trial(self._single(100, 100, 100), self._params())
        w = {q["key"]: q["reaction"] for q in r["wheels"]}
        self.assertEqual(w, {"FL": 100.0, "FR": 0.0, "RL": 0.0, "RR": 0.0})
        self.assertTrue(r["checks"]["allOk"])
        self.assertFalse(any(q["negative"] for q in r["wheels"]))
        self.assertEqual(r["overhangCount"], 0)

    def test_extrapolation_keeps_negative_reactions(self):
        # 点 (0,0) 越出轮组矩形 [100,900]²：两个远端轮反力为负，绝不截零
        r = wheel_load_trial(self._single(0, 0, 100), self._params())
        w = {q["key"]: q for q in r["wheels"]}
        self.assertGreater(w["FL"]["reaction"], 100.0)
        self.assertLess(w["FR"]["reaction"], 0.0)
        self.assertLess(w["RL"]["reaction"], 0.0)
        self.assertGreater(w["RR"]["reaction"], 0.0)
        self.assertTrue(w["FL"]["overloaded"])   # 额定 100
        self.assertTrue(w["FR"]["negative"])
        self.assertTrue(w["RL"]["negative"])
        self.assertEqual(r["overhangCount"], 1)
        # 外推后力与力矩仍然平衡
        self.assertTrue(r["checks"]["allOk"])
        risk_types = {q["type"] for q in r["risks"]}
        self.assertIn("negative-reaction", risk_types)
        self.assertIn("wheel-overload", risk_types)
        self.assertIn("extrapolation", risk_types)

    def test_overload_flag_without_negative(self):
        # 点在轮位上，额定 80 < 100：超载但无负反力
        r = wheel_load_trial(
            self._single(100, 100, 100), self._params(ratedFL=80)
        )
        w = {q["key"]: q for q in r["wheels"]}
        self.assertTrue(w["FL"]["overloaded"])
        self.assertGreater(w["FL"]["utilization"], 1.0)
        self.assertFalse(any(q["negative"] for q in r["wheels"]))

    def test_overlapping_landings_listed_separately_but_merged(self):
        state = {
            "car": {"w": 1000, "d": 1000},
            "layers": [
                {
                    "name": "底",
                    "board": {"x": 0, "y": 0, "w": 1000, "d": 1000, "weight": 0},
                    "wareWeight": 100,
                    "posts": [
                        {"id": "A", "x": 498, "y": 500, "capacity": 0},
                        {"id": "B", "x": 503, "y": 500, "capacity": 0},
                    ],
                }
            ],
        }
        r = wheel_load_trial(state, self._params())
        self.assertTrue(r["evaluable"])
        self.assertEqual(r["pointLoadCount"], 2)
        self.assertTrue(all(p["overlap"] for p in r["pointLoads"]))
        # 两根柱 y=500（前后各半）；左列份额 = 50·(0.5025+0.49625)=49.9375。
        # 逐轮反力保留两位小数，合计有约 0.01 kg 的展示舍入。
        w = {q["key"]: q["reaction"] for q in r["wheels"]}
        self.assertAlmostEqual(w["FL"] + w["RL"], 50.0 * (0.5025 + 0.49625), places=2)
        self.assertAlmostEqual(w["FR"] + w["RR"], 50.0 * (0.4975 + 0.50375), places=2)
        self.assertAlmostEqual(w["FL"] + w["FR"], 50.0, places=2)
        self.assertAlmostEqual(w["RL"] + w["RR"], 50.0, places=2)
        # 内部平衡核对用未截断精确值
        self.assertTrue(r["checks"]["allOk"])
        self.assertEqual(r["checks"]["force"]["residual"], 0.0)

    def test_hanging_posts_excluded(self):
        # 台面 1000×1000，唯一柱 (2000,2000) 悬空：无点载荷、无法试算
        state = {
            "car": {"w": 1000, "d": 1000},
            "layers": [
                {
                    "name": "底",
                    "board": {"x": 0, "y": 0, "w": 100, "d": 100, "weight": 100},
                    "wareWeight": 0,
                    "posts": [{"id": "H", "x": 2000, "y": 2000, "capacity": 0}],
                }
            ],
        }
        r = wheel_load_trial(state, self._params())
        self.assertFalse(r["evaluable"])
        self.assertEqual(r["pointLoadCount"], 0)
        self.assertIn("实载合计为 0", r["reason"])
        # 悬空 + 有效柱：只保留有效柱
        state["layers"][0]["posts"].append(
            {"id": "A", "x": 500, "y": 500, "capacity": 0}
        )
        r2res = wheel_load_trial(state, self._params())
        self.assertTrue(r2res["evaluable"])
        self.assertEqual([p["post"] for p in r2res["pointLoads"]], ["A"])

    def test_no_layer_not_evaluable(self):
        r = wheel_load_trial(
            {"car": {"w": 1000, "d": 1000}, "layers": []}, self._params()
        )
        self.assertFalse(r["evaluable"])
        self.assertIn("没有任何层", r["reason"])

    def test_zero_load_not_evaluable(self):
        state = {
            "car": {"w": 1000, "d": 1000},
            "layers": [
                {
                    "name": "底",
                    "board": {"x": 0, "y": 0, "w": 1000, "d": 1000, "weight": 0},
                    "wareWeight": 0,
                    "posts": [{"id": "A", "x": 500, "y": 500, "capacity": 0}],
                }
            ],
        }
        r = wheel_load_trial(state, self._params())
        self.assertFalse(r["evaluable"])
        self.assertIn("正载荷", r["reason"])

    def test_invalid_track_and_params(self):
        state = self._single()
        with self.assertRaises(ValueError):
            wheel_load_trial(state, self._params(xLeft=900))       # xLeft==xRight
        with self.assertRaises(ValueError):
            wheel_load_trial(state, self._params(xLeft=950))       # xLeft>xRight
        with self.assertRaises(ValueError):
            wheel_load_trial(state, self._params(yRear=100))       # yFront==yRear
        with self.assertRaises(ValueError):
            wheel_load_trial(state, self._params(yFront=950))      # 前后颠倒
        with self.assertRaises(ValueError):
            wheel_load_trial(state, self._params(xLeft=-1))        # 越出台面
        with self.assertRaises(ValueError):
            wheel_load_trial(state, self._params(xRight=1001))     # 超出台面宽
        with self.assertRaises(ValueError):
            wheel_load_trial(state, self._params(yRear=1001))      # 超出台面深
        with self.assertRaises(ValueError):
            wheel_load_trial(state, self._params(ratedFL=0))       # 额定非正
        with self.assertRaises(ValueError):
            wheel_load_trial(state, self._params(ratedRR=-5))
        with self.assertRaises(ValueError):
            wheel_load_trial(state, self._params(xLeft="abc"))    # 非数值
        with self.assertRaises(ValueError):
            wheel_load_trial(state, self._params(xRight=float("inf")))
        with self.assertRaises(ValueError):
            wheel_load_trial(state, self._params(ratedFR=None))   # 缺参数（默认覆盖）

    def test_wheels_object_param_form(self):
        state = self._single(500, 500, 400)
        params = {
            "wheels": {
                "FL": {"x": 100, "y": 100, "rating": 100},
                "FR": {"x": 900, "y": 100, "rating": 100},
                "RL": {"x": 100, "y": 900, "rating": 100},
                "RR": {"x": 900, "y": 900, "rating": 100},
            }
        }
        r = wheel_load_trial(state, params)
        self.assertTrue(r["evaluable"])
        self.assertEqual([w["reaction"] for w in r["wheels"]], [100.0] * 4)

    def test_multi_points_equilibrium(self):
        # 多点任意布置：反力之和与双向力矩恒等于点载荷侧（双线性性质）
        import random
        rng = random.Random(42)
        state = {
            "car": {"w": 1000, "d": 1000},
            "layers": [
                {
                    "name": "底",
                    "board": {"x": 0, "y": 0, "w": 1000, "d": 1000, "weight": 0},
                    "wareWeight": 500,
                    "posts": [
                        {"id": f"P{i}", "x": rng.uniform(0, 1000),
                         "y": rng.uniform(0, 1000), "capacity": 0}
                        for i in range(7)
                    ],
                }
            ],
        }
        r = wheel_load_trial(state, self._params(
            xLeft=200, xRight=800, yFront=150, yRear=850))
        self.assertTrue(r["evaluable"])
        self.assertTrue(r["checks"]["allOk"])
        self.assertEqual(r["checks"]["force"]["residual"], 0.0)

    def test_sample_state_trial(self):
        with open(SAMPLE, encoding="utf-8") as f:
            state = json.load(f)
        r = wheel_load_trial(
            state,
            self._params(
                xLeft=150, xRight=1150, yFront=150, yRear=750,
                ratedFL=500, ratedFR=500, ratedRL=500, ratedRR=500,
            ),
        )
        self.assertTrue(r["evaluable"])
        self.assertEqual(r["totalLoad"], 1980.0)
        self.assertEqual([w["reaction"] for w in r["wheels"]], [495.0] * 4)
        self.assertTrue(r["checks"]["allOk"])
        self.assertFalse(r["risks"])

    def test_does_not_mutate_input(self):
        state = self._single()
        before = json.loads(json.dumps(state, ensure_ascii=False))
        wheel_load_trial(state, self._params())
        self.assertEqual(state, before)


if __name__ == "__main__":
    unittest.main()
