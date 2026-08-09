/* HMS 셋팅 — 제품 준비/QC 실시간 작업보드 (기존 order-workflow 기능 이식).
   누가 어떤 주문을 준비 중인지 실시간 표시: 5초 폴링 + 담당자 클레임(409) + 포커스 갱신 */
"use strict";

function renderSetupView(main) {
  // 셋팅 전용 사용자(setup.view)도 들어올 수 있어야 한다 — orders.view만 보면 안 된다
  if (!canSeeMenu("setup")) {
    main.innerHTML = `<h1 class="page-title">셋팅</h1><div class="card placeholder"><p>셋팅 메뉴 접근 권한이 없습니다.</p></div>`;
    return;
  }
  const canWork = hasPerm("orders.work");
  main.innerHTML = `
    <h1 class="page-title">셋팅 <span class="muted" style="font-size:14px;">제품 준비 · QC 작업보드</span></h1>
    <p class="page-desc">체크한 사람이 담당자로 기록됩니다. 다른 사람이 준비 중인 주문은 잠깁니다. (5초 자동 동기화)</p>
    <div id="setup-stock-warn"></div>
    <div id="setup-shipped-warn"></div>
    <div id="setup-rental-warn"></div>
    <div id="setup-qcwatch-warn"></div>
    <div class="kpi-row" id="setup-kpi"></div>
    <div class="card">
      <div class="inline-row">
        <input type="text" id="sf-q" placeholder="주문번호/수취인/상품 검색" style="min-width:220px;">
        <button class="btn btn-sm btn-primary" id="sf-search">조회</button>
        <select id="sf-view" title="끝난 작업은 기본으로 숨깁니다 — 누가 했는지 보려면 [완료 포함]">
          <option value="todo">진행 중만</option>
          <option value="all">완료 포함 (담당자 보기)</option>
          <option value="shipped">✅ 출고 완료 고객</option>
        </select>
        <select id="sf-sort" title="어느 것부터 볼지 — 작업은 먼저 들어온 주문부터 합니다">
          <option value="old">오래된 주문부터</option>
          <option value="new">최신 주문부터</option>
        </select>
        <button class="btn btn-sm btn-primary" id="sf-shipout"
          title="출고 확인까지 끝난 주문을 오늘 출고로 마감하고 준비 목록에서 내립니다">🚚 금일 출고 확인</button>
        <button class="btn btn-sm" id="sf-stats" title="누가 몇 대를 셋팅했는지 — 일·주·월·분기·연도">📅 셋팅 실적</button>
        <span class="muted" id="setup-sync"></span>
      </div>
      <div class="table-wrap"><table class="setup-table">
        <colgroup>
          <col class="col-chan"><col><col class="col-recipient"><col class="col-assets">
          <col class="col-stage"><col class="col-stage"><col class="col-stage">
          <col class="col-status">
        </colgroup>
        <thead><tr>
          <th>채널 / 주문</th><th>상품</th><th>수취인</th><th>자산번호</th>
          <th class="stage-th">준비 중</th><th class="stage-th">제작 완료</th><th class="stage-th" title="송장을 뽑으면 자동으로 켜집니다">출고 확인</th><th>상태</th>
        </tr></thead>
        <tbody id="setup-rows" class="setup-rows-fixed"><tr><td colspan="8" class="muted">불러오는 중…</td></tr></tbody>
      </table></div>
      <div id="setup-pager"></div>
    </div>
    <div id="setup-match"></div>`;

  renderShippedWarn();          // 보드에 남은 '이미 출고된 건'을 알려 준다(첫 진입에 1회)
  renderRentalWarn();           // 렌탈(RMS) 주문이 섞여 있으면 알려 준다
  renderQcWatchWarn();          // QC 폴더 연결이 끊겼으면 크게 알려 준다
  // 행에 붙일 'QC 출고됨' 표시 — 받아 두고 다음 렌더부터 보여 준다
  api("/api/qc-shipped/flags").then((r) => {
    state.qcShippedFlags = r.flags || {};
    if (state.view === "setup" && state.setupOrders && !isEditingInput()) {
      renderSetupRows(setupOrder(filterByStage(state.setupOrders)), canWork);
    }
  }).catch(() => {});

  const load = async (force) => {
    const seq = state.renderSeq;
    if (!force && isEditingInput()) return;
    try {
      const q = $("#sf-q") ? $("#sf-q").value.trim() : "";
      // ★기본은 '아직 할 일'만 — 끝난 것까지 다 뜨면 작업 보드로 못 쓴다.
      //   다만 누가 했는지 보려고 [완료 포함]을 고르면 출고완료분도 보여 준다
      //   (2026-08-05 대표: "작업 끝낸 사람들에 대한 담당자 목록이 안 뜬다").
      // ★[출고 완료 고객]은 나간 건만 따로 본다(대표 2026-08-05).
      //   QC 프로그램은 출고 확인을 누르면 목록에서 빼 버려 지난 이력이 남지 않는다 —
      //   HMS에서는 이 보기가 그 이력 화면이 된다.
      const mode = ($("#sf-view") || {}).value || "todo";
      // ★끝난 주문은 대부분 '보관'까지 돼 있어 view=active로는 안 잡힌다
      //   (실측 출고완료 700건 중 699건이 보관됨). 완료 보기에서는 보관분까지 가져온다.
      const all = await fetchOrders({ view: mode === "todo" ? "active" : "all", q });
      const orders = mode === "shipped"
        ? all.filter((o) => o.shippingDone && !o.cancelledAt)
        : mode === "all"
          ? all.filter((o) => !o.cancelledAt)    // 취소만 빼고 다 본다
          : all.filter((o) => !o.shippingDone);
      if (seq !== state.renderSeq || !$("#setup-rows")) return;
      state.setupOrders = orders;
      renderSetupRows(setupOrder(filterByStage(orders)), canWork);
      renderSetupKpi(orders);           // 숫자는 항상 전체 기준 — 걸러도 개수는 그대로 보인다
      // 스펙·재고는 몰에서 가져온다(캐시가 있으면 호출 없음). 새로 받았을 때만 다시 그린다.
      loadProductInfo(orders).then((changed) => {
        if (changed && state.view === "setup" && !isEditingInput()) {
          renderSetupRows(setupOrder(filterByStage(state.setupOrders)), canWork);
        }
      });
      const sync = $("#setup-sync");
      if (sync) sync.textContent = "동기화 " + new Date().toTimeString().slice(0, 8);
    } catch (_e) { /* 폴링 오류는 조용히 — 다음 주기에 재시도 */ }
  };
  // ★[금일 출고 확인] — QC 프로그램과 같은 흐름(대표 2026-08-05).
  //   출고 확인까지 끝난 주문을 오늘 출고로 마감하고 준비 목록에서 내린다.
  //   내려간 건은 [📋 출고 기록 조회]에 쌓인다.
  $("#sf-shipout").addEventListener("click", async () => {
    let d;
    try { d = await api("/api/orders/ship-today/preview"); }
    catch (e) { toast(e.message, true); return; }
    if (!d.count) { toast("출고 확인까지 끝난 주문이 없습니다."); return; }
    const warn = d.noWaybill
      ? `\n\n⚠ 그중 ${d.noWaybill}건은 택배인데 송장번호가 없습니다 — 추적이 안 됩니다.` : "";
    if (!confirm(`출고 확인된 ${d.count}건을 오늘 출고로 마감합니다.\n`
      + "준비 목록에서 내려가고, [📋 출고 기록 조회]에서 다시 볼 수 있습니다." + warn
      + "\n\n진행할까요?")) return;
    try {
      const r = await api("/api/orders/ship-today", { method: "POST", body: {} });
      toast(`${r.shipped}건 출고 마감했습니다.`);
      resetPage("setup");
      load(true);
    } catch (e) { toast(e.message, true); }
  });
  $("#sf-search").addEventListener("click", () => { resetPage("setup"); load(true); });
  $("#sf-view").addEventListener("change", () => { resetPage("setup"); load(true); });
  // 정렬만 바꿀 때는 서버를 다시 부르지 않는다 — 이미 받아 둔 목록을 다시 줄 세우면 된다
  $("#sf-sort").addEventListener("change", () => {
    resetPage("setup");
    renderSetupRows(setupOrder(filterByStage(state.setupOrders || [])), canWork);
  });
  // 셋팅 작업자에게는 [설정] 메뉴가 없다 — 자기 실적은 이 화면에서 바로 볼 수 있어야 한다
  $("#sf-stats").addEventListener("click", () => {
    const host = $("#setup-match");
    if (!host) return;
    host.innerHTML = `<div class="card"><div class="inline-row">
        <h3 style="margin:0; flex:1;">📅 셋팅 실적</h3>
        <button class="btn btn-sm" id="ss-inline-close">닫기</button>
      </div><div id="tab-body"></div></div>`;
    revealPanel(host);
    $("#ss-inline-close").addEventListener("click", () => { host.innerHTML = ""; });
    renderSetupStatsTab($("#tab-body"));
  });
  autoSearch("#sf-q", () => { resetPage("setup"); load(true); });
  load(true);
  addPoller(load, 5000);
  // 창 복귀 시 즉시 동기화 (핸들러 중복 등록 방지)
  if (state.setupFocusHandler) window.removeEventListener("focus", state.setupFocusHandler);
  state.setupFocusHandler = () => { if (state.view === "setup") load(true); };
  window.addEventListener("focus", state.setupFocusHandler);
}

/* 출고 마감을 누가 언제 했는지 — 준비·제작·출고확인은 각자 칸에 뜨는데
   마지막 '출고'만 상태 칩뿐이라 [출고 기록 조회]에서 담당자가 비어 보였다
   (대표 2026-08-05: "누가 준비했는지 언제 준비했는지도 기록이 다 있을 텐데 그대로 뜨게 해줘"). */
function shipDoneLine(o) {
  if (!o.shippingDone) return "";
  const who = String(o.shippingBy || "").trim();
  const when = stageWhen(o.shippingAt);
  if (!who && !when) return "";
  return `<div class="ship-done" title="출고 ${escapeHtml(who)} ${
    escapeHtml(String(o.shippingAt || "").replace("T", " ").slice(0, 16))}">${
    who ? `<b>${escapeHtml(who)}</b>` : ""}${when ? ` <span>${escapeHtml(when)}</span>` : ""}</div>`;
}

/* ★'QC에서는 이미 출고된 건'을 행에 알려 준다(대표 2026-08-05:
   "출고 완료인 제품인데 왜 제작대기에 있는지 알 수 있나?").
   HMS는 주문 단위 합계 금액, QC는 상품·대수 단위 금액이라 금액이 달라 자동 반영에서 빠진다.
   자동으로 처리하진 않되(잘못 찍으면 안 나간 물건이 나간 게 된다), 눈에는 보이게 한다. */
function qcShippedChip(o) {
  const f = (state.qcShippedFlags || {})[String(o.id)];
  if (!f) return "";
  const when = String(f.shippedAt || "").replace("T", " ").slice(0, 10);
  return `<span class="chip chip-amber qc-shipped-chip" title="QC 프로그램에서는 이미 출고된 건입니다&#10;`
    + `QC 주문번호 ${escapeHtml(f.qcOrderNumber)}&#10;`
    + `QC 금액 ${fmtWon(f.qcAmount)} (우리 ${fmtWon(o.amount)} — 우리는 주문 합계, QC는 상품별 금액)&#10;`
    + `${f.qcAssetNo ? "관리번호 " + escapeHtml(f.qcAssetNo) + "&#10;" : ""}`
    + `출고 ${escapeHtml(when)} ${escapeHtml(f.shippedBy || "")}">⚠ QC 출고됨</span>`;
}

/* QC 폴더 연결이 끊기면 크게 알린다.

   ★왜 필요한가(2026-08-06 실측): 폴더를 못 읽어도 감시가 **조용히** 멈췄다.
     화면은 평소와 똑같아서, QC에서 체크한 것이 안 넘어오는 줄 아무도 모른 채 하루가 갔다.
     연동이 죽었으면 죽었다고 보여 줘야 손으로라도 챙긴다. */
