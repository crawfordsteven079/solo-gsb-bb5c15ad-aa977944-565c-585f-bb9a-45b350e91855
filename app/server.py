# -*- coding: utf-8 -*-
"""Flask 服务：静态页面 + 状态持久化 API。

接口
----
GET  /api/state      读取当前架体（输入），首次启动自动载入示例数据；
                     响应头 ETag / X-Frame-Version 给出架体版本号
PUT  /api/state      保存编辑后的架体（JSON body）；可用 If-Match: "版本号"
                     （或 X-Frame-Version 头、body.version 字段）携带读取时版本，
                     服务端在一次加锁写入中核对：匹配才提交完整架体并把版本 +1，
                     不匹配返回 409 及最新已保存架体/版本（乐观锁）；
                     不带版本的调用沿用原语义（总是写入），但同样会推进版本
POST /api/recalc     对给定（或当前持久化）架体重算，返回完整结果
POST /api/simulate   立柱失效推演：body {"state": 架体JSON(可省), "failed": [{"layer": 0, "post": "P1"}]}，
                     返回基准/推演/差值；只计算，不写盘、不保存失效标记
POST /api/suggest    加柱建议：body {"state"(可省), "layer": 0, "spacing": 200, "capacity": 300}，
                     在目标层承托面内按候选网格逐点试算新增一根立柱，返回前 5 个方案；
                     只计算，不写盘；用户确认后由前端走 PUT /api/state 正式写入
POST /api/load-shed  减载配额试算：body {"state"(可省)}，只下调各层坯件重量，求消除
                     全部有限容量立柱超载的最小总减载量，按现有整架分摊规则约束并
                     重新整架校验；只计算，不写盘；用户确认后由前端走 PUT /api/state
                     把建议坯件重量正式写入
POST /api/overturn   层架抗倾覆试算：body {"state"(可省), "ax": 0.1, "ay": 0}，
                     按各层累计高度/棚板中心求整架重心，以底层非悬空立柱落点凸包
                     为支撑多边形，把重心投影按 (ax, ay)·重心高度平移后计算到凸包
                     边界的带符号最短距离，判定稳定/临界/倾覆；只计算，不写盘；
                     落点不足以形成非零面积或任一层无有效承托时返回 evaluable=false
POST /api/loading-sequence
                     逐层装货顺序规划：body {"state"(可省), "ax": 0.1, "ay": 0}，
                     保留全部棚板自重、坯件先全部置零，再把坯件重量大于零的层
                     （至多 12 层）按整层一次加入；对各已装子集求抗倾覆带符号裕量，
                     用动态规划求全过程最小裕量最大的装货顺序（同分按层序号字典序）；
                     只计算，不写盘；前置条件不足或候选层超过 12 时返回 planable=false
POST /api/door-passage
                     连续窑门通行试算：body {"state"(可省), "doors":
                     [{center,width,height,maxShift}, ...]}（按行进顺序，单位 mm），
                     由窑车台面与各层棚板求横向外廓、累加层高求最高点，逐门计算
                     可通过的横移闭区间（高度不足直接阻断），按门序用上一可达区间
                     扩张本段横移量后与本门区间求交，首次交集为空即阻断并给出冲突；
                     全程可达时回溯一条位置序列与逐门左/右/顶部净空；
                     只计算，不写盘；架体没有层时返回 evaluable=false，门参数非法返回 400
POST /api/reset      恢复内置示例数据；无论内容是否变化都推进版本号，
                     使重置前发出（携带旧版本）的保存不能事后覆盖重置结果
GET  /api/result     直接返回当前持久化架体的重算结果（同样带版本响应头）
POST /api/wheel-load 轮组承载试算：body {"state"(可省), "xLeft","xRight","yFront",
                     "yRear"(台面内四轮坐标 mm), "ratedFL/FR/RL/RR"(四轮额定载荷 kg)}
                     ——以底层非悬空立柱的落点与实载为点载荷，按矩形四轮的双向杠杆
                     （双线性）比例分配反力；落点越出轮组矩形时允许外推、负反力不
                     截零。逐轮返回反力、利用率、超载与负反力风险，并核对四轮反力
                     之和及绕 X/Y 轴力矩与输入点载荷一致；只计算，不写盘；轮距不正、
                     轮位越出台面、额定载荷非正返回 400，底层无正载荷时 evaluable=false
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from contextlib import contextmanager

try:  # Linux/macOS 上用文件锁兜底多进程；容器内为单 worker，线程锁即够用
    import fcntl
except ImportError:  # pragma: no cover - Windows 兜底
    fcntl = None

from flask import Flask, jsonify, request, send_from_directory

from .calc import (
    calculate,
    door_passage,
    load_shed,
    loading_sequence,
    overturn_check,
    simulate,
    suggest_posts,
    wheel_load_trial,
)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC_DIR = os.path.join(BASE_DIR, "static")
DATA_DIR = os.environ.get(
    "KILN_DATA_DIR", os.path.join(BASE_DIR, "data")
)
STATE_FILE = os.path.join(DATA_DIR, "state.json")
META_FILE = os.path.join(DATA_DIR, "state.meta.json")
LOCK_FILE = os.path.join(DATA_DIR, ".state.lock")
SAMPLE_FILE = os.path.join(DATA_DIR, "sample_state.json")

app = Flask(__name__, static_folder=None)

# 架体版本（乐观锁）：随每次成功的 PUT/重置单调 +1，与 state.json 分文件存放，
# 架体 JSON 自身结构（car/layers）保持不变。同一进程内用线程锁串行化，
# 锁文件保证同一台机器上多个进程（如本地直跑 + 容器）也不会交叉写入。
_state_lock = threading.RLock()


@contextmanager
def state_lock():
    """串行化「读版本 → 核对 → 写架体/版本」整段临界区。"""
    os.makedirs(DATA_DIR, exist_ok=True)
    with _state_lock:
        fh = open(LOCK_FILE, "a+")
        try:
            if fcntl is not None:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            fh.close()


def _read_json(path: str):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _atomic_write_json(path: str, payload) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _read_meta() -> dict:
    try:
        meta = _read_json(META_FILE)
        if isinstance(meta, dict):
            return meta
    except (OSError, ValueError):
        pass
    return {}


def _write_meta(meta: dict) -> None:
    _atomic_write_json(META_FILE, meta)


def load_version() -> int:
    """读取当前架体版本；元数据缺失/损坏（如老版本升级上来）时视为 1。"""
    version = _read_meta().get("version")
    return version if isinstance(version, int) and version >= 1 else 1


def _ensure_files() -> None:
    """调用方须已持有 state_lock：state.json 缺失则由示例生成，并补齐版本元数据。"""
    if not os.path.exists(STATE_FILE):
        _atomic_write_json(STATE_FILE, _read_json(SAMPLE_FILE))
    if not isinstance(_read_meta().get("version"), int):
        _write_meta({"version": 1})


def load_state():
    with state_lock():
        _ensure_files()
        return _read_json(STATE_FILE)


def load_state_with_version():
    with state_lock():
        _ensure_files()
        return _read_json(STATE_FILE), load_version()


def ensure_initialized() -> None:
    """容器/进程启动时完成初始化：状态文件缺失则立即由示例数据生成。

    依赖已在镜像构建时装好；这里保证服务一旦启动，data/state.json
    即存在且可读写，无需等待首次请求。
    """
    with state_lock():
        _ensure_files()


def commit_state(state_obj: dict, expected):
    """在一次加锁写入中核对版本并保存完整架体。

    expected 为 None（老调用方，不带版本）时按原语义总是写入；
    否则必须等于当前版本才提交。版本号先落盘再换架体：即便进程在两步之间
    崩溃，旧版本重试也只会得到 409（拒绝），绝不会把旧架体事后覆盖回去。

    返回 (ok, version, current_state)：ok=False 时 current_state 为最新已保存架体。
    """
    with state_lock():
        _ensure_files()
        current_version = load_version()
        if expected is not None and expected != current_version:
            return False, current_version, _read_json(STATE_FILE)
        # 先推进版本（带新号的 meta），再原子替换架体文件
        _write_meta({"version": current_version + 1})
        _atomic_write_json(STATE_FILE, state_obj)
        return True, current_version + 1, state_obj


def reset_to_sample():
    """恢复示例并推进版本（即使内容与当前相同），使重置前的旧版本保存无法覆盖。"""
    sample = _read_json(SAMPLE_FILE)
    with state_lock():
        _ensure_files()
        new_version = load_version() + 1
        _write_meta({"version": new_version})
        _atomic_write_json(STATE_FILE, sample)
        return sample, new_version


def _expected_version_from_request(payload: dict):
    """取调用方携带的读取时版本：If-Match（HTTP 标准）优先，其次 X-Frame-Version，
    再次 body.version；都没有则返回 None（老调用方，按原语义执行）。

    携带了但无法解析为正整数 → ValueError，由调用方返回 400。
    "*" 表示强制写入（不校验版本，等价于老语义）。
    """
    header = request.headers.get("If-Match")
    if header is not None:
        token = header.strip()
        if token == "*":
            return None
        # If-Match 允许多个 ETag：取第一个可解析的整数版本号
        for etag in (part.strip() for part in token.split(",")):
            m = re.fullmatch(r'"?(\d+)"?', etag)
            if m:
                return int(m.group(1))
        raise ValueError("If-Match 必须是 \"版本号\"（正整数）或 *")
    header = request.headers.get("X-Frame-Version")
    if header is not None:
        if not re.fullmatch(r"\d+", header.strip()):
            raise ValueError("X-Frame-Version 必须是非负整数版本号")
        return int(header.strip())
    if "version" in payload:
        version = payload["version"]
        if isinstance(version, bool) or not isinstance(version, int) or version < 0:
            raise ValueError("version 必须是非负整数")
        return version
    return None


def _attach_version_headers(resp, version: int):
    resp.headers["ETag"] = f'"{version}"'
    resp.headers["X-Frame-Version"] = str(version)
    # 同源页面无需 CORS；暴露头名方便外部脚本/扩展读取
    resp.headers["Access-Control-Expose-Headers"] = "ETag, X-Frame-Version"
    return resp


ensure_initialized()


@app.route("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


@app.route("/static/<path:filename>")
def static_files(filename):
    return send_from_directory(STATIC_DIR, filename)


@app.route("/api/state", methods=["GET"])
def api_get_state():
    state_obj, version = load_state_with_version()
    return _attach_version_headers(jsonify(state_obj), version)


@app.route("/api/state", methods=["PUT"])
def api_put_state():
    payload = request.get_json(force=True, silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "请求体必须是 JSON 对象"}), 400
    try:
        expected = _expected_version_from_request(payload)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    # version 仅为乐观锁参数，不属于架体 JSON，不能落盘进架体结构
    state_obj = {k: v for k, v in payload.items() if k != "version"}
    ok, version, current = commit_state(state_obj, expected)
    if not ok:
        # 版本冲突：拒绝旧写入，返回最新已保存架体与版本；本地未保存编辑由页面保留
        return _attach_version_headers(
            jsonify({
                "error": "架体版本冲突：您读取的架体已被其他页面/调用方保存，请合并后重试",
                "conflict": True,
                "version": version,
                "state": current,
                "result": calculate(current),
            }),
            version,
        ), 409
    return _attach_version_headers(
        jsonify({"ok": True, "version": version, "result": calculate(state_obj)}),
        version,
    )


@app.route("/api/recalc", methods=["POST"])
def api_recalc():
    payload = request.get_json(force=True, silent=True)
    state = payload if isinstance(payload, dict) else load_state()
    return jsonify(calculate(state))


@app.route("/api/result", methods=["GET"])
def api_result():
    state_obj, version = load_state_with_version()
    return _attach_version_headers(jsonify(calculate(state_obj)), version)


@app.route("/api/simulate", methods=["POST"])
def api_simulate():
    """立柱失效推演：只计算，不写盘；失效标记不持久化。"""
    payload = request.get_json(force=True, silent=True)
    payload = payload if isinstance(payload, dict) else {}
    state = payload.get("state")
    if not isinstance(state, dict):
        state = load_state()
    failed = payload.get("failed")
    if not isinstance(failed, list):
        failed = []
    return jsonify(simulate(state, failed))


@app.route("/api/reset", methods=["POST"])
def api_reset():
    # 重置同样推进版本：重置前发出的保存携带旧版本，事后返回时只会被 409 拒绝，
    # 不能把旧架体覆盖回示例。
    sample, version = reset_to_sample()
    return _attach_version_headers(
        jsonify({"ok": True, "version": version, "state": sample, "result": calculate(sample)}),
        version,
    )


@app.route("/api/wheel-load", methods=["POST"])
def api_wheel_load():
    """轮组承载试算：底层非悬空立柱点载荷按矩形四轮双向杠杆分配反力。

    只读取当前编辑架体与随请求提交的轮位/额定载荷，不写盘、不改已保存状态。
    轮距不正（坐标相等/顺序颠倒）、轮位越出台面、额定载荷非正 → 400；
    底层没有正载荷属于正常结果 → 200 + evaluable=false。
    """
    payload = request.get_json(force=True, silent=True)
    payload = payload if isinstance(payload, dict) else {}
    state = payload.get("state")
    if not isinstance(state, dict):
        state = load_state()
    # 轮组参数既可用扁平字段，也可放在 wheels 对象里，整体透传给计算引擎
    params = {k: v for k, v in payload.items() if k != "state"}
    try:
        result = wheel_load_trial(state, params)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"ok": True, **result})


@app.route("/api/suggest", methods=["POST"])
def api_suggest():
    """加柱建议：逐点试算新增一根立柱。只计算，不写盘、不改动已保存状态。"""
    payload = request.get_json(force=True, silent=True)
    payload = payload if isinstance(payload, dict) else {}
    state = payload.get("state")
    if not isinstance(state, dict):
        state = load_state()
    # 参数非法（非数字、越界层、间距<=0）→ 400；无可行点属于正常结果 → 200 + reason
    try:
        result = suggest_posts(
            state,
            payload.get("layer"),
            payload.get("spacing"),
            payload.get("capacity"),
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"ok": True, **result})


@app.route("/api/load-shed", methods=["POST"])
def api_load_shed():
    """减载配额试算：只调整坯件重量求最小总减载量。只计算，不写盘、不改已保存状态。"""
    payload = request.get_json(force=True, silent=True)
    payload = payload if isinstance(payload, dict) else {}
    state = payload.get("state")
    if not isinstance(state, dict):
        state = load_state()
    return jsonify({"ok": True, **load_shed(state)})


@app.route("/api/overturn", methods=["POST"])
def api_overturn():
    """层架抗倾覆试算：重心/支撑凸包/等效偏心带符号距离。只计算，不写盘、不改已保存状态。"""
    payload = request.get_json(force=True, silent=True)
    payload = payload if isinstance(payload, dict) else {}
    state = payload.get("state")
    if not isinstance(state, dict):
        state = load_state()
    # 加速度分量非法（非数字）→ 400；几何条件不足（无承托/凸包退化）属于正常结果 → 200 + evaluable=false
    try:
        result = overturn_check(state, payload.get("ax", 0), payload.get("ay", 0))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"ok": True, **result})


@app.route("/api/loading-sequence", methods=["POST"])
def api_loading_sequence():
    """逐层装货顺序规划：最大化全过程最小抗倾覆裕量。只计算，不写盘、不改已保存状态。"""
    payload = request.get_json(force=True, silent=True)
    payload = payload if isinstance(payload, dict) else {}
    state = payload.get("state")
    if not isinstance(state, dict):
        state = load_state()
    # 加速度分量非法（非数字）→ 400；前置条件不足（无承托/凸包退化/超 12 层等）属于正常结果 → 200 + planable=false
    try:
        result = loading_sequence(state, payload.get("ax", 0), payload.get("ay", 0))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"ok": True, **result})


@app.route("/api/door-passage", methods=["POST"])
def api_door_passage():
    """连续窑门通行试算：横向外廓 + 门序可达闭区间递推 + 回溯位置序列。

    只读取当前编辑架体与随请求提交的窑门参数，不写盘、不改已保存状态。
    窑门参数非法（非数组、非数值、宽度/高度非正、横移量为负）→ 400；
    架体条件不足（没有层）属于正常结果 → 200 + evaluable=false。
    """
    payload = request.get_json(force=True, silent=True)
    payload = payload if isinstance(payload, dict) else {}
    state = payload.get("state")
    if not isinstance(state, dict):
        state = load_state()
    try:
        result = door_passage(state, payload.get("doors", []))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"ok": True, **result})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
