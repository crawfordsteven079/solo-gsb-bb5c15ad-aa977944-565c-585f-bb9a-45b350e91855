/* 陶瓷窑车层架载荷追踪台 —— 原生 JS 前端
 * 编辑重算走 PUT /api/state（自动保存），保存携带读取时的 X-Rack-Version 版本头
 * 做乐观并发控制：同页保存串行合并、跨页冲突（409）保留未保存草稿并提示；
 * 立柱失效推演走 POST /api/simulate（只算不存）。
 * 输入变化防抖触发，结果整架同步刷新；后端不可用时用浏览器内同一套算法兜底
 * （但兜底只代表本地重算，绝不代表已保存）。
 */
"use strict";

const TOL_MM = 10;
const LAYER_COLORS = ["#4f9dff", "#34c08a", "#c08bff", "#f0b03c", "#ef5b6b", "#3fd6d6"];

let state = null;        // 输入架体（可编辑，可能含未保存的本地草稿）
let result = null;       // 后端计算结果（基准；冲突后为本地草稿的只读重算）
let savedVersion = null; // 最近一次已知的服务端架体版本（GET / PUT成功 / reset 后更新）
let simulation = null;   // 失效推演结果（非 null 时视图/明细展示推演值）
let simDiff = null;      // 基准 vs 推演差异（台面载荷/丢失载荷/逐柱差值/新增问题）
let failedSelection = []; // 失效柱选择 [{layer, post}]，仅会话内有效，不保存
let simSeq = 0;          // 推演请求序号，丢弃过期响应
let selection = null;    // {kind:'post'|'board', layer, post?}
let sideAxis = "x";
let recalcTimer = null;
const topHit = [];       // 俯视图可点击热区
const sideHit = [];      // 侧视图可点击热区

/* ================= 保存队列与版本冲突（多页面并发编辑） =================
 * 同一窑车可能在多个页面/标签同时编辑：PUT /api/state 必须携带读取时的
 * 架体版本（X-Rack-Version 头），服务端核对版本通过才提交并返回新版本。
 *
 * - saveChain：保存严格串行化。同页连续编辑只允许一个 PUT 在途，期间的
 *   新草稿合并进 pending（携带上一次成功返回的版本），避免同页自己与
 *   自己冲突；每个入队请求都挂到最终一次写入的 Promise 上。
 * - saveEpoch：页面本地保存代次。reset 成功后 +1，使重置前已发出、尚未
 *   返回的保存响应一律作废——较早请求的响应不得覆盖较新草稿/重置状态。
 * - conflictInfo：版本冲突（409）时保存服务端返回的最新架体与版本。
 *   未保存编辑原样保留在编辑器里，绝不显示“已保存”，也不用本地试算
 *   伪装成保存成功；只读 POST /api/recalc 仍可对本地草稿重算供查看。
 */
let saveInFlight = false;
let savePending = null;  // {payload, applyChange, force, tick, waiters:[{resolve,reject}]}
let saveEpoch = 0;       // reset 成功后 +1：使重置前发出的保存响应整体作废
let saveTick = 0;        // 入队单调序号：较早请求的响应不得覆盖较新草稿
let conflictInfo = null; // {version, state, result}

/* ================= 加柱建议状态 =================
 * suggestData 只来自 POST /api/suggest 的试算结果（或离线兜底），绝不写盘；
 * 仅当用户在预览中点「确认加柱」后，才把新柱追加进 state 走原有 PUT /api/state 保存。
 */
let suggestData = null;        // 最近一次试算结果
let suggestPreviewRank = null; // 当前预览方案 rank（1..5）
let suggestSeq = 0;            // 试算请求序号，丢弃过期响应
let suggestAppliedMsg = null;  // 确认加柱后的一次性提示
const SUGGEST_COLOR = "#ffd54a";

/* ================= 减载配额试算状态 =================
 * shedData 只来自 POST /api/load-shed（或离线兜底），绝不写盘；
 * 仅当用户点「按建议重量减载并保存」后，才把各层 wareWeight 写入 state 走 PUT /api/state。
 */
let shedData = null;           // 最近一次减载试算结果
let shedSeq = 0;               // 试算请求序号，丢弃过期响应
let shedAppliedMsg = null;     // 确认减载后的一次性提示

/* ================= 层架抗倾覆试算状态 =================
 * overturnData 只来自 POST /api/overturn（或离线兜底），只读不写盘；
 * 编辑架体后立即作废（invalidateOverturn），需要重新试算。
 */
let overturnData = null;       // 最近一次抗倾覆试算结果
let overturnSeq = 0;           // 试算请求序号，丢弃过期响应
let overturnErr = null;        // 最近一次参数错误提示（400）
let overturnTimer = null;      // 加速度输入变化后的防抖重算
const OVERTURN_COLOR = "#46b8e8";

/* ================= 逐层装货顺序规划状态 =================
 * seqData 只来自 POST /api/loading-sequence（或离线兜底），只读不写盘；
 * 编辑架体后立即作废（invalidateSequence），需要重新规划。
 */
let seqData = null;            // 最近一次装货顺序规划结果
let seqReqSeq = 0;             // 请求序号，丢弃过期响应
let seqErr = null;             // 最近一次参数错误提示（400）
let seqTimer = null;           // 加速度输入变化后的防抖重算

/* ================= 连续窑门通行试算状态 =================
 * doorInputs 只存在于浏览器（窑门参数不写入架体状态、不持久化）；
 * doorData 只来自 POST /api/door-passage（或离线兜底），只读不写盘。
 * 架体或门参数改变后旧结果立即作废（stale），需重新试算。
 */
let doorInputs = [
  { center: 650, width: 1400, height: 970, maxShift: 100 },
  { center: 570, width: 1400, height: 960, maxShift: 100 },
];
let doorData = null;           // 最近一次通行试算结果
let doorReqSeq = 0;            // 请求序号，丢弃过期响应
let doorErr = null;            // 最近一次参数错误提示（400）
let doorStale = false;         // 试算后架体/门参数已变化：旧结果失效
let doorTimer = null;          // 门参数变化后的防抖重算

/* ================= 轮组承载试算状态 =================
 * wheelData 只来自 POST /api/wheel-load（或离线兜底），只读不写盘；
 * 轮位/额定载荷只存在于浏览器，不写入架体状态。编辑架体后旧结果立即作废。
 */
let wheelData = null;          // 最近一次轮组试算结果
let wheelReqSeq = 0;           // 请求序号，丢弃过期响应
let wheelErr = null;           // 最近一次参数错误提示（400）
let wheelStale = false;        // 试算后架体/轮参数已变化：旧结果失效
let wheelTimer = null;         // 轮参数变化后的防抖重算
const WHEEL_COLOR = "#ff9f43";

/* ================= 数值/工具 ================= */
const $ = (sel) => document.querySelector(sel);
const fmt = (v) => (Math.round(v * 100) / 100).toLocaleString("zh-CN", { maximumFractionDigits: 2 });
const num = (v) => { const n = parseFloat(v); return Number.isFinite(n) ? n : 0; };
const r2 = (v) => Math.round(v * 100) / 100;
const viewResult = () => simulation || result; // 当前展示用结果：推演进行中为推演值，否则为基准
const debounce = (fn, ms) => (...args) => {
  clearTimeout(recalcTimer);
  recalcTimer = setTimeout(() => fn(...args), ms);
};

function setSaveState(cls, text) {
  const el = $("#saveState");
  el.className = "save-state " + cls;
  el.textContent = text;
}

/* ================= 数据采集（从输入框） ================= */
function collectState() {
  const car = {
    name: "窑车台面",
    w: num(document.querySelector('[data-car="w"]').value),
    d: num(document.querySelector('[data-car="d"]').value),
  };
  const layers = [...document.querySelectorAll(".layer-card")].map((card) => {
    const li = +card.dataset.index;
    const posts = [...card.querySelectorAll(".post-row")].map((row) => ({
      id: row.querySelector('[data-pf="id"]').value.trim() || row.dataset.pid,
      x: num(row.querySelector('[data-pf="x"]').value),
      y: num(row.querySelector('[data-pf="y"]').value),
      capacity: num(row.querySelector('[data-pf="capacity"]').value),
    }));
    return {
      id: card.dataset.layerid,
      name: card.querySelector('[data-lf="name"]').value.trim() || `第${li + 1}层`,
      height: num(card.querySelector('[data-lf="height"]').value),
      board: {
        x: num(card.querySelector('[data-lf="bx"]').value),
        y: num(card.querySelector('[data-lf="by"]').value),
        w: num(card.querySelector('[data-lf="bw"]').value),
        d: num(card.querySelector('[data-lf="bd"]').value),
        weight: num(card.querySelector('[data-lf="bweight"]').value),
      },
      wareWeight: num(card.querySelector('[data-lf="wareWeight"]').value),
      posts,
    };
  });
  state = { car, layers };
  return state;
}

/* ================= 重算 + 保存（版本化） + 渲染 ================= */

/* 编辑后统一作废依赖已保存架体的只读试算（确认写入的那次除外） */
function invalidateDependentTrials(applyChange) {
  // 架体一经编辑/写盘，之前的加柱试算与减载试算结果即作废（确认写入的那次除外）
  if (!applyChange) {
    invalidateSuggestions({ silent: true });
    invalidateShed({ silent: true });
  }
  // 抗倾覆试算只依赖架体几何/重量：加柱、减载确认写入同样会改变它，任何重算都作废
  invalidateOverturn({ silent: true });
  // 逐层装货顺序规划同样只依赖架体几何/重量：任何编辑都使旧规划作废
  invalidateSequence({ silent: true });
  // 连续窑门通行试算只依赖架体几何：任何编辑都使旧试算失效
  invalidateDoorTrial({ silent: true });
  // 轮组承载试算的点载荷来自底层立柱实载：任何编辑都使旧试算失效
  invalidateWheelTrial({ silent: true });
  suggestAppliedMsg = null;
  if (applyChange !== "shed") shedAppliedMsg = null;
}

async function recalcAndRender(applyChange) {
  const payload = collectState();
  invalidateDependentTrials(applyChange);

  // 已处于版本冲突（保存被服务端拒绝）状态：
  // 后续编辑只更新本地草稿并走只读重算，绝不自动重试保存、绝不显示“已保存”，
  // 也不用本地试算掩盖未保存事实。用户须在冲突横幅上显式选择如何解决。
  if (conflictInfo) {
    setSaveState(
      "conflict",
      "⚠ 版本冲突未解决：编辑仅保留在本页，未保存（v" + savedVersion + "）"
    );
    await runDraftRecalc(payload, applyChange);
    return "conflict";
  }

  return enqueueSave(payload, applyChange, false);
}

/* 把一次保存并入串行队列；返回的 Promise 在其对应（或更新一次）写入落定后兑现 */
function enqueueSave(payload, applyChange, force) {
  return new Promise((resolve, reject) => {
    saveTick += 1;
    if (savePending) {
      // 已有在途保存：原地更新为最新草稿。较早草稿的保存响应到达时将被 tick 判废，
      // 不会覆盖较新草稿；本次调用挂在同一作业上一起等待最终结果。
      savePending.payload = payload;
      savePending.applyChange = applyChange;
      savePending.force = force || savePending.force;
      savePending.tick = saveTick;
      savePending.waiters.push({ resolve, reject });
    } else {
      savePending = {
        payload,
        applyChange,
        force,
        tick: saveTick,
        waiters: [{ resolve, reject }],
      };
    }
    pumpSaveQueue();
  });
}

async function pumpSaveQueue() {
  if (saveInFlight || !savePending) return;
  saveInFlight = true;
  while (savePending) {
    const job = savePending; // 在途期间该对象会被原地更新为更新草稿，或被 reset 清空
    const payload = job.payload;
    const applyChange = job.applyChange;
    const force = job.force;
    const epoch = saveEpoch;
    const tick = job.tick;
    const label =
      applyChange === "suggest" ? "写入新柱并保存中…"
      : applyChange === "shed" ? "写入建议坯件重量并保存中…"
      : "重算并保存中…";
    setSaveState("saving", label);
    // eslint-disable-next-line no-await-in-loop
    const outcome = await doSave(payload, applyChange, force, epoch, tick);

    if (saveEpoch !== epoch) {
      // reset 已在途中发生：队列已被清空，唤醒仍挂起的等待方后退出
      job.waiters.forEach((w) => w.resolve("stale"));
      break;
    }
    if (outcome === "stale" && savePending === job && savePending.tick !== tick) {
      // 在途期间本作业已被更新草稿取代：版本号已在 doSave 内吸收，
      // 等待方继续挂到本作业的最终一次写入上，本次不唤醒、不清队列。
      continue;
    }

    // 作业最终落定（saved/conflict/error，或无后续草稿）：唤醒全部等待方
    job.waiters.forEach((w) => w.resolve(outcome));
    if (outcome === "conflict") {
      savePending = null;                  // 冲突后停止自动保存；后续编辑只走只读重算
      break;
    }
    if (savePending === job) savePending = null; // 在途期间没有更新草稿：队列处理完
    // 若 savePending 已被换成新对象（极端情况下在 await 间隙入队），循环继续处理它。
  }
  saveInFlight = false;
}

/* 执行一次 PUT。返回 "saved" | "conflict" | "error" | "stale"。
 * tick 落后于最新草稿时为过期响应：不覆盖较新草稿/视图；
 * 但成功返回的新版本号仍要记下，供排在后面的最新草稿请求使用。 */
async function doSave(payload, applyChange, force, epoch, tick) {
  let outcome;
  try {
    const headers = { "Content-Type": "application/json" };
    // force=true 时故意不带版本头：走无版本旧语义强制覆盖（用户在冲突后显式选择）
    if (!force && Number.isInteger(savedVersion)) {
      headers["X-Rack-Version"] = String(savedVersion);
    }
    const res = await fetch("/api/state", {
      method: "PUT",
      headers,
      body: JSON.stringify(payload),
    });
    const data = await res.json().catch(() => null);

    // reset 已推进代次：重置前发出的保存响应（无论结果）一律丢弃，不得覆盖重置
    if (epoch !== saveEpoch) return "stale";
    // 在途期间本页又产生了更新草稿（无论合并进本作业还是已生成下一作业）：
    // 本响应属于较早请求，不得覆盖较新草稿。成功提交的版本号仍要吸收
    //（最新草稿的下一次 PUT 必须基于它），然后静默退出。
    if (saveTick !== tick) {
      // 2xx 成功才吸收版本；409/其它错误不改变本地版本（仍由冲突流程处理）
      if (res.ok && data) savedVersion = extractVersion(res, data);
      return "stale";
    }

    if (res.status === 409 && data && data.conflict) {
      // 版本冲突：服务端拒绝了旧写入。保留本地未保存编辑，记录最新已保存架体。
      conflictInfo = {
        version: data.version,
        state: data.state,
        result: data.result,
      };
      savedVersion = data.version; // 之后若强制覆盖，无需再带版本头
      state = payload;             // 编辑器内容就是用户的未保存草稿，原样保留
      // 结果区按本地草稿只读重算（不写盘），保证视图与左侧未保存编辑一致；
      // 绝不显示“已保存”。服务端最新架体留在 conflictInfo.state，供“放弃”时载入。
      simulation = null; simDiff = null;
      setSaveState("conflict", "⚠ 保存被拒：版本冲突，本页编辑未保存（服务端最新 v" + data.version + "）");
      renderConflictBanner();
      await runDraftRecalc(payload, applyChange);
      outcome = "conflict";
    } else if (!res.ok || !data) {
      throw new Error("保存失败");
    } else {
      conflictInfo = null;
      savedVersion = extractVersion(res, data);
      result = data.result;
      state = payload;
      setSaveState("saved", "✓ 已重算并保存 " + new Date().toLocaleTimeString("zh-CN"));
      if (applyChange === "suggest") suggestAppliedMsg = "✓ 新柱已写入架体并触发自动保存";
      if (applyChange === "shed") shedAppliedMsg = "✓ 建议坯件重量已写入架体并触发自动保存";
      renderConflictBanner();
      outcome = "saved";
    }
  } catch (err) {
    // 网络/服务不可用（不是版本冲突）：才允许本地兜底重算，并明确标注未能保存
    if (epoch !== saveEpoch) return "stale";
    if (saveTick !== tick) return "stale";
    setSaveState("error", "保存失败，仅本地重算（未保存到服务端）");
    result = calculateLocal(payload);
    state = payload;
    if (applyChange === "suggest") suggestAppliedMsg = "✓ 新柱已加入本地编辑（后端不可用，未能保存）";
    if (applyChange === "shed") shedAppliedMsg = "✓ 建议坯件重量已加入本地编辑（后端不可用，未能保存）";
    outcome = "error";
  }
  selection = selection && stillExists(selection) ? selection : null;
  // 编辑只更新基准；若正在推演，用同一失效选择在新基准上重跑（失效标记不保存）
  if (outcome === "saved" || outcome === "error") {
    if (failedSelection.length) await refreshSimulation();
    else { simulation = null; simDiff = null; }
  }
  renderResults();
  markLayerBadges();
  return outcome;
}

/* 冲突未解决期间继续编辑：只读重算本地草稿（POST /api/recalc 不写盘、不递增版本），
 * 结果仅用于查看，顶栏始终保留“未保存/冲突”状态。 */
async function runDraftRecalc(payload, applyChange) {
  state = payload;
  try {
    const res = await fetch("/api/recalc", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!res.ok) throw new Error("recalc failed");
    result = await res.json();
  } catch (e) {
    result = calculateLocal(payload);
  }
  selection = selection && stillExists(selection) ? selection : null;
  if (failedSelection.length) await refreshSimulation();
  else { simulation = null; simDiff = null; }
  renderResults();
  markLayerBadges();
}

function extractVersion(res, data) {
  const hv = parseInt(res.headers.get("X-Rack-Version"), 10);
  if (Number.isInteger(hv) && hv > 0) return hv;
  if (data && Number.isInteger(data.version) && data.version > 0) return data.version;
  return savedVersion;
}

/* ================= 版本冲突横幅 ================= */
function renderConflictBanner() {
  const box = $("#conflictBanner");
  if (!box) return;
  if (!conflictInfo) { box.hidden = true; box.innerHTML = ""; return; }
  box.hidden = false;
  box.innerHTML = `
    <div class="cb-title">⚠ 检测到版本冲突：本页编辑未保存</div>
    <div>
      架体已被<b>其他页面</b>（或「恢复示例」）更新到 <b>v${conflictInfo.version}</b>。
      您读取时基于的是旧版本，服务端已拒绝本次写入，<b>左侧编辑器中的未保存修改已原样保留</b>，
      结果区按您的本地草稿只读重算（<b>未落盘</b>）。请选择如何处理：
    </div>
    <div class="cb-actions">
      <button id="cbDiscard" class="btn btn-cb-discard" type="button">放弃本页修改，载入最新架体</button>
      <button id="cbForce" class="btn btn-cb-force" type="button">以本页编辑强制覆盖保存（忽略版本）</button>
      <button id="cbKeep" class="btn btn-cb-keep" type="button">先保留修改，我再看看</button>
    </div>`;
  $("#cbDiscard").addEventListener("click", resolveConflictDiscard);
  $("#cbForce").addEventListener("click", resolveConflictForce);
  $("#cbKeep").addEventListener("click", () => {
    $("#conflictBanner").hidden = true; // 顶栏的冲突/未保存状态仍保留
  });
}

/* 放弃本地未保存编辑，载入服务端最新已保存架体 */
async function resolveConflictDiscard() {
  if (!conflictInfo) return;
  const latest = conflictInfo.state;
  const latestResult = conflictInfo.result;
  const version = conflictInfo.version;
  conflictInfo = null;
  state = latest;
  result = latestResult;
  savedVersion = version;
  selection = null;
  failedSelection = []; simulation = null; simDiff = null; simSeq++;
  renderEditor();
  renderConflictBanner();
  setSaveState("saved", "✓ 已载入最新架体 v" + version + " " + new Date().toLocaleTimeString("zh-CN"));
  renderResults();
  markLayerBadges();
}

/* 保留本地编辑并强制覆盖：走无版本 PUT（旧语义），服务端仍会生成新版本 */
async function resolveConflictForce() {
  if (!conflictInfo) return;
  const payload = collectState(); // 以编辑器中保留的未保存草稿为准
  setSaveState("saving", "强制覆盖保存中…");
  conflictInfo = null;
  renderConflictBanner();
  await enqueueSave(payload, null, true);
}

/* ================= 立柱失效推演 =================
 * 失效柱选择只保存在会话内（failedSelection），绝不写入 state / 持久化；
 * 基准 = 当前编辑数据，推演 = 失效柱视为不承载后按原规则整架重新分摊。
 */
function isFailed(li, pid) {
  return failedSelection.some((f) => f.layer === li && f.post === pid);
}

async function toggleFailed(li, pid, on) {
  if (on) {
    if (!isFailed(li, pid)) failedSelection.push({ layer: li, post: pid });
  } else {
    failedSelection = failedSelection.filter((f) => !(f.layer === li && f.post === pid));
  }
  await applySimulation();
}

async function applySimulation() {
  if (failedSelection.length) await refreshSimulation();
  else { simulation = null; simDiff = null; }
  renderResults();
  markLayerBadges();
}

