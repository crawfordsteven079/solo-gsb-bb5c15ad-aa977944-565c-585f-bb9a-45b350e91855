# -*- coding: utf-8 -*-
"""Flask 服务：静态页面 + 状态持久化 API。

接口
----
GET  /api/state      读取当前架体（输入），首次启动自动载入示例数据
PUT  /api/state      保存编辑后的架体（JSON body）
POST /api/recalc     对给定（或当前持久化）架体重算，返回完整结果
GET  /api/result     直接返回当前持久化架体的重算结果
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
POST /api/reset      恢复内置示例数据
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
import tempfile

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
SAMPLE_FILE = os.path.join(DATA_DIR, "sample_state.json")

app = Flask(__name__, static_folder=None)


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


def load_state():
    if not os.path.exists(STATE_FILE):
        _atomic_write_json(STATE_FILE, _read_json(SAMPLE_FILE))
    return _read_json(STATE_FILE)


def ensure_initialized() -> None:
    """容器/进程启动时完成初始化：状态文件缺失则立即由示例数据生成。

    依赖已在镜像构建时装好；这里保证服务一旦启动，data/state.json
    即存在且可读写，无需等待首次请求。
    """
    if not os.path.exists(STATE_FILE):
        _atomic_write_json(STATE_FILE, _read_json(SAMPLE_FILE))


ensure_initialized()


@app.route("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


@app.route("/static/<path:filename>")
def static_files(filename):
    return send_from_directory(STATIC_DIR, filename)


@app.route("/api/state", methods=["GET"])
def api_get_state():
    return jsonify(load_state())


@app.route("/api/state", methods=["PUT"])
def api_put_state():
    payload = request.get_json(force=True, silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "请求体必须是 JSON 对象"}), 400
    _atomic_write_json(STATE_FILE, payload)
    return jsonify({"ok": True, "result": calculate(payload)})


@app.route("/api/recalc", methods=["POST"])
def api_recalc():
    payload = request.get_json(force=True, silent=True)
    state = payload if isinstance(payload, dict) else load_state()
    return jsonify(calculate(state))


@app.route("/api/result", methods=["GET"])
def api_result():
    return jsonify(calculate(load_state()))


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
    sample = _read_json(SAMPLE_FILE)
    _atomic_write_json(STATE_FILE, sample)
    return jsonify({"ok": True, "state": sample, "result": calculate(sample)})


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