async function renderQcWatchWarn() {
  const host = $("#setup-qcwatch-warn");
  if (!host) return;
  let d;
  try { d = await api("/api/qc-watch/status"); } catch (_e) { return; }
  if (!$("#setup-qcwatch-warn")) return;
  if (!d.enabled || d.found) { host.innerHTML = ""; return; }
  const last = d.lastAt ? String(d.lastAt).replace("T", " ").slice(0, 16) : "";
  host.innerHTML = `<div class="card warn-card">
    <div class="inline-row">
      <b style="flex:1; color:var(--danger);">⚠ QC 프로그램 폴더에 연결하지 못하고 있습니다 —
        지금 체크하는 내용이 여기로 넘어오지 않습니다</b>
      <button class="btn btn-sm" id="qw-retry">다시 시도</button>
    </div>
    <p class="muted" style="margin:6px 0 0;">
      폴더: <code>${escapeHtml(d.folder || "")}</code><br>
      ${escapeHtml(d.error || "폴더를 읽을 수 없습니다.")}
      ${last ? `<br>마지막으로 받아온 시각: ${escapeHtml(last)}` : ""}
      <br>그 PC가 켜져 있는지, 공유 폴더 접근 권한이 살아 있는지 확인해 주세요.
      그때까지는 <b>QC에서 체크한 담당자·시각이 이 화면에 반영되지 않습니다.</b>
    </p></div>`;
  const btn = $("#qw-retry");
  if (btn) btn.addEventListener("click", async () => {
    btn.disabled = true;
    try {
      const r = await api("/api/qc-watch/run", { method: "POST", body: {} });
      if (r.error) { toast(r.error, true); btn.disabled = false; return; }
      toast(`주문 ${r.rows}건을 읽었습니다 — 단계 ${r.advanced}건 반영.`);
      renderSetup();
    } catch (e) { toast(e.message, true); btn.disabled = false; }
  });
}

/* 렌탈(RMS) 주문이 판매 작업 목록에 섞여 있으면 알려 주고, 한 번에 내린다.

   ★대표 지시(2026-08-05): "RMS로 들어가는 렌탈 상품이 HMS에 뜬다. 상품번호로 안 뜨게 하자."
   ★수집 필터만으로는 부족하다 — 그건 앞으로 들어올 것만 막는다.
     이미 떠 있는 건 여기서 내리고, 그 상품번호를 몰 설정에 **자동으로 등록**해
     다음부터는 아예 안 들어오게 한다(상품번호를 몰에서 찾아 옮겨 적을 필요가 없다).
   ★매출·재고는 건드리지 않는다. 작업 목록에서만 내린다. */
async function renderRentalWarn() {
  const host = $("#setup-rental-warn");
  if (!host) return;
  let d;
  try { d = await api("/api/orders/rental-suspects"); } catch (_e) { return; }
  if (!$("#setup-rental-warn")) return;
  if (!d.count) { host.innerHTML = ""; return; }
  host.innerHTML = `<div class="card warn-card">
    <div class="inline-row">
      <b style="flex:1;">🏷 렌탈(RMS) 주문으로 보이는 건이 ${d.count}건 섞여 있습니다</b>
      <button class="btn btn-sm" id="rt-list">목록 보기</button>
      <button class="btn btn-sm btn-primary" id="rt-move">${d.count}건 렌탈로 보내기</button>
    </div>
    <p class="muted" style="margin:6px 0 0;">
      렌탈은 RMS 몫이라 여기서 만들면 안 됩니다. [렌탈로 보내기]를 누르면 작업 목록에서 내리고,
      <b>그 상품번호를 몰 설정에 등록해 다음부터는 수집하지 않습니다.</b>
      매출·재고는 건드리지 않습니다.
    </p>
    <div id="rt-detail"></div></div>`;

  const draw = () => {
    const box = $("#rt-detail");
    if (!box) return;
    if (box.innerHTML) { box.innerHTML = ""; return; }
    box.innerHTML = `<div class="table-wrap" style="margin-top:10px; max-height:300px; overflow:auto;">
      <table><thead><tr>
        <th style="width:34px;"><input type="checkbox" id="rt-all" checked></th>
        <th>채널</th><th>수취인</th><th>상품</th><th>상품번호</th><th>걸린 말</th>
      </tr></thead><tbody>${d.orders.map((o) => `<tr>
        <td><input type="checkbox" class="rt-pick" data-id="${o.id}" checked></td>
        <td>${escapeHtml(o.channel)}</td>
        <td>${escapeHtml(o.recipient)}</td>
        <td title="${escapeHtml(o.productName)}">${escapeHtml((o.productName || "").slice(0, 34))}</td>
        <td>${escapeHtml(o.productCode || "-")}${o.registered
          ? ' <span class="chip chip-slate">등록됨</span>' : ""}</td>
        <td><span class="chip chip-amber">${escapeHtml((o.words || []).join(", "))}</span></td>
      </tr>`).join("")}</tbody></table></div>`;
    const all = $("#rt-all");
    if (all) all.addEventListener("change", () => {
      $$(".rt-pick", box).forEach((c) => { c.checked = all.checked; });
    });
    attachShiftPick(box, ".rt-pick");
  };
  $("#rt-list").addEventListener("click", draw);

  $("#rt-move").addEventListener("click", async () => {
    const picked = $$(".rt-pick").filter((c) => c.checked).map((c) => Number(c.dataset.id));
    const ids = picked.length ? picked : d.orders.map((o) => o.id);
    if (!confirm(`${ids.length}건을 렌탈(RMS)로 보냅니다.\n\n`
      + "· 판매 작업 목록에서 내려갑니다\n"
      + "· 그 상품번호를 몰 설정의 [렌탈 상품번호]에 등록해 다음부터는 수집하지 않습니다\n"
      + "· 매출·재고는 건드리지 않습니다 (되돌릴 수 있습니다)\n\n진행할까요?")) return;
    try {
      const r = await api("/api/orders/mark-rental",
                          { method: "POST", body: { ids, register: true } });
      const regs = Object.values(r.registered || {}).flat();
      toast(`${r.moved}건을 렌탈로 보냈습니다.`
        + (regs.length ? ` 상품번호 ${regs.length}개를 등록했습니다.` : ""));
      renderSetup();
    } catch (e) { toast(e.message || "보내지 못했습니다.", true); }
  });
}

/* 매출이 두 번 잡힌 중복 — 합치기 버튼 배선(배너가 어떻게 그려지든 같은 코드를 쓴다). */
function wireMerge(mg) {
  if (!mg || !mg.count) return;
  const listBtn = $("#qm-list");
  if (listBtn) listBtn.addEventListener("click", () => {
    const box = $("#qm-detail");
    if (!box) return;
    if (box.innerHTML) { box.innerHTML = ""; return; }
    box.innerHTML = `<div class="table-wrap" style="margin-top:10px; max-height:300px; overflow:auto;">
      <table><thead><tr>
        <th>수취인</th><th>남길 주문</th><th>남길 금액</th>
        <th>내릴 주문</th><th>빠질 금액</th><th>자산 이동</th>
      </tr></thead><tbody>${mg.items.map((it) => `<tr>
        <td>${escapeHtml(it.recipient || "")}</td>
        <td>#${it.keepId}</td><td>${fmtWon(it.keepAmount)}</td>
        <td>#${it.dropId}</td><td>${fmtWon(it.dropAmount)}</td>
        <td>${it.moveAssets.length ? `${it.moveAssets.length}대` : "-"}</td>
      </tr>`).join("")}</tbody></table></div>`;
  });
  const runBtn = $("#qm-run");
  if (runBtn) runBtn.addEventListener("click", async () => {
    if (!confirm(`${mg.count}건을 합칩니다.\n\n`
      + `· 금액이 있는 줄을 남기고 자산 ${mg.movingAssets}대를 그 줄로 옮깁니다\n`
      + `· 나머지 줄은 출고완료를 풀고 목록에서 내립니다\n`
      + `· 매출 ${fmtWon(mg.removedAmount)}이 이중계상에서 빠집니다\n\n진행할까요?`)) return;
    runBtn.disabled = true;
    try {
      const r = await api("/api/qc-shipped/merge", { method: "POST", body: {} });
      toast(`${r.merged}건 합쳤습니다 — 매출 ${fmtWon(r.removedAmount)} 정리, 자산 ${r.movedAssets}대 이동`);
      renderSetup();
    } catch (e) { toast(e.message || "합치지 못했습니다.", true); runBtn.disabled = false; }
  });
}

/* QC에서 이미 출고된 고객이 보드에 남아 있으면 알려 주고, 눈으로 확인한 뒤 내린다.

   ★왜 필요한가(대표 2026-08-05)
     QC 프로그램은 [금일 출고 확인 엑셀]을 누르면 목록에서 빼 버린다. 그런데 HMS는
     몰에서 직접 주문을 받아 오므로 **같은 주문이 서로 다른 키**로 존재한다
     (QC "주문수집:…" vs HMS 몰 주문번호). 키가 다르니 자동으로 이어지지 않고,
     이미 나간 고객이 셋팅/QC 보드에 계속 남는다(실측 잔여 166건 중 46건).
   ★자동으로 반영하지 않는다 — 엉뚱한 주문을 출고완료로 만들면 나가지 않은 물건이
     나간 것으로 잡힌다. 근거를 보여 주고 대표가 [반영]을 눌러야 움직인다. */