async function refreshSimulation() {
  const seq = ++simSeq;
  const payload = { state, failed: failedSelection };
  let data = null;
  try {
    const res = await fetch("/api/simulate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!res.ok) throw new Error("simulate failed");
    data = await res.json();
  } catch (e) {
    data = simulateLocal(state, failedSelection); // 离线兜底：与后端同一套算法
  }
  if (seq !== simSeq) return; // 已有更新的推演请求，丢弃过期响应
  simulation = data.simulation;
  simDiff = data.diff;
  // 以实际生效的失效柱为准（失效标记只作用于非悬空柱，编辑后可能不再适用）
  failedSelection = (data.failed || []).map((f) => ({ layer: f.layer, post: f.post }));
}

function clearSimulation() {
  failedSelection = [];
  simulation = null;
  simDiff = null;
  simSeq++; // 使进行中的推演请求失效
  renderResults(); // 立即恢复基准
  markLayerBadges();
}

function stillExists(sel) {
  if (!result) return false;
  const L = result.layers[sel.layer];
  if (!L) return false;
  if (sel.kind === "board") return true;
  return L.posts.some((p) => p.id === sel.post);
}

const scheduleRecalc = debounce(recalcAndRender, 250);

/* ================= 编辑器渲染 ================= */
function renderEditor() {
  const list = $("#layerList");
  list.innerHTML = "";
  state.layers.forEach((layer, i) => list.appendChild(buildLayerCard(layer, i)));
  $('[data-car="w"]').value = state.car.w;
  $('[data-car="d"]').value = state.car.d;
}

function buildLayerCard(layer, i) {
  const card = document.createElement("section");
  card.className = "card layer-card";
  card.dataset.index = i;
  card.dataset.layerid = layer.id;
  card.style.borderLeftColor = LAYER_COLORS[i % LAYER_COLORS.length];

  const tag = i === 0 ? "底层 · 柱落窑车台面"
    : i === state.layers.length - 1 ? "顶层" : `第 ${i + 1} 层`;
  card.innerHTML = `
    <div class="layer-row">
      <div class="layer-name">
        <input type="text" data-lf="name" value="${escapeHtml(layer.name)}" />
        <span class="layer-tag">${tag}</span>
      </div>
      <div class="layer-badges">
        <button class="btn-del" data-act="del-layer" title="删除本层">✕</button>
      </div>
    </div>
    <div class="layer-badges" style="margin-bottom:6px">
      <span class="mini-badge" data-mb="overload">超载 0</span>
      <span class="mini-badge" data-mb="hanging">悬空 0</span>
      <span class="mini-badge" data-mb="overlap">重叠 0</span>
    </div>

    <div class="sub-title">矩形棚板（左下角原点）</div>
    <div class="grid4">
      <label>x <input type="number" step="any" data-lf="bx" value="${layer.board.x}"></label>
      <label>y <input type="number" step="any" data-lf="by" value="${layer.board.y}"></label>
      <label>宽 W <input type="number" step="any" data-lf="bw" value="${layer.board.w}"></label>
      <label>深 D <input type="number" step="any" data-lf="bd" value="${layer.board.d}"></label>
    </div>
    <div class="grid2" style="margin-top:6px">
      <label>棚板自重 (kg) <input type="number" step="any" data-lf="bweight" value="${layer.board.weight}"></label>
      <label>坯件重量 (kg) <input type="number" step="any" data-lf="wareWeight" value="${layer.wareWeight}"></label>
    </div>
    <div class="grid2" style="margin-top:6px">
      <label>立柱高度 H (mm，层间距) <input type="number" step="any" data-lf="height" value="${layer.height}"></label>
    </div>

    <div class="sub-title"><span>立柱（顶端托本板，底端落在下层）</span></div>
    <div class="post-list"></div>
    <button class="btn-add-post" data-act="add-post" type="button">＋ 增加立柱</button>
  `;
  const postList = card.querySelector(".post-list");
  layer.posts.forEach((p, pi) => postList.appendChild(buildPostRow(p, pi)));
  return card;
}

function buildPostRow(p, pi) {
  const row = document.createElement("div");
  row.className = "post-row";
  row.dataset.pid = p.id || `P${pi + 1}`;
  row.innerHTML = `
    <div class="grid3">
      <label>编号 <input type="text" data-pf="id" value="${escapeHtml(p.id)}"></label>
      <label>x(mm) <input type="number" step="any" data-pf="x" value="${p.x}"></label>
      <label>y(mm) <input type="number" step="any" data-pf="y" value="${p.y}"></label>
    </div>
    <div class="grid3 two-col" style="margin-top:4px">
      <label>容量(kg，0=不限) <input type="number" step="any" data-pf="capacity" value="${p.capacity}"></label>
      <button class="btn-del" data-act="del-post" title="删除立柱" style="height:28px;margin-top:14px">✕</button>
    </div>`;
  return row;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function markLayerBadges() {
  if (!result) return;
  document.querySelectorAll(".layer-card").forEach((card) => {
    const L = result.layers[+card.dataset.index];
    if (!L) return;
    const counts = { overload: 0, hanging: 0, overlap: 0 };
    L.posts.forEach((p) => {
      if (p.hanging) counts.hanging++;
      if (p.overlap) counts.overlap++;
      if (p.overloaded) counts.overload++;
    });
    card.querySelector('[data-mb="overload"]').textContent = "超载 " + counts.overload;
    card.querySelector('[data-mb="hanging"]').textContent = "悬空 " + counts.hanging;
    card.querySelector('[data-mb="overlap"]').textContent = "重叠 " + counts.overlap;
  });
}

/* ================= 编辑器事件（委托） ================= */
$("#editor").addEventListener("input", (e) => {
  if (e.target.closest("input")) scheduleRecalc();
});

$("#editor").addEventListener("click", (e) => {
  const btn = e.target.closest("[data-act]");
  if (!btn) return;
  const act = btn.dataset.act;
  const card = e.target.closest(".layer-card");
  collectState();

  if (act === "del-layer") {
    const i = +card.dataset.index;
    if (!confirm(`删除「${state.layers[i].name}」？`)) return;
    state.layers.splice(i, 1);
    renderEditor(); scheduleRecalc();
  } else if (act === "add-post") {
    const i = +card.dataset.index;
    const ids = state.layers[i].posts.map((p) => p.id);
    let k = 1;
    while (ids.includes("P" + k)) k++;
    const lowerBoard = i === 0
      ? { x: 0, y: 0, w: state.car.w, d: state.car.d }
      : state.layers[i - 1].board;
    state.layers[i].posts.push({
      id: "P" + k,
      x: Math.min(lowerBoard.x + 200, lowerBoard.x + Math.max(lowerBoard.w - 10, 0)),
      y: Math.min(lowerBoard.y + 200, lowerBoard.y + Math.max(lowerBoard.d - 10, 0)),
      capacity: 200,
    });
    renderEditor(); scheduleRecalc();
  } else if (act === "del-post") {
    const i = +card.dataset.index;
    const row = e.target.closest(".post-row");
    const posts = card.querySelectorAll(".post-row");
    const pi = [...posts].indexOf(row);
    state.layers[i].posts.splice(pi, 1);
    renderEditor(); scheduleRecalc();
  }
});

$("#btnAddLayer").addEventListener("click", () => {
  collectState();
  const i = state.layers.length;
  state.layers.push({
    id: "L" + (i + 1),
    name: `第${i + 1}层${i === 0 ? "（底层）" : i >= 2 ? "（顶层）" : ""}`,
    height: 300,
    board: { x: 100, y: 100, w: Math.max(200, state.car.w - 200), d: Math.max(200, state.car.d - 200), weight: 150 },
    wareWeight: 200,
    posts: [],
  });
  renderEditor(); scheduleRecalc();
});

$("#btnReset").addEventListener("click", async () => {
  if (!confirm("恢复为内置示例数据？当前编辑将被覆盖。")) return;
  let data;
  try {
    const res = await fetch("/api/reset", { method: "POST" });
    data = await res.json();
    if (!res.ok || !data || !data.state) throw new Error("reset failed");
  } catch (e) {
    alert("恢复示例失败：无法连接后端服务，本地编辑保持不变。");
    return;
  }
  // 重置生成新版本：使重置前已发出、尚未返回的保存响应整体作废（旧响应不得覆盖重置）
  saveEpoch++;
  savePending = null;
  conflictInfo = null;
  state = data.state;
  result = data.result;
  savedVersion = Number.isInteger(data.version) ? data.version : savedVersion;
  selection = null;
  failedSelection = []; simulation = null; simDiff = null; simSeq++;
  invalidateSuggestions({ silent: true });
  invalidateShed({ silent: true });
  invalidateOverturn({ silent: true });
  invalidateSequence({ silent: true });
  invalidateDoorTrial({ silent: true });
  invalidateWheelTrial({ silent: true });
  suggestAppliedMsg = null;
  shedAppliedMsg = null;
  renderEditor();
  renderConflictBanner();
  renderResults(); markLayerBadges();
  setSaveState("saved", "✓ 已恢复示例（v" + savedVersion + "）" + new Date().toLocaleTimeString("zh-CN"));
});

/* 侧视图切换 */
document.querySelectorAll(".seg-btn").forEach((b) => b.addEventListener("click", () => {
  document.querySelectorAll(".seg-btn").forEach((x) => x.classList.remove("active"));
  b.classList.add("active");
  sideAxis = b.dataset.axis;
  drawSide();
}));

/* ================= 结果汇总/告警 ================= */
function renderResults() {
  renderTotals();
  renderIssues();
  drawTop();
  drawSide();
  renderDetail();
  renderSimPanel();
  renderSuggestOptions();
  renderSuggestPanel();
  renderShedPanel();
  renderOverturnPanel();
  renderSeqPanel();
  renderDoorPanel();
  renderWheelPanel();
}

function renderTotals() {
  const res = viewResult();
  const t = res.totals;
  const issues = res.issues;
  const errs = issues.filter((x) => x.level === "error").length;
  const warns = issues.filter((x) => x.level === "warn").length;
  $("#totals").innerHTML = `
    ${simulation ? `<div class="stat sim"><div class="k">失效推演中</div><div class="v">🔮 ${failedSelection.length}<small> 根立柱失效</small></div></div>` : ""}
    <div class="stat"><div class="k">层数</div><div class="v">${t.layerCount}<small> 层</small></div></div>
    <div class="stat"><div class="k">立柱总数</div><div class="v">${t.postCount}<small> 根</small></div></div>
    <div class="stat"><div class="k">架体总自重</div><div class="v">${fmt(t.ownWeight)}<small> kg（板+坯）</small></div></div>
    <div class="stat"><div class="k">传到台面总载荷</div><div class="v">${fmt(t.carriedAtBottom)}<small> kg</small></div></div>
    <div class="stat ${t.lostLoad > 0 ? "danger" : ""}"><div class="k">悬空丢失载荷</div><div class="v">${fmt(t.lostLoad)}<small> kg</small></div></div>
    <div class="stat ${errs ? "danger" : warns ? "warn" : ""}"><div class="k">告警</div><div class="v">${errs}<small> 严重 / ${warns} 提醒</small></div></div>`;
}

function renderIssues() {
  const res = viewResult();
  const ul = $("#issueList");
  const badge = $("#issueCount");
  badge.textContent = res.issues.length;
  badge.className = "badge" + (res.issues.some((x) => x.level === "error") ? " has-error" : "");
  if (!res.issues.length) {
    ul.innerHTML = `<li class="muted" style="cursor:default">✓ 未发现悬空、落点重叠或超载。</li>`;
    return;
  }
  ul.innerHTML = "";
  res.issues.forEach((iss) => {
    const li = document.createElement("li");
    li.className = iss.level;
    const labelMap = { overload: "超载", hang: "悬空", unsupported: "无支撑", overlap: "落点重叠", invalid: "数据", failed: "失效" };
    li.innerHTML = `<span class="itype">${labelMap[iss.type] || iss.type}</span><span>${escapeHtml(iss.message)}</span>`;
    li.addEventListener("click", () => {
      if (iss.post == null) {
        selection = { kind: "board", layer: iss.layer };
      } else {
        selection = { kind: "post", layer: iss.layer, post: iss.post };
      }
      renderDetail(); drawTop(); drawSide();
    });
    ul.appendChild(li);
  });
}

/* ================= Canvas 公共 ================= */
function setupCanvas(canvas) {
  const dpr = window.devicePixelRatio || 1;
  const cssW = canvas.clientWidth || 500;
  const cssH = canvas.clientHeight || 360;
  canvas.width = cssW * dpr;
  canvas.height = cssH * dpr;
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  return { ctx, w: cssW, h: cssH };
}

function postColor(p) {
  if (p.failed) return "#ff5c8a";   // 失效（推演）：与悬空紫色区分
  if (p.hanging) return "#c061e8";
  if (p.overloaded) return "#ef5b6b";
  if (p.overlap) return "#ff8c42";
  return "#34c08a";
}
function isSelected(kind, li, pid) {
  return selection && selection.kind === kind && selection.layer === li && (kind === "board" || selection.post === pid);
}

/* ================= 俯视图 ================= */
function topTransform() {
  const car = viewResult().car;
  const pad = 46;
  const cw = Math.max(car.w, 1);
  const cd = Math.max(car.d, 1);
  const c = $("#topView");
  const W = c.clientWidth || 560;
  const H = Math.max(360, Math.min(560, W * cd / cw + pad * 2));
  c.style.height = H + "px";
  const scale = Math.min((W - pad * 2) / cw, (H - pad * 2) / cd);
  // 台面居中
  const ox = (W - cw * scale) / 2;
  const oy = (H - cd * scale) / 2;
  return {
    scale, ox, oy,
    X: (x) => ox + x * scale,
    Y: (y) => oy + cd * scale - y * scale, // 屏幕 y 向下，世界 y 向上
  };
}

function drawTop() {
  if (!viewResult()) return;
  const canvas = $("#topView");
  const { ctx, w, h } = setupCanvas(canvas);
  topHit.length = 0;
  const tr = topTransform();
  const res = viewResult();
  const car = res.car;

  ctx.clearRect(0, 0, w, h);

  // 台面
  ctx.fillStyle = "rgba(79,157,255,0.07)";
  ctx.strokeStyle = "#3a5a8c";
  ctx.lineWidth = 1.5;
  ctx.fillRect(tr.X(0), tr.Y(car.d), car.w * tr.scale, car.d * tr.scale);
  ctx.strokeRect(tr.X(0), tr.Y(car.d), car.w * tr.scale, car.d * tr.scale);
  ctx.fillStyle = "#8b97ad";
  ctx.font = "10px sans-serif";
  ctx.fillText(`${car.name} ${fmt(car.w)}×${fmt(car.d)} mm`, tr.X(0) + 4, tr.Y(0) + 12);

  // 棚板（半透明，自下而上绘制，上层覆盖在下层之上）
  res.layers.forEach((L, li) => {
    const b = L.board;
    const col = LAYER_COLORS[li % LAYER_COLORS.length];
    ctx.fillStyle = col + "26";
    if (isSelected("board", li)) ctx.fillStyle = col + "55";
    ctx.strokeStyle = col;
    ctx.lineWidth = isSelected("board", li) ? 2.2 : 1.3;
    ctx.fillRect(tr.X(b.x), tr.Y(b.y + b.d), b.w * tr.scale, b.d * tr.scale);
    ctx.strokeRect(tr.X(b.x), tr.Y(b.y + b.d), b.w * tr.scale, b.d * tr.scale);
    ctx.fillStyle = col;
    ctx.fillText(`${L.name} ${fmt(b.w)}×${fmt(b.d)}`, tr.X(b.x) + 4, tr.Y(b.y + b.d) + 11);
    topHit.push({
      kind: "board", layer: li,
      x: tr.X(b.x), y: tr.Y(b.y + b.d), ww: b.w * tr.scale, hh: b.d * tr.scale,
    });
  });

  // 重叠落点（画虚线圆圈强调）
  res.layers.forEach((L) => {
    L.posts.forEach((p) => {
      const cx = tr.X(p.x), cy = tr.Y(p.y);
      if (p.overlap) {
        ctx.beginPath();
        ctx.arc(cx, cy, 9, 0, Math.PI * 2);
        ctx.strokeStyle = "#ff8c42";
        ctx.setLineDash([3, 3]);
        ctx.lineWidth = 1.5;
        ctx.stroke();
        ctx.setLineDash([]);
      }
    });
  });

  // 立柱
  res.layers.forEach((L, li) => {
    L.posts.forEach((p) => {
      const cx = tr.X(p.x), cy = tr.Y(p.y);
      const col = postColor(p);
      const sel = isSelected("post", li, p.id);
      ctx.beginPath();
      ctx.arc(cx, cy, sel ? 7 : 4.5, 0, Math.PI * 2);
      ctx.fillStyle = col;
      ctx.globalAlpha = p.hanging ? 0.35 : p.failed ? 0.6 : 1; // 悬空/失效柱半透明
      ctx.fill();
      ctx.globalAlpha = 1;
      if (p.hanging) {
        // 悬空：红紫色 ✕ 画在圆点之上
        ctx.strokeStyle = "#c061e8";
        ctx.lineWidth = 1.8;
        ctx.beginPath();
        ctx.moveTo(cx - 6, cy - 6); ctx.lineTo(cx + 6, cy + 6);
        ctx.moveTo(cx + 6, cy - 6); ctx.lineTo(cx - 6, cy + 6);
        ctx.stroke();
      }
      if (p.failed) {
        // 失效（推演）：粉色 ⊘ 标记，与悬空 ✕ 区分
        ctx.strokeStyle = "#ff5c8a";
        ctx.lineWidth = 1.8;
        ctx.beginPath();
        ctx.arc(cx, cy, 6.5, 0, Math.PI * 2);
        ctx.stroke();
        ctx.beginPath();
        ctx.moveTo(cx - 4.5, cy + 4.5); ctx.lineTo(cx + 4.5, cy - 4.5);
        ctx.stroke();
      }
      if (sel) {
        ctx.strokeStyle = "#fff";
        ctx.lineWidth = 1.5;
        ctx.stroke();
      }
      ctx.fillStyle = "#cdd7ea";
      ctx.font = "9px sans-serif";
      ctx.fillText(`${p.id} ${fmt(p.load)}`, cx + 8, cy - 5);
      topHit.push({ kind: "post", layer: li, post: p.id, x: cx, y: cy, r: 9 });
    });
  });

  drawSuggestPreview(ctx, tr);
  drawOverturnOverlay(ctx, tr);
  drawWheelOverlay(ctx, tr);
}

/* 加柱建议预览：在俯视图上叠加建议新柱（金色 ⊕），不改基准数据 */
function drawSuggestPreview(ctx, tr) {
  const legend = $("#suggestLegend");
  if (legend) legend.classList.toggle("on", !!previewPlan());
  const plan = previewPlan();
  if (!plan) return;
  const sp = plan.post;
  const cx = tr.X(sp.x), cy = tr.Y(sp.y);
  // 外圈光晕
  ctx.beginPath();
  ctx.arc(cx, cy, 12, 0, Math.PI * 2);
  ctx.fillStyle = "rgba(255,213,74,0.18)";
  ctx.fill();
  ctx.beginPath();
  ctx.arc(cx, cy, 6.5, 0, Math.PI * 2);
  ctx.fillStyle = SUGGEST_COLOR;
  ctx.fill();
  ctx.strokeStyle = "#5a4a00";
  ctx.lineWidth = 1.6;
  ctx.beginPath();
  ctx.moveTo(cx - 3.6, cy); ctx.lineTo(cx + 3.6, cy);
  ctx.moveTo(cx, cy - 3.6); ctx.lineTo(cx, cy + 3.6);
  ctx.stroke();
  ctx.fillStyle = SUGGEST_COLOR;
  ctx.font = "bold 10px sans-serif";
  ctx.fillText(`新柱 ${sp.id} (${fmt(sp.x)},${fmt(sp.y)}) 载荷 ${fmt(sp.load)} kg`, cx + 12, cy + 3);
}

/* 俯视图点击/悬停 */
$("#topView").addEventListener("click", (e) => {
  const hit = pickHit(e, $("#topView"), topHit);
  if (hit) {
    selection = hit.kind === "post"
      ? { kind: "post", layer: hit.layer, post: hit.post }
      : { kind: "board", layer: hit.layer };
    renderDetail(); drawTop(); drawSide();
  }
});

/* ================= 侧视图 ================= */
function sideGeometry() {
  const res = viewResult();
  const car = res.car;
  // 沿观察轴的台面长度
  const axisLen = sideAxis === "x" ? car.w : car.d;
  let zMax = res.layers.reduce((s, L) => s + L.height, 0);
  zMax = Math.max(zMax, 1);
  const c = $("#sideView");
  const W = c.clientWidth || 460;
  const padX = 46, padTop = 30, padBottom = 36;
  const H = Math.max(360, Math.min(560, W * 0.62));
  c.style.height = H + "px";
  const sx = (W - padX * 2) / Math.max(axisLen, 1);
  const sy = (H - padTop - padBottom) / zMax;
  const X = (v) => padX + v * sx;
  const Z = (z) => H - padBottom - z * sy;
  return { axisLen, zMax, X, Z, padBottom, W, H };
}

function drawSide() {
  if (!viewResult()) return;
  const canvas = $("#sideView");
  const { ctx, w, h } = setupCanvas(canvas);
  sideHit.length = 0;
  const g = sideGeometry();
  const res = viewResult();
  const car = res.car;

  ctx.clearRect(0, 0, w, h);

  // 台面（z=0 粗线）
  ctx.strokeStyle = "#3a5a8c";
  ctx.lineWidth = 4;
  ctx.beginPath();
  ctx.moveTo(g.X(0), g.Z(0));
  ctx.lineTo(g.X(g.axisLen), g.Z(0));
  ctx.stroke();
  ctx.fillStyle = "#8b97ad";
  ctx.font = "10px sans-serif";
  ctx.fillText(`${car.name}（${sideAxis.toUpperCase()} 向 ${fmt(g.axisLen)} mm）`, g.X(0) + 4, g.Z(0) + 18);

  // 各棚板高度：zPos[i] 为第 i 层棚板（= 第 i 层立柱顶端）的累计高度
  const boardRects = [];
  const zPos = [];
  let acc = 0;
  res.layers.forEach((L, li) => {
    acc += L.height;
    zPos.push(acc);
    const b = L.board;
    const v0 = sideAxis === "x" ? b.x : b.y;
    const v1 = v0 + (sideAxis === "x" ? b.w : b.d);
    boardRects.push({ z: acc, v0, v1, li });
  });

  // 立柱：顶端在本层棚板 zPos[i]，底端落在下层棚板 zPos[i-1]（底层为台面 0）
  res.layers.forEach((L, li) => {
    const zTop = zPos[li];
    const zBottom = li === 0 ? 0 : zPos[li - 1];
    L.posts.forEach((p) => {
      const v = sideAxis === "x" ? p.x : p.y;
      const col = postColor(p);
      const sel = isSelected("post", li, p.id);
      const broken = p.hanging || p.failed; // 悬空/失效均不承载：柱身“短一截”
      ctx.strokeStyle = col;
      ctx.lineWidth = sel ? 3 : 1.8;
      if (broken) ctx.setLineDash([5, 4]);
      ctx.beginPath();
      ctx.moveTo(g.X(v), g.Z(zTop));
      // 悬空/失效柱底端画不到承托面：在层间 60% 处终止，视觉上表现为“短一截”
      const zEnd = broken ? zBottom + (zTop - zBottom) * 0.35 : zBottom;
      ctx.lineTo(g.X(v), g.Z(zEnd));
      ctx.stroke();
      ctx.setLineDash([]);

      // 底端落点标记
      ctx.beginPath();
      ctx.arc(g.X(v), g.Z(zEnd), sel ? 6 : 4, 0, Math.PI * 2);
      if (p.hanging) {
        ctx.strokeStyle = col;
        ctx.lineWidth = 1.5;
        ctx.stroke(); // 空心：未接触承托面
        const x0 = g.X(v), y0 = g.Z(zEnd);
        ctx.beginPath();
        ctx.moveTo(x0 - 4, y0 - 4); ctx.lineTo(x0 + 4, y0 + 4);
        ctx.moveTo(x0 + 4, y0 - 4); ctx.lineTo(x0 - 4, y0 + 4);
        ctx.stroke();
      } else if (p.failed) {
        // 失效（推演）：空心圆 + 斜杠 ⊘，与悬空 ✕ 区分
        ctx.strokeStyle = col;
        ctx.lineWidth = 1.5;
        ctx.stroke();
        const x0 = g.X(v), y0 = g.Z(zEnd);
        ctx.beginPath();
        ctx.moveTo(x0 - 4, y0 + 4); ctx.lineTo(x0 + 4, y0 - 4);
        ctx.stroke();
      } else {
        ctx.fillStyle = col;
        ctx.fill();
      }

      // 编号 + 载荷（柱身上端）
      ctx.fillStyle = "#cdd7ea";
      ctx.font = "9px sans-serif";
      ctx.fillText(`${p.id}:${fmt(p.load)}`, g.X(v) + 3, g.Z(zTop) - 3);
      sideHit.push({
        kind: "post", layer: li, post: p.id,
        x: g.X(v), z0: g.Z(zTop), z1: g.Z(zEnd),
      });
    });
  });

  // 棚板线（最后画，覆盖在柱线上）
  boardRects.forEach(({ z: zz, v0, v1, li }) => {
    const col = LAYER_COLORS[li % LAYER_COLORS.length];
    ctx.strokeStyle = col;
    ctx.lineWidth = isSelected("board", li) ? 4 : 2.5;
    ctx.beginPath();
    ctx.moveTo(g.X(v0), g.Z(zz));
    ctx.lineTo(g.X(v1), g.Z(zz));
    ctx.stroke();
    const L = res.layers[li];
    ctx.fillStyle = col;
    ctx.font = "10px sans-serif";
    ctx.fillText(`${L.name} 合计 ${fmt(L.loadTotal)} kg`, g.X(v0) + 3, g.Z(zz) - 7);
    sideHit.push({
      kind: "board", layer: li,
      x0: g.X(v0), x1: g.X(v1), zz: g.Z(zz),
    });
  });

  // z 轴刻度（每层高度）
  ctx.strokeStyle = "#2a3650";
  ctx.fillStyle = "#8b97ad";
  ctx.lineWidth = 1;
  let zz = 0;
  res.layers.forEach((L) => {
    ctx.beginPath();
    ctx.moveTo(g.X(0) - 5, g.Z(zz));
    ctx.lineTo(g.X(0), g.Z(zz));
    ctx.stroke();
    zz += L.height;
  });
  ctx.beginPath();
  ctx.moveTo(g.X(0) - 5, g.Z(zz));
  ctx.lineTo(g.X(0), g.Z(zz));
  ctx.stroke();
  ctx.fillText(`总高 ${fmt(zz)} mm`, g.X(0) - 40, g.Z(zz) - 5);
}

$("#sideView").addEventListener("click", (e) => {
  const rect = $("#sideView").getBoundingClientRect();
  const mx = e.clientX - rect.left, my = e.clientY - rect.top;
  // 柱
  let best = null, bestD = 1e9;
  const distToSeg = (px, pz, x, z0, z1) => {
    const t = Math.max(0, Math.min(1, (pz - z0) / (z1 - z0 || 1)));
    return Math.hypot(px - x, pz - (z0 + t * (z1 - z0)));
  };
  for (const h of sideHit) {
    if (h.kind === "post") {
      const d = distToSeg(mx, my, h.x, h.z0, h.z1);
      if (d < bestD && d < 10) { bestD = d; best = { kind: "post", layer: h.layer, post: h.post }; }
    }
  }
  if (!best) {
    for (const h of sideHit) {
      if (h.kind === "board" && mx >= h.x0 && mx <= h.x1 && Math.abs(my - h.zz) < 8) {
        best = { kind: "board", layer: h.layer };
        break;
      }
    }
  }
  if (best) {
    selection = best;
    renderDetail(); drawTop(); drawSide();
  }
});

function pickHit(e, canvas, hits) {
  const rect = canvas.getBoundingClientRect();
  const mx = e.clientX - rect.left, my = e.clientY - rect.top;
  // 优先立柱
  let best = null, bestD = 1e9;
  for (const h of hits) {
    if (h.kind === "post") {
      const d = Math.hypot(mx - h.x, my - h.y);
      if (d < h.r + 3 && d < bestD) { bestD = d; best = h; }
    }
  }
  if (best) return best;
  for (const h of hits) {
    if (h.kind === "board" && mx >= h.x && mx <= h.x + h.ww && my >= h.y && my <= h.y + h.hh) return h;
  }
  return null;
}

/* ================= 明细面板 ================= */
function renderDetail() {
  const title = $("#detailTitle");
  const body = $("#detailBody");
  if (!selection) {
    title.textContent = "载荷来源明细";
    body.innerHTML = `<p class="muted">点击俯视图/侧视图中的立柱或棚板，查看其载荷来源链。${simulation ? "当前处于失效推演模式，显示推演后的来源链。" : ""}</p>`;
    return;
  }
  const L = viewResult().layers[selection.layer];
  if (selection.kind === "board") return renderBoardDetail(L);
  const p = L && L.posts.find((x) => x.id === selection.post);
  if (p) renderPostDetail(L, p);
}

function statusPills(p) {
  const pills = [];
  if (p.failed) pills.push(`<span class="pill failed">失效 · 推演</span>`);
  else pills.push(p.hanging ? `<span class="pill hang">悬空</span>` : `<span class="pill ok">有效</span>`);
  if (p.overloaded) pills.push(`<span class="pill bad">超载</span>`);
  if (p.overlap) pills.push(`<span class="pill ovl">与 ${p.overlapWith.join("、")} 落点重叠</span>`);
  return pills.join(" ");
}

function baseLoadOf(L, p) {
  // 基准（未推演）中同层同编号立柱的载荷，用于失效柱说明
  const BL = result && result.layers[L.index];
  const bp = BL && BL.posts.find((x) => x.id === p.id);
  return bp ? bp.load : 0;
}

function renderPostDetail(L, p) {
  $("#detailTitle").textContent = `${L.name} · 立柱 ${p.id}${simulation ? "（推演）" : ""}`;
  const rows = (p.sources || []).map((s) =>
    `<tr><td>${escapeHtml(s.label)}</td><td>${fmt(s.amount)}</td></tr>`).join("");
  const body = $("#detailBody");
  body.innerHTML = `
    <div class="detail-head">
      <span>底端坐标 <b>(${fmt(p.x)}, ${fmt(p.y)})</b></span>
      <span>载荷 <b>${fmt(p.load)} kg</b></span>
      <span>容量 <b>${p.capacity > 0 ? `${fmt(p.capacity)} kg` : "不限"}</b></span>
      ${p.capacity > 0 ? `<span>利用率 <b>${Math.round(p.load / p.capacity * 100)}%</b></span>` : ""}
    </div>
    <div class="detail-head">${statusPills(p)}</div>
    ${p.failed
      ? `<p class="kvline" style="color:#ff5c8a">该柱在推演中被标记为失效，不参与均摊；基准中载荷 ${fmt(baseLoadOf(L, p))} kg 改由本层其余有效柱承担。点击其他立柱可查看推演后的载荷来源链。</p>`
      : p.hanging
      ? `<p class="kvline" style="color:#c061e8">该柱底端未落在${L.supportName}矩形范围内，不参与均摊；本层载荷由其余有效立柱承担。</p>`
      : `<table class="src-table">
          ${rows}
          <tr class="sum"><td>本柱载荷合计（${L.supportedCount} 根有效柱均摊 ${fmt(L.loadTotal)} kg）</td><td>${fmt(p.load)}</td></tr>
        </table>`}
  `;
}

function renderBoardDetail(L) {
  $("#detailTitle").textContent = `${L.name} · 棚板${simulation ? "（推演）" : ""}`;
  const postTable = incomingRowsHtml(L);
  $("#detailBody").innerHTML = `
    <div class="detail-head">
      <span>位置 <b>(${fmt(L.board.x)}, ${fmt(L.board.y)})</b></span>
      <span>尺寸 <b>${fmt(L.board.w)}×${fmt(L.board.d)} mm</b></span>
      <span>承托面 <b>${L.supportName}</b></span>
    </div>
    <p class="kvline">
      棚板自重：<b>${fmt(L.board.weight)} kg</b><br>
      坯件重量：<b>${fmt(L.wareWeight)} kg</b><br>
      上层立柱传来点载荷合计：<b>${fmt(L.loadIncoming)} kg</b><br>
      棚板承受总载荷：<b>${fmt(L.loadTotal)} kg</b><br>
      有效立柱 ${L.supportedCount} 根 / 悬空 ${L.hangingCount} 根${L.failedCount ? ` / 失效 ${L.failedCount} 根` : ""}，
      下传合计 <b>${fmt(L.loadCarried)} kg</b>
      ${L.lostLoad > 0 ? `<br><span style="color:#ef5b6b">无有效支撑，丢失载荷 ${fmt(L.lostLoad)} kg</span>` : ""}
    </p>
    ${postTable}`;
}

function incomingRowsHtml(L) {
  // 展示本板总载荷分摊到每根立柱的结果明细（悬空/失效柱载荷为 0）
  const rows = L.posts.map((p) =>
    `<tr><td>立柱 ${escapeHtml(p.id)} (${fmt(p.x)},${fmt(p.y)}) ${p.failed ? "—失效·推演" : p.hanging ? "—悬空" : ""}</td><td>${fmt(p.load)}</td></tr>`
  ).join("");
  return `<table class="src-table">${rows}<tr class="sum"><td>下传合计</td><td>${fmt(L.loadCarried)}</td></tr></table>`;
}

/* ================= 失效推演面板 ================= */
function renderSimPanel() {
  renderSimPicker();
  renderSimOutput();
}

function renderSimPicker() {
  const box = $("#simPicker");
  if (!box) return;
  box.innerHTML = "";
  if (!result) return;
  // 勾选列表基于基准结果：悬空柱本就不承载，不可选为失效柱
  result.layers.forEach((L, li) => {
    const row = document.createElement("div");
    row.className = "sim-layer";
    const name = document.createElement("span");
    name.className = "sim-layer-name";
    name.textContent = L.name;
    row.appendChild(name);
    const chips = document.createElement("div");
    chips.className = "sim-chips";
    if (!L.posts.length) {
      chips.innerHTML = `<span class="muted" style="font-size:11px">（无立柱）</span>`;
    }
    L.posts.forEach((p) => {
      const checked = isFailed(li, p.id);
      const chip = document.createElement("label");
      chip.className = "sim-chip" + (p.hanging ? " hanging" : "") + (checked ? " checked" : "");
      chip.title = p.hanging
        ? "悬空柱本就不承载，无需失效推演"
        : `基准载荷 ${fmt(p.load)} kg，勾选后视为失效（不承载）`;
      chip.innerHTML = p.hanging
        ? `<input type="checkbox" disabled> ${escapeHtml(p.id)} <small>悬空</small>`
        : `<input type="checkbox" ${checked ? "checked" : ""}> ${escapeHtml(p.id)} <small>${fmt(p.load)}kg</small>`;
      if (!p.hanging) {
        chip.querySelector("input").addEventListener("change", (e) => {
          toggleFailed(li, p.id, e.target.checked);
        });
      }
      chips.appendChild(chip);
    });
    row.appendChild(chips);
    box.appendChild(row);
  });
}

function renderSimOutput() {
  const out = $("#simOutput");
  const status = $("#simStatus");
  const btn = $("#btnClearSim");
  if (!out) return;
  const active = !!(simulation && simDiff);
  status.textContent = active ? `推演中 · 失效 ${failedSelection.length} 根` : "未启用";
  status.className = "badge" + (active ? " has-sim" : "");
  btn.disabled = !active && !failedSelection.length;
  if (!active) {
    out.innerHTML = `<p class="muted" style="margin:8px 0 0">当前显示基准结果。勾选失效柱后，此处并排展示基准与推演的台面载荷、丢失载荷及每根立柱载荷差值，并单独列出推演新增的超载 / 无支撑项。</p>`;
    return;
  }
  const d = simDiff;
  const cmpCard = (label, pair, badWhen) => {
    const bad = badWhen === "up" ? pair.delta > 0 : pair.delta < 0;
    return `<div class="stat ${bad ? "danger" : ""}">
      <div class="k">${label}</div>
      <div class="v">${fmt(pair.base)} → ${fmt(pair.sim)}<small> kg</small></div>
      <div class="delta ${pair.delta > 0 ? "up" : pair.delta < 0 ? "down" : ""}">Δ ${pair.delta > 0 ? "+" : ""}${fmt(pair.delta)} kg</div>
    </div>`;
  };
  const labelMap = { overload: "超载", unsupported: "无支撑" };
  const newIssuesHtml = d.newIssues.length
    ? d.newIssues.map((iss) =>
        `<li class="${iss.level}" data-layer="${iss.layer}" data-post="${iss.post == null ? "" : escapeHtml(iss.post)}">
           <span class="itype">${labelMap[iss.type] || iss.type}</span><span>${escapeHtml(iss.message)}</span>
         </li>`).join("")
    : `<li class="muted" style="cursor:default">✓ 推演未新增超载或无支撑项。</li>`;
  const rows = d.posts.map((p) => `
    <tr class="${p.failed ? "is-failed" : ""}">
      <td>${escapeHtml(p.layerName)}</td>
      <td>${escapeHtml(p.post)}${p.failed ? ' <span class="tag-failed">失效</span>' : p.hanging ? ' <span class="tag-hang">悬空</span>' : ""}</td>
      <td>${fmt(p.base)}</td>
      <td>${fmt(p.sim)}</td>
      <td class="${p.delta > 0 ? "up" : p.delta < 0 ? "down" : ""}">${p.delta > 0 ? "+" : ""}${fmt(p.delta)}</td>
    </tr>`).join("");
  out.innerHTML = `
    <div class="sim-compare">
      ${cmpCard("传到台面总载荷（基准 → 推演）", d.carriedAtBottom, "down")}
      ${cmpCard("丢失载荷（基准 → 推演）", d.lostLoad, "up")}
    </div>
    <div class="sim-cols">
      <div>
        <div class="sub-title">推演新增超载 / 无支撑</div>
        <ul id="simNewIssues">${newIssuesHtml}</ul>
      </div>
      <div>
        <div class="sub-title">每根立柱载荷差值（基准 → 推演，kg）</div>
        <div class="diff-table-wrap">
          <table class="src-table diff-table">
            <tr class="head"><td>层</td><td>立柱</td><td>基准</td><td>推演</td><td>差值</td></tr>
            ${rows}
          </table>
        </div>
      </div>
    </div>`;
  // 新增问题可点击定位到对应立柱/棚板
  out.querySelectorAll("#simNewIssues li[data-layer]").forEach((li) => {
    li.addEventListener("click", () => {
      const layer = +li.dataset.layer;
      selection = li.dataset.post
        ? { kind: "post", layer, post: li.dataset.post }
        : { kind: "board", layer };
      renderDetail(); drawTop(); drawSide();
    });
  });
}

$("#btnClearSim").addEventListener("click", clearSimulation);

/* ================= 加柱建议面板 ================= */
function previewPlan() {
  if (!suggestData || suggestPreviewRank == null) return null;
  return (suggestData.plans || []).find((p) => p.rank === suggestPreviewRank) || null;
}

/* 架体发生编辑/重置：上次试算作废（不影响已确认保存的数据） */
function invalidateSuggestions(opts) {
  const wasActive = !!suggestData;
  suggestData = null;
  suggestPreviewRank = null;
  suggestSeq++;
 
  if (wasActive && !(opts && opts.silent)) {
    // 目前作废由 renderSuggestPanel 统一渲染提示；保留钩子便于扩展
  }
  if (!opts || !opts.silent) renderSuggestPanel();
}

function renderSuggestOptions() {
  const sel = $("#suggestLayer");
  if (!sel || !state) return;
  const keep = sel.value;
  const opts = state.layers.map((L, i) =>
    `<option value="${i}">${escapeHtml(L.name)}（承托面：${i === 0 ? escapeHtml(state.car.name) : escapeHtml(state.layers[i - 1].name) + "棚板"}）</option>`).join("");
  if (sel.innerHTML !== opts) sel.innerHTML = opts;
  if (keep !== "" && state.layers[+keep]) sel.value = keep;
  if (result && typeof result.toleranceMm === "number") {
    const t = $("#tolText");
    if (t) t.textContent = result.toleranceMm;
  }
}

async function runSuggest() {
  if (!state) return;
  const layer = +$("#suggestLayer").value;
  const spacing = num($("#suggestSpacing").value);
  const capacity = num($("#suggestCapacity").value);
  const status = $("#suggestStatus");
  suggestAppliedMsg = null;
  status.textContent = "试算中…";
  status.className = "badge has-sim";
  const seq = ++suggestSeq;
  let data;
  try {
    const res = await fetch("/api/suggest", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ state, layer, spacing, capacity }),
    });
    data = await res.json().catch(() => null);
    if (!res.ok) {
      if (seq !== suggestSeq) return;
      suggestData = null; suggestPreviewRank = null;
      renderSuggestPanel((data && data.error) || `试算失败（HTTP ${res.status}）`);
      return;
    }
  } catch (e) {
    try {
      data = suggestLocal(state, layer, spacing, capacity); // 离线兜底：与后端同一套算法
    } catch (e2) {
      if (seq !== suggestSeq) return;
      suggestData = null; suggestPreviewRank = null;
      renderSuggestPanel(e2.message);
      return;
    }
  }
  if (seq !== suggestSeq) return; // 期间已有编辑/重置，丢弃过期响应
  suggestData = data;
  suggestPreviewRank = null;
 
  renderSuggestPanel();
}

