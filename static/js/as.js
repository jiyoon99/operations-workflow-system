/* HMS A/S — 접수 → 회수 → 수리 → 반송. 자산번호와 연결해 이력이 자산에도 남는다. */
"use strict";

const AS_CHIP = {
  received: "chip-blue", collecting: "chip-blue", repairing: "chip-violet",
  done: "chip-green", returned: "chip-slate", cancelled: "chip-red",
};

/* A/S 상세에서 손으로 보낼 수 있는 안내 문자 */
const AS_SMS_EVENTS = [
  ["received", "접수 완료"], ["collecting", "회수 예약"],
  ["done", "수리 완료"], ["returned", "발송 안내"],
];

/* 문자 종류 코드를 사람 말로 — 재발송은 received#2 처럼 뒤에 회차가 붙는다.
   예전엔 목록에 'received#2' 같은 코드가 그대로 찍혀 무슨 문자인지 알 수 없었다. */
function smsEventLabel(ev) {
  const [code, nth] = String(ev || "").split("#");
  const hit = AS_SMS_EVENTS.find((e) => e[0] === code);
  const name = hit ? hit[1] : (code === "test" ? "시험 발송" : code);
  return name + (nth ? ` (재발송 ${nth}회차)` : "");
}

async function ensureAsMeta() {
  if (!state.asMeta) state.asMeta = await api("/api/as-meta");
  return state.asMeta;
}

function asLabel(list, code) {
  const f = (state.asMeta && state.asMeta[list] || []).find((x) => x.code === code);
  return f ? f.label : code;
}

function renderAsView(main) {
  if (!hasPerm("as.view") && !hasPerm("as.manage")) {
    main.innerHTML = `<h1 class="page-title">A/S</h1><div class="card placeholder"><p>A/S 접근 권한이 없습니다.</p></div>`;
    return;
  }
  const f = state.asFilter || (state.asFilter = { view: "open", q: "" });
  const canEdit = hasPerm("as.manage");
  main.innerHTML = `
    <h1 class="page-title">A/S</h1>
    <p class="page-desc">접수 → 회수 → 수리 → 반송. 자산번호를 연결하면 그 제품 이력에도 기록됩니다.</p>
    <div class="kpi-row" id="as-kpi"></div>
    <div class="card">
      <div class="inline-row">
        <select id="asf-view">
          <option value="open" ${f.view === "open" ? "selected" : ""}>진행 중</option>
          <option value="closed" ${f.view === "closed" ? "selected" : ""}>종료</option>
          <option value="all" ${f.view === "all" ? "selected" : ""}>전체</option>
        </select>
        <input type="text" id="asf-q" placeholder="접수번호/고객/연락처/관리번호/증상" value="${escapeHtml(f.q)}" style="min-width:240px;">
        <button class="btn btn-sm btn-primary" id="asf-search">조회</button>
        <span style="flex:1"></span>
        ${canEdit ? `<button class="btn btn-sm btn-primary" id="asf-new">＋ A/S 접수</button>` : ""}
      </div>
      <div id="as-form"></div>
      <div class="table-wrap"><table>
        <thead><tr><th>접수번호</th><th>고객</th><th>관리번호 / 모델</th><th>유형</th><th>증상</th><th>상태</th><th>비용</th><th>담당</th><th></th></tr></thead>
        <tbody id="as-rows"><tr><td colspan="9" class="muted">불러오는 중…</td></tr></tbody>
      </table></div>
    </div>
    <div id="as-detail"></div>`;

  const load = async () => {
    const seq = state.renderSeq;
    try {
      await ensureAsMeta();
      const p = new URLSearchParams({ view: f.view });
      if (f.q) p.set("q", f.q);
      const rows = await api("/api/as-tickets?" + p.toString());
      if (seq !== state.renderSeq || !$("#as-rows")) return;
      state.asTickets = rows;
      $("#as-rows").innerHTML = rows.map((t) => `
        <tr>
          <td><b>${escapeHtml(t.ticketNo)}</b><div class="muted" style="font-size:12px;">${escapeHtml(t.receivedAt)}</div></td>
          <td>${escapeHtml(t.customer)}<div class="muted" style="font-size:12px;">${escapeHtml(t.phone)}</div></td>
          <td>${t.assetNo ? `<span class="chip chip-green">${escapeHtml(t.assetNo)}</span>` : '<span class="muted">미연결</span>'}
            <div class="muted" style="font-size:12px;">${escapeHtml(t.model || "")}</div></td>
          <td>${escapeHtml(t.asTypeLabel)}</td>
          <td style="max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${escapeHtml(t.symptom)}">${escapeHtml(t.symptom)}</td>
          <td><span class="chip ${AS_CHIP[t.status] || "chip-slate"}">${escapeHtml(t.statusLabel)}</span></td>
          <td>${t.cost ? fmtWon(t.cost) + `<div class="muted" style="font-size:12px;">${escapeHtml(t.chargeLabel)}</div>` : "-"}</td>
          <td class="muted">${escapeHtml(t.assignee)}</td>
          <td><button class="btn btn-sm" data-as="${t.id}">열기</button></td>
        </tr>`).join("") || `<tr><td colspan="9" class="muted">A/S 건이 없습니다.</td></tr>`;
      $$("button[data-as]").forEach((b) => b.addEventListener("click", () => {
        state.asDetailId = Number(b.dataset.as);
        renderAsDetail();
      }));
      const cnt = (s) => rows.filter((t) => t.status === s).length;
      $("#as-kpi").innerHTML = `
        <div class="kpi"><div class="kpi-label">접수</div><div class="kpi-value">${cnt("received")}</div></div>
        <div class="kpi"><div class="kpi-label">회수 중</div><div class="kpi-value">${cnt("collecting")}</div></div>
        <div class="kpi"><div class="kpi-label">수리 중</div><div class="kpi-value">${cnt("repairing")}</div></div>
        <div class="kpi"><div class="kpi-label">수리 완료(반송 대기)</div><div class="kpi-value">${cnt("done")}</div></div>`;
    } catch (err) {
      const el = $("#as-rows");
      if (el) el.innerHTML = `<tr><td colspan="9" class="muted">${escapeHtml(err.message)}</td></tr>`;
    }
  };
  const doSearch = () => { f.view = $("#asf-view").value; f.q = $("#asf-q").value.trim(); load(); };
  $("#asf-search").addEventListener("click", doSearch);
  $("#asf-view").addEventListener("change", doSearch);
  autoSearch("#asf-q", doSearch);
  const newBtn = $("#asf-new");
  if (newBtn) newBtn.addEventListener("click", renderAsForm);
  load();
  if (state.asDetailId) renderAsDetail();
}