async function renderShippedWarn() {
  const host = $("#setup-shipped-warn");
  if (!host || !state.user.isAdmin) return;
  let d, mg;
  try { d = await api("/api/qc-shipped/preview"); }
  catch (_e) { return; }                        // 폴더가 끊겼을 뿐 — 작업은 계속돼야 한다
  try { mg = await api("/api/qc-shipped/merge/preview"); } catch (_e) { mg = { count: 0 }; }
  if (!$("#setup-shipped-warn")) return;
  // ★같은 주문이 두 줄인데 **양쪽 다 출고완료**면 매출이 두 번 잡힌다.
  //   한 줄은 금액(몰 수집분), 다른 줄은 자산번호(QC 이관분)를 갖고 있어
  //   한쪽을 되돌리면 매출이 사라진다 — 그래서 '합치기'로 처리한다.
  const mergeBox = mg.count ? `<div class="card warn-card dup-warn" style="margin-top:10px;">
    <div class="inline-row">
      <b style="flex:1;">💰 매출이 두 번 잡힌 중복 ${mg.count}건 — ${fmtWon(mg.removedAmount)} 과다</b>
      <button class="btn btn-sm" id="qm-list">목록 보기</button>
      <button class="btn btn-sm btn-primary" id="qm-run">${mg.count}건 합치기</button>
    </div>
    <p class="muted" style="margin:6px 0 0;">
      같은 주문이 우리 쪽에 두 줄인데 <b>양쪽 다 출고완료</b>라 매출이 두 번 잡혀 있습니다.
      금액이 있는 줄을 남기고, 자산번호(${mg.movingAssets}대)를 그 줄로 옮긴 뒤 나머지를 내립니다.
      <b>매출은 한 건만 남습니다.</b>
    </p>
    <div id="qm-detail"></div></div>` : "";
  const n = (d.matched || []).length;
  const dup = d.duplicates || [];
  const uns = d.unsure || [];
  if (!n && !dup.length && !uns.length && !mg.count) { host.innerHTML = ""; return; }
  if (!n && !dup.length && !uns.length) { host.innerHTML = mergeBox; wireMerge(mg); return; }
  host.innerHTML = `<div class="card warn-card">
    <div class="inline-row">
      <b style="flex:1;">📦 QC에서 이미 출고된 주문이 이 보드에 ${n}건 남아 있습니다</b>
      ${n ? `<button class="btn btn-sm" id="qs-list">확인하기</button>
             <button class="btn btn-sm btn-primary" id="qs-apply">${n}건 모두 반영</button>` : ""}
    </div>
    <p class="muted" style="margin:6px 0 0;">
      QC 프로그램은 출고 확인을 누르면 목록에서 빼기 때문에 HMS로 이어지지 않은 건입니다.
      ${uns.length ? `근거가 갈리는 ${uns.length}건은 자동 반영하지 않습니다.` : ""}
    </p>
    ${dup.length ? `<div class="dup-warn">
      <div class="inline-row">
        <b style="flex:1;">⚠ 같은 주문이 우리 쪽에 두 줄인 것 ${dup.length}건 — 출고 처리하면 안 됩니다</b>
        <button class="btn btn-sm" id="qs-duplist">목록 보기</button>
        <button class="btn btn-sm btn-primary" id="qs-dupdismiss">${
          dup.filter((m) => m.duplicateShipped).length}건 보드에서 내리기</button>
      </div>
      <p class="muted" style="margin:6px 0 0;">
        QC 이관으로 이미 들어온 주문이 몰 수집으로 한 번 더 들어온 것입니다.
        여기서 출고 처리하면 <b>매출과 출고 건수가 두 배로 잡힙니다.</b>
        어느 줄을 남길지 정해서 정리해야 하는 건이라 자동 반영에서 뺐습니다.
      </p>
      <div id="qs-dupdetail"></div>
    </div>` : ""}
    <div id="qs-detail"></div></div>` + mergeBox;
  wireMerge(mg);

  const drawDup = () => {
    const box = $("#qs-dupdetail");
    if (!box) return;
    box.innerHTML = `<div class="table-wrap" style="margin-top:10px; max-height:300px; overflow:auto;">
      <table><thead><tr>
        <th>우리 주문</th><th>수취인</th><th>금액</th><th>중복 상대</th><th>QC 주문</th><th>상태</th>
      </tr></thead><tbody>${dup.map((m) => `<tr>
        <td>#${m.id}</td>
        <td>${escapeHtml(m.recipient)}</td>
        <td>${fmtWon(m.amount)}</td>
        <td>#${m.duplicateOf}</td>
        <td>${escapeHtml(m.qcOrderNumber)}</td>
        <td>${m.duplicateShipped
          ? '<span class="chip chip-red">그 줄은 이미 출고완료</span>'
          : '<span class="chip chip-slate">확인 필요</span>'}</td>
      </tr>`).join("")}</tbody></table></div>`;
  };
  const dismissBtn = $("#qs-dupdismiss");
  if (dismissBtn) dismissBtn.addEventListener("click", async () => {
    const ids = dup.filter((m) => m.duplicateShipped).map((m) => m.id);
    if (!confirm(`${ids.length}건을 보드에서 내립니다.\n\n`
      + "이 건들은 물건이 이미 다른 줄로 출고 완료된 것입니다.\n"
      + "★출고완료로 찍지 않고 '중복'으로만 내립니다 — 그래야 매출이 두 배가 되지 않습니다.\n"
      + "나중에 되돌릴 수 있습니다. 진행할까요?")) return;
    dismissBtn.disabled = true;
    try {
      const r = await api("/api/qc-shipped/dismiss-duplicates", { method: "POST", body: { ids } });
      toast(`${r.archived}건을 보드에서 내렸습니다.${r.skipped ? ` (건너뜀 ${r.skipped})` : ""}`);
      renderSetup();
    } catch (e) { toast(e.message || "내리지 못했습니다.", true); dismissBtn.disabled = false; }
  });
  const dupBtn = $("#qs-duplist");
  if (dupBtn) dupBtn.addEventListener("click", () => {
    const box = $("#qs-dupdetail");
    if (box.innerHTML) box.innerHTML = ""; else drawDup();
  });

  const draw = () => {
    const box = $("#qs-detail");
    if (!box) return;
    box.innerHTML = `<div class="table-wrap" style="margin-top:10px; max-height:320px; overflow:auto;">
      <table><thead><tr>
        <th style="width:34px;"><input type="checkbox" id="qs-all" checked></th>
        <th>수취인</th><th>상품</th><th>금액</th><th>QC 출고</th><th>맞춘 근거</th>
      </tr></thead><tbody>${(d.matched || []).map((m) => `<tr>
        <td><input type="checkbox" class="qs-pick" data-id="${m.id}" checked></td>
        <td>${escapeHtml(m.recipient)}</td>
        <td title="${escapeHtml(m.productName)}">${escapeHtml((m.productName || "").slice(0, 40))}</td>
        <td>${fmtWon(m.amount)}</td>
        <td>${escapeHtml((m.shippedAt || "").replace("T", " ").slice(0, 16))}
            ${escapeHtml(m.shippedBy || "")}</td>
        <td><span class="chip chip-blue">${escapeHtml(m.matchedBy)}</span></td>
      </tr>`).join("")}</tbody></table></div>`;
    const all = $("#qs-all");
    if (all) all.addEventListener("change", () => {
      $$(".qs-pick", box).forEach((c) => { c.checked = all.checked; });
    });
    attachShiftPick(box, ".qs-pick");
  };
  const listBtn = $("#qs-list");
  if (listBtn) listBtn.addEventListener("click", () => {
    const box = $("#qs-detail");
    if (box.innerHTML) box.innerHTML = ""; else draw();
  });
  const applyBtn = $("#qs-apply");
  if (applyBtn) applyBtn.addEventListener("click", async () => {
    const picked = $$(".qs-pick").filter((c) => c.checked).map((c) => Number(c.dataset.id));
    const ids = picked.length ? picked : (d.matched || []).map((m) => m.id);
    if (!confirm(`${ids.length}건을 출고 완료로 올리고 보드에서 내립니다.
`
      + "되돌리려면 각 주문에서 직접 풀어야 합니다. 진행할까요?")) return;
    applyBtn.disabled = true;
    try {
      const r = await api("/api/qc-shipped/apply", { method: "POST", body: { ids } });
      toast(`${r.applied}건 반영했습니다.${r.skipped ? ` (건너뜀 ${r.skipped})` : ""}`);
      renderSetup();
    } catch (e) { toast(e.message || "반영하지 못했습니다.", true); applyBtn.disabled = false; }
  });
}

function renderSetupKpi(orders) {
  const host = $("#setup-kpi");
  if (!host) return;
  const cnt = { waiting: 0, preparing: 0, produced: 0, inspected: 0 };
  for (const o of orders) {
    const k = orderStatusInfo(o).key;
    if (cnt[k] !== undefined) cnt[k]++;
  }
  // ★붙일 수 있는 자산이 0대면 스캔은 무조건 실패한다 — 번호 탓을 하며 헤매지 않게 먼저 알린다
  const av = state.availableAssets;
  const warn = $("#setup-stock-warn");
  if (warn) {
    warn.innerHTML = (av === 0)
      ? `<div class="chip chip-red" style="white-space:normal; display:block; padding:8px 12px;">
           ⚠ 창고에 붙일 수 있는 자산이 <b>0대</b>입니다. 자산번호를 찍어도 매칭되지 않습니다 —
           <b>매입 → 자산 등록</b>으로 새 제품을 먼저 등록하세요.
           (기존 ${"자산은 모두 출고 완료 상태입니다"})</div>`
      : "";
  }
  // ★대표 지적(2026-08-05): "위에 있는 제작대기·준비중·제작완료·검수완료는 왜 누를 수 없나."
  //   숫자만 보여 주고 끝이 아니라, 눌러서 그 단계만 골라 볼 수 있어야 작업 도구다.
  const cur = state.setupStage || "";
  const card = (key, label, value, hint) => `
    <button class="kpi kpi-btn${cur === key ? " on" : ""}" data-stage-filter="${key}"
            title="${hint}">
      <div class="kpi-label">${label}</div><div class="kpi-value">${value}</div></button>`;
  host.innerHTML =
    card("waiting", "제작 대기", cnt.waiting, "아직 아무도 손대지 않은 주문만 봅니다")
    + card("preparing", "준비 중", cnt.preparing, "누군가 준비를 시작한 주문만 봅니다")
    + card("produced", "제작 완료", cnt.produced, "제작이 끝나 송장을 뽑을 수 있는 주문만 봅니다")
    + card("inspected", "출고 확인", cnt.inspected, "송장까지 나가 출고 마감을 기다리는 주문만 봅니다")
    // ★[금일 출고 확인]으로 목록에서 내린 건들은 여기에 쌓인다(대표 2026-08-05)
    + `<button class="kpi kpi-btn kpi-log${cur === "shipped" ? " on" : ""}"
               data-stage-filter="shipped" title="출고 마감된 주문을 봅니다">
         <div class="kpi-label">📋 출고 기록 조회</div>
         <div class="kpi-value">보기</div></button>`;

  $$("button[data-stage-filter]", host).forEach((b) => b.addEventListener("click", () => {
    const k = b.dataset.stageFilter;
    state.setupStage = state.setupStage === k ? "" : k;   // 다시 누르면 해제
    resetPage("setup");
    const sel = $("#sf-view");
    const sort = $("#sf-sort");
    if (sel) {           // '출고 기록 조회'는 완료 보기라야 나온다
      if (state.setupStage === "shipped") {
        sel.value = "shipped";
        if (sort) sort.value = "new";      // 기록은 최근 것부터 보는 게 자연스럽다
      } else if (sel.value === "shipped") {
        sel.value = "todo";
        if (sort) sort.value = "old";      // 작업으로 돌아오면 다시 오래된 것부터
      }
    }
    $("#sf-search") && $("#sf-search").click();
  }));
}

/* ★작업 순서 — **오래된 주문부터가 기본**(대표 2026-08-05: "가장 오래된 주문 건부터
   준비해야 한다"). 목록 API는 최신순으로 준다(새 주문을 먼저 보려고) — 작업 보드는 반대다.
   먼저 들어온 고객이 먼저 나가야 하고, 오래 밀린 건이 맨 위에 있어야 눈에 띈다.
   ★고정하지 않고 [최신 주문부터]로 바꿀 수 있게 둔다(대표 요청).
   ★[출고 기록 조회]는 '주문일'이 아니라 '출고일' 기준으로 본다 — 언제 나갔는지가 궁금한 화면이다. */
function setupOrder(orders) {
  const newest = (($("#sf-sort") || {}).value || "old") === "new";
  const shipped = state.setupStage === "shipped"
    || (($("#sf-view") || {}).value === "shipped");
  const key = (o) => String((shipped ? o.shippingAt : o.orderedAt) || o.createdAt || "");
  return orders.slice().sort((a, b) => {
    const x = key(a), y = key(b);
    if (x !== y) return (x < y ? -1 : 1) * (newest ? -1 : 1);
    return (a.id - b.id) * (newest ? -1 : 1);
  });
}

/* 단계 카드로 고른 것만 남긴다. 카드를 안 눌렀으면 그대로 전부. */
function filterByStage(orders) {
  const k = state.setupStage;
  if (!k || k === "shipped") return orders;
  return orders.filter((o) => {
    if (o.cancelledAt) return false;
    if (k === "waiting") return !o.preparing && !o.productionDone;
    if (k === "preparing") return o.preparing && !o.productionDone;
    if (k === "produced") return o.productionDone && !o.softwareInspectionDone;
    if (k === "inspected") return o.softwareInspectionDone && !o.shippingDone;
    return true;
  });
}

/* 언제·누가 체크했는지를 짧게 — '08/05 14:32' 형태.
   ★칸이 좁아 전체 날짜를 다 쓰면 잘린다. 연도는 대개 올해라 뺀다(전체는 title로 본다). */
function stageWhen(at) {
  const t = String(at || "").trim();
  if (!t) return "";
  const m = t.match(/^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})/);
  if (!m) return t.slice(5, 16).replace("T", " ");
  const thisYear = String(new Date().getFullYear());
  const d = (x) => String(Number(x));      // 앞 0을 빼 칸을 아낀다(08/05 → 8/5)
  return (m[1] === thisYear ? "" : m[1].slice(2) + "/")
    + `${d(m[2])}/${d(m[3])} ${m[4]}:${m[5]}`;
}

function stageCell(o, stage, done, by, canWork, at) {
  // ★끝난 주문은 기록을 보는 화면이다 — 체크를 되돌릴 수 있으면 실수로 지워진다.
  if (o.shippingDone) canWork = false;
  const locked = stage === "preparing" && o.preparing && by && by !== state.user.displayName && !state.user.isAdmin;
  // ★대표 요청(2026-08-05): 언제 몇 시에 누가 체크했는지가 칸에 보여야 한다.
  //   칸 폭을 46px→86px로 넓히고 이름 아래에 시각을 한 줄 더 둔다.
  //   행 높이(62px)는 그대로 — 체크박스 18 + 이름 13 + 시각 12 로 맞춘다.
  const when = done ? stageWhen(at) : "";
  const who = by
    ? `${escapeHtml(by)}${when ? " · " + escapeHtml(String(at).replace("T", " ").slice(0, 16)) : ""}`
      + (locked ? " (다른 사람이 준비 중)" : "")
    : "";
  return `<td class="stage-cell${locked ? " stage-locked" : ""}" ${who ? `title="${who}"` : ""}>
    <label class="check-line" style="justify-content:center;">
      <input type="checkbox" data-stage="${stage}" data-oid="${o.id}" ${done ? "checked" : ""} ${canWork && !locked ? "" : "disabled"}>
    </label>
    ${by ? `<div class="stage-by">${escapeHtml(by)}</div>` : ""}
    ${when ? `<div class="stage-at">${escapeHtml(when)}</div>` : ""}
  </td>`;
}