function selectSuggestPlan(rank) {
  if (!suggestData) return;
  suggestPreviewRank = rank;
  renderSuggestPanel();
  drawTop();
}

function cancelSuggestPreview() {
  // 取消预览：仅清掉叠加层，试算结果保留可再次点击；绝不改动架体数据
  suggestPreviewRank = null;
  renderSuggestPanel();
  drawTop();
}

async function confirmSuggestPlan() {
  const plan = previewPlan();
  if (!plan || !suggestData) return;
  const li = suggestData.layer;
  if (!state.layers[li]) {
    invalidateSuggestions();
    return;
  }
  // 二次确认：此前任何编辑都会清空试算结果，走到这里说明数据未变
  if (!confirm(`在「${suggestData.layerName}」新增立柱 ${plan.post.id}（${fmt(plan.post.x)}, ${fmt(plan.post.y)}），容量 ${plan.post.capacity > 0 ? fmt(plan.post.capacity) + " kg" : "不限"}？确认后将写入架体并自动保存。`)) return;
  // 确认后才写入：追加新柱 → 走原有编辑通道（PUT /api/state）触发自动保存
  state.layers[li].posts.push({
    id: plan.post.id,
    x: plan.post.x,
    y: plan.post.y,
    capacity: plan.post.capacity,
  });
  const appliedId = plan.post.id;
  suggestData = null;
  suggestPreviewRank = null;
  renderEditor();
  await recalcAndRender("suggest");
  selection = { kind: "post", layer: li, post: appliedId };
  renderDetail(); drawTop(); drawSide();
  renderSuggestPanel();
}

function pctText(u) {
  return u == null ? "—（无容量限制柱）" : Math.round(u * 100) + "%";
}

function renderSuggestPanel(errText) {
  const out = $("#suggestOutput");
  const status = $("#suggestStatus");
  const btnCancel = $("#btnCancelSuggest");
  const btnConfirm = $("#btnConfirmSuggest");
  if (!out) return;
  const plan = previewPlan();
  btnCancel.disabled = !plan;
  btnConfirm.disabled = !plan;

  if (suggestAppliedMsg) {
    status.textContent = "已加柱";
    status.className = "badge has-suggest";
  } else if (!suggestData) {
    status.textContent = errText ? "参数有误" : "未试算";
    status.className = "badge";
  } else if (plan) {
    status.textContent = `预览方案 ${plan.rank}/5`;
    status.className = "badge has-suggest";
  } else {
    status.textContent = suggestData.plans.length ? `已试算 · ${suggestData.feasibleCount} 个可行点` : "无可行点";
    status.className = "badge";
  }

  if (suggestAppliedMsg) {
    out.innerHTML = `<p class="sug-applied">${escapeHtml(suggestAppliedMsg)}</p>` + suggestionClosedHtml();
    return;
  }
  if (errText) {
    out.innerHTML = `<p class="sug-error">⚠ ${escapeHtml(errText)}</p>`;
    return;
  }
  if (!suggestData) {
    out.innerHTML = `<p class="muted" style="margin:8px 0 0">设置目标层、候选网格间距与新柱容量后点击「开始试算」。试算只在后端逐点计算，不会改动当前架体；点击返回的方案可在俯视图预览新柱位置与试算后的告警变化。</p>`;
    return;
  }
  if (!suggestData.plans.length) {
    out.innerHTML = `
      <p class="sug-error">🚫 无可行点：${escapeHtml(suggestData.reason || "候选条件下没有可新增立柱的位置")}</p>
      <p class="muted" style="margin:4px 0 0">
        目标层：<b>${escapeHtml(suggestData.layerName)}</b>；候选网格点 ${suggestData.gridCount} 个、可行点 ${suggestData.feasibleCount} 个；
        重叠阈值 ${suggestData.toleranceMm || TOL_MM} mm。可尝试加大网格间距、调整现有立柱或更换目标层。
      </p>`;
    return;
  }

  const bm = suggestData.baselineMetrics;
  const summary = `
    <div class="sug-summary">
      <span>${escapeHtml(suggestData.layerName)}</span>
      <span>网格间距 <b>${fmt(suggestData.spacing)}</b> mm</span>
      <span>新柱容量 <b>${suggestData.capacity > 0 ? fmt(suggestData.capacity) + " kg" : "不限"}</b></span>
      <span>候选点 <b>${suggestData.gridCount}</b></span>
      <span>可行点 <b>${suggestData.feasibleCount}</b></span>
    </div>
    <p class="hint" style="margin:4px 0 0">基准：丢失载荷 ${fmt(bm.lostLoad)} kg · 超载 ${bm.overloadCount} 根 · 最高利用率 ${pctText(bm.maxUtilization)}。点击方案卡片在俯视图预览；确认后才写入架体并自动保存。</p>`;

  const cards = suggestData.plans.map((p) => suggestCardHtml(p, bm)).join("");
  out.innerHTML = summary + `<div class="sug-cards">${cards}</div>`;

  out.querySelectorAll(".sug-card").forEach((el) => {
    el.addEventListener("click", (e) => {
      if (e.target.closest(".sug-inner")) return; // 展开区内点击不重复切换
      selectSuggestPlan(+el.dataset.rank);
    });
  });
  out.querySelectorAll(".sug-issues li[data-layer]").forEach((li) => {
    li.addEventListener("click", (e) => {
      e.stopPropagation();
      const layer = +li.dataset.layer;
      const post = li.dataset.post ? decodeURIComponent(li.dataset.post) : "";
      selection = post
        ? { kind: "post", layer, post }
        : { kind: "board", layer };
      renderDetail(); drawTop(); drawSide();
    });
  });
}

function suggestionClosedHtml() {
  return `<p class="muted" style="margin:4px 0 0">可重新设置参数并试算下一根新柱。</p>`;
}

function suggestCardHtml(p, bm) {
  const selected = p.rank === suggestPreviewRank;
  const m = p.metrics;
  const layerRows = p.layers.map((r) => {
    const totD = r.loadTotal.trial - r.loadTotal.base;
    const lostD = r.lostLoad.trial - r.lostLoad.base;
    const carD = r.loadCarried.trial - r.loadCarried.base;
    const ovD = r.overloadCount.trial - r.overloadCount.base;
    const cell = (base, trial, d, goodDown) => {
      const cls = Math.abs(d) < 1e-9 ? "" : (goodDown ? (d < 0 ? "down" : "up2") : (d > 0 ? "down" : "up2"));
      return `<td>${fmt(base)} → <b>${fmt(trial)}</b> <span class="${cls}">${Math.abs(d) < 1e-9 ? "" : (d > 0 ? "+" : "") + fmt(d)}</span></td>`;
    };
    return `<tr${r.layer === suggestData.layer ? ' class="target"' : ""}>
      <td>${escapeHtml(r.name)}${r.layer === suggestData.layer ? ' <em>目标层</em>' : ""}</td>
      ${cell(r.loadTotal.base, r.loadTotal.trial, totD, false)}
      ${cell(r.lostLoad.base, r.lostLoad.trial, lostD, true)}
      ${cell(r.loadCarried.base, r.loadCarried.trial, carD, false)}
      ${cell(r.overloadCount.base, r.overloadCount.trial, ovD, true)}
    </tr>`;
  }).join("");
  const issueBlock = (title, arr, cls, mark) => arr.length
    ? arr.map((iss) =>
        `<li class="${iss.level} ${cls}" data-layer="${iss.layer}" data-post="${iss.post == null ? "" : encodeURIComponent(iss.post)}">
           <span class="itype">${escapeHtml(mark)}</span><span>${escapeHtml(iss.message)}</span></li>`).join("")
    : `<li class="muted" style="cursor:default">无${title}告警</li>`;
  return `
  <div class="sug-card${selected ? " selected" : ""}" data-rank="${p.rank}">
    <div class="sug-card-head">
      <span class="sug-rank">方案 ${p.rank}</span>
      <span class="sug-coord">新柱 ${escapeHtml(p.post.id)}（${fmt(p.post.x)}, ${fmt(p.post.y)}）</span>
      <span class="sug-load">载荷 ${fmt(p.post.load)} kg${p.post.capacity > 0 ? ` / ${fmt(p.post.capacity)} kg` : " / 不限容量"}</span>
      <span class="sug-go">${selected ? "预览中 ▴" : "点击预览 ▸"}</span>
    </div>
    ${selected ? `
    <div class="sug-inner" onclick="event.stopPropagation()">
      <div class="sug-metrics">
        <div class="sug-metric ${m.lostLoad < bm.lostLoad ? "good" : ""}"><span>丢失载荷</span><b>${fmt(bm.lostLoad)} → ${fmt(m.lostLoad)}</b><small>${m.lostLoad < bm.lostLoad ? "−" + fmt(bm.lostLoad - m.lostLoad) + " kg" : "±0"}</small></div>
        <div class="sug-metric ${m.overloadCount < bm.overloadCount ? "good" : m.overloadCount > bm.overloadCount ? "bad" : ""}"><span>超载柱数</span><b>${bm.overloadCount} → ${m.overloadCount}</b><small>${m.overloadCount < bm.overloadCount ? "−" + (bm.overloadCount - m.overloadCount) : m.overloadCount > bm.overloadCount ? "+" + (m.overloadCount - bm.overloadCount) : "±0"} 根</small></div>
        <div class="sug-metric"><span>最高利用率</span><b>${pctText(bm.maxUtilization)} → ${pctText(m.maxUtilization)}</b><small>容量 0 不计</small></div>
        <div class="sug-metric"><span>传到台面</span><b>${fmt(bm.carriedAtBottom)} → ${fmt(m.carriedAtBottom)}</b><small>kg</small></div>
      </div>
      <div class="sug-table-wrap">
        <table class="src-table sug-table">
          <tr class="head"><td>层</td><td>本层总载荷 kg</td><td>丢失载荷 kg</td><td>下传载荷 kg</td><td>超载柱</td></tr>
          ${layerRows}
        </table>
      </div>
      <div class="sug-issue-cols">
        <div>
          <div class="sub-title">新增告警</div>
          <ul class="sug-issues">${issueBlock("新增", p.issues.added, "is-added", "新增")}</ul>
        </div>
        <div>
          <div class="sub-title">消除告警</div>
          <ul class="sug-issues">${issueBlock("消除", p.issues.removed, "is-removed", "消除")}</ul>
        </div>
      </div>
    </div>` : ""}
  </div>`;
}