function renderAsForm() {
  const host = $("#as-form");
  const meta = state.asMeta || { types: [], charges: [] };
  let picked = null;
  host.innerHTML = `
    <div style="border:1px dashed var(--border); border-radius:8px; padding:14px; margin:8px 0;">
      <b>A/S 접수</b>
      <div class="inline-row" style="margin-top:8px;">
        <input type="text" id="asn-assetq" placeholder="관리번호/시리얼/모델로 제품 찾기(2자 이상)" style="min-width:280px;">
        <button class="btn btn-sm" id="asn-search">제품 찾기</button>
        <span id="asn-picked" class="muted">제품 미연결</span>
      </div>
      <div id="asn-results"></div>
      <div class="form-grid" style="margin-top:8px;">
        <label>고객명 *<input type="text" id="asn-customer"></label>
        <label>연락처<input type="text" id="asn-phone"></label>
        <label>유형<select id="asn-type">${meta.types.map((t) => `<option value="${t.code}">${escapeHtml(t.label)}</option>`).join("")}</select></label>
        <label>비용 부담<select id="asn-charge">${meta.charges.map((c) => `<option value="${c.code}">${escapeHtml(c.label)}</option>`).join("")}</select></label>
        <label>접수일<input type="date" id="asn-date" value="${ymd()}"></label>
        <label>담당자<input type="text" id="asn-assignee" value="${escapeHtml(state.user.displayName)}"></label>
      </div>
      <label class="muted" style="display:block; margin-top:8px;">주소
        <input type="text" id="asn-address" style="width:100%; margin-top:4px; padding:8px 10px; border:1px solid var(--border); border-radius:8px; background:var(--bg);"></label>
      <label class="muted" style="display:block; margin-top:8px;">증상 *
        <input type="text" id="asn-symptom" placeholder="예: 전원이 켜지지 않음" style="width:100%; margin-top:4px; padding:8px 10px; border:1px solid var(--border); border-radius:8px; background:var(--bg);"></label>
      <div class="editor-actions">
        <button class="btn btn-primary" id="asn-save">접수</button>
        <button class="btn" id="asn-close">닫기</button>
      </div>
    </div>`;
  $("#asn-close").addEventListener("click", () => { host.innerHTML = ""; });
  const search = async () => {
    const q = $("#asn-assetq").value.trim();
    if (q.length < 2) { toast("2자 이상 입력하세요.", true); return; }
    try {
      const rows = await api("/api/as-tickets/asset-search?q=" + encodeURIComponent(q));
      $("#asn-results").innerHTML = rows.length ? `<div class="table-wrap"><table>
        <thead><tr><th>관리번호</th><th>모델</th><th>상태</th><th>구매 고객</th><th></th></tr></thead>
        <tbody>${rows.map((r) => `<tr>
          <td><b>${escapeHtml(r.assetNo)}</b></td>
          <td>${escapeHtml([r.maker, r.model].filter(Boolean).join(" "))}</td>
          <td>${escapeHtml(r.statusLabel || r.status)}</td>
          <td>${escapeHtml(r.recipient || "-")}</td>
          <td><button class="btn btn-sm" data-pick="${r.assetId}" data-no="${escapeHtml(r.assetNo)}"
            data-recipient="${escapeHtml(r.recipient || "")}" data-order="${r.orderId || ""}">선택</button></td>
        </tr>`).join("")}</tbody></table></div>`
        : `<p class="muted">검색 결과가 없습니다.</p>`;
      $$("button[data-pick]", host).forEach((b) => b.addEventListener("click", () => {
        picked = { assetId: Number(b.dataset.pick), assetNo: b.dataset.no, orderId: b.dataset.order || null };
        $("#asn-picked").innerHTML = `연결됨: <span class="chip chip-green">${escapeHtml(b.dataset.no)}</span>`;
        if (b.dataset.recipient && !$("#asn-customer").value) $("#asn-customer").value = b.dataset.recipient;
        $("#asn-results").innerHTML = "";
      }));
    } catch (err) { toast(err.message, true); }
  };
  $("#asn-search").addEventListener("click", search);
  $("#asn-assetq").addEventListener("keydown", (e) => { if (e.key === "Enter") search(); });
  $("#asn-save").addEventListener("click", async () => {
    try {
      const res = await api("/api/as-tickets", { method: "POST", body: {
        customer: $("#asn-customer").value.trim(), phone: $("#asn-phone").value,
        address: $("#asn-address").value, symptom: $("#asn-symptom").value.trim(),
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
  const meta = state.asMeta || { types: [], statuses: [], charges: [] };
  const dis = canEdit ? "" : "disabled";
  revealPanel(host);
  host.innerHTML = `
    <div class="card">
      <div class="inline-row">
        <h3 style="margin:0; flex:1;">${escapeHtml(t.ticketNo)}
          <span class="chip ${AS_CHIP[t.status] || "chip-slate"}">${escapeHtml(t.statusLabel)}</span>
          ${t.assetNo ? `<span class="chip chip-green">${escapeHtml(t.assetNo)}</span>` : ""}
        </h3>
        <button class="btn btn-sm" id="asd-close">닫기</button>
      </div>
      <div class="form-grid" style="margin-top:8px;">
        <label>고객명<input type="text" id="asd-customer" value="${escapeHtml(t.customer)}" ${dis}></label>
        <label>연락처<input type="text" id="asd-phone" value="${escapeHtml(t.phone)}" ${dis}></label>
        <label>유형<select id="asd-type" ${dis}>${meta.types.map((x) => `<option value="${x.code}" ${x.code === t.asType ? "selected" : ""}>${escapeHtml(x.label)}</option>`).join("")}</select></label>
        <label>상태<select id="asd-status" ${dis}>${meta.statuses.map((x) => `<option value="${x.code}" ${x.code === t.status ? "selected" : ""}>${escapeHtml(x.label)}</option>`).join("")}</select></label>
        <label>비용 부담<select id="asd-charge" ${dis}>${meta.charges.map((x) => `<option value="${x.code}" ${x.code === t.chargeTo ? "selected" : ""}>${escapeHtml(x.label)}</option>`).join("")}</select></label>
        <label>수리비<input type="text" id="asd-cost" value="${t.cost}" ${dis}></label>
        <label>담당자<input type="text" id="asd-assignee" value="${escapeHtml(t.assignee)}" ${dis}></label>
        <label>접수일<input type="date" id="asd-date" value="${escapeHtml(t.receivedAt)}" ${dis}></label>
      </div>
      <label class="muted" style="display:block; margin-top:8px;">주소 <span style="font-weight:400;">(회수 예약에 필요)</span>
        <input type="text" id="asd-address" value="${escapeHtml(t.address || "")}" style="width:100%; margin-top:4px; padding:8px 10px; border:1px solid var(--border); border-radius:8px; background:var(--bg);" ${dis}></label>
      <label class="muted" style="display:block; margin-top:8px;">증상
        <input type="text" id="asd-symptom" value="${escapeHtml(t.symptom)}" style="width:100%; margin-top:4px; padding:8px 10px; border:1px solid var(--border); border-radius:8px; background:var(--bg);" ${dis}></label>
      <label class="muted" style="display:block; margin-top:8px;">처리 내용
        <input type="text" id="asd-result" value="${escapeHtml(t.result)}" style="width:100%; margin-top:4px; padding:8px 10px; border:1px solid var(--border); border-radius:8px; background:var(--bg);" ${dis}></label>
      ${canEdit ? `<div class="editor-actions">
        <button class="btn btn-primary" id="asd-save">저장</button>
        ${t.recallWid
          ? `<span class="chip ${t.recallDone ? "chip-green" : "chip-blue"}"
                title="배송/송장 → 회수/반품 탭에서 진행 상황을 봅니다">🚚 ${t.recallDone ? "회수 입고 완료" : "회수 예약됨"} ${escapeHtml(t.recallWid)}</span>`
          : `<button class="btn" id="asd-recall" title="고객에게서 제품을 수거합니다(CJ 회수 접수)">🚚 회수 예약</button>`}
        ${t.returnWid
          ? `<a class="btn" href="/api/waybills/${encodeURIComponent(t.returnWid)}/pdf" target="_blank"
               title="반송 송장 ${escapeHtml(t.returnInvoiceNo || "")}">🧾 반송 송장 출력</a>`
          : `<button class="btn" id="asd-return" title="수리를 마친 물건을 고객에게 돌려보냅니다">📦 반송 송장 발급</button>`}
        ${t.assetId && t.cost > 0 && t.chargeTo === "company"
          ? `<button class="btn" id="asd-tocost">수리비를 자산 원가에 반영</button>` : ""}
      </div>
      <div id="asd-recall-form"></div>
      <div class="inline-row" style="margin:10px 0 0; flex-wrap:wrap;">
        <b class="muted" style="font-size:13px;">문자 보내기</b>
        ${AS_SMS_EVENTS.map(([code, label]) =>
          `<button class="btn btn-ghost btn-sm" data-sms="${code}">${label}</button>`).join("")}
        <span class="muted" style="font-size:12px;">— 고객 휴대폰으로 안내를 보냅니다</span>
      </div>` : ""}
    </div>
    <div class="card">
      <h3>보낸 문자</h3>
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
    </div>`;
  $("#asd-close").addEventListener("click", () => { state.asDetailId = undefined; host.innerHTML = ""; });
  if (!canEdit) return;
  $("#asd-save").addEventListener("click", async () => {
    try {
      await api(`/api/as-tickets/${t.id}`, { method: "PATCH", body: {
        customer: $("#asd-customer").value, phone: $("#asd-phone").value,
        asType: $("#asd-type").value, status: $("#asd-status").value,
        chargeTo: $("#asd-charge").value, cost: $("#asd-cost").value.replaceAll(",", "") || 0,
        assignee: $("#asd-assignee").value, receivedAt: $("#asd-date").value,
        address: $("#asd-address").value,
        symptom: $("#asd-symptom").value, result: $("#asd-result").value,
      } });
      toast("저장했습니다.");
      renderAsView($("#main"));
    } catch (err) { toast(err.message, true); }
  });
  const recallBtn = $("#asd-recall");
  if (recallBtn) recallBtn.addEventListener("click", () => {
    const host = $("#asd-recall-form");
    if (host.innerHTML) { host.innerHTML = ""; return; }
    host.innerHTML = `
      <div style="border:1px dashed var(--border); border-radius:8px; padding:12px; margin-top:10px;">
        <b>회수 예약</b>
        <p class="muted" style="margin:4px 0 8px;">고객 주소로 CJ 기사가 방문해 제품을 가져옵니다.
        회수 송장번호는 집화할 때 부여되므로 예약 직후에는 비어 있는 것이 정상입니다.</p>
        <div class="inline-row" style="margin:0;">
          <label class="muted">수거 희망일 <input type="date" id="asr-date"></label>
          <button class="btn btn-sm btn-primary" id="asr-go">회수 예약</button>
          <button class="btn btn-ghost btn-sm" id="asr-cancel">닫기</button>
        </div>
      </div>`;
    $("#asr-cancel").addEventListener("click", () => { host.innerHTML = ""; });
    $("#asr-go").addEventListener("click", async () => {
      const go = $("#asr-go");
      go.disabled = true;
      try {
        const r = await api(`/api/as-tickets/${t.id}/recall`, { method: "POST",
          body: { pickupDate: $("#asr-date").value } });
        toast(r.simulated ? `테스트 회수 예약: ${r.wid}` : `회수 예약 완료: ${r.wid}`);
        renderAsView($("#main"));   // 목록·요약 숫자도 함께 갱신
      } catch (err) { toast(err.message, true); go.disabled = false; }
    });
  });
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

  // ★범위는 host(=이 상세 팝업)다. 예전엔 renderAsView의 파라미터 이름(main)을 그대로 썼는데,
  //   이 함수 스코프에는 그런 변수가 없어 ReferenceError로 함수가 끊겼다. 그래서 문자를
  //   한 번 보내 화면이 다시 그려지고 나면 버튼 4개가 통째로 먹통이 됐다(2026-07-29 전수조사).
  $$("button[data-sms]", host).forEach((b) => b.addEventListener("click", async () => {
    const label = b.textContent.trim();
    if (!confirm(`${t.customer}님(${t.phone || "번호 없음"})에게 '${label}' 안내를 보냅니다.\n계속할까요?`)) return;
    b.disabled = true;
    try {
      // ★먼저 force 없이 보낸다. 이미 보낸 건이면 서버가 막아 주고, 그때 한 번 더 물어본다
      //   (무조건 재발송으로 두면 실수로 같은 문자를 여러 번 보내게 된다).
      let r = await api(`/api/as-tickets/${t.id}/sms`, { method: "POST",
        body: { event: b.dataset.sms } });
      if (!r.ok && /이미 보낸/.test(r.message || "")) {
        if (confirm(`이미 보낸 안내입니다.\n${t.customer}님에게 '${label}'를 한 번 더 보낼까요?`)) {
          r = await api(`/api/as-tickets/${t.id}/sms`, { method: "POST",
            body: { event: b.dataset.sms, force: true } });
        } else { b.disabled = false; return; }
      }
      // 야간은 재발송과 별개다 — 새벽에 고객 휴대폰이 울리는 일은 한 번 더 확인받는다
      if (!r.ok && /야간/.test(r.message || "")) {
        if (confirm(`${r.message}\n\n지금은 고객이 자고 있을 수 있습니다.\n그래도 지금 보낼까요?`)) {
          r = await api(`/api/as-tickets/${t.id}/sms`, { method: "POST",
            body: { event: b.dataset.sms, force: true, allowQuiet: true } });
        } else { b.disabled = false; return; }
      }
      toast(r.message, !r.ok);
      renderAsDetail();
    } catch (err) { toast(err.message, true); }
    finally { b.disabled = false; }
  }));
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