/* 상품 칸 — 상품명 → 스펙 → 옵션 → 재고 순서로 보여준다(대표 요청 2026-07-29).
   스펙과 재고는 고도몰에서 그때그때 가져온다(우리 DB에 저장하지 않는다 —
   같은 재고를 여러 고객이 사면 줄어들기 때문에 몰이 항상 정답이다). */
/* 옵션을 색 칩으로 나눠 보여준다(대표 요청 2026-07-31).
   글자로만 이어 붙이면 '옵션1/옵션2'가 한 줄로 뭉쳐 무엇을 챙겨야 하는지 눈에 안 들어온다.
   같은 옵션 이름은 항상 같은 색이 되게 이름에서 색을 뽑는다 — 담당자가 색으로 기억하게. */
const OPT_CHIPS = ["chip-blue", "chip-green", "chip-violet", "chip-amber",
                   "chip-teal", "chip-pink"];

function optionChips(text) {
  const raw = String(text || "").trim();
  if (!raw) return "";
  // 몰마다 구분자가 다르다: '/', ',', '|', 줄바꿈
  // ★단, 금액의 천 단위 쉼표에서 자르면 안 된다.
  //   'A급 배터리 (-20,000원)'이 'A급 배터리 (-20'과 '000원)'으로 쪼개졌다(대표 지적 2026-08-03).
  //   숫자 사이의 쉼표는 잠시 다른 글자로 바꿔 두고, 나눈 뒤 되돌린다.
  const KEEP = String.fromCharCode(1);   // 본문에 나올 리 없는 표식
  const parts = raw.replace(/(\d),(\d)/g, "$1" + KEEP + "$2")
    .split(/\s*[/,|\n]\s*/)
    .map((x) => x.split(KEEP).join(",").trim())
    .filter(Boolean);
  if (!parts.length) return "";
  return parts.map((t) => {
    let h = 0;
    for (let i = 0; i < t.length; i++) h = (h * 31 + t.charCodeAt(i)) >>> 0;
    // 크기는 CSS(.setup-prod .chip)가 화면 폭에 맞춰 정한다 — 여기서 고정하지 않는다
    return `<span class="chip ${OPT_CHIPS[h % OPT_CHIPS.length]}">${escapeHtml(t)}</span>`;
  }).join("");
}

/* 주문이 요구하는 '등급'을 읽는다(등급 체계는 대표 확인 2026-08-03).
     앞 글자 = 외관, 뒤 글자 = 배터리. B급만 둘을 통틀어 한 글자.
     쓰는 등급: S+S · S+A · SS · SA · AS · AA · B
   몰 주문은 외관과 배터리가 옵션에 따로 온다 — 'A급 외관' + 'S급 배터리' → AS급.
   상품명에 'AA급2'처럼 붙어 오기도 하는데, 뒤 숫자는 잘못 붙은 것이라 무시한다. */
const RE_LOOK = /(S\+|[SABC])\s*급\s*외관/;
const RE_BATT = /(S\+|[SABC])\s*급\s*배터리/;
const RE_PAIR = /(S\+[SA]|[SA][SAB]|B)\s*급/;

function orderGrade(o) {
  const t = `${o.productName || ""} ${o.optionName || ""}`;
  const look = RE_LOOK.exec(t);
  const batt = RE_BATT.exec(t);
  if (look && batt) return (look[1] + batt[1]).toUpperCase();
  const pair = RE_PAIR.exec(t);
  return pair ? pair[1].toUpperCase() : "";
}

function normGrade(g) { return String(g || "").replace("급", "").trim().toUpperCase(); }

/* 창고 재고 칩 — 숫자 하나만 두지 않고 '무엇을 셌는지'를 함께 알려 준다.
   포함 검색이라 뒤에 글자가 붙는 다른 모델까지 섞일 수 있다(갤럭시탭 S6 ← S6 Lite).
   섞였으면 ⚠를 붙이고, 마우스를 올리면 모델별 내역이 보인다. */
function stockChip(info, want) {
  const k = Number(info.ourStock);
  const by = Array.isArray(info.ourStockBy) ? info.ourStockBy : [];
  const gr = Array.isArray(info.ourStockGrade) ? info.ourStockGrade : [];
  const mixed = by.length > 1;
  const exact = by.filter((b) => b.exact).reduce((s, b) => s + b.count, 0);
  const lines = by.map((b) => `${b.model} ${b.count}대${b.exact ? " (표기 일치)" : ""}`);
  const gLines = gr.map((g) => `  ${g.grade} ${g.count}대`);
  const head = mixed
    ? `'${info.ourModel || ""}' 로 찾은 결과 ${by.length}종이 섞였습니다 — 확인하세요\n`
      + lines.join("\n") + `\n표기가 정확히 같은 것만: ${exact}대`
    : (lines[0] || "창고에 같은 모델 없음");
  const tip = head + (gr.length ? "\n\n등급별\n" + gLines.join("\n") : "");
  // ★재고 3단계(2026-08-04) — 가재고는 매칭은 되지만 출고가 막힌다.
  //   총합만 보여 주면 담당자가 있는 줄 알고 잡았다가 송장 단계에서 되돌아온다.
  const tierMap = info.ourStockTier || {};
  const prov = Number(tierMap["가재고"] || 0);
  const ship = info.ourShippable === undefined ? k : Number(info.ourShippable);
  const NL = String.fromCharCode(10);
  const tierLine = ["양품", "실재고", "가재고"]
    .filter((t) => tierMap[t]).map((t) => `  ${t} ${tierMap[t]}대`).join(NL);
  const fullTip = tip + (tierLine ? NL + NL + "재고 구분" + NL + tierLine
    + (prov ? NL + "※ 가재고는 매입에서 양품/실재고로 바꾸기 전엔 출고할 수 없습니다" : "") : "");
  // ★제품코드로 대조한 값이 정답이다(2026-08-04 대표). 모델명 근사는 코드가 없을 때의 보조.
  //   코드가 아직 안 들어간 자산은 코드 대조에 안 잡히므로, 그 사실을 분명히 알린다.
  const sku = info.sku || "";
  const codeTier = info.codeTier || {};
  const codeProv = Number(codeTier["가재고"] || 0);
  const codeShip = Number(info.codeShippable || 0);
  const codeTierLine = ["양품", "실재고", "가재고"]
    .filter((t) => codeTier[t]).map((t) => `  ${t} ${codeTier[t]}대`).join(NL);

  let html = "";
  if (info.codeRegistered) {
    // 코드로 정확히 대조됨 — 이게 믿을 수 있는 숫자다
    const tip2 = `제품코드 ${sku} 기준` + NL + `출고 가능 ${codeShip}대 / 전체 ${info.codeStock}대`
      + (codeTierLine ? NL + NL + "재고 구분" + NL + codeTierLine : "")
      + (codeProv ? NL + "※ 가재고는 매입에서 양품/실재고로 바꾸기 전엔 출고할 수 없습니다" : "");
    const c2 = codeShip <= 0 ? "chip-red" : (codeShip <= 2 ? "chip-violet" : "chip-green");
    html = `<span class="chip ${c2}" style="cursor:help;"
      title="${escapeHtml(tip2)}">재고 ${codeShip}대</span>`;
    if (codeProv) {
      html += ` <span class="chip chip-amber" style="cursor:help;"
        title="${escapeHtml(`가재고 ${codeProv}대 — 완전한 수리가 끝나야 출고할 수 있습니다.` + NL
          + `매입 화면에서 양품 또는 실재고로 바꾸세요.`)}">가재고 ${codeProv}</span>`;
    }
  } else {
    // 코드로 등록된 자산이 아직 없다 — 모델명으로 짐작한 값임을 칩 이름으로 알린다
    const why = (sku ? `제품코드 '${sku}' 로 등록된 자산이 없어 모델명 '${info.ourModel || ""}' 로 짐작한 값입니다.`
                     : `제품코드를 찾지 못해 모델명으로 짐작한 값입니다.`)
      + NL + `매입에서 자산에 제품코드를 넣으면 정확한 재고가 뜹니다.`;
    const cls = ship <= 0 ? "chip-red" : "chip-slate";
    html = `<span class="chip ${cls}" style="cursor:help;"
      title="${escapeHtml(why + NL + NL + fullTip)}">모델 ${ship}대${mixed ? " ⚠" : ""}</span>`;
  }
  // ★주문이 등급을 지정했으면 '그 등급이 몇 대인지'가 진짜 필요한 숫자다.
  //   전체 90대인데 요구 등급은 0대인 경우가 흔하다(2026-08-03 실측: 89건 중 59건).
  //   등급 대조는 모델명 기준이라 근사값이다 — 제품코드로 정확히 대조되는 동안에는
  //   숫자 두 개(정확/짐작)가 나란히 서서 헷갈리므로, 코드가 등록된 상품에는 안 붙인다.
  if (want && !info.codeRegistered) {
    const n = gr.filter((g) => normGrade(g.grade) === want).reduce((s, g) => s + g.count, 0);
    const undecided = gr.filter((g) => normGrade(g.grade) === "미정" || !normGrade(g.grade))
      .reduce((s, g) => s + g.count, 0);
    const gtip = (n > 0 ? `${want}급 재고 ${n}대` : `${want}급 재고가 없습니다`)
      + (undecided ? `\n등급 미정 ${undecided}대는 확인이 필요합니다` : "")
      + (gr.length ? "\n\n등급별\n" + gLines.join("\n") : "");
    html += ` <span class="chip ${n > 0 ? "chip-green" : "chip-red"}" style="cursor:help;"
      title="${escapeHtml(gtip)}">${escapeHtml(want)}급 ${n}대${n ? "" : " ⚠"}</span>`;
  }
  return html;
}

/* 상품명을 누르면 그 쇼핑몰의 상품으로 간다(대표 요청 2026-08-03).
   ★몰이 주는 정보가 제각각이라 두 가지로 나뉜다.
     · 고도몰(자사몰) — 설정에 자사몰 주소를 넣어 두면 goodsNo로 상품 페이지에 바로 간다.
     · 나머지 몰 — 주문에 상품ID만 있고 상품 주소 규칙을 알 수 없어 '검색'으로 연다.
       (쿠팡 vendorItemId 등은 상품 주소에 그대로 못 쓴다 — 억지 링크는 404가 된다)
   수기·전화 주문은 몰이 없으므로 링크를 만들지 않는다. */
function mallProductUrl(o, info) {
  const ch = String(o.channel || "");
  const q = encodeURIComponent(o.productName || o.productCode || "");
  if (!q) return "";
  if (ch.indexOf("고도몰") >= 0 || ch.indexOf("자사몰") >= 0 || ch.indexOf("하프북") >= 0) {
    const shop = String(state.shopUrl || "").replace(/\/+$/, "");
    const no = info && info.goodsNo ? String(info.goodsNo) : "";
    if (shop && no) return `${shop}/goods/goods_view.php?goodsNo=${encodeURIComponent(no)}`;
    if (shop) return `${shop}/goods/goods_search.php?keyword=${q}`;
    return "";                                   // 자사몰 주소를 아직 안 넣었다
  }
  if (ch.indexOf("쿠팡") >= 0) return `https://www.coupang.com/np/search?q=${q}`;
  if (ch.indexOf("스마트스토어") >= 0 || ch.indexOf("네이버") >= 0)
    return `https://search.shopping.naver.com/search/all?query=${q}`;
  if (ch.indexOf("11번가") >= 0) return `https://search.11st.co.kr/Search.tmall?kwd=${q}`;
  if (ch.indexOf("카카오") >= 0) return `https://store.kakao.com/search?q=${q}`;
  if (ch.indexOf("롯데온") >= 0) return `https://www.lotteon.com/search/search/search.ecn?render=search&q=${q}`;
  if (ch.indexOf("G마켓") >= 0 || ch.indexOf("지마켓") >= 0)
    return `https://browse.gmarket.co.kr/search?keyword=${q}`;
  if (ch.indexOf("옥션") >= 0) return `https://www.auction.co.kr/n/search?keyword=${q}`;
  if (ch.indexOf("테무") >= 0) return `https://www.temu.com/search_result.html?search_key=${q}`;
  return "";                                     // 전화·수기 등 — 갈 곳이 없다
}