$("#btnSuggest").addEventListener("click", runSuggest);
$("#btnCancelSuggest").addEventListener("click", cancelSuggestPreview);
$("#btnConfirmSuggest").addEventListener("click", confirmSuggestPlan);
// 调整试算参数即视为新一轮试算开始，清掉旧提示
["suggestLayer", "suggestSpacing", "suggestCapacity"].forEach((id) => {
  const el = document.getElementById(id);
  if (el) el.addEventListener("change", () => { suggestAppliedMsg = null; renderSuggestPanel(); });
});

/* ================= 减载配额试算面板 ================= */
/* 架体发生编辑/重置：上次减载方案作废（不影响已确认保存的数据） */
function invalidateShed(opts) {
  const wasActive = !!shedData;
  shedData = null;
  shedSeq++;
  if (wasActive && !(opts && opts.silent)) renderShedPanel();
  if (!opts || !opts.silent) renderShedPanel();
}

async function runLoadShed() {
  if (!state) return;
  const status = $("#shedStatus");
  shedAppliedMsg = null;
  status.textContent = "试算中…";
  status.className = "badge has-sim";
  const seq = ++shedSeq;
  let data;
  try {
    const res = await fetch("/api/load-shed", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ state }),
    });
    data = await res.json().catch(() => null);
    if (!res.ok) {
      if (seq !== shedSeq) return;
      shedData = null;
      renderShedPanel((data && data.error) || `试算失败（HTTP ${res.status}）`);
      return;
    }
  } catch (e) {
    try {
      data = loadShedLocal(state); // 离线兜底：与后端同一套算法
    } catch (e2) {
      if (seq !== shedSeq) return;
      shedData = null;
      renderShedPanel(e2.message);
      return;
    }
  }
  if (seq !== shedSeq) return; // 期间已有编辑/重置，丢弃过期响应
  shedData = data;
  renderShedPanel();
}

async function confirmShed() {
  if (!shedData || !shedData.feasible || !state) return;
  const total = shedData.totalReduction;
  const changed = shedData.layers.filter((L) => L.changed).length;
  if (!confirm(`按建议重量下调 ${changed} 层坯件、合计减载 ${fmt(total)} kg？\n确认后写入架体并自动保存，棚板/立柱等其它数据不变。`)) return;
  // 确认后才写入：逐层替换 wareWeight → 走原有编辑通道（PUT /api/state）自动保存
  shedData.layers.forEach((row) => {
    if (state.layers[row.index]) state.layers[row.index].wareWeight = row.wareSuggested;
  });
  shedData = null;
  renderEditor();
  await recalcAndRender("shed");
  renderShedPanel();
}

function cancelShed() {
  shedData = null;
  renderShedPanel();
}

function renderShedPanel(errText) {
  const out = $("#shedOutput");
  const status = $("#shedStatus");
  const btnCancel = $("#btnCancelShed");
  const btnConfirm = $("#btnConfirmShed");
  if (!out) return;
  btnCancel.disabled = !shedData;
  btnConfirm.disabled = !(shedData && shedData.feasible && shedData.totalReduction > 0);

  if (shedAppliedMsg) {
    status.textContent = "已减载";
    status.className = "badge has-suggest";
    out.innerHTML = `<p class="sug-applied">${escapeHtml(shedAppliedMsg)}</p>
      <p class="muted" style="margin:4px 0 0">可继续编辑架体，或重新试算下一轮减载。</p>`;
    return;
  }
  if (errText) {
    status.textContent = "试算失败";
    status.className = "badge";
    out.innerHTML = `<p class="sug-error">⚠ ${escapeHtml(errText)}</p>`;
    return;
  }
  if (!shedData) {
    status.textContent = "未试算";
    status.className = "badge";
    out.innerHTML = `<p class="muted" style="margin:8px 0 0">点击「开始减载试算」：后端以当前架体为基准，只调整各层坯件重量，按现有整架分摊规则求消除全部超载的<b>最小总减载量</b>，并对方案重新整架校验。试算不会改动当前架体，确认后才写入并自动保存。</p>`;
    return;
  }

  const m = shedData.metrics;
  if (!shedData.feasible) {
    status.textContent = "无解";
    status.className = "badge has-error";
    const remainRows = shedData.remainingOverloads.map((p) =>
      `<li class="error"><span class="itype">超载</span><span>${escapeHtml(p.layerName)} 立柱 ${escapeHtml(p.post)}：清空坯件后载荷 ${fmt(p.load)} kg &gt; 容量 ${fmt(p.capacity)} kg</span></li>`
    ).join("");
    out.innerHTML = `
      <p class="sug-error">🚫 ${escapeHtml(shedData.reason || "全部坯件清空仍有超载，仅靠减载无解")}</p>
      <div class="sug-metrics">
        <div class="sug-metric bad"><span>超载柱（减载前 → 全部清空）</span><b>${m.overloadCount.before} → ${m.overloadCount.after}</b><small>根</small></div>
        <div class="sug-metric"><span>全部清空减载量</span><b>${fmt(shedData.totalReduction)}</b><small>kg</small></div>
        <div class="sug-metric"><span>丢失载荷</span><b>${fmt(m.lostLoad.before)} → ${fmt(m.lostLoad.after)}</b><small>kg</small></div>
        <div class="sug-metric"><span>传到台面</span><b>${fmt(m.carriedAtBottom.before)} → ${fmt(m.carriedAtBottom.after)}</b><small>kg</small></div>
      </div>
      <div class="sub-title">剩余超载柱（清空全部坯件后整架校验）</div>
      <ul class="sug-issues">${remainRows}</ul>
      ${shedLayerTable(shedData)}`;
    return;
  }

  if (shedData.totalReduction === 0) {
    status.textContent = "无需减载";
    status.className = "badge";
    out.innerHTML = `
      <p class="sug-applied" style="color:var(--normal)">✓ 当前架体没有有限容量立柱超载，无需减载。</p>
      ${shedLayerTable(shedData)}`;
    return;
  }

  status.textContent = "有解 · 待确认";
  status.className = "badge has-suggest";
  out.innerHTML = `
    <div class="sug-metrics">
      <div class="sug-metric good"><span>超载柱（减载前 → 减载后）</span><b>${m.overloadCount.before} → ${m.overloadCount.after}</b><small>根</small></div>
      <div class="sug-metric good"><span>最小总减载量</span><b>${fmt(shedData.totalReduction)}</b><small>kg</small></div>
      <div class="sug-metric"><span>丢失载荷</span><b>${fmt(m.lostLoad.before)} → ${fmt(m.lostLoad.after)}</b><small>kg</small></div>
      <div class="sug-metric"><span>传到台面</span><b>${fmt(m.carriedAtBottom.before)} → ${fmt(m.carriedAtBottom.after)}</b><small>kg</small></div>
    </div>
    <p class="hint" style="margin:2px 0 6px">下方为重新整架校验后的逐柱/逐层结果；点「✓ 按建议重量减载并保存」才写入架体并自动保存。</p>
    ${shedLayerTable(shedData)}`;
}

function shedLayerTable(d) {
  const rows = d.layers.map((L) => `
    <tr class="${L.changed ? "target" : ""}">
      <td>${escapeHtml(L.name)}</td>
      <td>${fmt(L.wareOriginal)}</td>
      <td><b>${fmt(L.wareSuggested)}</b></td>
      <td class="down">${L.changed ? "−" + fmt(L.reduction) : "—"}</td>
      <td>${L.overloadBefore} → <b>${L.overloadAfter}</b></td>
      <td>${fmt(L.lostBefore)} → <b>${fmt(L.lostAfter)}</b></td>
    </tr>`).join("");
  return `
    <div class="sug-table-wrap">
      <table class="src-table sug-table">
        <tr class="head"><td>层</td><td>原坯件重量 kg</td><td>建议重量 kg</td><td>本层减量</td><td>超载柱（前→后）</td><td>丢失载荷 kg（前→后）</td></tr>
        ${rows}
      </table>
    </div>`;
}

$("#btnShed").addEventListener("click", runLoadShed);
$("#btnCancelShed").addEventListener("click", cancelShed);
$("#btnConfirmShed").addEventListener("click", confirmShed);

/* ================= 层架抗倾覆试算面板 ================= */
/* 架体发生编辑/重置：上次抗倾覆试算作废（试算只读，不存在确认写入） */
function invalidateOverturn(opts) {
  overturnData = null;
  overturnErr = null;
  overturnSeq++;
  clearTimeout(overturnTimer);
  if (!opts || !opts.silent) {
    renderOverturnPanel();
    drawTop();
  }
}

function overturnInputs() {
  const ax = parseFloat($("#overturnAx").value);
  const ay = parseFloat($("#overturnAy").value);
  if (!Number.isFinite(ax) || !Number.isFinite(ay)) {
    throw new Error("水平加速度 x/y 分量必须是有限数值（单位 g，可为负）");
  }
  return { ax, ay };
}

async function runOverturn() {
  if (!state) return;
  let ax, ay;
  try {
    ({ ax, ay } = overturnInputs());
  } catch (e) {
    overturnData = null;
    overturnErr = e.message;
    renderOverturnPanel();
    drawTop();
    return;
  }
  const status = $("#overturnStatus");
  overturnErr = null;
  status.textContent = "试算中…";
  status.className = "badge has-overturn";
  const seq = ++overturnSeq;
  let data;
  try {
    const res = await fetch("/api/overturn", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ state, ax, ay }),
    });
    data = await res.json().catch(() => null);
    if (!res.ok) {
      if (seq !== overturnSeq) return;
      overturnData = null;
      overturnErr = (data && data.error) || `试算失败（HTTP ${res.status}）`;
      renderOverturnPanel();
      drawTop();
      return;
    }
  } catch (e) {
    data = overturnLocal(state, ax, ay); // 离线兜底：与后端同一套算法
  }
  if (seq !== overturnSeq) return; // 期间已有编辑/重置，丢弃过期响应
  overturnData = data;
  renderOverturnPanel();
  drawTop();
}

function cancelOverturn() {
  overturnData = null;
  overturnErr = null;
  overturnSeq++;
  clearTimeout(overturnTimer);
  renderOverturnPanel();
  drawTop();
}

const VERDICT_TEXT = { stable: "稳定", critical: "临界（投影恰在支撑边界）", overturn: "倾覆" };
const VERDICT_COLOR = { stable: "var(--normal)", critical: "var(--warn)", overturn: "var(--error)" };

function renderOverturnPanel() {
  const out = $("#overturnOutput");
  const status = $("#overturnStatus");
  const btnCancel = $("#btnCancelOverturn");
  if (!out) return;
  btnCancel.disabled = !overturnData && !overturnErr;

  if (overturnErr) {
    status.textContent = "参数有误";
    status.className = "badge";
    out.innerHTML = `<p class="ov-reason">⚠ ${escapeHtml(overturnErr)}</p>`;
    return;
  }
  if (!overturnData) {
    status.textContent = "未试算";
    status.className = "badge";
    out.innerHTML = `<p class="muted" style="margin:8px 0 0">填写水平加速度 x/y 分量（单位 g，0=静止）后点击「开始抗倾覆试算」。
      试算只读取当前架体、不写盘；编辑架体后旧试算立即作废。落点凸包与整架重心/投影点会叠加显示在俯视图。</p>`;
    return;
  }

  const d = overturnData;
  if (!d.evaluable) {
    status.textContent = "无法评估";
    status.className = "badge";
    const layerRows = (d.layers || []).map((L) => `
      <tr>
        <td>${escapeHtml(L.name)}</td>
        <td>${fmt(L.z)}</td>
        <td>${fmt(L.cx)}, ${fmt(L.cy)}</td>
        <td>${fmt(L.weight)}</td>
        <td>${L.supportedCount === 0
          ? '<span style="color:var(--error)">无有效承托</span>'
          : `${L.supportedCount} 根有效柱`}</td>
      </tr>`).join("");
    out.innerHTML = `
      <p class="ov-reason">🚫 <b>无法评估：</b>${escapeHtml(d.reason || "架体条件不足")}</p>
      <div class="ov-table-wrap">
        <table class="src-table sug-table ov-table">
          <tr class="head"><td>层</td><td>累计高度 z mm</td><td>棚板中心 (x, y) mm</td><td>重量 kg</td><td>有效承托</td></tr>
          ${layerRows}
        </table>
      </div>`;
    return;
  }

  const v = d.verdict;
  status.textContent = VERDICT_TEXT[v].split("（")[0];
  status.className = "badge has-overturn " + v;
  const marginCls = v === "overturn" ? "bad" : v === "critical" ? "warn" : "";
  const layerRows = d.layers.map((L) => `
    <tr>
      <td>${escapeHtml(L.name)}</td>
      <td>${fmt(L.height)} → <b>${fmt(L.z)}</b></td>
      <td>${fmt(L.cx)}, ${fmt(L.cy)}</td>
      <td>${fmt(L.weight)}</td>
      <td>${L.supportedCount} 根</td>
    </tr>`).join("");
  out.innerHTML = `
    <p class="ov-verdict ${v}">
      <b>${VERDICT_TEXT[v]}</b>
      <span class="sub">水平加速度 (${fmt(d.ax)}, ${fmt(d.ay)}) g · 支撑边界判定：投影在
        ${v === "stable" ? "多边形内，尚有正裕量" : v === "critical" ? "边界上" : "多边形外"}</span>
    </p>
    <div class="ov-metrics">
      <div class="ov-metric"><span>整架总重</span><b>${fmt(d.totalWeight)}</b><small>kg（各层板+坯）</small></div>
      <div class="ov-metric"><span>整架重心 G</span><b>${fmt(d.cg.x)}, ${fmt(d.cg.y)}</b><small>高度 ${fmt(d.cg.z)} mm</small></div>
      <div class="ov-metric"><span>等效平移 (ax·Gz, ay·Gz)</span><b>${fmt(d.shift.x)}, ${fmt(d.shift.y)}</b><small>mm</small></div>
      <div class="ov-metric ${marginCls}"><span>到支撑边界带符号距离</span><b>${fmt(d.margin)}</b><small>mm（正=稳定 / 0=临界 / 负=倾覆）</small></div>
    </div>
    <p class="hint" style="margin:2px 0 6px">
      投影点 <b>(${fmt(d.projection.x)}, ${fmt(d.projection.y)})</b>；
      底层非悬空立柱落点 ${d.supportPointCount} 个，凸包 ${d.polygon.length} 个顶点。
      俯视图中蓝色虚线为支撑边界、蓝环为整架重心、彩色点为平移后投影。
    </p>
    <div class="ov-table-wrap">
      <table class="src-table sug-table ov-table">
        <tr class="head"><td>层</td><td>层高 → 累计高度 z mm</td><td>重心（棚板中心）(x, y) mm</td><td>重量 kg</td><td>有效承托柱</td></tr>
        ${layerRows}
      </table>
    </div>`;
}

/* 抗倾覆俯视图叠加：支撑凸包（蓝虚线+浅蓝填充）、整架重心（蓝环）、平移投影（判定色） */
function drawOverturnOverlay(ctx, tr) {
  const legend = $("#overturnLegend");
  if (legend) {
    legend.classList.toggle("on", !!overturnData);
    const swatch = legend.querySelector(".ot-proj-swatch");
    if (swatch && overturnData && overturnData.evaluable) {
      swatch.className = "ot-proj-swatch " + overturnData.verdict;
    }
  }
  if (!overturnData) return;
  const d = overturnData;

  if (d.evaluable && d.polygon && d.polygon.length >= 3) {
    // 支撑多边形填充 + 虚线边界
    ctx.beginPath();
    d.polygon.forEach((pt, i) => {
      const x = tr.X(pt.x), y = tr.Y(pt.y);
      if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    });
    ctx.closePath();
    ctx.fillStyle = "rgba(70,184,232,0.10)";
    ctx.fill();
    ctx.strokeStyle = OVERTURN_COLOR;
    ctx.lineWidth = 1.8;
    ctx.setLineDash([7, 5]);
    ctx.stroke();
    ctx.setLineDash([]);
    // 顶点（底层非悬空落点）小方点
    ctx.fillStyle = OVERTURN_COLOR;
    d.polygon.forEach((pt) => {
      ctx.fillRect(tr.X(pt.x) - 2.5, tr.Y(pt.y) - 2.5, 5, 5);
    });

    // 平移向量箭头：重心 → 投影
    const g = d.cg, p = d.projection;
    const gx = tr.X(g.x), gy = tr.Y(g.y);
    const px = tr.X(p.x), py = tr.Y(p.y);
    if (Math.hypot(px - gx, py - gy) > 4) {
      ctx.strokeStyle = "rgba(70,184,232,0.85)";
      ctx.lineWidth = 1.6;
      ctx.beginPath();
      ctx.moveTo(gx, gy);
      ctx.lineTo(px, py);
      ctx.stroke();
      const ang = Math.atan2(py - gy, px - gx);
      ctx.beginPath();
      ctx.moveTo(px, py);
      ctx.lineTo(px - 8 * Math.cos(ang - 0.4), py - 8 * Math.sin(ang - 0.4));
      ctx.moveTo(px, py);
      ctx.lineTo(px - 8 * Math.cos(ang + 0.4), py - 8 * Math.sin(ang + 0.4));
      ctx.stroke();
    }

    // 整架重心：蓝色空心环 + 十字
    ctx.strokeStyle = OVERTURN_COLOR;
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.arc(gx, gy, 7, 0, Math.PI * 2);
    ctx.stroke();
    ctx.beginPath();
    ctx.moveTo(gx - 10, gy); ctx.lineTo(gx - 4, gy);
    ctx.moveTo(gx + 4, gy); ctx.lineTo(gx + 10, gy);
    ctx.moveTo(gx, gy - 10); ctx.lineTo(gx, gy - 4);
    ctx.moveTo(gx, gy + 4); ctx.lineTo(gx, gy + 10);
    ctx.stroke();
    ctx.fillStyle = OVERTURN_COLOR;
    ctx.font = "bold 10px sans-serif";
    ctx.fillText(`重心 G (${fmt(g.x)},${fmt(g.y)}) z=${fmt(g.z)}`, gx + 10, gy - 9);

    // 平移后投影点：颜色随判定（绿=稳定 / 黄=临界 / 红=倾覆）
    const vColor = d.verdict === "overturn" ? "#ef5b6b"
      : d.verdict === "critical" ? "#f0b03c" : "#34c08a";
    ctx.beginPath();
    ctx.arc(px, py, 6, 0, Math.PI * 2);
    ctx.fillStyle = vColor;
    ctx.fill();
    ctx.strokeStyle = "#0c1120";
    ctx.lineWidth = 1.4;
    ctx.stroke();
    ctx.fillStyle = vColor;
    ctx.font = "bold 10px sans-serif";
    ctx.fillText(`投影 (${fmt(p.x)},${fmt(p.y)}) d=${fmt(d.margin)} mm`, px + 10, py + 4);
  }
}

$("#btnOverturn").addEventListener("click", runOverturn);
$("#btnCancelOverturn").addEventListener("click", cancelOverturn);
// 修改加速度分量：已有试算时防抖重算（只重算试算，不触碰架体）；无试算时仅清参数错误提示
["overturnAx", "overturnAy"].forEach((id) => {
  const el = document.getElementById(id);
  if (!el) return;
  el.addEventListener("input", () => {
    clearTimeout(overturnTimer);
    overturnTimer = setTimeout(() => {
      if (overturnData) runOverturn();
      else if (overturnErr) { overturnErr = null; renderOverturnPanel(); }
    }, 250);
  });
});

/* ================= 逐层装货顺序规划面板 ================= */
/* 架体发生编辑/重置：上次规划作废（规划只读，不存在确认写入） */
function invalidateSequence(opts) {
  seqData = null;
  seqErr = null;
  seqReqSeq++;
  clearTimeout(seqTimer);
  if (!opts || !opts.silent) renderSeqPanel();
}

function seqInputs() {
  const ax = parseFloat($("#seqAx").value);
  const ay = parseFloat($("#seqAy").value);
  if (!Number.isFinite(ax) || !Number.isFinite(ay)) {
    throw new Error("水平加速度 x/y 分量必须是有限数值（单位 g，可为负）");
  }
  return { ax, ay };
}

async function runSequence() {
  if (!state) return;
  let ax, ay;
  try {
    ({ ax, ay } = seqInputs());
  } catch (e) {
    seqData = null;
    seqErr = e.message;
    renderSeqPanel();
    return;
  }
  const status = $("#seqStatus");
  seqErr = null;
  status.textContent = "规划中…";
  status.className = "badge has-seq";
  const seq = ++seqReqSeq;
  let data;
  try {
    const res = await fetch("/api/loading-sequence", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ state, ax, ay }),
    });
    data = await res.json().catch(() => null);
    if (!res.ok) {
      if (seq !== seqReqSeq) return;
      seqData = null;
      seqErr = (data && data.error) || `规划失败（HTTP ${res.status}）`;
      renderSeqPanel();
      return;
    }
  } catch (e) {
    data = loadingSequenceLocal(state, ax, ay); // 离线兜底：与后端同一套算法
  }
  if (seq !== seqReqSeq) return; // 期间已有编辑/重置，丢弃过期响应
  seqData = data;
  renderSeqPanel();
}

function cancelSequence() {
  seqData = null;
  seqErr = null;
  seqReqSeq++;
  clearTimeout(seqTimer);
  renderSeqPanel();
}

const SEQ_VERDICT_TEXT = { stable: "稳定", critical: "临界", overturn: "倾覆" };

function renderSeqPanel() {
  const out = $("#seqOutput");
  const status = $("#seqStatus");
  const btnCancel = $("#btnCancelSeq");
  if (!out) return;
  btnCancel.disabled = !seqData && !seqErr;

  if (seqErr) {
    status.textContent = "参数有误";
    status.className = "badge";
    out.innerHTML = `<p class="ov-reason">⚠ ${escapeHtml(seqErr)}</p>`;
    return;
  }
  if (!seqData) {
    status.textContent = "未规划";
    status.className = "badge";
    out.innerHTML = `<p class="muted" style="margin:8px 0 0">填写水平加速度 x/y 分量（单位 g，0=静止）后点击「开始装货顺序规划」。
      规划只读取当前架体、不写盘；编辑架体后旧结果立即作废。</p>`;
    return;
  }

  const d = seqData;
  if (!d.planable) {
    status.textContent = "无法规划";
    status.className = "badge";
    const cand = (d.candidates || []).map((c) =>
      `<li>${escapeHtml(c.name)}（第 ${c.index + 1} 层，坯件 ${fmt(c.wareWeight)} kg）</li>`).join("");
    out.innerHTML = `
      <p class="ov-reason">🚫 <b>无法规划：</b>${escapeHtml(d.reason || "架体条件不足")}</p>
      <p class="hint" style="margin:6px 0 2px">
        水平加速度 (${fmt(d.ax)}, ${fmt(d.ay)}) g；坯件重量大于零的层
        <b>${d.candidateCount}</b> 个（规划上限 ${d.candidateLimit} 个）：
      </p>
      <ul class="sug-issues" style="max-height:160px">${cand || '<li class="muted">无</li>'}</ul>`;
    return;
  }

  status.textContent = d.allStable ? "全程稳定" : "无法全程稳定";
  status.className = "badge has-seq " + (d.allStable ? "all-stable" : "has-error");

  const orderText = d.order.map((i) => {
    const L = state.layers[i];
    return L ? escapeHtml(L.name) : `第${i + 1}层`;
  }).join(" → ");
  const rows = d.steps.map((s) => {
    const vCls = s.verdict === "overturn" ? "bad" : s.verdict === "critical" ? "warn" : "good";
    return `<tr>
      <td>${s.step === 0 ? "初始" : s.step}</td>
      <td>${s.addedName
        ? `<b>${escapeHtml(s.addedName)}</b><small>（第 ${s.addedIndex + 1} 层，坯 ${fmt(s.addedWareWeight)} kg）</small>`
        : '<span class="muted">— 坯件全部置零 —</span>'}</td>
      <td>${fmt(s.cumulativeWareWeight)}</td>
      <td>${fmt(s.totalWeight)}</td>
      <td>${fmt(s.cg.x)}, ${fmt(s.cg.y)}<small> z=${fmt(s.cg.z)}</small></td>
      <td>${fmt(s.projection.x)}, ${fmt(s.projection.y)}</td>
      <td class="${vCls}"><b>${fmt(s.margin)}</b></td>
      <td class="${vCls}">${s.verdict === "stable" ? "🟢 稳定" : s.verdict === "critical" ? "🟡 临界" : "🔴 倾覆"}</td>
    </tr>`;
  }).join("");

  out.innerHTML = `
    <p class="ov-verdict ${d.allStable ? "stable" : "overturn"}">
      <b>${d.allStable ? "✓ 该顺序可全程稳定" : "⚠ 最优顺序仍有非正裕量，无法全程稳定"}</b>
      <span class="sub">水平加速度 (${fmt(d.ax)}, ${fmt(d.ay)}) g · 候选层 ${d.candidateCount} 个（上限 ${d.candidateLimit}）·
      全过程最小裕量出现在第 <b>${d.minMarginStep}</b> 步</span>
    </p>
    <div class="ov-metrics">
      <div class="ov-metric ${d.minMargin > 0 ? "" : "bad"}"><span>全过程最小裕量</span><b>${fmt(d.minMargin)}</b><small>mm（正=稳定 / 0=临界 / 负=倾覆）</small></div>
      <div class="ov-metric"><span>初始裕量（坯件全零）</span><b>${fmt(d.steps[0].margin)}</b><small>mm · 初始总重 ${fmt(d.steps[0].totalWeight)} kg（棚板自重）</small></div>
      <div class="ov-metric"><span>终态裕量（全部装好）</span><b>${fmt(d.steps[d.steps.length - 1].margin)}</b><small>mm · 总重 ${fmt(d.steps[d.steps.length - 1].totalWeight)} kg</small></div>
      <div class="ov-metric"><span>底层支撑点 / 凸包顶点</span><b>${d.supportPointCount} / ${d.polygon.length}</b><small>个</small></div>
    </div>
    <p class="hint" style="margin:2px 0 6px">
      <b>最优装货顺序：</b>${orderText}
      （动态规划最大化全过程最小裕量；同分按层序号字典序自下而上选取）
    </p>
    <div class="ov-table-wrap">
      <table class="src-table sug-table ov-table seq-table">
        <tr class="head">
          <td>步</td><td>本步新增层</td><td>累计坯件 kg</td><td>总重 kg</td>
          <td>重心 (x, y) mm</td><td>投影 (x, y) mm</td><td>带符号裕量 mm</td><td>稳定判定</td>
        </tr>
        ${rows}
      </table>
    </div>`;
}

