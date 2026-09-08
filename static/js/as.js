/* OWS A/S — 접수 → 회수 → 수리 → 반송. 자산번호와 연결해 이력이 자산에도 남는다. */
"use strict";

const AS_CHIP = {
  received: "chip-blue", collecting: "chip-blue", arrived: "chip-violet", repairing: "chip-violet",
  done: "chip-green", returned: "chip-slate", cancelled: "chip-red",
};

/* A/S 상세에서 손으로 보낼 수 있는 안내 문자 — 진행 순서대로 */
const AS_SMS_EVENTS = [
  ["received", "접수 완료"], ["collecting", "회수 예약"], ["collected", "입고 완료"],
  ["repairing", "수리 진행"], ["payment", "결제 안내"], ["done", "수리 완료"],
  ["shipping_today", "발송 예정"], ["returned", "발송 안내"],
];

/* 진행 보드의 칸 → 그 칸에서 보낼 안내(2026-09-07 대표 "개별 고객 탭에서 SMS 발송").
   각 칸의 [💬 …] 버튼 하나가 이 문구를 미리보기로 열어 준다 — 그 자리에서 고쳐 보낼 수 있다. */
const AS_STAGE_SMS = {
  intake_wait: ["received", "💬 접수 안내"],
  collecting: ["collecting", "💬 회수 안내"],      // 기사님께 전달해 달라는 내용
  arrived: ["collected", "💬 입고 안내"],
  repairing: ["repairing", "💬 수리 안내"],
  pay_wait: ["payment", "💬 결제 안내"],           // 계좌번호 포함
  ship_wait: ["shipping_today", "💬 발송 예정"],
  visit_wait: ["shipping_today", "💬 수령 안내"],
  returning: ["returned", "💬 발송 안내"],         // 송장번호 포함 — 번호가 붙어야 나간다
};

/* 문자 종류 코드를 사람 말로 — 재발송은 received#2 처럼 뒤에 회차가 붙는다.
   예전엔 목록에 'received#2' 같은 코드가 그대로 찍혀 무슨 문자인지 알 수 없었다. */
function smsEventLabel(ev) {
  const [code, nth] = String(ev || "").split("#");
  const hit = AS_SMS_EVENTS.find((e) => e[0] === code);
  const name = hit ? hit[1]
    : code === "test" ? "시험 발송"
    : code.startsWith("custom:")
      ? ((state.asSmsNames || {})[code] || "직접 만든 양식")   // 양식 탭을 열면 이름이 채워진다
      : code;
  return name + (nth ? ` (재발송 ${nth}회차)` : "");
}

/* SMS 바이트 수(비즈고 EUC-KR 기준 근사) — 한글 2바이트, 영문·숫자 1바이트 */
function smsBytes(s) {
  let n = 0;
  for (const ch of String(s || "")) n += ch.charCodeAt(0) > 127 ? 2 : 1;
  return n;
}

/* 발송 전 미리보기·수정(2026-08-31 대표 — "기본 값은 냅두되 일부 내용이 수정 가능") */
async function openAsSmsModal(t, code, label) {
  let p;
  try { p = await api(`/api/as-tickets/${t.id}/sms-preview?event=${encodeURIComponent(code)}`); }
  catch (err) { toast(err.message, true); return; }
  let host = $("#assms-panel");
  if (!host) {
    host = document.createElement("div");
    host.id = "assms-panel";
    document.body.appendChild(host);
  }
  const lenInfo = (s) => {
    const n = smsBytes(s);
    return `${n}바이트 · ${n > 90 ? "LMS(장문 — 요금 3배가량)" : "SMS(단문)"}`;
  };
  host.innerHTML = `
    <div class="card" style="max-width:480px;">
      <div class="inline-row"><h3 style="margin:0; flex:1;">💬 ${escapeHtml(label || p.label)}</h3>
        <button class="btn btn-sm" id="assms-close">✕</button></div>
      <p class="muted" style="font-size:12.5px; margin:6px 0;">받는 사람:
        <b>${escapeHtml(t.customer)}</b> ${escapeHtml(p.phone || "번호 없음")}
        ${p.alreadySent ? ` · <b style="color:var(--danger);">이미 ${p.alreadySent}회 보냄</b>` : ""}</p>
      <p class="muted" style="font-size:12px; margin:0 0 6px;">기본 문구가 채워져 있습니다 —
        필요한 부분만 고쳐 보내세요. {수리비} 같은 자리를 적으면 보낼 때 실제 값으로 바뀝니다.</p>
      <label class="muted" style="display:block; font-size:12px;">제목 <span style="font-weight:400;">(장문 LMS일 때만 쓰입니다 — 비우면 기본 제목)</span>
        <input type="text" id="assms-subject" value="${escapeHtml(p.subject || "")}" maxlength="40" style="width:100%; margin-top:2px;"></label>
      <textarea id="assms-text" rows="6" style="width:100%; margin-top:6px;">${escapeHtml(p.text)}</textarea>
      <div class="inline-row" style="margin-top:6px;">
        <span class="muted" style="font-size:12px;" id="assms-len">${lenInfo(p.text)}</span>
        <span style="flex:1"></span>
        <button class="btn btn-primary btn-sm" id="assms-send">보내기</button>
      </div>
    </div>`;
  // ★2층 팝업 — A/S 상세(1층 팝업)를 닫지 않고 그 위에 겹친다(2026-08-31 대표:
  //   매크로를 누르면 상세가 사라지고 문자 창만 남던 문제)
  openModalOver(host);
  $("#assms-close").addEventListener("click", () => closeModalOver());
  $("#assms-text").addEventListener("input", () => {
    const el = $("#assms-len");
    if (el) el.textContent = lenInfo($("#assms-text").value);
  });
  const send = async (extra) => {
    const btn = $("#assms-send");
    if (btn) btn.disabled = true;
    try {
      const body = Object.assign({ event: code, text: $("#assms-text").value,
                                   subject: $("#assms-subject").value }, extra || {});
      const r = await api(`/api/as-tickets/${t.id}/sms`, { method: "POST", body });
      // ★먼저 force 없이 — 이미 보낸 건·야간은 서버가 막고, 그때 한 번 더 물어본다
      if (!r.ok && /이미 보낸/.test(r.message || "")) {
        if (confirm(`이미 보낸 안내입니다.\n${t.customer}님에게 한 번 더 보낼까요?`)) {
          return send(Object.assign({}, extra, { force: true }));
        }
        return;
      }
      if (!r.ok && /야간/.test(r.message || "")) {
        if (confirm(`${r.message}\n\n지금은 고객이 자고 있을 수 있습니다.\n그래도 지금 보낼까요?`)) {
          return send(Object.assign({}, extra, { force: true, allowQuiet: true }));
        }
        return;
      }
      toast(r.message, !r.ok);
      if (r.ok) {
        closeModalOver();
        renderAsDetail();          // 아래층(상세)은 그대로 — 내용만 새로 그린다
      }
    } catch (err) { toast(err.message, true); }
    finally {
      const b2 = $("#assms-send");
      if (b2) b2.disabled = false;
    }
  };
  $("#assms-send").addEventListener("click", () => send());
}

/* 직접 만든 양식 고르기 — 없으면 [💬 문자 양식] 탭으로 안내 */
async function openAsSmsPicker(t) {
  let d;
  try { d = await api("/api/as-sms-templates"); }
  catch (err) { toast(err.message, true); return; }
  state.asSmsNames = Object.fromEntries(
    (d.custom || []).map((c) => ["custom:" + c.id, c.label]));
  const customs = d.custom || [];
  if (!customs.length) {
    toast("직접 만든 양식이 아직 없습니다 — A/S 화면의 [💬 문자 양식] 탭에서 만들 수 있습니다.", true);
    return;
  }
  let host = $("#assms-pick");
  if (!host) {
    host = document.createElement("div");
    host.id = "assms-pick";
    document.body.appendChild(host);
  }
  const trigLabel = (code) => {
    const hit = (d.triggers || []).find((x) => x.code === code);
    return hit ? hit.label : "";
  };
  host.innerHTML = `
    <div class="card" style="max-width:420px;">
      <div class="inline-row"><h3 style="margin:0; flex:1;">📑 어떤 안내를 보낼까요?</h3>
        <button class="btn btn-sm" id="assms-pick-close">✕</button></div>
      ${customs.map((c, i) => `<button class="btn" data-pick-sms="${i}"
          style="display:block; width:100%; text-align:left; margin-top:6px;">
          <b>${escapeHtml(c.label)}</b>
          <div class="muted" style="font-size:12px;">${escapeHtml(trigLabel(c.trigger))}${c.on ? "" : " · 자동 꺼짐"}</div>
        </button>`).join("")}
    </div>`;
  openModalOver(host);
  $("#assms-pick-close").addEventListener("click", () => closeModalOver());
  $$("button[data-pick-sms]", host).forEach((b) => b.addEventListener("click", () => {
    const c = customs[Number(b.dataset.pickSms)];
    closeModalOver();
    openAsSmsModal(t, "custom:" + c.id, c.label);
  }));
}

async function ensureAsMeta() {
  if (!state.asMeta) state.asMeta = await api("/api/as-meta");
  return state.asMeta;
}

function asLabel(list, code) {
  const f = (state.asMeta && state.asMeta[list] || []).find((x) => x.code === code);
  return f ? f.label : code;
}

/* ─────────── A/S 탭(2026-09-07 대표 개편) ───────────
   [🏠 대시보드] [🚦 진행 상황] [⚙ 설정]
   · 대시보드 = 옛 [📋 접수] 자리 — 숫자·지금 할 일·최근 움직임·늦은 건, 그리고 찾기/접수
   · 진행 상황 = 옛 [📊 현황] — 접수 → 회수 중 → 입고 완료 → 수리 중 → 결제 전 → 발송 전 →
     택배 출고 → 종료. 칸마다 '다음 할 일' 버튼 하나.
   옛 state.asTab 값(list/stats)은 새 이름으로 흡수한다 — 다른 화면이 넘겨주는 값 호환. */
function asTabKey() {
  const t = state.asTab;
  if (t === "settings" || t === "sms") return "settings";
  if (t === "board" || t === "stats") return "board";
  if (t === "intake" || t === "list") return "intake";     // 옛 [📋 접수] 값 흡수
  return "dash";
}

function asTabsHtml(active) {
  const on = (key) => (active === key ? "active" : "");
  return `<div class="tabs">
      <button data-atab="dash" class="${on("dash")}">🏠 대시보드</button>
      <button data-atab="intake" class="${on("intake")}">📝 접수</button>
      <button data-atab="board" class="${on("board")}">🚦 진행 상황</button>
      ${hasPerm("as.manage") ? `<button data-atab="settings" class="${on("settings")}">⚙ 설정</button>` : ""}
    </div>`;
}

function wireAsTabs(main) {
  $$("button[data-atab]", main).forEach((b) => b.addEventListener("click", () => {
    state.asTab = b.dataset.atab;
    renderAsView(main);
  }));
}

function renderAsView(main) {
  if (!hasPerm("as.view") && !hasPerm("as.manage")) {
    main.innerHTML = `<h1 class="page-title">A/S</h1><div class="card placeholder"><p>A/S 접근 권한이 없습니다.</p></div>`;
    return;
  }
  const key = asTabKey();
  if (key === "settings" && hasPerm("as.manage")) return renderAsSettingsTab(main);
  if (key === "board") return renderAsBoardTab(main);
  if (key === "intake") return renderAsIntakeTab(main);
  return renderAsDashTab(main);
}

/* ─────────── 📝 접수(2026-09-07 대표 "A/S 접수탭 신설") ───────────
   접수하는 사람이 쓰는 화면 하나 — 새로 접수하고, 아직 택배를 안 부른 건을 여기서 부르고,
   지난 건을 찾는다. 대시보드는 '보는' 화면이고 여기는 '넣는' 화면이다. */
function renderAsIntakeTab(main) {
  const canEdit = hasPerm("as.manage");
  main.innerHTML = `
    <h1 class="page-title">A/S</h1>
    <p class="page-desc">A/S를 <b>접수하고</b>, 택배로 받을 건은 여기서 <b>택배 접수(회수 예약)</b>를 겁니다.
      방문으로 받은 건은 접수하는 순간 <b>입고 완료</b>로 넘어갑니다.</p>
    ${asTabsHtml("intake")}
    <div class="card">
      <div class="inline-row" style="flex-wrap:wrap; align-items:center;">
        <h3 style="margin:0;">📮 택배 접수 대기</h3>
        <span class="muted" style="font-size:12.5px;">— 택배로 받기로 했는데 아직 기사를 안 부른 건입니다.</span>
        <span style="flex:1"></span>
        ${canEdit ? `<button class="btn btn-sm btn-primary" id="asi-new">＋ A/S 접수</button>` : ""}
      </div>
      <div id="as-form"></div>
      <div id="asi-wait"><p class="muted">불러오는 중…</p></div>
    </div>
    <div class="card">
      <div class="inline-row" style="flex-wrap:wrap; align-items:center;">
        <h3 style="margin:0;">🔎 찾기</h3>
        <span class="muted" style="font-size:12.5px;">— 접수번호·고객·연락처·관리번호·증상. 종료·취소 건도 찾습니다.</span>
      </div>
      <div id="as-tags" style="margin-top:8px;"></div>
      <div class="inline-row">
        <select id="asf-view">
          <option value="open">진행 중</option>
          <option value="closed">종료·취소</option>
          <option value="all">전체</option>
        </select>
        <input type="text" id="asf-q" placeholder="접수번호/고객/연락처/관리번호/증상" style="min-width:240px;">
        <button class="btn btn-sm btn-primary" id="asf-search">조회</button>
      </div>
      <div class="table-wrap"><table>
        <thead><tr><th>접수번호</th><th>고객</th><th>관리번호 / 모델</th><th>유형</th><th>증상</th><th>상태</th><th>비용</th><th>담당</th><th></th></tr></thead>
        <tbody id="as-rows"><tr><td colspan="9" class="muted">불러오는 중…</td></tr></tbody>
      </table></div>
    </div>
    <div id="as-detail"></div>`;
  wireAsTabs(main);
  const f = state.asFilter || (state.asFilter = { view: "open", q: "" });
  $("#asf-view").value = f.view;
  $("#asf-q").value = f.q;
  const newBtn = $("#asi-new");
  if (newBtn) newBtn.addEventListener("click", renderAsForm);
  renderAsIntakeWait($("#asi-wait", main));
  renderAsFindList(main);
  if (state.asDetailId) renderAsDetail();
}

/* 📮 택배 접수 대기 — 진행 보드의 [📝 접수] 칸과 같은 자료(서버 산식 하나)로 그린다. */
async function renderAsIntakeWait(host) {
  let d;
  try { d = await api("/api/as-board?view=open"); }
  catch (e) { host.innerHTML = `<p class="muted">${escapeHtml(e.message || "조회 실패")}</p>`; return; }
  if (!host.isConnected) return;
  const rows = d.rows.filter((r) => r.stage === "intake_wait");
  const canEdit = hasPerm("as.manage");
  if (!rows.length) {
    host.innerHTML = `<p class="muted">택배 접수를 기다리는 건이 없습니다 👍</p>`;
    return;
  }
  host.innerHTML = `<div class="table-wrap"><table>
      <thead><tr><th>접수번호</th><th>고객</th><th>주소</th><th>증상</th><th>경과</th><th></th></tr></thead>
      <tbody>${rows.map((r) => `<tr${r.late ? ' class="as-late"' : ""}>
        <td><b>${escapeHtml(r.ticketNo)}</b><div class="muted" style="font-size:12px;">${escapeHtml(r.receivedAt)}</div></td>
        <td>${escapeHtml(r.customer)}<div class="muted" style="font-size:12px;">${escapeHtml(r.phone)}</div></td>
        <td class="muted" style="font-size:12px; max-width:260px;">${r.address
          ? escapeHtml(r.address)
          : '<span style="color:var(--danger);">주소 없음 — [열기]에서 입력</span>'}</td>
        <td class="muted" style="font-size:12px; max-width:200px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;"
            title="${escapeHtml(r.symptom)}">${escapeHtml(r.symptom)}</td>
        <td class="muted">${r.ageDays}일째${r.late ? " ⏰" : ""}</td>
        <td class="as-bact">${canEdit ? `<button class="btn btn-sm btn-primary" data-iact="recall" data-iid="${r.id}"
            title="수거 희망일을 정해 CJ 기사가 고객 주소로 방문하도록 예약합니다">🚚 택배 접수</button>
          <button class="btn btn-sm" data-iact="visit" data-iid="${r.id}"
            title="고객이 직접 가져왔습니다 — 택배를 부르지 않고 바로 입고 완료로">🧍 방문 접수로</button> ` : ""}
          <button class="btn btn-sm" data-iopen="${r.id}">열기</button></td>
      </tr>`).join("")}</tbody></table></div>`;
  const redraw = () => { renderAsIntakeWait(host); renderAsFindList($("#main")); };
  $$("button[data-iopen]", host).forEach((b) => b.addEventListener("click", () => {
    state.asDetailId = Number(b.dataset.iopen);
    renderAsDetail();
  }));
  $$("button[data-iact]", host).forEach((b) => b.addEventListener("click", async () => {
    const row = rows.find((x) => x.id === Number(b.dataset.iid));
    if (!row) return;
    if (b.dataset.iact === "recall") { openAsRecallModal(row, redraw); return; }
    if (!confirm(`${row.customer} 고객이 제품을 직접 가져온 것으로 하고 '입고 완료'로 넘길까요?\n` +
                 `택배 회수는 걸지 않습니다.`)) return;
    b.disabled = true;
    try {
      await api(`/api/as-tickets/${row.id}`, { method: "PATCH", body: { intake: "visit" } });
      toast("방문 접수로 바꿔 입고 완료로 넘겼습니다.");
      redraw();
    } catch (err) { toast(err.message, true); b.disabled = false; }
  }));
}

/* ─────────── 🏠 대시보드 ───────────
   숫자는 진행 보드와 같은 서버 산식(/api/as-dashboard ← _board_stage)이라 두 화면이 어긋나지 않는다.
   '지금 할 일' 칸을 누르면 진행 상황의 그 칸으로 간다 — 여기서는 보기만, 일은 거기서. */
function renderAsDashTab(main) {
  const canEdit = hasPerm("as.manage");
  main.innerHTML = `
    <h1 class="page-title">A/S</h1>
    <p class="page-desc">지금 A/S가 <b>어디에 몇 건</b> 있고 <b>무엇을 해야 하는지</b>를 봅니다 —
      접수는 <b>[📝 접수]</b>, 일은 <b>[🚦 진행 상황]</b>에서 합니다.</p>
    ${asTabsHtml("dash")}
    <div id="asdash"><div class="card placeholder"><p>불러오는 중…</p></div></div>
    <div id="as-detail"></div>`;
  wireAsTabs(main);
  renderAsDashSummary($("#asdash", main));
  if (state.asDetailId) renderAsDetail();
  if (canEdit) { /* 접수는 [📝 접수] 탭에서 — 같은 폼을 두 곳에 두지 않는다 */ }
}

/* 사람이 손을 대야 움직이는 칸 — 숫자가 있으면 빨갛게 부른다 */
const AS_HOT_STAGES = ["intake_wait", "arrived", "pay_wait", "ship_wait", "visit_wait"];