/* 재고 칩('고도몰 0대' · '모델 1대') — 대표 요청(2026-08-05)으로 채널/주문 칸 아래로 옮겼다.
   ★상품 칸에 두면 칩이 접혀 행이 두꺼워지던 원인이기도 했다(실측 62→94px). */
function stockChipsHtml(o) {
  const info = (state.productInfo || {})[o.productCode] || null;
  let html = "";
  // ★'고도몰 N대'는 고도몰 API가 알려 주는 고도몰 재고다. 제품코드를 몰마다 통일한 뒤로는
  //   쿠팡·카카오 주문도 같은 코드를 쓰기 때문에, 그대로 두면 쿠팡 행에 고도몰 재고가 떠서
  //   "쿠팡에서 팔렸는데 왜 안 줄지?"가 된다(대표 지적 2026-08-04). 고도몰 행에만 보여준다.
  const isGodo = String(o.channel || "").indexOf("고도몰") >= 0;
  if (isGodo && info && info.stock !== null && info.stock !== undefined) {
    const n = Number(info.stock);
    const cls = info.soldOut || n <= 0 ? "chip-red" : (n <= 2 ? "chip-violet" : "chip-green");
    html += `<span class="chip ${cls}"
      title="고도몰에 남은 판매 수량 — 고도몰에서 팔릴 때만 줄어듭니다(다른 몰 판매와는 무관)"
      >고도몰 ${info.soldOut ? "품절" : n + "대"}</span>`;
  }
  // 창고 재고는 몰 API가 없어도 나온다(모든 쇼핑몰 공통)
  if (info && info.ourStock !== null && info.ourStock !== undefined) {
    html += " " + stockChip(info, orderGrade(o));
  }
  return html;
}

function productCell(o, canWork) {
  const info = (state.productInfo || {})[o.productCode] || null;
  const spec = info && info.spec ? info.spec : "";
  // ★'몰 N대'는 고도몰 API가 알려 주는 고도몰 재고다. 제품코드를 몰마다 통일한 뒤로는
  //   쿠팡·카카오 주문도 같은 코드를 쓰기 때문에, 그대로 두면 쿠팡 행에 고도몰 재고가 떠서
  //   "쿠팡에서 팔렸는데 왜 안 줄지?"가 된다(대표 지적 2026-08-04). 고도몰 행에만 보여준다.
  // 재고 칩은 채널/주문 칸으로 옮겼다(stockChipsHtml) — 여기서는 안 그린다.
  // ★모든 행을 딱 2줄로 고정한다(대표 결정 2026-08-03).
  //   내용 길이에 따라 행이 두꺼워지던 문제 — 옵션이 38자(중앙)에서 137자(최대)까지
  //   벌어져 행 높이가 들쭉날쭉했다. 넘치는 글자는 …으로 접고, 마우스를 올리면 전부 보인다.
  //   ★2026-08-05 대표: "가로로 너무 빽빽하다" — 한 줄에 몰아넣지 않고 세로로 푼다.
  //   1줄 = 제품코드 · 수량 · 메모   2줄 = 상품명(칸 전체)   3줄 = 스펙   4줄 = 옵션 칩
  const memoHtml = o.memo
    ? `<span class="chip chip-red" title="${escapeHtml(o.memo)}">📌 ${escapeHtml(o.memo)}</span>` : "";
  return `<td class="setup-prod">
    <div class="prod-line prod-meta">
      ${o.productCode ? `<span class="prod-code"
        title="상품코드 ${escapeHtml(o.productCode)}">${escapeHtml(o.productCode)}</span>` : ""}
      <span class="prod-qty">${o.quantity}대</span>
      ${o.isReview ? `<span class="chip chip-violet"
        title="${escapeHtml(o.reviewNote || "리뷰어 출고")}">🎁</span>` : ""}
      ${memoHtml}
    </div>
    <div class="prod-line">
      ${(() => {
        const url = mallProductUrl(o, info);
        const t = escapeHtml(o.productName);
        return url
          ? `<a class="prod-title" href="${escapeHtml(url)}" target="_blank" rel="noopener"
               title="${t}&#10;&#10;클릭하면 ${escapeHtml(o.channel || "쇼핑몰")}에서 이 상품을 엽니다">${t}</a>`
          : `<span class="prod-title" title="${t}">${t}</span>`;
      })()}
    </div>
    ${spec ? `<div class="prod-line">
      <span class="prod-spec" title="${escapeHtml(spec)}">${escapeHtml(spec)}</span>
    </div>` : ""}
    ${(o.optionName || (o.prepOptions || []).length) ? `<div class="setup-opts"
      title="${escapeHtml(o.optionName || "")}">${optionChips(o.optionName)}${prepChips(o, canWork)}</div>` : ""}
  </td>`;
}

/* 화면에 뜬 주문의 상품코드를 모아 한 번에 물어본다(코드당 1회, 서버가 10분 캐시).
   실패해도 조용히 넘어간다 — 스펙이 안 보일 뿐 작업은 계속돼야 한다. */
async function loadProductInfo(orders) {
  // 몰을 가리지 않는다 — 스펙은 고도몰만 오지만 창고 재고는 모든 몰에 필요하다
  const list = orders || [];
  const codes = [...new Set(list.map((o) => (o.productCode || "").trim()).filter(Boolean))];
  if (!codes.length) return false;
  // 스펙은 고도몰 주문 코드만 물어본다 — 쿠팡 상품ID를 고도몰에 물으면 헛호출이 된다
  const specCodes = [...new Set(list
    .filter((o) => (o.channel || "").indexOf("고도몰") >= 0)
    .map((o) => (o.productCode || "").trim()).filter(Boolean))];
  // 코드가 숫자뿐인 몰은 상품명에서 모델을 뽑아야 창고 재고가 나온다
  const names = {};
  for (const o of list) {
    const c = (o.productCode || "").trim();
    if (c && !names[c]) names[c] = o.productName || "";
  }
  const known = state.productInfo || {};
  // 아직 몰에 못 물어본 것(pending)은 '모르는 것'으로 친다 — 다음 폴링에서 다시 묻는다
  let need = codes.filter((c) => !(c in known) || known[c].pending);
  // ★창고 재고는 주기적으로 다시 물어본다. 예전에는 코드당 한 번만 묻고 끝이라
  //   스캔해서 자산을 붙여도 '창고 3대'가 그대로 남았다. 담당자가 없는 재고를
  //   있다고 믿고 진행하게 된다(2026-07-31 감사 A-4). F5를 눌러야 맞는 숫자가 나왔다.
  //   몰 스펙은 서버가 10분 캐시하므로 다시 물어도 몰 호출은 늘지 않는다 —
  //   창고 수량만 DB에서 새로 세어 온다.
  const STOCK_REFRESH_MS = 30000;
  const stale = Date.now() - (state.productInfoAt || 0) > STOCK_REFRESH_MS;
  if (stale) need = codes;
  if (!need.length) return false;                        // 이미 다 있다 — 호출하지 않는다
  try {
    const r = await api("/api/orders/product-info", {
      method: "POST", body: { mall: "godomall", codes: need, specCodes, names },
    });
    const got = r.products || {};
    state.productInfo = Object.assign({}, known, got);
    state.productInfoAt = Date.now();
    if (r.shopUrl !== undefined) state.shopUrl = r.shopUrl;   // 자사몰 주소(설정에서 입력)
    if (r.availableAssets !== null && r.availableAssets !== undefined) {
      state.availableAssets = r.availableAssets;
      renderSetupKpi(state.setupOrders || []);
    }
    // 다시 그릴 이유: ① 스펙이 새로 붙었다 ② 창고 재고 숫자가 바뀌었다
    //   (②를 빼면 자산을 붙여도 '창고 N대'가 그대로 남는다 — 감사 A-4)
    return Object.keys(got).some((c) => {
      const before = known[c], after = got[c];
      if (!after.pending && !(before && !before.pending)) return true;
      return before && before.ourStock !== after.ourStock;
    });
  } catch (_e) { return false; }
}

/* 자산 매칭 — 목록 '그 행'에서 바로 찍는다(팝업 없음, 대표 요청 2026-07-29).
   주문 수량만큼 칸이 뜨고, 스캐너로 찍으면 그 자리에서 저장된다.
   같은 주문에 남은 칸이 있으면 그 칸으로, 다 채웠으면 '다음 주문'의 첫 칸으로 커서가 넘어간다. */