$("#btnSeq").addEventListener("click", runSequence);
$("#btnCancelSeq").addEventListener("click", cancelSequence);
// 修改加速度分量：已有规划时防抖重算（只重算规划，不触碰架体）；无规划时仅清参数错误提示
["seqAx", "seqAy"].forEach((id) => {
  const el = document.getElementById(id);
  if (!el) return;
  el.addEventListener("input", () => {
    clearTimeout(seqTimer);
    seqTimer = setTimeout(() => {
      if (seqData && seqData.planable) runSequence();
      else if (seqErr) { seqErr = null; renderSeqPanel(); }
    }, 250);
  });
});

/* ================= 连续窑门通行试算面板 ================= */
/* 架体发生编辑/重置：旧试算立即失效（试算只读，不存在确认写入） */
function invalidateDoorTrial(opts) {
  doorData = null;
  doorErr = null;
  doorStale = false;
  doorReqSeq++;
  clearTimeout(doorTimer);
  // 行着色不依赖面板渲染，静默作废（架体编辑）时也立即清掉，避免旧红绿框残留
  markDoorRows();
  if (!opts || !opts.silent) {
    renderDoorPanel();
  }
}

function collectDoorInputs() {
  doorInputs = [...document.querySelectorAll("#doorList .door-row")].map((row) => ({
    center: num(row.querySelector('[data-df="center"]').value),
    width: num(row.querySelector('[data-df="width"]').value),
    height: num(row.querySelector('[data-df="height"]').value),
    maxShift: num(row.querySelector('[data-df="maxShift"]').value),
  }));
  return doorInputs;
}

function renderDoorEditor() {
  const list = $("#doorList");
  if (!list) return;
  if (!doorInputs.length) doorInputs = [];
  list.innerHTML = `<div class="door-rows">${doorInputs.map((d, i) => doorRowHtml(d, i)).join("")}</div>`;
  markDoorRows();
}

function doorRowHtml(d, i) {
  return `
  <div class="door-row" data-di="${i}">
    <span class="door-no">第${i + 1}道</span>
    <label>开口中心 c (mm)<input type="number" step="any" data-df="center" value="${d.center}"></label>
    <label>宽度 w (mm)<input type="number" step="any" data-df="width" value="${d.width}"></label>
    <label>高度 h (mm)<input type="number" step="any" data-df="height" value="${d.height}"></label>
    <label>本段最大横移 (mm)<input type="number" step="any" data-df="maxShift" value="${d.maxShift}" min="0"></label>
    <div class="door-order">
      <button type="button" data-dact="up" title="前移（先通过）">▲</button>
      <button type="button" data-dact="down" title="后移（后通过）">▼</button>
      <button type="button" data-dact="del" title="删除该门">✕</button>
    </div>
  </div>`;
}

/* 试算结果只给行着色，不重建输入框（避免输入中失焦） */
function markDoorRows() {
  document.querySelectorAll("#doorList .door-row").forEach((row) => {
    const i = +row.dataset.di;
    const d = doorData && (doorData.doors || [])[i];
    row.classList.toggle("is-blocked", !!(d && d.evaluated && !d.passable));
    row.classList.toggle("is-passable", !!(d && d.passable));
  });
}

async function runDoorTrial() {
  if (!state) return;
  const doors = collectDoorInputs();
  // 前端先做一遍基本校验（与后端一致），参数非法时直接提示，不发请求
  try {
    validateDoors(doors);
  } catch (e) {
    doorData = null;
    doorErr = e.message;
    doorStale = false;
    markDoorRows();
    renderDoorPanel();
    return;
  }
  const status = $("#doorStatus");
  doorErr = null;
  doorStale = false;
  status.textContent = "试算中…";
  status.className = "badge has-door";
  const seq = ++doorReqSeq;
  let data;
  try {
    const res = await fetch("/api/door-passage", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ state, doors }),
    });
    data = await res.json().catch(() => null);
    if (!res.ok) {
      if (seq !== doorReqSeq) return;
      doorData = null;
      doorErr = (data && data.error) || `试算失败（HTTP ${res.status}）`;
      markDoorRows();
      renderDoorPanel();
      return;
    }
  } catch (e) {
    data = doorPassageLocal(state, doors); // 离线兜底：与后端同一套算法
  }
  if (seq !== doorReqSeq) return; // 期间已有编辑/重置，丢弃过期响应
  doorData = data;
  markDoorRows();
  renderDoorPanel();
}

function validateDoors(doors) {
  doors.forEach((d, i) => {
    const label = `第 ${i + 1} 道门`;
    [["center", "开口横向中心"], ["width", "开口宽度"], ["height", "开口高度"], ["maxShift", "本段最大横移量"]]
      .forEach(([k, name]) => {
        if (!Number.isFinite(d[k])) throw new Error(`${label}${name}必须是数值（mm）`);
      });
    if (d.width <= 0) throw new Error(`${label}开口宽度必须大于 0`);
    if (d.height <= 0) throw new Error(`${label}开口高度必须大于 0`);
    if (d.maxShift < 0) throw new Error(`${label}本段最大横移量不能为负`);
  });
}

function cancelDoorTrial() {
  doorData = null;
  doorErr = null;
  doorStale = false;
  doorReqSeq++;
  clearTimeout(doorTimer);
  markDoorRows();
  renderDoorPanel();
}

function ivText(iv) {
  if (!iv) return "—";
  const l = iv.lo == null ? "-∞" : fmt(iv.lo);
  const h = iv.hi == null ? "+∞" : fmt(iv.hi);
  return `[${l}, ${h}]`;
}

const DOOR_BLOCK_TEXT = {
  height: "高度不足（直接阻断）",
  width: "单门宽度不足",
  reach: "横移序列不可达",
};

function renderDoorPanel() {
  const out = $("#doorOutput");
  const status = $("#doorStatus");
  const btnCancel = $("#btnCancelDoor");
  if (!out) return;
  btnCancel.disabled = !doorData && !doorErr && !doorStale;

  if (doorErr) {
    status.textContent = "参数有误";
    status.className = "badge";
    out.innerHTML = `<p class="ov-reason">⚠ ${escapeHtml(doorErr)}</p>`;
    return;
  }
  if (!doorData) {
    status.textContent = doorStale ? "结果已失效" : "未试算";
    status.className = "badge" + (doorStale ? " stale" : "");
    out.innerHTML = doorStale
      ? `<p class="ov-reason">⚠ 架体或门参数已改变，旧试算结果已失效，请重新点击「开始通行试算」。</p>`
      : `<p class="muted" style="margin:8px 0 0">按行进顺序录入窑门参数（开口中心 / 宽 / 高 / 本段最大横移，单位 mm）后点击「开始通行试算」。
         首门从横移 0 出发；试算只读取当前架体、不写盘，架体或门参数改变后旧结果立即失效。</p>`;
    return;
  }

  const d = doorData;
  if (doorStale) {
    status.textContent = "结果已失效";
    status.className = "badge stale";
  } else if (!d.evaluable) {
    status.textContent = "无法评估";
    status.className = "badge";
  } else if (d.passable) {
    status.textContent = d.doorCount ? "全程可通行" : "无门可过";
    status.className = "badge has-door";
  } else {
    status.textContent = `第${d.blockedAt + 1}道门阻断`;
    status.className = "badge has-door blocked";
  }

  const p = d.profile;
  const env = p.envelope;
  const slabHtml = (p.slabs || []).map((s) =>
    `<span class="s">${s.kind === "car" ? "台面" : "棚板"} ${escapeHtml(s.name)}
      <b>z=${fmt(s.z)}</b> <b>[${fmt(s.left)}, ${fmt(s.right)}]</b></span>`).join(" ｜ ");

  let body = `
    <div class="door-profile">
      <span>架体最高点（累加层高）<b>${fmt(p.totalHeight)}</b> mm</span>
      <span>横向外廓 <b>[${fmt(env.left)}, ${fmt(env.right)}]</b>（宽 <b>${fmt(env.width)}</b> mm）</span>
      <span>窑门 <b>${d.doorCount}</b> 道</span>
    </div>
    <p class="door-slabs muted" style="margin:2px 0 0">${slabHtml || '<span class="muted">（无外廓件）</span>'}</p>`;

  if (!d.evaluable) {
    out.innerHTML = body + `<p class="ov-reason">🚫 <b>无法评估：</b>${escapeHtml(d.reason || "架体条件不足")}</p>`;
    return;
  }
  if (d.note) {
    body += `<p class="hint" style="margin:6px 0 0">${escapeHtml(d.note)}</p>`;
  }

  if (!d.passable) {
    const bd = d.doors[d.blockedAt];
    let detail = "";
    if (bd.blockReason === "height") {
      const c = bd.conflict;
      detail = `门高 <b>${fmt(c.doorHeight)}</b> mm &lt; 架体最高点 <b>${fmt(c.totalHeight)}</b> mm，
        顶部尚差 <b class="bad">${fmt(c.shortage)}</b> mm；高度不足直接判为阻断，不再计算横移区间。`;
    } else if (bd.blockReason === "width") {
      const c = bd.conflict;
      detail = `本门可通过的横移区间为 ${ivText(c.interval)}（下界 &gt; 上界，区间为空）：
        门宽 <b>${fmt(c.doorWidth)}</b> mm 小于架体横向外廓宽 <b>${fmt(c.envelopeWidth)}</b> mm，
        至少还差 <b class="bad">${fmt(c.shortage)}</b> mm；该门即使单独评估也无法通过。`;
    } else if (bd.blockReason === "reach") {
      const c = bd.conflict;
      detail = `进入本门之前，把上一道门后的可达区间按本段最大横移量扩张得 ${ivText(c.reachExpanded)}，
        与本门可通过区间 ${ivText(c.doorInterval)} <b>没有交集</b>
        （缺口 <b class="bad">${fmt(c.shortage)}</b> mm，本门要求向${c.side === "left" ? "左" : "右"}调整）；
        虽然各门单独都能给出横移区间，但<b>连续通过的位置序列不存在</b>。`;
    }
    body += `
      <p class="ov-reason">🚫 <b>第 ${d.blockedAt + 1} 道门阻断（${DOOR_BLOCK_TEXT[bd.blockReason]}）：</b><br>${detail}</p>`;
  } else if (d.doorCount > 0) {
    const chips = (d.positions || []).map((q, i) =>
      `<span class="pos-chip">第${i + 1}道 · 横移 <b>${fmt(q.shift)}</b> mm
        <small>（本段移动 ${fmt(q.move)} / ≤ ${fmt(q.maxShift)}）</small></span>`).join("");
    body += `
      <p class="sug-applied" style="margin:8px 0 0">✓ ${d.doorCount} 道门全程可达。下面是回溯得到的一条满足各段横移限制的位置序列（首门从 0 出发）：</p>
      <div class="door-pos">${chips}</div>`;
  }

  if (d.doors.length) {
    const rows = d.doors.map((dr) => {
      const verdict = !dr.evaluated
        ? '<td colspan="3" class="muted">未评估（前序门已阻断）</td>'
        : dr.blockReason === "height"
        ? '<td colspan="3" class="bad">高度不足，阻断</td>'
        : dr.blockReason
        ? `<td class="bad">${ivText(dr.interval)}</td>
           <td>${ivText(dr.reachExpanded)}</td>
           <td class="bad">空（${DOOR_BLOCK_TEXT[dr.blockReason]}）</td>`
        : `<td>${ivText(dr.interval)}</td>
           <td>${ivText(dr.reachExpanded)}</td>
           <td class="good"><b>${ivText(dr.reachable)}</b></td>`;
      const clr = dr.clearance
        ? `<td class="${dr.clearance.top <= 0 ? "warn" : "good"}">${fmt(dr.clearance.top)}</td>
           <td class="${dr.clearance.left <= 0 ? "warn" : "good"}">${fmt(dr.clearance.left)}</td>
           <td class="${dr.clearance.right <= 0 ? "warn" : "good"}">${fmt(dr.clearance.right)}</td>`
        : '<td colspan="3" class="muted">—</td>';
      const judge = !dr.evaluated
        ? '<span class="muted">—</span>'
        : dr.passable
          ? '<span style="color:var(--normal)">✓ 可通过</span>'
          : `<span class="bad">✕ ${DOOR_BLOCK_TEXT[dr.blockReason] || "阻断"}</span>`;
      return `<tr class="${dr.evaluated && !dr.passable ? "blocked" : ""}">
        <td>${dr.index + 1}</td>
        <td>${fmt(dr.center)}<br><small>${fmt(dr.width)} × ${fmt(dr.height)}</small></td>
        <td><small>[${fmt(dr.opening.left)}, ${fmt(dr.opening.right)}]</small></td>
        <td>${fmt(dr.maxShift)}</td>
        ${verdict}
        ${clr}
        <td>${dr.chosenShift == null ? "—" : `<b>${fmt(dr.chosenShift)}</b>`}<br><small>${dr.move == null ? "" : `本段移动 ${fmt(dr.move)}`}</small></td>
        <td>${judge}</td>
      </tr>`;
    }).join("");
    body += `
      <div class="door-table-wrap">
        <table class="src-table sug-table ov-table door-table">
          <tr class="head">
            <td>门序</td><td>中心 c<br><small>宽×高</small></td><td>开口左右沿</td><td>本段<br>最大横移</td>
            <td>本门可通过区间</td><td>扩张后可达</td><td>交集(可达区间)</td>
            <td>顶部净空</td><td>左净空</td><td>右净空</td>
            <td>选定横移<br><small>(mm)</small></td><td>判定</td>
          </tr>
          ${rows}
        </table>
      </div>
      <p class="hint" style="margin:0">所有长度单位均为 mm；闭区间端点相等（净空 0）视为刚好可通过。左/右/顶部净空为回溯选定横移位置下的值。</p>`;
  }
  out.innerHTML = body;
}

$("#btnDoor").addEventListener("click", runDoorTrial);
$("#btnCancelDoor").addEventListener("click", cancelDoorTrial);
$("#btnAddDoor").addEventListener("click", () => {
  collectDoorInputs();
  doorInputs.push({ center: 0, width: 1400, height: 1000, maxShift: 100 });
  renderDoorEditor();
  markDoorResultStale();
  scheduleDoorRun();
});
/* 门行内：上移 / 下移 / 删除（事件委托，不影响输入框焦点） */
$("#doorList").addEventListener("click", (e) => {
  const btn = e.target.closest("[data-dact]");
  if (!btn) return;
  collectDoorInputs();
  const row = e.target.closest(".door-row");
  const i = +row.dataset.di;
  const act = btn.dataset.dact;
  if (act === "del") {
    doorInputs.splice(i, 1);
  } else if (act === "up" && i > 0) {
    [doorInputs[i - 1], doorInputs[i]] = [doorInputs[i], doorInputs[i - 1]];
  } else if (act === "down" && i < doorInputs.length - 1) {
    [doorInputs[i + 1], doorInputs[i]] = [doorInputs[i], doorInputs[i + 1]];
  } else {
    return;
  }
  renderDoorEditor();
  markDoorResultStale();
  scheduleDoorRun();
});
/* 门参数输入：仅采集（不重建 DOM，保持焦点），有旧结果时防抖自动重算并标记失效 */
$("#doorList").addEventListener("input", () => {
  collectDoorInputs();
  markDoorResultStale();
  scheduleDoorRun();
});

/* 门参数变化后的防抖重算：只有已经试算过（有结果或参数错误）才自动重跑 */
function scheduleDoorRun() {
  clearTimeout(doorTimer);
  doorTimer = setTimeout(() => {
    if (doorData || doorErr) runDoorTrial();
  }, 300);
}

/* 门参数变化但尚未重算：标记旧结果失效（不立即清空，重算后替换） */
function markDoorResultStale() {
  doorErr = null;
  if (doorData && !doorStale) {
    doorStale = true;
    renderDoorPanel();
  }
}

/* ================= 轮组承载试算面板 ================= */
/* 架体发生编辑/重置：上次轮组试算立即作废（试算只读，不存在确认写入） */
function invalidateWheelTrial(opts) {
  wheelData = null;
  wheelErr = null;
  wheelStale = false;
  wheelReqSeq++;
  clearTimeout(wheelTimer);
  if (!opts || !opts.silent) {
    renderWheelPanel();
    drawTop();
  }
}

function collectWheelInputs() {
  return {
    xLeft: parseFloat($("#wheelXLeft").value),
    xRight: parseFloat($("#wheelXRight").value),
    yFront: parseFloat($("#wheelYFront").value),
    yRear: parseFloat($("#wheelYRear").value),
    ratedFL: parseFloat($("#wheelRatedFL").value),
    ratedFR: parseFloat($("#wheelRatedFR").value),
    ratedRL: parseFloat($("#wheelRatedRL").value),
    ratedRR: parseFloat($("#wheelRatedRR").value),
  };
}

/* 与后端 _parse_wheel_params / wheel_load_trial 一致的前置校验 */
function validateWheelInputs(p, car) {
  const coordNames = {
    xLeft: "左轮位 X 坐标 xLeft", xRight: "右轮位 X 坐标 xRight",
    yFront: "前轮位 Y 坐标 yFront", yRear: "后轮位 Y 坐标 yRear",
  };
  Object.entries(coordNames).forEach(([k, name]) => {
    if (!Number.isFinite(p[k])) throw new Error(`${name}必须是有限数值（mm）`);
  });
  [["ratedFL", "左前轮"], ["ratedFR", "右前轮"], ["ratedRL", "左后轮"], ["ratedRR", "右后轮"]]
    .forEach(([k, name]) => {
      if (!Number.isFinite(p[k])) throw new Error(`${name}额定载荷必须是有限数值（kg）`);
      if (p[k] <= 0) throw new Error(`${name}额定载荷必须大于 0（收到 ${fmt(p[k])} kg）`);
    });
  if (p.xRight <= p.xLeft) {
    throw new Error(`左右轮位 X 坐标必须严格满足 xLeft < xRight：收到 xLeft=${fmt(p.xLeft)}、xRight=${fmt(p.xRight)}（轮距 ${fmt(p.xRight - p.xLeft)} mm），轮距不正`);
  }
  if (p.yRear <= p.yFront) {
    throw new Error(`前后轮位 Y 坐标必须严格满足 yFront < yRear：收到 yFront=${fmt(p.yFront)}、yRear=${fmt(p.yRear)}（轴距 ${fmt(p.yRear - p.yFront)} mm），轮距不正`);
  }
  [["左前轮 FL", p.xLeft, p.yFront], ["右前轮 FR", p.xRight, p.yFront],
   ["左后轮 RL", p.xLeft, p.yRear], ["右后轮 RR", p.xRight, p.yRear]]
    .forEach(([name, x, y]) => {
      if (x < 0 || x > car.w || y < 0 || y > car.d) {
        throw new Error(`${name}轮位（${fmt(x)}, ${fmt(y)}）越出台面矩形（0~${fmt(car.w)} × 0~${fmt(car.d)} mm）`);
      }
    });
}

async function runWheelTrial() {
  if (!state) return;
  const p = collectWheelInputs();
  try {
    validateWheelInputs(p, state.car);
  } catch (e) {
    wheelData = null;
    wheelErr = e.message;
    wheelStale = false;
    renderWheelPanel();
    drawTop();
    return;
  }
  const status = $("#wheelStatus");
  wheelErr = null;
  wheelStale = false;
  status.textContent = "试算中…";
  status.className = "badge has-wheel";
  const seq = ++wheelReqSeq;
  let data;
  try {
    const res = await fetch("/api/wheel-load", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ state, ...p }),
    });
    data = await res.json().catch(() => null);
    if (!res.ok) {
      if (seq !== wheelReqSeq) return;
      wheelData = null;
      wheelErr = (data && data.error) || `试算失败（HTTP ${res.status}）`;
      renderWheelPanel();
      drawTop();
      return;
    }
  } catch (e) {
    data = wheelLoadLocal(state, p); // 离线兜底：与后端同一套算法
  }
  if (seq !== wheelReqSeq) return; // 期间已有编辑/重置，丢弃过期响应
  wheelData = data;
  renderWheelPanel();
  drawTop();
}

function cancelWheelTrial() {
  wheelData = null;
  wheelErr = null;
  wheelStale = false;
  wheelReqSeq++;
  clearTimeout(wheelTimer);
  renderWheelPanel();
  drawTop();
}

function utilText(w) {
  if (w.utilization == null) return "不限";
  const pct = Math.round(w.utilization * 1000) / 10;
  return pct + "%";
}