async function renderAsDashSummary(host) {
  let d;
  try { d = await api("/api/as-dashboard"); }
  catch (e) { host.innerHTML = `<div class="card placeholder"><p>${escapeHtml(e.message || "조회 실패")}</p></div>`; return; }
  if (!host.isConnected) return;                   // 그 사이 다른 탭으로 옮겼다
  const stageLabel = Object.fromEntries(d.stages.map((s) => [s.code, s.label]));
  const when = (ts) => escapeHtml((ts || "").replace("T", " ").slice(5, 16));
  host.innerHTML = `
    <div class="kpi-row">
      <div class="kpi"><div class="kpi-label">진행 중</div><div class="kpi-value">${d.open}건</div>
        <div class="kpi-sub">종료·취소 제외</div></div>
      <div class="kpi"><div class="kpi-label">⏰ 지연</div>
        <div class="kpi-value" style="${d.late ? "color:var(--danger);" : ""}">${d.late}건</div>
        <div class="kpi-sub">접수 후 ${d.dueDays}일 초과(설정에서 조정)</div></div>
      <div class="kpi"><div class="kpi-label">오늘 접수</div><div class="kpi-value">${d.todayReceived}건</div></div>
      <div class="kpi"><div class="kpi-label">이번 주 종료</div><div class="kpi-value">${d.weekClosed}건</div>
        <div class="kpi-sub">최근 7일</div></div>
      <div class="kpi"><div class="kpi-label">💰 결제 대기</div><div class="kpi-value">${fmtWon(d.unpaidTotal || 0)}</div>
        <div class="kpi-sub">${d.unpaidCount}건 · 유상 미결제</div></div>
    </div>
    <div class="card">
      <h3 style="margin-top:0;">✅ 지금 할 일 <span class="muted" style="font-weight:400; font-size:13px;">— 누르면 진행 상황의 그 칸으로 갑니다</span></h3>
      <div class="as-board" style="margin-bottom:0;">
        ${d.stages.map((s) => `
          <button class="as-bcard${s.count && AS_HOT_STAGES.includes(s.code) ? " hot" : ""}"
                  data-goto="${s.code}" title="${escapeHtml(s.hint)}">
            <span class="as-bcard-l">${escapeHtml(s.label)}</span>
            <span class="as-bcard-n">${s.count}</span>
          </button>`).join("")}
      </div>
    </div>
    <div class="as-dash-cols">
      <div class="card">
        <h3 style="margin-top:0;">🕒 최근 움직임</h3>
        ${d.recent.length ? `<div class="timeline">${d.recent.map((e) => `
          <div class="tl-item">
            <div class="tl-time">${when(e.ts)}</div>
            <div class="tl-action"><span class="chip chip-slate">${escapeHtml(e.action)}</span>
              <button class="btn btn-ghost btn-sm" data-open="${e.ticketId}" style="padding:0 4px;"><b>${escapeHtml(e.ticketNo)}</b></button>
              ${escapeHtml(e.customer || "")} <span class="muted" style="font-size:12px;">· ${escapeHtml(e.actor || "")}</span></div>
          </div>`).join("")}</div>` : `<p class="muted">아직 기록이 없습니다.</p>`}
      </div>
      <div class="card">
        <h3 style="margin-top:0;">⏰ 늦어지는 건 <span class="muted" style="font-weight:400; font-size:13px;">— 오래된 순</span></h3>
        ${d.lateRows.length ? `<div class="table-wrap"><table>
          <thead><tr><th>접수번호</th><th>고객</th><th>단계</th><th>경과</th></tr></thead>
          <tbody>${d.lateRows.map((r) => `<tr>
            <td><button class="btn btn-ghost btn-sm" data-open="${r.id}" style="padding:0 4px;"><b>${escapeHtml(r.ticketNo)}</b></button></td>
            <td>${escapeHtml(r.customer || "")}</td>
            <td><span class="chip ${AS_STAGE_CHIP[r.stage] || "chip-slate"}">${escapeHtml(stageLabel[r.stage] || r.stage)}</span></td>
            <td><b style="color:var(--danger);">${r.ageDays}일</b></td></tr>`).join("")}</tbody></table></div>`
          : `<p class="muted">기한을 넘긴 건이 없습니다 👍</p>`}
      </div>
    </div>`;
  $$("button[data-goto]", host).forEach((b) => b.addEventListener("click", () => {
    state.asTab = "board";
    state.asBoard = Object.assign(state.asBoard || {}, { stage: b.dataset.goto, q: "" });
    renderAsView($("#main"));
  }));
  $$("button[data-open]", host).forEach((b) => b.addEventListener("click", () => {
    state.asDetailId = Number(b.dataset.open);
    renderAsDetail();
  }));
}

/* 🔎 찾기 목록 — 옛 [📋 접수] 탭의 표 그대로. 증상 분류(대/소분류) 필터는 조사용. */
function renderAsFindList(main) {
  const f = state.asFilter || (state.asFilter = { view: "open", q: "" });
  const canEdit = hasPerm("as.manage");
  const load = async () => {
    const seq = state.renderSeq;
    try {
      await ensureAsMeta();
      const p = new URLSearchParams({ view: f.view });
      if (f.q) p.set("q", f.q);
      const rows = await api("/api/as-tickets?" + p.toString());
      if (seq !== state.renderSeq || !$("#as-rows")) return;
      state.asTickets = rows;
      // ★증상분류 필터(2026-08-31 대표) — 조사용. 상태별 보기는 [🚦 진행 상황] 몫이다.
      let shown = rows;
      if (f.symCat) shown = shown.filter((t) => t.symptomCat === f.symCat);
      if (f.symSub) shown = shown.filter((t) => t.symptomSub === f.symSub);
      $("#as-rows").innerHTML = shown.map((t) => `
        <tr>
          <td><b>${escapeHtml(t.ticketNo)}</b><div class="muted" style="font-size:12px;">${escapeHtml(t.receivedAt)}</div></td>
          <td>${escapeHtml(t.customer)}<div class="muted" style="font-size:12px;">${escapeHtml(t.phone)}</div></td>
          <td>${t.assetNo ? `<span class="chip chip-green">${escapeHtml(t.assetNo)}</span>` : '<span class="muted">미연결</span>'}
            ${t.intake === "visit" ? ` <span class="chip chip-slate" style="font-size:11px;" title="고객이 직접 가져온 건">🧍 방문</span>` : ""}
            ${(t.returnMethod || "parcel") === "visit" ? ` <span class="chip chip-slate" style="font-size:11px;" title="고객이 직접 찾아감 — 송장 불필요">🧍 방문수령</span>` : ""}
            ${asRepeatChip(t)}
            <div class="muted" style="font-size:12px;">${escapeHtml(t.model || "")}</div></td>
          <td>${escapeHtml(t.asTypeLabel)}</td>
          <td style="max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${escapeHtml(t.symptom)}">${t.symptomCat ? `<span class="chip chip-blue" style="font-size:11px;">${escapeHtml(t.symptomCat)}</span> ` : ""}${t.symptomSub ? `<span class="chip chip-amber" style="font-size:11px;">${escapeHtml(t.symptomSub)}</span> ` : ""}${escapeHtml(t.symptom)}</td>
          <td><span class="chip ${AS_CHIP[t.status] || "chip-slate"}">${escapeHtml(t.statusLabel)}</span></td>
          <td>${t.cost ? fmtWon(t.cost) + `<div class="muted" style="font-size:12px;">${escapeHtml(t.chargeLabel)}</div>` : "-"}</td>
          <td class="muted">${escapeHtml(t.assignee)}</td>
          <td><button class="btn btn-sm" data-as="${t.id}">열기</button></td>
        </tr>`).join("") || `<tr><td colspan="9" class="muted">A/S 건이 없습니다.</td></tr>`;
      $$("button[data-as]").forEach((b) => b.addEventListener("click", () => {
        state.asDetailId = Number(b.dataset.as);
        renderAsDetail();
      }));
      // ★증상 분류 필터 바(2026-08-31 대표 저녁 — 대분류/소분류로 조사).
      //   목록에서 지워진 옛 분류도 실제 접수에 붙어 있으면 보인다(기록 보존).
      const symHost = $("#as-tags");
      if (symHost) {
        const m = state.asMeta || {};
        const cats = [...new Set([...(m.symptomCats || []),
                                  ...rows.map((t) => t.symptomCat).filter(Boolean)])];
        const subs = [...new Set([...(m.symptomSubs || []),
                                  ...rows.map((t) => t.symptomSub).filter(Boolean)])];
        const cnt = (key, v) => rows.filter((t) => t[key] === v).length;
        const line = (label, list, key, cur, dk, extra) => (list.length || extra) ? `
          <div class="inline-row" style="flex-wrap:wrap; gap:6px; margin:0 0 6px;">
            <span class="muted" style="font-size:12.5px;">${label}:</span>
            ${list.map((v) => `<button class="btn btn-sm${cur === v ? " btn-primary" : ""}"
              data-${dk}="${escapeHtml(v)}" title="누르면 이 분류만 봅니다 (다시 누르면 해제)">${escapeHtml(v)} <b>${cnt(key, v)}</b></button>`).join("")}
            ${extra || ""}
          </div>` : "";
        symHost.innerHTML =
          line("대분류", cats, "symptomCat", f.symCat, "symcat", "")
          + line("소분류", subs, "symptomSub", f.symSub, "symsub",
                 canEdit ? `<button class="btn btn-ghost btn-sm" id="as-sym-edit"
                   title="⚙ 설정 ▸ 🏷 증상 분류에서 목록을 고칩니다">✎ 분류 관리</button>` : "");
        $$("button[data-symcat]", symHost).forEach((b) => b.addEventListener("click", () => {
          f.symCat = f.symCat === b.dataset.symcat ? "" : b.dataset.symcat;
          load();
        }));
        $$("button[data-symsub]", symHost).forEach((b) => b.addEventListener("click", () => {
          f.symSub = f.symSub === b.dataset.symsub ? "" : b.dataset.symsub;
          load();
        }));
        const edit = $("#as-sym-edit", symHost);
        if (edit) edit.addEventListener("click", () => {
          state.asTab = "settings";
          state.asSetTab = "cats";
          renderAsView($("#main"));
        });
      }
    } catch (err) {
      const el = $("#as-rows");
      if (el) el.innerHTML = `<tr><td colspan="9" class="muted">${escapeHtml(err.message)}</td></tr>`;
    }
  };
  if (!$("#asf-view")) { load(); return; }         // 찾기 칸이 없는 화면(대시보드)에서도 안전
  const doSearch = () => { f.view = $("#asf-view").value; f.q = $("#asf-q").value.trim(); load(); };
  $("#asf-search").addEventListener("click", doSearch);
  $("#asf-view").addEventListener("change", doSearch);
  autoSearch("#asf-q", doSearch);
  const newBtn = $("#asf-new");
  if (newBtn) newBtn.addEventListener("click", renderAsForm);
  load();
}

/* 증상 분류(2026-08-31 대표 저녁 — "대분류/소분류/사유"). 대분류=장비 종류,
   소분류=성격, 사유=증상 칸 자유 서술. select 는 접수 폼·상세 공용 —
   목록에서 지워진 옛 값이 이미 붙어 있으면 보기로 넣는다(기록 보존). */
function symSelectHtml(id, list, cur, blank, disabled) {
  const all = [...new Set([...(list || []), ...(cur ? [cur] : [])])];
  return `<select id="${id}" style="min-width:130px;" ${disabled ? "disabled" : ""}>
    <option value="">${blank}</option>
    ${all.map((v) => `<option value="${escapeHtml(v)}" ${v === cur ? "selected" : ""}>${escapeHtml(v)}</option>`).join("")}
  </select>`;
}

/* ＋ 그 자리에서 분류 추가 — 목록(KV)에 저장하고 지금 고른 select 에도 바로 붙인다 */
/* ── 📦 회수 품목 칸(2026-09-08 대표) ─────────────────────────────
   자주 같이 들어오는 구성품은 눌러서 담고, 없는 것은 [＋]로 만들어 담는다
   (증상 분류의 ＋ 와 같은 방식 — 목록에도 저장돼 다음 접수부터 버튼으로 뜬다). */
function intakeList(input) {
  return input.value.split(",").map((s) => s.trim()).filter(Boolean);
}

/* ★id 는 반드시 'asn-recv-items' / 'asd-recv-items' 다 — 'asd-items' 로 두면 안 된다.
   상세 화면에는 이미 🧾 수리 내역 패널이 <div id="asd-items"> 로 있어서 id 가 겹치고,
   $("#asd-items") 가 문서 위쪽에 있는 이 <input> 을 먼저 잡는다. 그러면 renderAsItems 의
   host.innerHTML 이 input 에 씌어져(void 요소라) 아무 일도 안 일어나고, 수리 내역·수리내역서가
   '불러오는 중…' 에서 영영 멈춘다(2026-09-08 대표 신고로 발견). */
function intakeItemsHtml(id, value, canEdit, inputCss) {
  if (!canEdit) {
    return `<label class="muted as-fl">📦 회수 품목</label>
      <div style="margin-top:4px;">${escapeHtml(value || "") || '<span class="muted">미기재</span>'}</div>
      <input type="text" id="${id}" value="${escapeHtml(value || "")}" hidden disabled>`;
  }
  return `<label class="muted as-fl">📦 회수 품목
      <span style="font-weight:400;">— 고객에게서 함께 받은 물건. 입고·발송 안내 문자에 그대로 나갑니다</span></label>
    <div style="border:1px solid var(--border); border-radius:8px; padding:8px 10px; margin-top:4px;">
      <div id="${id}-picked" class="inline-row" style="flex-wrap:wrap; gap:4px; min-height:28px;"></div>
      <div class="inline-row" style="flex-wrap:wrap; gap:4px; margin-top:6px; padding-top:6px;
           border-top:1px dashed var(--border);">
        <span class="muted" style="font-size:11.5px;">담을 것 고르기</span>
        <span id="${id}-pool" class="inline-row" style="flex-wrap:wrap; gap:4px;"></span>
        <button type="button" class="btn btn-ghost btn-sm" data-itemnew="${id}"
          title="목록에 없는 구성품을 새로 만들어 바로 담습니다 — 목록에 저장돼 다음 접수부터 버튼으로 뜹니다">＋ 새 구성품</button>
        <button type="button" class="btn btn-ghost btn-sm" data-itemtype="${id}"
          title="목록에 둘 필요 없는 일회성 품목은 여기에 직접 적습니다">✎ 직접 입력</button>
      </div>
      <input type="text" id="${id}" value="${escapeHtml(value || "")}"
        placeholder="쉼표로 구분 — 예: 본체, 충전기" style="${inputCss}" hidden>
    </div>`;
}

/* 담긴 품목 칩과 '고를 것' 버튼을 입력칸 값에 맞춰 다시 그린다.
   ★입력칸(#id)이 늘 정본이다 — 버튼도 직접 입력도 결국 이 문자열 하나를 고친다. */
function paintIntakeItems(id) {
  const input = $("#" + id);
  if (!input) return;
  const picked = $("#" + id + "-picked");
  const pool = $("#" + id + "-pool");
  if (!picked || !pool) return;
  const have = intakeList(input);
  picked.innerHTML = have.length
    ? have.map((x) => `<button type="button" class="btn btn-primary btn-sm" data-itemdel="${id}"
        data-item="${escapeHtml(x)}" title="빼려면 누르세요">${escapeHtml(x)} ✕</button>`).join("")
    : `<span class="muted" style="font-size:12px;">아직 없습니다 — 아래에서 골라 담으세요.</span>`;
  pool.innerHTML = ((state.asMeta || {}).intakeItemPresets || [])
    .filter((x) => !have.includes(x))
    .map((x) => `<button type="button" class="btn btn-ghost btn-sm" data-itemadd="${id}"
        data-item="${escapeHtml(x)}" title="누르면 위에 담깁니다">＋ ${escapeHtml(x)}</button>`).join("")
    || `<span class="muted" style="font-size:12px;">다 담았습니다.</span>`;
  $$(`button[data-itemadd="${id}"]`).forEach((b) =>
    b.addEventListener("click", () => setIntakeItems(id, [...intakeList(input), b.dataset.item])));
  $$(`button[data-itemdel="${id}"]`).forEach((b) =>
    b.addEventListener("click", () =>
      setIntakeItems(id, intakeList(input).filter((x) => x !== b.dataset.item))));
}

function setIntakeItems(id, list) {
  const input = $("#" + id);
  const seen = [];
  list.forEach((x) => { if (x && !seen.includes(x)) seen.push(x); });   // 같은 품목 두 번 금지
  input.value = seen.join(", ");
  paintIntakeItems(id);
}

function wireIntakeItems(host, id) {
  const input = $("#" + id, host) || $("#" + id);
  if (!input) return;
  input.addEventListener("input", () => paintIntakeItems(id));
  const typeBtn = $(`button[data-itemtype="${id}"]`, host);
  if (typeBtn) {
    typeBtn.addEventListener("click", () => {
      input.hidden = !input.hidden;
      if (!input.hidden) input.focus();
    });
  }
  const add = $(`button[data-itemnew="${id}"]`, host);
  if (add) {
    add.addEventListener("click", async () => {
      const name = (prompt("새 구성품 이름(20자 이내, 쉼표 없이):") || "").trim();
      if (!name) return;
      try {
        const items = [...((state.asMeta || {}).intakeItemPresets || [])];
        if (!items.includes(name)) items.push(name);
        const r = await api("/api/as-intake-presets", { method: "PUT", body: { items } });
        if (state.asMeta) state.asMeta.intakeItemPresets = r.items;
        setIntakeItems(id, [...intakeList(input), name]);      // 목록에 저장 + 이 건에 바로 담기
        toast(`구성품 '${name}' — 목록에 저장했습니다. 다음 접수부터 버튼으로 뜹니다.`);
      } catch (err) { toast(err.message, true); }
    });
  }
  paintIntakeItems(id);
}

async function symQuickAdd(kind, sel) {
  const label = kind === "cats" ? "대분류" : "소분류";
  const name = (prompt(`새 ${label} 이름(30자 이내):`) || "").trim();
  if (!name) return;
  try {
    const m = state.asMeta || {};
    const cats = [...(m.symptomCats || [])];
    const subs = [...(m.symptomSubs || [])];
    const list = kind === "cats" ? cats : subs;
    if (!list.includes(name)) list.push(name);
    const r = await api("/api/as-symptom-cats", { method: "PUT", body: { cats, subs } });
    if (state.asMeta) {
      state.asMeta.symptomCats = r.cats;
      state.asMeta.symptomSubs = r.subs;
    }
    if (sel && ![...sel.options].some((o) => o.value === name)) {
      const opt = document.createElement("option");
      opt.value = name;
      opt.textContent = name;
      sel.appendChild(opt);
    }
    if (sel) sel.value = name;
  } catch (err) { toast(err.message, true); }
}

/* A/S 자동완성(2026-08-31 대표) — 목록 상자는 입력칸과 분리해서만 그린다.
   ★입력칸을 다시 그리면 한글 조합(IME)이 끊긴다(2026-08-26 검색창 수리와 같은 원칙). */
const ASN_AC_BOX = `display:none; position:absolute; z-index:40; left:0; right:0; top:100%;
  max-height:280px; overflow:auto; background:var(--surface); border:1px solid var(--border);
  border-radius:8px; box-shadow:0 6px 18px rgba(0,0,0,.18);`;

function attachAsAutocomplete(input, box, fetcher, rowHtml, onPick) {
  if (!input || !box) return;
  input.setAttribute("autocomplete", "off");
  let seq = 0;
  const close = () => { box.style.display = "none"; box.innerHTML = ""; };
  const run = async () => {
    const q = input.value.trim();
    if (q.length < 2) return close();
    const my = ++seq;
    try {
      const rows = await fetcher(q);
      if (my !== seq) return;                      // 늦게 온 응답은 버린다
      if (!rows.length) {
        box.innerHTML = `<div class="muted" style="padding:8px 10px;">검색 결과가 없습니다.</div>`;
        box.style.display = "block";
        return;
      }
      box.innerHTML = rows.map((r, i) => `<div class="asn-ac-row" data-i="${i}"
        style="padding:8px 10px; cursor:pointer; border-bottom:1px solid var(--border);">${rowHtml(r)}</div>`).join("");
      box.style.display = "block";
      $$(".asn-ac-row", box).forEach((el) => el.addEventListener("mousedown", (e) => {
        e.preventDefault();                        // blur보다 먼저 — 클릭이 죽지 않게
        onPick(rows[Number(el.dataset.i)]);
        close();
      }));
    } catch (_e) { close(); }
  };
  autoSearch(input, run, 300);                     // IME 안전 디바운스(조합 중 미발화)
  input.addEventListener("blur", () => setTimeout(close, 150));
  input.addEventListener("keydown", (e) => { if (e.key === "Escape") close(); });
}

/* 🚫 회수 예약 취소(2026-09-03 대표 "회수예약건 자체를 취소하는 기능도").
   ★CJ가 거절하는 경우가 있다(이미 집화됐거나 CJ 쪽에서 먼저 취소된 건). 그때 그냥 실패로
     끝내면 송장이 '발행'으로 남아 중복 가드에 걸려 **다시 회수를 걸 수 없다** — 서버의
     탈출구(forceLocal)를 사람이 확인한 뒤에만 쓴다. RMS 의 skip_cancel 과 같은 규칙. */
