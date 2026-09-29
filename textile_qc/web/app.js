/* 布卷疵点复检与退修系统 · 前端（无框架） */
"use strict";

const $ = (id) => document.getElementById(id);
const state = {
  user: "wang",
  meta: { users: {}, status_text: {}, defect_types: [] },
  entry: { bolt: null, version: null, segments: [], defects: [] },
  map: null,
};

const RESULT_TEXT = {
  cleared: "已消除/合格", confirmed: "确认（须退修）",
  escalated: "升级（须退修）",
};

// ---------------------------------------------------------------- 基础

async function api(path, opts = {}) {
  const opt = { method: opts.method || "GET", headers: {} };
  if (opts.body) {
    opt.headers["Content-Type"] = "application/json";
    opt.body = JSON.stringify({ ...opts.body, user: state.user });
  }
  const res = await fetch(path, opt);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.message || `HTTP ${res.status}`);
  return data;
}

function toast(msg, ok = true) {
  const box = $("flash");
  const div = document.createElement("div");
  div.className = ok ? "ok" : "err";
  div.textContent = msg;
  box.appendChild(div);
  setTimeout(() => div.remove(), 3600);
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"]/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

function badge(status) {
  const text = state.meta.status_text[status] || status;
  return `<span class="badge ${status}">${esc(text)}</span>`;
}

function gradeTag(g) {
  return g ? `<span class="grade ${g}">${g}</span>` : "";
}

function photoCell(d) {
  if (d.photo_path) {
    return `<img class="photo-thumb" loading="lazy" alt="疵点照片"
      src="/api/photos/${d.id}?user=${state.user}"
      onclick="openPhoto('${d.id}','${esc(d.type_name)}')">`;
  }
  return `<div class="photo-missing">照片缺失</div>`;
}

window.openPhoto = (id, name) => {
  $("photoTitle").textContent = `疵点照片 · ${name}`;
  $("photoImg").src = `/api/photos/${id}?user=${state.user}&t=${Date.now()}`;
  $("photoModal").classList.add("show");
};
window.closePhoto = () => $("photoModal").classList.remove("show");

// 横向长度条：整卷按米数铺成横条，检验段着色，疵点按位置落点
function boltBar(lengthM, segments, defects, opts = {}) {
  const pct = (m) => `${(m / lengthM) * 100}%`;
  const segColors = ["#d8cbae", "#d2c4a4", "#dccdb1", "#cfc0a0"];
  let segHtml = "";
  (segments || []).forEach((s, i) => {
    segHtml += `<div class="seg" style="left:${pct(s.start_m)};width:${pct(s.end_m - s.start_m)};
      background:${segColors[i % segColors.length]}"
      title="段${s.seg_index} [${s.start_m},${s.end_m})"></div>`;
  });
  let dots = "";
  (defects || []).forEach((d) => {
    const missing = d.type_code === "missing_photo";
    dots += `<div class="def-dot ${missing ? "missing" : d.grade}"
      style="left:${pct(d.position_m)}" title="${esc(d.type_name)} ${d.position_m}m / ${d.grade}级"></div>`;
  });
  const ticks = [0, lengthM / 4, lengthM / 2, (lengthM * 3) / 4, lengthM];
  let ruler = ticks.map((m, i) =>
    `<span style="left:${i === ticks.length - 1 ? "auto" : pct(m)};
      ${i === ticks.length - 1 ? "right:0;transform:none" : ""}">${Math.round(m * 10) / 10}m</span>`
  ).join("");
  return `<div style="min-width:220px; padding-bottom:20px">
    <div class="boltbar">${segHtml}${dots}</div>
    <div class="ruler">${ruler}</div>
  </div>`;
}

function fmtTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  return `${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")} ` +
    `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}

// ---------------------------------------------------------------- 页签

$("tabs").addEventListener("click", (e) => {
  const btn = e.target.closest("button");
  if (!btn) return;
  document.querySelectorAll("nav.tabs button").forEach((b) => b.classList.toggle("active", b === btn));
  document.querySelectorAll(".page").forEach((p) =>
    p.classList.toggle("active", p.id === `page-${btn.dataset.tab}`));
  refreshTab(btn.dataset.tab);
});

function refreshTab(tab) {
  if (tab === "ledger") loadLedger();
  if (tab === "entry") loadEntry();
  if (tab === "map") loadMap();
  if (tab === "reinspect") loadReinspections();
  if (tab === "rework") loadReworks();
  if (tab === "release") loadReleasePage();
}

// ---------------------------------------------------------------- 用户

function renderUserSelect() {
  const sel = $("userSel");
  sel.innerHTML = Object.entries(state.meta.users).map(([id, u]) =>
    `<option value="${id}">${u.name}（${id}）</option>`).join("");
  sel.value = state.user;
  renderRole();
}
function renderRole() {
  const u = state.meta.users[state.user];
  $("userRole").textContent = u ? u.roles.map((r) =>
    ({ inspector: "初检", reinspector: "复检", manager: "主任" }[r])).join("/") : "";
}
$("userSel").addEventListener("change", (e) => {
  state.user = e.target.value;
  refreshTab(document.querySelector("nav.tabs button.active").dataset.tab);
});

// ---------------------------------------------------------------- 台账

async function loadLedger() {
  const [bolts, reminders] = await Promise.all([
    api(`/api/bolts?status=${$("ledgerFilter").value || ""}`),
    api("/api/reminders"),
  ]);
  $("ledgerReminders").innerHTML = reminders.map((r) =>
    `<div class="reminder-bar">⏰ ${esc(r.bolt_no)}：${esc(r.message)}（${fmtTime(r.created_at)}）</div>`
  ).join("");
  $("ledgerBody").innerHTML = bolts.map((b) => `
    <tr>
      <td><strong>${esc(b.bolt_no)}</strong>${b.length_short ? '<div class="bad-text" style="font-size:11px">长度不足</div>' : ""}</td>
      <td>${esc(b.loom_code)}<div class="muted">${esc(b.loom_name)}</div></td>
      <td>${esc(b.color_name)}<div class="muted">${esc(b.color_code)} · ${esc(b.dye_lot)}</div></td>
      <td class="num ${b.length_short ? "bad-text" : ""}">${b.length_m}</td>
      <td class="num muted">${b.nominal_length_m}</td>
      <td class="num">${b.width_cm}</td>
      <td><div id="bar-${b.id}"><div class="boltbar" style="opacity:.4"></div></div></td>
      <td class="num">${b.defect_count}</td>
      <td>${badge(b.status)}</td>
      <td><button class="act mini" onclick="gotoMap('${b.id}')">地图</button></td>
    </tr>`).join("");
  // 异步补每卷的当前版本长度条（段 + 疵点落点）
  bolts.filter((b) => b.current_version_id).forEach(async (b) => {
    try {
      const v = await api(`/api/bolts/${b.id}/versions/current`);
      const cell = $(`bar-${b.id}`);
      if (cell) cell.outerHTML = boltBar(b.length_m, v.segments, v.defects);
    } catch { /* 旧版/无版本时留空 */ }
  });
}
window.gotoMap = (id) => {
  document.querySelector('nav.tabs button[data-tab="map"]').click();
  $("mapBolt").value = id;
  loadMap();
};
$("ledgerFilter").addEventListener("change", loadLedger);
$("ledgerRefresh").addEventListener("click", loadLedger);

// ---------------------------------------------------------------- 检验录入

async function loadEntry() {
  const bolts = await api("/api/bolts");
  const options = bolts
    .filter((b) => ["pending", "inspecting", "reinspect_after_rework"].includes(b.status))
    .map((b) => `<option value="${b.id}">${b.bolt_no} · ${state.meta.status_text[b.status]}</option>`)
    .join("");
  $("entryBolt").innerHTML = options || '<option value="">（无可录入布卷）</option>';
  if (state.entry.bolt && [...$("entryBolt").options].some((o) => o.value === state.entry.bolt)) {
    $("entryBolt").value = state.entry.bolt;
  }
  await refreshEntry();
}

$("entryBolt").addEventListener("change", async (e) => {
  state.entry.bolt = e.target.value || null;
  await refreshEntry();
});

async function refreshEntry() {
  const id = state.entry.bolt;
  if (!id) {
    $("fpLength").textContent = "—";
    $("fpWidth").textContent = "—";
    $("fpVersion").textContent = "";
    $("segBody").innerHTML = "";
    $("defBody").innerHTML = "";
    return;
  }
  const b = await api(`/api/bolts/${id}`);
  $("fpLength").textContent = b.length_m;
  $("fpWidth").textContent = b.width_cm;
  $("openVersionBtn").disabled = b.status !== "pending";
  $("completeVersionBtn").disabled =
    !["inspecting", "reinspect_after_rework"].includes(b.status);

  $("defType").innerHTML = state.meta.defect_types.map((t) =>
    `<option value="${t.code}">${t.name}（默认${t.default_grade}级）</option>`).join("");

  if (b.current_version_id) {
    const v = await api(`/api/bolts/${id}/versions/current`);
    state.entry.version = v.version;
    state.entry.segments = v.segments;
    state.entry.defects = v.defects;
    const tag = v.version.kind === "post_rework"
      ? `v${v.version.version_no} 退修后新版本（录入人 ${v.version.inspector}）`
      : `v${v.version.version_no} 初检（初检人 ${v.version.inspector}）`;
    $("fpVersion").innerHTML = tag + `<br>状态：${v.version.status}`;
    $("completeVersionBtn").disabled = v.version.status !== "open";
  } else {
    state.entry.version = null;
    state.entry.segments = [];
    state.entry.defects = [];
    $("fpVersion").textContent = "尚未开检";
  }
  renderSegs();
  renderEntryDefects();
}

function renderSegs() {
  const segs = state.entry.segments;
  $("defSeg").innerHTML = segs.map((s) =>
    `<option value="${s.id}">段${s.seg_index} [${s.start_m}, ${s.end_m})</option>`).join("");
  $("segBody").innerHTML = segs.map((s) => {
    const cnt = state.entry.defects.filter((d) => d.segment_id === s.id).length;
    return `<tr><td>段${s.seg_index}</td><td class="num">${s.start_m}</td>
      <td class="num">${s.end_m}</td><td class="num">${(s.end_m - s.start_m).toFixed(1)}</td>
      <td class="num">${cnt}</td></tr>`;
  }).join("");
}

function renderEntryDefects() {
  const segMap = Object.fromEntries(state.entry.segments.map((s) => [s.id, s]));
  $("defBody").innerHTML = state.entry.defects.map((d) => {
    const s = segMap[d.segment_id];
    return `<tr><td>${esc(d.type_name)}</td><td>${gradeTag(d.grade)}</td>
      <td class="num">${d.position_m}</td>
      <td>${s ? `段${s.seg_index} [${s.start_m},${s.end_m})` : "—"}</td>
      <td>${photoCell(d)}</td><td>${esc(d.recorded_by)}</td></tr>`;
  }).join("");
}

$("openVersionBtn").addEventListener("click", async () => {
  try {
    await api("/api/inspection/open", { body: { bolt_no: state.entry.bolt } });
    toast("初检版本 v1 已开检");
    await refreshEntry();
  } catch (e) { toast(e.message, false); }
});

$("addSegBtn").addEventListener("click", async () => {
  try {
    await api("/api/segments", { body: {
      version_id: state.entry.version.id,
      start_m: parseFloat($("segStart").value),
      end_m: parseFloat($("segEnd").value),
    }});
    $("segStart").value = $("segEnd").value;
    await refreshEntry();
    toast("检验段已添加");
  } catch (e) { toast(e.message, false); }
});

$("addDefBtn").addEventListener("click", async () => {
  try {
    await api("/api/defects", { body: {
      segment_id: $("defSeg").value,
      type_code: $("defType").value,
      grade: $("defGrade").value || null,
      position_m: parseFloat($("defPos").value),
      require_photo: $("defPhoto").value === "1",
      photo: $("defPhoto").value === "1" ? DEMO_PHOTO : null,
    }});
    $("defPos").value = "";
    await refreshEntry();
    toast("疵点已登记（按米数定位）");
  } catch (e) { toast(e.message, false); }
});

$("completeVersionBtn").addEventListener("click", async () => {
  try {
    await api("/api/inspection/complete", { body: { version_id: state.entry.version.id } });
    toast("检验版本已封板，进入复检队列");
    await refreshEntry();
  } catch (e) { toast(e.message, false); }
});

// 1x1 红点 PNG 的 base64（服务端会落盘保存为疵点照片）
const DEMO_PHOTO = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGP4z8DwHwAFBQIAX8jx0gAAAABJRU5ErkJggg==";

// ---------------------------------------------------------------- 疵点地图

async function loadMap() {
  const bolts = await api("/api/bolts");
  const cur = $("mapBolt").value || bolts[0]?.id;
  $("mapBolt").innerHTML = bolts.map((b) =>
    `<option value="${b.id}">${b.bolt_no} · ${state.meta.status_text[b.status]}</option>`).join("");
  if (cur && [...$("mapBolt").options].some((o) => o.value === cur)) $("mapBolt").value = cur;
  const id = $("mapBolt").value;
  if (!id) { $("mapBody").innerHTML = ""; return; }
  const data = await api(`/api/bolts/${id}/map`);
  state.map = data;
  const b = data.bolt;
  let html = `<div class="card">
    <div class="card-row" style="justify-content:space-between">
      <div><strong>${esc(b.bolt_no)}</strong> · ${esc(b.loom_code)} · ${esc(b.color_name)}
        <span class="muted">（${esc(b.color_code)} / ${esc(b.dye_lot)}）</span></div>
      <div>${badge(b.status)}
        <span class="muted">长度 ${b.length_m}m / 标称 ${b.nominal_length_m}m / 幅宽 ${b.width_cm}cm</span>
      </div>
    </div></div>`;
  data.versions.forEach((vv, idx) => {
    const isCurrent = vv.version.id === b.current_version_id;
    html += `<div class="card map-version ${isCurrent ? "" : "ver-old"}">
      <div style="margin-bottom:14px">
        <span class="ver-tag ${isCurrent ? "new" : ""}">v${vv.version.version_no}
          ${vv.version.kind === "post_rework" ? "退修后" : "初检"}</span>
        ${vv.version.supersedes_id ? `<span class="muted">↳ 退修后新版本，supersedes 关联上一版本</span>` : ""}
        <span class="muted">初检/录入人：${esc(vv.version.inspector)} · 状态：${vv.version.status}</span>
      </div>`;
    vv.segments.forEach((s) => {
      html += `<div style="margin:14px 0 6px" class="muted">段${s.seg_index}
        区间 [${s.start_m}m, ${s.end_m}m) ${esc(s.note || "")}</div>`;
      html += boltBar(b.length_m, [s], s.defects);
      if (s.defects.length) {
        html += `<table style="margin-top:10px"><thead><tr><th>类型</th><th>等级</th>
          <th class="num">位置</th><th>照片</th><th>记录人</th></tr></thead><tbody>`;
        s.defects.forEach((d) => {
          html += `<tr><td>${esc(d.type_name)}</td><td>${gradeTag(d.grade)}</td>
            <td class="num">${d.position_m}m</td><td>${photoCell(d)}</td>
            <td>${esc(d.recorded_by)}</td></tr>`;
        });
        html += "</tbody></table>";
      }
    });
    if (!vv.segments.length) html += `<p class="muted">该版本尚无检验段</p>`;
    html += "</div>";
  });
  $("mapBody").innerHTML = html;
}
$("mapBolt").addEventListener("change", loadMap);

// ---------------------------------------------------------------- 复检队列

async function loadReinspections() {
  const [tasks, reminders] = await Promise.all([
    api(`/api/reinspections?status=${$("riFilter").value || ""}`),
    api("/api/reminders"),
  ]);
  $("riReminders").innerHTML = reminders.map((r) =>
    `<div class="reminder-bar">⏰ ${esc(r.bolt_no)}：${esc(r.message)}</div>`).join("");
  $("riBody").innerHTML = tasks.map((t) => {
    const overdue = t.is_overdue && t.status !== "overdue";
    let acts = "";
    if (t.status === "queued" || t.status === "overdue") {
      acts = `<button class="act mini" onclick="claimRi('${t.id}')">接收</button>`;
      if (t.status === "overdue")
        acts += ` <button class="act ghost mini" onclick="requeueRi('${t.id}')">主任重派</button>`;
    } else if (t.status === "accepted") {
      acts = `<button class="act mini" onclick="openRiModal('${t.id}')">完成复检</button>`;
    }
    return `<tr>
      <td><strong>${esc(t.bolt_no)}</strong></td>
      <td>v${t.version_no}</td>
      <td>段${t.seg_index} [${t.start_m},${t.end_m})</td>
      <td>${esc(t.initial_inspector)}</td>
      <td>${esc(t.assigned_to)}</td>
      <td class="${overdue ? "bad-text" : ""}">${fmtTime(t.due_at)}${overdue ? " 已超时" : ""}</td>
      <td>${badge(t.status)}</td>
      <td>${esc(t.claimed_by || "—")}</td>
      <td>${t.result ? esc(RESULT_TEXT[t.result] || t.result) : "—"}
        ${t.comment ? `<div class="muted">${esc(t.comment)}</div>` : ""}</td>
      <td>${acts}</td></tr>`;
  }).join("");
}
window.claimRi = async (id) => {
  try { await api(`/api/reinspections/${id}/claim`, { body: {} }); toast("已接收复检任务"); loadReinspections(); }
  catch (e) { toast(e.message, false); }
};
window.requeueRi = async (id) => {
  try { await api(`/api/reinspections/${id}/requeue`, { body: {} }); toast("已重新派发"); loadReinspections(); }
  catch (e) { toast(e.message, false); }
};
window.openRiModal = (id) => { $("riModalId").value = id; $("riModal").classList.add("show"); };
window.closeRiModal = () => $("riModal").classList.remove("show");
$("riSubmitBtn").addEventListener("click", async () => {
  try {
    await api(`/api/reinspections/${$("riModalId").value}/complete`, { body: {
      result: $("riResult").value, comment: $("riComment").value,
    }});
    closeRiModal();
    toast("复检结论已提交");
    loadReinspections();
  } catch (e) { toast(e.message, false); }
});
$("riFilter").addEventListener("change", loadReinspections);
$("riRefresh").addEventListener("click", loadReinspections);

// ---------------------------------------------------------------- 退修

async function loadReworks() {
  const [orders, bolts] = await Promise.all([api("/api/reworks"), api("/api/bolts")]);
  $("rwBody").innerHTML = orders.map((o) => `
    <tr><td>${esc(o.id)}</td><td><strong>${esc(o.bolt_no)}</strong></td>
      <td>v${o.version_no}</td><td>${esc(o.reason)}</td>
      <td>${fmtTime(o.created_at)}</td><td>${fmtTime(o.finished_at)}</td>
      <td>${o.status === "done" ? '<span class="ok-text">已完工（新版本已生成）</span>'
        : '<span class="bad-text">退修中</span>'}</td>
      <td>${o.status === "in_progress"
        ? `<button class="act mini" onclick="openRwModal('${o.id}')">登记完工</button>` : "—"}</td>
    </tr>`).join("");
  const eligible = bolts.filter((b) =>
    ["inspected", "reinspect_after_rework"].includes(b.status));
  $("rwBolt").innerHTML = eligible.map((b) =>
    `<option value="${b.id}">${b.bolt_no}</option>`).join("") ||
    '<option value="">（无可退修布卷）</option>';
  $("rwInspector").innerHTML = Object.entries(state.meta.users)
    .filter(([, u]) => u.roles.includes("inspector"))
    .map(([id, u]) => `<option value="${id}">${u.name}（${id}）</option>`).join("");
}
$("rwCreateBtn").addEventListener("click", async () => {
  try {
    await api("/api/reworks", { body: {
      bolt_no: $("rwBolt").value, reason: $("rwReason").value,
    }});
    toast("退修工单已开出，旧复检任务取消");
    loadReworks();
  } catch (e) { toast(e.message, false); }
});
window.openRwModal = (id) => { $("rwModalId").value = id; $("rwModal").classList.add("show"); };
window.closeRwModal = () => $("rwModal").classList.remove("show");
$("rwSubmitBtn").addEventListener("click", async () => {
  try {
    await api(`/api/reworks/${$("rwModalId").value}/finish`, { body: {
      inspector: $("rwInspector").value,
    }});
    closeRwModal();
    toast("退修完工，新版本已生成，请到检验录入页录入复检数据");
    loadReworks();
  } catch (e) { toast(e.message, false); }
});

// ---------------------------------------------------------------- 放行

async function loadReleasePage() {
  const bolts = await api("/api/bolts");
  const cur = $("relBolt").value || bolts.find((b) =>
    ["inspected", "reinspect_after_rework"].includes(b.status))?.id || bolts[0]?.id;
  $("relBolt").innerHTML = bolts.map((b) =>
    `<option value="${b.id}">${b.bolt_no} · ${state.meta.status_text[b.status]}</option>`).join("");
  if (cur) $("relBolt").value = cur;
  if (!$("relBolt").value) { $("gatesBox").innerHTML = ""; return; }
  const data = await api(`/api/bolts/${$("relBolt").value}/gates`);
  const b = data.bolt;
  let html = `<p style="margin:6px 0 12px"><strong>${esc(b.bolt_no)}</strong>
    长度 ${b.length_m}m / 标称 ${b.nominal_length_m}m ${badge(b.status)}</p>`;
  html += data.gates.map((g) => {
    const hard = g.key === "grade_c_info";
    return `<div class="gate ${g.passed ? "pass" : "fail"}">
      <span class="ico">${g.passed ? "✔" : "✘"}</span>
      <strong>${esc(g.label)}</strong>
      <span class="${g.passed ? "ok-text" : "bad-text"}">${esc(g.detail)}</span>
      ${hard ? '<span class="muted">（不阻断）</span>' : ""}
      ${g.key === "length_short" && g.needs_concession ? '<span class="muted">→ 经理特许可豁免</span>' : ""}
    </div>`;
  }).join("");
  $("gatesBox").innerHTML = html;

  const releases = await api("/api/releases");
  $("relBody").innerHTML = releases.map((r) => `
    <tr><td>${fmtTime(r.created_at)}</td><td><strong>${esc(r.bolt_no)}</strong></td>
      <td>${esc(r.version_id.slice(-6))}</td><td>${esc(r.released_by)}</td>
      <td>${r.decision === "release"
        ? '<span class="badge released">放行</span>'
        : '<span class="badge rejected">拒收</span>'}</td>
      <td>${esc(r.basis)}</td></tr>`).join("");
}
$("relBolt").addEventListener("change", loadReleasePage);
$("doReleaseBtn").addEventListener("click", async () => {
  try {
    await api("/api/release", { body: {
      bolt_no: $("relBolt").value,
      concession: $("concessionChk").checked,
    }});
    toast("已签发放行");
    $("concessionChk").checked = false;
    loadReleasePage();
  } catch (e) { toast(e.message, false); }
});
$("doRejectBtn").addEventListener("click", async () => {
  try {
    await api("/api/release", { body: {
      bolt_no: $("relBolt").value, decision: "reject",
    }});
    toast("已签发拒收");
    loadReleasePage();
  } catch (e) { toast(e.message, false); }
});

// ---------------------------------------------------------------- 启动

async function boot() {
  state.meta = await api("/api/meta");
  renderUserSelect();
  await loadLedger();
}
boot().catch((e) => toast(`初始化失败：${e.message}`, false));