function renderWheelPanel() {
  const out = $("#wheelOutput");
  const status = $("#wheelStatus");
  const btnCancel = $("#btnCancelWheel");
  if (!out) return;
  btnCancel.disabled = !wheelData && !wheelErr && !wheelStale;

  if (wheelErr) {
    status.textContent = "参数有误";
    status.className = "badge";
    out.innerHTML = `<p class="ov-reason">⚠ ${escapeHtml(wheelErr)}</p>`;
    return;
  }
  if (!wheelData) {
    status.textContent = wheelStale ? "结果已失效" : "未试算";
    status.className = "badge" + (wheelStale ? " stale" : "");
    out.innerHTML = wheelStale
      ? `<p class="ov-reason">⚠ 架体或轮组参数已改变，旧试算结果已失效，请重新点击「开始轮组试算」。</p>`
      : `<p class="muted" style="margin:8px 0 0">填写台面内的左/右轮位 X 坐标、前/后轮位 Y 坐标（mm）与四轮额定载荷（kg）后
         点击「开始轮组试算」。点载荷取底层非悬空立柱落点与实载；越出轮组矩形时按杠杆外推、负反力不截零。
         试算只读取当前架体、不写盘，编辑架体后旧结果立即失效。</p>`;
    return;
  }

  const d = wheelData;
  if (!d.evaluable) {
    status.textContent = "无法试算";
    status.className = "badge";
    out.innerHTML = `
      <div class="wheel-summary">
        <span>轮距 X <b>${fmt(d.trackX)}</b> mm</span>
        <span>轴距 Y <b>${fmt(d.wheelbaseY)}</b> mm</span>
        <span>台面 <b>${fmt(d.car.w)} × ${fmt(d.car.d)}</b> mm</span>
      </div>
      <p class="ov-reason">🚫 <b>无法试算：</b>${escapeHtml(d.reason || "架体条件不足")}</p>`;
    return;
  }

  const overN = d.wheels.filter((w) => w.overloaded).length;
  const negN = d.wheels.filter((w) => w.negative).length;
  if (wheelStale) {
    status.textContent = "结果已失效";
    status.className = "badge stale";
  } else if (overN || negN) {
    status.textContent = `${overN} 轮超载 / ${negN} 轮负反力`;
    status.className = "badge has-wheel blocked";
  } else {
    status.textContent = "四轮正常";
    status.className = "badge has-wheel";
  }

  const wheelRows = d.wheels.map((w) => {
    const tags = [];
    if (w.overloaded) tags.push('<span class="wh-tag bad">超载</span>');
    if (w.negative) tags.push('<span class="wh-tag neg">负反力</span>');
    if (!tags.length) tags.push('<span class="wh-tag ok">正常</span>');
    const cls = w.negative ? "neg" : w.overloaded ? "bad" : "good";
    return `<tr class="${w.overloaded ? "is-over" : ""}">
      <td>${w.name} <small>${w.key}</small></td>
      <td>${fmt(w.x)}, ${fmt(w.y)}</td>
      <td>${fmt(w.rating)}</td>
      <td class="${cls}"><b>${fmt(w.reaction)}</b></td>
      <td class="${cls}">${utilText(w)}</td>
      <td>${tags.join(" ")}</td>
    </tr>`;
  }).join("");

  const pointRows = d.pointLoads.map((p) => `
    <tr class="${p.outsideWheelRect ? "is-outside" : ""}">
      <td>${escapeHtml(p.post)}</td>
      <td>${fmt(p.x)}, ${fmt(p.y)}</td>
      <td>${fmt(p.load)}</td>
      <td>${p.overlap ? `<span class="wh-tag ovl">与 ${p.overlapWith.map(escapeHtml).join("、")} 重叠</span>` : "—"}</td>
      <td>${p.outsideWheelRect ? '<span class="wh-tag warn">越出轮组矩形（外推）</span>' : "—"}</td>
    </tr>`).join("");

  const riskHtml = d.risks.length
    ? `<ul class="sug-issues wheel-risks">${d.risks.map((r) => `
        <li class="${r.level === "error" ? "is-added" : ""}">
          <span class="itype">${r.type === "wheel-overload" ? "超载" : r.type === "negative-reaction" ? "负反力" : "外推"}</span>
          <span>${escapeHtml(r.message)}</span>
        </li>`).join("")}</ul>`
    : '<p class="sug-applied" style="margin:6px 0 0">✓ 四轮反力均未超过额定载荷，且无负反力。</p>';

  const c = d.checks;
  const checkRow = (label, row, unit) => `
    <tr>
      <td>${label}</td>
      <td>${fmt(row.reactionMoment != null ? row.reactionMoment : row.reactionSum)}</td>
      <td>${fmt(row.loadMoment != null ? row.loadMoment : row.loadSum)}</td>
      <td class="${Math.abs(row.residual) <= 0.01 ? "good" : "bad"}"><b>${fmt(row.residual)}</b> <small>${unit}</small></td>
      <td>${row.ok ? '<span class="wh-tag ok">✓ 一致</span>' : '<span class="wh-tag bad">✕ 不一致</span>'}</td>
    </tr>`;

  out.innerHTML = `
    <div class="wheel-summary">
      <span>传到台面总载荷 <b>${fmt(d.totalLoad)}</b> kg</span>
      <span>轮距 X（左右） <b>${fmt(d.trackX)}</b> mm</span>
      <span>轴距 Y（前后） <b>${fmt(d.wheelbaseY)}</b> mm</span>
      <span>底层点载荷 <b>${d.pointLoadCount}</b> 个${d.overhangCount ? `（<b style="color:var(--warn)">${d.overhangCount} 个越出轮组矩形</b>）` : ""}</span>
    </div>
    ${riskHtml}
    <div class="ov-table-wrap">
      <table class="src-table sug-table ov-table wheel-table">
        <tr class="head"><td>车轮</td><td>轮位 (x, y) mm</td><td>额定 kg</td><td>反力 kg</td><td>利用率</td><td>风险标记</td></tr>
        ${wheelRows}
      </table>
    </div>
    <p class="hint" style="margin:2px 0 4px">点载荷明细（底层非悬空立柱；重叠落点在分配时合并为一个点，负反力不截零）：</p>
    <div class="ov-table-wrap">
      <table class="src-table sug-table ov-table wheel-table">
        <tr class="head"><td>底层立柱</td><td>落点 (x, y) mm</td><td>实载 kg</td><td>落点重叠</td><td>相对轮组矩形</td></tr>
        ${pointRows || '<tr><td colspan="5" class="muted">无</td></tr>'}
      </table>
    </div>
    <p class="hint" style="margin:2px 0 4px">平衡核对（反力侧 − 点载荷侧；残差为 0.01 kg 级取整误差时视为一致）：</p>
    <div class="ov-table-wrap">
      <table class="src-table sug-table ov-table wheel-table">
        <tr class="head"><td>核对项</td><td>四轮反力侧</td><td>输入点载荷侧</td><td>残差</td><td>判定</td></tr>
        ${checkRow("竖向力之和（kg）", c.force, "kg")}
        ${checkRow(`绕 X 力矩（${escapeHtml(c.momentX.axis)}，kg·mm）`, c.momentX, "kg·mm")}
        ${checkRow(`绕 Y 力矩（${escapeHtml(c.momentY.axis)}，kg·mm）`, c.momentY, "kg·mm")}
      </table>
    </div>`;
}

/* 轮组俯视图叠加：轮组矩形（橙色虚线）+ 四轮标记（绿=正常 / 红=超载 / 红环下箭头=负反力） */
function drawWheelOverlay(ctx, tr) {
  const legend = $("#wheelLegend");
  if (legend) legend.classList.toggle("on", !!wheelData);
  if (!wheelData || !wheelData.evaluable) return;
  const d = wheelData;

  // 轮组矩形
  const x0 = tr.X(d.xLeft), x1 = tr.X(d.xRight);
  const y0 = tr.Y(d.yRear), y1 = tr.Y(d.yFront);
  ctx.fillStyle = "rgba(255,159,67,0.07)";
  ctx.fillRect(x0, y0, x1 - x0, y1 - y0);
  ctx.strokeStyle = WHEEL_COLOR;
  ctx.lineWidth = 1.6;
  ctx.setLineDash([7, 5]);
  ctx.strokeRect(x0, y0, x1 - x0, y1 - y0);
  ctx.setLineDash([]);

  d.wheels.forEach((w) => {
    const cx = tr.X(w.x), cy = tr.Y(w.y);
    const col = w.negative ? "#ef5b6b" : w.overloaded ? "#ef5b6b" : "#34c08a";
    // 车轮画成圆角小方块（区别于立柱圆点）
    ctx.fillStyle = w.negative ? "rgba(239,91,107,0.18)" : col;
    ctx.strokeStyle = col;
    ctx.lineWidth = 2;
    const s = 7;
    ctx.beginPath();
    ctx.rect(cx - s, cy - s, s * 2, s * 2);
    ctx.fill();
    ctx.stroke();
    if (w.negative) {
      // 负反力：方块内向下箭头，表示需要向下拉住台面
      ctx.beginPath();
      ctx.moveTo(cx, cy - 3.5); ctx.lineTo(cx, cy + 3.5);
      ctx.moveTo(cx - 3, cy + 1); ctx.lineTo(cx, cy + 4); ctx.lineTo(cx + 3, cy + 1);
      ctx.stroke();
    }
    ctx.fillStyle = col;
    ctx.font = "bold 10px sans-serif";
    ctx.fillText(`${w.name} ${fmt(w.reaction)} kg`, cx + 10, cy - 6);
    ctx.font = "9px sans-serif";
    ctx.fillStyle = "#8b97ad";
    ctx.fillText(`额定 ${fmt(w.rating)} · ${utilText(w)}`, cx + 10, cy + 6);
  });

  // 越出轮组矩形的点载荷用橙色 ◇ 标出
  d.pointLoads.forEach((p) => {
    if (!p.outsideWheelRect) return;
    const cx = tr.X(p.x), cy = tr.Y(p.y);
    ctx.strokeStyle = "#ff9f43";
    ctx.lineWidth = 1.6;
    ctx.beginPath();
    ctx.moveTo(cx, cy - 6); ctx.lineTo(cx + 6, cy); ctx.lineTo(cx, cy + 6); ctx.lineTo(cx - 6, cy);
    ctx.closePath();
    ctx.stroke();
  });
}

$("#btnWheel").addEventListener("click", runWheelTrial);
$("#btnCancelWheel").addEventListener("click", cancelWheelTrial);
/* 轮参数输入：有旧结果时先标记失效并防抖重算；无结果时仅清参数错误提示 */
[
  "wheelXLeft", "wheelXRight", "wheelYFront", "wheelYRear",
  "wheelRatedFL", "wheelRatedFR", "wheelRatedRL", "wheelRatedRR",
].forEach((id) => {
  const el = document.getElementById(id);
  if (!el) return;
  el.addEventListener("input", () => {
    markWheelResultStale();
    clearTimeout(wheelTimer);
    wheelTimer = setTimeout(() => {
      if (wheelData) runWheelTrial();
      else if (wheelErr) { wheelErr = null; renderWheelPanel(); }
    }, 300);
  });
});

/* 轮参数变化但尚未重算：标记旧结果失效（不立即清空，重算后替换） */
function markWheelResultStale() {
  wheelErr = null;
  if (wheelData && !wheelStale) {
    wheelStale = true;
    renderWheelPanel();
  }
}

/* ================= 画布随窗口缩放重绘 ================= */
window.addEventListener("resize", debounce(() => { drawTop(); drawSide(); }, 150));

/* ================= 启动 ================= */
async function init() {
  setSaveState("", "加载中…");
  try {
    const res = await fetch("/api/result");
    const r = await res.json();
    result = r;
    const sres = await fetch("/api/state");
    state = await sres.json();
    // 读取时携带架体版本：此后所有保存都基于该版本做乐观并发控制
    const hv = parseInt(sres.headers.get("X-Rack-Version"), 10);
    savedVersion = Number.isInteger(hv) && hv > 0 ? hv : null;
  } catch (err) {
    alert("无法连接后端计算服务：" + err);
    return;
  }
  renderEditor();
  renderDoorEditor();
  renderConflictBanner();
  renderResults();
  markLayerBadges();
  setSaveState("saved", "✓ 就绪（v" + savedVersion + "）" + new Date().toLocaleTimeString("zh-CN"));
}
init();

/* ======================================================================
 * 离线兜底：后端不可用时在浏览器内执行同一套分摊算法（逻辑与 app/calc.py 对齐）
 * ==================================================================== */
function calculateLocal(raw, failed) {
  const failedSet = new Set((failed || []).map((f) => `${f.layer}|${f.post}`));
  const car = {
    name: (raw.car && raw.car.name) || "窑车台面",
    w: Math.max(0, num(raw.car && raw.car.w)),
    d: Math.max(0, num(raw.car && raw.car.d)),
  };
  const layers = (raw.layers || []).map((l0, idx) => {
    const b0 = l0.board || {};
    return {
      id: l0.id || "L" + idx,
      name: l0.name || "第" + (idx + 1) + "层",
      height: Math.max(0, num(l0.height)),
      board: { x: num(b0.x), y: num(b0.y), w: Math.max(0, num(b0.w)), d: Math.max(0, num(b0.d)), weight: Math.max(0, num(b0.weight)) },
      wareWeight: Math.max(0, num(l0.wareWeight)),
      posts: (l0.posts || []).map((p0, j) => ({ id: p0.id || "P" + (j + 1), x: num(p0.x), y: num(p0.y), capacity: num(p0.capacity) })),
    };
  });
  const n = layers.length;
  const inRect = (x, y, r) => x >= r.x && x <= r.x + r.w && y >= r.y && y <= r.y + r.d;
  const R = (v) => Math.round(v * 100) / 100;
  const incoming = Array.from({ length: n }, () => []);
  const issues = [];
  const outLayers = [];
  let totalOwn = 0, totalLost = 0;
  const carRect = { x: 0, y: 0, w: car.w, d: car.d };

  for (let i = n - 1; i >= 0; i--) {
    const L = layers[i], b = L.board;
    const bw = b.weight, ww = L.wareWeight;
    totalOwn += bw + ww;
    const points = incoming[i];
    const loadIn = points.reduce((s, p) => s + p.items.reduce((a, it) => a + it.amount, 0), 0);
    const total = bw + ww + loadIn;
    const supportRect = i === 0 ? carRect : { x: layers[i - 1].board.x, y: layers[i - 1].board.y, w: layers[i - 1].board.w, d: layers[i - 1].board.d };
    const supportName = i === 0 ? car.name : layers[i - 1].name + "棚板";

    const posts = L.posts.map((p) => {
      const hanging = !inRect(p.x, p.y, supportRect);
      const isFailed = !hanging && failedSet.has(i + "|" + p.id);
      if (hanging) issues.push({ level: "error", type: "hang", layer: i, post: p.id,
        message: `${L.name} 立柱 ${p.id}（${R(p.x)}, ${R(p.y)}）底端悬空，未落在${supportName}范围内，不承载` });
      else if (isFailed) issues.push({ level: "info", type: "failed", layer: i, post: p.id,
        message: `${L.name} 立柱 ${p.id} 被标记为失效（推演），按不承载参与整架重新分摊` });
      return { id: p.id, x: R(p.x), y: R(p.y), capacity: R(p.capacity), load: 0, hanging, failed: isFailed, overlap: false, overloaded: false, overlapWith: [], sources: [] };
    });
    const supported = posts.filter((p) => !p.hanging && !p.failed);
    const unsupported = supported.length === 0;
    let lost = 0;
    if (unsupported && total > 0) {
      lost = total; totalLost += lost;
      const hangN = posts.filter((p) => p.hanging).length;
      const failN = posts.filter((p) => p.failed).length;
      issues.push({ level: "error", type: "unsupported", layer: i, post: null,
        message: failN && L.posts.length
          ? `${L.name}全部 ${L.posts.length} 根立柱均不承载（悬空 ${hangN} 根、失效 ${failN} 根），棚板载荷 ${R(total)} kg 无支撑、无法下传`
          : `${L.name}没有有效立柱，棚板载荷 ${R(total)} kg 无支撑、无法下传` });
    }
    const c = supported.length;
    if (c > 0) {
      const share = total / c;
      supported.forEach((po) => {
        po.load = R(share);
        po.overloaded = po.capacity > 0 && share > po.capacity + 1e-9;
        const sources = [];
        if (bw > 0) sources.push({ label: "本层棚板自重", amount: R(bw / c) });
        if (ww > 0) sources.push({ label: "本层坯件重量", amount: R(ww / c) });
        points.forEach((pt) => pt.items.forEach((it) => sources.push({ label: it.chain, amount: R(it.amount / c) })));
        const agg = {};
        sources.forEach((s) => { agg[s.label] = (agg[s.label] || 0) + s.amount; });
        po.sources = Object.entries(agg).map(([label, amount]) => ({ label, amount: R(amount) }));
      });
    }
    // 重叠分组（并查集，距离 <= TOL_MM，与 app/calc.py 对齐）
    const Np = supported.length;
    const parent = Array.from({ length: Np }, (_, k) => k);
    const find = (a) => { while (parent[a] !== a) { parent[a] = parent[parent[a]]; a = parent[a]; } return a; };
    for (let a = 0; a < Np; a++) {
      for (let bb2 = a + 1; bb2 < Np; bb2++) {
        if (Math.hypot(supported[a].x - supported[bb2].x, supported[a].y - supported[bb2].y) <= TOL_MM) {
          parent[find(bb2)] = find(a);
        }
      }
    }
    const groupsMap = {};
    for (let a = 0; a < Np; a++) (groupsMap[find(a)] = groupsMap[find(a)] || []).push(a);
    const groups = Object.values(groupsMap).filter((g) => g.length >= 2);
    groups.forEach((group) => {
      const ids = group.map((g) => supported[g].id);
      group.forEach((g) => {
        supported[g].overlap = true;
        supported[g].overlapWith = ids.filter((x) => x !== supported[g].id);
      });
      issues.push({ level: "warn", type: "overlap", layer: i, post: ids[0],
        message: `${L.name} 立柱 ${ids.join("、")} 底端落在同一坐标，对${supportName}的合并点载荷 ${R(group.reduce((s, g) => s + supported[g].load, 0))} kg` });
    });
    supported.forEach((po) => {
      if (po.overloaded) issues.push({ level: "error", type: "overload", layer: i, post: po.id,
        message: `${L.name} 立柱 ${po.id} 超载：载荷 ${po.load} kg > 容量 ${po.capacity} kg` });
    });
    // 向下传点载荷；重叠簇合并为一个点
    if (i > 0 && !unsupported) {
      const clustered = new Set();
      groups.forEach((g) => g.forEach((x) => clustered.add(x)));
      const pushPoint = (members) => {
        const rep = supported[members[0]];
        const items = [];
        members.forEach((g) => {
          const q = supported[g];
          const own = (bw + ww) / c;
          if (own > 0) items.push({ chain: `${L.name}·${q.id}`, amount: own });
          points.forEach((pt) => pt.items.forEach((it) => items.push({ chain: `${it.chain} → ${L.name}·${q.id}`, amount: it.amount / c })));
        });
        const agg = {};
        items.forEach((it) => { agg[it.chain] = (agg[it.chain] || 0) + it.amount; });
        incoming[i - 1].push({ x: rep.x, y: rep.y, items: Object.entries(agg).map(([chain, amount]) => ({ chain, amount: R(amount) })) });
      };
      groups.forEach((g) => pushPoint(g));
      supported.forEach((po, idx) => { if (!clustered.has(idx)) pushPoint([idx]); });
    }
    outLayers.push({
      index: i, id: L.id, name: L.name, height: R(L.height),
      board: { x: R(b.x), y: R(b.y), w: R(b.w), d: R(b.d), weight: R(b.weight) },
      wareWeight: R(ww), supportKind: i === 0 ? "car" : "board", supportName,
      supportRect: { ...supportRect, x: R(supportRect.x), y: R(supportRect.y), w: R(supportRect.w), d: R(supportRect.d) },
      loadOwn: R(bw + ww), loadIncoming: R(loadIn), loadTotal: R(total),
      loadCarried: R(posts.reduce((s, p) => s + p.load, 0)), lostLoad: R(lost),
      supportedCount: c,
      hangingCount: posts.filter((p) => p.hanging).length,
      failedCount: posts.filter((p) => p.failed).length,
      posts,
    });
  }
  outLayers.reverse();
  const order = { overload: 0, hang: 1, unsupported: 2, failed: 3, overlap: 4, invalid: 5 };
  issues.sort((a, z) =>
    (order[a.type] ?? 9) - (order[z.type] ?? 9)
    || a.layer - z.layer
    || String(a.post || "").localeCompare(String(z.post || "")));
  return {
    car, layers: outLayers, issues,
    totals: {
      ownWeight: R(totalOwn),
      carriedAtBottom: outLayers.length ? outLayers[0].loadCarried : 0,
      lostLoad: R(totalLost),
      postCount: outLayers.reduce((s, L) => s + L.posts.length, 0),
      failedCount: outLayers.reduce((s, L) => s + (L.failedCount || 0), 0),
      layerCount: n,
    },
    toleranceMm: TOL_MM,
  };
}

/* 失效推演离线兜底：基准 vs 推演并排对比（与 app/calc.py 的 simulate 对齐） */
function simulateLocal(raw, failed) {
  const baseline = calculateLocal(raw);
  const simulation = calculateLocal(raw, failed);
  const applied = [];
  simulation.layers.forEach((L) => L.posts.forEach((p) => {
    if (p.failed) applied.push({ layer: L.index, layerName: L.name, post: p.id });
  }));
  return { baseline, simulation, failed: applied, diff: diffResults(baseline, simulation) };
}

function diffResults(base, sim) {
  const key = (e) => `${e.type}|${e.layer}|${e.post}`;
  const baseKeys = new Set(base.issues.map(key));
  const newIssues = sim.issues.filter(
    (e) => (e.type === "overload" || e.type === "unsupported") && !baseKeys.has(key(e))
  );
  const basePosts = {};
  base.layers.forEach((L) => L.posts.forEach((p) => { basePosts[L.index + "|" + p.id] = p; }));
  const posts = [];
  sim.layers.forEach((L) => L.posts.forEach((p) => {
    const b = basePosts[L.index + "|" + p.id] || { load: 0 };
    posts.push({
      layer: L.index, layerName: L.name, post: p.id,
      base: b.load, sim: p.load, delta: r2(p.load - b.load),
      failed: !!p.failed, hanging: !!p.hanging, overloaded: !!p.overloaded,
    });
  }));
  const pair = (k) => ({ base: base.totals[k], sim: sim.totals[k], delta: r2(sim.totals[k] - base.totals[k]) });
  return {
    carriedAtBottom: pair("carriedAtBottom"),
    lostLoad: pair("lostLoad"),
    posts,
    newIssues,
  };
}

/* ======================================================================
 * 加柱建议离线兜底：与 app/calc.py 的 suggest_posts 对齐（只算不存）
 * ==================================================================== */