async function cancelAsRecall(t, onDone) {
  const stage = (t.recallStage || "").trim();
  if (!confirm(`${t.customer} 고객의 회수 예약을 취소합니다.
` +
      `${t.recallInvoiceNo ? `송장번호 ${t.recallInvoiceNo}
` : ""}` +
      `${stage ? `현재 택배 단계: ${stage}
` : ""}` +
      `
취소하면 CJ 기사가 방문하지 않고, 접수 건은 '접수'로 돌아갑니다.
계속할까요?`)) return;
  const go = async (forceLocal) => {
    const r = await api(`/api/waybills/${encodeURIComponent(t.recallWid)}/cancel`,
                        { method: "POST", body: forceLocal ? { forceLocal: true } : {} });
    toast(r.cjCancelFailed
      ? "OWS 기록만 취소했습니다 — CJ 예약은 직접 확인하세요."
      : "회수 예약을 취소했습니다.");
    if (onDone) onDone();
  };
  try { await go(false); }
  catch (err) {
    if (/CJ 예약 취소 실패/.test(err.message)) {
      if (!confirm(`${err.message}

이미 기사가 다녀갔거나 CJ 쪽에서 먼저 취소된 건일 수 있습니다.
` +
                   `OWS 기록만 취소할까요? (그래야 이 건에 회수를 다시 걸 수 있습니다)
` +
                   `★CJ 예약이 살아 있다면 기사가 방문할 수 있으니 CJ에서도 확인하세요.`)) return;
      try { await go(true); } catch (e2) { toast(e2.message, true); }
      return;
    }
    toast(err.message, true);
  }
}

function renderAsForm() {
  const host = $("#as-form");
  const meta = state.asMeta || { types: [], charges: [], intakes: [] };
  let picked = null;
  const inputCss = "width:100%; margin-top:4px; padding:8px 10px; border:1px solid var(--border); border-radius:8px; background:var(--bg);";
  host.innerHTML = `
    <div style="border:1px dashed var(--border); border-radius:8px; padding:16px 18px; margin:8px 0;">
      <b style="font-size:15px;">A/S 접수</b>
      <p class="muted" style="font-size:12.5px; margin:4px 0 0;">
        왼쪽은 <b>누구에게서 받아 어디로 돌려주는지</b>, 오른쪽은 <b>무엇이 왜 들어왔는지</b>입니다.</p>
      <div class="as-form2">
        <section class="as-fcol">
          <h4 class="as-fcol-h">👤 고객 · 주소</h4>
          <div class="form-grid">
            <label style="position:relative;">고객명 *<input type="text" id="asn-customer">
              <div id="asn-customer-list" style="${ASN_AC_BOX}"></div></label>
            <label style="position:relative;">연락처<input type="text" id="asn-phone">
              <div id="asn-phone-list" style="${ASN_AC_BOX}"></div></label>
          </div>
          <label class="muted as-fl">주소
            <span style="font-weight:400;">— 택배 회수·반송에 씁니다(방문 접수는 비워도 됩니다)</span></label>
          <div class="inline-row" style="margin-top:4px;">
            <input type="text" id="asn-zip" placeholder="우편번호" style="width:110px;">
            <button class="btn btn-sm" id="asn-addr-find" type="button">🔍 주소검색</button>
          </div>
          <input type="text" id="asn-address" placeholder="기본주소 (주소검색으로 채우는 걸 권장)" style="${inputCss}">
          <input type="text" id="asn-addr2" placeholder="상세주소 (동·호수 등)" style="${inputCss}">
        </section>
        <section class="as-fcol">
          <h4 class="as-fcol-h">🖥 접수 내용</h4>
          <label class="muted as-fl" style="margin-top:0;">제품 연결
            <span style="font-weight:400;">— 우리가 판 기계면 찾아서 이어 주세요(자산 이력에 남습니다)</span></label>
          <div class="inline-row" style="margin-top:4px;">
            <span style="position:relative; flex:1; min-width:220px;">
              <input type="text" id="asn-assetq" style="width:100%;"
                     placeholder="관리번호/시리얼/모델/고객명/연락처 — 치면 바로 뜹니다">
              <div id="asn-assetq-list" style="${ASN_AC_BOX}"></div>
            </span>
            <span id="asn-picked" class="muted">제품 미연결</span>
          </div>
          <div class="form-grid" style="margin-top:10px;">
            <label>접수 경로<select id="asn-intake">${(meta.intakes || []).map((x) => `<option value="${x.code}">${escapeHtml(x.label)}</option>`).join("")}</select></label>
            <label>유형<select id="asn-type">${meta.types.map((t) => `<option value="${t.code}">${escapeHtml(t.label)}</option>`).join("")}</select></label>
            <label>비용 부담<select id="asn-charge">${meta.charges.map((c) => `<option value="${c.code}">${escapeHtml(c.label)}</option>`).join("")}</select></label>
            <label>접수일<input type="date" id="asn-date" value="${ymd()}"></label>
            <label>담당자<input type="text" id="asn-assignee" value="${escapeHtml(state.user.displayName)}"></label>
          </div>
          <label class="muted as-fl">증상 분류</label>
          <div class="inline-row" style="flex-wrap:wrap; gap:6px; margin-top:4px; align-items:center;">
            ${symSelectHtml("asn-symcat", (state.asMeta || {}).symptomCats, "", "대분류 (선택)")}
            <button type="button" class="btn btn-ghost btn-sm" data-symadd="cats"
              title="새 대분류를 만들어 바로 고릅니다 — 목록에도 저장됩니다">＋</button>
            ${symSelectHtml("asn-symsub", (state.asMeta || {}).symptomSubs, "", "소분류 (선택)")}
            <button type="button" class="btn btn-ghost btn-sm" data-symadd="subs"
              title="새 소분류를 만들어 바로 고릅니다 — 목록에도 저장됩니다">＋</button>
          </div>
          <label class="muted as-fl">증상(사유) * <span style="font-weight:400;">— 자유 서술</span>
            <input type="text" id="asn-symptom" placeholder="예: 전원이 켜지지 않음" style="${inputCss}"></label>
          ${intakeItemsHtml("asn-recv-items", "", true, inputCss)}
        </section>
      </div>
      <div class="editor-actions">
        <button class="btn btn-primary" id="asn-save">접수</button>
        <button class="btn" id="asn-close">닫기</button>
      </div>
    </div>`;
  // ★접수는 팝업으로 띄운다(2026-09-03 대표) — 목록 아래에 길게 펼쳐지면
  //   아래로 한참 스크롤해야 해서 무엇을 적는 중인지 안 보였다.
  //   [닫기]·접수 완료가 host 를 비우면 팝업도 같이 닫힌다(openModalWith 의 감시자).
  host.classList.add("as-wide-form");
  openModalWith(host);
  $("#asn-close").addEventListener("click", () => { host.innerHTML = ""; });
  attachAddrSearch($("#asn-addr-find"), { zip: "#asn-zip", addr: "#asn-address", detail: "#asn-addr2" });
  $$("button[data-symadd]", host).forEach((b) => b.addEventListener("click", () =>
    symQuickAdd(b.dataset.symadd, $(b.dataset.symadd === "cats" ? "#asn-symcat" : "#asn-symsub"))));
  wireIntakeItems(host, "asn-recv-items");

  // 고른 결과로 빈칸만 채운다(overwrite=true면 덮어씀) — 치던 값을 말없이 바꾸지 않는다
  const fill = (r, overwrite) => {
    const set = (sel, v) => {
      const el = $(sel);
      if (el && v && (overwrite || !el.value.trim())) el.value = v;
    };
    set("#asn-customer", r.name || r.recipient);
    set("#asn-phone", r.phone);
    set("#asn-zip", r.postalCode);
    set("#asn-address", r.address);
  };
  attachAsAutocomplete(
    $("#asn-assetq"), $("#asn-assetq-list"),
    (q) => api("/api/as-tickets/asset-search?q=" + encodeURIComponent(q)),
    (r) => `<b>${escapeHtml(r.assetNo)}</b> · ${escapeHtml([r.maker, r.model].filter(Boolean).join(" ") || "-")}
      <span class="chip chip-slate">${escapeHtml(r.statusLabel || r.status)}</span>
      <div class="muted" style="font-size:12px;">${escapeHtml(r.recipient || "구매 고객 없음")}${r.phone ? " · " + escapeHtml(r.phone) : ""}</div>`,
    (r) => {
      picked = { assetId: r.assetId, assetNo: r.assetNo, orderId: r.orderId || null };
      $("#asn-picked").innerHTML = `연결됨: <span class="chip chip-green">${escapeHtml(r.assetNo)}</span>
        <span class="muted">${escapeHtml([r.maker, r.model].filter(Boolean).join(" "))}</span>`;
      fill(r, false);
    });
  const customerFetch = (q) => api("/api/as-tickets/customer-search?q=" + encodeURIComponent(q));
  const customerRow = (r) => `<b>${escapeHtml(r.name)}</b> ${escapeHtml(r.phone || "")}
    <span class="chip chip-slate">${escapeHtml(r.source)}</span>
    <div class="muted" style="font-size:12px;">${escapeHtml(r.address || "")}${r.lastAt ? " · " + escapeHtml(r.lastAt) : ""}</div>`;
  attachAsAutocomplete($("#asn-customer"), $("#asn-customer-list"), customerFetch, customerRow, (r) => fill(r, true));
  attachAsAutocomplete($("#asn-phone"), $("#asn-phone-list"), customerFetch, customerRow, (r) => fill(r, true));

  $("#asn-save").addEventListener("click", async () => {
    try {
      const addr = [$("#asn-address").value.trim(), $("#asn-addr2").value.trim()].filter(Boolean).join(" ");
      const res = await api("/api/as-tickets", { method: "POST", body: {
        customer: $("#asn-customer").value.trim(), phone: $("#asn-phone").value,
        address: addr, postalCode: $("#asn-zip").value.trim(),
        intake: $("#asn-intake").value, symptom: $("#asn-symptom").value.trim(),
        intakeItems: $("#asn-recv-items").value.trim(),
        symptomCat: $("#asn-symcat").value, symptomSub: $("#asn-symsub").value,
        asType: $("#asn-type").value, chargeTo: $("#asn-charge").value,
        receivedAt: $("#asn-date").value, assignee: $("#asn-assignee").value,
        assetId: picked ? picked.assetId : null, orderId: picked ? picked.orderId : null,
      } });
      toast(`A/S 접수 완료: ${res.ticketNo}`);
      // 접수 안내 문자가 못 나갔으면(번호 없음·050 등) 조용히 넘기면 안 된다
      if (res.sms && res.sms.ok === false && res.sms.message) toast(res.sms.message, true);
      host.innerHTML = "";
      renderAsView($("#main"));
    } catch (err) { toast(err.message, true); }
  });
}

