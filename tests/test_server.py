# -*- coding: utf-8 -*-
"""架体版本（乐观锁）HTTP 语义测试：python -m pytest -q 或 python tests/test_server.py

依赖 Flask（已在 requirements.txt）；未安装时整体跳过，不影响
`python tests/test_calc.py` 的纯标准库核心算法测试。
"""

import importlib.util
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

HAS_FLASK = importlib.util.find_spec("flask") is not None


def _sample_path():
    return os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "data",
        "sample_state.json",
    )


@unittest.skipUnless(HAS_FLASK, "未安装 Flask，跳过服务端版本测试")
class VersionLockTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp(prefix="kiln-state-")
        shutil.copy(_sample_path(), os.path.join(cls.tmpdir, "sample_state.json"))
        os.environ["KILN_DATA_DIR"] = cls.tmpdir
        from app import server  # noqa: E402

        cls.server = server
        cls.client = server.app.test_client()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def _get(self):
        r = self.client.get("/api/state")
        self.assertEqual(r.status_code, 200)
        return r.get_json(), int(r.headers["X-Frame-Version"])

    def test_get_exposes_version_headers(self):
        r = self.client.get("/api/state")
        self.assertIn("X-Frame-Version", r.headers)
        self.assertEqual(r.headers["ETag"], f'"{r.headers["X-Frame-Version"]}"')
        self.assertGreaterEqual(int(r.headers["X-Frame-Version"]), 1)

        rr = self.client.get("/api/result")
        self.assertEqual(rr.status_code, 200)
        self.assertIn("X-Frame-Version", rr.headers)

    def test_put_with_matching_version_commits_and_bumps(self):
        state, v = self._get()
        r = self.client.put(
            "/api/state", json=state, headers={"If-Match": f'"{v}"'}
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["version"], v + 1)
        self.assertEqual(int(r.headers["X-Frame-Version"]), v + 1)

    def test_put_with_stale_version_rejected_returns_latest(self):
        state, v = self._get()
        # 先用正确版本写一次，制造更新的已保存版本
        changed = json.loads(json.dumps(state))
        changed["car"]["w"] = changed["car"]["w"] + 1
        r = self.client.put(
            "/api/state", json=changed, headers={"If-Match": f'"{v}"'}
        )
        self.assertEqual(r.status_code, 200)
        new_v = r.get_json()["version"]

        # 携带旧版本的写入必须被 409 拒绝，且不写盘
        stale = json.loads(json.dumps(state))
        stale["car"]["w"] = 1
        r = self.client.put(
            "/api/state", json=stale, headers={"If-Match": f'"{v}"'}
        )
        self.assertEqual(r.status_code, 409)
        body = r.get_json()
        self.assertTrue(body["conflict"])
        self.assertEqual(body["version"], new_v)
        self.assertEqual(body["state"]["car"]["w"], changed["car"]["w"])
        self.assertIn("result", body)
        self.assertEqual(int(r.headers["X-Frame-Version"]), new_v)
        # 磁盘仍是先成功写入的那份
        on_disk, disk_v = self._get()
        self.assertEqual(on_disk["car"]["w"], changed["car"]["w"])
        self.assertEqual(disk_v, new_v)

    def test_put_legacy_without_version_still_works_and_bumps(self):
        _, v = self._get()
        r = self.client.put("/api/state", json={"car": {"w": 1, "d": 1}, "layers": []})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["version"], v + 1)

    def test_put_version_via_header_and_body_field(self):
        state, v = self._get()
        r = self.client.put(
            "/api/state", json=state, headers={"X-Frame-Version": str(v)}
        )
        self.assertEqual(r.status_code, 200)
        v = r.get_json()["version"]

        state, v2 = self._get()
        self.assertEqual(v2, v)
        payload = dict(state)
        payload["version"] = v2  # body 顶层版本字段
        r = self.client.put("/api/state", json=payload)
        self.assertEqual(r.status_code, 200)
        # version 字段不得落进架体 JSON 结构
        on_disk, _ = self._get()
        self.assertNotIn("version", on_disk)

    def test_put_bad_version_header_is_400(self):
        state, _ = self._get()
        r = self.client.put(
            "/api/state", json=state, headers={"If-Match": "not-a-number"}
        )
        self.assertEqual(r.status_code, 400)

    def test_if_match_star_forces_overwrite(self):
        state, v = self._get()
        r = self.client.put(
            "/api/state", json=state, headers={"If-Match": "*"}
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["version"], v + 1)

    def test_reset_always_bumps_and_blocks_pre_reset_save(self):
        state, v = self._get()
        r = self.client.post("/api/reset")
        self.assertEqual(r.status_code, 200)
        reset_v = r.get_json()["version"]
        self.assertGreater(reset_v, v)
        # 重置前发出（携带重置前版本）的保存事后到达 → 409，不能覆盖示例
        r = self.client.put(
            "/api/state", json=state, headers={"If-Match": f'"{v}"'}
        )
        self.assertEqual(r.status_code, 409)
        on_disk, disk_v = self._get()
        self.assertEqual(disk_v, reset_v)
        with open(_sample_path(), encoding="utf-8") as f:
            sample = json.load(f)
        self.assertEqual(on_disk, sample)

    def test_concurrent_puts_only_one_wins_per_version(self):
        # 恢复到一个已知版本
        r = self.client.post("/api/reset")
        v = r.get_json()["version"]
        state = r.get_json()["state"]

        results = []
        barrier = threading.Barrier(8)

        def worker(i):
            payload = json.loads(json.dumps(state))
            payload["car"]["name"] = f"worker-{i}"
            barrier.wait()
            rr = self.client.put(
                "/api/state", json=payload, headers={"If-Match": f'"{v}"'}
            )
            results.append(rr.status_code)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # 同版本并发：恰好一个 200，其余全部 409
        self.assertEqual(results.count(200), 1)
        self.assertEqual(results.count(409), 7)
        on_disk, disk_v = self._get()
        self.assertEqual(disk_v, v + 1)
        self.assertTrue(on_disk["car"]["name"].startswith("worker-"))


if __name__ == "__main__":
    unittest.main()