function suggestLocal(rawState, layer, spacing, capacity) {
  const norm = (() => {
    // calculateLocal 内部已做宽容规范化；这里显式构造一份可安全深改的规范化输入
    const car = {
      name: (rawState.car && rawState.car.name) || "窑车台面",
      w: Math.max(0, num(rawState.car && rawState.car.w)),
      d: Math.max(0, num(rawState.car && rawState.car.d)),
    };
    const layers = (rawState.layers || []).map((l0, idx) => {
      const b0 = l0.board || {};
      return {
        id: l0.id || "L" + idx,
        name: l0.name || "第" + (idx + 1) + "层",
        height: Math.max(0, num(l0.height)),
        board: { x: num(b0.x), y: num(b0.y), w: Math.max(0, num(b0.w)), d: Math.max(0, num(b0.d)), weight: Math.max(0, num(b0.weight)) },
        wareWeight: Math.max(0, num(l0.wareWeight)),
        posts: (l0.posts || []).map((p0, j) => ({ id: String(p0.id || "P" + (j + 1)), x: num(p0.x), y: num(p0.y), capacity: num(p0.capacity) })),
      };
    });
    return { car, layers };
  })();

  const err = (msg) => { throw new Error(msg); };
  const li = Math.trunc(+layer);
  if (!Number.isInteger(li) || li < 0 || li >= norm.layers.length) {
    err(`目标层 ${layer} 不存在：当前共 ${norm.layers.length} 层`);
  }
  const gap = parseFloat(spacing);
  if (!Number.isFinite(gap) || gap <= 0) err("候选网格间距必须大于 0");
  const cap = parseFloat(capacity);
  if (!Number.isFinite(cap) || cap < 0) err("新柱容量不能为负；0 表示不限容量");

  const supportRect = li === 0
    ? { x: 0, y: 0, w: norm.car.w, d: norm.car.d }
    : { x: norm.layers[li - 1].board.x, y: norm.layers[li - 1].board.y, w: norm.layers[li - 1].board.w, d: norm.layers[li - 1].board.d };

  const baseline = calculateLocal(norm);
  const layerName = norm.layers[li].name;
  const noPlans = (gridCount, feasibleCount, reason) => ({
    ok: true, layer: li, layerName, spacing: r2(gap), capacity: r2(cap),
    gridCount, feasibleCount, plans: [], baseline, reason, toleranceMm: TOL_MM,
  });

  if (supportRect.w <= 0 || supportRect.d <= 0) {
    return noPlans(0, 0, `${layerName}的承托面宽或深为 0（${r2(supportRect.w)}×${r2(supportRect.d)} mm），没有可落柱的位置，请先修正承托面尺寸`);
  }
  const inRect = (x, y, rc) => x >= rc.x - 1e-9 && x <= rc.x + rc.w + 1e-9 && y >= rc.y - 1e-9 && y <= rc.y + rc.d + 1e-9;
  // 与后端一致：用 normalize 后的未截断坐标判定现有有效柱距离
  const existing = norm.layers[li].posts
    .filter((p) => inRect(p.x, p.y, supportRect))
    .map((p) => [p.x, p.y]);
  const x0 = supportRect.x, y0 = supportRect.y;
  const x1 = x0 + supportRect.w, y1 = y0 + supportRect.d;
  const nx = Math.floor((x1 - x0) / gap) + 1;
  const ny = Math.floor((y1 - y0) / gap) + 1;
  const gridCount = nx * ny;
  if (gridCount > 4096) {
    return noPlans(gridCount, 0, `候选网格过密：间距 ${r2(gap)} mm 在承托面（${r2(supportRect.w)}×${r2(supportRect.d)} mm）上产生 ${gridCount} 个候选点，超过试算上限 4096 个，请加大候选网格间距后重试`);
  }
  const used = new Set(norm.layers[li].posts.map((p) => p.id));
  let newId = null;
  for (let kk = 1; !newId; kk++) { if (!used.has("P" + kk)) newId = "P" + kk; }

  const feasible = [];
  for (let ix = 0; ix < nx; ix++) {
    const x = x0 + ix * gap;
    if (x > x1 + 1e-9) continue;
    for (let iy = 0; iy < ny; iy++) {
      const y = y0 + iy * gap;
      if (y > y1 + 1e-9) continue;
      let close = false;
      for (const [ex, ey] of existing) {
        if (Math.hypot(x - ex, y - ey) <= TOL_MM) { close = true; break; }
      }
      if (!close) feasible.push([x, y]);
    }
  }
  if (!feasible.length) {
    return noPlans(gridCount, 0, `${layerName}承托面内的 ${gridCount} 个网格候选点，全部与现有有效立柱落点距离 ≤ ${TOL_MM} mm（重叠阈值），没有可新增立柱的位置；可减小现有立柱密度后再试`);
  }

  const countOver = (res) => res.layers.reduce((s, L) => s + L.posts.filter((p) => p.overloaded).length, 0);
  const maxUtil = (res) => {
    let best = null;
    res.layers.forEach((L) => L.posts.forEach((p) => {
      if (p.capacity > 0) { const u = p.load / p.capacity; if (best === null || u > best) best = u; }
    }));
    return best;
  };
  const issueKey = (e) => `${e.type}|${e.layer}|${e.post}`;
  const issueChanges = (base, trial) => {
    const bm = new Map(base.issues.map((e) => [issueKey(e), e]));
    const tm = new Map(trial.issues.map((e) => [issueKey(e), e]));
    const order = { overload: 0, hang: 1, unsupported: 2, failed: 3, overlap: 4, invalid: 5 };
    const sortI = (arr) => arr.slice().sort((a, z) =>
      (order[a.type] ?? 9) - (order[z.type] ?? 9) || a.layer - z.layer || String(a.post).localeCompare(String(z.post)));
    const added = [], removed = [];
    tm.forEach((v, k) => { if (!bm.has(k)) added.push(v); });
    bm.forEach((v, k) => { if (!tm.has(k)) removed.push(v); });
    return { added: sortI(added), removed: sortI(removed) };
  };

  const baseLost = baseline.totals.lostLoad;
  const baseOver = countOver(baseline);
  const baseUtil = maxUtil(baseline);
  const scored = [];
  for (const [x, y] of feasible) {
    const trialState = JSON.parse(JSON.stringify(norm));
    trialState.layers[li].posts.push({ id: newId, x, y, capacity: cap });
    const trial = calculateLocal(trialState);
    const lost = trial.totals.lostLoad;
    const over = countOver(trial);
    const util = maxUtil(trial);
    scored.push({ lost, over, util: util === null ? Infinity : util, x, y, trial });
  }
  scored.sort((a, z) => a.lost - z.lost || a.over - z.over || a.util - z.util || a.x - z.x || a.y - z.y);

  const plans = scored.slice(0, 5).map((s, idx) => {
    const t = s.trial;
    const np = t.layers[li].posts.find((p) => p.id === newId);
    const rows = baseline.layers.map((bL, k) => {
      const tL = t.layers[k];
      const bOv = bL.posts.filter((p) => p.overloaded).length;
      const tOv = tL.posts.filter((p) => p.overloaded).length;
      return {
        layer: tL.index, name: tL.name,
        loadTotal: { base: bL.loadTotal, trial: tL.loadTotal },
        lostLoad: { base: bL.lostLoad, trial: tL.lostLoad },
        loadCarried: { base: bL.loadCarried, trial: tL.loadCarried },
        overloadCount: { base: bOv, trial: tOv },
      };
    });
    return {
      rank: idx + 1,
      post: { id: newId, x: r2(s.x), y: r2(s.y), capacity: r2(cap), load: np.load, overloaded: !!np.overloaded },
      metrics: {
        lostLoad: r2(s.lost),
        overloadCount: s.over,
        maxUtilization: s.util === Infinity ? null : Math.round(s.util * 10000) / 10000,
        carriedAtBottom: t.totals.carriedAtBottom,
      },
      layers: rows,
      issues: issueChanges(baseline, t),
      result: t,
    };
  });

  return {
    ok: true, layer: li, layerName, spacing: r2(gap), capacity: r2(cap),
    gridCount, feasibleCount: feasible.length,
    baselineMetrics: {
      lostLoad: baseLost, overloadCount: baseOver,
      maxUtilization: baseUtil === null ? null : Math.round(baseUtil * 10000) / 10000,
      carriedAtBottom: baseline.totals.carriedAtBottom,
    },
    plans, reason: null, toleranceMm: TOL_MM,
  };
}

/* ======================================================================
 * 减载配额试算离线兜底：与 app/calc.py 的 load_shed 对齐（只算不存）
 * 几何不变时各柱载荷关于层坯件重量为线性：
 *   T_i = t0_i + Σ_{k=i..segTop[i]}(w_k - r_k)，约束为嵌套后缀减量和。
 * ==================================================================== */
function loadShedLocal(rawState) {
  // 规范化一份可安全深改的架体（与 calculateLocal 同样的宽容规则）
  const norm = (() => {
    const car = {
      name: (rawState.car && rawState.car.name) || "窑车台面",
      w: Math.max(0, num(rawState.car && rawState.car.w)),
      d: Math.max(0, num(rawState.car && rawState.car.d)),
    };
    const layers = (rawState.layers || []).map((l0, idx) => {
      const b0 = l0.board || {};
      return {
        id: l0.id || "L" + idx,
        name: l0.name || "第" + (idx + 1) + "层",
        height: Math.max(0, num(l0.height)),
        board: { x: num(b0.x), y: num(b0.y), w: Math.max(0, num(b0.w)), d: Math.max(0, num(b0.d)), weight: Math.max(0, num(b0.weight)) },
        wareWeight: Math.max(0, num(l0.wareWeight)),
        posts: (l0.posts || []).map((p0, j) => ({ id: String(p0.id || "P" + (j + 1)), x: num(p0.x), y: num(p0.y), capacity: num(p0.capacity) })),
      };
    });
    return { car, layers };
  })();
  const layers = norm.layers;
  const n = layers.length;
  const baseline = calculateLocal(norm);
  const toUnits = (v) => Math.round(Math.max(0, v) * 100 + 1e-9);

  const supportRect = (i) => i === 0
    ? { x: 0, y: 0, w: norm.car.w, d: norm.car.d }
    : { x: layers[i - 1].board.x, y: layers[i - 1].board.y, w: layers[i - 1].board.w, d: layers[i - 1].board.d };
  const inRect = (x, y, rc) => x >= rc.x && x <= rc.x + rc.w && y >= rc.y && y <= rc.y + rc.d;

  // 与坯件无关的整架常量（保留精确浮点容量）：有效柱数 c、最小正容量 capmin、清零总载荷 t0
  const c = new Array(n).fill(0);
  const capmin = new Array(n).fill(null);
  for (let i = 0; i < n; i++) {
    const rc = supportRect(i);
    let cm = null;
    layers[i].posts.forEach((p) => {
      if (inRect(p.x, p.y, rc)) {
        c[i]++;
        if (p.capacity > 0 && (cm === null || p.capacity < cm)) cm = p.capacity;
      }
    });
    capmin[i] = cm;
  }
  const t0 = new Array(n).fill(0);
  for (let i = n - 1; i >= 0; i--) {
    const total = layers[i].board.weight + t0[i];
    if (i > 0 && c[i] > 0) t0[i - 1] += total; // 有效柱均摊且全部下传；c=0 层载荷丢失
    t0[i] = total;
  }
  // 连通层段顶（载荷自上而下，c=0 层截断）
  const segTop = new Array(n).fill(0);
  let top = n - 1;
  for (let i = n - 1; i >= 0; i--) {
    if (c[i] === 0) top = i - 1;
    segTop[i] = top;
  }
  const w = layers.map((L) => Math.max(0, L.wareWeight));
  const wu = w.map(toUnits); // 各层坯件重量（0.01 kg 整数单位）

  // 连续意义上每个有限容量层所需后缀减量（kg）
  const d = new Array(n).fill(0);
  for (let i = 0; i < n; i++) {
    if (c[i] > 0 && capmin[i] !== null) {
      let runWare = 0;
      for (let k = i; k <= segTop[i]; k++) runWare += w[k];
      const need = t0[i] + runWare - c[i] * capmin[i];
      d[i] = need > 1e-9 ? need : 0;
    }
  }

  const withWares = (arr) => {
    const trialState = JSON.parse(JSON.stringify(norm));
    trialState.layers.forEach((L, i) => { L.wareWeight = arr[i]; });
    return calculateLocal(trialState);
  };
  const overloadPosts = (res) => {
    const out = [];
    res.layers.forEach((L) => L.posts.forEach((p) => {
      if (p.overloaded) out.push({ layer: L.index, layerName: L.name, post: p.id, load: p.load, capacity: p.capacity });
    }));
    return out;
  };
  const countOver = (res) => res.layers.reduce((s, L) => s + L.posts.filter((p) => p.overloaded).length, 0);

  // 0.01 kg 整数网格上的嵌套后缀约束贪心：先减本层、不足由近及远向上层回补
  const greedyUnits = (dU) => {
    const alloc = new Array(n).fill(0);
    for (let i = n - 1; i >= 0; i--) {
      if (c[i] === 0 || dU[i] <= 0) continue;
      let rUpper = 0;
      for (let k = i + 1; k <= segTop[i]; k++) rUpper += alloc[k];
      if (dU[i] <= rUpper) continue;
      let need = dU[i] - rUpper;
      let take = Math.min(need, wu[i] - alloc[i]);
      alloc[i] += take; need -= take;
      for (let j = i + 1; need > 0 && j <= segTop[i]; j++) {
        take = Math.min(need, wu[j] - alloc[j]);
        alloc[j] += take; need -= take;
      }
    }
    return alloc;
  };

  const buildResult = (feasible, suggested, reductions, trial, remaining, reason) => {
    const rows = layers.map((L, i) => {
      const bL = baseline.layers[i], tL = trial.layers[i];
      const reduction = reductions[i];
      return {
        index: i, id: L.id, name: L.name,
        wareOriginal: r2(w[i]), wareSuggested: r2(suggested[i]), reduction: r2(reduction),
        changed: reduction > 1e-9,
        supportedCount: bL.supportedCount,
        overloadBefore: bL.posts.filter((p) => p.overloaded).length,
        overloadAfter: tL.posts.filter((p) => p.overloaded).length,
        lostBefore: bL.lostLoad, lostAfter: tL.lostLoad,
      };
    });
    return {
      ok: true, feasible, totalReduction: r2(reductions.reduce((s, v) => s + v, 0)),
      layers: rows,
      metrics: {
        overloadCount: { before: countOver(baseline), after: countOver(trial) },
        lostLoad: { before: baseline.totals.lostLoad, after: trial.totals.lostLoad },
        carriedAtBottom: { before: baseline.totals.carriedAtBottom, after: trial.totals.carriedAtBottom },
      },
      baseline, trial, remainingOverloads: remaining, reason,
    };
  };

  if (n === 0) {
    return { ok: true, feasible: true, totalReduction: 0, layers: [], metrics: null, baseline, trial: baseline, remainingOverloads: [], reason: null };
  }

  // 全部坯件清空仍超载 → 无解，列出剩余超载柱（是否有解的唯一权威判据）
  const cleared = withWares(new Array(n).fill(0));
  const remainingCleared = overloadPosts(cleared);
  if (remainingCleared.length) {
    const totalW = w.reduce((s, v) => s + v, 0);
    return buildResult(false, new Array(n).fill(0), w.slice(), cleared, remainingCleared,
      `即使全部坯件清空（共减载 ${r2(totalW)} kg），仍有 ${remainingCleared.length} 根有限容量立柱超载；仅靠减载无法消除，请加柱、更换大容量柱或减轻棚板自重后再试`);
  }

  // 连续需求向上取整到 0.01 kg 整单位后贪心分配
  const dU = d.map((dv) => (dv > 0 ? Math.ceil(dv * 100 - 1e-9) : 0));
  let alloc = greedyUnits(dU);
  const buildTrial = (al) => withWares(al.map((ru, i) => (ru === 0 ? w[i] : (wu[i] - ru) / 100)));
  let trial = buildTrial(alloc);
  let remaining = overloadPosts(trial);
  if (remaining.length) {
    // 极小概率（非网格容量边界）：从超载层起向其连通上层补 1 个网格单位并重算
    for (let guard = 0; guard < wu.reduce((s, v) => s + v, 0) + 1 && remaining.length; guard++) {
      const overLayers = [...new Set(remaining.map((p) => p.layer))].sort((a, b) => a - b);
      let bumped = false;
      for (const li of overLayers) {
        for (let j = li; j <= segTop[li]; j++) {
          if (alloc[j] < wu[j]) { alloc[j]++; bumped = true; break; }
        }
        if (bumped) break;
      }
      if (!bumped) break;
      trial = buildTrial(alloc);
      remaining = overloadPosts(trial);
    }
  }
  if (remaining.length) {
    // 安全兜底（清空已无超载，理论不可达）
    return buildResult(false, new Array(n).fill(0), w.slice(), cleared, remainingCleared,
      `试算方案重新整架校验时仍有 ${remaining.length} 根立柱超载，已按全部坯件清空返回剩余超载柱，请加柱或更换大容量柱`);
  }
  const suggested = alloc.map((ru, i) => (ru === 0 ? w[i] : (wu[i] - ru) / 100));
  const reductions = w.map((wi, i) => wi - suggested[i]);
  return buildResult(true, suggested, reductions, trial, [], null);
}

/* ======================================================================
 * 层架抗倾覆试算离线兜底：与 app/calc.py 的 overturn_check 对齐（只读不存）
 *
 * 各层棚板+坯件为同一质点（棚板中心，z=累计高度，w=板重+坯重），按重量求整架重心；
 * 支撑多边形 = 底层非悬空立柱落点的凸包（Andrew 单调链）；投影平移 (ax·Gz, ay·Gz)
 * 后取到凸包边界的带符号最短距离（内正/边界0/外负），判定稳定/临界/倾覆。
 * ==================================================================== */
function overturnLocal(rawState, ax, ay) {
  if (!Number.isFinite(ax)) throw new Error("水平加速度 x 分量必须是数值（单位 g）");
  if (!Number.isFinite(ay)) throw new Error("水平加速度 y 分量必须是数值（单位 g）");
  // 规范化（与 calculateLocal 同样的宽容规则）
  const car = {
    name: (rawState.car && rawState.car.name) || "窑车台面",
    w: Math.max(0, num(rawState.car && rawState.car.w)),
    d: Math.max(0, num(rawState.car && rawState.car.d)),
  };
  const layers = (rawState.layers || []).map((l0, idx) => {
    const b0 = l0.board || {};
    return {
      id: l0.id || "L" + idx,
      name: l0.name || "第" + (idx + 1) + "层",
      height: Math.max(0, num(l0.height)),
      board: { x: num(b0.x), y: num(b0.y), w: Math.max(0, num(b0.w)), d: Math.max(0, num(b0.d)), weight: Math.max(0, num(b0.weight)) },
      wareWeight: Math.max(0, num(l0.wareWeight)),
      posts: (l0.posts || []).map((p0, j) => ({ id: String(p0.id || "P" + (j + 1)), x: num(p0.x), y: num(p0.y), capacity: num(p0.capacity) })),
    };
  });
  const n = layers.length;
  const supportRect = (i) => i === 0
    ? { x: 0, y: 0, w: car.w, d: car.d }
    : { x: layers[i - 1].board.x, y: layers[i - 1].board.y, w: layers[i - 1].board.w, d: layers[i - 1].board.d };
  const inRect = (x, y, rc) => x >= rc.x && x <= rc.x + rc.w && y >= rc.y && y <= rc.y + rc.d;
  const EPS = 1e-6;

  const layerRows = [];
  const unsupported = [];
  let bottomPoints = [];
  let cumZ = 0, totalWeight = 0, sumX = 0, sumY = 0, sumZ = 0;
  for (let i = 0; i < n; i++) {
    const L = layers[i], b = L.board;
    const rc = supportRect(i);
    const validPosts = L.posts.filter((p) => inRect(p.x, p.y, rc));
    if (!validPosts.length) {
      unsupported.push({ index: i, name: L.name, postCount: L.posts.length, validCount: 0 });
    }
    if (i === 0) bottomPoints = validPosts.map((p) => [p.x, p.y, p.id]);
    const w = b.weight + L.wareWeight;
    cumZ += L.height;
    const cx = b.x + b.w / 2, cy = b.y + b.d / 2;
    totalWeight += w;
    sumX += w * cx; sumY += w * cy; sumZ += w * cumZ;
    layerRows.push({
      index: i, name: L.name, height: r2(L.height), z: r2(cumZ),
      cx: r2(cx), cy: r2(cy), weight: r2(w), supportedCount: validPosts.length,
    });
  }

  const fail = (reason) => ({
    ok: true, evaluable: false, reason, ax: r2(ax), ay: r2(ay),
    totalWeight: r2(totalWeight), cg: null, shift: null, projection: null,
    polygon: null, supportPoints: null, supportPointCount: bottomPoints.length,
    margin: null, verdict: null, unsupportedLayers: unsupported, layers: layerRows,
  });

  if (n === 0) return fail("当前架体没有任何层，无法评估抗倾覆稳定性");
  if (unsupported.length) {
    const names = unsupported.map((u) =>
      `${u.name}（录入 ${u.postCount} 根立柱，有效承托 0 根）`).join("、");
    return fail(`以下 ${unsupported.length} 层无有效承托（立柱均悬空或未录入），整架传力不完整，无法评估抗倾覆：${names}`);
  }

  // Andrew 单调链凸包（剔除共线点；输入不去精度）
  const convexHull = (points) => {
    const pts = [...new Map(points.map(([x, y]) => [x + "|" + y, [x, y]])).values()];
    pts.sort((a, b) => a[0] - b[0] || a[1] - b[1]);
    if (pts.length <= 2) return pts;
    const cross = (o, a, b) => (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0]);
    const lower = [];
    pts.forEach((p) => {
      while (lower.length >= 2 && cross(lower[lower.length - 2], lower[lower.length - 1], p) <= 0) lower.pop();
      lower.push(p);
    });
    const upper = [];
    for (let i = pts.length - 1; i >= 0; i--) {
      const p = pts[i];
      while (upper.length >= 2 && cross(upper[upper.length - 2], upper[upper.length - 1], p) <= 0) upper.pop();
      upper.push(p);
    }
    lower.pop(); upper.pop();
    return lower.concat(upper);
  };
  const area2 = (hull) => {
    let s = 0;
    hull.forEach(([x0, y0], i) => {
      const [x1, y1] = hull[(i + 1) % hull.length];
      s += x0 * y1 - x1 * y0;
    });
    return s;
  };

  const hullPts = bottomPoints.map(([x, y]) => [x, y]);
  const uniqueN = new Set(hullPts.map(([x, y]) => x + "|" + y)).size;
  const hull = convexHull(hullPts);
  const hullArea = Math.abs(area2(hull)) / 2;
  if (hull.length < 3 || hullArea <= EPS) {
    return fail(`底层非悬空立柱落点 ${bottomPoints.length} 个（去重后 ${uniqueN} 个），不足以形成非零面积的支撑多边形（至少需要 3 个不共线落点），无法评估抗倾覆`);
  }
  if (totalWeight <= 0) {
    return fail("整架棚板自重与坯件重量合计为 0，无法按重量求整架重心，不能评估抗倾覆");
  }

  const gx = sumX / totalWeight, gy = sumY / totalWeight, gz = sumZ / totalWeight;
  const dx = ax * gz, dy = ay * gz;
  const px = gx + dx, py = gy + dy;

  // 带符号最短距离：逆时针凸包，点在任一边右侧（叉积<0）即在外
  let inside = true, best = Infinity;
  for (let i = 0; i < hull.length; i++) {
    const [ax0, ay0] = hull[i];
    const [bx0, by0] = hull[(i + 1) % hull.length];
    const ex = bx0 - ax0, ey = by0 - ay0;
    const cr = ex * (py - ay0) - ey * (px - ax0);
    const el = Math.hypot(ex, ey);
    if (cr < -EPS * Math.max(1, el)) inside = false;
    const den = ex * ex + ey * ey;
    let t = den > 0 ? ((px - ax0) * ex + (py - ay0) * ey) / den : 0;
    t = Math.max(0, Math.min(1, t));
    const dd = Math.hypot(px - (ax0 + t * ex), py - (ay0 + t * ey));
    if (dd < best) best = dd;
  }
  const margin = inside ? best : -best;
  const verdict = margin > EPS ? "stable" : margin < -EPS ? "overturn" : "critical";

  const spMap = new Map();
  bottomPoints.forEach(([x, y, pid]) => {
    const key = x + "|" + y;
    if (!spMap.has(key)) spMap.set(key, { x, y, posts: [] });
    spMap.get(key).posts.push(pid);
  });
  const supportPoints = [...spMap.values()]
    .sort((a, b) => a.x - b.x || a.y - b.y)
    .map((q) => ({ x: r2(q.x), y: r2(q.y), posts: q.posts }));

  return {
    ok: true, evaluable: true, reason: null, ax: r2(ax), ay: r2(ay),
    totalWeight: r2(totalWeight),
    cg: { x: r2(gx), y: r2(gy), z: r2(gz) },
    shift: { x: r2(dx), y: r2(dy) },
    projection: { x: r2(px), y: r2(py) },
    polygon: hull.map(([x, y]) => ({ x: r2(x), y: r2(y) })),
    supportPoints, supportPointCount: bottomPoints.length,
    margin: r2(margin), verdict, unsupportedLayers: [], layers: layerRows,
  };
}

/* ======================================================================
 * 逐层装货顺序规划离线兜底：与 app/calc.py 的 loading_sequence 对齐（只读不存）
 *
 * 保留全部棚板自重、坯件先全部置零；候选层（坯件>0，至多 12 个）整层一次加入。
 * 对每个已装子集按与 overturnLocal 相同的物理模型求带符号裕量，
 * 位掩码动态规划最大化全过程最小裕量；同分按层序号字典序（自下而上）。
 * ==================================================================== */
const MAX_LOAD_SEQ_LAYERS = 12;