function assetCell(o, canWork) {
  const need = Math.max(1, Number(o.quantity) || 1);
  const have = o.assets || [];
  // 리뷰어 출고는 제품 없이 나가는 경우가 있어 자산 매칭을 강요하지 않는다
  if (o.isReview && !have.length) {
    return `<td class="setup-assets">
      <div class="as-slots">
        <span class="chip chip-violet as-item" title="리뷰어 출고 — 자산 매칭 없이 출고할 수 있습니다">🎁 리뷰어</span>
        ${canWork ? `<input type="text" class="as-slot as-item" data-oid="${o.id}" data-idx="0"
          placeholder="📷 스캔" title="제품을 함께 보낼 때만 스캔하세요"
          style="width:104px; padding:4px 8px; border:1px solid var(--border);
          border-radius:8px; background:var(--bg);">` : ""}
      </div>
    </td>`;
  }
  // ★모든 행이 같은 높이여야 한다(대표 결정 2026-08-03). 그래서 칸을 수량만큼 늘어놓지 않는다.
  //   10대짜리 주문이면 열 줄이 되어 행이 686px까지 두꺼워졌다(2026-07-31 실측).
  //   대신 '지금 찍을 칸' 하나만 두고, 찍으면 그 자리가 다음 칸으로 넘어간다.
  //   (수량 2대 이상은 전체의 4%뿐이고, 스캐너로 연속해서 찍는 동작은 그대로다.)
  const done = have.length;
  const nextIdx = (() => {
    for (let i = 0; i < need; i++) if (!have[i]) return i;
    return -1;                                   // 다 채웠다
  })();
  const rows = [];
  // 이미 붙인 자산 — 두 개까지만 보이고 나머지는 개수로 접는다(마우스를 올리면 전부 보인다)
  const SHOW = 2;
  have.slice(0, SHOW).forEach((a) => {
    rows.push(`<span class="as-item">
      <span class="chip chip-green" title="${escapeHtml(a.model || "")}">${escapeHtml(a.assetNo)}</span>
      ${canWork ? `<button class="btn btn-ghost btn-sm" data-unmatch="${o.id}:${a.assetId}"
         title="이 자산 빼기">✕</button>` : ""}
    </span>`);
  });
  if (have.length > SHOW) {
    rows.push(`<span class="chip chip-green as-item"
      title="${escapeHtml(have.slice(SHOW).map((a) => a.assetNo).join(", "))}">+${have.length - SHOW}</span>`);
  }
  if (nextIdx >= 0 && canWork) {
    rows.push(`<span class="as-item">
      <input type="text" class="as-slot" data-oid="${o.id}" data-idx="${nextIdx}"
        placeholder="📷 스캔" title="${need > 1 ? `${done + 1}번째 / 총 ${need}대` : "관리번호를 스캔하세요"}"
        style="width:104px; padding:4px 8px; border:1px solid var(--border);
        border-radius:8px; background:var(--bg);">
    </span>`);
  } else if (nextIdx >= 0) {
    rows.push(`<span class="as-item muted" style="font-size:12px;">미매칭</span>`);
  }
  // ★스캔 실패 사유는 칸 옆에 남겨 둔다. 토스트는 몇 초 뒤 사라져서
  //   담당자는 번호를 잘못 친 줄 알고 같은 번호를 계속 다시 찍는다(2026-07-30 대표 지적).
  const err = (state.setupScanErr || {})[o.id];
  return `<td class="setup-assets">
    <div class="as-slots">
      ${rows.join("")}
      <span class="as-item muted" style="font-size:11px;">${done}/${need}대</span>
      ${canWork ? `<button class="btn btn-ghost btn-sm as-item" data-match="${o.id}"
        title="번호를 모를 때 — 검색해서 고릅니다">🔍</button>` : ""}
    </div>
    ${err ? `<div class="chip chip-red as-err" title="${escapeHtml(err)}">${escapeHtml(err)}</div>` : ""}
  </td>`;
}

/* 수취인을 누르면 성함·연락처·주소를 보여 준다(대표 요청 2026-08-03).
   ★고객정보 열람 권한이 없는 계정에는 서버가 이미 연락처를 가리고 주소를 비워서 준다.
     그 경우 '가려진 상태'임을 화면에 적어 준다 — 빈칸을 버그로 오해하지 않게. */
function showRecipient(oid) {
  const o = (state.setupOrders || []).find((x) => x.id === oid);
  const host = $("#setup-match");
  if (!o || !host) return;
  const masked = !o.address && !o.postalCode;      // 서버가 가린 상태
  const row = (label, value, extra) => `
    <div class="inline-row" style="gap:10px; align-items:flex-start; padding:6px 0;
         border-bottom:1px solid var(--border);">
      <span class="muted" style="width:64px; flex:0 0 auto;">${label}</span>
      <b style="flex:1 1 auto; white-space:normal; word-break:break-all;">${value || "-"}</b>
      ${extra || ""}
    </div>`;
  const addr = [o.postalCode ? `(${escapeHtml(o.postalCode)})` : "", escapeHtml(o.address || "")]
    .filter(Boolean).join(" ");
  host.innerHTML = `<div class="card">
    <div class="inline-row">
      <h3 style="margin:0; flex:1;">수취인 정보</h3>
      <button class="btn btn-sm" id="who-close">닫기</button>
    </div>
    ${row("성함", escapeHtml(o.recipient || ""))}
    ${row("연락처", escapeHtml(o.phone || ""),
          o.phone ? `<button class="btn btn-sm" id="who-copy-phone">복사</button>` : "")}
    ${row("주소", addr, addr ? `<button class="btn btn-sm" id="who-copy-addr">복사</button>` : "")}
    ${o.deliveryMessage ? row("배송메시지", escapeHtml(o.deliveryMessage)) : ""}
    ${row("주문", escapeHtml(`${o.channel || ""} ${o.orderNumber || ""}`.trim()))}
    ${masked ? `<p class="muted" style="margin:10px 0 0;">
      연락처 뒷자리와 주소는 <b>고객정보 열람 권한</b>이 있어야 보입니다.
      필요하면 관리자에게 요청하세요.</p>` : ""}
  </div>`;
  revealPanel(host);
  $("#who-close").addEventListener("click", () => { host.innerHTML = ""; });
  const copy = (id, text) => {
    const b = $(id);
    if (!b) return;
    b.addEventListener("click", async () => {
      try { await navigator.clipboard.writeText(text); toast("복사했습니다."); }
      catch (_e) { toast("복사가 안 되면 글자를 직접 선택해 주세요.", true); }
    });
  };
  copy("#who-copy-phone", o.phone || "");
  copy("#who-copy-addr", `${o.postalCode ? "(" + o.postalCode + ") " : ""}${o.address || ""}`);
}

/* 스캔 실패 사유를 주문별로 기억한다(다시 그려도 남아 있게). */
function setScanErr(oid, msg) {
  state.setupScanErr = state.setupScanErr || {};
  if (msg) state.setupScanErr[oid] = msg;
  else delete state.setupScanErr[oid];
  if (state.setupOrders) renderSetupRows(state.setupOrders, hasPerm("orders.work"));
}

/* 찍은 번호를 그 주문에 바로 저장한다. 저장이 끝나면 다음 빈 칸으로 커서를 옮긴다. */
async function scanAssetInto(oid, raw, box) {
  const no = (raw || "").trim();
  if (!no) return;
  const order = (state.setupOrders || []).find((x) => x.id === oid);
  if (!order) return;
  if (box) box.disabled = true;
  try {
    const found = await api("/api/orders/asset-search?q=" + encodeURIComponent(no));
    const hit = found.find((r) => r.assetNo.toUpperCase() === no.toUpperCase());
    if (!hit || !hit.available) {
      // 번호는 있는데 못 쓰는 경우와, 아예 없는 경우를 구분해 준다
      const msg = hit ? `${hit.assetNo} — ${hit.reason}`
                      : `'${no}' — 자산으로 등록되지 않은 번호입니다. 매입 등록을 먼저 하세요.`;
      // ★setScanErr가 목록을 다시 그리므로 원래 칸은 사라진다.
      //   다시 그린 뒤 같은 자리에 커서를 놓아야 연속 스캔이 끊기지 않는다.
      state.setupScanNext = { oid, after: false };
      setScanErr(oid, msg);
      toast(msg, true);
      focusNextScanSlot();
      return;
    }
    if ((order.assets || []).some((a) => a.assetId === hit.assetId)) {
      toast(`${hit.assetNo}는 이미 이 주문에 있습니다.`, true);
      if (box) { box.disabled = false; box.value = ""; box.focus(); }
      return;
    }
    const ids = (order.assets || []).map((a) => a.assetId).concat(hit.assetId);
    const updated = await api(`/api/orders/${oid}`, {
      method: "PATCH", body: { action: "assets", assetIds: ids },
    });
    state.setupScanNext = { oid, after: true };     // 다시 그린 뒤 커서를 옮길 위치
    setScanErr(oid, "");                            // 성공했으니 지난 실패 안내는 지운다
    replaceSetupOrder(updated);
    focusNextScanSlot();                            // 바로 다음 칸으로 — 연속 스캔이 끊기지 않게
    toast(`${hit.assetNo} 매칭됨`);
  } catch (err) {
    toast(err.message, true);
    state.setupScanNext = { oid, after: false };
    if (err.data && err.data.order) { replaceSetupOrder(err.data.order); focusNextScanSlot(); }
    else if (box) { box.disabled = false; box.value = ""; box.focus(); }
  }
}

/* 다시 그린 뒤 커서 자리 잡기 — 방금 찍은 주문에 남은 칸, 없으면 다음 주문의 첫 칸 */
function focusNextScanSlot() {
  const want = state.setupScanNext;
  state.setupScanNext = null;
  const slots = $$(".as-slot");
  if (!slots.length) return;
  if (!want) return;
  const same = slots.filter((s) => Number(s.dataset.oid) === want.oid);
  if (same.length) { same[0].focus(); return; }
  // 이 주문은 다 찼다 — 목록에서 그 다음에 오는 빈 칸으로
  const ids = (state.setupOrders || []).map((o) => o.id);
  const pos = ids.indexOf(want.oid);
  const next = slots.find((s) => ids.indexOf(Number(s.dataset.oid)) > pos) || slots[0];
  if (next) next.focus();
}

/* 챙길 옵션 — 별도 칸·체크 없이 그냥 옵션 칩으로 함께 보여준다(대표 2026-08-04:
   "옵션은 옵션으로써 쓰는 것"). 몰이 주문서에 안 실어 주는 것(리브레오피스·리커버리 등)을
   설정 ▸ 제공 옵션 규칙이 붙여 주면, 몰 옵션과 같은 줄에 색칩으로 나란히 선다. */
/* 챙길 옵션 — ★누를 수 있어야 한다.
   2026-08-03 표를 한 줄로 압축하면서 체크박스를 없앴는데, 서버는 여전히
   '체크 안 된 옵션이 있으면 제작완료를 막는' 상태였다. 그래서 쿠팡·카카오·전화·b2b·방문
   주문이 통째로 제작완료가 안 됐다(2026-08-05 대표 지적, 체크 기록 0건).
   행 높이를 늘리지 않으려고 별도 칸 대신 '누르면 체크되는 칩'으로 되돌린다. */
function prepChips(o, canWork) {
  return (o.prepOptions || []).map((x) => {
    const on = !!x.checked;
    const who = on && x.checkedBy ? ` · ${x.checkedBy}` : "";
    return `<button class="chip prep-chip ${on ? "chip-green" : "chip-red"}"
      data-prep="${o.id}" data-opt="${x.id}" ${canWork ? "" : "disabled"}
      title="${escapeHtml((x.note || "챙길 것") + (on ? " (체크됨" + who + ")" : " — 누르면 체크"))}"
      >${on ? "☑" : "☐"} ${escapeHtml(x.name)}</button>`;
  }).join("");
}

/* 제작 완료 → 송장 발급/미리보기 — 제작이 끝난 주문은 셋팅 화면에서 바로 송장을 뽑는다
   (대표 요청 2026-07-29: 주문수집 건과 연동해 자동으로, 송장에는 쇼핑몰/상품·코드/옵션/
    수량/자산번호와 고객 배송요청이 함께 찍힌다). */
function waybillCell(o) {
  // ★대표 2026-08-05: 제작이 끝나면 바로 송장을 뽑고, 뽑으면 '출고 확인'이 자동으로 켜진다
  if (!o.productionDone || o.cancelledAt) return "";
  // 이미 출고 마감된 건은 기록을 보는 화면이다 — 새 송장을 뽑으면 안 된다.
  // 기존 송장이 있으면 아래 분기에서 인쇄만 할 수 있게 남는다.
  const done = o.shippingDone;
  const wb = (o.waybills || []).find((w) => w.type !== "recall"
    && ["issued", "test", "pending"].includes(w.status));
  if (wb) {
    // 발급 중(pending)은 아직 라벨이 없다 — 인쇄를 누르면 오류만 뜨므로 버튼을 감춘다
    if (wb.status === "pending") {
      return `<span class="chip chip-slate wb-line"
        title="오래 걸리면 배송/송장 화면에서 취소한 뒤 다시 발급하세요">발급 중…</span>`;
    }
    const test = wb.status === "test";
    return `<span class="wb-line">
      <span class="chip ${test ? "chip-slate" : "chip-green"}"
            title="송장번호 ${escapeHtml(wb.invoiceNo || "")}">${test ? "🧪 테스트" : "🧾 발급"}</span>
      <button class="btn btn-sm" data-wbprint="${escapeHtml(wb.wid)}" title="송장 인쇄">🖨</button>
    </span>`;
  }
  // ★한 줄로 압축한다. 예전에는 박스수·미리보기·발급이 세로로 쌓여 그 행만 102px가 됐다
  //   (모든 행 같은 높이 — 대표 결정 2026-08-03). 버튼 뜻은 title로 남긴다.
  if (done) return "";                 // 나간 건에 발급·미리보기 버튼을 두지 않는다
  const canIssue = hasPerm("orders.ship") || hasPerm("waybills.manage");
  return `<span class="wb-line">
    <input type="number" class="wb-box" data-oid="${o.id}" min="1" max="10" value="1"
           title="한 송장으로 나가는 상자 수 — 2개 이상이면 송장에 「박스 N개」로 찍힙니다"
           style="width:42px; padding:3px 5px; border:1px solid var(--border);
                  border-radius:8px; background:var(--bg); font-size:12px;">
    <button class="btn btn-sm" data-wbpreview="${o.id}"
      title="송장에 무엇이 찍히는지 발급 전에 확인합니다">👁</button>
    ${canIssue ? `<button class="btn btn-sm btn-primary" data-wbissue="${o.id}"
                    title="송장 발급">🧾</button>`
               : `<span class="muted" style="font-size:11px;" title="발급은 배송 담당이 합니다">—</span>`}
  </span>`;
}

async function issueWaybillFromSetup(oid, boxQty) {
  try {
    const r = await api(`/api/orders/${oid}/waybill`, {
      method: "POST", body: { boxQty: Math.max(1, Math.min(Number(boxQty) || 1, 10)) },
    });
    toast(`송장 발급 완료 — ${r.invoiceNo || r.wid}${r.simulated ? " (테스트 발행)" : ""}`
      + " · 출고 확인으로 넘겼습니다");
    window.open(`/api/waybills/${r.wid}/pdf`, "_blank");
    $("#sf-search") && $("#sf-search").click();               // 작업보드 갱신
    const host = $("#setup-match");
    if (host) host.innerHTML = "";
  } catch (err) { toast(err.message, true); }
}

async function renderWaybillPreview(oid, boxQty) {
  const host = $("#setup-match");
  if (!host) return;
  const box = Math.max(1, Math.min(Number(boxQty) || 1, 10));
  revealPanel(host);
  host.innerHTML = `<div class="card"><p class="muted">송장 내용을 만드는 중…</p></div>`;
  let p;
  try { p = await api(`/api/orders/${oid}/waybill-preview?boxQty=${box}`); }
  catch (err) { host.innerHTML = `<div class="card"><p class="muted">${escapeHtml(err.message)}</p></div>`; return; }
  const canIssue = hasPerm("orders.ship") || hasPerm("waybills.manage");
  host.innerHTML = `
    <div class="card">
      <div class="inline-row">
        <h3 style="margin:0; flex:1;">송장 미리보기 — ${escapeHtml(p.recipient || "")}</h3>
        <button class="btn btn-sm" id="wp-close">닫기</button>
      </div>
      ${p.existing ? `<div style="background:var(--primary-soft); border-radius:8px; padding:8px 12px; margin:8px 0;">
        이미 발급된 송장이 있습니다: <b>${escapeHtml(p.existing.invoiceNo || p.existing.wid)}</b></div>` : ""}
      <div style="border:1px solid var(--border); border-radius:10px; padding:12px 14px; margin-top:10px;
                  font-family:inherit; background:var(--bg);">
        <div class="muted" style="font-size:12px;">⑯ 상품명 칸 — 셋팅·QC·포장이 이 줄로 대조합니다</div>
        <div style="margin:4px 0 10px; font-weight:600; overflow-wrap:anywhere;">${escapeHtml(p.itemSummary || "(비어 있음)")}</div>
        <div class="muted" style="font-size:12px;">⑰ 배송메세지 칸 — 고객 요청 + 챙긴 옵션</div>
        <div style="margin:4px 0 10px; overflow-wrap:anywhere;">${escapeHtml(p.remark || "(없음)")}</div>
        <div class="muted" style="font-size:12px;">받는 분</div>
        <div style="margin-top:4px;">${escapeHtml(p.recipient || "")} · ${escapeHtml(p.address || "")}</div>
        <div class="muted" style="font-size:12px; margin-top:8px;">자산 ${p.assetNos.length}대: ${escapeHtml(p.assetNos.join(", ") || "미매칭")}</div>
      </div>
      <div class="inline-row" style="margin-top:10px;">
        <label for="wp-box" style="font-size:13px;">📦 박스 수</label>
        <input type="number" id="wp-box" min="1" max="10" value="${box}"
               style="width:70px; padding:6px 8px; border:1px solid var(--border);
                      border-radius:8px; background:var(--bg);">
        <span class="muted" style="font-size:12px;">
          한 송장으로 여러 상자가 나갈 때만 올리세요 — 상품명 칸 맨 앞에 「박스 N개」로 찍힙니다.</span>
      </div>
      ${p.blockers.length ? `<div style="background:var(--danger-soft); border-radius:8px; padding:8px 12px; margin-top:8px;">
        ${p.blockers.map((b) => `<div>⚠ ${escapeHtml(b)}</div>`).join("")}</div>` : ""}
      <div class="editor-actions">
        ${canIssue && !p.existing ? `<button class="btn btn-primary" id="wp-issue" ${p.blockers.length ? "disabled" : ""}>
          🧾 이대로 발급${p.real ? "" : " (테스트 발행)"}</button>` : ""}
        ${p.existing ? `<button class="btn" id="wp-print">🖨 인쇄</button>` : ""}
      </div>
    </div>`;
  $("#wp-close").addEventListener("click", () => { host.innerHTML = ""; });
  const boxIn = $("#wp-box");
  // 바꾸면 바로 다시 그린다 — 발급 전에 종이에 뭐가 찍히는지 눈으로 확인하는 게 목적이다
  if (boxIn) boxIn.addEventListener("change", () => renderWaybillPreview(oid, boxIn.value));
  const issueBtn = $("#wp-issue");
  if (issueBtn) issueBtn.addEventListener("click", () =>
    issueWaybillFromSetup(oid, boxIn ? boxIn.value : 1));
  const printBtn = $("#wp-print");
  if (printBtn) printBtn.addEventListener("click", () =>
    window.open(`/api/waybills/${p.existing.wid}/pdf`, "_blank"));
}

/* 행 상세 — QC 프로그램(192.168.0.185:3000)과 같은 4칸 구성
   (대표 요청 2026-08-05): 상품 정보 / 결제 정보 / 구매자·배송지 / 처리 이력. */
function setupDetailRow(o) {
  const info = (state.productInfo || {})[o.productCode] || null;
  const st = orderStatusInfo(o);
  const line = (label, val) => `<div class="dt-line"><span class="dt-k">${label}</span>
    <span class="dt-v">${val === "" || val === null || val === undefined
      ? '<span class="muted">-</span>' : val}</span></div>`;

  // 처리 이력 — 4단계를 순서대로. 끝난 것은 담당자·시각까지.
  const steps = [
    ["준비 중", o.preparing, o.preparingBy, o.preparingAt],
    ["제작 완료", o.productionDone, o.productionBy, o.productionAt],
    ["출고 확인", o.softwareInspectionDone, o.softwareInspectionBy, o.softwareInspectionAt],
    ["출고 확인", o.shippingDone, o.shippingBy, o.shippingAt],
  ].map(([name, done, by, at]) => `
    <div class="dt-step${done ? " on" : ""}">
      <span class="dt-dot"></span>
      <div><b>${name}</b>
        <div class="muted" style="font-size:12px;">${done
          ? escapeHtml((by || "") + (at ? " · " + String(at).replace("T", " ").slice(0, 16) : ""))
          : "대기"}</div></div>
    </div>`).join("");

  return `<tr class="setup-detail"><td colspan="8">
    <div class="dt-grid">
      <div class="dt-card">
        <div class="dt-head">상품 정보 <span class="chip ${st.chip}">${st.label}</span></div>
        <b style="display:block; margin-bottom:6px;">${escapeHtml(o.productName || "-")}</b>
        ${o.productCode ? `<div class="muted" style="font-size:12.5px;">등록옵션명: ${escapeHtml(o.productCode)}</div>` : ""}
        <div style="margin:6px 0;">${optionChips(o.optionName)}</div>
        ${info && info.spec ? `<div class="muted" style="font-size:12.5px;">${escapeHtml(info.spec)}</div>` : ""}
        ${line("수량", `${o.quantity}개`)}
        ${line("재고", stockChipsHtml(o) || '<span class="muted">-</span>')}
        ${o.memo ? line("내부 메모", escapeHtml(o.memo)) : ""}
      </div>
      <div class="dt-card">
        <div class="dt-head">결제 정보</div>
        ${line("결제금액", `<b>${fmtWon(o.amount)}</b>`)}
        ${line("주문일", escapeHtml((o.orderedAt || "").replace("T", " ").slice(0, 19)))}
        ${line("채널", escapeHtml(o.channel || ""))}
        ${line("수령방식", escapeHtml(o.receiveMethod || "택배"))}
        ${o.orderNumber ? line("주문번호", escapeHtml(o.orderNumber)) : ""}
      </div>
      <div class="dt-card">
        <div class="dt-head">구매자 / 배송지${o.piiMasked
          ? ' <span class="muted" style="font-weight:400;">일부 가림</span>' : ""}</div>
        ${line("수령인", escapeHtml(o.recipient || ""))}
        ${line("연락처", escapeHtml(o.phone || ""))}
        ${line("주소", escapeHtml([o.postalCode, o.address].filter(Boolean).join(" ")))}
        ${line("배송메시지", escapeHtml(o.deliveryMessage || ""))}
        ${line("자산번호", (o.assets || []).length
          ? (o.assets || []).map((a) => escapeHtml(a.assetNo)).join(", ") : "")}
      </div>
      <div class="dt-card">
        <div class="dt-head">처리 이력</div>
        ${steps}
      </div>
    </div>
  </td></tr>`;
}

function renderSetupRows(orders, canWork) {
  const host = $("#setup-rows");
  if (!host) return;
  // ★수백 줄을 한 번에 그리면 느리고 스크롤이 끝없다 — 잘라서 화살표로 넘긴다
  const pg = paged(orders, "setup");
  const bar = $("#setup-pager");
  if (bar) {
    bar.innerHTML = pg.bar;
    wirePager(bar, "setup", () => renderSetupRows(orders, canWork));
  }
  host.innerHTML = pg.rows.map((o) => {
    const st = orderStatusInfo(o);
    return `<tr>
      <td class="setup-chan">${chBadge(o.channel)}${receiveChip(o)}<span class="chan-no">${escapeHtml(o.orderNumber || `#${o.id}`)}</span>
        <div class="chan-stock">${stockChipsHtml(o)}</div>
        <button class="btn btn-sm chan-more" data-more="${o.id}">${
          state.setupOpen && state.setupOpen[o.id] ? "상세 닫기" : "상세 보기"}</button></td>
      ${productCell(o, canWork)}
      <td class="setup-recipient"><button class="link-btn" data-who="${o.id}"
        title="누르면 성함·연락처·주소를 봅니다">${escapeHtml(o.recipient)}</button>${
        // ★배송 메시지는 작업 전에 봐야 하는 정보다(대표 2026-08-05).
        //   주소는 자리를 너무 먹어 [상세 보기]에만 둔다.
        o.deliveryMessage ? `<div class="recv-msg" title="${escapeHtml(o.deliveryMessage)}"
          >💬 ${escapeHtml(o.deliveryMessage)}</div>` : ""}</td>
      ${assetCell(o, canWork)}
      ${stageCell(o, "preparing", o.preparing, o.preparingBy && o.preparing ? o.preparingBy : "", canWork, o.preparingAt)}
      ${stageCell(o, "production", o.productionDone, o.productionBy, canWork, o.productionAt)}
      ${stageCell(o, "softwareInspection", o.softwareInspectionDone, o.softwareInspectionBy, canWork, o.softwareInspectionAt)}
      <td class="setup-status"><span class="chip ${st.chip}">${st.label}</span>${
        shipDoneLine(o)}${qcShippedChip(o)}${waybillCell(o)}</td>
    </tr>${state.setupOpen && state.setupOpen[o.id] ? setupDetailRow(o) : ""}`;
  }).join("") || `<tr><td colspan="8" class="muted">진행 중인 주문이 없습니다.</td></tr>`;

  // 상세 보기 — 행 아래에 4칸(상품/결제/구매자·배송지/처리이력)을 편다.
  // ★행을 다시 그리는 건 이 함수라 여기서 바로 갱신한다(폴링과 충돌하지 않게).
  $$("button[data-more]", host).forEach((b) => b.addEventListener("click", () => {
    const oid = Number(b.dataset.more);
    state.setupOpen = state.setupOpen || {};
    if (state.setupOpen[oid]) delete state.setupOpen[oid];
    else state.setupOpen[oid] = true;
    renderSetupRows(orders, canWork);
  }));
  // 챙길 옵션 칩 — 누르면 체크/해제. 이게 없으면 제작완료가 영영 안 눌린다.
  $$("button[data-prep]", host).forEach((b) => b.addEventListener("click", async () => {
    const oid = Number(b.dataset.prep);
    const optId = Number(b.dataset.opt);
    const on = b.classList.contains("chip-green");
    b.disabled = true;
    try {
      await api(`/api/orders/${oid}/options/${optId}`, {
        method: "POST", body: { checked: !on } });
      // renderSetupRows는 load() 범위 밖이라 조회 버튼으로 새로 그린다(다른 곳과 같은 방식)
      const btn = $("#sf-search");
      if (btn) btn.click();
    } catch (err) {
      toast(err.message, true);
      b.disabled = false;
    }
  }));
  $$("input[data-stage]", host).forEach((cb) => cb.addEventListener("change", async () => {
    const oid = Number(cb.dataset.oid);
    cb.disabled = true;
    try {
      const updated = await api(`/api/orders/${oid}`, {
        method: "PATCH", body: { action: cb.dataset.stage, value: cb.checked },
      });
      replaceSetupOrder(updated);
    } catch (err) {
      toast(err.message, true);
      if (err.data && err.data.order) replaceSetupOrder(err.data.order);
      else cb.checked = !cb.checked;
    } finally { cb.disabled = false; }
  }));
  $$("button[data-match]", host).forEach((b) => b.addEventListener("click", () => {
    renderMatchPanel(Number(b.dataset.match));
  }));
  $$("button[data-who]", host).forEach((b) => b.addEventListener("click", () => {
    showRecipient(Number(b.dataset.who));
  }));
  // 행 안에서 바로 스캔 — 스캐너가 번호를 치고 Enter를 보낸다
  $$("input.as-slot", host).forEach((box) => {
    box.addEventListener("keydown", (e) => {
      if (e.key !== "Enter") return;
      e.preventDefault();
      const v = box.value; box.value = "";
      scanAssetInto(Number(box.dataset.oid), v, box);
    });
    box.addEventListener("blur", () => {
      const v = box.value.trim();
      if (v) { box.value = ""; scanAssetInto(Number(box.dataset.oid), v, box); }
    });
  });
  // 매칭에서 뺀 자산 — 즉시 반영
  $$("button[data-unmatch]", host).forEach((b) => b.addEventListener("click", async () => {
    const [oid, aid] = b.dataset.unmatch.split(":").map(Number);
    const order = (state.setupOrders || []).find((x) => x.id === oid);
    if (!order) return;
    b.disabled = true;
    try {
      const updated = await api(`/api/orders/${oid}`, {
        method: "PATCH",
        body: { action: "assets", assetIds: (order.assets || [])
          .filter((a) => a.assetId !== aid).map((a) => a.assetId) },
      });
      replaceSetupOrder(updated);
    } catch (err) {
      toast(err.message, true);
      if (err.data && err.data.order) replaceSetupOrder(err.data.order);
      else b.disabled = false;
    }
  }));
  focusNextScanSlot();
  const boxOf = (oid) => {
    const el = $(`.wb-box[data-oid="${oid}"]`, host);
    return el ? el.value : 1;
  };
  $$("button[data-wbpreview]", host).forEach((b) => b.addEventListener("click", () =>
    renderWaybillPreview(Number(b.dataset.wbpreview), boxOf(b.dataset.wbpreview))));
  $$("button[data-wbissue]", host).forEach((b) => b.addEventListener("click", () =>
    issueWaybillFromSetup(Number(b.dataset.wbissue), boxOf(b.dataset.wbissue))));
  $$("button[data-wbprint]", host).forEach((b) => b.addEventListener("click", () =>
    window.open(`/api/waybills/${b.dataset.wbprint}/pdf`, "_blank")));
}

function replaceSetupOrder(updated) {
  const idx = (state.setupOrders || []).findIndex((x) => x.id === updated.id);
  if (idx >= 0) state.setupOrders[idx] = updated;
  renderSetupRows(state.setupOrders, hasPerm("orders.work"));
  renderSetupKpi(state.setupOrders);
}

/* ---------------- 자산 매칭 패널 ----------------
   주문 수량만큼 칸을 만들고, 스캐너로 찍으면 자동으로 다음 칸으로 넘어간다
   (대표 요청 2026-07-29: "수량만큼 떠야 하고, 바코드 찍으면 자동 엔터니 다음 건으로 이동").
   QR/바코드 스캐너는 번호를 입력한 뒤 Enter를 보내므로, Enter를 '다음 칸' 신호로 쓴다. */

function renderMatchPanel(oid) {
  const host = $("#setup-match");
  const o = (state.setupOrders || []).find((x) => x.id === oid);
  if (!host || !o) return;
  const need = Math.max(1, Number(o.quantity) || 1);         // 이 주문에 나갈 대수
  // 칸을 수량만큼 만들고, 이미 매칭된 자산은 앞 칸부터 채워 둔다
  const slots = Array.from({ length: need }, (_, i) => {
    const a = o.assets[i];
    return a ? { assetId: a.assetId, assetNo: a.assetNo, model: a.model } : null;
  });
  // 수량보다 많이 매칭돼 있으면(주문 수량이 나중에 줄었을 때) 뒤에 덧붙여 보존한다
  o.assets.slice(need).forEach((a) =>
    slots.push({ assetId: a.assetId, assetNo: a.assetNo, model: a.model }));

  const filled = () => slots.filter(Boolean);
  const firstEmpty = () => slots.findIndex((x) => !x);

  const draw = (focusIndex) => {
    revealPanel(host);
    const done = filled().length;
    host.innerHTML = `
    <div class="card">
      <div class="inline-row">
        <h3 style="margin:0; flex:1;">자산 매칭 — ${escapeHtml(o.productName)} (${escapeHtml(o.recipient)})</h3>
        <button class="btn btn-sm" id="mt-close">닫기</button>
      </div>
      <p class="muted">이 주문은 <b>${need}대</b>입니다. 칸마다 관리번호를 스캔하세요 —
        찍으면 자동으로 다음 칸으로 넘어갑니다. 송장 상품명에 이 관리번호가 인쇄됩니다.</p>
      <div style="margin:10px 0;">
        ${slots.map((a, i) => `
          <div class="inline-row" style="margin-bottom:6px;">
            <span class="chip ${a ? "chip-green" : "chip-slate"}" style="min-width:56px; text-align:center;">
              ${i < need ? `${i + 1} / ${need}` : "추가"}</span>
            ${a ? `<b style="flex:1;">${escapeHtml(a.assetNo)}</b>
                   <span class="muted">${escapeHtml(a.model || "")}</span>
                   <button class="btn btn-ghost btn-sm" data-clear="${i}" title="이 칸 비우기">✕</button>`
                : `<input type="text" class="mt-slot" data-slot="${i}"
                     placeholder="📷 ${i + 1}번째 관리번호를 스캔하세요"
                     style="flex:1; min-width:200px; padding:8px 10px; border:1px solid var(--border);
                            border-radius:8px; background:var(--bg);">`}
          </div>`).join("")}
      </div>
      <p class="muted" id="mt-scan-msg" style="min-height:18px;"></p>
      <details style="margin:6px 0;">
        <summary class="muted" style="cursor:pointer;">여러 개 한 번에 붙여넣기 / 검색으로 찾기</summary>
        <div style="padding:8px 0;">
          <textarea id="mt-paste" rows="3" placeholder="관리번호를 줄바꿈이나 쉼표로 구분해 붙여넣으세요"
            style="width:100%; padding:8px; border:1px solid var(--border); border-radius:8px; background:var(--bg);"></textarea>
          <div class="inline-row" style="margin-top:6px;">
            <button class="btn btn-sm" id="mt-paste-go">붙여넣은 번호 추가</button>
            <span style="flex:1"></span>
            <input type="text" id="mt-q" placeholder="관리번호/모델/시리얼 검색 (2자 이상)" style="min-width:240px;">
            <button class="btn btn-sm" id="mt-search">검색</button>
          </div>
        </div>
      </details>
      <div id="mt-results"></div>
      <div class="editor-actions">
        <button class="btn btn-primary" id="mt-save">저장 (${done}/${need}대)</button>
        ${done < need ? `<span class="muted" style="align-self:center;">아직 ${need - done}대 남았습니다</span>` : ""}
      </div>
    </div>`;

    $("#mt-close").addEventListener("click", () => { host.innerHTML = ""; });
    $$("button[data-clear]", host).forEach((b) => b.addEventListener("click", () => {
      const i = Number(b.dataset.clear);
      if (i < need) slots[i] = null; else slots.splice(i, 1);
      draw(i);
    }));

    // 한 칸에 찍으면 검증 후 채우고, 다음 빈 칸으로 커서를 옮긴다
    const putAt = async (index, raw) => {
      const no = (raw || "").trim();
      if (!no) return;
      const fail = (text) => {
        const m = $("#mt-scan-msg");
        if (m) { m.textContent = text; m.style.color = "var(--danger)"; }
        const box = $(`.mt-slot[data-slot="${index}"]`);
        if (box) { box.value = ""; box.focus(); }
      };
      try {
        const hit = (await api("/api/orders/asset-search?q=" + encodeURIComponent(no)))
          .find((r) => r.assetNo.toUpperCase() === no.toUpperCase());
        if (!hit) return fail(`'${no}' — 자산으로 등록되지 않은 번호입니다.`);
        // ★상태가 안 되는 자산은 그대로 막는다(A/S 회수품이 판매 재고로 섞이면 남의 물건을 판다).
        //   다만 '왜 안 되는지'는 알려 준다 — 예전엔 아무 말이 없어 번호를 계속 다시 찍었다.
        if (!hit.available) return fail(`${hit.assetNo} — ${hit.reason}`);
        if (slots.some((x) => x && x.assetId === hit.assetId))
          return fail(`${hit.assetNo}는 이미 이 주문에 있습니다.`);
        if (index < slots.length) slots[index] = { assetId: hit.assetId, assetNo: hit.assetNo, model: hit.model };
        else slots.push({ assetId: hit.assetId, assetNo: hit.assetNo, model: hit.model });
        draw(firstEmpty());                       // 다시 그리면서 다음 빈 칸으로 커서 이동
        const m = $("#mt-scan-msg");
        if (m) { m.textContent = `${hit.assetNo} 추가됨`; m.style.color = ""; }
      } catch (err) { toast(err.message, true); }
    };

    $$(".mt-slot", host).forEach((box) => {
      box.addEventListener("keydown", (e) => {
        if (e.key !== "Enter") return;
        e.preventDefault();                       // 스캐너의 Enter가 폼을 보내지 않게
        const v = box.value; box.value = "";
        putAt(Number(box.dataset.slot), v);
      });
      // Enter를 안 보내는 스캐너 대비 — 칸을 벗어날 때 확인한다
      box.addEventListener("blur", () => {
        const v = box.value.trim();
        if (v) { box.value = ""; putAt(Number(box.dataset.slot), v); }
      });
    });

    // 커서: 방금 채운 다음 칸 → 없으면 첫 빈 칸
    const want = (focusIndex != null && focusIndex >= 0) ? focusIndex : firstEmpty();
    const target = $(`.mt-slot[data-slot="${want}"]`) || $(".mt-slot", host);
    if (target) target.focus();

    const addNos = async (nos) => {
      for (const raw of nos.map((x) => x.trim()).filter(Boolean)) {
        const i = firstEmpty();
        await putAt(i >= 0 ? i : slots.length, raw);
      }
    };
    $("#mt-paste-go").addEventListener("click", () => {
      const box = $("#mt-paste");
      const nos = box.value.split(/[\s,;]+/);
      box.value = "";
      addNos(nos);
    });

    const search = async () => {
      const q = $("#mt-q").value.trim();
      if (q.length < 2) { toast("2자 이상 입력하세요.", true); return; }
      try {
        const rows = await api("/api/orders/asset-search?q=" + encodeURIComponent(q));
        $("#mt-results").innerHTML = rows.length ? `<div class="table-wrap"><table>
          <thead><tr><th>관리번호</th><th>모델</th><th>스펙</th><th>등급</th><th>상태</th><th>위치</th><th></th></tr></thead>
          <tbody>${rows.map((r) => `
            <tr><td><b>${escapeHtml(r.assetNo)}</b></td><td>${escapeHtml([r.maker, r.model].filter(Boolean).join(" "))}</td>
            <td class="muted">${escapeHtml(r.spec || "")}</td>
            <td>${escapeHtml(r.grade)}</td>
            <td>${escapeHtml(r.statusLabel)}${r.available ? "" :
              `<div class="muted" style="font-size:11px; white-space:normal;">${escapeHtml(r.reason)}</div>`}</td>
            <td>${escapeHtml(r.location || "-")}</td>
            <td><button class="btn btn-sm" data-add="${r.assetId}" data-no="${escapeHtml(r.assetNo)}"
              ${!r.available || slots.some((c) => c && c.assetId === r.assetId) ? "disabled" : ""}
              >추가</button></td></tr>`).join("")}
          </tbody></table></div>` : `<p class="muted">검색 결과가 없습니다. 자산으로 등록되지 않은 번호입니다.</p>`;
        $$("button[data-add]", host).forEach((b) => b.addEventListener("click", () => {
          const i = firstEmpty();
          putAt(i >= 0 ? i : slots.length, b.dataset.no);
        }));
      } catch (err) { toast(err.message, true); }
    };
    $("#mt-search").addEventListener("click", search);
    autoSearch("#mt-q", search);

    $("#mt-save").addEventListener("click", async () => {
      try {
        const updated = await api(`/api/orders/${oid}`, {
          method: "PATCH", body: { action: "assets", assetIds: filled().map((c) => c.assetId) },
        });
        toast("자산 매칭을 저장했습니다.");
        host.innerHTML = "";
        replaceSetupOrder(updated);
      } catch (err) {
        toast(err.message, true);
        if (err.data && err.data.order) replaceSetupOrder(err.data.order);
      }
    });
  };
  draw();
}