async function renderAsDetail() {
  const host = $("#as-detail");
  if (!host || !state.asDetailId) return;
  let t;
  try { t = await api(`/api/as-tickets/${state.asDetailId}`); }
  catch (err) { host.innerHTML = `<div class="card"><p class="muted">${escapeHtml(err.message)}</p></div>`; return; }
  const canEdit = hasPerm("as.manage");
  const meta = state.asMeta || { types: [], statuses: [], charges: [], intakes: [] };
  const dis = canEdit ? "" : "disabled";
  revealPanel(host);
  host.innerHTML = `
    <div class="as-dcol">
    <div class="card">
      <div class="inline-row">
        <h3 style="margin:0; flex:1;">${escapeHtml(t.ticketNo)}
          <span class="chip ${AS_CHIP[t.status] || "chip-slate"}">${escapeHtml(t.statusLabel)}</span>
          <span class="chip chip-slate" title="접수 경로">${t.intake === "visit" ? "🧍 방문" : "🚚 택배"}</span>
          ${t.assetNo ? `<span class="chip chip-green">${escapeHtml(t.assetNo)}</span>` : ""}
        </h3>
        <button class="btn btn-sm" id="asd-close">닫기</button>
      </div>
      <div class="form-grid" style="margin-top:8px;">
        <label>고객명<input type="text" id="asd-customer" value="${escapeHtml(t.customer)}" ${dis}></label>
        <label>연락처<input type="text" id="asd-phone" value="${escapeHtml(t.phone)}" ${dis}></label>
        <label>자산번호(관리번호)<input type="text" id="asd-assetno" value="${escapeHtml(t.assetNo || "")}" ${dis}
          placeholder="우리 관리번호와 일치하면 자산과 연결됩니다"
          title="저장할 때 우리 자산 관리번호와 정확히 일치하면 그 자산에 연결됩니다 — 아니면 수기로만 남습니다"></label>
        <label>모델명<input type="text" id="asd-model" value="${escapeHtml(t.model || "")}" ${dis}
          placeholder="예: LG그램 15Z990"></label>
        <label>유형<select id="asd-type" ${dis}>${meta.types.map((x) => `<option value="${x.code}" ${x.code === t.asType ? "selected" : ""}>${escapeHtml(x.label)}</option>`).join("")}</select></label>
        <label id="asd-exwrap" style="${t.asType === "exchange" ? "" : "display:none;"}">교환 제품코드
          <input type="text" id="asd-exproduct" value="${escapeHtml(t.exchangeProductCode || "")}" ${dis}
            placeholder="교환해 드릴 우리 제품의 제품코드"
            title="어떤 제품과 교환하는지 — 수리내역서·청구내역서에 함께 찍힙니다"></label>
        <label>접수 경로<select id="asd-intake" ${dis}>${(meta.intakes || []).map((x) => `<option value="${x.code}" ${x.code === t.intake ? "selected" : ""}>${escapeHtml(x.label)}</option>`).join("")}</select></label>
        <label>수령 방법 <span style="font-weight:400;">— 돌려줄 때</span>
          <select id="asd-retmethod" ${dis}>${(meta.returnMethods || [{ code: "parcel", label: "택배 발송" }, { code: "visit", label: "방문 수령" }]).map((x) => `<option value="${x.code}" ${x.code === (t.returnMethod || "parcel") ? "selected" : ""}>${escapeHtml(x.label)}</option>`).join("")}</select></label>
        <label>상태<select id="asd-status" ${dis}>${meta.statuses.map((x) => `<option value="${x.code}" ${x.code === t.status ? "selected" : ""}>${escapeHtml(x.label)}</option>`).join("")}</select></label>
        <label>비용 부담<select id="asd-charge" ${dis}>${meta.charges.map((x) => `<option value="${x.code}" ${x.code === t.chargeTo ? "selected" : ""}>${escapeHtml(x.label)}</option>`).join("")}</select></label>
        <label>수리비 <span class="muted" style="font-weight:400;">(수리 내역 합계 — 자동)</span>
          <input type="text" id="asd-cost" value="${t.cost}" readonly
            title="아래 🧾 수리 내역에 적은 금액의 합계가 자동으로 들어옵니다 — 여기서 직접 고칠 수 없습니다"
            style="background:var(--bg-subtle, #f3f4f6); cursor:not-allowed;"></label>
        <label>담당자<input type="text" id="asd-assignee" value="${escapeHtml(t.assignee)}" ${dis}></label>
        <label>접수일<input type="date" id="asd-date" value="${escapeHtml(t.receivedAt)}" ${dis}></label>
      </div>
      <label class="muted" style="display:block; margin-top:8px;">주소 <span style="font-weight:400;">(택배 회수·반송에 필요 — 우편번호까지 있어야 CJ 규격입니다)</span>
        ${canEdit ? `<button class="btn btn-sm" id="asd-addr-find" type="button" style="margin-left:6px;">🔍 주소검색</button>` : ""}
        <div class="inline-row" style="margin-top:4px;">
          <input type="text" id="asd-zip" value="${escapeHtml(t.postalCode || "")}" placeholder="우편번호" style="width:100px;" ${dis}>
          <input type="text" id="asd-address" value="${escapeHtml(t.address || "")}" style="flex:1; padding:8px 10px; border:1px solid var(--border); border-radius:8px; background:var(--bg);" ${dis}>
        </div></label>
      <div class="inline-row" style="flex-wrap:wrap; gap:6px; margin-top:8px; align-items:center;">
        <span class="muted" style="font-size:12.5px;">분류:</span>
        ${symSelectHtml("asd-symcat", meta.symptomCats, t.symptomCat || "", "대분류 (선택)", !canEdit)}
        ${canEdit ? `<button type="button" class="btn btn-ghost btn-sm" data-symadd="cats"
          title="새 대분류를 만들어 바로 고릅니다 — 목록에도 저장됩니다">＋</button>` : ""}
        ${symSelectHtml("asd-symsub", meta.symptomSubs, t.symptomSub || "", "소분류 (선택)", !canEdit)}
        ${canEdit ? `<button type="button" class="btn btn-ghost btn-sm" data-symadd="subs"
          title="새 소분류를 만들어 바로 고릅니다 — 목록에도 저장됩니다">＋</button>` : ""}
      </div>
      <label class="muted" style="display:block; margin-top:8px;">증상(사유) <span style="font-weight:400;">— 자유 서술</span>
        <input type="text" id="asd-symptom" value="${escapeHtml(t.symptom)}" style="width:100%; margin-top:4px; padding:8px 10px; border:1px solid var(--border); border-radius:8px; background:var(--bg);" ${dis}></label>
      <div style="margin-top:8px;">${intakeItemsHtml("asd-recv-items", t.intakeItems, canEdit,
        "width:100%; margin-top:4px; padding:8px 10px; border:1px solid var(--border); border-radius:8px; background:var(--bg);")}</div>
      <label class="muted" style="display:block; margin-top:8px;">처리 내용
        <input type="text" id="asd-result" value="${escapeHtml(t.result)}" style="width:100%; margin-top:4px; padding:8px 10px; border:1px solid var(--border); border-radius:8px; background:var(--bg);" ${dis}></label>
      ${t.chargeTo === "customer" ? `<div class="as-payline${t.paidAt ? " is-paid" : ""}">
        <b>💰 청구 ${fmtWon(t.billTotal || 0)}</b>
        ${t.paidAt
          ? `<span class="chip chip-green">받음 ${escapeHtml(t.paidAt)} · ${fmtWon(t.paidAmount || 0)}${t.paidMethod ? " · " + escapeHtml(t.paidMethod) : ""}</span>
             ${canEdit ? `<button class="btn btn-ghost btn-sm" id="asd-unpay"
                 title="잘못 눌렀으면 되돌립니다">결제 확인 해제</button>` : ""}`
          : `<span class="chip chip-amber">결제 전</span>
             ${canEdit && (t.billTotal || 0) > 0
               ? `<button class="btn btn-sm btn-primary" id="asd-pay"
                    title="입금을 받았다고 표시합니다 — 현황의 [결제 전] 칸에서 빠집니다">💰 결제 확인</button>`
               : `<span class="muted" style="font-size:12px;">— 아래 🧾 수리 내역에 금액을 적으면 결제 확인을 할 수 있습니다</span>`}`}
        <span class="muted" style="font-size:12px;">받은 돈만 기록합니다(세금계산서·현금영수증은 별도)</span>
      </div>` : ""}
      ${canEdit ? `<div class="editor-actions">
        <button class="btn btn-primary" id="asd-save">저장</button>
        ${t.recallWid
          ? `<span class="chip ${t.recallDone ? "chip-green" : "chip-blue"}"
                title="진행 상황 탭에서 택배 단계를 봅니다${t.recallStageAt ? `&#10;${escapeHtml(t.recallStageAt)}` : ""}">🚚 ${t.recallDone ? "회수 입고 완료" : "회수 예약됨"} ${escapeHtml(t.recallWid)}${t.recallStage && !t.recallDone ? ` · ${escapeHtml(t.recallStage)}` : ""}</span>${
            // 물건이 이미 들어온 건(입고 완료)은 무를 대상이 아니다 — 서버도 막는다
            t.recallDone ? "" : `<button class="btn" id="asd-received"
                title="물건이 도착했는데 CJ 추적이 아직이면 직접 입고 완료로 넘깁니다">📥 입고 처리</button>
              <button class="btn" id="asd-recall-cancel"
              title="예약을 취소하면 CJ 기사가 방문하지 않습니다">🚫 회수 예약 취소</button>`}`
          : (t.intake === "visit"
              // 방문 접수는 고객이 들고 왔다 — 회수할 것이 없다(2026-09-03 대표)
              ? `<span class="chip chip-slate" title="고객이 직접 가져온 건이라 회수가 필요 없습니다">🧍 방문 접수 — 회수 불필요</span>`
              : `<button class="btn" id="asd-recall" title="수거 희망일을 정해 CJ 회수를 예약합니다(쉬는 날은 고를 수 없습니다)">🚚 택배 접수</button>`)}
        ${(t.status === "received" || (t.status === "arrived" && !t.recallWid))
          ? `<button class="btn btn-ghost" id="asd-delete"
               title="잘못 넣었거나 고객이 무른 접수 — '취소'로 바뀌고 진행 보드에서 빠집니다(기록은 남습니다)">🗑 접수 삭제</button>` : ""}
        ${t.returnWid
          ? `<a class="btn" href="/api/waybills/${encodeURIComponent(t.returnWid)}/pdf" target="_blank"
               title="신규 송장 ${escapeHtml(t.returnInvoiceNo || "")}">🧾 송장 출력</a>${
            t.returnStage ? `<span class="chip ${t.returnStage === "배송완료" ? "chip-green" : "chip-blue"}"
               title="${escapeHtml(t.returnStageAt || "")}">🚚 ${escapeHtml(t.returnStage)}</span>` : ""}`
          : ((t.returnMethod || "parcel") === "visit"
              // 방문 수령은 고객이 찾으러 온다 — 송장을 끊지 않는다(2026-09-03 대표)
              ? `<span class="chip chip-slate" title="고객이 직접 찾아가는 건이라 송장이 필요 없습니다">🧍 방문 수령 — 송장 불필요</span>`
              : `<button class="btn" id="asd-return" title="A/S를 마친 제품을 고객에게 보냅니다 — 새 송장을 끊습니다">📦 신규 송장 발급</button>`)}
        ${t.assetId && t.cost > 0 && t.chargeTo === "company"
          ? `<button class="btn" id="asd-tocost">수리비를 자산 원가에 반영</button>` : ""}
      </div>
      <div id="asd-recall-form"></div>` : ""}
    </div>
    <div class="card">
      <h3>👤 이 고객 · 이 기계의 과거</h3>
      <div id="asd-history"><p class="muted">불러오는 중…</p></div>
    </div>
    </div>
    <div class="as-dcol">
    <div class="card">
      <h3>🧾 수리 내역 <span class="muted" style="font-weight:400; font-size:13px;">
        — 항목별 금액(부가세 포함 총액). 공급가·부가세는 자동으로 갈라 적히고,
        수리내역서·청구내역서가 이 표로 발행됩니다.</span></h3>
      <div id="asd-items"><p class="muted">불러오는 중…</p></div>
    </div>
    <div class="card">
      <h3>💬 SMS 발송</h3>
      ${canEdit ? `<div class="inline-row" style="margin:2px 0 10px; flex-wrap:wrap;">
        ${AS_SMS_EVENTS.map(([code, label]) =>
          `<button class="btn btn-ghost btn-sm" data-sms="${code}">${label}</button>`).join("")}
        <button class="btn btn-ghost btn-sm" id="asd-sms-custom"
          title="[💬 문자 양식] 탭에서 직접 만든 양식으로 보냅니다">📑 기타 양식…</button>
        <span class="muted" style="font-size:12px;">— 누르면 문구를 확인·수정한 뒤 보냅니다</span>
      </div>` : ""}
      <b class="muted" style="font-size:13px;">보낸 문자</b>
      <div id="asd-sms"><p class="muted">불러오는 중…</p></div>
    </div>
    <div class="card">
      <h3>이력</h3>
      <div class="timeline">${t.events.map((e) => `
        <div class="tl-item">
          <div class="tl-time">${escapeHtml(e.ts.replace("T", " ").slice(0, 19))}</div>
          <div class="tl-action"><span class="chip chip-slate">${escapeHtml(e.action)}</span> <b>${escapeHtml(e.actor)}</b></div>
          <div class="tl-detail muted">${escapeHtml(e.detail ? JSON.stringify(e.detail) : "")}</div>
        </div>`).join("") || `<p class="muted">이력이 없습니다.</p>`}
      </div>
    </div>
    </div>`;
  // ★상세도 팝업 + 좌우 2단(2026-09-03 대표 "하단 스크롤보다 좌/우로 볼 수 있게").
  //   ★칸을 자동 배치에 맡겼더니 오른쪽 위가 통째로 빈 구멍이 됐다(2026-09-03 대표
  //   "수정 중에 깨진 것 같다"). 이제 왼쪽(.as-dcol)·오른쪽(.as-dcol) 두 기둥에
  //   카드를 직접 담는다 — 구멍이 생길 수 없고, 좁은 화면에서는 위아래로 쌓인다.
  host.classList.add("as-wide-form");
  openModalWith(host);
  $("#asd-close").addEventListener("click", () => { state.asDetailId = undefined; host.innerHTML = ""; });
  attachAddrSearch($("#asd-addr-find"), { zip: "#asd-zip", addr: "#asd-address" });
  if (canEdit) {
    $$("button[data-symadd]", host).forEach((b) => b.addEventListener("click", () =>
      symQuickAdd(b.dataset.symadd, $(b.dataset.symadd === "cats" ? "#asd-symcat" : "#asd-symsub"))));
    wireIntakeItems(host, "asd-recv-items");
  }
  renderAsItems(t);
  renderAsHistory(t);
  if (!canEdit) return;
  // 유형을 교환으로 바꾸면 교환 제품코드 칸이 나타난다(저장 전에도 적을 수 있게)
  $("#asd-type").addEventListener("change", () => {
    $("#asd-exwrap").style.display = $("#asd-type").value === "exchange" ? "" : "none";
  });
  // 💰 결제 확인/해제(2026-09-03 대표 "진행완료되어 결제전인지")
  const payBtn = $("#asd-pay", host);
  if (payBtn) payBtn.addEventListener("click", () => openAsPayModal(t, () => renderAsDetail()));
  const unpayBtn = $("#asd-unpay", host);
  if (unpayBtn) unpayBtn.addEventListener("click", async () => {
    if (!confirm("결제 확인을 해제할까요?\n다시 '결제 전'으로 돌아갑니다.")) return;
    try {
      await api(`/api/as-tickets/${t.id}/payment`, { method: "POST", body: { paid: false } });
      toast("결제 확인을 해제했습니다.");
      renderAsDetail();
    } catch (err) { toast(err.message, true); }
  });
  $("#asd-save").addEventListener("click", async () => {
    try {
      // ★수리비(cost)는 안 보낸다 — 수리 내역 합계가 진실이고, 여기서 덮으면 어긋난다
      await api(`/api/as-tickets/${t.id}`, { method: "PATCH", body: {
        customer: $("#asd-customer").value, phone: $("#asd-phone").value,
        assetNo: $("#asd-assetno").value.trim(), model: $("#asd-model").value.trim(),
        exchangeProductCode: $("#asd-exproduct").value.trim(),
        asType: $("#asd-type").value, status: $("#asd-status").value,
        intake: $("#asd-intake").value,
        returnMethod: $("#asd-retmethod") ? $("#asd-retmethod").value : undefined,
        chargeTo: $("#asd-charge").value,
        assignee: $("#asd-assignee").value, receivedAt: $("#asd-date").value,
        address: $("#asd-address").value, postalCode: $("#asd-zip").value.trim(),
        symptom: $("#asd-symptom").value,
        intakeItems: $("#asd-recv-items").value,
        symptomCat: $("#asd-symcat").value, symptomSub: $("#asd-symsub").value,
        result: $("#asd-result").value,
      } });
      toast("저장했습니다.");
      renderAsView($("#main"));
    } catch (err) {
      // 예전엔 수리비가 늘 실려 가서 무변경 저장도 조용히 지나갔다 — 같은 감각 유지
      if (/변경할 항목이 없습니다/.test(err.message)) { toast("바뀐 내용이 없습니다."); return; }
      toast(err.message, true);
    }
  });
  const recallCancelBtn = $("#asd-recall-cancel", host);
  if (recallCancelBtn) recallCancelBtn.addEventListener("click", () =>
    cancelAsRecall(t, () => renderAsView($("#main"))));
  // 택배 접수는 보드·접수 탭과 같은 창(openAsRecallModal) — 상세 위에서는 2층으로 뜬다
  const recallBtn = $("#asd-recall");
  if (recallBtn) recallBtn.addEventListener("click", () =>
    openAsRecallModal(t, () => renderAsView($("#main"))));   // 목록·요약 숫자도 함께 갱신
  const receivedBtn = $("#asd-received", host);
  if (receivedBtn) receivedBtn.addEventListener("click", async () => {
    if (!confirm(`${t.customer} 고객의 회수 물건이 도착한 것으로 하고 '입고 완료'로 넘길까요?`)) return;
    receivedBtn.disabled = true;
    try {
      await api(`/api/waybills/${encodeURIComponent(t.recallWid)}/received`,
                { method: "POST", body: { status: "as" } });
      toast("입고 완료로 넘겼습니다.");
      renderAsView($("#main"));
    } catch (err) { toast(err.message, true); receivedBtn.disabled = false; }
  });
  const deleteBtn = $("#asd-delete", host);
  if (deleteBtn) deleteBtn.addEventListener("click", () =>
    deleteAsTicket(t, () => { state.asDetailId = undefined; renderAsView($("#main")); }));
  // 이 고객에게 무엇을 보냈는지 — 같은 안내를 또 보내지 않도록 여기서 확인한다
  (async () => {
    const host = $("#asd-sms");
    if (!host) return;
    try {
      const rows = await api(`/api/sms/log?ticketId=${t.id}`);
      host.innerHTML = rows.length ? `<div class="table-wrap"><table>
        <thead><tr><th>시각</th><th>종류</th><th>결과</th><th>내용</th></tr></thead>
        <tbody>${rows.map((r) => `<tr>
          <td class="muted">${escapeHtml((r.sentAt || "").replace("T", " ").slice(5, 16))}</td>
          <td>${escapeHtml(smsEventLabel(r.event))}</td>
          <td>${r.status === "sent" ? '<span class="chip chip-green">발송</span>'
               : r.status === "simulated" ? '<span class="chip chip-slate">미발송(기록만)</span>'
               : r.status === "skipped" ? '<span class="chip chip-slate">건너뜀</span>'
               : '<span class="chip chip-red">실패</span>'}
            ${r.reason ? `<div class="muted" style="font-size:12px;">${escapeHtml(r.reason.slice(0, 60))}</div>` : ""}</td>
          <td style="max-width:340px;"><span class="muted" style="font-size:12px; white-space:pre-wrap;">${escapeHtml(r.text || "")}</span></td>
        </tr>`).join("")}</tbody></table></div>`
        : `<p class="muted">보낸 문자가 없습니다.</p>`;
    } catch (err) { host.innerHTML = `<p class="muted">${escapeHtml(err.message)}</p>`; }
  })();

  // ★범위는 host(=이 상세 팝업)다(2026-07-29 전수조사의 스코프 교훈 유지).
  //   2026-08-31 대표: 바로 보내지 않고 미리보기 모달에서 문구를 고쳐 보낼 수 있다.
  $$("button[data-sms]", host).forEach((b) => b.addEventListener("click", () => {
    openAsSmsModal(t, b.dataset.sms, b.textContent.trim());
  }));
  const customSmsBtn = $("#asd-sms-custom", host);
  if (customSmsBtn) customSmsBtn.addEventListener("click", () => openAsSmsPicker(t));
  const returnBtn = $("#asd-return");
  if (returnBtn) returnBtn.addEventListener("click", async () => {
    if (!confirm(`${t.customer} 고객에게 반송할 송장을 발급합니다.\n주소: ${t.address || "(비어 있음)"}\n\n계속할까요?`)) return;
    returnBtn.disabled = true;
    try {
      const r = await api(`/api/as-tickets/${t.id}/return-waybill`, { method: "POST", body: {} });
      toast(r.simulated ? `테스트 반송 송장: ${r.wid}` : `반송 송장 발급 완료: ${r.invoiceNo}`);
      window.open(`/api/waybills/${encodeURIComponent(r.wid)}/pdf`, "_blank");
      renderAsView($("#main"));   // 목록·요약 숫자도 함께 갱신
    } catch (err) { toast(err.message, true); returnBtn.disabled = false; }
  });
  const toCost = $("#asd-tocost");
  if (toCost) toCost.addEventListener("click", async () => {
    if (!confirm("이 수리비를 해당 자산의 수리 내역으로 기록할까요? (자산 원가에 반영됩니다)")) return;
    try {
      await api(`/api/as-tickets/${t.id}/to-asset-repair`, { method: "POST" });
      toast("자산 원가에 반영했습니다.");
      renderAsView($("#main"));   // 목록·요약 숫자도 함께 갱신
    } catch (err) { toast(err.message, true); }
  });
}

/* ─────────────── 수리 내역(항목별 금액) + 수리내역서·청구내역서(2026-08-31 대표) ───────────────
   항목 금액 = 부가세 포함 총액(OWS 규약). 공급가·부가세는 서버 split_vat 와 같은 식으로
   즉시 갈라 보여 주고, 합계 칸에 총액을 넣으면 줄별로 비례 배분한다(끝줄이 끝전을 먹는다 —
   월정기 세금계산서 마지막 회차와 같은 규칙). 발행은 서버 스냅샷 → 인쇄창(@page A4). */

function splitVatJs(total) {
  const vat = Math.round(total * 10 / 110);   // 서버 split_vat 와 같은 식 — 다르면 문서가 어긋난다
  return { vat, net: total - vat };
}

async function renderAsItems(t) {
  const host = $("#asd-items");
  if (!host) return;
  const canEdit = hasPerm("as.manage");
  let items = [];
  let docs = { docs: [], stamp: "" };
  try {
    items = await api(`/api/as-tickets/${t.id}/items`);
    docs = await api(`/api/as-tickets/${t.id}/documents`);
  } catch (err) { host.innerHTML = `<p class="muted">${escapeHtml(err.message)}</p>`; return; }
  // 저장된 내역이 없고 단일 수리비만 있으면 그걸 첫 줄로 — 옛 건도 문서를 바로 뽑을 수 있게
  if (!items.length) items = [{ name: t.cost ? "수리비" : "", qty: 1, amount: t.cost || 0 }];
  let repairParts = [];
  try { repairParts = await api("/api/parts?kind=repair"); } catch (_e) {}   // 매입 권한 없으면 자동완성만 생략

  const paint = () => {
    const total = items.reduce((s, it) => s + (Number(it.amount) || 0), 0);
    const tv = splitVatJs(total);
    host.innerHTML = `
      <datalist id="asi-parts">${repairParts.map((p) =>
        `<option value="${escapeHtml(p.name)}"></option>`).join("")}</datalist>
      <div class="table-wrap"><table>
        <thead><tr><th style="min-width:180px;">항목</th><th style="width:70px;">수량</th>
          <th style="width:120px;">금액(부가세 포함)</th><th style="width:110px;">공급가</th>
          <th style="width:100px;">부가세</th>${canEdit ? '<th style="width:36px;"></th>' : ""}</tr></thead>
        <tbody>${items.map((it, i) => {
          const v = splitVatJs(Number(it.amount) || 0);
          return `<tr>
            <td><input type="text" data-asi="name" data-i="${i}" list="asi-parts"
              value="${escapeHtml(it.name || "")}" placeholder="부품·공임·출장비…"
              style="width:100%;" ${canEdit ? "" : "disabled"}></td>
            <td><input type="number" data-asi="qty" data-i="${i}" value="${it.qty || 1}" min="1"
              style="width:100%;" ${canEdit ? "" : "disabled"}></td>
            <td><input type="number" data-asi="amount" data-i="${i}" value="${it.amount || 0}" min="0"
              step="1000" style="width:100%;" ${canEdit ? "" : "disabled"}></td>
            <td style="text-align:right;" class="muted" data-asinet="${i}">${fmtWon(v.net)}</td>
            <td style="text-align:right;" class="muted" data-asivat="${i}">${fmtWon(v.vat)}</td>
            ${canEdit ? `<td><button class="btn btn-ghost btn-sm" data-asidel="${i}" title="줄 삭제">✕</button></td>` : ""}
          </tr>`;
        }).join("")}</tbody>
        <tfoot><tr style="border-top:2px solid var(--border);">
          <td><b>합계</b>${canEdit ? ' <span class="muted" style="font-size:12px;">— 총액을 넣으면 줄별로 나눠 채웁니다</span>' : ""}</td>
          <td></td>
          <td>${canEdit ? `<input type="number" id="asi-total" value="${total}" min="0" step="1000" style="width:100%;">`
                        : `<b>${fmtWon(total)}</b>`}</td>
          <td style="text-align:right;"><b id="asi-net">${fmtWon(tv.net)}</b></td>
          <td style="text-align:right;"><b id="asi-vat">${fmtWon(tv.vat)}</b></td>
          ${canEdit ? "<td></td>" : ""}
        </tr></tfoot>
      </table></div>
      ${canEdit ? `<div class="inline-row" style="margin-top:8px; flex-wrap:wrap; align-items:center;">
        <b class="muted" style="font-size:13px;">추가비용</b>
        <input type="number" id="asi-extra" value="${t.extraCharge || 0}" min="0" step="1000" style="width:120px;">
        <input type="text" id="asi-extra-note" value="${escapeHtml(t.extraNote || "")}"
          placeholder="사유 — 예: 교환 차액" style="flex:1; min-width:160px;">
        <span class="muted" style="font-size:12px;">수리비와 별개로 문서 합계에 얹힙니다 — [내역 저장]으로 함께 저장</span>
      </div>` : (t.extraCharge ? `<p class="muted" style="margin:8px 0 0;">추가비용 ${fmtWon(t.extraCharge)}${t.extraNote ? ` — ${escapeHtml(t.extraNote)}` : ""}</p>` : "")}
      ${canEdit ? `<div class="inline-row" style="margin-top:8px;">
        <button class="btn btn-sm" id="asi-add">＋ 항목</button>
        <button class="btn btn-sm btn-primary" id="asi-save">내역 저장</button>
        <span style="flex:1"></span>
        <button class="btn btn-sm" id="asi-doc-repair" title="수리 내용·항목·금액이 담긴 내역서를 발행합니다">🧾 수리내역서</button>
        <button class="btn btn-sm" id="asi-doc-invoice" title="고객 청구용 — 공급가/부가세/합계로 발행합니다">💰 청구내역서</button>
      </div>` : ""}
      ${docs.docs.length ? `<div style="margin-top:10px;">
        <b class="muted" style="font-size:13px;">발행 이력</b>
        <div class="table-wrap"><table>
          <thead><tr><th>문서번호</th><th>종류</th><th>합계</th><th>발행</th><th></th></tr></thead>
          <tbody>${docs.docs.map((d) => `<tr>
            <td><b>${escapeHtml(d.docNo)}</b></td>
            <td>${escapeHtml(d.snapshot.docLabel)}</td>
            <td>${fmtWon(d.snapshot.totals.amount)}</td>
            <td class="muted">${escapeHtml((d.issuedAt || "").replace("T", " ").slice(0, 16))} · ${escapeHtml(d.issuedBy)}</td>
            <td><button class="btn btn-sm" data-asdoc="${d.id}">다시 인쇄</button>${canEdit
              ? ` <button class="btn btn-ghost btn-sm" data-asdocdel="${d.id}"
                    title="발행 기록 삭제 — 지운 문서번호는 다시 쓰지 않습니다">🗑</button>` : ""}</td>
          </tr>`).join("")}</tbody></table></div>
        <p class="muted" style="font-size:12px; margin:4px 0 0;">발행본은 발행 당시 내용 그대로입니다 — 항목을 고쳤다면 새로 발행하세요.</p>
      </div>` : ""}`;
    wire();
  };

  const readRow = (el) => {
    const i = Number(el.dataset.i);
    const it = items[i];
    if (!it) return;
    if (el.dataset.asi === "name") it.name = el.value;
    else if (el.dataset.asi === "qty") it.qty = Math.max(1, Number(el.value) || 1);
    else it.amount = Math.max(0, Math.round(Number(el.value) || 0));
  };
  const refreshTotals = () => {
    items.forEach((it, i) => {
      const v = splitVatJs(Number(it.amount) || 0);
      const n = host.querySelector(`[data-asinet="${i}"]`);
      const vt = host.querySelector(`[data-asivat="${i}"]`);
      if (n) n.textContent = fmtWon(v.net);
      if (vt) vt.textContent = fmtWon(v.vat);
    });
    const total = items.reduce((s, it) => s + (Number(it.amount) || 0), 0);
    const tv = splitVatJs(total);
    const ti = $("#asi-total", host);
    if (ti && document.activeElement !== ti) ti.value = total;
    const n = $("#asi-net", host), v = $("#asi-vat", host);
    if (n) n.textContent = fmtWon(tv.net);
    if (v) v.textContent = fmtWon(tv.vat);
  };
  const wire = () => {
    if (!canEdit) return;
    $$("[data-asi]", host).forEach((el) => el.addEventListener("input", () => {
      readRow(el);
      refreshTotals();
    }));
    // 합계에 총액을 넣으면 — 줄별 비례 배분, 전부 0이면 균등, 끝줄이 끝전을 먹는다
    $("#asi-total", host)?.addEventListener("change", () => {
      const want = Math.max(0, Math.round(Number($("#asi-total", host).value) || 0));
      const cur = items.reduce((s, it) => s + (Number(it.amount) || 0), 0);
      let acc = 0;
      items.forEach((it, i) => {
        if (i === items.length - 1) { it.amount = want - acc; return; }
        const share = cur > 0 ? Math.round(want * (Number(it.amount) || 0) / cur)
                              : Math.round(want / items.length);
        it.amount = share;
        acc += share;
      });
      paint();
    });
    $("#asi-add", host)?.addEventListener("click", () => {
      items.push({ name: "", qty: 1, amount: 0 });
      paint();
    });
    $$("button[data-asidel]", host).forEach((b) => b.addEventListener("click", () => {
      items.splice(Number(b.dataset.asidel), 1);
      if (!items.length) items.push({ name: "", qty: 1, amount: 0 });
      paint();
    }));
    const save = async () => {
      const body = { items: items.filter((it) => (it.name || "").trim())
        .map((it) => ({ name: it.name.trim(), qty: Number(it.qty) || 1,
                        amount: Math.max(0, Math.round(Number(it.amount) || 0)) })) };
      if (!body.items.length) { toast("항목 이름을 하나 이상 적어 주세요.", true); return null; }
      const r = await api(`/api/as-tickets/${t.id}/items`, { method: "POST", body });
      items = r.items;
      const costEl = $("#asd-cost");
      if (costEl) costEl.value = r.cost;   // 상세의 수리비 칸도 합계로 동기화
      // 추가비용(교환 차액 등)도 함께 저장 — 바뀌었을 때만(무변경 PATCH 는 400)
      const exEl = $("#asi-extra", host);
      if (exEl) {
        const want = Math.max(0, Math.round(Number(exEl.value) || 0));
        const note = ($("#asi-extra-note", host)?.value || "").trim();
        if (want !== (t.extraCharge || 0) || note !== (t.extraNote || "")) {
          await api(`/api/as-tickets/${t.id}`, { method: "PATCH",
                    body: { extraCharge: want, extraNote: note } });
          t.extraCharge = want;
          t.extraNote = note;
        }
      }
      return r;
    };
    $("#asi-save", host)?.addEventListener("click", async () => {
      try {
        await save();
        toast("수리 내역을 저장했습니다.");
        paint();
      } catch (err) { toast(err.message, true); }
    });
    const issue = async (docType) => {
      try {
        await save();                       // 화면의 최신 표 그대로 발행되게 먼저 저장
        const r = await api(`/api/as-tickets/${t.id}/documents`,
                            { method: "POST", body: { docType } });
        printAsDocument(r.doc, r.stamp);
        renderAsItems(t);                   // 발행 이력 갱신
      } catch (err) { toast(err.message, true); }
    };
    $("#asi-doc-repair", host)?.addEventListener("click", () => issue("repair"));
    $("#asi-doc-invoice", host)?.addEventListener("click", () => issue("invoice"));
    $$("button[data-asdoc]", host).forEach((b) => b.addEventListener("click", () => {
      const d = docs.docs.find((x) => x.id === Number(b.dataset.asdoc));
      if (d) printAsDocument(d.snapshot, docs.stamp);
    }));
    $$("button[data-asdocdel]", host).forEach((b) => b.addEventListener("click", async () => {
      const d = docs.docs.find((x) => x.id === Number(b.dataset.asdocdel));
      if (!d) return;
      if (!confirm(`발행 기록 ${d.docNo} (${d.snapshot.docLabel})을 삭제할까요?\n` +
                   `지운 문서번호는 다른 문서에 다시 쓰이지 않습니다.`)) return;
      try {
        await api(`/api/as-tickets/${t.id}/documents/${d.id}`, { method: "DELETE" });
        toast("발행 기록을 삭제했습니다.");
        renderAsItems(t);
      } catch (err) { toast(err.message, true); }
    }));
  };
  paint();
}

/* A4 인쇄 — 견본/발행이 같은 렌더러(라벨과 같은 원칙). 도장은 서명란 (인) 위에 겹쳐 찍는다. */
function asDocHtml(d, stamp) {
  const m = (n) => (Number(n) || 0).toLocaleString("ko-KR");
  const c = d.company, cu = d.customer, tk = d.ticket;
  return `
  <div class="doc">
    <h1>${escapeHtml(d.docLabel)}</h1>
    <div class="meta">문서번호 ${escapeHtml(d.docNo)} · 발행일 ${escapeHtml((d.issuedAt || "").slice(0, 10))}</div>
    <table class="grid two">
      <tr><th colspan="2" class="sec">공급자</th><th colspan="2" class="sec">고객</th></tr>
      <tr><th>상호</th><td class="stampcell">${escapeHtml(c.name)}
          ${stamp ? `<img class="stamp" src="${stamp}">` : ""}</td>
        <th>고객명</th><td>${escapeHtml(cu.name || "")}</td></tr>
      <tr><th>사업자번호</th><td>${escapeHtml(c.bizReg || "")}</td>
        <th>연락처</th><td>${escapeHtml(cu.phone || "")}</td></tr>
      <tr><th>담당자</th><td>${escapeHtml(c.manager || "")} ${c.phone ? `(${escapeHtml(c.phone)})` : ""}</td>
        <th>주소</th><td>${escapeHtml(cu.address || "")}</td></tr>
      <tr><th>주소</th><td colspan="3">${escapeHtml(c.address || "")}</td></tr>
    </table>
    <table class="grid">
      <tr><th class="sec" colspan="4">대상 제품 · 접수 내용</th></tr>
      <tr><th>접수번호</th><td>${escapeHtml(tk.ticketNo)} (${escapeHtml(tk.asType)})</td>
        <th>접수일</th><td>${escapeHtml(tk.receivedAt || "")}</td></tr>
      <tr><th>관리번호</th><td>${escapeHtml(tk.assetNo || "-")}</td>
        <th>모델</th><td>${escapeHtml(tk.model || "-")}</td></tr>
      ${tk.exchangeProductCode
        ? `<tr><th>교환 제품코드</th><td colspan="3">${escapeHtml(tk.exchangeProductCode)}</td></tr>` : ""}
      <tr><th>증상</th><td colspan="3">${escapeHtml(tk.symptom || "")}</td></tr>
      <tr><th>처리 내용</th><td colspan="3">${escapeHtml(tk.result || "")}</td></tr>
    </table>
    <table class="grid items">
      <thead><tr><th style="width:6%;">#</th><th>항목</th><th style="width:8%;">수량</th>
        <th style="width:16%;">공급가</th><th style="width:14%;">부가세</th>
        <th style="width:17%;">금액(부가세 포함)</th></tr></thead>
      <tbody>${d.items.map((it, i) => `<tr>
        <td class="ctr">${i + 1}</td><td>${escapeHtml(it.name)}</td><td class="ctr">${it.qty}</td>
        <td class="num">${m(it.net)}</td><td class="num">${m(it.vat)}</td>
        <td class="num">${m(it.amount)}</td></tr>`).join("")}</tbody>
      <tfoot>${(() => {
        // 추가비용(교환 차액 등)이 있으면 수리 소계 → 추가비용 → 합계 3줄, 없으면 합계 1줄
        const ex = d.extra && d.extra.amount > 0 ? d.extra : null;
        const total = `<tr><th colspan="3">합계</th>
          <td class="num"><b>${m(d.totals.net)}</b></td>
          <td class="num"><b>${m(d.totals.vat)}</b></td>
          <td class="num"><b>${m(d.totals.amount)}</b></td></tr>`;
        if (!ex) return total;
        const ia = d.items.reduce((s, it) => s + (Number(it.amount) || 0), 0);
        const iv = d.items.reduce((s, it) => s + (Number(it.vat) || 0), 0);
        return `<tr><th colspan="3">수리 소계</th>
            <td class="num">${m(ia - iv)}</td><td class="num">${m(iv)}</td><td class="num">${m(ia)}</td></tr>
          <tr><th colspan="3">추가비용${ex.note ? ` (${escapeHtml(ex.note)})` : ""}</th>
            <td class="num">${m(ex.net)}</td><td class="num">${m(ex.vat)}</td><td class="num">${m(ex.amount)}</td></tr>` + total;
      })()}</tfoot>
    </table>
    <p class="total-line">${d.docType === "invoice"
      ? `청구 금액: <b>${m(d.totals.amount)}원</b> <span class="dim">(공급가 ${m(d.totals.net)}원 + 부가세 ${m(d.totals.vat)}원)</span>`
      : `수리 금액 합계: <b>${m(d.totals.amount)}원</b> <span class="dim">(부가세 ${m(d.totals.vat)}원 포함)</span>`}</p>
    ${d.docType === "invoice" && c.bank
      ? `<p class="bank-line">입금 계좌: <b>${escapeHtml(c.bank)}</b></p>` : ""}
    <p class="sign">${escapeHtml((d.issuedAt || "").slice(0, 10))}
      &nbsp;&nbsp; ${escapeHtml(c.name)} &nbsp; 담당 ${escapeHtml(c.manager || d.issuedBy)}
      <span class="seal">(인)${stamp ? `<img class="stamp" src="${stamp}">` : ""}</span></p>
  </div>`;
}

const AS_DOC_CSS = `
  @page { size: A4; margin: 15mm; }
  * { box-sizing: border-box; }
  body { margin: 0; font-family: 'Malgun Gothic', '맑은 고딕', sans-serif; color: #000; background: #fff; }
  .doc h1 { text-align: center; letter-spacing: 14px; font-size: 26pt; margin: 4mm 0 2mm; }
  .meta { text-align: right; font-size: 9.5pt; color: #333; margin-bottom: 4mm; }
  table.grid { width: 100%; border-collapse: collapse; margin-bottom: 5mm; font-size: 10pt; }
  table.grid th, table.grid td { border: 1px solid #444; padding: 2.2mm 2.6mm; text-align: left; }
  table.grid th { background: #f1f1f1; font-weight: 700; width: 13%; white-space: nowrap; }
  table.grid th.sec { background: #e3e3e3; text-align: center; }
  table.items thead th { text-align: center; width: auto; }
  td.num { text-align: right; } td.ctr { text-align: center; }
  .stampcell { position: relative; }
  .stamp { position: absolute; right: 2mm; top: 50%; transform: translateY(-50%); width: 16mm; opacity: .92; }
  .total-line { font-size: 12pt; text-align: right; margin: 2mm 0 10mm; }
  .total-line .dim { font-size: 9.5pt; color: #444; }
  /* 계좌 줄은 청구내역서에만 붙는다 — 위 10mm 여백을 파고들어 다른 문서 배치는 안 바꾼다 */
  .bank-line { font-size: 11pt; text-align: right; margin: -8mm 0 10mm; }
  .sign { text-align: right; font-size: 11pt; margin-top: 14mm; }
  .sign .seal { position: relative; padding: 0 4mm; }
  .sign .seal .stamp { right: -2mm; top: -7mm; transform: none; width: 18mm; }`;

function printAsDocument(d, stamp) {
  // ★라벨 인쇄와 같은 이유로 숨은 iframe 인쇄(2026-08-31) — 발행(저장→서버→인쇄)이
  //   클릭에서 한 박자 늦어 팝업이 차단되면 문서가 조용히 안 나간다.
  const fr = document.createElement("iframe");
  fr.style.cssText = "position:fixed; right:0; bottom:0; width:0; height:0; border:0;";
  document.body.appendChild(fr);
  const doc = fr.contentDocument;
  doc.open();
  doc.write(`<!doctype html><html><head><meta charset="utf-8">
    <title>${escapeHtml(d.docLabel)} ${escapeHtml(d.docNo)}</title>
    <style>${AS_DOC_CSS}</style></head><body>${asDocHtml(d, stamp)}</body></html>`);
  doc.close();
  setTimeout(() => {
    try { fr.contentWindow.focus(); fr.contentWindow.print(); } catch (_e) {}
    setTimeout(() => fr.remove(), 60000);   // 인쇄 중 제거하면 백지가 나간다
  }, 300);
  return true;
}

/* 이 고객·이 기계의 과거 — 전화번호로 주문·A/S, 자산으로 출고·수리 이력 */
async function renderAsHistory(t) {
  const host = $("#asd-history");
  if (!host) return;
  let h;
  try { h = await api(`/api/as-tickets/${t.id}/history`); }
  catch (err) { host.innerHTML = `<p class="muted">${escapeHtml(err.message)}</p>`; return; }
  const empty = !h.orders.length && !h.tickets.length && !h.assetEvents.length && !h.repairs.length;
  if (empty) {
    host.innerHTML = `<p class="muted">이 연락처·자산으로 남은 과거 기록이 없습니다.</p>`;
    return;
  }
  host.innerHTML = `
    ${h.orders.length ? `<b class="muted" style="font-size:13px;">구매 이력 (같은 연락처 주문 ${h.orders.length}건)</b>
    <div class="table-wrap"><table>
      <thead><tr><th>채널</th><th>주문번호</th><th>상품</th><th>자산번호</th><th>주문일</th><th>출고일</th></tr></thead>
      <tbody>${h.orders.map((o) => `<tr${o.cancelled ? ' style="opacity:.55;"' : ""}>
        <td>${chBadge(o.channel)}</td>
        <td>${escapeHtml(o.orderNumber || "-")}${o.cancelled ? ' <span class="chip chip-red">취소</span>' : ""}</td>
        <td style="max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${escapeHtml(o.productName)}">${escapeHtml(o.productName)}</td>
        <td class="muted">${escapeHtml(o.assetNos || "-")}</td>
        <td class="muted">${escapeHtml(o.orderedAt)}</td>
        <td class="muted">${escapeHtml(o.shippedAt || "-")}</td>
      </tr>`).join("")}</tbody></table></div>` : ""}
    ${h.tickets.length ? `<b class="muted" style="font-size:13px; display:block; margin-top:8px;">과거 A/S ${h.tickets.length}건</b>
    <div class="table-wrap"><table>
      <thead><tr><th>접수번호</th><th>유형</th><th>증상</th><th>상태</th><th>접수일</th></tr></thead>
      <tbody>${h.tickets.map((x) => `<tr>
        <td><b>${escapeHtml(x.ticketNo)}</b></td><td>${escapeHtml(x.asType)}</td>
        <td style="max-width:240px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${escapeHtml(x.symptom)}">${escapeHtml(x.symptom)}</td>
        <td>${escapeHtml(x.status)}</td><td class="muted">${escapeHtml(x.receivedAt)}</td>
      </tr>`).join("")}</tbody></table></div>` : ""}
    ${h.repairs.length ? `<b class="muted" style="font-size:13px; display:block; margin-top:8px;">이 자산의 수리 기록</b>
    <ul style="margin:4px 0 0 18px;">${h.repairs.map((r) => `<li class="muted">${escapeHtml(r.date)} —
      ${escapeHtml(r.description)} (${fmtWon(r.cost)})</li>`).join("")}</ul>` : ""}
    ${h.assetEvents.length ? `<details style="margin-top:8px;"><summary class="muted" style="cursor:pointer; font-size:13px;">
      이 자산의 흐름(매입→매칭→출고→회수) ${h.assetEvents.length}건</summary>
      <div class="timeline" style="margin-top:6px;">${h.assetEvents.map((e) => `
        <div class="tl-item">
          <div class="tl-time">${escapeHtml((e.ts || "").replace("T", " ").slice(0, 16))}</div>
          <div class="tl-action"><span class="chip chip-slate">${escapeHtml(e.action)}</span></div>
          <div class="tl-detail muted">${escapeHtml(e.detail ? JSON.stringify(e.detail) : "")}</div>
        </div>`).join("")}</div></details>` : ""}`;
}

/* 설정 ▸ 운영 설정 ▸ A/S 문서 — 회사 정보(상호·담당자·연락처 필수)와 도장.
   app.js renderOperationsTab 이 부른다. 문서 발행이 이 값을 그대로 찍는다. */
async function renderAsDocSettings(host) {
  if (!host) return;
  let d;
  try { d = await api("/api/as-doc-settings"); }
  catch (_e) { host.innerHTML = ""; return; }   // 권한 없으면 조용히 생략
  const canEdit = hasPerm("settings.manage");
  const c = d.company || {};
  host.innerHTML = `
    <div class="card">
      <h3 style="margin-top:0;">🧾 A/S 문서 (수리내역서 · 청구내역서)</h3>
      <p class="muted" style="margin:2px 0 8px; font-size:13px;">
        문서에 찍히는 공급자 정보입니다 — <b>상호·담당자·연락처·도장이 있어야 발행됩니다.</b>
        ${c.suggested ? "아직 저장 전이라 CJ 설정에서 가져온 추천값이 채워져 있습니다 — 확인 후 저장하세요." : ""}</p>
      <div class="form-grid">
        <label>상호 *<input type="text" id="asdoc-name" value="${escapeHtml(c.name || "")}" ${canEdit ? "" : "disabled"}></label>
        <label>대표자<input type="text" id="asdoc-ceo" value="${escapeHtml(c.ceo || "")}" ${canEdit ? "" : "disabled"}></label>
        <label>담당자 *<input type="text" id="asdoc-manager" value="${escapeHtml(c.manager || "")}" ${canEdit ? "" : "disabled"}></label>
        <label>연락처 *<input type="text" id="asdoc-phone" value="${escapeHtml(c.phone || "")}" ${canEdit ? "" : "disabled"}></label>
        <label>사업자번호<input type="text" id="asdoc-bizreg" value="${escapeHtml(c.bizReg || "")}" ${canEdit ? "" : "disabled"}></label>
        <label>주소<input type="text" id="asdoc-address" value="${escapeHtml(c.address || "")}" ${canEdit ? "" : "disabled"}></label>
        <label>계좌번호 <span class="muted" style="font-weight:400;">(수리비 입금 계좌)</span>
          <input type="text" id="asdoc-bank" value="${escapeHtml(c.bank || "")}" ${canEdit ? "" : "disabled"}
            placeholder="예: 국민 000000-00-000000 예시 운영사"
            title="청구내역서 아래와 {계좌번호} 문자 자리에 그대로 찍힙니다"></label>
      </div>
      <div class="inline-row" style="gap:8px; margin-top:8px; align-items:center;">
        <b style="font-size:13px; flex:0 0 130px;">회사 도장</b>
        ${canEdit ? `<input type="file" id="asdoc-stamp-file" accept="image/*" style="font-size:12px;">
        <button class="btn btn-ghost btn-sm" id="asdoc-stamp-del" ${d.stamp ? "" : "disabled"}>제거</button>` : ""}
        ${d.stamp ? `<img src="${d.stamp}" style="height:44px;" alt="도장 미리보기">`
                  : `<span class="muted" style="font-size:12px;">없음 — 배경이 투명한 PNG를 권장합니다</span>`}
      </div>
      ${canEdit ? `<div class="editor-actions"><button class="btn btn-primary" id="asdoc-save">저장</button></div>` : ""}
    </div>`;
  if (!canEdit) return;
  $("#asdoc-save", host).addEventListener("click", async () => {
    try {
      await api("/api/as-doc-settings", { method: "POST", body: { company: {
        name: $("#asdoc-name", host).value, ceo: $("#asdoc-ceo", host).value,
        manager: $("#asdoc-manager", host).value, phone: $("#asdoc-phone", host).value,
        bizReg: $("#asdoc-bizreg", host).value, address: $("#asdoc-address", host).value,
        bank: $("#asdoc-bank", host).value,
      } } });
      toast("A/S 문서 회사 정보를 저장했습니다.");
      renderAsDocSettings(host);
    } catch (err) { toast(err.message, true); }
  });
  $("#asdoc-stamp-file", host)?.addEventListener("change", (e) => {
    const f = e.target.files && e.target.files[0];
    if (!f) return;
    if (f.size > 280_000) { toast("도장 이미지가 너무 큽니다 — 300KB 이내로 줄여 주세요.", true); return; }
    const rd = new FileReader();
    rd.onload = async () => {
      try {
        await api("/api/as-doc-stamp", { method: "POST", body: { dataUrl: rd.result } });
        toast("도장을 등록했습니다.");
        renderAsDocSettings(host);
      } catch (err) { toast(err.message, true); }
    };
    rd.readAsDataURL(f);
  });
  $("#asdoc-stamp-del", host)?.addEventListener("click", async () => {
    try {
      await api("/api/as-doc-stamp", { method: "POST", body: { dataUrl: "" } });
      toast("도장을 제거했습니다.");
      renderAsDocSettings(host);
    } catch (err) { toast(err.message, true); }
  });
}

/* ⚙ 설정 탭 셸(2026-08-31 대표 저녁 — "A/S 내 설정을 나누고 … 세부탭").
   세부탭: [🏷 증상 분류] [💬 문자 양식]. 옛 state.asTab==="sms" 는 설정▸문자 양식으로 흡수. */
/* 📊 A/S 현황 — 기간별 접수·분류 분포·처리 소요일·유상무상·원가/수익(2026-09-02 대표).
   ★돈은 판매 매출과 합치지 않는다(대표 확정) — 'A/S 수입'으로 나란히 본다.
     수입 = 유상 건의 수리비 + 추가비용, 원가 = 수리내역에 동결된 부품값. */
/* 재발 배지(2026-09-02 대표 "같은 자산·고객이 반복 접수될 때 표시 — 불량 모델 파악").
   같은 기계가 또 들어온 것이 먼저다(제품 문제일 수 있음), 같은 고객은 그 다음. */
function asRepeatChip(t) {
  const a = Number(t.repeatAsset || 0), c = Number(t.repeatCust || 0);
  if (a > 0) {
    return ` <span class="chip chip-red" style="font-size:11px;"
      title="이 기계가 A/S로 들어온 것이 이번이 ${a + 1}번째입니다 — 제품 문제일 수 있습니다.">🔁 재입고 ${a + 1}회</span>`;
  }
  if (c > 0) {
    return ` <span class="chip chip-amber" style="font-size:11px;"
      title="이 고객의 A/S가 이번이 ${c + 1}번째입니다(다른 기계 포함).">👤 ${c + 1}번째</span>`;
  }
  return "";
}

/* ─────────── 🚦 진행 상황(옛 📊 현황, 2026-09-07 대표 개편) ───────────
   대표 요구: "택배 회수 신청해서 배송완료된 게 있는지, 접수해서 진행 중인지,
   진행완료되어 결제 전인지, 배송 전인지 — A/S 담당자가 현황 파악이 되도록."
   담당자가 매일 보는 것은 '지금 무엇을 해야 하나'다 — 통계는 매출/실적 ▸ [🔧 A/S 실적]. */
function renderAsBoardTab(main) {
  main.innerHTML = `
    <h1 class="page-title">A/S</h1>
    <p class="page-desc">접수 → 회수 중 → 입고 완료 → 수리 중 → 결제 전 → 발송 전(방문 수령) → 택배 출고 → 종료.
      칸을 누르면 그 단계만 보고, 줄마다 <b>다음 할 일</b> 버튼이 있습니다.
      회수·출고 택배의 <b>배달완료는 CJ 추적이 알아서 넘깁니다</b>.</p>
    ${asTabsHtml("board")}
    <div id="ast-shell"><div class="card placeholder"><p>불러오는 중…</p></div></div>
    <div id="as-detail"></div>`;
  wireAsTabs(main);
  renderAsBoard($("#ast-shell"));
  // 보드에서 [열기]를 눌러도 이 화면 위에 상세 팝업이 뜬다(탭이 튀지 않게)
  if (state.asDetailId) renderAsDetail();
}

/* 📈 A/S 실적 — 기간별 접수·분류 분포·처리 소요일·유상무상·원가/수익(2026-09-02 대표).
   ★2026-09-03 대표: "A/S 내 현황 통계는 매출실적에서 보여주면 돼" —
     A/S 탭에서 빼고 설정 ▸ 매출/실적 ▸ [🔧 A/S 실적]에서 그린다(app.js renderSalesTab).
     A/S 탭의 [📊 현황]은 '지금 어디에 있나'만 본다. 돈은 다른 실적과 나란히 봐야 한다.
   ★돈은 판매 매출과 합치지 않는다(대표 확정) — 'A/S 수입'으로 나란히 본다. */
function renderAsStatsBody(host) {
  const f = state.asStatsRange || (state.asStatsRange = (() => {
    const to = new Date();
    const from = new Date(to.getTime() - 29 * 864e5);
    const d = (x) => x.toISOString().slice(0, 10);
    return { from: d(from), to: d(to) };
  })());
  host.innerHTML = `
    <div class="card">
      <div class="inline-row">
        <input type="date" id="ast-from" value="${f.from}">
        <span class="muted">~</span>
        <input type="date" id="ast-to" value="${f.to}">
        <button class="btn btn-sm btn-primary" id="ast-go">조회</button>
        <span style="flex:1"></span>
        <button class="btn btn-sm" data-astq="30">최근 30일</button>
        <button class="btn btn-sm" data-astq="90">최근 90일</button>
        <button class="btn btn-sm" data-astq="365">최근 1년</button>
      </div>
    </div>
    <div id="ast-body"><div class="card placeholder"><p>불러오는 중…</p></div></div>`;
  const load = async () => {
    const body = $("#ast-body", host);
    try {
      await ensureAsMeta();          // 유형·상태 이름을 코드가 아니라 한글로 보여주기 위해
      const d = await api(`/api/as-stats?from=${f.from}&to=${f.to}`);
      if (!$("#ast-body", host)) return;
      body.innerHTML = asStatsHtml(d);
    } catch (e) {
      if (body) body.innerHTML = `<div class="card placeholder"><p>${escapeHtml(e.message || "조회 실패")}</p></div>`;
    }
  };
  $("#ast-go", host).addEventListener("click", () => {
    f.from = $("#ast-from", host).value; f.to = $("#ast-to", host).value; load();
  });
  $$("button[data-astq]", host).forEach((b) => b.addEventListener("click", () => {
    const n = Number(b.dataset.astq);
    const to = new Date(), from = new Date(to.getTime() - (n - 1) * 864e5);
    f.to = to.toISOString().slice(0, 10); f.from = from.toISOString().slice(0, 10);
    $("#ast-from", host).value = f.from; $("#ast-to", host).value = f.to;
    load();
  }));
  load();
}

/* ─────────── 🚦 진행 보드 ───────────
   칸(stage)은 서버(app/asvc AS_BOARD_STAGES)가 정한다 — 접수 상태 하나로는
   '택배가 왔는지'를 알 수 없어서, 서버가 회수·반송 송장 단계와 결제 도장까지 합쳐
   한 칸을 정해 내려 준다. 화면은 그 이름만 보고 그린다. */
const AS_STAGE_CHIP = {
  intake_wait: "chip-slate", collecting: "chip-blue", arrived: "chip-violet",
  repairing: "chip-violet", pay_wait: "chip-amber", ship_wait: "chip-blue",
  visit_wait: "chip-slate", returning: "chip-blue", closed: "chip-green",
};

function asParcelCell(r) {
  const bits = [];
  const rTip = `회수 송장 ${escapeHtml(r.recallWid)}${r.recallInvoiceNo
    ? ` · 송장번호 ${escapeHtml(r.recallInvoiceNo)}` : " · 송장번호는 기사가 집화할 때 붙습니다"}${
    r.recallStageAt ? `&#10;${escapeHtml(r.recallStageAt)}` : ""}`;
  if (r.recallWid) {
    // ★대표가 가장 먼저 물은 것 — "회수 신청한 택배가 배송완료됐는지"
    bits.push(r.recallDone
      ? `<span class="chip chip-green" title="${rTip}">📥 회수 배송완료</span>`
      : `<span class="chip chip-blue" title="${rTip}">🚚 회수 중${r.recallStage ? ` · ${escapeHtml(r.recallStage)}` : " · 집화 대기"}</span>`);
    // 회수 접수 확인(2026-09-07 대표 "회수 접수 시 정상 접수 확인") — CJ 가 받았는지·언제 수거인지
    if (!r.recallDone) {
      const ok = r.recallTest ? "🧪 시험 접수" : (r.recallBooked ? `✅ CJ 접수 ${escapeHtml(r.recallRcptDate || "")}` : "⚠ CJ 접수 미확인");
      bits.push(`<span class="muted" style="font-size:12px;">${ok}${
        r.recallScheduled ? ` · 수거 ${escapeHtml(r.recallScheduled)}` : ""}${
        r.recallInvoiceNo ? ` · ${escapeHtml(r.recallInvoiceNo)}` : ""}</span>`);
    }
  } else if (r.intake === "visit") {
    bits.push(`<span class="chip chip-slate" title="고객이 직접 가져온 건이라 회수가 필요 없습니다">🧍 방문 접수</span>`);
  } else if (r.stage === "intake_wait") {
    bits.push(`<span class="muted" style="font-size:12px;">회수 일정 미정</span>`);
  }
  if (r.returnWid) {
    const fTip = `출고 송장 ${escapeHtml(r.returnWid)}${r.returnInvoiceNo
      ? ` · ${escapeHtml(r.returnInvoiceNo)}` : ""}${
      r.returnStageAt ? `&#10;${escapeHtml(r.returnStageAt)}` : ""}`;
    bits.push(r.returnDone
      ? `<span class="chip chip-green" title="${fTip}">✅ 배송완료</span>`
      : `<span class="chip chip-blue" title="${fTip}">🚚 택배 출고${r.returnStage ? ` · ${escapeHtml(r.returnStage)}` : ""}</span>`);
  } else if ((r.returnMethod || "parcel") === "visit" && ["ship_wait", "visit_wait", "pay_wait"].includes(r.stage)) {
    bits.push(`<span class="chip chip-slate" title="고객이 직접 찾아가는 건이라 송장이 필요 없습니다">🧍 방문 수령</span>`);
  }
  return bits.length ? `<div class="as-parcel">${bits.join("")}</div>`
                     : `<span class="muted" style="font-size:12px;">-</span>`;
}

/* 청구·결제 칸 — 무상이면 돈 이야기를 아예 안 한다 */
function asMoneyCell(r) {
  if (r.chargeTo !== "customer") return `<span class="chip chip-slate">무상</span>`;
  const bill = r.billTotal || 0;
  if (!bill) return `<span class="muted" style="font-size:12px;">유상 · 금액 미입력</span>`;
  return `<b>${fmtWon(bill)}</b>` + (r.paidAt
    ? `<div><span class="chip chip-green" title="${escapeHtml(r.paidMethod || "")}">받음 ${escapeHtml(r.paidAt)}</span></div>`
    : `<div><span class="chip chip-amber">결제 전</span></div>`);
}

/* 회수 품목 한 줄(2026-09-08 대표) — 접수부터 발송까지 모든 칸에 보인다.
   ★수리 내역(asWorkLine)과 달리 접수·회수 중에도 보여야 한다: 무엇을 맡았는지가
   돌려줄 때까지 계속 필요하고, 안 적혀 있으면 그 자리에서 적으라고 알려야 한다. */
function asIntakeItemsLine(r) {
  if (r.stage === "closed") return "";
  if (r.intakeItems) {
    return `<div class="muted" style="font-size:12px;" title="고객에게서 함께 받은 물건 — 안내 문자에 그대로 나갑니다">📦 ${escapeHtml(r.intakeItems)}</div>`;
  }
  return `<div class="muted" style="font-size:12px; opacity:.7;" title="[열기]에서 적어 두면 입고·발송 안내 문자에 함께 나가 분실 시비를 막습니다">📦 회수 품목 미기재</div>`;
}

/* 수리 내역 한 줄(2026-09-07 대표 "수리중에서 수리 관련된 내역 상세") — 팝업을 안 열어도
   무엇을 고치고 있는지 보인다. 자세한 표·문서는 [열기]. */
function asWorkLine(r) {
  if (["intake_wait", "collecting"].includes(r.stage)) return "";
  const bits = [];
  if (r.itemsCount) {
    bits.push(`🧾 ${escapeHtml(r.itemsSummary || "")}${r.itemsCount > 4 ? ` 외 ${r.itemsCount - 4}` : ""}`);
  }
  if (r.result) bits.push(`✎ ${escapeHtml(r.result)}`);
  if (!bits.length) {
    return r.stage === "repairing"
      ? `<div class="muted" style="font-size:12px;">수리 내역 미입력 — [열기]에서 항목·금액을 적습니다</div>` : "";
  }
  return `<div class="muted as-workline" style="font-size:12px;" title="${escapeHtml([r.itemsSummary, r.result].filter(Boolean).join(" / "))}">${bits.join(" · ")}</div>`;
}

/* 한 줄에 '지금 이 건에 대해 할 다음 일'만 버튼으로 낸다.
   전부 이미 있는 창구를 그대로 부른다(상세 팝업의 버튼과 같은 API). */
function asBoardActions(r) {
  if (!hasPerm("as.manage")) return "";
  const b = (act, label, cls, title) =>
    `<button class="btn btn-sm ${cls || ""}" data-bact="${act}" data-bid="${r.id}"
       title="${escapeHtml(title)}">${label}</button> `;
  // 유상인데 못 받은 돈이 있으면 어느 칸에서든 결제 확인이 먼저다(2026-09-07 대표 "수리중에서
  // 결제완료 버튼이 당연히 있어야겠지") — 받아야 송장·방문 수령이 열린다.
  const pay = (r.chargeTo === "customer" && (r.unpaid || 0) > 0)
    ? b("pay", "💰 결제 확인", "btn-primary", `청구 ${fmtWon(r.unpaid)} — 입금을 받았다고 표시합니다. 받아야 송장·방문 수령이 열립니다.`) : "";
  const del = b("delete", "🗑 삭제", "btn-ghost", "잘못 넣었거나 고객이 무른 접수 — '취소'로 바뀌고 보드에서 빠집니다(기록은 남습니다).");
  // 이 칸에서 고객에게 보낼 안내 문자(2026-09-07 대표) — 누르면 문구를 확인·수정한 뒤 보낸다
  const sms = AS_STAGE_SMS[r.stage]
    ? b("sms", AS_STAGE_SMS[r.stage][1], "btn-ghost",
        `${escapeHtml(r.customer)} 고객에게 이 단계 안내 문자를 보냅니다 — 문구를 확인·수정한 뒤 나갑니다.`)
    : "";
  switch (r.stage) {
    case "intake_wait":
      return b("recall", "🚚 택배 접수", "btn-primary", "수거 희망일을 정해 CJ 기사가 고객 주소로 방문하도록 예약합니다.")
        + sms + del;
    case "collecting":
      return b("recallCheck", "🔄 CJ 확인", "", "CJ에 정상 접수됐는지·집화됐는지·어디까지 왔는지 지금 확인합니다.")
        + b("received", "📥 입고 처리", "", "물건이 도착했는데 추적이 아직이면 직접 입고 완료로 넘깁니다.")
        + sms
        + b("recallCancel", "🚫 회수 취소", "btn-ghost", "회수 예약을 무릅니다 — CJ 기사가 방문하지 않습니다.");
    case "arrived":
      return b("repairing", "🔧 수리 시작", "btn-primary", "물건이 우리 손에 있습니다 — 작업 중으로 표시합니다.")
        + sms + (!r.recallWid ? del : "");
    case "repairing":
      return b("done", "✅ 수리 완료", pay ? "" : "btn-primary", "작업을 마쳤습니다 — 결제·발송 단계로 넘깁니다.") + pay + sms;
    case "pay_wait":
      return (pay || b("open", "열기", "", "청구 금액을 적어야 결제 확인을 할 수 있습니다.")) + sms;
    case "ship_wait":
      return b("ship", "🧾 송장 출력", "btn-primary", "CJ 송장을 끊어 인쇄합니다 — 택배 출고로 넘어갑니다.") + sms;
    case "visit_wait":
      return b("handover", "🧍 인도 완료", "btn-primary", "고객이 직접 찾아갔습니다 — 종료합니다.") + sms;
    case "returning":
      return (r.returnDone ? b("close", "✅ 종료", "", "배송이 끝났습니다 — 접수 건을 종료합니다.") : "") + sms;
  }
  return "";
}

function asBoardRow(r, stageLabel) {
  const stageChip = r.cancelled
    ? `<span class="chip chip-red" title="접수 삭제 또는 취소 — 상세에서 상태를 '접수'로 바꾸면 되살아납니다">🗑 취소</span>`
    : `<span class="chip ${AS_STAGE_CHIP[r.stage] || "chip-slate"}">${escapeHtml(stageLabel)}</span>`;
  const when = r.stage === "closed" && r.closedAt
    ? `종료 ${escapeHtml((r.closedAt || "").replace("T", " ").slice(0, 10))}`
    : `${escapeHtml(r.receivedAt)} · ${r.ageDays}일째${r.late ? " ⏰" : ""}`;
  return `<tr${r.late ? ' class="as-late"' : ""}>
    <td>${stageChip}</td>
    <td><b>${escapeHtml(r.ticketNo)}</b>
      <div class="muted" style="font-size:12px;">${when}</div></td>
    <td>${escapeHtml(r.customer)}<div class="muted" style="font-size:12px;">${escapeHtml(r.phone)}</div></td>
    <td class="as-symcell">
      ${r.assetNo ? `<span class="chip chip-green">${escapeHtml(r.assetNo)}</span> ` : ""}${asRepeatChip(r)}
      <div class="muted" style="font-size:12px;" title="${escapeHtml(r.symptom)}">${escapeHtml([r.symptomCat, r.symptomSub].filter(Boolean).join(" · "))}${r.symptom ? (r.symptomCat || r.symptomSub ? " — " : "") + escapeHtml(r.symptom) : ""}</div>
      ${asIntakeItemsLine(r)}${asWorkLine(r)}</td>
    <td>${asParcelCell(r)}</td>
    <td>${asMoneyCell(r)}</td>
    <td class="muted">${escapeHtml(r.assignee)}</td>
    <td class="as-bact">${asBoardActions(r)}<button class="btn btn-sm" data-bopen="${r.id}">열기</button></td>
  </tr>`;
}

async function renderAsBoard(host) {
  const f = state.asBoard || (state.asBoard = { stage: "", q: "" });
  if (f.view === "all") f.view = "open";            // 옛 '끝난 건도 보기' 상태 흡수
  // ★종료 칸은 '쌓이는' 곳이다(2026-09-07 대표) — 그 칸을 누르면 끝난 건만, 검색과 함께
  const closedView = f.stage === "closed";
  let d;
  try {
    await ensureAsMeta();
    const p = new URLSearchParams({ view: closedView ? "closed" : "open" });
    if (closedView && f.q) p.set("q", f.q);
    d = await api("/api/as-board?" + p.toString());
  } catch (e) {
    host.innerHTML = `<div class="card placeholder"><p>${escapeHtml(e.message || "조회 실패")}</p></div>`;
    return;
  }
  if (!host.isConnected) return;                 // 그 사이 다른 탭으로 옮겼다
  const stageOf = Object.fromEntries(d.stages.map((s) => [s.code, s]));
  const shown = (f.stage && !closedView) ? d.rows.filter((r) => r.stage === f.stage) : d.rows;
  const cur = f.stage ? stageOf[f.stage] : null;
  host.innerHTML = `
    <div class="as-board">
      ${d.stages.map((s) => `
        <button class="as-bcard${f.stage === s.code ? " on" : ""}${s.count && AS_HOT_STAGES.includes(s.code) ? " hot" : ""}"
                data-bstage="${s.code}" title="${escapeHtml(s.hint)}&#10;&#10;누르면 이 단계만 봅니다 (다시 누르면 전체).">
          <span class="as-bcard-l">${escapeHtml(s.label)}</span>
          <span class="as-bcard-n">${s.count}</span>
        </button>`).join("")}
    </div>
    <div class="card">
      <div class="inline-row" style="flex-wrap:wrap;">
        <b>${cur ? escapeHtml(cur.label) : "진행 중 전체"}</b>
        <span class="muted">${shown.length}건</span>
        ${cur ? `<span class="muted" style="font-size:12.5px;">— ${escapeHtml(cur.hint)}</span>` : ""}
        <span style="flex:1"></span>
        ${!closedView && d.late ? `<span class="chip chip-red"
          title="접수한 지 ${d.dueDays}일이 지나도록 안 끝난 건입니다(기한은 설정에서 바꿉니다)">⏰ 지연 ${d.late}건</span>` : ""}
        <button class="btn btn-sm" id="asb-reload" title="택배 단계는 CJ 추적이 채웁니다 — 다시 읽습니다">↻ 새로고침</button>
      </div>
      ${closedView ? `<div class="inline-row" style="margin-top:8px;">
        <input type="text" id="asb-q" placeholder="접수번호/고객/연락처/관리번호/증상" value="${escapeHtml(f.q || "")}" style="min-width:260px;">
        <button class="btn btn-sm btn-primary" id="asb-go">찾기</button>
        <span class="muted" style="font-size:12px;">최근 300건 — 더 오래된 건은 검색으로 찾습니다. [열기]에서 수리 내역·문서·이력을 봅니다.</span>
      </div>` : ""}
      <div class="table-wrap"><table class="as-board-tb">
        <thead><tr><th>단계</th><th>접수번호</th><th>고객</th><th>제품 · 증상 · 수리 내역</th>
          <th>택배</th><th>청구 · 결제</th><th>담당</th><th>다음 할 일</th></tr></thead>
        <tbody>${shown.map((r) => asBoardRow(r, (stageOf[r.stage] || {}).label || r.stage)).join("")
          || `<tr><td colspan="8" class="muted">${closedView ? (f.q ? "검색 결과가 없습니다." : "끝난 건이 아직 없습니다.")
                                                     : (f.stage ? "이 단계에 해당하는 건이 없습니다." : "진행 중인 A/S가 없습니다.")}</td></tr>`}</tbody>
      </table></div>
    </div>`;
  $$("button[data-bstage]", host).forEach((b) => b.addEventListener("click", () => {
    f.stage = f.stage === b.dataset.bstage ? "" : b.dataset.bstage;
    renderAsBoard(host);
  }));
  $("#asb-reload", host).addEventListener("click", () => renderAsBoard(host));
  const qEl = $("#asb-q", host);
  if (qEl) {
    const go = () => { f.q = qEl.value.trim(); renderAsBoard(host); };
    $("#asb-go", host).addEventListener("click", go);
    qEl.addEventListener("keydown", (e) => { if (e.key === "Enter") go(); });
    qEl.focus();
  }
  $$("button[data-bopen]", host).forEach((b) => b.addEventListener("click", () => {
    state.asDetailId = Number(b.dataset.bopen);
    renderAsDetail();
  }));
  $$("button[data-bact]", host).forEach((btn) => btn.addEventListener("click", async () => {
    const id = Number(btn.dataset.bid);
    const row = d.rows.find((x) => x.id === id);
    const act = btn.dataset.bact;
    if (!row) return;
    const redraw = () => renderAsBoard(host);
    if (act === "open") { state.asDetailId = id; renderAsDetail(); return; }
    if (act === "pay") { openAsPayModal(row, redraw); return; }
    if (act === "recall") { openAsRecallModal(row, redraw); return; }
    if (act === "delete") { deleteAsTicket(row, redraw); return; }
    if (act === "recallCancel") { cancelAsRecall(row, redraw); return; }
    if (act === "sms") {
      const pair = AS_STAGE_SMS[row.stage];
      if (pair) openAsSmsModal(row, pair[0], pair[1].replace(/^💬\s*/, ""));
      return;
    }
    if (act === "received" && !confirm(
      `${row.customer} 고객의 회수 물건이 도착한 것으로 하고 '입고 완료'로 넘길까요?\n` +
      `(CJ 추적이 배달완료를 아직 못 봤을 때 씁니다)`)) return;
    if (act === "ship" && !confirm(
      `${row.customer} 고객에게 보낼 송장을 발급합니다.\n주소: ${row.address || "(비어 있음)"}\n\n계속할까요?`)) return;
    if (act === "handover" && !confirm(
      `${row.customer} 고객이 직접 찾아간 것으로 하고 이 건을 종료할까요?`)) return;
    btn.disabled = true;
    try {
      if (act === "recallCheck") {
        const rr = await api(`/api/as-tickets/${id}/recall-check`, { method: "POST", body: {} });
        toast(rr.simulated ? "시험 접수 건이라 CJ에 묻지 않았습니다."
          : rr.delivered ? "배송완료 — 입고 완료로 넘어갔습니다."
          : rr.invoiceNo ? `CJ 접수 정상 · 송장 ${rr.invoiceNo}${rr.stage ? ` · ${rr.stage}` : ""}`
          : rr.booked ? "CJ 접수 정상 — 아직 기사가 집화하지 않았습니다(번호는 집화 때 붙습니다)."
          : "CJ에서 접수를 확인하지 못했습니다 — 회수 예약을 다시 확인하세요.", !rr.booked);
      } else if (act === "received") {
        const rr = await api(`/api/waybills/${encodeURIComponent(row.recallWid)}/received`,
                             { method: "POST", body: { status: "as" } });
        toast(`입고 완료로 넘겼습니다.${rr.assets && rr.assets.length ? ` (자산 ${rr.assets.join(", ")})` : ""}`);
      } else if (act === "ship") {
        const rr = await api(`/api/as-tickets/${id}/return-waybill`, { method: "POST", body: {} });
        toast(rr.simulated ? `테스트 송장: ${rr.wid}` : `송장 발급 완료: ${rr.invoiceNo}`);
        window.open(`/api/waybills/${encodeURIComponent(rr.wid)}/pdf`, "_blank");
      } else {
        // repairing / done / (handover·close → returned) 는 전부 상태 바꾸기 하나다
        const to = (act === "handover" || act === "close") ? "returned" : act;
        await api(`/api/as-tickets/${id}`, { method: "PATCH", body: { status: to } });
        toast("상태를 바꿨습니다.");
      }
      redraw();
    } catch (err) { toast(err.message, true); btn.disabled = false; }
  }));
}

/* 🚚 택배 접수(2026-09-07 대표 "접수쪽에서 택배 접수가능하게") — 수거 희망일을 받아
   CJ 회수를 예약한다. 상세 팝업 위에서 부르면 2층으로, 보드에서 부르면 보통 팝업으로. */
function openAsRecallModal(t, onDone) {
  const panel = document.createElement("div");     // DOM 밖에서 만든다 → 닫을 때 통째로 사라진다
  panel.id = "asrecall-panel";
  panel.innerHTML = `
    <div class="card" style="max-width:480px;">
      <div class="inline-row"><h3 style="margin:0; flex:1;">🚚 택배 접수</h3>
        <button class="btn btn-sm" id="asrecall-close">✕</button></div>
      <p class="muted" style="font-size:12.5px; margin:6px 0 10px;">
        <b>${escapeHtml(t.customer)}</b> · ${escapeHtml(t.ticketNo)}<br>
        ${t.address ? escapeHtml(t.address) : '<span style="color:var(--danger);">주소가 비어 있습니다 — [열기]에서 먼저 입력하세요.</span>'}</p>
      <label>수거 희망일<input type="date" id="asrecall-date" disabled></label>
      <div id="asrecall-why" class="muted" style="font-size:12px; margin-top:6px;">가능한 날짜를 확인하는 중…</div>
      <div id="asrecall-off" style="margin-top:8px;"></div>
      <p class="muted" style="font-size:12px; margin:10px 0 0;">CJ 기사가 이 날 고객 주소로 방문합니다.
        회수 송장번호는 기사가 집화할 때 붙어 예약 직후엔 비어 있는 것이 정상이고,
        접수가 정상인지는 [🔄 CJ 확인]으로 봅니다. 예약하면 고객에게 회수 안내 문자가 나갑니다.</p>
      <div class="editor-actions">
        <button class="btn btn-primary" id="asrecall-go" disabled>택배 접수</button>
      </div>
    </div>`;
  const over = !!document.getElementById("modal-back");
  const shut = () => (over ? closeModalOver() : closeModal());
  if (over) openModalOver(panel); else openModalWith(panel);
  $("#asrecall-close").addEventListener("click", shut);

  // ★쉬는 날은 고르지 못하게 한다(2026-09-07 대표) — 기사가 안 오는 날로 예약하면
  //   고객은 하루를 헛기다리고 우리는 며칠 뒤에야 안다. 서버도 같은 규칙으로 막지만,
  //   고르는 그 자리에서 막아야 사람이 헤매지 않는다.
  const dateEl = $("#asrecall-date");
  const goEl = $("#asrecall-go");
  const whyEl = $("#asrecall-why");
  let cal = null;
  const check = () => {
    if (!cal) return;
    const hit = cal.days.find((d) => d.date === dateEl.value);
    const bad = hit ? hit.reason : (dateEl.value ? "" : "수거 희망일을 골라 주세요.");
    goEl.disabled = !!bad || !t.address;
    whyEl.innerHTML = bad
      ? `<span style="color:var(--danger);">${escapeHtml(bad)}</span>`
      : (hit ? `${escapeHtml(hit.date)} (${escapeHtml(hit.weekday)}) — 예약 가능합니다.` : "");
  };
  (async () => {
    try { cal = await api("/api/cj/pickup-calendar?days=45"); }
    catch (e) {
      // 달력을 못 읽어도 접수 자체를 막지는 않는다 — 서버가 마지막으로 검사한다
      whyEl.innerHTML = `<span class="muted">쉬는 날 확인을 못 했습니다(${escapeHtml(e.message)}) — 저장할 때 서버가 다시 봅니다.</span>`;
      dateEl.disabled = false;
      dateEl.value = ymd(new Date(Date.now() + 864e5));
      goEl.disabled = !t.address;
      return;
    }
    if (!panel.isConnected) return;
    dateEl.disabled = false;
    dateEl.min = cal.days.length ? cal.days[0].date : ymd();
    dateEl.max = cal.days.length ? cal.days[cal.days.length - 1].date : "";
    dateEl.value = cal.next;
    const off = cal.days.filter((d) => !d.ok).slice(0, 8);
    $("#asrecall-off", panel).innerHTML = off.length ? `
      <div class="muted" style="font-size:12px;">쉬는 날(예약 불가):
        ${off.map((d) => `<span class="chip chip-slate" style="font-size:11px;"
          title="${escapeHtml(d.reason)}">${escapeHtml(d.date.slice(5))} ${escapeHtml(d.weekday)}${
          d.holiday ? " " + escapeHtml(d.holiday) : ""}</span>`).join("")}
        ${cal.enabled ? "" : '<span class="chip chip-amber" style="font-size:11px;">휴무일 검사 꺼짐</span>'}
      </div>
      <div class="muted" style="font-size:11.5px; margin-top:4px;">
        설날·부처님오신날 같은 음력 공휴일은 해마다 날짜가 달라 자동으로 알지 못합니다 —
        설정 ▸ API 관리 ▸ CJ 대한통운 ▸ 집화 휴무일에 추가하세요.</div>` : "";
    check();
  })();
  dateEl.addEventListener("change", check);
  dateEl.addEventListener("input", check);

  goEl.addEventListener("click", async () => {
    goEl.disabled = true;
    try {
      const r = await api(`/api/as-tickets/${t.id}/recall`, { method: "POST",
        body: { pickupDate: dateEl.value } });
      toast(r.simulated ? `테스트 택배 접수: ${r.wid}` : `택배 접수 완료: ${r.wid}`);
      if (r.sms && r.sms.ok === false && r.sms.message) toast(r.sms.message, true);
      shut();
      if (onDone) onDone();
    } catch (err) { toast(err.message, true); goEl.disabled = false; }
  });
}

/* 🗑 접수 삭제(2026-09-07 대표 "접수 고객 삭제 기능") — 서버가 '취소'로 바꾸고 이력에 남긴다.
   접수번호는 되쓰지 않는다(이미 문자로 나갔을 수 있다). */
async function deleteAsTicket(t, onDone) {
  const reason = prompt(
    `${t.customer} 고객 ${t.ticketNo} 접수를 삭제합니다.\n` +
    `진행 보드에서 빠지고 '취소'로 남습니다(접수번호는 그대로, 되살릴 수 있습니다).\n\n사유(선택):`, "");
  if (reason === null) return;
  try {
    await api(`/api/as-tickets/${t.id}`, { method: "DELETE", body: { reason } });
    toast("접수를 삭제(취소)했습니다.");
    if (onDone) onDone();
  } catch (err) { toast(err.message, true); }
}

/* 💰 결제 확인 창(2026-09-03 대표 "진행완료되어 결제전인지").
   ★상세 팝업 위에서 부르면 2층으로, 보드에서 부르면 보통 팝업으로 뜬다 —
     연 층과 닫는 층이 어긋나면 창이 안 닫힌다(문자 미리보기에서 겪었던 문제). */
function openAsPayModal(t, onDone) {
  const panel = document.createElement("div");     // DOM 밖에서 만든다 → 닫을 때 통째로 사라진다
  panel.id = "aspay-panel";
  const bill = t.billTotal || 0;
  const inputCss = "width:100%; margin-top:4px; padding:8px 10px; border:1px solid var(--border); border-radius:8px; background:var(--bg);";
  panel.innerHTML = `
    <div class="card" style="max-width:420px;">
      <div class="inline-row"><h3 style="margin:0; flex:1;">💰 결제 확인</h3>
        <button class="btn btn-sm" id="aspay-close">✕</button></div>
      <p class="muted" style="font-size:12.5px; margin:6px 0 10px;">
        <b>${escapeHtml(t.customer)}</b> · ${escapeHtml(t.ticketNo)} — 청구 <b>${fmtWon(bill)}</b></p>
      <div class="form-grid">
        <label>받은 금액<input type="number" id="aspay-amount" value="${bill}" min="0" step="1000"></label>
        <label>받은 날<input type="date" id="aspay-date" value="${ymd()}"></label>
      </div>
      <label class="muted" style="display:block; margin-top:8px;">받은 방법
        <span style="font-weight:400;">(선택 — 계좌이체·카드·현금 등)</span>
        <input type="text" id="aspay-method" maxlength="20" placeholder="예: 계좌이체" style="${inputCss}"></label>
      <p class="muted" style="font-size:12px; margin:10px 0 0;">여기서는 <b>돈을 받았는지만</b> 기록합니다 —
        세금계산서·현금영수증 발행과는 별개입니다. 표시하면 현황의 [💰 결제 전] 칸에서 빠지고
        [📦 발송 전]으로 넘어갑니다.</p>
      <div class="editor-actions">
        <button class="btn btn-primary" id="aspay-go">받았음으로 표시</button>
      </div>
    </div>`;
  const over = !!document.getElementById("modal-back");
  const shut = () => (over ? closeModalOver() : closeModal());
  if (over) openModalOver(panel); else openModalWith(panel);
  $("#aspay-close").addEventListener("click", shut);
  $("#aspay-go").addEventListener("click", async () => {
    const go = $("#aspay-go");
    go.disabled = true;
    try {
      await api(`/api/as-tickets/${t.id}/payment`, { method: "POST", body: {
        paid: true, amount: Number($("#aspay-amount").value) || 0,
        date: $("#aspay-date").value, method: $("#aspay-method").value.trim(),
      } });
      toast("결제 확인을 기록했습니다.");
      shut();
      if (onDone) onDone();
    } catch (err) { toast(err.message, true); go.disabled = false; }
  });
}


function asBar(rows, total) {
  if (!rows || !rows.length) return `<p class="muted">해당 기간에 자료가 없습니다.</p>`;
  const max = Math.max(...rows.map((r) => r.count), 1);
  return `<div class="as-bars">${rows.map((r) => `
    <div class="as-bar-row">
      <span class="as-bar-k" title="${escapeHtml(r.key)}">${escapeHtml(r.key)}</span>
      <span class="as-bar-t"><i style="width:${Math.round(r.count * 100 / max)}%"></i></span>
      <span class="as-bar-n">${r.count}건${total ? ` · ${Math.round(r.count * 100 / total)}%` : ""}</span>
    </div>`).join("")}</div>`;
}

function asStatsHtml(d) {
  const won = (n) => fmtWon(n || 0);
  const card = (t, v, sub, color) => `
    <div class="kpi"><div class="kpi-label">${t}</div>
      <div class="kpi-value" ${color ? `style="color:${color}"` : ""}>${v}</div>
      ${sub ? `<div class="muted" style="font-size:12px;margin-top:2px;">${sub}</div>` : ""}</div>`;
  return `
    <div class="kpi-row">
      ${card("접수", `${d.tickets}건`, `종료 ${d.closed}건`, "var(--primary)")}
      ${card("유상 / 무상", `${d.paid} / ${d.free}`, `유상 비율 ${d.paidRate}%`, "#7c3aed")}
      ${card("A/S 수입", won(d.income), "유상 수리비 + 추가비용", "#0f766e")}
      ${card("나간 부품 원가", won(d.partCost), "재고에서 빠진 값", "#b45309")}
      ${card("A/S 남는 돈", won(d.profit), "수입 − 부품 원가",
             (d.profit || 0) < 0 ? "var(--danger)" : "#0f766e")}
      ${card("무상 부담", won(d.companyCost), "회사가 떠안은 수리비", "var(--slate)")}
    </div>
    <p class="muted" style="margin:-4px 0 12px;">
      ★A/S 수입은 판매 매출과 합치지 않고 나란히 봅니다. 나간 부품은 부품 재고에서 자동으로 빠집니다.</p>
    <div class="as-grid2">
      <div class="card"><h3>증상 대분류</h3>${asBar(d.byCat, d.tickets)}</div>
      <div class="card"><h3>증상 소분류</h3>${asBar(d.bySub, d.tickets)}</div>
    </div>
    <div class="as-grid2">
      <div class="card"><h3>처리 유형</h3>${asBar(
        (d.byType || []).map((r) => ({
          // 코드(repair/exchange…) 대신 사람이 읽는 이름으로 — 화면 다른 곳과 같은 말
          key: ((state.asMeta || {}).types || []).find((t) => t.code === r.key)?.label || r.key,
          count: r.count,
        })), d.tickets)}</div>
      <div class="card"><h3>처리 소요일</h3>
        ${d.leadCount ? `
          <p>평균 <b>${d.leadAvg}일</b> · 중간값 <b>${d.leadMid}일</b>
             <span class="muted">(끝난 ${d.leadCount}건 기준)</span></p>
          <table><thead><tr><th>가장 오래 걸린 건</th><th>고객</th><th>소요</th></tr></thead>
          <tbody>${d.slowest.map((x) => `<tr><td>${escapeHtml(x.ticketNo)}</td>
            <td>${escapeHtml(x.customer || "")}</td><td>${x.days}일</td></tr>`).join("")}</tbody></table>`
          : `<p class="muted">이 기간에 끝난 건이 없습니다.</p>`}
      </div>
    </div>
    <div class="card">
      <h3>⏰ 처리 지연 <span class="muted" style="font-weight:400;">
        — 접수 후 ${d.dueDays}일이 지났는데 아직 안 끝난 건(기간과 무관한 지금 현황)</span></h3>
      ${(d.late || []).length ? `<table>
        <thead><tr><th>접수번호</th><th>고객</th><th>상태</th><th>경과</th></tr></thead>
        <tbody>${d.late.map((x) => `<tr>
          <td>${escapeHtml(x.ticketNo)}</td><td>${escapeHtml(x.customer || "")}</td>
          <td>${escapeHtml(((state.asMeta || {}).statuses || []).find((s) => s.code === x.status)?.label || x.status)}</td>
          <td><b style="color:var(--danger)">${x.days}일</b></td></tr>`).join("")}</tbody></table>`
        : `<p class="muted">지연된 건이 없습니다.</p>`}
    </div>`;
}

function renderAsSettingsTab(main) {
  if (state.asTab === "sms") {              // 옛 상태 호환(오늘 오전 배포분)
    state.asTab = "settings";
    state.asSetTab = "sms";
  }
  const sub = state.asSetTab || (state.asSetTab = "cats");
  main.innerHTML = `
    <h1 class="page-title">A/S</h1>
    <p class="page-desc">A/S 설정 — 증상 분류·회수 구성품·문자 양식을 관리합니다.</p>
    ${asTabsHtml("settings")}
    <div class="tabs">
      <button data-aset="cats" class="${sub === "cats" ? "active" : ""}">🏷 증상 분류</button>
      <button data-aset="items" class="${sub === "items" ? "active" : ""}">📦 회수 구성품</button>
      <button data-aset="sms" class="${sub === "sms" ? "active" : ""}">💬 문자 양식</button>
    </div>
    <div id="aset-body"><p class="muted">불러오는 중…</p></div>`;
  wireAsTabs(main);
  $$("button[data-aset]", main).forEach((b) => b.addEventListener("click", () => {
    state.asSetTab = ["sms", "items"].includes(b.dataset.aset) ? b.dataset.aset : "cats";
    renderAsSettingsTab(main);
  }));
  const body = $("#aset-body");
  if (sub === "sms") renderAsSmsTab(body);
  else if (sub === "items") renderAsItemsTab(body);
  else renderAsCatsTab(body);
}

/* ⚙ 설정 ▸ 📦 회수 구성품(2026-09-08 대표 "적어두고 저장하면 클릭하는 형태로… 두고두고 쓸 수
   있잖아") — 여기에 한 번 적어 두면 접수·상세의 회수 품목 칸에 [＋ 이름] 버튼으로 뜬다.
   접수 화면의 [＋ 새 구성품]도 결국 이 목록에 넣는다(같은 저장소). */
async function renderAsItemsTab(host) {
  let m;
  try { m = await api("/api/as-meta"); }               // 캐시 말고 최신 목록
  catch (err) { host.innerHTML = `<div class="card"><p class="muted">${escapeHtml(err.message)}</p></div>`; return; }
  state.asMeta = m;
  const items = m.intakeItemPresets || [];
  host.innerHTML = `
    <div class="card" style="max-width:560px;">
      <h3 style="margin-top:0;">📦 회수 구성품</h3>
      <p class="muted" style="font-size:12.5px; margin:4px 0 10px;">
        고객에게서 함께 받는 물건 목록입니다. <b>한 줄에 하나씩</b> — 적어 두면 A/S 접수·상세의
        [📦 회수 품목] 칸에 <b>[＋ 이름] 버튼</b>으로 떠서 클릭만 하면 담깁니다. 이 순서 그대로 보입니다.<br>
        지워도 <b>이미 접수된 건에 적힌 품목은 그대로</b> 남습니다. 이름에 쉼표는 쓸 수 없습니다(품목 구분자).</p>
      <textarea id="asitem-list" rows="12" style="width:100%;"
        placeholder="본체&#10;충전기&#10;키스킨&#10;가방">${escapeHtml(items.join("\n"))}</textarea>
      <div class="inline-row" style="flex-wrap:wrap; gap:4px; margin-top:8px;">
        <span class="muted" style="font-size:11.5px;">지금 모습</span>
        <span id="asitem-preview" class="inline-row" style="flex-wrap:wrap; gap:4px;"></span>
      </div>
      <div class="editor-actions"><button class="btn btn-primary" id="asitem-save">저장</button></div>
    </div>`;
  const lines = () => $("#asitem-list").value.split("\n").map((s) => s.trim()).filter(Boolean);
  const preview = () => {
    const seen = [];
    lines().forEach((x) => { if (!seen.includes(x)) seen.push(x); });
    $("#asitem-preview").innerHTML = seen.length
      ? seen.map((x) => `<span class="btn btn-ghost btn-sm">＋ ${escapeHtml(x)}</span>`).join("")
      : `<span class="muted" style="font-size:12px;">비어 있습니다 — 접수 화면에서는 직접 입력만 됩니다.</span>`;
  };
  $("#asitem-list").addEventListener("input", preview);
  preview();
  $("#asitem-save").addEventListener("click", async () => {
    try {
      const r = await api("/api/as-intake-presets", { method: "PUT", body: { items: lines() } });
      state.asMeta.intakeItemPresets = r.items;
      $("#asitem-list").value = r.items.join("\n");
      preview();
      toast(`회수 구성품 ${r.items.length}개를 저장했습니다.`);
    } catch (err) { toast(err.message, true); }
  });
}

/* ⚙ 설정 ▸ 🏷 증상 분류 — 대분류(장비 종류)/소분류(성격)를 줄 단위로 관리.
   지워도 이미 접수된 건의 분류 기록은 남는다. */
async function renderAsCatsTab(host) {
  let m;
  try { m = await api("/api/as-meta"); }               // 캐시 말고 최신 목록
  catch (err) { host.innerHTML = `<div class="card"><p class="muted">${escapeHtml(err.message)}</p></div>`; return; }
  state.asMeta = m;
  host.innerHTML = `
    <div class="card" style="max-width:720px;">
      <h3 style="margin-top:0;">🏷 증상 분류</h3>
      <p class="muted" style="font-size:12.5px; margin:4px 0 10px;">
        접수·상세의 분류 선택지가 이 목록으로 뜹니다. 한 줄에 하나씩 — 이 순서 그대로 보입니다.
        항목을 지워도 이미 접수된 건의 분류 기록은 남습니다. 사유는 목록 없이 증상 칸에 자유 서술합니다.</p>
      <div class="form-grid">
        <label>대분류 <span class="muted" style="font-weight:400;">(장비 종류 — 데스크탑·노트북·태블릿…)</span>
          <textarea id="ascat-cats" rows="8" style="width:100%; margin-top:4px;">${escapeHtml((m.symptomCats || []).join("\n"))}</textarea></label>
        <label>소분류 <span class="muted" style="font-weight:400;">(성격 — H/W·S/W·OS·택배파손…)</span>
          <textarea id="ascat-subs" rows="8" style="width:100%; margin-top:4px;">${escapeHtml((m.symptomSubs || []).join("\n"))}</textarea></label>
      </div>
      <div class="editor-actions"><button class="btn btn-primary" id="ascat-save">저장</button></div>
    </div>`;
  $("#ascat-save").addEventListener("click", async () => {
    try {
      const lines = (id) => $(id).value.split("\n").map((s) => s.trim()).filter(Boolean);
      const r = await api("/api/as-symptom-cats", { method: "PUT",
        body: { cats: lines("#ascat-cats"), subs: lines("#ascat-subs") } });
      state.asMeta.symptomCats = r.cats;
      state.asMeta.symptomSubs = r.subs;
      toast("증상 분류를 저장했습니다.");
    } catch (err) { toast(err.message, true); }
  });
}

/* [💬 문자 양식](2026-08-31 대표) — 기본 4종 문구·자동 on/off + 직접 만든 양식.
   직접 만든 양식은 발송 시점(어떤 사건 때 자동으로 나갈지)을 고르거나 '수동 전용'으로 둔다.
   저장은 /api/as-sms-templates — 설정 키의 API 키 등은 서버가 병합으로 보존한다.
   ⚙ 설정 탭의 세부탭 몸통으로 그린다(main = #aset-body). */
async function renderAsSmsTab(main) {
  main.innerHTML = `
    <p class="muted" style="margin:2px 0 10px;">문자 양식 — 기본 문구는 그대로 두고 필요한 부분만 고치세요.
      저장하면 자동·수동 발송 모두 이 문구로 나갑니다.</p>
    <div id="assmst-body"><p class="muted">불러오는 중…</p></div>`;
  const body = $("#assmst-body");
  let d;
  try { d = await api("/api/as-sms-templates"); }
  catch (err) {
    body.innerHTML = `<div class="card"><p class="muted">${escapeHtml(err.message)}</p></div>`;
    return;
  }
  state.asSmsNames = Object.fromEntries(
    (d.custom || []).map((c) => ["custom:" + c.id, c.label]));
  let custom = (d.custom || []).map((c) => Object.assign({}, c));   // 편집용 사본

  const trigOptions = (sel) => (d.triggers || []).map((x) =>
    `<option value="${escapeHtml(x.code)}" ${x.code === sel ? "selected" : ""}>${escapeHtml(x.label)}</option>`).join("");
  const evCard = (e) => `
    <div class="card" data-ev="${e.code}">
      <div class="inline-row">
        <b>${escapeHtml(e.label)}</b>
        <span class="muted" style="font-size:12px;">${escapeHtml(e.when)}</span>
        <span style="flex:1"></span>
        <label class="muted" style="font-size:12px;"><input type="checkbox" class="ev-on" ${e.on ? "checked" : ""}> 자동 발송</label>
      </div>
      <input type="text" class="ev-subject" value="${escapeHtml(e.subject || "")}" maxlength="40"
             placeholder="제목 — 장문(LMS)일 때만 쓰입니다. 비우면 기본 제목" style="width:100%; margin-top:6px;">
      <textarea class="ev-text" rows="3" style="width:100%; margin-top:6px;">${escapeHtml(e.text)}</textarea>
      <div class="inline-row" style="margin-top:4px;">
        <span class="muted ev-len" style="font-size:12px;"></span><span style="flex:1"></span>
        <button class="btn btn-ghost btn-sm ev-reset" data-default="${escapeHtml(e.default)}">기본 문구로</button>
      </div>
    </div>`;
  const cuRow = (c, i) => `
    <div class="card" data-cu="${i}">
      <div class="inline-row" style="flex-wrap:wrap;">
        <input type="text" class="cu-label" value="${escapeHtml(c.label)}" maxlength="30"
               placeholder="양식 이름 (예: 입금 안내)" style="width:180px;">
        <select class="cu-trigger">${trigOptions(c.trigger)}</select>
        <label class="muted" style="font-size:12px;"><input type="checkbox" class="cu-on" ${c.on ? "checked" : ""}> 자동 발송</label>
        <span style="flex:1"></span>
        <button class="btn btn-ghost btn-sm cu-del">🗑 삭제</button>
      </div>
      <input type="text" class="cu-subject" value="${escapeHtml(c.subject || "")}" maxlength="40"
             placeholder="제목 — 장문(LMS)일 때만" style="width:100%; margin-top:6px;">
      <textarea class="cu-text" rows="3" style="width:100%; margin-top:6px;"
        placeholder="문구 — {고객명} {수리비} {비용안내} 같은 자리를 쓸 수 있습니다">${escapeHtml(c.text)}</textarea>
      <div class="muted cu-len" style="font-size:12px; margin-top:4px;"></div>
    </div>`;

  const collect = () => {
    $$(".card[data-cu]", body).forEach((card) => {
      const i = Number(card.dataset.cu);
      if (!custom[i]) return;
      custom[i] = Object.assign({}, custom[i], {
        label: $(".cu-label", card).value.trim(),
        trigger: $(".cu-trigger", card).value,
        subject: $(".cu-subject", card).value.trim(),
        text: $(".cu-text", card).value.trim(),
        on: $(".cu-on", card).checked,
      });
    });
  };

  const save = async () => {
    collect();
    const events = {};
    $$(".card[data-ev]", body).forEach((card) => {
      events[card.dataset.ev] = {
        on: $(".ev-on", card).checked,
        subject: $(".ev-subject", card).value.trim(),
        text: $(".ev-text", card).value.trim(),
      };
    });
    try {
      await api("/api/as-sms-templates", { method: "POST", body: { events, custom } });
      toast("문자 양식을 저장했습니다.");
      renderAsSmsTab(main);            // 새로 만든 양식의 id까지 다시 읽는다
    } catch (err) { toast(err.message, true); }
  };

  const paint = () => {
    body.innerHTML = `
      ${d.armed ? (d.live
        ? `<div class="card" style="border-left:4px solid var(--green, #16a34a);">📡 실발송 켜짐 — 저장된 문구 그대로 고객에게 나갑니다.</div>`
        : `<div class="card" style="border-left:4px solid var(--amber, #d97706);">⚠ 실발송이 설정돼 있지만 지금은 나가지 않습니다 — ${escapeHtml(d.reason)}</div>`)
        : `<div class="card muted">지금은 시뮬레이션 모드입니다(기록만 남고 실제 발송 없음) — 실발송은 설정 ▸ API 관리의 문자 설정에서 켭니다.</div>`}
      <div class="card"><b class="muted" style="font-size:13px;">문구에 쓸 수 있는 자리(매크로)</b>
        <div class="inline-row" style="flex-wrap:wrap; gap:6px; margin-top:6px;">
          ${(d.vars || []).map((v) => `<span class="chip chip-slate">{${escapeHtml(v)}}</span>`).join("")}
        </div>
        <p class="muted" style="font-size:12px; margin:6px 0 0;">보낼 때 그 접수 건의 실제 값으로
          바뀝니다. 90바이트(한글 45자)를 넘으면 장문(LMS)으로 나가 요금이 3배가량 듭니다.</p></div>
      <h3 style="margin:14px 0 6px;">기본 양식
        <span class="muted" style="font-size:13px; font-weight:400;">— 발송 시점이 정해져 있습니다</span></h3>
      ${d.events.map(evCard).join("")}
      <h3 style="margin:14px 0 6px;">직접 만든 양식
        <span class="muted" style="font-size:13px; font-weight:400;">— A/S 비용·현금영수증·입금 안내 등.
        발송 시점을 고르면 그 사건 때 자동으로, '수동 전용'이면 상세의 [📑 기타 양식…]으로 보냅니다</span></h3>
      <div id="cu-list">${custom.map(cuRow).join("")
        || `<p class="muted">아직 없습니다 — 아래 [＋ 양식 추가]로 만드세요.</p>`}</div>
      <div class="editor-actions">
        <button class="btn" id="cu-add">＋ 양식 추가</button>
        <button class="btn btn-primary" id="assmst-save">저장</button>
      </div>`;
    const wireLen = (ta, out) => {
      const upd = () => {
        const n = smsBytes(ta.value);
        out.textContent = `${n}바이트 · ${n > 90 ? "LMS(장문)" : "SMS(단문)"}`;
      };
      ta.addEventListener("input", upd);
      upd();
    };
    $$(".card[data-ev]", body).forEach((card) => wireLen($(".ev-text", card), $(".ev-len", card)));
    $$(".card[data-cu]", body).forEach((card) => wireLen($(".cu-text", card), $(".cu-len", card)));
    $$(".ev-reset", body).forEach((b) => b.addEventListener("click", () => {
      const card = b.closest(".card");
      $(".ev-text", card).value = b.dataset.default;
      $(".ev-text", card).dispatchEvent(new Event("input"));
    }));
    $$(".cu-del", body).forEach((b) => b.addEventListener("click", () => {
      const i = Number(b.closest(".card").dataset.cu);
      if (!confirm(`'${custom[i].label || "(이름 없음)"}' 양식을 지울까요?\n[저장]을 눌러야 반영됩니다.`)) return;
      collect();
      custom.splice(i, 1);
      paint();
    }));
    $("#cu-add").addEventListener("click", () => {
      collect();
      custom.push({ id: "", label: "", trigger: "", subject: "", text: "", on: true });
      paint();
    });
    $("#assmst-save").addEventListener("click", save);
  };
  paint();
}

/* ─────────── ⚙ 설정 ▸ API 관리 ▸ CJ ▸ 집화 휴무일(2026-09-07 대표) ───────────
   "택배 쉬는날이나 휴무인 경우 그 날에는 예약접수가 불가능하게" — 규칙은 서버
   app/cj/calendar.py 가 갖고, 이 화면은 그 규칙을 눈으로 확인하고 고치는 자리다.
   ★날짜가 고정된 공휴일(신정·삼일절·어린이날·현충일·광복절·개천절·한글날·성탄절)은
     코드가 이미 알아서 목록에 안 적어도 막힌다. 여기 적는 것은 음력·대체공휴일처럼
     해마다 달라지는 날이다. (app.js renderApiSettings 가 부른다) */
async function renderCjPickupOff(host, cj) {
  if (!host) return;
  const p = (cj && cj.pickup) || {};
  const off = Array.isArray(p.offWeekdays) ? p.offWeekdays.map(Number) : [6];
  const holidays = (p.holidays && typeof p.holidays === "object") ? { ...p.holidays } : null;
  let cal = null;
  try { cal = await api("/api/cj/pickup-calendar?days=45"); } catch (_e) { /* 저장 전에도 그린다 */ }
  const days = holidays || (cal ? cal.holidays : {}) || {};
  const enabled = p.enabled !== false;
  const paint = () => {
    const keys = Object.keys(days).sort();
    host.innerHTML = `
      <label class="check-line"><input type="checkbox" id="cjp-on" ${enabled ? "checked" : ""}>
        <span>쉬는 날에는 회수 예약을 막는다 <span class="muted">(끄면 아무 날짜나 예약됩니다 — 임시로만)</span></span></label>
      <div class="inline-row" style="flex-wrap:wrap; gap:10px; margin-top:8px;">
        <span class="muted" style="font-size:12.5px;">쉬는 요일:</span>
        ${["월", "화", "수", "목", "금", "토", "일"].map((lb, i) => `
          <label class="check-line" style="margin:0;"><input type="checkbox" data-cjwd="${i}"
            ${off.includes(i) ? "checked" : ""}> <span>${lb}</span></label>`).join("")}
        <span class="muted" style="font-size:12px;">— CJ는 일요일 집화를 하지 않습니다. 토요일은 계약·지역에 따라 다릅니다.</span>
      </div>
      <div style="margin-top:10px;">
        <span class="muted" style="font-size:12.5px;">공휴일(음력·대체공휴일만 적습니다 — 날짜가 고정된 공휴일은 자동으로 막힙니다)</span>
        <div class="inline-row" style="margin-top:4px;">
          <input type="date" id="cjp-date">
          <input type="text" id="cjp-name" placeholder="이름 (예: 설날)" style="min-width:150px;">
          <button class="btn btn-sm" type="button" id="cjp-add">＋ 추가</button>
        </div>
        <div class="inline-row" style="flex-wrap:wrap; gap:6px; margin-top:8px;">
          ${keys.length ? keys.map((d) => `<span class="chip chip-slate">${escapeHtml(d)} ${escapeHtml(days[d])}
            <button class="btn btn-ghost btn-sm" type="button" data-cjpdel="${escapeHtml(d)}"
              style="padding:0 4px; margin-left:2px;" title="목록에서 지웁니다">✕</button></span>`).join("")
            : `<span class="muted" style="font-size:12px;">등록된 날이 없습니다.</span>`}
        </div>
      </div>
      <p class="muted" style="font-size:12px; margin:10px 0 0;">위 [저장]을 눌러야 반영됩니다.
        ${cal && cal.next ? `지금 기준 가장 이른 예약 가능일은 <b>${escapeHtml(cal.next)}</b>입니다.` : ""}</p>`;
    $("#cjp-add", host).addEventListener("click", () => {
      const d = $("#cjp-date", host).value;
      if (!d) { toast("날짜를 골라 주세요.", true); return; }
      days[d] = ($("#cjp-name", host).value.trim() || "공휴일").slice(0, 20);
      paint();
    });
    $$("button[data-cjpdel]", host).forEach((b) => b.addEventListener("click", () => {
      delete days[b.dataset.cjpdel];
      paint();
    }));
  };
  paint();
  // 저장 버튼이 읽어 갈 값 — 화면 상태를 그대로 돌려준다(app.js 의 CJ 저장에서 부른다)
  host._value = () => ({
    enabled: $("#cjp-on", host) ? $("#cjp-on", host).checked : true,
    offWeekdays: $$("input[data-cjwd]", host).filter((c) => c.checked).map((c) => Number(c.dataset.cjwd)),
    holidays: days,
  });
}