function loadingSequenceLocal(rawState, ax, ay) {
  if (!Number.isFinite(ax)) throw new Error("水平加速度 x 分量必须是数值（单位 g）");
  if (!Number.isFinite(ay)) throw new Error("水平加速度 y 分量必须是数值（单位 g）");
  const EPS = 1e-6;
  // 规范化（与 calculateLocal 同样的宽容规则）
  const car = {
    name: (rawState.car && rawState.car.name) || "窑车台面",
    w: Math.max(0, num(rawState.car && rawState.car.w)),
    d: Math.max(0, num(rawState.car && rawState.car.d)),
  };
  const layers = (rawState.layers || []).map((l0, idx) => {
    const b0 = l0.board || {};
    return {
      id: l0.id || "L" + idx,
      name: l0.name || "第" + (idx + 1) + "层",
      height: Math.max(0, num(l0.height)),
      board: { x: num(b0.x), y: num(b0.y), w: Math.max(0, num(b0.w)), d: Math.max(0, num(b0.d)), weight: Math.max(0, num(b0.weight)) },
      wareWeight: Math.max(0, num(l0.wareWeight)),
      posts: (l0.posts || []).map((p0, j) => ({ id: String(p0.id || "P" + (j + 1)), x: num(p0.x), y: num(p0.y), capacity: num(p0.capacity) })),
    };
  });
  const n = layers.length;
  const supportRect = (i) => i === 0
    ? { x: 0, y: 0, w: car.w, d: car.d }
    : { x: layers[i - 1].board.x, y: layers[i - 1].board.y, w: layers[i - 1].board.w, d: layers[i - 1].board.d };
  const inRect = (x, y, rc) => x >= rc.x && x <= rc.x + rc.w && y >= rc.y && y <= rc.y + rc.d;

  const candidateIdx = layers.map((L, i) => (L.wareWeight > 0 ? i : -1)).filter((i) => i >= 0);
  const m = candidateIdx.length;
  const candidateRows = candidateIdx.map((i) => ({
    index: i, id: layers[i].id, name: layers[i].name, wareWeight: r2(layers[i].wareWeight),
  }));

  const unsupported = [];
  let bottomPoints = [];
  for (let i = 0; i < n; i++) {
    const rc = supportRect(i);
    const valid = layers[i].posts.filter((p) => inRect(p.x, p.y, rc));
    if (!valid.length) unsupported.push({ index: i, name: layers[i].name, postCount: layers[i].posts.length, validCount: 0 });
    if (i === 0) bottomPoints = valid.map((p) => [p.x, p.y, p.id]);
  }

  const fail = (reason) => ({
    ok: true, planable: false, reason, ax: r2(ax), ay: r2(ay),
    candidateCount: m, candidateLimit: MAX_LOAD_SEQ_LAYERS, candidates: candidateRows,
    unsupportedLayers: unsupported, polygon: null, order: null, steps: null,
    initial: null, minMargin: null, allStable: false,
  });

  if (n === 0) return fail("当前架体没有任何层，无法规划逐层装货顺序");
  if (unsupported.length) {
    const names = unsupported.map((u) => `${u.name}（录入 ${u.postCount} 根立柱，有效承托 0 根）`).join("、");
    return fail(`以下 ${unsupported.length} 层无有效承托（立柱均悬空或未录入），整架传力不完整，无法规划逐层装货顺序：${names}`);
  }

  // Andrew 单调链凸包（与 overturnLocal 一致）
  const convexHull = (points) => {
    const pts = [...new Map(points.map(([x, y]) => [x + "|" + y, [x, y]])).values()];
    pts.sort((a, b) => a[0] - b[0] || a[1] - b[1]);
    if (pts.length <= 2) return pts;
    const cross = (o, a, b) => (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0]);
    const lower = [];
    pts.forEach((p) => {
      while (lower.length >= 2 && cross(lower[lower.length - 2], lower[lower.length - 1], p) <= 0) lower.pop();
      lower.push(p);
    });
    const upper = [];
    for (let i = pts.length - 1; i >= 0; i--) {
      const p = pts[i];
      while (upper.length >= 2 && cross(upper[upper.length - 2], upper[upper.length - 1], p) <= 0) upper.pop();
      upper.push(p);
    }
    lower.pop(); upper.pop();
    return lower.concat(upper);
  };
  const area2 = (hull) => hull.reduce((s, [x0, y0], i) => {
    const [x1, y1] = hull[(i + 1) % hull.length];
    return s + x0 * y1 - x1 * y0;
  }, 0);

  const hull = convexHull(bottomPoints.map(([x, y]) => [x, y]));
  const uniqueN = new Set(bottomPoints.map(([x, y]) => x + "|" + y)).size;
  const hullArea = Math.abs(area2(hull)) / 2;
  if (hull.length < 3 || hullArea <= EPS) {
    return fail(`底层非悬空立柱落点 ${bottomPoints.length} 个（去重后 ${uniqueN} 个），不足以形成非零面积的支撑多边形（至少需要 3 个不共线落点），无法规划逐层装货顺序`);
  }

  const boardWeightTotal = layers.reduce((s, L) => s + L.board.weight, 0);
  if (boardWeightTotal <= 0) {
    return fail("初始棚板总重为 0：坯件全部置零后整架没有重量，无法求重心，不能规划逐层装货顺序（请先录入棚板自重）");
  }
  if (m > MAX_LOAD_SEQ_LAYERS) {
    return fail(`共有 ${m} 个坯件重量大于零的层，超过逐层装货顺序规划上限 ${MAX_LOAD_SEQ_LAYERS} 层，请缩减规划范围（将部分层坯件重量置零）后重试`);
  }
  if (m === 0) return fail("当前没有坯件重量大于零的层，所有层均已处于无坯件状态，无需规划装货顺序");

  // 各层质点常量
  const zAt = [], cxAt = [], cyAt = [], bwAt = [], wwAt = [];
  let cumZ = 0;
  layers.forEach((L) => {
    cumZ += L.height;
    zAt.push(cumZ);
    cxAt.push(L.board.x + L.board.w / 2);
    cyAt.push(L.board.y + L.board.d / 2);
    bwAt.push(L.board.weight);
    wwAt.push(L.wareWeight);
  });
  const baseW = boardWeightTotal;
  let baseSx = 0, baseSy = 0, baseSz = 0;
  for (let i = 0; i < n; i++) {
    baseSx += bwAt[i] * cxAt[i];
    baseSy += bwAt[i] * cyAt[i];
    baseSz += bwAt[i] * zAt[i];
  }
  const dW = candidateIdx.map((i) => wwAt[i]);
  const dSX = candidateIdx.map((i) => wwAt[i] * cxAt[i]);
  const dSY = candidateIdx.map((i) => wwAt[i] * cyAt[i]);
  const dSZ = candidateIdx.map((i) => wwAt[i] * zAt[i]);

  // 子集（已装候选层）带符号裕量等：按候选层序号升序累加，保证同子集结果逐位确定
  const subsetMargin = (mask) => {
    let tw = baseW, sx = baseSx, sy = baseSy, sz = baseSz, wareSum = 0;
    for (let k = 0; k < m; k++) {
      if (mask & (1 << k)) {
        tw += dW[k]; sx += dSX[k]; sy += dSY[k]; sz += dSZ[k];
        wareSum += dW[k];
      }
    }
    const gx = sx / tw, gy = sy / tw, gz = sz / tw;
    const px = gx + ax * gz, py = gy + ay * gz;
    let inside = true, best = Infinity;
    for (let i = 0; i < hull.length; i++) {
      const [ax0, ay0] = hull[i];
      const [bx0, by0] = hull[(i + 1) % hull.length];
      const ex = bx0 - ax0, ey = by0 - ay0;
      const cr = ex * (py - ay0) - ey * (px - ax0);
      const el = Math.hypot(ex, ey);
      if (cr < -EPS * Math.max(1, el)) inside = false;
      const den = ex * ex + ey * ey;
      let t = den > 0 ? ((px - ax0) * ex + (py - ay0) * ey) / den : 0;
      t = Math.max(0, Math.min(1, t));
      const dd = Math.hypot(px - (ax0 + t * ex), py - (ay0 + t * ey));
      if (dd < best) best = dd;
    }
    return { margin: inside ? best : -best, tw, gx, gy, gz, wareSum };
  };
  const verdictOf = (mv) => (mv > EPS ? "stable" : mv < -EPS ? "overturn" : "critical");

  // 位掩码动态规划：best[mask] = [全过程最小裕量, 层索引顺序元组]；同分字典序
  const initMv = subsetMargin(0).margin;
  const best = new Map();
  best.set(0, [initMv, []]);
  const lexLess = (a, b) => {
    for (let i = 0; i < Math.max(a.length, b.length); i++) {
      const x = a[i] === undefined ? -1 : a[i], y = b[i] === undefined ? -1 : b[i];
      if (x !== y) return x < y;
    }
    return false;
  };
  for (let mask = 1; mask < (1 << m); mask++) {
    let bestVal = -Infinity, bestOrder = null;
    let bits = mask;
    while (bits) {
      const lb = bits & -bits;
      const k = Math.log2(lb) | 0;
      const prev = mask ^ lb;
      const [prevVal, prevOrder] = best.get(prev);
      const val = Math.min(prevVal, subsetMargin(mask).margin);
      const order = prevOrder.concat([candidateIdx[k]]);
      if (val > bestVal || (val === bestVal && (bestOrder === null || lexLess(order, bestOrder)))) {
        bestVal = val; bestOrder = order;
      }
      bits ^= lb;
    }
    best.set(mask, [bestVal, bestOrder]);
  }
  const fullMask = (1 << m) - 1;
  const [minMarginVal, orderIdx] = best.get(fullMask);

  const stepRow = (stepNo, added, q) => {
    const v = verdictOf(q.margin);
    return {
      step: stepNo,
      addedIndex: added,
      addedId: added === null ? null : layers[added].id,
      addedName: added === null ? null : layers[added].name,
      addedWareWeight: added === null ? 0 : r2(wwAt[added]),
      cumulativeWareWeight: r2(q.wareSum),
      totalWeight: r2(q.tw),
      cg: { x: r2(q.gx), y: r2(q.gy), z: r2(q.gz) },
      projection: { x: r2(q.gx + ax * q.gz), y: r2(q.gy + ay * q.gz) },
      margin: r2(q.margin),
      verdict: v,
      stable: v === "stable",
    };
  };
  const q0 = subsetMargin(0);
  const steps = [stepRow(0, null, q0)];
  let maskNow = 0;
  orderIdx.forEach((li, j) => {
    const k = candidateIdx.indexOf(li);
    maskNow |= 1 << k;
    steps.push(stepRow(j + 1, li, subsetMargin(maskNow)));
  });
  const allStable = steps.every((s) => s.stable);
  let minStep = steps[0];
  steps.forEach((s) => { if (s.margin < minStep.margin || (s.margin === minStep.margin && s.step < minStep.step)) minStep = s; });

  const spMap = new Map();
  bottomPoints.forEach(([x, y, pid]) => {
    const key = x + "|" + y;
    if (!spMap.has(key)) spMap.set(key, { x, y, posts: [] });
    spMap.get(key).posts.push(pid);
  });
  const supportPoints = [...spMap.values()]
    .sort((a, b) => a.x - b.x || a.y - b.y)
    .map((q) => ({ x: r2(q.x), y: r2(q.y), posts: q.posts }));

  return {
    ok: true, planable: true, reason: null, ax: r2(ax), ay: r2(ay),
    candidateCount: m, candidateLimit: MAX_LOAD_SEQ_LAYERS, candidates: candidateRows,
    unsupportedLayers: [],
    polygon: hull.map(([x, y]) => ({ x: r2(x), y: r2(y) })),
    supportPoints, supportPointCount: bottomPoints.length,
    order: orderIdx.slice(), steps, initial: steps[0],
    minMargin: r2(minMarginVal), minMarginStep: minStep.step, allStable,
  };
}

/* ======================================================================
 * 连续窑门通行试算离线兜底：与 app/calc.py 的 door_passage 对齐（只读不存）
 *
 * 横向外廓只取台面与各层棚板 X 向跨度（z=累计高度），最高点 = Σ层高；
 * 每道门：高度不足直接阻断，否则求外廓各件平移后落在开口内的横移闭区间；
 * 按门序把上一可达区间扩张本段 maxShift 后与本门区间求交，首次空交集即阻断；
 * 全程可达时回溯「最靠近 0」的位置序列并给逐门左/右/顶部净空。
 * ==================================================================== */
function doorPassageLocal(rawState, rawDoors) {
  validateDoors(rawDoors || []);
  const doors = (rawDoors || []).map((d, i) => ({
    index: i,
    center: parseFloat(d.center), width: parseFloat(d.width),
    height: parseFloat(d.height), maxShift: parseFloat(d.maxShift),
  }));
  // 规范化（与 calculateLocal 同样的宽容规则）
  const car = {
    name: (rawState.car && rawState.car.name) || "窑车台面",
    w: Math.max(0, num(rawState.car && rawState.car.w)),
    d: Math.max(0, num(rawState.car && rawState.car.d)),
  };
  const layers = (rawState.layers || []).map((l0, idx) => {
    const b0 = l0.board || {};
    return {
      id: l0.id || "L" + idx,
      name: l0.name || "第" + (idx + 1) + "层",
      height: Math.max(0, num(l0.height)),
      board: { x: num(b0.x), y: num(b0.y), w: Math.max(0, num(b0.w)), d: Math.max(0, num(b0.d)), weight: Math.max(0, num(b0.weight)) },
      wareWeight: Math.max(0, num(l0.wareWeight)),
    };
  });
  const n = layers.length;
  const EPS = 1e-9;

  const slabs = [];
  if (car.w > 0) slabs.push({ kind: "car", name: car.name, z: 0, left: 0, right: car.w, width: car.w });
  let cumZ = 0;
  layers.forEach((L) => {
    cumZ += L.height;
    if (L.board.w > 0) {
      slabs.push({ kind: "board", name: L.name, z: cumZ, left: L.board.x, right: L.board.x + L.board.w, width: L.board.w });
    }
  });
  const envL = slabs.length ? Math.min(...slabs.map((s) => s.left)) : 0;
  const envR = slabs.length ? Math.max(...slabs.map((s) => s.right)) : 0;
  const profile = {
    totalHeight: r2(cumZ),
    envelope: { left: r2(envL), right: r2(envR), width: r2(envR - envL) },
    slabs: slabs.map((s) => ({ kind: s.kind, name: s.name, z: r2(s.z), left: r2(s.left), right: r2(s.right), width: r2(s.width) })),
  };
  const echo = (d) => ({
    index: d.index, center: r2(d.center), width: r2(d.width), height: r2(d.height),
    maxShift: r2(d.maxShift),
    opening: { left: r2(d.center - d.width / 2), right: r2(d.center + d.width / 2) },
  });
  const ivJson = (lo, hi) => ({
    lo: lo === -Infinity ? null : r2(lo),
    hi: hi === Infinity ? null : r2(hi),
  });

  if (n === 0) {
    return {
      ok: true, evaluable: false,
      reason: "当前架体没有任何层，无法确定横向外廓与累计层高，不能进行窑门通行试算",
      profile, doorCount: doors.length, passable: false,
      blockedAt: null, blockReason: null, doors: doors.map(echo), positions: null, note: null,
    };
  }

  const m = doors.length;
  const rows = [];
  const reachIvs = [];
  let prevLo = 0, prevHi = 0;
  let blockedAt = null, blockReason = null;

  doors.forEach((d) => {
    const openL = d.center - d.width / 2, openR = d.center + d.width / 2;
    const row = {
      ...echo(d), evaluated: true, heightOk: true, interval: null, intervalEmpty: false,
      reachExpanded: null, reachable: null, passable: false, blockReason: null,
      conflict: null, chosenShift: null, move: null, clearance: null,
    };
    rows.push(row);
    if (blockedAt !== null) { row.evaluated = false; return; }

    const exLo = prevLo - d.maxShift, exHi = prevHi + d.maxShift;
    row.reachExpanded = ivJson(exLo, exHi);

    if (d.height + EPS < cumZ) {
      blockedAt = d.index; blockReason = "height";
      row.heightOk = false; row.blockReason = "height";
      row.conflict = { doorHeight: r2(d.height), totalHeight: r2(cumZ), shortage: r2(cumZ - d.height) };
      reachIvs.push([Infinity, -Infinity]);
      return;
    }

    let lo = -Infinity, hi = Infinity;
    slabs.forEach((s) => {
      lo = Math.max(lo, openL - s.left);
      hi = Math.min(hi, openR - s.right);
    });
    row.interval = ivJson(lo, hi);
    if (lo > hi + EPS) {
      blockedAt = d.index; blockReason = "width";
      row.intervalEmpty = true; row.blockReason = "width";
      row.conflict = {
        doorWidth: r2(d.width), envelopeWidth: r2(envR - envL),
        shortage: r2(lo - hi), interval: ivJson(lo, hi),
      };
      reachIvs.push([Infinity, -Infinity]);
      return;
    }

    const rLo = Math.max(exLo, lo), rHi = Math.min(exHi, hi);
    if (rLo > rHi + EPS) {
      blockedAt = d.index; blockReason = "reach";
      row.blockReason = "reach";
      row.conflict = {
        side: lo > exHi ? "right" : "left",
        shortage: r2(rLo - rHi),
        reachExpanded: ivJson(exLo, exHi),
        doorInterval: ivJson(lo, hi),
      };
      reachIvs.push([Infinity, -Infinity]);
      return;
    }
    row.reachable = ivJson(rLo, rHi);
    row.passable = true;
    prevLo = rLo; prevHi = rHi;
    reachIvs.push([rLo, rHi]);
  });

  let positions = null;
  if (blockedAt === null && m > 0) {
    const clamp = (v, lo, hi) => Math.min(Math.max(v, lo), hi);
    const chosen = new Array(m).fill(0);
    let cur = clamp(0, reachIvs[m - 1][0], reachIvs[m - 1][1]);
    chosen[m - 1] = cur;
    for (let k = m - 2; k >= 0; k--) cur = clamp(cur, reachIvs[k][0], reachIvs[k][1]), chosen[k] = cur;

    positions = [];
    let prevShift = 0;
    rows.forEach((row, k) => {
      const shift = chosen[k], move = shift - prevShift;
      row.chosenShift = r2(shift);
      row.move = r2(move);
      row.clearance = {
        left: r2(Math.max(0, (envL + shift) - (doors[k].center - doors[k].width / 2))),
        right: r2(Math.max(0, (doors[k].center + doors[k].width / 2) - (envR + shift))),
        top: r2(Math.max(0, doors[k].height - cumZ)),
      };
      positions.push({ door: k, shift: r2(shift), move: r2(move), maxShift: r2(doors[k].maxShift), clearance: row.clearance });
      prevShift = shift;
    });
  }

  return {
    ok: true, evaluable: true, reason: null, profile, doorCount: m,
    passable: blockedAt === null, blockedAt, blockReason,
    doors: rows, positions,
    note: m === 0 ? "未录入窑门，按全程可达处理（没有需要通过的门）" : null,
  };
}

/* ======================================================================
 * 轮组承载试算离线兜底：与 app/calc.py 的 wheel_load_trial 对齐（只读不存）
 *
 * 点载荷 = 底层非悬空立柱（calculateLocal 结果）的落点与实载；
 * 单个点 P(x,y)：wL=(xR-x)/(xR-xL)、wR=(x-xL)/…、wF=(yR-y)/(yR-yF)、
 * wB=(y-yF)/…，四轮反力 P·wL·wF / P·wR·wF / P·wL·wB / P·wR·wB，
 * 系数恒和为 1：越出轮组矩形时允许外推、负反力不截零。
 * ==================================================================== */
function wheelLoadLocal(rawState, p) {
  const xLeft = p.xLeft, xRight = p.xRight, yFront = p.yFront, yRear = p.yRear;
  const ratings = { FL: p.ratedFL, FR: p.ratedFR, RL: p.ratedRL, RR: p.ratedRR };
  const calc = calculateLocal(rawState);
  const car = calc.car;

  const base = {
    xLeft: r2(xLeft), xRight: r2(xRight), yFront: r2(yFront), yRear: r2(yRear),
    trackX: r2(xRight - xLeft), wheelbaseY: r2(yRear - yFront),
    ratings: { FL: r2(ratings.FL), FR: r2(ratings.FR), RL: r2(ratings.RL), RR: r2(ratings.RR) },
    car: { name: car.name, w: car.w, d: car.d },
  };
  const fail = (reason) => ({
    ok: true, evaluable: false, reason, ...base,
    totalLoad: 0.0, pointLoads: [], pointLoadCount: 0, overhangCount: 0,
    wheels: null, checks: null, risks: [],
  });

  const bottom = calc.layers[0];
  if (!bottom) return fail("当前架体没有任何层，不存在底层立柱点载荷，无法进行轮组承载试算");

  // 合并组：与后端一致，仅完全相同坐标的落点合并为一个点载荷；
  // TOL_MM 内但不重合的点各自仍是独立点载荷（与点载荷明细一一对应）
  const groups = new Map();
  const pointLoads = [];
  bottom.posts.forEach((po) => {
    if (po.hanging) return;
    const x = po.x, y = po.y;
    const key = x + "|" + y;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(po);
    pointLoads.push({
      post: po.id, x: r2(x), y: r2(y), load: po.load,
      overlap: !!po.overlap, overlapWith: po.overlapWith || [],
      capacity: po.capacity,
      outsideWheelRect: !(xLeft <= x && x <= xRight && yFront <= y && y <= yRear),
    });
  });
  const totalLoad = pointLoads.reduce((s, q) => s + q.load, 0);
  const overhangN = pointLoads.filter((q) => q.outsideWheelRect).length;
  Object.assign(base, { totalLoad: r2(totalLoad), pointLoads, pointLoadCount: pointLoads.length, overhangCount: overhangN });
  if (totalLoad <= 1e-9) {
    return fail(`${bottom.name}非悬空立柱的实载合计为 0（无正载荷），台面没有需要四个车轮承担的载荷，无法进行轮组承载试算；请先在架体录入棚板自重或坯件重量`);
  }

  const dx = xRight - xLeft, dy = yRear - yFront;
  const react = { FL: 0, FR: 0, RL: 0, RR: 0 };
  let momentXLoads = 0, momentYLoads = 0;
  groups.forEach((members, key) => {
    const x = members[0].x, y = members[0].y;
    const P = members.reduce((s, m) => s + m.load, 0);
    const wl = (xRight - x) / dx, wr = (x - xLeft) / dx;
    const wf = (yRear - y) / dy, wb = (y - yFront) / dy;
    react.FL += P * wl * wf; react.FR += P * wr * wf;
    react.RL += P * wl * wb; react.RR += P * wr * wb;
    momentXLoads += P * (y - yFront);
    momentYLoads += P * (x - xLeft);
  });

  const wheelPos = { FL: [xLeft, yFront], FR: [xRight, yFront], RL: [xLeft, yRear], RR: [xRight, yRear] };
  const wheelNames = { FL: "左前轮", FR: "右前轮", RL: "左后轮", RR: "右后轮" };
  const wheels = [];
  const risks = [];
  (["FL", "FR", "RL", "RR"]).forEach((key) => {
    const [wx, wy] = wheelPos[key];
    const rv = react[key], rating = ratings[key];
    const overloaded = rv > rating + 1e-9;
    const negative = rv < -1e-9;
    const util = rv / rating;
    wheels.push({
      key, name: wheelNames[key], x: r2(wx), y: r2(wy),
      reaction: r2(rv), rating: r2(rating),
      utilization: Math.round(util * 10000) / 10000,
      overloaded, negative,
    });
    if (overloaded) risks.push({
      level: "error", type: "wheel-overload", wheel: key,
      message: `${wheelNames[key]} ${key}（${r2(wx)}, ${r2(wy)}）超额受力：反力 ${r2(rv)} kg > 额定载荷 ${r2(rating)} kg，利用率 ${Math.round(util * 1000) / 10}%`,
    });
    if (negative) risks.push({
      level: "error", type: "negative-reaction", wheel: key,
      message: `${wheelNames[key]} ${key}（${r2(wx)}, ${r2(wy)}）出现负反力 ${r2(rv)} kg：点载荷合力相对四轮矩形外偏，该轮需要向下拉住台面才能平衡（刚体杠杆外推结果，未截断为零），实际车轮只能承压，存在台面上翘/脱轮风险`,
    });
  });
  if (overhangN) risks.push({
    level: "warn", type: "extrapolation", wheel: null,
    message: `${bottom.name}有 ${overhangN} 个非悬空立柱落点越出四轮矩形（已按杠杆比例外推分配，负反力不截零）；相关车轮可能出现负反力，请结合上方逐轮结果核对`,
  });

  const momentXReact = (react.RL + react.RR) * dy;
  const momentYReact = (react.FR + react.RR) * dx;
  const balanced = (a, b) => Math.abs(a - b) <= 1e-7 * Math.max(1, Math.abs(a), Math.abs(b));
  const checks = {
    force: { reactionSum: r2(react.FL + react.FR + react.RL + react.RR), loadSum: r2(totalLoad), residual: r2(react.FL + react.FR + react.RL + react.RR - totalLoad), ok: balanced(react.FL + react.FR + react.RL + react.RR, totalLoad) },
    momentX: { axis: "过前轮 y=yFront 的 X 向轴", reactionMoment: r2(momentXReact), loadMoment: r2(momentXLoads), residual: r2(momentXReact - momentXLoads), ok: balanced(momentXReact, momentXLoads) },
    momentY: { axis: "过左轮 x=xLeft 的 Y 向轴", reactionMoment: r2(momentYReact), loadMoment: r2(momentYLoads), residual: r2(momentYReact - momentYLoads), ok: balanced(momentYReact, momentYLoads) },
  };
  checks.allOk = checks.force.ok && checks.momentX.ok && checks.momentY.ok;

  return { ok: true, evaluable: true, reason: null, ...base, wheels, checks, risks };
}
