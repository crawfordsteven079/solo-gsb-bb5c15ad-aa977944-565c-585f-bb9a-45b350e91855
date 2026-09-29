# -*- coding: utf-8 -*-
"""架体版本（乐观并发控制）服务端测试。

覆盖需求：
- 读取（GET /api/state）通过 X-Rack-Version 头返回版本，架体 JSON 结构不变；
- 保存（PUT /api/state）携带版本时服务端在一次写入中核对版本、保存完整架体，
  匹配才提交并更新版本；
- 版本冲突（409）拒绝旧写入、不改动磁盘，返回最新已保存架体、版本与重算结果；
- 不带版本的保存仍按旧语义直接覆盖，但版本照样递增；
- 重置示例（POST /api/reset）也更新版本，重置前发出的旧版本保存不能事后覆盖它；
- 升级前的裸架体 state.json（旧格式）首次读取即迁移为 version=1，内容不变。

运行：python -m pytest -q 或 python -m unittest tests.test_server_version
"""

import json
import os
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.server as server  # noqa: E402

SAMPLE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data",
    "sample_state.json",
)
VERSION_HEADER = "X-Rack-Version"


def _rack(w=100, d=100, ware=0):
    """构造一个最小合法架体（与读取到的 JSON 同构，不含任何版本字段）。"""
    return {
        "car": {"name": "窑车台面", "w": w, "d": d},
        "layers": [
            {
                "id": "L1",
                "name": "第一层",
                "height": 300,
                "board": {"x": 0, "y": 0, "w": w, "d": d, "weight": 100},
                "wareWeight": ware,
                "posts": [
                    {"id": "P1", "x": 10, "y": 10, "capacity": 0},
                ],
            }
        ],
    }


class VersionedStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # 让服务端的路径常量指向临时目录
        self._orig = (server.DATA_DIR, server.STATE_FILE, server.SAMPLE_FILE)
        server.DATA_DIR = self.tmp.name
        server.STATE_FILE = os.path.join(self.tmp.name, "state.json")
        server.SAMPLE_FILE = os.path.join(self.tmp.name, "sample_state.json")
        with open(server.SAMPLE_FILE, "w", encoding="utf-8") as f:
            json.dump(_rack(1300, 900, 50), f)
        self.client = server.app.test_client()

    def tearDown(self):
        server.DATA_DIR, server.STATE_FILE, server.SAMPLE_FILE = self._orig

    # ---- 初始化 / 读取 ----
    def test_get_initializes_and_returns_version_header(self):
        r = self.client.get("/api/state")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers.get(VERSION_HEADER), "1")
        body = r.get_json()
        # 架体 JSON 结构保持兼容：直接就是架体（car/layers），不含版本包装
        self.assertIn("car", body)
        self.assertIn("layers", body)
        self.assertNotIn("version", body)
        self.assertNotIn("state", body)
        disk = json.load(open(server.STATE_FILE, encoding="utf-8"))
        self.assertEqual(disk, {"version": 1, "state": body})

    def test_legacy_bare_state_migrates_to_version_1(self):
        # 升级前遗留的裸架体文件（无版本包装）
        legacy = _rack(777, 666, 12)
        with open(server.STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(legacy, f)
        r = self.client.get("/api/state")
        self.assertEqual(r.headers.get(VERSION_HEADER), "1")
        self.assertEqual(r.get_json(), legacy)  # 架体内容原样保留
        disk = json.load(open(server.STATE_FILE, encoding="utf-8"))
        self.assertEqual(disk, {"version": 1, "state": legacy})

    # ---- 保存：带版本的 CAS ----
    def test_put_with_matching_version_commits_and_bumps(self):
        self.assertEqual(self.client.get("/api/state").headers[VERSION_HEADER], "1")
        new = _rack(200, 200, 5)
        r = self.client.put(
            "/api/state", json=new, headers={VERSION_HEADER: "1"}
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers[VERSION_HEADER], "2")
        data = r.get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["version"], 2)
        self.assertIn("result", data)
        # 完整架体已保存
        disk = json.load(open(server.STATE_FILE, encoding="utf-8"))
        self.assertEqual(disk, {"version": 2, "state": new})
        self.assertEqual(self.client.get("/api/state").headers[VERSION_HEADER], "2")

    def test_put_with_stale_version_rejected_returns_latest(self):
        self.client.put(
            "/api/state", json=_rack(200, 200), headers={VERSION_HEADER: "1"}
        )  # -> v2
        stale = _rack(999, 999, 99)
        r = self.client.put(
            "/api/state", json=stale, headers={VERSION_HEADER: "1"}
        )
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.headers[VERSION_HEADER], "2")
        data = r.get_json()
        self.assertFalse(data["ok"])
        self.assertTrue(data["conflict"])
        self.assertEqual(data["version"], 2)
        self.assertEqual(data["state"]["car"]["w"], 200)  # 最新已保存架体
        self.assertIn("result", data)                    # 最新架体重算结果
        # 磁盘未被旧写入改动
        disk = json.load(open(server.STATE_FILE, encoding="utf-8"))
        self.assertEqual(disk["version"], 2)
        self.assertEqual(disk["state"]["car"]["w"], 200)
        self.assertNotIn("999", json.dumps(disk, ensure_ascii=False))

    def test_then_matching_latest_version_succeeds(self):
        self.client.put("/api/state", json=_rack(200), headers={VERSION_HEADER: "1"})
        # 用冲突响应里拿到的最新版本重试 → 成功（页面放弃/合并后再保存的路径）
        r = self.client.put(
            "/api/state", json=_rack(300), headers={VERSION_HEADER: "2"}
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers[VERSION_HEADER], "3")

    # ---- 无版本旧调用兼容 ----
    def test_put_without_version_keeps_old_semantics_but_bumps(self):
        r1 = self.client.put("/api/state", json=_rack(200))
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r1.headers[VERSION_HEADER], "2")
        # 当前已是 v2（例如被别的页面/重置更新过），无版本 PUT 依旧直接覆盖且 +1
        r2 = self.client.put("/api/state", json=_rack(300))
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(r2.headers[VERSION_HEADER], "3")
        disk = json.load(open(server.STATE_FILE, encoding="utf-8"))
        self.assertEqual(disk["version"], 3)
        self.assertEqual(disk["state"]["car"]["w"], 300)

    def test_put_with_blank_version_header_treated_as_unversioned(self):
        r = self.client.put(
            "/api/state", json=_rack(200), headers={VERSION_HEADER: "  "}
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers[VERSION_HEADER], "2")

    def test_put_with_non_positive_integer_version_header_400(self):
        for bad in ("abc", "1.5", "-3", "0"):
            r = self.client.put(
                "/api/state", json=_rack(), headers={VERSION_HEADER: bad}
            )
            self.assertEqual(r.status_code, 400, msg=bad)
            self.assertIn("error", r.get_json())

    def test_put_non_object_body_still_400(self):
        r = self.client.put("/api/state", data="[]", content_type="application/json")
        self.assertEqual(r.status_code, 400)

    # ---- 重置也更新版本 ----
    def test_reset_bumps_version_and_restores_sample(self):
        self.client.put("/api/state", json=_rack(200))  # v1 -> v2
        self.client.put("/api/state", json=_rack(201))  # v2 -> v3
        r = self.client.post("/api/reset")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers[VERSION_HEADER], "4")  # 单调递增，不回到 1
        data = r.get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["version"], 4)
        self.assertEqual(data["state"], _rack(1300, 900, 50))
        self.assertIn("result", data)
        disk = json.load(open(server.STATE_FILE, encoding="utf-8"))
        self.assertEqual(disk["version"], 4)
        self.assertEqual(disk["state"], _rack(1300, 900, 50))

    def test_save_issued_before_reset_cannot_overwrite_afterward(self):
        # 页面 A 在 v1 上准备保存（尚未到达）；期间发生重置 -> v2
        self.client.post("/api/reset")
        # 重置后，A 那个带着旧版本 v1 的请求才到达 → 必须被拒，示例不被覆盖
        r = self.client.put(
            "/api/state", json=_rack(555, 555), headers={VERSION_HEADER: "1"}
        )
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.get_json()["version"], 2)
        disk = json.load(open(server.STATE_FILE, encoding="utf-8"))
        self.assertEqual(disk["version"], 2)
        self.assertEqual(disk["state"]["car"]["w"], 1300)  # 仍是重置后的示例

    def test_repeated_reset_keeps_monotonic_version(self):
        self.assertEqual(self.client.post("/api/reset").headers[VERSION_HEADER], "2")
        self.assertEqual(self.client.post("/api/reset").headers[VERSION_HEADER], "3")

    # ---- 只读结果接口 ----
    def test_result_endpoint_carries_version_header(self):
        self.client.put("/api/state", json=_rack(200), headers={VERSION_HEADER: "1"})
        r = self.client.get("/api/result")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers[VERSION_HEADER], "2")
        self.assertIn("totals", r.get_json())

    def test_read_only_apis_do_not_change_version(self):
        self.client.post("/api/recalc", json=_rack(42))
        self.client.post("/api/simulate", json={"state": _rack(), "failed": []})
        self.client.post("/api/load-shed", json={"state": _rack()})
        self.assertEqual(
            self.client.get("/api/state").headers[VERSION_HEADER], "1"
        )

    # ---- 并发：锁内 CAS 不会互相覆盖 ----
    def test_concurrent_cas_only_one_wins(self):
        outcomes = []

        def save(tag):
            c = server.app.test_client()
            r = c.put(
                "/api/state",
                json=_rack(100 + tag, 100 + tag),
                headers={VERSION_HEADER: "1"},
            )
            outcomes.append((tag, r.status_code, r.headers[VERSION_HEADER]))

        threads = [threading.Thread(target=save, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        ok = [o for o in outcomes if o[1] == 200]
        conflicts = [o for o in outcomes if o[1] == 409]
        self.assertEqual(len(ok), 1)          # 只有一个 v1 保存能提交
        self.assertEqual(len(conflicts), 7)   # 其余全部 409
        self.assertEqual(ok[0][2], "2")
        disk = json.load(open(server.STATE_FILE, encoding="utf-8"))
        self.assertEqual(disk["version"], 2)


if __name__ == "__main__":
    unittest.main()
