/* OWS 셋팅 — 제품 준비/QC 실시간 작업보드 (기존 order-workflow 기능 이식).
   누가 어떤 주문을 준비 중인지 실시간 표시: 5초 폴링 + 담당자 클레임(409) + 포커스 갱신 */
"use strict";

function renderSetupView(main) {
  if (!canSeeMenu("setup")) {
    main.innerHTML = `<h1 class="page-title">셋팅</h1><div class="card placeholder"><p>셋팅 메뉴 접근 권한이 없습니다.</p></div>`;
    return;
  }
  state.setupSubtab = state.setupSubtab || "orders";
  main.innerHTML = `
    <h1 class="page-title">셋팅 <span class="muted" style="font-size:14px;">제품 준비 · QC 작업보드</span></h1>
    <div class="tabs">
      <button data-setup-tab="orders" class="${state.setupSubtab === "orders" ? "active" : ""}">셋팅</button>
      <button data-setup-tab="prebuild" class="${state.setupSubtab === "prebuild" ? "active" : ""}">선제작</button>
    </div>
    <div id="setup-subtab"></div>`;
  $$("button[data-setup-tab]", main).forEach((b) => b.addEventListener("click", () => {
    state.setupSubtab = b.dataset.setupTab;
    renderSetupView(main);
  }));
  const host = $("#setup-subtab", main);
  if (state.setupSubtab === "prebuild") renderPrebuildTab(host);
  else renderOrderSetupTab(host);
}

function renderOrderSetupTab(main) {
  // 셋팅 전용 사용자(setup.view)도 들어올 수 있어야 한다 — orders.view만 보면 안 된다
  if (!canSeeMenu("setup")) {
    main.innerHTML = `<h1 class="page-title">셋팅</h1><div class="card placeholder"><p>셋팅 메뉴 접근 권한이 없습니다.</p></div>`;
    return;
  }
  const canWork = hasPerm("orders.work");
  main.innerHTML = `
    <p class="page-desc">체크한 사람이 담당자로 기록됩니다. 다른 사람이 준비 중인 주문은 잠깁니다. (5초 자동 동기화)</p>
    <div id="setup-stock-warn"></div>
    <div id="setup-shipped-warn"></div>
    <div id="setup-rental-warn"></div>
    <div id="setup-qcwatch-warn"></div>
    <div id="setup-wbmode"></div>
    <div class="kpi-row" id="setup-kpi"></div>
    <div id="setup-parts-bar"></div>
    <div id="setup-notice"></div>
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
          title="SW 검수 완료까지 끝난 주문을 오늘 출고 확인으로 넘기고 준비 목록에서 내립니다">🚚 금일 출고 확인</button>
        <button class="btn btn-sm" id="sf-wbprint-all"
          style="display:${WB_PRINT_STAGES.includes(state.setupStage) ? "" : "none"};"
          title="지금 화면에 보이는 목록(필터·검색 반영)의 발급된 송장을 화면 순서 그대로 한 PDF로 모아 인쇄합니다 — 건마다 그 고객의 송장번호가 붙습니다">🖨 송장 일괄 인쇄</button>
        <button class="btn btn-sm" id="sf-stats" title="누가 몇 대를 셋팅했는지 — 일·주·월·분기·연도">📅 셋팅 실적</button>
        <span class="muted" id="setup-sync"></span>
      </div>
      <div id="setup-tag-filters" class="setup-tag-filters"></div>
      <div class="table-wrap"><table class="setup-table">
        <colgroup>
          <col style="width:34px;">
          <col class="col-chan"><col><col class="col-recipient"><col class="col-assets">
          <col class="col-stage"><col class="col-stage"><col class="col-stage"><col class="col-stage">
          <col class="col-wb"><col class="col-status">
        </colgroup>
        <thead><tr>
          <th style="width:34px;" title="전체 선택 — 고른 건만 송장이 인쇄됩니다">
            <input type="checkbox" id="sf-all"></th>
          <th>채널 / 주문</th><th>상품</th><th>수취인</th><th>자산번호</th>
          <th class="stage-th">준비 중</th><th class="stage-th">제작 완료</th><th class="stage-th">SW 검수 완료</th><th class="stage-th">출고 확인</th><th title="셋팅라벨은 어느 단계에서나, 송장은 출고 확인 뒤에">송장 / 라벨</th><th>상태</th>
        </tr></thead>
        <tbody id="setup-rows" class="setup-rows-fixed"><tr><td colspan="11" class="muted">불러오는 중…</td></tr></tbody>
      </table></div>
      <div id="setup-pager"></div>
    </div>
    <div id="setup-match"></div>`;

  renderRentalWarn();           // 렌탈(RMS) 주문이 섞여 있으면 알려 준다
  renderPartsBar();             // 램·SSD 가용 수량(대표 2026-08-25) — 맨 상단
  renderSetupNotice();          // 필수 참고 사항(대표 2026-08-26) — 부품 수량 아래

  /* ★QC 프로그램(127.0.0.1:3000) 실시간 조회를 여기서 다 뗐다 — 2026-08-07 대표 지시.
       "셋팅 및 QC는 OWS를 바로 관련 사람들이 사용할 예정이니까. 앞으로 OWS에서 데이터가 쌓일거야."

     떼어낸 것: QC 출고 대조 배너(renderShippedWarn) · QC 폴더 연결 경고(renderQcWatchWarn)
                · 행에 붙던 '⚠ QC 출고됨' 칩(/api/qc-shipped/flags)

     이제 이 보드가 원본이다. 남의 프로그램을 실시간으로 들여다보면, 여기서 한 일이
     그쪽 파일에 밀려 되살아난다(2026-08-05에 실제로 그랬다).

     함수와 라우트는 지우지 않았다 — 마지막 중복 정리가 한 번 더 필요할 때
     [설정 ▸ 데이터 이관]에서 손으로 부르면 된다. */

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
      //   OWS에서는 이 보기가 그 이력 화면이 된다.
      const mode = ($("#sf-view") || {}).value || "todo";
      // ★끝난 주문은 대부분 '보관'까지 돼 있어 view=active로는 안 잡힌다
      //   (실측 출고완료 700건 중 699건이 보관됨). 완료 보기에서는 보관분까지 가져온다.
      const all = await fetchOrders({ view: mode === "todo" ? "active" : "all", q });
      const orders = mode === "shipped"
        ? all.filter((o) => o.shippingDone && !o.cancelledAt)
        : mode === "all"
          ? all.filter((o) => !o.cancelledAt)    // 취소만 빼고 다 본다
          // ★취소된 주문은 어느 보기에서도 안 보인다(2026-09-03 대표 "모든 API 기준 주문
          //   취소 건에 대해서 셋팅/QC탭에서 보이지 않게"). 예전에는 기본 보기가 취소를
          //   안 걸러서, 몰에서 취소된 주문이 계속 작업 목록에 남아 있었다.
          // ★출고 확인만 눌렀다고 목록에서 내리지 않는다(2026-09-03 대표 "출고 확인 누르면
          //   출고확인으로 넘어가게만"). 송장을 이 단계에서 뽑게 바뀌었으니, 출고 확인된
          //   건이 사라지면 정작 송장을 뽑을 자리가 없어진다 — **송장이 나가야** 내린다.
          //   ★내려가는 문은 배송완료다(inShippingBucket) — 대표 2026-09-04.
          : all.filter((o) => !o.cancelledAt && (!o.shippingDone || inShippingBucket(o)));
      if (seq !== state.renderSeq || !$("#setup-rows")) return;
      state.setupOrders = orders;
      renderSetupTagFilters(orders, canWork);
      renderSetupRows(setupOrder(filterSetupOrders(orders)), canWork);
      renderWaybillMode();
      renderSetupKpi(orders);           // 숫자는 항상 전체 기준 — 걸러도 개수는 그대로 보인다
      // 스펙·재고는 몰에서 가져온다(캐시가 있으면 호출 없음). 새로 받았을 때만 다시 그린다.
      loadProductInfo(orders).then((changed) => {
        if (changed && state.view === "setup" && !isEditingInput()) {
          renderSetupRows(setupOrder(filterSetupOrders(state.setupOrders)), canWork);
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
    // ★대상은 **지금 화면에 보이는 것**이다 — 고른 게 있으면 고른 것만.
    //   옆의 [🖨 송장 일괄 인쇄]와 같은 규칙이라야 한 줄에 있는 두 버튼이 같게 동작한다.
    //   예전에는 화면 필터를 무시하고 조건에 맞는 주문을 **전량** 마감했다(서버가
    //   ids 를 안 받으면 전체를 집었다). 단계 카드로 목록을 좁혀 놓고 눌러도
    //   화면 밖 건까지 함께 나가, 안내문 건수와 화면 줄 수가 어긋났다.
    const picked = new Set([...$$(".sf-pick")].filter((c) => c.checked)
      .map((c) => Number(c.dataset.oid)));
    let list = setupOrder(filterSetupOrders(state.setupOrders || []));
    if (picked.size) list = list.filter((o) => picked.has(o.id));
    // 마감 대상 = SW 검수까지 끝났고 아직 출고 확인 전인 건(서버 조건과 같다)
    const targets = list.filter((o) => o.softwareInspectionDone && !o.shippingDone
      && !o.cancelledAt);
    if (!targets.length) {
      toast(picked.size
        ? "고른 건 중 SW 검수 완료까지 끝난 주문이 없습니다."
        : "지금 목록에 SW 검수 완료까지 끝난 주문이 없습니다.", true);
      return;
    }
    const noWaybill = targets.filter((o) => (o.receiveMethod || "택배") === "택배"
      && !hasIssuedWaybill(o) && !(o.trackingNumber || "").trim()).length;
    const noAsset = targets.filter((o) => !(o.assets || []).length).length;
    let warn = noWaybill
      ? `\n\n⚠ 그중 ${noWaybill}건은 택배인데 송장번호가 없습니다 — 추적이 안 됩니다.` : "";
    // ★자산 미매칭 경고(2026-08-10 대표 승인) — 이대로 마감하면 어느 기계가
    //   나갔는지 자산 이력에 안 남는다. 막지는 않고 알려만 준다(소프트 경고).
    if (noAsset) {
      warn += `\n\n⚠ 그중 ${noAsset}건은 자산번호(관리번호) 매칭이 없습니다`
        + ` — 어느 기계가 나갔는지 이력에 남지 않습니다.`
        + `\n   작업보드의 🔍 버튼으로 자산을 매칭한 뒤 마감하는 것을 권장합니다.`;
    }
    if (!confirm(`${picked.size ? "고른 " : "지금 목록의 "}${targets.length}건을 `
      + `오늘 출고 확인으로 넘깁니다.\n`
      + "[출고 확인] 칸으로 옮겨 가고, 배송완료되면 [📋 출고 기록 조회]로 넘어갑니다."
      + warn + "\n\n진행할까요?")) return;
    try {
      const r = await api("/api/orders/ship-today",
        { method: "POST", body: { ids: targets.map((o) => o.id) } });
      toast(`${r.shipped}건 출고 확인했습니다.`);
      resetPage("setup");
      load(true);
    } catch (e) { toast(e.message, true); }
  });
  // ★[🖨 송장 일괄 인쇄](대표 2026-08-31 "출고확인까지 온 건들 송장 일괄/개별 출력").
  //   지금 화면에 보이는 목록의 발급 송장을 화면 순서대로 한 PDF로 합쳐 인쇄한다 —
  //   서버 병합(/api/waybills/print)이라 건마다 자기 송장번호가 그대로 붙는다.
  //   fetch 뒤 window.open 은 팝업 차단에 걸리므로 숨은 iframe 인쇄(라벨과 같은 방식).
  $("#sf-wbprint-all").addEventListener("click", async () => {
    // ★고른 게 있으면 고른 것만, 하나도 안 골랐으면 화면에 보이는 전부(2026-09-03 대표
    //   "출고확인쪽에서 전체 선택 후 송장 출력"). 맨 위 체크박스로 한 번에 고를 수 있다.
    const picked = new Set([...$$(".sf-pick")].filter((c) => c.checked)
      .map((c) => Number(c.dataset.oid)));
    let list = setupOrder(filterSetupOrders(state.setupOrders || []));
    if (picked.size) list = list.filter((o) => picked.has(o.id));
    // ★SW 검수 완료 탭에서는 '발급 + 인쇄'(대표 2026-09-08 "송장을 뽑지 않았던 애들에게 송장 넣어서
    //   실제 출력이 전부 되어야 해 — 이미 출력했던 애들은 제외"). 출고 확인·출고 기록에서는
    //   예전처럼 발급된 송장을 다시 인쇄한다.
    if (state.setupStage === "inspected") { await bulkIssueAndPrint(list, picked.size); return; }
    const wids = [];
    let noWb = 0;
    for (const o of list) {
      if (o.cancelledAt) continue;
      const wbs = (o.waybills || []).filter((w) => w.type !== "recall"
        && ["issued", "test"].includes(w.status));
      if (wbs.length) wids.push(...wbs.map((w) => w.wid));
      else noWb++;
    }
    if (!wids.length) {
      toast(picked.size
        ? "고른 건에 발급된 송장이 없습니다 — 행의 [🧾 송장]으로 먼저 발급하세요."
        : "인쇄할 발급 송장이 없습니다 — 먼저 행의 [🧾 송장]으로 발급하세요.", true);
      return;
    }
    if (wids.length > 100) {
      toast(`한 번에 100장까지 인쇄할 수 있습니다 — 지금 ${wids.length}장. 단계·검색 필터로 줄여 주세요.`, true);
      return;
    }
    if (!confirm(`${picked.size ? `고른 ${picked.size}건의 ` : ""}발급된 송장 ${wids.length}장을 한 번에 인쇄합니다`
      + `${noWb ? ` (송장 없는 ${noWb}건은 건너뜁니다)` : ""}.\n계속할까요?`)) return;
    const btn = $("#sf-wbprint-all");
    btn.disabled = true;
    try {
      const res = await fetch("/api/waybills/print", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ wids }),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.description || err.message || "송장 인쇄에 실패했습니다.");
      }
      printPdfBlob(await res.blob());
      toast(`송장 ${wids.length}장을 인쇄창으로 보냈습니다.`);
    } catch (e) { toast(e.message, true); }
    finally { btn.disabled = false; }
  });
  // 맨 위 체크박스 = 지금 화면의 (인쇄 가능한) 행 전부 고르기/풀기
  // ★송장 일괄 인쇄는 '출고 확인'·'출고 기록' 단계에서만 쓴다(2026-09-03 대표) —
  //   제작 대기~SW 검수 완료 목록에서 누르면 뽑을 송장이 없거나 남의 건까지 섞인다.
  const syncWbPrintBtn = () => {
    const b = $("#sf-wbprint-all");
    if (b) b.style.display = WB_PRINT_STAGES.includes(state.setupStage) ? "" : "none";
    syncWbPrintLabel();
  };
  syncWbPrintBtn();
  $("#sf-all").addEventListener("change", (e) => {
    $$(".sf-pick").forEach((c) => { if (!c.disabled) c.checked = e.target.checked; });
  });
  // 행을 하나라도 풀면 머리 체크도 풀린다(반대로 전부 고르면 켜진다)
  $("#setup-rows").addEventListener("change", (e) => {
    if (!e.target.classList.contains("sf-pick")) return;
    const all = [...$$(".sf-pick")].filter((c) => !c.disabled);
    const head = $("#sf-all");
    if (head) head.checked = all.length > 0 && all.every((c) => c.checked);
  });
  $("#sf-search").addEventListener("click", () => { resetPage("setup"); load(true); });
  $("#sf-view").addEventListener("change", () => { resetPage("setup"); load(true); });
  // 정렬만 바꿀 때는 서버를 다시 부르지 않는다 — 이미 받아 둔 목록을 다시 줄 세우면 된다
  $("#sf-sort").addEventListener("change", () => {
    resetPage("setup");
    renderSetupRows(setupOrder(filterSetupOrders(state.setupOrders || [])), canWork);
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

const PB_RAM_SPECS = ["4GB","8GB","16GB","24GB","32GB","64GB","128GB"];
const PB_RAM_TYPES = ["온보드","D3","D4","D5"];
const PB_SSD_SPECS = ["128GB","256GB","512GB","1TB","2TB","4TB"];
const PB_SSD_TYPES = ["M.2 NVMe","M.2 SATA","M.2 SSD"];
const PB_HDD_SPECS = ["없음","320GB","500GB","1TB","2TB","4TB"];
const pbSpecOpts = (values, selected, label) => `<option value="">${label} 선택</option>` + values
  .map((v) => `<option value="${v}" ${String(selected || "").toUpperCase() === v.toUpperCase() ? "selected" : ""}>${v}</option>`).join("");

async function renderPrebuildTab(main) {
  const canWork = hasPerm("orders.work");
  main.innerHTML = `
    <p class="page-desc">자산번호를 입력해 주문 전에 제작·SW검수·출고 준비까지 완료합니다. 실제 주문에 사용될 때 기존 실적으로 확정됩니다.</p>
    <div class="card"><div class="inline-row">
      <input id="pb-asset-no" type="text" placeholder="자산번호 입력 또는 스캔 (예: 260825-0065)" style="min-width:340px;">
      <button class="btn btn-primary" id="pb-add" ${canWork ? "" : "disabled"}>선제작 추가</button>
      <input id="pb-q" type="text" placeholder="자산번호/모델/제품코드 검색">
      <select id="pb-view"><option value="active">준비 중·완료</option><option value="used">주문 사용완료</option><option value="all">전체</option></select>
      <button class="btn btn-sm" id="pb-search">조회</button>
    </div></div>
    <div class="kpi-row" id="pb-kpi"></div>
    <div class="card"><div class="table-wrap"><table>
      <thead><tr><th>자산번호</th><th>제품 / 사양</th><th>제작 사양 선택</th><th>선제작완료</th><th>선SW검수 완료</th><th>출고 준비완료</th><th>상태</th><th></th></tr></thead>
      <tbody id="pb-rows"><tr><td colspan="8" class="muted">불러오는 중…</td></tr></tbody>
    </table></div></div>`;
  const load = async () => {
    const q = $("#pb-q", main).value.trim(), view = $("#pb-view", main).value;
    try { const d = await api(`/api/prebuilds?view=${view}&q=${encodeURIComponent(q)}`); draw(d.rows || []); }
    catch (err) { $("#pb-rows", main).innerHTML = `<tr><td colspan="8" class="muted">${escapeHtml(err.message)}</td></tr>`; }
  };
  const stage = (r, action, done, by, at, disabled) => `<td class="stage-cell">
    <label class="check-line" style="justify-content:center;"><input type="checkbox" data-pb-stage="${action}" data-pid="${r.id}" ${done ? "checked" : ""} ${canWork && !disabled ? "" : "disabled"}></label>
    ${by ? `<div class="stage-by">${escapeHtml(by)}</div>` : ""}${at ? `<div class="stage-at">${escapeHtml(stageWhen(at))}</div>` : ""}</td>`;
  const draw = (rows) => {
    const counts = [
      ["waiting", "제작 대기", rows.filter((x) => !x.productionDone && !x.usedOrderId).length,
        "아직 제작완료하지 않은 선제작 자산만 봅니다"],
      ["produced", "제작 완료", rows.filter((x) => x.productionDone && !x.inspectionDone && !x.usedOrderId).length,
        "제작이 끝나 SW 검수를 기다리는 자산만 봅니다"],
      ["inspected", "SW 검수 완료", rows.filter((x) => x.inspectionDone && !x.ready && !x.usedOrderId).length,
        "SW 검수가 끝나 출고 준비를 기다리는 자산만 봅니다"],
      ["ready", "출고 준비", rows.filter((x) => x.ready && !x.usedOrderId).length,
        "주문에 바로 사용할 수 있는 선제작 자산만 봅니다"]];
    const currentStage = state.prebuildStage || "";
    $("#pb-kpi", main).innerHTML = counts.map(([key, label, count, title]) => `
      <button class="kpi kpi-btn${currentStage === key ? " on" : ""}"
              data-pb-stage-filter="${key}" title="${title}">
        <div class="kpi-label">${label}</div><div class="kpi-value">${count}</div>
      </button>`).join("");
    const stageRows = currentStage ? rows.filter((x) => {
      if (x.usedOrderId) return false;
      if (currentStage === "waiting") return !x.productionDone;
      if (currentStage === "produced") return x.productionDone && !x.inspectionDone;
      if (currentStage === "inspected") return x.inspectionDone && !x.ready;
      if (currentStage === "ready") return x.ready;
      return true;
    }) : rows;
    $("#pb-rows", main).innerHTML = stageRows.length ? stageRows.map((r) => {
      const cancelled = !!r.cancelledAt, locked = !!r.usedOrderId || cancelled;
      const specLocked = locked || r.productionDone;
      const ramParts = r.ram1
        ? [[r.ram1Type, r.ram1].filter(Boolean).join(" "),
           r.ram2 && r.ram2Type !== "없음" ? [r.ram2Type, r.ram2].filter(Boolean).join(" ") : ""].filter(Boolean)
        : [[r.ramType, r.finalRam].filter(Boolean).join(" ")].filter(Boolean);
      const ramLabel = ramParts.length > 1
        ? `${ramParts.join(" + ")} (총 ${r.finalRam})` : (ramParts[0] || "-");
      const ssdLabel = [r.ssdType, r.finalSsd].filter(Boolean).join(" ") || "-";
      const fullSpec = `${ramLabel} / ${ssdLabel}${r.hdd && r.hdd !== "없음" ? ` / HDD ${r.hdd}` : ""}`;
      return `<tr><td><b>${escapeHtml(r.assetNo)}</b>${r.productCode ? `<div class="muted">${escapeHtml(r.productCode)}</div>` : ""}</td>
        <td>${escapeHtml([r.maker, r.model].filter(Boolean).join(" "))}
          <div><b>선제작 사양 · ${escapeHtml(fullSpec)}</b></div></td>
        <td>${cancelled ? '<span class="muted">취소됨</span>' : r.usedOrderId ? `<b>${escapeHtml(fullSpec)}</b>` : `
          <div style="display:grid; gap:5px; min-width:290px;">
            <div style="display:grid; grid-template-columns:1fr 1fr; gap:5px;">
              <select data-pb-spec-ram1-type="${r.id}" ${specLocked ? "disabled" : ""}>${pbSpecOpts(PB_RAM_TYPES, r.ram1Type, "RAM 1 종류")}</select>
              <select data-pb-spec-ram1="${r.id}" ${specLocked ? "disabled" : ""}>${pbSpecOpts(PB_RAM_SPECS, r.ram1, "RAM 1 용량")}</select>
            </div>
            <div style="display:grid; grid-template-columns:1fr 1fr; gap:5px;">
              <select data-pb-spec-ram2-type="${r.id}" ${specLocked ? "disabled" : ""}>${pbSpecOpts(PB_RAM_TYPES, r.ram2Type === "없음" ? "" : r.ram2Type, "RAM 2 종류")}</select>
              <select data-pb-spec-ram2="${r.id}" ${specLocked ? "disabled" : ""}>${pbSpecOpts(PB_RAM_SPECS, r.ram2, "RAM 2 용량")}</select>
            </div>
            <div style="display:grid; grid-template-columns:1fr 1fr; gap:5px;">
              <select data-pb-spec-ssd-type="${r.id}" ${specLocked ? "disabled" : ""}>${pbSpecOpts(PB_SSD_TYPES, r.ssdType, "SSD 종류")}</select>
              <select data-pb-spec-ssd="${r.id}" ${specLocked ? "disabled" : ""}>${pbSpecOpts(PB_SSD_SPECS, r.finalSsd, "SSD 용량")}</select>
            </div>
            <select data-pb-spec-hdd="${r.id}" ${specLocked ? "disabled" : ""}>${pbSpecOpts(PB_HDD_SPECS, r.hdd || "없음", "HDD")}</select>
            <button class="btn btn-sm" data-pb-spec-save="${r.id}" ${canWork && !specLocked ? "" : "disabled"}>제작 사양 저장</button>
          </div>`}</td>
        ${stage(r, "production", r.productionDone, r.productionBy, r.productionAt, locked)}
        ${stage(r, "inspection", r.inspectionDone, r.inspectionBy, r.inspectionAt, locked || !r.productionDone)}
        ${stage(r, "ready", r.ready, r.readyBy, r.readyAt, locked || !r.inspectionDone)}
        <td>${cancelled
          ? `<span class="chip chip-red">선제작 취소</span><div class="muted" style="max-width:190px; white-space:normal;">${escapeHtml(r.cancelReason || "-")} · ${escapeHtml(r.cancelledBy || "-")}</div>`
          : locked && r.specChangeRequired
          ? `<span class="chip chip-amber">사양변경 사용</span><div class="muted" style="max-width:190px; white-space:normal;">${escapeHtml(r.specChangeReason || "주문 사양 변경")}</div>`
          : locked ? `<span class="chip chip-green">주문 #${r.usedOrderId} 사용완료</span>`
          : r.ready ? '<span class="chip chip-green">사용 가능</span>' : '<span class="chip chip-amber">작업 중</span>'}</td>
        <td>${locked ? "" : `<button class="btn btn-ghost btn-sm" data-pb-cancel="${r.id}" ${canWork ? "" : "disabled"}>선제작 취소</button>`}</td></tr>`;
    }).join("") : `<tr><td colspan="8" class="muted">${currentStage ? "선택한 단계의 선제작 자산이 없습니다." : "선제작 자산이 없습니다."}</td></tr>`;
    $$("button[data-pb-stage-filter]", main).forEach((button) => button.addEventListener("click", () => {
      const selected = button.dataset.pbStageFilter;
      state.prebuildStage = state.prebuildStage === selected ? "" : selected;
      const view = $("#pb-view", main);
      if (view.value !== "active") {
        view.value = "active";
        load();
      } else {
        draw(rows);
      }
    }));
    $$("input[data-pb-stage]", main).forEach((cb) => cb.addEventListener("change", async () => {
      cb.disabled = true;
      try { await api(`/api/prebuilds/${cb.dataset.pid}`, { method: "PATCH", body: { action: cb.dataset.pbStage, value: cb.checked } }); await load(); }
      catch (err) { toast(err.message, true); await load(); }
    }));
    $$("button[data-pb-spec-save]", main).forEach((b) => b.addEventListener("click", async () => {
      const id = b.dataset.pbSpecSave;
      const ram1Type = $(`select[data-pb-spec-ram1-type="${id}"]`, main).value;
      const ram1 = $(`select[data-pb-spec-ram1="${id}"]`, main).value;
      const ram2Type = $(`select[data-pb-spec-ram2-type="${id}"]`, main).value || "없음";
      const ram2 = $(`select[data-pb-spec-ram2="${id}"]`, main).value;
      const ssdType = $(`select[data-pb-spec-ssd-type="${id}"]`, main).value;
      const ssd = $(`select[data-pb-spec-ssd="${id}"]`, main).value;
      const hdd = $(`select[data-pb-spec-hdd="${id}"]`, main).value;
      if (!ram1Type || !ram1 || (ram2Type !== "없음" && !ram2) || !ssdType || !ssd || !hdd) { toast("RAM 구성, SSD 종류·용량, HDD를 확인하세요.", true); return; }
      b.disabled = true;
      try { await api(`/api/prebuilds/${id}`, { method: "PATCH", body: { action: "spec", ram1Type, ram1, ram2Type, ram2, ssdType, ssd, hdd } }); toast("선제작 사양을 저장했습니다."); await load(); }
      catch (err) { toast(err.message, true); b.disabled = false; }
    }));
    $$("button[data-pb-cancel]", main).forEach((b) => b.addEventListener("click", async () => {
      const reason = prompt("선제작 취소 사유를 입력하세요.\n취소하면 선제작 기록과 저장한 제작 사양이 삭제됩니다.");
      if (reason === null) return;
      if (!reason.trim()) { toast("취소 사유를 입력하세요.", true); return; }
      try { await api(`/api/prebuilds/${b.dataset.pbCancel}`, { method: "PATCH", body: { action: "cancel", reason: reason.trim() } }); toast("선제작을 취소했습니다."); await load(); }
      catch (err) { toast(err.message, true); }
    }));
  };
  const add = async () => {
    const box = $("#pb-asset-no", main), assetNo = box.value.trim();
    if (!assetNo) return;
    try { await api("/api/prebuilds", { method: "POST", body: { assetNo } }); box.value = ""; toast(`${assetNo} 선제작 작업을 추가했습니다.`); await load(); box.focus(); }
    catch (err) { toast(err.message, true); box.focus(); }
  };
  $("#pb-add", main).addEventListener("click", add);
  $("#pb-asset-no", main).addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); add(); } });
  $("#pb-search", main).addEventListener("click", load);
  $("#pb-view", main).addEventListener("change", () => { state.prebuildStage = ""; load(); });
  autoSearch("#pb-q", load);
  await load();
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
   OWS는 주문 단위 합계 금액, QC는 상품·대수 단위 금액이라 금액이 달라 자동 반영에서 빠진다.
   자동으로 처리하진 않되(잘못 찍으면 안 나간 물건이 나간 게 된다), 눈에는 보이게 한다.

   ※2026-08-07부터 화면에서 안 부른다(QC 연동 해제). state.qcShippedFlags 가 비어 있어
     늘 "" 를 돌려준다. 마지막 대조를 손으로 할 때를 위해 남겨 둔다. */
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

   ★대표 지시(2026-08-05): "RMS로 들어가는 렌탈 상품이 OWS에 뜬다. 상품번호로 안 뜨게 하자."
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
     QC 프로그램은 [금일 출고 확인 엑셀]을 누르면 목록에서 빼 버린다. 그런데 OWS는
     몰에서 직접 주문을 받아 오므로 **같은 주문이 서로 다른 키**로 존재한다
     (QC "주문수집:…" vs OWS 몰 주문번호). 키가 다르니 자동으로 이어지지 않고,
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
      QC 프로그램은 출고 확인을 누르면 목록에서 빼기 때문에 OWS로 이어지지 않은 건입니다.
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
  const shippingCount = orders.filter(inShippingBucket).length;
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
    + card("inspected", "SW 검수 완료", cnt.inspected, "SW 검수가 끝나 출고 확인을 기다리는 주문만 봅니다")
    // ★출고 확인 = 'CJ 간선상차 뒤 ~ 배송완료 전'(대표 2026-09-04). 배송완료가 찍히면
    //   [출고 기록 조회]로 넘어간다. 송장이 없어 추적할 수 없는 건만 오늘·어제까지 둔다.
    + card("shipping", "출고 확인", shippingCount,
           "간선상차되어 나갔지만 아직 배송완료가 안 된 주문입니다. "
           + "배송완료되면 [출고 기록 조회]로 넘어갑니다")
    // ★[금일 출고 확인]으로 목록에서 내린 건들은 여기에 쌓인다(대표 2026-08-05)
    + `<button class="kpi kpi-btn kpi-log${cur === "shipped" ? " on" : ""}"
               data-stage-filter="shipped" title="출고 확인된 주문을 봅니다">
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
    const pb = $("#sf-wbprint-all");
    if (pb) pb.style.display = WB_PRINT_STAGES.includes(state.setupStage) ? "" : "none";
    syncWbPrintLabel();
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

/* 송장 일괄 인쇄 버튼이 보이는 단계 — 뽑을 수 있는 단계와 같아야 한다.
   ★대표 2026-09-04 정정: SW 검수 완료 탭에서도 일괄 출력이 돼야 한다. */
const WB_PRINT_STAGES = ["inspected", "shipping", "shipped"];

/* 이미 나간 물건을 보는 단계. 여기서는 판매유형·판매채널 칩을 감춘다(대표 2026-09-04:
   "판매유형, 판매채널은 SW 검수완료까지만 보여야 해"). 출고 확인·출고 기록 조회는
   '어느 몰에서 팔렸나'로 골라 낼 일이 없는 화면이라 줄만 차지한다. */
const SHIPPED_STAGES = ["shipping", "shipped"];

/* 발급된(취소 아닌) 출고 송장이 있나 — '작업이 끝났는가'의 기준이다(2026-09-03).
   송장이 나가야 셋팅 보드에서 내려가고 [출고 기록 조회]로 넘어간다. */
function hasIssuedWaybill(o) {
  return (o.waybills || []).some((w) => w.type !== "recall"
    && ["issued", "test"].includes(w.status));
}

/* 일괄 버튼의 이름·설명 — SW 검수 완료 탭에서는 '발급·인쇄', 그 뒤 단계에서는 '인쇄'.
   (표시 여부는 WB_PRINT_STAGES 로 따로 정한다 — 여기서는 글자만 바꾼다) */
function syncWbPrintLabel() {
  const b = $("#sf-wbprint-all");
  if (!b) return;
  const issueMode = state.setupStage === "inspected";
  b.textContent = issueMode ? "🧾 송장 일괄 발급·인쇄" : "🖨 송장 일괄 인쇄";
  b.title = issueMode
    ? "지금 화면에 보이는 목록에서 송장이 아직 없는 주문에 송장을 발급하고 한 번에 인쇄합니다 — "
      + "이미 발급(출력)된 건은 제외합니다(다시 인쇄는 행의 🖨)"
    : "지금 화면에 보이는 목록(필터·검색 반영)의 발급된 송장을 화면 순서 그대로 한 PDF로 모아 인쇄합니다 "
      + "— 건마다 그 고객의 송장번호가 붙습니다";
}

/* ★[🧾 송장 일괄 발급·인쇄] — SW 검수 완료 탭(대표 2026-09-08 "송장을 뽑았던 애를 뽑는 게 아니라
   송장을 뽑지 않았던 애들에게 송장 넣어서 실제 출력이 전부 되어야 해 — 이미 출력했던 애들은 제외").
   송장이 없는 주문마다 행의 [🧾 송장]과 똑같은 발급(CJ 접수 · 몰 전송 포함)을 차례로 하고,
   새로 나온 송장만 한 PDF로 모아 인쇄한다. 이미 발급된 건은 건드리지 않는다(다시 인쇄는 행의 🖨).
   상자 수·송장 수는 기본(1·1) — 여러 상자로 나가는 주문은 행에서 따로 발급한다.
   한 건이 실패해도 멈추지 않는다 — 나머지는 발급·인쇄하고, 실패한 건은 사유와 함께 알려 준다. */
async function bulkIssueAndPrint(list, pickedCount) {
  const scope = pickedCount ? "고른 " : "지금 목록의 ";
  const targets = list.filter((o) => !o.cancelledAt && o.softwareInspectionDone && !o.shippingDone);
  const parcel = targets.filter((o) => (o.receiveMethod || "택배") === "택배");
  const already = parcel.filter((o) => hasIssuedWaybill(o)
    || (o.waybills || []).some((w) => w.type !== "recall" && w.status === "pending"));
  const toIssue = parcel.filter((o) => !already.includes(o));
  const notParcel = targets.length - parcel.length;
  if (!toIssue.length) {
    if (!targets.length) { toast(`${scope}SW 검수 완료까지 끝난 주문이 없습니다.`, true); return; }
    // 전부 발급돼 있다 — 기본은 제외지만, 인쇄가 안 됐던 경우를 위해 물어보고 다시 인쇄할 수 있다
    const wids = [];
    already.forEach((o) => (o.waybills || []).forEach((w) => {
      if (w.type !== "recall" && ["issued", "test"].includes(w.status)) wids.push(w.wid);
    }));
    if (!wids.length || !confirm(`${scope}${targets.length}건은 모두 송장이 발급돼 있습니다(이미 출력한 건은 제외).\n`
      + `발급된 송장 ${wids.length}장을 다시 인쇄할까요?`)) return;
    await printWaybillWids(wids);
    return;
  }
  if (toIssue.length > 100) {
    toast(`한 번에 100건까지 발급할 수 있습니다 — 지금 ${toIssue.length}건. 검색·필터로 줄여 주세요.`, true);
    return;
  }
  const multi = toIssue.filter((o) => Math.max((o.assets || []).length, Number(o.quantity) || 1) > 1).length;
  if (!confirm(`${scope}송장이 없는 ${toIssue.length}건에 송장을 발급하고 한 번에 인쇄합니다.`
    + (already.length ? `\n이미 발급된 ${already.length}건은 제외합니다(다시 인쇄는 행의 🖨).` : "")
    + (notParcel ? `\n택배가 아닌 ${notParcel}건은 건너뜁니다.` : "")
    + "\n상자 1개 · 송장 1장 기준입니다."
    + (multi ? ` 수량이 2 이상인 ${multi}건도 한 송장으로 나갑니다 — 상자를 나누려면 취소하고 행에서 따로 발급하세요.` : "")
    + "\n\n진행할까요?")) return;
  const btn = $("#sf-wbprint-all");
  const label = btn ? btn.textContent : "";
  if (btn) btn.disabled = true;
  const wids = [], failed = [];
  let mallFail = 0;
  try {
    for (let i = 0; i < toIssue.length; i++) {
      const o = toIssue[i];
      if (btn) btn.textContent = `발급 중 ${i + 1}/${toIssue.length}…`;
      try {
        // boxQty 를 안 보내면 서버가 그 주문의 지난 상자 수(없으면 1)를 쓴다 — 행의 발급과 같은 규칙
        const r = await api(`/api/orders/${o.id}/waybill`, { method: "POST", body: {} });
        wids.push(...((r.wids && r.wids.length) ? r.wids : [r.wid]));
        if (r.mallPush && !r.mallPush.ok) mallFail++;
      } catch (err) {
        failed.push(`${o.recipient || o.orderNumber || ("#" + o.id)}: ${err.message}`);
      }
    }
    if (btn) btn.textContent = "인쇄 준비 중…";
    if (wids.length) await printWaybillWids(wids);
    toast(`송장 ${wids.length}장을 발급해 인쇄창으로 보냈습니다.`
      + (failed.length ? ` 실패 ${failed.length}건.` : "")
      + (mallFail ? ` 몰 전송 실패 ${mallFail}건(송장 칸의 ⚠ 참고).` : ""), failed.length > 0);
    if (failed.length) {
      alert(`발급하지 못한 주문 ${failed.length}건 — 고친 뒤 다시 누르면 그 건만 발급됩니다.\n\n`
        + failed.slice(0, 15).join("\n") + (failed.length > 15 ? `\n… 외 ${failed.length - 15}건` : ""));
    }
  } catch (e) {
    toast(e.message, true);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = label; }
    const s = $("#sf-search");
    if (s) s.click();                    // 작업보드 갱신 — 발급된 송장·몰 전송 칩
  }
}

/* 송장 wid 목록을 서버 병합 PDF 로 받아 숨은 iframe 으로 인쇄(라벨 렌더러는 손대지 않는다). */
async function printWaybillWids(wids) {
  const res = await fetch("/api/waybills/print", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ wids }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.description || err.message || "송장 인쇄에 실패했습니다.");
  }
  printPdfBlob(await res.blob());
}

/* ★[출고 확인] 칸에 남아 있어야 하는 주문인가 — 나갔지만 아직 도착하지 않은 것.

   대표 2026-09-04: "간선상차 커밋 이후로 출고확인으로 넘어가고, 출고확인에서
   배송완료가 되어야만 출고기록 조회로 넘어가게." → 나가는 문은 CJ 간선상차(11)가 열고
   (서버 SHIP_CONFIRM_STAGES), **나가는 문은 배송완료(91)가 닫는다.**
   배송완료가 찍히면 서버가 보관(archived)까지 하므로 이 화면 조회에서 아예 빠진다.

   ★송장이 없는 건만 날짜로 끊는다. OWS 송장을 아직 안 쓰고 나간 주문이 실측 257건
   (8/24~9/3) 쌓여 있는데, 이런 건은 CJ가 배송완료를 알려 줄 방법이 없어 영원히 남는다
   (대표 신고: "제작대기를 한 번 더 누르면 이미 출고된 것까지 나온다"). 추적할 수 없는
   건은 오늘·어제까지만 두고, 그 뒤는 [출고 기록 조회]에서 본다. */
function inShippingBucket(o) {
  if (!o.shippingDone || o.cancelledAt) return false;
  if (o.deliveredAt) return false;                 // 배송완료 → [출고 기록 조회]
  if (hasIssuedWaybill(o)) return true;            // 추적 가능 — CJ가 배송완료를 알려 줄 때까지
  const d = String(o.shippingAt || "").slice(0, 10);
  return !!d && d >= ymd(new Date(Date.now() - 864e5));
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
    // 출고 확인은 됐는데 아직 송장을 안 뽑은 건 — 여기서 [🧾 송장]을 뽑는다
    if (k === "shipping") return inShippingBucket(o);
    return true;
  });
}

/* 판매유형과 채널은 서로 다른 축이다. b2b 채널만 B2B로 보고 나머지는 B2C로
   분류한다. 채널 목록은 실제 수집된 주문에서 만들어 새 몰이 추가돼도 자동으로 뜬다. */
function setupSalesType(o) {
  const ch = String(o.channel || "").trim().toLowerCase();
  return ch === "b2b" || ch.includes("b2b") ? "b2b" : "b2c";
}

function filterSetupOrders(orders) {
  const staged = filterByStage(orders);
  const type = state.setupSalesType || "all";
  const channel = state.setupChannel ?? "all";
  return staged.filter((o) => {
    if (type !== "all" && setupSalesType(o) !== type) return false;
    return channel === "all" || String(o.channel || "").trim() === channel;
  });
}

function renderSetupTagFilters(orders, canWork) {
  const host = $("#setup-tag-filters");
  if (!host) return;
  // ★출고 확인·출고 기록 조회에서는 칩을 감춘다. 골라 둔 값도 함께 푼다 —
  //   안 풀면 그 채널이 켜진 채로 남아 다음 단계 목록을 조용히 걸러 낸다.
  if (SHIPPED_STAGES.includes(state.setupStage)) {
    state.setupSalesType = "all";
    state.setupChannel = "all";
    host.innerHTML = "";
    return;
  }
  const type = state.setupSalesType || "all";
  const channel = state.setupChannel ?? "all";
  const typeCount = { all: orders.length, b2c: 0, b2b: 0 };
  const channels = new Map();
  for (const o of orders) {
    typeCount[setupSalesType(o)]++;
    const ch = String(o.channel || "").trim();
    channels.set(ch, (channels.get(ch) || 0) + 1);
  }
  if (channel !== "all" && !channels.has(channel)) state.setupChannel = "all";
  const pickedChannel = state.setupChannel ?? "all";
  const tag = (kind, value, label, count, on) =>
    `<button type="button" class="setup-filter-tag${on ? " on" : ""}"
      data-setup-${kind}="${escapeHtml(value)}">${escapeHtml(label)} <b>${count}</b></button>`;
  const channelTags = [...channels.entries()]
    .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0], "ko"))
    .map(([ch, count]) => tag("channel", ch, ch || "(미지정)", count,
      pickedChannel === ch)).join("");
  host.innerHTML = `
    <div class="setup-filter-line"><span class="setup-filter-label">판매유형</span>
      ${tag("type", "all", "전체", typeCount.all, type === "all")}
      ${tag("type", "b2c", "B2C", typeCount.b2c, type === "b2c")}
      ${tag("type", "b2b", "B2B", typeCount.b2b, type === "b2b")}</div>
    <div class="setup-filter-line"><span class="setup-filter-label">판매채널</span>
      ${tag("channel", "all", "전체", orders.length, pickedChannel === "all")}${channelTags}</div>`;
  const redraw = () => {
    resetPage("setup");
    renderSetupTagFilters(state.setupOrders || [], canWork);
    renderSetupRows(setupOrder(filterSetupOrders(state.setupOrders || [])), canWork);
  };
  $$("button[data-setup-type]", host).forEach((b) => b.addEventListener("click", () => {
    state.setupSalesType = b.dataset.setupType;
    redraw();
  }));
  $$("button[data-setup-channel]", host).forEach((b) => b.addEventListener("click", () => {
    state.setupChannel = b.dataset.setupChannel;
    redraw();
  }));
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
// '급' 자 없이 제품코드 꼬리로만 오는 등급 — 롯데온 epdNo "…내장 AA"·쿠팡 "…내장 AS 256"
// (뒤 숫자는 용량 장식). ★코드 모양(밑줄 2개)의 "/" 앞 조각 끝에서만 읽는다 — 자유 텍스트
// 상품명("…무상 AS")을 등급으로 오인하지 않게. 서버 waybill._order_grade 와 같은 규칙(9/1).
const RE_BARE_TAIL = /\s+(S\+[SA]|[SA][SAB])(?:\s+\d+)?\s*$/;

function orderGrade(o) {
  const t = `${o.productName || ""} ${o.optionName || ""}`;
  const look = RE_LOOK.exec(t);
  const batt = RE_BATT.exec(t);
  if (look && batt) return (look[1] + batt[1]).toUpperCase();
  const pair = RE_PAIR.exec(t);
  if (pair) return pair[1].toUpperCase();
  for (const raw of [o.productCode || "", o.productName || "", o.optionName || ""]) {
    const seg = raw.split("/")[0].trim();
    if (seg.split("_").length !== 3) continue;         // 코드 모양이 아니면 안 읽는다
    const m = RE_BARE_TAIL.exec(seg);
    if (m) return m[1].toUpperCase();
  }
  return "";
}

function normGrade(g) { return String(g || "").replace("급", "").trim().toUpperCase(); }

/* 창고 재고 칩 — 숫자 하나만 두지 않고 '무엇을 셌는지'를 함께 알려 준다.
   포함 검색이라 뒤에 글자가 붙는 다른 모델까지 섞일 수 있다(갤럭시탭 S6 ← S6 Lite).
   섞였으면 ⚠를 붙이고, 마우스를 올리면 모델별 내역이 보인다. */
/* 셋팅·QC의 창고 재고 칩.
   ★★재고는 오직 제품코드로만 센다(대표 2026-08-14 재확인: "제품코드가 입력되기 전까지
     셋팅·QC 탭에서 그 자산들이 잡히면 안 된다 — 제품은 없다고 보면 된다").
     그래서 모델명으로 짐작한 '모델 N대'는 화면에서 완전히 뺐다. 모델명 근사는
     'NT371B5M'이 'NT371B5M2'까지 끌어와 없는 재고를 있다고 말하던 원인이었고,
     그 숫자를 믿고 잡았다가 출고 단계에서 되돌아오는 일이 실제로 있었다.
     코드가 없으면 '재고 없음 · 제품코드 미입력'으로 표시하고, 매입에서 코드를 넣게 한다. */
function stockChip(info, want) {
  const gr = Array.isArray(info.ourStockGrade) ? info.ourStockGrade : [];
  const gLines = gr.map((g) => `  ${g.grade} ${g.count}대`);
  const NL = String.fromCharCode(10);
  const sku = info.sku || "";
  const codeTier = info.codeTier || {};
  const codeProv = Number(codeTier["가재고"] || 0);
  const codeShip = Number(info.codeShippable || 0);
  const codeTierLine = ["가용", "실재고", "가재고"]
    .filter((t) => codeTier[t]).map((t) => `  ${t} ${codeTier[t]}대`).join(NL);

  let html = "";
  if (info.codeRegistered) {
    // 코드로 정확히 대조됨 — 이게 믿을 수 있는 숫자다
    const tip2 = `제품코드 ${sku} 기준` + NL + `출고 가능 ${codeShip}대 / 전체 ${info.codeStock}대`
      + (codeTierLine ? NL + NL + "재고 구분" + NL + codeTierLine : "")
      + (codeProv ? NL + "※ 가재고는 매입에서 가용/실재고로 바꾸기 전엔 출고할 수 없습니다" : "");
    const c2 = codeShip <= 0 ? "chip-red" : (codeShip <= 2 ? "chip-violet" : "chip-green");
    html = `<span class="chip ${c2}" style="cursor:help;"
      title="${escapeHtml(tip2)}">재고 ${codeShip}대</span>`;
    if (codeProv) {
      html += ` <span class="chip chip-amber" style="cursor:help;"
        title="${escapeHtml(`가재고 ${codeProv}대 — 완전한 수리가 끝나야 출고할 수 있습니다.` + NL
          + `매입 화면에서 가용 또는 실재고로 바꾸세요.`)}">가재고 ${codeProv}</span>`;
    }
  } else {
    // ★코드로 등록된 자산이 없다 = 재고 없음. 모델명으로 짐작한 숫자는 보여주지 않는다
    //   (대표 지시: 제품코드가 들어가기 전까지 그 자산은 없는 것으로 본다).
    const why = (sku ? `제품코드 '${sku}' 로 등록된 자산이 없습니다.`
                     : `이 주문에서 제품코드를 찾지 못했습니다.`)
      + NL + `매입 ▸ 🚫 판매불가·제품코드 화면에서 자산에 제품코드를 넣으면 재고로 잡힙니다.`
      + NL + `※ 모델명이 비슷한 자산은 재고로 세지 않습니다 — 다른 모델이 섞여 틀린 숫자가 됩니다.`;
    html = `<span class="chip chip-red" style="cursor:help;"
      title="${escapeHtml(why)}">재고 없음 · 코드 미입력</span>`;
  }
  // ★주문이 등급을 지정했으면 '그 등급이 몇 대인지'가 진짜 필요한 숫자다.
  //   ★단 등급 대조도 제품코드로 잡힌 자산에만 붙인다 — 코드가 없으면 재고 자체가
  //     없는 것이므로 '무슨 등급이 몇 대'를 말할 근거도 없다(모델명 근사 금지).
  if (want && info.codeRegistered) {
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
/* 상품명 링크 — ★'진짜 그 상품' 페이지로 보낸다(2026-08-14 대표: "링크가 실제 상품이
   아니더라"). 몰이 준 상품 식별자(mallProductId·mallItemId)가 있으면 상품 페이지로,
   없으면 예전처럼 검색으로 떨어진다(수기 주문·옛 주문은 식별자가 없다).
   ★쿠팡 URL은 '노출상품ID'를 쓴다 — 등록상품ID로는 안 열린다. 옵션ID(vendorItemId)까지
     붙이면 제목이 옵션표인 쿠팡에서 '그 옵션'이 선택된 채 열린다. */
function mallProductUrl(o, info) {
  const ch = String(o.channel || "");
  const q = encodeURIComponent(o.productName || o.productCode || "");
  const pid = String(o.mallProductId || "").trim();
  const item = String(o.mallItemId || "").trim();
  const shops = state.shopUrls || {};
  if (!q && !pid) return "";
  if (ch.indexOf("고도몰") >= 0 || ch.indexOf("자사몰") >= 0 || ch.indexOf("업무관리") >= 0) {
    const shop = String(shops.godomall || state.shopUrl || "").replace(/\/+$/, "");
    const no = info && info.goodsNo ? String(info.goodsNo) : "";
    if (shop && no) return `${shop}/goods/goods_view.php?goodsNo=${encodeURIComponent(no)}`;
    if (shop) return `${shop}/goods/goods_search.php?keyword=${q}`;
    return "";                                   // 자사몰 주소를 아직 안 넣었다
  }
  if (ch.indexOf("쿠팡") >= 0) {
    if (pid) {
      return `https://www.coupang.com/vp/products/${encodeURIComponent(pid)}`
        + (item ? `?vendorItemId=${encodeURIComponent(item)}` : "");
    }
    return `https://www.coupang.com/np/search?q=${q}`;
  }
  if (ch.indexOf("스마트스토어") >= 0 || ch.indexOf("네이버") >= 0) {
    const store = String(shops.smartstore || "").replace(/\/+$/, "");
    if (store && pid) return `${store}/products/${encodeURIComponent(pid)}`;
    return `https://search.shopping.naver.com/search/all?query=${q}`;
  }
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
  // 창고 재고는 몰 API가 없어도 나온다(모든 쇼핑몰 공통) — 기준은 제품코드뿐이다
  if (info) {
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
    ? `<button type="button" class="chip setup-memo-chip" data-setup-memo="${o.id}"
        title="${escapeHtml(o.memo)}&#10;클릭해서 수정">📌 ${escapeHtml(o.memo)}</button>`
    : (canWork ? `<button type="button" class="setup-memo-add" data-setup-memo="${o.id}">+ 비고</button>` : "");
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
        // 진짜 상품 페이지인지, 이름으로 검색만 하는지 툴팁에 그대로 밝힌다
        const direct = /\/vp\/products\/|goods_view\.php|\/products\//.test(url || "");
        const ch = escapeHtml(o.channel || "쇼핑몰");
        return url
          ? `<a class="prod-title" href="${escapeHtml(url)}" target="_blank" rel="noopener"
               title="${t}&#10;&#10;${direct
                 ? `클릭하면 ${ch}의 이 상품 페이지가 열립니다`
                 : `클릭하면 ${ch}에서 상품명으로 검색합니다(상품 링크 정보가 없는 주문)`}">${t}</a>`
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
  // ★몰을 가리지 않는다(2026-08-14 대표 "쿠팡뿐 아니라 모든 쇼핑몰"): 제품코드가
  //   몰마다 같으므로, 서버가 고도몰 → 상품 조회 되는 몰 순으로 코드 자체를 찾는다.
  //   어느 몰에도 없는 코드는 서버가 캐시해 두므로 헛호출이 반복되지 않는다.
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
  // ★앞 요청이 아직 도는 중이면 보내지 않는다(2026-08-14 검토). 폴링은 5초인데 몰 조회는
  //   그보다 오래 걸릴 수 있고, need는 응답이 와야 갱신되므로 같은 코드가 겹쳐 나간다.
  if (state.piInflight) return false;
  state.piInflight = true;
  try {
    const r = await api("/api/orders/product-info", {
      method: "POST", body: { mall: "godomall", codes: need, names },
    });
    const got = r.products || {};
    state.productInfo = Object.assign({}, known, got);
    state.productInfoAt = Date.now();
    if (r.shopUrl !== undefined) state.shopUrl = r.shopUrl;   // 자사몰 주소(설정에서 입력)
    if (r.shopUrls) state.shopUrls = r.shopUrls;              // 몰별 상점 주소(링크 조립용)
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
  finally { state.piInflight = false; }     // 실패해도 반드시 풀어야 다음 폴링이 산다
}

/* 자산 매칭 — 목록 '그 행'에서 바로 찍는다(팝업 없음, 대표 요청 2026-07-29).
   주문 수량만큼 칸이 뜨고, 스캐너로 찍으면 그 자리에서 저장된다.
   같은 주문에 남은 칸이 있으면 그 칸으로, 다 채웠으면 '다음 주문'의 첫 칸으로 커서가 넘어간다. */
/* 2대 이상 주문의 대별 목록(대표 2026-08-24) — 들여쓰기·구분선으로 한 대씩.
   대마다: 자산번호(붙었으면 칩, 아니면 스캔 칸) + [준비 완료] 체크 + 누가 언제.
   ★출고 확인은 주문 단위 그대로다(배송은 한 건) — 준비만 대별로 센다. */
function unitsRow(o, canWork) {
  const need = Math.max(1, Number(o.quantity) || 1);
  const have = o.assets || [];
  const lines = [];
  for (let i = 0; i < Math.max(need, have.length); i++) {
    const a = have[i];
    lines.push(`<div class="unit-line">
      <span class="unit-no">${i + 1}</span>
      ${a ? `<span class="chip chip-green" title="${escapeHtml(a.model || "")}">${escapeHtml(a.assetNo)}</span>
             <span class="muted" style="font-size:12px;">${escapeHtml(a.model || "")}</span>
             <label class="check-line unit-prep" title="이 기계의 셋팅이 끝났으면 체크하세요">
               <input type="checkbox" data-prep-unit="${o.id}:${a.assetId}"
                      ${a.prepared ? "checked" : ""} ${canWork ? "" : "disabled"}>
               <span>준비 완료</span></label>
             ${a.prepared && a.preparedBy
               ? `<span class="muted" style="font-size:11.5px;">${escapeHtml(a.preparedBy)}</span>` : ""}
             ${canWork ? `<button class="btn btn-ghost btn-sm" data-unmatch="${o.id}:${a.assetId}"
                title="이 자산 빼기">✕</button>` : ""}`
          : `${canWork ? `<input type="text" class="as-slot" data-oid="${o.id}" data-idx="${i}"
                placeholder="📷 ${i + 1}번째 관리번호 스캔"
                style="width:170px; padding:4px 8px; border:1px solid var(--border);
                       border-radius:8px; background:var(--bg);">`
              : '<span class="muted" style="font-size:12px;">미매칭</span>'}`}
    </div>`);
  }
  return `<tr class="setup-detail setup-units"><td colspan="11">
    <div class="units-box">${lines.join("")}</div>
  </td></tr>`;
}

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
  // ★2대 이상은 자산 칩·스캔칸을 여기 안 그린다(대표 2026-08-25: "우측과 아래 두 개가
  //   표시될 필요는 없으니 하단에만") — 대별 줄(unitsRow)이 자산번호·스캔·준비를 다 가진다.
  //   여기는 요약(N/M대)과 🔍·펼침 토글만 남긴다. 1대짜리는 예전 그대로.
  if (need === 1) {
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
          placeholder="📷 스캔" title="관리번호를 스캔하세요"
          style="width:104px; padding:4px 8px; border:1px solid var(--border);
          border-radius:8px; background:var(--bg);">
      </span>`);
    } else if (nextIdx >= 0) {
      rows.push(`<span class="as-item muted" style="font-size:12px;">미매칭</span>`);
    }
  }
  // ★스캔 실패 사유는 칸 옆에 남겨 둔다. 토스트는 몇 초 뒤 사라져서
  //   담당자는 번호를 잘못 친 줄 알고 같은 번호를 계속 다시 찍는다(2026-07-30 대표 지적).
  const err = (state.setupScanErr || {})[o.id];
  const prebuildButtons = canWork && !o.shippingDone ? have.filter((a) => a.isPrebuilt).map((a) =>
    `<button class="btn btn-sm as-rework" data-prebuild-rework="${o.id}:${a.assetId}"
      title="선제작 제품을 주문 사양으로 변경할 때 누릅니다">사양변경 · ${escapeHtml(a.assetNo)}</button>`
  ).join("") : "";
  return `<td class="setup-assets">
    <div class="as-slots">
      ${rows.join("")}
      <span class="as-item muted" style="font-size:11px;">${done}/${need}대</span>
      ${canWork ? `<button class="btn btn-ghost btn-sm as-item" data-match="${o.id}"
        title="번호를 모를 때 — 검색해서 고릅니다">🔍</button>` : ""}
      ${need > 1 && (state.setupUnitsOpen || {})[o.id] === false && nextIdx >= 0
        ? `<span class="as-item muted" style="font-size:11.5px;">▸를 눌러 대별 입력</span>` : ""}
      ${need > 1 ? (() => {
        // ★2대 이상은 대별로 준비를 체크한다(대표 2026-08-24) — 펼쳐서 한 대씩 본다
        const prepped = have.filter((a) => a.prepared).length;
        // ★기본이 '펼침'이다(대표 2026-08-24: "2대 이상은 각각 수량별로 떠야") —
        //   접은 주문만 false 로 기억한다. 버튼은 접기/펴기 토글이다.
        const open = (state.setupUnitsOpen || {})[o.id] !== false;
        return `<button class="btn btn-ghost btn-sm as-item" data-units="${o.id}"
          title="대별로 자산번호·준비 완료를 확인합니다">${open ? "▾" : "▸"}
          <span class="chip ${prepped >= need ? "chip-green" : "chip-slate"}"
            style="font-size:11px;">준비 ${prepped}/${need}</span></button>`;
      })() : ""}
    </div>
    ${prebuildButtons ? `<div style="margin-top:5px;">${prebuildButtons}</div>` : ""}
    ${err ? `<div class="chip chip-red as-err" title="${escapeHtml(err.msg)}">${escapeHtml(err.msg)}</div>` : ""}
    ${err && err.force && canWork ? `<button class="btn btn-sm as-force"
        data-force-oid="${o.id}" data-force-aid="${err.force.assetId}"
        title="TMS에서 번호를 잘못 적어 판매로 찍힌 경우입니다.&#10;실물이 여기 있다면 붙이고, 매입 ▸ 번호 충돌에서 번호를 바로잡으세요.&#10;※OWS 주문이 잡고 있는 자산은 이 버튼이 뜨지 않습니다.">⚠ 실물 있음 — ${escapeHtml(err.force.assetNo)} 붙이기</button>` : ""}
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

/* 스캔 실패 사유를 주문별로 기억한다(다시 그려도 남아 있게).
   force = {assetId, assetNo} 를 함께 주면 [⚠ 실물 있음으로 붙이기] 버튼이 같이 뜬다
   — TMS에서 번호를 잘못 적어 '판매'로 찍힌 탓에 실물이 멀쩡한데 막히는 경우다
   (대표 2026-08-18: "중복이라고 떠도 셋팅/QC에서는 입력은 되게"). */
function setScanErr(oid, msg, force) {
  state.setupScanErr = state.setupScanErr || {};
  if (msg) state.setupScanErr[oid] = { msg, force: force || null };
  else delete state.setupScanErr[oid];
  if (state.setupOrders) renderSetupRows(state.setupOrders, hasPerm("orders.work"));
}

/* [⚠ 실물 있음] — TMS 오기입으로 막힌 자산을 붙인다.
   ★서버가 자산에 '번호충돌' 표시를 남기고 TMS 자동반영을 잠근다. 안 잠그면
     다음 수집(2시간)에 다시 '출고'로 되돌아가 주문이 통째로 잠긴다. */
async function forceMatchAsset(oid, assetId) {
  const order = (state.setupOrders || []).find((x) => x.id === oid);
  if (!order) return;
  if (!confirm("TMS에는 나간 것으로 적혀 있지만 실물이 여기 있다는 뜻입니다.\n\n"
      + "이 주문에 붙이고, 매입 ▸ ⚠ 번호 충돌 목록에 올립니다.\n"
      + "나중에 매입에서 번호를 맞바꾸거나 넘겨받아 바로잡으세요.\n\n계속할까요?")) return;
  try {
    const ids = (order.assets || []).map((a) => a.assetId).concat(assetId);
    const updated = await api(`/api/orders/${oid}`, {
      method: "PATCH", body: { action: "assets", assetIds: ids, force: true },
    });
    setScanErr(oid, "");
    replaceSetupOrder(updated);
    state.setupScanNext = { oid, after: true };
    focusNextScanSlot();
    toast("붙였습니다 — 매입 ▸ ⚠ 번호 충돌에서 번호를 바로잡아 주세요.");
  } catch (err) {
    toast(err.message, true);
  }
}

function showPrebuildReworkNotice(oid, asset) {
  const host = document.createElement("div");
  host.className = "card editor";
  host.style.maxWidth = "620px";
  host.innerHTML = `<div style="padding:18px;">
    <h3 style="margin-top:0;">선제작 사양변경</h3>
    <p><b>${escapeHtml(asset.assetNo || "")}</b> 선제작 완료 제품입니다.</p>
    ${asset.prebuildReworkReason ? `<div class="card" style="margin:12px 0; color:var(--warning-text,#8a4b08);">${escapeHtml(asset.prebuildReworkReason)}</div>` : ""}
    <div style="display:grid; gap:8px; margin:14px 0;">
      <label>RAM 1<div style="display:grid; grid-template-columns:1fr 1fr; gap:6px;">
        <select id="rw-ram1-type">${pbSpecOpts(PB_RAM_TYPES, asset.prebuildRam1Type, "RAM 1 종류")}</select>
        <select id="rw-ram1">${pbSpecOpts(PB_RAM_SPECS, asset.prebuildRam1, "RAM 1 용량")}</select>
      </div></label>
      <label>RAM 2 <span class="muted">(없으면 선택하지 않음)</span><div style="display:grid; grid-template-columns:1fr 1fr; gap:6px;">
        <select id="rw-ram2-type">${pbSpecOpts(PB_RAM_TYPES, asset.prebuildRam2Type === "없음" ? "" : asset.prebuildRam2Type, "RAM 2 종류")}</select>
        <select id="rw-ram2">${pbSpecOpts(PB_RAM_SPECS, asset.prebuildRam2, "RAM 2 용량")}</select>
      </div></label>
      <label>SSD<div style="display:grid; grid-template-columns:1fr 1fr; gap:6px;">
        <select id="rw-ssd-type">${pbSpecOpts(PB_SSD_TYPES, asset.prebuildSsdType, "SSD 종류")}</select>
        <select id="rw-ssd">${pbSpecOpts(PB_SSD_SPECS, asset.prebuildSsd, "SSD 용량")}</select>
      </div></label>
      <label>HDD<select id="rw-hdd" style="width:100%;">${pbSpecOpts(PB_HDD_SPECS, asset.prebuildHdd || "없음", "HDD")}</select></label>
    </div>
    <p class="muted">실제로 변경할 최종 사양을 저장하세요. 이후 제작완료를 누르면 기존 선제작·선SW 실적이 차감되고 변경 작업자에게 사양변경 실적이 반영됩니다.</p>
    <div class="editor-actions"><button class="btn btn-primary" id="pb-rework-ok">사양변경으로 사용</button></div>
  </div>`;
  openModalWith(host);
  $("#pb-rework-ok", host).addEventListener("click", async (e) => {
    const ram1Type = $("#rw-ram1-type", host).value, ram1 = $("#rw-ram1", host).value;
    const ram2Type = $("#rw-ram2-type", host).value || "없음", ram2 = $("#rw-ram2", host).value;
    const ssdType = $("#rw-ssd-type", host).value, ssd = $("#rw-ssd", host).value;
    const hdd = $("#rw-hdd", host).value;
    if (!ram1Type || !ram1 || (ram2Type !== "없음" && !ram2) || !ssdType || !ssd || !hdd) {
      toast("RAM 구성, SSD 종류·용량, HDD를 확인하세요.", true); return;
    }
    e.currentTarget.disabled = true;
    try {
      const updated = await api(`/api/orders/${oid}`, {
        method: "PATCH", body: { action: "prebuildRework", assetId: asset.assetId,
          ram1Type, ram1, ram2Type, ram2, ssdType, ssd, hdd },
      });
      closeModal();
      replaceSetupOrder(updated);
      toast("사양변경 작업으로 전환했습니다. 변경 후 제작완료·SW검수를 진행하세요.");
    } catch (err) { toast(err.message, true); e.currentTarget.disabled = false; }
  });
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
      setScanErr(oid, msg, hit && hit.forceable
        ? { assetId: hit.assetId, assetNo: hit.assetNo } : null);
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
    const matched = (updated.assets || []).find((a) => a.assetId === hit.assetId);
    if (hit.prebuild && hit.prebuild.ready && matched) showPrebuildReworkNotice(oid, matched);
    else toast(`${hit.assetNo} 매칭됨`);
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
/* 송장 발행 모드 표시 — 🧾를 누르면 진짜 접수인지 테스트인지 종이 나가기 전에 알린다.
   (대표 2026-08-24 실발행 테스트 준비 중 확인: 설정은 채워졌는데 환경이 dev·무장 꺼짐이라
    누르면 999 테스트 번호가 나가는 상태였다 — 화면에 아무 표시가 없었다.) */
async function renderWaybillMode() {
  const box = $("#setup-wbmode");
  if (!box) return;
  let d;
  try { d = await api("/api/waybills/mode"); } catch { box.innerHTML = ""; return; }
  if (d.real) {
    // ★실발행 안내 배너는 띄우지 않는다(2026-09-03 대표) — 실발행이 이제 정상 상태라
    //   매번 뜨는 빨간 경고가 화면만 차지한다. '테스트 발행'일 때의 경고는 그대로 둔다
    //   (그건 999 가짜 번호가 종이로 나가는 걸 막는 장치다).
    box.innerHTML = "";
    return;
  }
  // 왜 테스트인지 항목별로 알려 준다 — '무장만 켜면 되는지'를 화면에서 알 수 있게
  const missing = [];
  if (d.env !== "prod") missing.push("환경이 <b>개발(dev)</b>");
  if (!d.armed) missing.push("<b>실발행 무장</b>이 꺼짐");
  if (!d.custId) missing.push("고객코드 없음");
  if (!d.bizReg) missing.push("사업자등록번호 없음");
  if (!d.senderReady) missing.push("출고지(보내는 분) 비어 있음");
  box.innerHTML = `<div class="card" style="padding:10px 14px; margin-bottom:10px;">
    <b>🧪 지금은 테스트 발행입니다</b>
    <span class="muted"> — 송장 번호가 <b>999…</b>로 나가고 CJ에 접수되지 않습니다.</span>
    <div class="muted" style="font-size:12px; margin-top:4px;">
      실발행에 필요한 것: ${missing.join(" · ")}
      ${hasPerm("settings.manage")
        ? ' → 설정 ▸ API 관리 ▸ 🚚 CJ대한통운에서 켜세요.'
        : ' → 관리자에게 요청하세요.'}</div></div>`;
}

/* 출고 확인 되돌리기 — 잘못 눌렀거나 잘못 인쇄한 경우(대표 2026-08-24).
   순서가 중요하다:
   ① 보관돼 있으면 보관 해제(보관 상태에선 단계 변경이 서버에서 막힌다)
   ② 출고 확인 해제 — 서버가 자산을 '주문매칭'으로 원복하고 '출고취소' 이력을 남긴다
   ③ 송장이 있으면: 테스트 발행은 조용히 함께 취소, 실발행은 물어보고 취소
      (실발행 송장을 그대로 두면 CJ 기사가 집화하러 온다). */
/* ✏️ 주문자 정보 수정(대표 2026-08-24, 양식 재지시) — 성함·연락처·주소.
   ★주소는 표준 양식이다: 우편번호 + [🔍 주소 찾기](다음 우편번호 검색) + 기본주소 + 상세주소.
     손으로 치면 오타·비표준 표기가 생겨 CJ 분류가 어긋난다 — 주소 찾기로 채우는 게 FM이다.
     저장할 때 기본+상세를 합쳐 보낸다(orders.address 는 한 칸 — 백엔드 무변경).
   [📍 CJ 주소 확인] = 송장 발급과 같은 주소정제 API. 수정 전 값은 이력에 남아 롤백된다. */
async function openContactEdit(oid) {
  let d;
  try { d = await api(`/api/orders/${oid}/contact-history`); }
  catch (err) { toast(err.message, true); return; }
  const cur = d.current || {};
  const host = document.createElement("div");
  host.innerHTML = `
    <div class="slip-form">
      <div class="sf-title">
        <b>✏️ 주문자 정보 수정</b>
        <span class="muted">— 성함·연락처·주소를 고칩니다. 변경 이력이 남고 되돌릴 수 있습니다.</span>
        ${d.hasWaybill ? `<span class="chip chip-red" title="종이에는 수정 전 주소가 찍혀 있습니다 — 저장 후 [↩ 되돌리기] → 재발급하세요">⚠ 송장 발급됨 — 재발급 필요</span>` : ""}
      </div>
      <section class="sf-step">
        <h4 class="sf-step-head"><span class="sf-no">1</span> 수취인</h4>
        <div class="form-grid">
          <label>수취인 성함<input type="text" id="ct-name" value="${escapeHtml(cur.recipient || "")}"></label>
          <label>연락처<input type="text" id="ct-phone" value="${escapeHtml(cur.phone || "")}"></label>
        </div>
      </section>
      <section class="sf-step">
        <h4 class="sf-step-head"><span class="sf-no">2</span> 주소</h4>
        <div class="form-grid">
          <label>우편번호
            <span class="inline-row" style="gap:6px; margin:0;">
              <input type="text" id="ct-zip" value="${escapeHtml(cur.postalCode || "")}"
                     style="max-width:120px;" placeholder="주소 찾기로 채움">
              <button class="btn btn-sm btn-primary" id="ct-find" type="button"
                title="다음(카카오) 우편번호 검색 — 표준 표기로 채워집니다">🔍 주소 찾기</button>
            </span></label>
          <label>기본주소<input type="text" id="ct-addr" value="${escapeHtml(cur.address || "")}"
                 placeholder="주소 찾기로 채워집니다"></label>
          <label>상세주소<input type="text" id="ct-addr2" value="" placeholder="동·호수 등 — 직접 입력"></label>
        </div>
        <div class="inline-row" style="margin-top:8px;">
          <button class="btn btn-sm" id="ct-orig" type="button"
            title="주문이 처음 들어왔을 때의 성함·연락처·주소·배송메모를 입력칸에 되채웁니다 — [저장]을 눌러야 반영됩니다">↩ 처음 값으로</button>
          <span class="muted" style="font-size:12px;">잘못 고쳤을 때 — 최초 수집값을 칸에 되채웁니다</span>
        </div>
      </section>
      <section class="sf-step">
        <h4 class="sf-step-head"><span class="sf-no">3</span> 배송 메모</h4>
        <div class="form-grid">
          <label style="grid-column:1 / -1;">배송메시지
            <input type="text" id="ct-msg" value="${escapeHtml(cur.deliveryMessage || "")}"
                   placeholder="예: 부재시 경비실에 맡겨주세요"></label>
        </div>
      </section>
      ${(d.history || []).length ? `
      <section class="sf-step">
        <details>
          <summary class="muted" style="cursor:pointer;">변경 이력 ${d.history.length}건 — 되돌리려면 펼치세요</summary>
          <div class="ct-hist">${d.history.map((h) => `
            <div class="ct-hist-row">
              <span class="muted" style="font-size:12px; flex:0 0 118px;">${escapeHtml((h.at || "").slice(5, 16).replace("T", " "))}<br>${escapeHtml(h.by || "")} · ${escapeHtml(h.reason || "")}</span>
              <span style="flex:1; min-width:0; overflow-wrap:anywhere;">${escapeHtml(h.recipient || "-")} · ${escapeHtml(h.phone || "-")}<br>
                <span class="muted">${escapeHtml([h.postalCode, h.address].filter(Boolean).join(" "))}</span></span>
              <button class="btn btn-sm" data-ctroll="${h.id}"
                title="이 시점의 성함·연락처·주소로 되돌립니다(지금 값도 이력에 남습니다)">↩ 이 정보로</button>
            </div>`).join("")}</div>
        </details>
      </section>` : ""}
      <div class="editor-actions sf-actions">
        <button class="btn btn-primary" id="ct-save">저장</button>
        <button class="btn" id="ct-cancel">닫기</button>
      </div>
    </div>`;
  openModalWith(host);
  const fullAddr = () =>
    [$("#ct-addr", host).value.trim(), $("#ct-addr2", host).value.trim()]
      .filter(Boolean).join(" ");
  // 🔍 주소 찾기 — 표준 표기(도로명)로 우편번호·기본주소가 채워진다. 상세는 사람이 마저 적는다.
  $("#ct-find", host).addEventListener("click", (e) => {
    e.preventDefault();
    openAddressSearch((got) => {
      $("#ct-zip", host).value = got.zip;
      $("#ct-addr", host).value = got.addr;
      const detail = $("#ct-addr2", host);
      detail.focus();                     // 바로 상세주소를 적게 커서를 옮긴다
      toast("기본주소가 표준 표기로 채워졌습니다 — 상세주소를 마저 적어 주세요.");
    });
  });
  $("#ct-cancel", host).addEventListener("click", () => closeModal());
  // ↩ 처음 값으로 — 최초 수집값을 '입력칸'에 되채운다([저장]을 눌러야 반영, 실수 안전).
  //   가장 오래된 스냅샷이 곧 원래 값이다(첫 수정 때 '수정 전'으로 남긴 것). 이력이 없으면 지금 값.
  $("#ct-orig", host).addEventListener("click", () => {
    const hist = d.history || [];
    const orig = hist.length ? hist[hist.length - 1] : d.current;
    $("#ct-name", host).value = orig.recipient || "";
    $("#ct-phone", host).value = orig.phone || "";
    $("#ct-zip", host).value = orig.postalCode || "";
    $("#ct-addr", host).value = orig.address || "";
    $("#ct-addr2", host).value = "";
    // ★옛 스냅샷(메모 칸이 생기기 전)엔 메모가 빈 값이다 — 그걸로 지금 메모를 지우면
    //   실수가 되므로, 스냅샷 메모가 비어 있으면 현재 메모를 그대로 둔다.
    $("#ct-msg", host).value = orig.deliveryMessage || d.current.deliveryMessage || "";
    toast("처음 값을 칸에 채웠습니다 — [저장]을 눌러야 반영됩니다.");
  });
  $("#ct-save", host).addEventListener("click", async () => {
    const btn = $("#ct-save", host);
    btn.disabled = true;
    try {
      const r = await api(`/api/orders/${oid}/contact`, { method: "POST", body: {
        recipient: $("#ct-name", host).value, phone: $("#ct-phone", host).value,
        postalCode: $("#ct-zip", host).value, address: fullAddr(),
        deliveryMessage: $("#ct-msg", host).value } });
      toast(r.changed ? "저장했습니다(수정 전 값은 이력에 남았습니다)." : "바뀐 내용이 없습니다.");
      closeModal();
      replaceSetupOrder(r.order);
    } catch (err) { toast(err.message, true); btn.disabled = false; }
  });
  $$("button[data-ctroll]", host).forEach((b) => b.addEventListener("click", async () => {
    if (!confirm("이 시점의 성함·연락처·주소로 되돌립니다.\n지금 값도 이력에 남으므로 다시 되돌릴 수 있습니다.\n\n계속할까요?")) return;
    try {
      const r = await api(`/api/orders/${oid}/contact-rollback`,
                          { method: "POST", body: { historyId: Number(b.dataset.ctroll) } });
      toast("되돌렸습니다.");
      closeModal();
      replaceSetupOrder(r.order);
    } catch (err) { toast(err.message, true); }
  }));
}

/* ➕ 추가 결제(대표 2026-08-24) — 셋팅 중 업그레이드 비용을 기존 주문에 합친다.
   결제 방법에 따라 수수료가 다르다: 입금=0원 / 몰 결제=채널 요율 / 요율 직접.
   금액·수수료는 주문에 바로 합쳐져 매출·자산별 마진에 그대로 반영된다(부가세 포함 총액). */
async function openExtraCharge(oid) {
  const o = (state.setupOrders || []).find((x) => x.id === oid);
  if (!o) return;
  let cur = { extras: [] };
  try { cur = await api(`/api/orders/${oid}/extras`); } catch (_e) { /* 목록 실패해도 등록은 가능 */ }
  const host = document.createElement("div");
  const listHtml = (extras) => extras.length ? `<div class="table-wrap" style="margin-top:10px;"><table>
      <thead><tr><th>금액</th><th>수수료</th><th>방법</th><th>메모</th><th>등록</th><th></th></tr></thead>
      <tbody>${extras.map((x) => `<tr>
        <td><b>${fmtWon(x.amount)}</b></td>
        <td>${x.fee ? fmtWon(x.fee) + ` <span class="muted">(${x.rate}%)</span>` : '<span class="muted">없음</span>'}</td>
        <td>${{ bank: "입금", mall: "몰 결제", custom: "요율 직접" }[x.method] || x.method}</td>
        <td class="muted">${escapeHtml(x.note || "-")}</td>
        <td class="muted" style="font-size:12px;">${escapeHtml(x.createdBy || "")}</td>
        <td><button class="btn btn-ghost btn-sm" data-xdel="${x.id}" title="이 추가 결제를 취소합니다(금액·수수료가 되돌아갑니다)">✕</button></td>
      </tr>`).join("")}</tbody></table></div>` : "";
  // ★매입등록 팝업과 같은 골격(sf-title + 번호 구획 + 하단 초록 버튼, 대표 2026-08-26 "통일성")
  host.innerHTML = `
    <div class="slip-form">
      <div class="sf-title">
        <b>➕ 추가 결제</b>
        <span class="muted">— ${escapeHtml(o.recipient || "")} · ${escapeHtml(o.productName || "")}.
          업그레이드 비용을 이 주문에 합칩니다. <b>금액은 부가세 포함 총액</b>이고,
          매출·수수료가 이 주문(과 매칭된 자산의 마진)에 바로 반영됩니다.</span>
      </div>
      <section class="sf-step">
        <h4 class="sf-step-head"><span class="sf-no">1</span> 금액과 결제 방법</h4>
        <div class="form-grid">
          <label>금액 (부가세 포함)<input type="text" id="xc-amount" data-money placeholder="예: 30,000"></label>
          <label>결제 방법<select id="xc-method">
            <option value="bank">입금(계좌이체) — 수수료 없음</option>
            <option value="mall">몰 결제(카드·에스크로 등) — ${escapeHtml(o.channel || "채널")} 요율 자동</option>
            <option value="custom">요율 직접 입력</option>
          </select></label>
          <label id="xc-rate-wrap" style="display:none;">요율(%)<input type="text" id="xc-rate" placeholder="예: 3.3"></label>
          <label style="grid-column:1 / -1;">메모 <span class="muted">(무엇에 대한 비용인지)</span>
            <input type="text" id="xc-note" placeholder="예: 램 16G 업그레이드"></label>
        </div>
        <p class="muted" id="xc-preview" style="font-size:12.5px; margin:8px 0 0;"></p>
      </section>
      ${(cur.extras || []).length ? `<section class="sf-step">
        <h4 class="sf-step-head"><span class="sf-no">2</span> 이미 합친 추가 결제</h4>
        ${listHtml(cur.extras || [])}
      </section>` : ""}
      <div class="editor-actions sf-actions">
        <button class="btn btn-primary" id="xc-save">합치기</button>
        <button class="btn" id="xc-cancel">닫기</button>
      </div>
    </div>`;
  openModalWith(host);
  const methodSel = $("#xc-method", host);
  const sync = () => {
    $("#xc-rate-wrap", host).style.display = methodSel.value === "custom" ? "" : "none";
    const amt = Number(($("#xc-amount", host).value || "").replace(/[^0-9]/g, "")) || 0;
    const pv = $("#xc-preview", host);
    if (!amt) { pv.textContent = ""; return; }
    if (methodSel.value === "bank") pv.textContent = `수수료 없음 — 실입금 ${fmtNum(amt)}원 전액이 매출·마진에 잡힙니다.`;
    else if (methodSel.value === "custom") {
      const r = Number($("#xc-rate", host).value) || 0;
      pv.textContent = `수수료 ${fmtNum(Math.round(amt * r / 100))}원 (${r}%) 예상`;
    } else pv.textContent = `저장할 때 ${o.channel || "채널"} 요율로 수수료가 자동 계산됩니다.`;
  };
  methodSel.addEventListener("change", sync);
  $("#xc-amount", host).addEventListener("input", sync);
  $("#xc-rate", host).addEventListener("input", sync);
  $("#xc-cancel", host).addEventListener("click", () => closeModal());
  $("#xc-save", host).addEventListener("click", async () => {
    const amt = ($("#xc-amount", host).value || "").replace(/[^0-9]/g, "");
    if (!amt || !Number(amt)) { toast("금액을 입력하세요.", true); return; }
    const btn = $("#xc-save", host);
    btn.disabled = true;
    try {
      const r = await api(`/api/orders/${oid}/extras`, { method: "POST", body: {
        amount: amt, method: methodSel.value,
        rate: $("#xc-rate", host).value, note: $("#xc-note", host).value } });
      toast(`합쳤습니다 — 금액 +${fmtNum(Number(amt))}원`
        + (r.fee ? ` · 수수료 ${fmtNum(r.fee)}원(${r.rate}%)` : " · 수수료 없음"));
      closeModal();
      replaceSetupOrder(r.order);
    } catch (err) { toast(err.message, true); btn.disabled = false; }
  });
  $$("button[data-xdel]", host).forEach((b) => b.addEventListener("click", async () => {
    if (!confirm("이 추가 결제를 취소합니다. 합쳐 둔 금액·수수료가 주문에서 빠집니다.\n계속할까요?")) return;
    try {
      const r = await api(`/api/orders/${oid}/extras/${b.dataset.xdel}`, { method: "DELETE" });
      toast("취소했습니다.");
      closeModal();
      replaceSetupOrder(r.order);
      openExtraCharge(oid);          // 목록을 새로 연다
    } catch (err) { toast(err.message, true); }
  }));
}

async function unshipOrder(oid) {
  const o = (state.setupOrders || []).find((x) => x.id === oid);
  if (!o) return;
  const wb = (o.waybills || []).find((w) => w.type !== "recall"
    && ["issued", "test", "pending"].includes(w.status));
  const real = wb && wb.status === "issued";
  if (!confirm(`출고 확인을 되돌립니다 — ${o.recipient || ""} (${o.productName || ""})\n\n`
      + "· 진행 중 목록으로 돌아오고, 자산도 '주문매칭' 상태로 돌아옵니다.\n"
      + "· 출고일·담당자 기록은 이력에 남습니다.\n"
      + (wb ? (real
          ? "· ★실발행 송장이 있습니다 — 이어서 송장 취소를 물어봅니다.\n"
          : "· 테스트 송장은 함께 취소됩니다(인쇄물은 폐기하세요).\n") : "")
      + "\n계속할까요?")) return;
  try {
    if (o.archivedAt) {
      // 보관된 주문은 단계 변경이 막힌다 — 먼저 꺼낸다
      const r = await api("/api/orders/bulk",
                          { method: "POST", body: { action: "unarchive", ids: [oid] } });
      if (r.failed && r.failed.length) throw new Error(r.failed[0].reason || "보관 해제 실패");
    }
    await api(`/api/orders/${oid}`, {
      method: "PATCH", body: { action: "shipping", value: false } });
    if (wb) {
      // ★실발행은 사람이 결정한다 — 재출고 예정이면 송장을 살려 둘 수도 있다.
      const doCancel = real
        ? confirm(`실발행 송장 ${wb.invoiceNo || wb.wid} 을(를) CJ에서도 취소할까요?\n\n`
            + "· 취소 안 하면 접수가 살아 있어 기사가 집화하러 올 수 있습니다.\n"
            + "· 같은 송장으로 다시 출고할 예정이면 [취소]를 눌러 남겨 두세요.")
        : true;
      if (doCancel) {
        try {
          await api(`/api/waybills/${wb.wid}/cancel`, { method: "POST", body: {} });
          toast(real ? "송장까지 취소했습니다." : "되돌렸습니다 — 테스트 송장도 취소했습니다.");
        } catch (cerr) {
          // CJ가 거절해도(이미 집화 등) 되돌리기 자체는 끝났다 — 어디서 마저 할지 알려 준다
          toast("출고는 되돌렸지만 송장 취소가 실패했습니다: " + cerr.message
            + " — 배송/송장 ▸ 이미 출력된 송장에서 처리하세요.", true);
        }
      } else {
        toast("되돌렸습니다 — 송장은 남겨 두었습니다(배송/송장에서 관리).");
      }
    } else {
      toast("출고 확인을 되돌렸습니다 — 진행 중 목록으로 돌아왔습니다.");
    }
    const btn = $("#sf-search");
    if (btn) btn.click();                        // 작업보드 갱신
  } catch (err) {
    toast(err.message, true);
  }
}

/* ── 옵션라벨(2026-08-31 대표): [제작 완료] 자동인쇄 + 송장 칸 밑 수동 버튼 ────────
   자동인쇄 켜고 끄기는 설정 ▸ 🏷 라벨 ▸ 옵션 라벨에서(전 작업대 공통, 서버 저장).
   이미 뽑은 주문이면 자동/수동 모두 "다시 출력할까요?"를 물어보고, 취소하면 체크만
   남고 인쇄는 하지 않는다. */
async function optLabelAutoOn() {
  try { return (await api("/api/asset-label")).optionAutoPrint !== false; }
  catch (_e) { return true; }        // 설정을 못 읽으면 기본(켜짐)으로 동작
}

async function setupOptLabel(o) {
  if (o.optLabelAt) {
    const when = String(o.optLabelAt).replace("T", " ").slice(0, 16);
    if (!confirm(`이미 출력되었습니다 (${when} · ${o.optLabelBy || "-"}).\n다시 출력할까요?`)) return;
  }
  const opened = await printOptionLabels([o]);        // app.js — 인쇄를 못 열면 false
  if (!opened) {
    toast("옵션라벨 인쇄를 열지 못했습니다 — 송장 칸의 [🏷 옵션라벨]로 다시 시도하세요.", true);
    return;
  }
  // ★인쇄창을 띄운 것과 '실제로 뽑힌 것'은 다르다(2026-09-03 대표 "안 뽑았는데 체크되어 있다").
  //   브라우저는 사용자가 인쇄를 눌렀는지 알려 주지 않으므로 사람에게 물어본다.
  //   [취소]하면 기록하지 않아 목록에서 계속 '안 뽑음'으로 남는다.
  if (!(await confirmPrinted())) {
    toast("표시하지 않았습니다 — 목록에는 '안 뽑음'으로 남습니다.");
    return;
  }
  try {
    await api("/api/orders/bulk", { method: "POST",
      body: { action: "optlabel", ids: [o.id], value: true } });
  } catch (err) { toast("인쇄는 됐지만 기록이 실패했습니다: " + err.message, true); }
  const btn = $("#sf-search");
  if (btn) btn.click();                               // 🏷 표시 갱신
}

/* 🏷 셋팅라벨(옵션라벨) — 제작 대기 ~ SW 검수 완료 단계에서 **행마다 개별로** 뽑는다
   (대표 2026-09-03). 송장과 달리 작업대에서 쓰는 종이라 단계 제한이 없다.
   ★뽑았는지 여부가 눈에 바로 들어와야 한다 — 뽑은 건은 초록 ✓, 안 뽑은 건은 기본 버튼.
     (인쇄창을 띄운 것만으로 ✓가 붙던 문제는 confirmPrinted 로 막았다) */
function optLabelBtn(o) {
  const when = o.optLabelAt ? String(o.optLabelAt).replace("T", " ").slice(0, 16) : "";
  const done = !!o.optLabelAt;
  const qty = Math.max(1, (o.assets || []).length || Number(o.quantity) || 1);
  return `<div style="margin-top:3px;"><button class="btn btn-sm${done ? " btn-done" : ""}"
    data-optlabel="${o.id}"
    title="${done
      ? `셋팅라벨 인쇄됨 · ${escapeHtml(when)} · ${escapeHtml(o.optLabelBy || "")} — 누르면 다시 인쇄합니다`
      : `셋팅라벨 인쇄 — 제품코드·주문자·모델명·옵션표·제공옵션${qty > 1 ? ` (${qty}장: 1/${qty}~${qty}/${qty})` : ""}`}"
    >🏷 ${done ? "라벨 ✓" : `라벨${qty > 1 ? ` ${qty}장` : ""}`}</button></div>`;
}

/* PDF 블롭을 숨은 iframe으로 인쇄 — fetch 뒤 window.open은 팝업 차단에 걸린다
   (옵션라벨·A/S 문서 인쇄와 같은 방식, 2026-08-31). 실패하면 새 탭으로 연다. */
function printPdfBlob(blob) {
  const url = URL.createObjectURL(blob);
  const fr = document.createElement("iframe");
  fr.style.cssText = "position:fixed; right:0; bottom:0; width:0; height:0; border:0;";
  fr.src = url;
  document.body.appendChild(fr);
  fr.addEventListener("load", () => setTimeout(() => {
    try { fr.contentWindow.focus(); fr.contentWindow.print(); }
    catch (_e) { window.open(url, "_blank"); }
  }, 300));
  // 인쇄 대화상자가 떠 있는 동안 제거하면 백지가 나간다 — 넉넉히 기다렸다 지운다
  setTimeout(() => { fr.remove(); URL.revokeObjectURL(url); }, 120000);
}

function waybillCell(o) {
  if (o.cancelledAt) return "";
  // ★셋팅라벨은 **제작 대기·준비 중부터** 뽑는다(대표 2026-09-03 "옵션라벨 출력은 제작대기,
  //   준비중에서 뽑아야 하는데 버튼 만들어줘"). 예전에는 제작 완료 전이면 이 칸을 통째로
  //   비워서 라벨 버튼까지 같이 사라졌다 — 정작 라벨이 필요한 단계가 그때다.
  //   송장은 아래에서 '출고 확인' 뒤에만 나온다.
  if (!o.productionDone) return optLabelBtn(o);
  // 이미 출고 마감된 건은 기록을 보는 화면이다 — 새 송장을 뽑으면 안 된다.
  // 기존 송장이 있으면 아래 분기에서 인쇄만 할 수 있게 남는다.
  const done = o.shippingDone;
  const wb = (o.waybills || []).find((w) => w.type !== "recall"
    && ["issued", "test", "pending"].includes(w.status));
  if (wb) {
    // 발급 중(pending)은 아직 라벨이 없다 — 인쇄를 누르면 오류만 뜨므로 버튼을 감춘다
    if (wb.status === "pending") {
      return `<span class="chip chip-slate wb-line"
        title="오래 걸리면 배송/송장 화면에서 취소한 뒤 다시 발급하세요">발급 중…</span>${optLabelBtn(o)}`;
    }
    const test = wb.status === "test";
    // 송장번호가 쇼핑몰에 올라갔는지(2026-09-08 대표) — 출고 확인 때 자동으로 보낸다.
    //   보내진 뒤엔 조용히(초록), 실패했으면 빨갛게 사유를 보여 준다(재전송은 배송/송장 화면).
    const mall = o.mallSentAt
      ? `<span class="chip chip-green" style="font-size:11px;" title="쇼핑몰에 송장번호를 보냈습니다 ${escapeHtml(o.mallSentAt.slice(0, 16).replace("T", " "))}">🛒 몰 전송</span>`
      : (o.mallSendError
          ? `<span class="chip chip-red" style="font-size:11px;" title="${escapeHtml(o.mallSendError)}&#10;배송/송장 화면 '몰 전송 대기'에서 다시 보냅니다">⚠ 몰 미전송</span>`
          : "");
    return `<span class="wb-line">
      <span class="chip ${test ? "chip-slate" : "chip-green"}"
            title="송장번호 ${escapeHtml(wb.invoiceNo || "")}">${test ? "🧪 테스트" : "🧾 발급"}</span>
      <button class="btn btn-sm" data-wbprint="${escapeHtml(wb.wid)}" title="송장 인쇄">🖨</button>${mall}
    </span>${optLabelBtn(o)}`;
  }
  // ★한 줄로 압축한다. 예전에는 박스수·미리보기·발급이 세로로 쌓여 그 행만 102px가 됐다
  //   (모든 행 같은 높이 — 대표 결정 2026-08-03). 버튼 뜻은 title로 남긴다.
  //   옵션라벨 줄은 2026-08-31 대표가 "송장 버튼 밑에" 지정 — 제작완료 행에만 붙는다.
  const canIssue = hasPerm("orders.ship") || hasPerm("waybills.manage");
  // ★송장은 'SW 검수 완료'부터 뽑는다(대표 2026-09-04 정정 — "SW 검수완료 탭에서
  //   송장 일괄 출력이 가능해야 함"). 검수까지 끝나면 나갈 실물이 확정되니 그 자리에서
  //   송장을 붙여 내보낸다. 제작 대기~제작 완료는 그대로 막는다(아직 확정 전) —
  //   그 단계에서 뽑는 것은 셋팅라벨(옵션라벨)뿐이다.
  if (!o.softwareInspectionDone && !done) {
    return `<span class="wb-line"><span class="muted" style="font-size:11px;"
      title="제작 → SW 검수를 체크하면 그때 송장을 뽑습니다">SW 검수 후 발급</span>
      </span>${optLabelBtn(o)}`;
  }
  // ★상자 수·송장 수는 미리보기 모달에서 정한다 — 행에 우겨넣으면 칸 밖으로 밀려 잘린다
  //   (2026-08-24: 그래서 대표가 "송장은 어디서 출력하냐"고 두 번 물었다).
  return `<span class="wb-line">
    ${canIssue ? `<button class="btn btn-sm btn-primary" data-wbpreview="${o.id}"
        title="송장에 무엇이 찍히는지 보고 발급합니다">🧾 송장</button>`
               : `<button class="btn btn-sm" data-wbpreview="${o.id}"
        title="송장에 무엇이 찍히는지 봅니다">👁 미리보기</button>`}
  </span>${optLabelBtn(o)}`;
}

async function issueWaybillFromSetup(oid, boxQty, wbQty) {
  const wq = Math.max(1, Math.min(Number(wbQty) || 1, 10));
  if (wq > 1 && !confirm(`송장을 ${wq}장 발급합니다(번호가 각각 나옵니다).\n계속할까요?`)) return;
  try {
    const r = await api(`/api/orders/${oid}/waybill`, {
      method: "POST", body: { boxQty: Math.max(1, Math.min(Number(boxQty) || 1, 10)),
                              waybillQty: wq },
    });
    const nos = (r.invoiceNos || [r.invoiceNo]).filter(Boolean).join(", ");
    // 발급 즉시 몰 전송 결과(2026-09-08) — 실패면 빨간 토스트, 사유는 셋팅 보드 칩과 '몰 전송 대기'에도 남는다
    const mp = r.mallPush;
    const mallNote = !mp ? "" : (mp.ok ? ` · 🛒 ${mp.message}` : ` · ⚠ 몰 전송 실패: ${mp.message}`);
    toast(`송장 발급 완료 — ${nos || r.wid}${r.simulated ? " (테스트 발행)" : ""}`
      + " · 출고 확인으로 넘겼습니다" + mallNote, !!(mp && !mp.ok));
    // 다매면 병합 PDF, 아니면 단건 PDF
    if ((r.qty || 1) > 1 && (r.wids || []).length > 1) {
      try {
        const res = await fetch("/api/waybills/print", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ wids: r.wids }),
        });
        if (res.ok) window.open(URL.createObjectURL(await res.blob()), "_blank");
        else window.open(`/api/waybills/${r.wid}/pdf`, "_blank");
      } catch { window.open(`/api/waybills/${r.wid}/pdf`, "_blank"); }
    } else {
      window.open(`/api/waybills/${r.wid}/pdf`, "_blank");
    }
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
        <span style="flex-basis:100%; height:0;"></span>
        <label for="wp-mqty" style="font-size:13px;">🧾 송장 수</label>
        <input type="number" id="wp-mqty" min="1" max="10" value="1"
               style="width:70px; padding:6px 8px; border:1px solid var(--border);
                      border-radius:8px; background:var(--bg);">
        <span class="muted" style="font-size:12px;">
          상자마다 번호를 따로 받아 개별 추적하려면 2 이상(다매) — 자산번호도 3대씩 나눠 실립니다.</span>
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
  const mqIn = $("#wp-mqty");
  if (issueBtn) issueBtn.addEventListener("click", () =>
    issueWaybillFromSetup(oid, boxIn ? boxIn.value : 1, mqIn ? mqIn.value : 1));
  const printBtn = $("#wp-print");
  if (printBtn) printBtn.addEventListener("click", () =>
    window.open(`/api/waybills/${p.existing.wid}/pdf`, "_blank"));
}

/* 행 상세 — QC 프로그램(127.0.0.1:3000)과 같은 4칸 구성
   (대표 요청 2026-08-05): 상품 정보 / 결제 정보 / 구매자·배송지 / 처리 이력. */
/* ── 🔩 부품 재고 바(대표 2026-08-25) ─────────────────────────────
   램·SSD 가용 수량을 보드 맨 위에. 가용 = 현재고 − 대기 소요(아직 체크 안 된
   부품 연결 옵션을 제품코드 스펙으로 확정해 셈). 매입은 매입 탭 [📦 부품 매입]. */
/* 📌 필수 참고 사항(대표 2026-08-26) — 셋팅하면서 꼭 봐야 할 유의사항.
   문구는 설정 ▸ API 관리 ▸ 제공 옵션에서 고친다. 비어 있으면 아무것도 안 그린다. */
async function renderSetupNotice() {
  const host = $("#setup-notice");
  if (!host) return;
  let d;
  try { d = await api("/api/setup-notice"); } catch (_e) { return; }
  if (!$("#setup-notice")) return;
  if (!(d.text || "").trim()) { host.innerHTML = ""; return; }
  host.innerHTML = `
    <div class="card" style="padding:10px 14px; margin:2px 0 10px;
         border-left:3px solid #d97706; background:rgba(217,119,6,.06);">
      <b style="font-size:13px;">📌 필수 참고 사항</b>
      <div style="white-space:pre-wrap; font-size:13px; margin-top:4px; line-height:1.6;">${escapeHtml(d.text)}</div>
    </div>`;
}

async function renderPartsBar() {
  const host = $("#setup-parts-bar");
  if (!host) return;
  let d;
  try { d = await api("/api/part-stocks"); } catch (_e) { return; }
  if (!$("#setup-parts-bar")) return;                 // 그 사이 화면이 바뀌었다
  // ★대제목 탭 구조(대표 2026-08-26 "대제목별/중제목으로 — RAM→RAM 종류별").
  //   ★재고 0이어도 바는 항상 보인다(대표: "왜 안 떠?"). 취급 SKU(단가표 켜짐) 전부.
  //   ★마지막 누른 탭·펼침 상태는 localStorage — 다른 화면을 갔다 와도, 새로고침해도
  //     그대로다(대표: "탭이 최소화되거나 하면 안 돼").
  const items = d.items.filter((x) => x.enabled || x.onhand || x.pending);
  if (!items.length) {
    host.innerHTML = `
      <details id="parts-bar-box" open
        style="border:1px solid var(--border); border-radius:10px; padding:4px 12px; margin:2px 0 10px;">
        <summary style="cursor:pointer; font-size:13px;"><b>🔩 부품 수량</b></summary>
        <p class="muted" style="margin:6px 0 4px; font-size:12.5px;">
          단가표에 램·SSD 부품이 없습니다 — 매입 ▸ 기준정보 ▸ 부품 단가표에서 취급 규격을
          추가하고, 매입 등록의 [🔩 부품 등록]으로 수량을 넣으면 여기 잡힙니다.</p>
      </details>`;
    return;
  }
  const ORDER = ["RAM", "M.2 NVMe", "M.2 SATA", "2.5 SSD", "2.5 HDD",
                 "CPU", "그래픽", "파워", "보드"];
  const byGroup = new Map();
  items.forEach((x) => {
    const g = x.group || "기타";
    if (!byGroup.has(g)) byGroup.set(g, []);
    byGroup.get(g).push(x);
  });
  const groups = [...ORDER.filter((g) => byGroup.has(g)),
                  ...[...byGroup.keys()].filter((g) => !ORDER.includes(g) && g !== "기타").sort(),
                  ...(byGroup.has("기타") ? ["기타"] : [])];
  let tab = "";
  try { tab = localStorage.getItem("ows.partsBarTab") || ""; } catch (_e) {}
  if (!groups.includes(tab)) tab = groups[0];
  let open = true;
  try { open = localStorage.getItem("ows.partsBarOpen") !== "0"; } catch (_e) {}
  const cell = (x) => {
    // 재고도 대기도 0인 규격은 흐리게 — 취급 목록은 보이되 눈은 있는 것부터 가게
    if (!x.onhand && !x.pending) return `<span class="muted"
      style="white-space:nowrap; opacity:.55;">${escapeHtml(x.name)} 0</span>`;
    const danger = x.available <= 0;
    const low = !danger && x.available <= 2;
    return `<span style="white-space:nowrap; ${danger ? "color:var(--danger); font-weight:700;"
        : low ? "color:#b45309; font-weight:600;" : ""}"
      title="현재고 ${x.onhand}개${x.pending ? ` − 대기 소요 ${x.pending}개` : ""}">
      ${escapeHtml(x.name)} <b>${x.available}</b></span>`;
  };
  const lineOf = (g) => {
    const arr = [...(byGroup.get(g) || [])];
    arr.sort((a, b) => ((b.onhand || b.pending) ? 1 : 0) - ((a.onhand || a.pending) ? 1 : 0));
    return arr.map(cell).join(" · ") || '<span class="muted">항목 없음</span>';
  };
  const draw = () => {
    host.innerHTML = `
      <details id="parts-bar-box"${open ? " open" : ""}
        style="border:1px solid var(--border); border-radius:10px; padding:4px 12px; margin:2px 0 10px;">
        <summary style="cursor:pointer; font-size:13px;"><b>🔩 부품 수량</b>
          <span class="muted" style="font-size:12px;">가용(현재고−대기)</span></summary>
        <div class="subtabs" style="max-width:max-content; margin:6px 0 4px;">
          ${groups.map((g) => `<button type="button" class="${g === tab ? "active" : ""}"
            data-pgtab="${escapeHtml(g)}" style="font-size:12px; padding:4px 10px;">${escapeHtml(g)}</button>`).join("")}
        </div>
        <div class="inline-row" style="gap:12px; flex-wrap:wrap; margin:2px 0 6px; font-size:13px; align-items:center;">
          ${lineOf(tab)}
          <button class="btn btn-ghost btn-sm" id="parts-bar-refresh" title="다시 계산">🔄</button>
          ${d.pendingUnresolved > 0 ? `<span class="muted" style="font-size:12px;"
            title="제품코드 스펙(DDR4/DDR5 등)을 몰라 부품을 확정 못 한 옵션 — 대기 소요에서 빠져 있습니다">
            스펙 미확정 ${d.pendingUnresolved}건</span>` : ""}
        </div>
      </details>`;
    $("#parts-bar-box", host).addEventListener("toggle", (e) => {
      open = e.target.open;
      try { localStorage.setItem("ows.partsBarOpen", open ? "1" : "0"); } catch (_e) {}
    });
    $$("button[data-pgtab]", host).forEach((b) => b.addEventListener("click", (e) => {
      e.preventDefault();
      tab = b.dataset.pgtab;
      try { localStorage.setItem("ows.partsBarTab", tab); } catch (_e) {}
      draw();                             // 서버 재호출 없이 탭만 바꾼다
    }));
    $("#parts-bar-refresh", host)?.addEventListener("click", (e) => {
      e.preventDefault();
      renderPartsBar();
    });
  };
  draw();
}

/* 🔗 임시 결제창 합치기(대표 2026-08-26) — 몰에서 업그레이드 비용만 결제한 주문을
   실제 주문의 추가 결제로 흡수한다. 매입등록 팝업과 같은 골격. */
function openMergeExtra(o) {
  const cands = (state.setupOrders || []).filter((x) =>
    x.id !== o.id && !x.cancelledAt && !x.shippingDone);
  // 같은 수취인 먼저 — 임시 결제창은 보통 같은 이름으로 들어온다
  cands.sort((a, b) =>
    ((b.recipient === o.recipient) ? 1 : 0) - ((a.recipient === o.recipient) ? 1 : 0));
  const host = document.createElement("div");
  const rowsHtml = (list) => list.length ? list.slice(0, 30).map((x) => `
      <label class="check-line" style="display:flex; gap:8px; padding:7px 4px;
             border-bottom:1px solid rgba(100,116,139,.16); align-items:center;">
        <input type="radio" name="mx-target" value="${x.id}">
        <b>${escapeHtml(x.recipient || "-")}</b>
        <span class="muted" style="font-size:12px;">${escapeHtml(x.orderNumber || "#" + x.id)}</span>
        <span style="flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;
              font-size:13px;">${escapeHtml(x.productName || "")}</span>
        <span class="muted" style="font-size:12px;">${fmtWon(x.amount)}</span>
        ${x.recipient === o.recipient ? '<span class="chip chip-violet" style="font-size:11px;">같은 이름</span>' : ""}
      </label>`).join("")
    : `<p class="muted">진행 중인 주문이 없습니다.</p>`;
  host.innerHTML = `
    <div class="slip-form">
      <div class="sf-title">
        <b>🔗 기존 주문에 합치기</b>
        <span class="muted">— 이 결제를 실제 주문의 <b>추가 결제</b>로 합치고, 이 임시 주문은
          취소로 내립니다(매출이 두 번 잡히지 않습니다). 수수료는 결제된 몰
          (${escapeHtml(o.channel || "채널")}) 요율로 자동 계산됩니다.</span>
      </div>
      <section class="sf-step">
        <h4 class="sf-step-head"><span class="sf-no">1</span> 합칠 결제</h4>
        <div class="inline-row" style="gap:16px; flex-wrap:wrap; font-size:13.5px;">
          <span>${escapeHtml(o.recipient || "-")}</span>
          <span class="muted">${escapeHtml(o.orderNumber || "#" + o.id)}</span>
          <span style="max-width:340px; overflow:hidden; text-overflow:ellipsis;
                white-space:nowrap;">${escapeHtml(o.productName || "")}</span>
          <b>${fmtWon(o.amount)}</b>
          <span class="chip chip-slate" style="font-size:11.5px;">${escapeHtml(o.channel || "")}</span>
        </div>
      </section>
      <section class="sf-step">
        <h4 class="sf-step-head"><span class="sf-no">2</span> 어느 주문에 합칠까요</h4>
        <input type="text" id="mx-q" placeholder="🔍 수취인/주문번호/상품 검색"
          style="min-width:240px; margin-bottom:6px;">
        <div id="mx-list" style="max-height:300px; overflow-y:auto;">${rowsHtml(cands)}</div>
      </section>
      <div class="editor-actions sf-actions">
        <button class="btn btn-primary" id="mx-save">합치기</button>
        <button class="btn" id="mx-cancel">닫기</button>
      </div>
    </div>`;
  openModalWith(host);
  // ★검색 입력창은 목록 밖 — 다시 그려도 한글 조합이 안 끊긴다(2026-08-26 규칙)
  $("#mx-q", host).addEventListener("input", () => {
    const q = $("#mx-q", host).value.trim().toLowerCase();
    const list = !q ? cands : cands.filter((x) =>
      (x.recipient || "").toLowerCase().includes(q)
      || (x.orderNumber || "").toLowerCase().includes(q)
      || (x.productName || "").toLowerCase().includes(q));
    $("#mx-list", host).innerHTML = rowsHtml(list);
  });
  $("#mx-cancel", host).addEventListener("click", () => closeModal());
  $("#mx-save", host).addEventListener("click", async () => {
    const sel = host.querySelector("input[name=mx-target]:checked");
    if (!sel) { toast("합칠 주문을 선택하세요.", true); return; }
    const tgt = cands.find((x) => x.id === Number(sel.value));
    if (!confirm(`${o.recipient || ""}의 ${fmtNum(o.amount)}원을\\n`
        + `${tgt ? (tgt.recipient || "") + " · " + (tgt.orderNumber || "#" + tgt.id) : "선택 주문"}에 추가 결제로 합칩니다.\\n`
        + "이 임시 주문은 취소로 내려갑니다. 계속할까요?")) return;
    const btn = $("#mx-save", host);
    btn.disabled = true;
    try {
      const r = await api(`/api/orders/${o.id}/merge-extra`, { method: "POST",
        body: { targetOrderId: Number(sel.value) } });
      toast(`합쳤습니다 — +${fmtNum(r.amount)}원`
        + (r.fee ? ` · 수수료 ${fmtNum(r.fee)}원(${r.rate}%)` : " · 수수료 없음"));
      closeModal();
      const canWork = hasPerm("orders.work");
      state.setupOrders = (state.setupOrders || []).filter((x) => x.id !== o.id);
      replaceSetupOrder(r.order);
      renderSetupTagFilters(state.setupOrders, canWork);
      renderSetupRows(setupOrder(filterSetupOrders(state.setupOrders)), canWork);
      renderSetupKpi(state.setupOrders);
    } catch (err) { toast(err.message, true); btn.disabled = false; }
  });
}

async function openPartUse(o) {
  // 램/SSD 수동 차감 — ★매입등록 팝업과 같은 골격(sf-title + 번호 구획 + 하단 초록 버튼,
  // 대표 2026-08-26 "통일성"). 옵션 칩과 같은 관문(_apply_part_to_asset)이라 규칙 동일.
  let stocks = { items: [] };
  try { stocks = await api("/api/part-stocks"); } catch (_e) {}
  const assets = o.assets || [];
  const parts = stocks.items.filter((x) => x.enabled);
  const host = document.createElement("div");
  host.innerHTML = `
    <div class="slip-form">
      <div class="sf-title">
        <b>🔩 램/SSD 차감</b>
        <span class="muted">— 주문 ${escapeHtml(o.orderNumber || "#" + o.id)}${
          o.recipient ? " · " + escapeHtml(o.recipient) : ""}.
          장착하면 재고에서 빠지고 그 자산 원가에 오늘 단가가 기입됩니다(회수는 반대).</span>
      </div>
      <section class="sf-step">
        <h4 class="sf-step-head"><span class="sf-no">1</span> 대상 자산</h4>
        <div class="form-grid">
          <label>자산<select id="pu-asset">${assets.length ? assets.map((a) =>
            `<option value="${a.assetId}">${escapeHtml(a.assetNo)}${a.model ? " · " + escapeHtml(a.model) : ""}</option>`).join("")
            : `<option value="0">매칭 전 — 매칭되면 원가 자동 기입</option>`}</select></label>
          <label>동작<select id="pu-mode">
            <option value="">장착 — 재고에서 빼고 원가에 더함</option>
            <option value="remove">회수 — 재고로 되돌리고 원가에서 뺌</option></select></label>
        </div>
      </section>
      <section class="sf-step">
        <h4 class="sf-step-head"><span class="sf-no">2</span> 부품 고르기</h4>
        <div class="form-grid">
          <label>부품<select id="pu-part">${(() => {
            // ★노트북 부품(RAM/SSD/HDD)이 먼저, 데스크탑 부품(CPU·그래픽·파워·보드)은
            //   별도 묶음으로 아래에(대표 2026-08-26). 각 항목에 가용 수량이 같이 보인다.
            const LAPTOP = ["RAM", "M.2 NVMe", "M.2 SATA", "2.5 SSD", "2.5 HDD"];
            const DESKTOP = ["CPU", "그래픽", "파워", "보드"];
            const opt = (x) => `<option value="${x.id}" data-price="${x.price}" data-avail="${x.available}">${escapeHtml(x.name)} — 가용 ${x.available} · ${fmtWon(x.price)}</option>`;
            const of = (gs) => parts.filter((x) => gs.includes(x.group || ""));
            const rest = parts.filter((x) => !LAPTOP.includes(x.group || "") && !DESKTOP.includes(x.group || ""));
            let html = "";
            LAPTOP.forEach((g) => {
              const ps = parts.filter((x) => (x.group || "") === g);
              if (ps.length) html += `<optgroup label="${g}">${ps.map(opt).join("")}</optgroup>`;
            });
            if (of(DESKTOP).length) html += `<optgroup label="🖥 데스크탑 부품">${of(DESKTOP).map(opt).join("")}</optgroup>`;
            if (rest.length) html += `<optgroup label="기타">${rest.map(opt).join("")}</optgroup>`;
            return html;
          })()}</select></label>
          <label>수량<input type="number" id="pu-qty" value="1" min="1" max="10"></label>
        </div>
        <p class="muted" id="pu-summary" style="margin:8px 0 0; font-size:13px;"></p>
      </section>
      <div class="editor-actions sf-actions">
        <button class="btn btn-primary" id="pu-save">기입</button>
        <button class="btn" id="pu-cancel">닫기</button>
      </div>
    </div>`;
  openModalWith(host);
  // 요약 한 줄이 설명을 대신한다 — 고르는 대로 단가·가용·기입될 원가를 보여 준다
  const paint = () => {
    const sel = $("#pu-part", host).selectedOptions[0];
    if (!sel) { $("#pu-summary", host).textContent = "단가표에 부품이 없습니다."; return; }
    const price = Number(sel.dataset.price) || 0;
    const avail = Number(sel.dataset.avail) || 0;
    const qty = Number($("#pu-qty", host).value) || 1;
    const rm = !!$("#pu-mode", host).value;
    $("#pu-summary", host).innerHTML =
      `단가 <b>${fmtWon(price)}</b> · 가용 <b style="${avail <= 0 ? "color:var(--danger);" : ""}">${avail}개</b>`
      + ` → ${rm ? "회수" : "장착"} ${qty}개 시 원가 <b>${rm ? "−" : "+"}${fmtNum(price * qty)}원</b>`
      + `${rm ? " · 재고 +" + qty : " · 재고 −" + qty}`;
  };
  ["pu-part", "pu-qty", "pu-mode"].forEach((id) =>
    $("#" + id, host).addEventListener(id === "pu-qty" ? "input" : "change", paint));
  paint();
  $("#pu-cancel", host).addEventListener("click", () => closeModal());
  $("#pu-save", host).addEventListener("click", async () => {
    const btn = $("#pu-save", host);
    btn.disabled = true;
    try {
      const r = await api(`/api/orders/${o.id}/part-use`, { method: "POST", body: {
        partId: Number($("#pu-part", host).value),
        assetId: Number($("#pu-asset", host).value),
        qty: Number($("#pu-qty", host).value) || 1,
        remove: !!$("#pu-mode", host).value } });
      toast(r.pending
        ? `${r.name} 재고 차감 — 자산이 매칭되면 원가가 자동 기입됩니다 · 현재고 ${r.onhand}개`
        : `${r.name} ${$("#pu-mode", host).value ? "회수" : "장착"} — 원가 ${fmtNum(r.cost)}원 기입 · 현재고 ${r.onhand}개`);
      closeModal();
      renderPartsBar();
    } catch (err) { toast(err.message, true); btn.disabled = false; }
  });
}

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
    ["SW 검수 완료", o.softwareInspectionDone, o.softwareInspectionBy, o.softwareInspectionAt],
    ["출고 확인", o.shippingDone, o.shippingBy, o.shippingAt],
  ].map(([name, done, by, at]) => `
    <div class="dt-step${done ? " on" : ""}">
      <span class="dt-dot"></span>
      <div><b>${name}</b>
        <div class="muted" style="font-size:12px;">${done
          ? escapeHtml((by || "") + (at ? " · " + String(at).replace("T", " ").slice(0, 16) : ""))
          : "대기"}</div></div>
    </div>`).join("");

  return `<tr class="setup-detail"><td colspan="11">
    <div class="dt-grid">
      <div class="dt-card">
        <div class="dt-head">상품 정보 <span class="chip ${st.chip}">${st.label}</span></div>
        <b style="display:block; margin-bottom:6px;">${escapeHtml(o.productName || "-")}</b>
        ${o.productCode ? `<div class="muted" style="font-size:12.5px;">등록옵션명: ${escapeHtml(o.productCode)}</div>` : ""}
        <div style="margin:6px 0;">${optionChips(o.optionName)}</div>
        ${info && info.spec ? `<div class="muted" style="font-size:12.5px;">${escapeHtml(info.spec)}</div>` : ""}
        ${line("수량", `${o.quantity}개`)}
        ${line("재고", stockChipsHtml(o) || '<span class="muted">-</span>')}
        ${canSeeMenu("setup") && !o.cancelledAt
          ? `<div style="margin-top:6px;" class="inline-row" data-partuse-row="${o.id}">
               <button class="btn btn-ghost btn-sm" data-partuse="${o.id}"
                 title="램·SSD를 장착(재고 차감)하거나 회수합니다.${(o.assets || []).length
                   ? " 원가는 고른 자산에 기입됩니다."
                   : "&#10;자산 매칭 전이라도 재고는 바로 차감되고, 매칭되는 순간 원가가 자산에 자동 기입됩니다."}">🔩 램/SSD 차감</button>
               <span class="muted" style="font-size:12px;" data-partuse-status="${o.id}"></span>
             </div>` : ""}
        ${o.memo ? line("내부 메모", escapeHtml(o.memo)) : ""}
      </div>
      <div class="dt-card">
        <div class="dt-head">결제 정보</div>
        ${line("결제금액", `<b>${fmtWon(o.amount)}</b>
          ${canSeeMenu("setup") && !o.shippingDone && !o.cancelledAt && !(o.assets || []).length
            ? `<button class="btn btn-ghost btn-sm" data-mergex="${o.id}" style="margin-left:6px;"
                 title="이 주문이 업그레이드 비용만 결제한 임시 결제창이면, 실제 주문에 추가 결제로 합칩니다">🔗 기존 주문에 합치기</button>` : ""}
          ${canSeeMenu("setup") && !o.shippingDone && !o.cancelledAt
            ? `<button class="btn btn-ghost btn-sm" data-extra="${o.id}" style="margin-left:6px;"
                 title="업그레이드 등 추가 결제를 이 주문에 합칩니다 — 결제 방법에 따라 수수료가 다르게 붙습니다">➕ 추가 결제</button>` : ""}`)}
        ${line("주문일", escapeHtml((o.orderedAt || "").replace("T", " ").slice(0, 19)))}
        ${line("채널", escapeHtml(o.channel || ""))}
        ${line("수령방식", escapeHtml(o.receiveMethod || "택배"))}
        ${o.orderNumber ? line("주문번호", escapeHtml(o.orderNumber)) : ""}
      </div>
      <div class="dt-card">
        <div class="dt-head">구매자 / 배송지${o.piiMasked
          ? ' <span class="muted" style="font-weight:400;">일부 가림</span>' : ""}
          ${!o.cancelledAt ? `<button class="btn btn-ghost btn-sm" data-contact="${o.id}"
            style="float:right;" title="성함·연락처·주소를 고칩니다 — CJ 주소 확인과 변경 이력 롤백이 됩니다">✏️ 수정</button>` : ""}</div>
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

/* 자산 매칭 경고 — 주문이 요구한 제품과 다른 자산을 붙였을 때 그 행 아래에 띄운다.
   (대표 2026-09-03 "실제 주문 건과 맞지 않는 제품입니다 라는 문구가 하단에 뜨게")
   저장은 이미 됐다 — 되돌리려면 자산 칸을 비우고 다시 저장하면 된다. */
function showAssetWarnings(oid, warnings) {
  const list = warnings || [];
  const row = document.querySelector(`#setup-rows [data-more="${oid}"]`)?.closest("tr");
  document.querySelectorAll(`tr.asset-warn-row[data-warn="${oid}"]`).forEach((n) => n.remove());
  if (!list.length || !row) return;
  const tr = document.createElement("tr");
  tr.className = "asset-warn-row";
  tr.dataset.warn = String(oid);
  tr.innerHTML = `<td colspan="11" class="asset-warn">
    ${list.map((w) => `<div>⚠ ${escapeHtml(w)}</div>`).join("")}
    <button class="btn btn-sm asset-warn-x" title="이 알림 닫기">확인했습니다</button></td>`;
  row.after(tr);
  tr.querySelector(".asset-warn-x").addEventListener("click", () => tr.remove());
  toast("주문 제품과 다른 자산이 붙었습니다 — 목록에서 확인하세요.", true);
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
    const specChange = (o.assets || []).find((a) => a.prebuildReworkRequired);
    // 발급된(취소 아닌) 송장이 있는 건만 고를 수 있다 — 없는 걸 골라 봐야 인쇄가 안 된다
    const printable = (o.waybills || []).filter(
      (w) => w.type !== "recall" && ["issued", "test"].includes(w.status)).length;
    return `<tr>
      <td style="text-align:center;"><input type="checkbox" class="sf-pick" data-oid="${o.id}"
        ${printable && !o.cancelledAt ? "" : "disabled"}
        title="${printable ? "송장 인쇄 대상으로 고릅니다" : "발급된 송장이 없습니다"}"></td>
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
      ${stageCell(o, "production", o.productionDone, o.productionDone ? o.productionBy : "", canWork, o.productionAt)}
      ${stageCell(o, "softwareInspection", o.softwareInspectionDone, o.softwareInspectionDone ? o.softwareInspectionBy : "", canWork, o.softwareInspectionAt)}
      ${stageCell(o, "shipping", o.shippingDone, o.shippingDone ? o.shippingBy : "", canWork, o.shippingAt)}
      <td class="setup-wb">${waybillCell(o)}</td>
      <td class="setup-status"><span class="chip ${st.chip}">${st.label}</span>${specChange
        ? `<div><span class="chip chip-amber" title="${escapeHtml(specChange.prebuildReworkReason || "")}">제작완료 = 사양변경</span></div>` : ""}${
        shipDoneLine(o)}${
        // ★출고 확인을 잘못 눌렀을 때의 되돌리기(대표 2026-08-24) — 출고 기록 조회에서 쓴다.
        o.shippingDone && !o.cancelledAt && canWork
          ? `<div><button class="btn btn-ghost btn-sm" data-unship="${o.id}"
               title="출고 확인을 되돌려 진행 중 목록으로 되살립니다.&#10;자산도 '출고완료'에서 '주문매칭'으로 돌아옵니다.">↩ 되돌리기</button></div>`
          : ""}${hasPerm("orders.cancel") && !o.cancelledAt
          ? `<button type="button" class="btn btn-sm setup-cancel" data-setup-cancel="${o.id}"
              title="이 화면에서 주문을 취소합니다">주문취소</button>` : ""}</td>
    </tr>${(state.setupUnitsOpen || {})[o.id] !== false && (Number(o.quantity) || 1) > 1
        ? unitsRow(o, canWork) : ""}${
      state.setupOpen && state.setupOpen[o.id] ? setupDetailRow(o) : ""}`;
  }).join("") || `<tr><td colspan="11" class="muted">진행 중인 주문이 없습니다.</td></tr>`;

  // 상세 보기 — 행 아래에 4칸(상품/결제/구매자·배송지/처리이력)을 편다.
  // ★행을 다시 그리는 건 이 함수라 여기서 바로 갱신한다(폴링과 충돌하지 않게).
  $$("button[data-more]", host).forEach((b) => b.addEventListener("click", () => {
    const oid = Number(b.dataset.more);
    state.setupOpen = state.setupOpen || {};
    if (state.setupOpen[oid]) delete state.setupOpen[oid];
    else state.setupOpen[oid] = true;
    renderSetupRows(orders, canWork);
  }));
  // 작업 비고 — 주문관리로 이동하지 않고 이 행에서 작성·수정한다.
  $$("button[data-setup-memo]", host).forEach((b) => b.addEventListener("click", async () => {
    const oid = Number(b.dataset.setupMemo);
    const order = (state.setupOrders || []).find((x) => x.id === oid);
    if (!order) return;
    const memo = prompt("셋팅 비고를 입력하세요. 빈칸으로 저장하면 삭제됩니다.", order.memo || "");
    if (memo === null || memo.trim() === String(order.memo || "").trim()) return;
    if (memo.trim().length > 500) { toast("비고는 500자 이하로 입력하세요.", true); return; }
    b.disabled = true;
    try {
      const updated = await api(`/api/orders/${oid}`, {
        method: "PATCH", body: { action: "setupMemo", memo: memo.trim() },
      });
      replaceSetupOrder(updated);
      toast(memo.trim() ? "셋팅 비고를 저장했습니다." : "셋팅 비고를 삭제했습니다.");
    } catch (err) { toast(err.message, true); b.disabled = false; }
  }));
  // 주문취소 — 서버의 기존 출고·송장 안전검사를 그대로 거친다.
  $$("button[data-setup-cancel]", host).forEach((b) => b.addEventListener("click", async () => {
    const oid = Number(b.dataset.setupCancel);
    const order = (state.setupOrders || []).find((x) => x.id === oid);
    if (!order) return;
    const reason = prompt(`${order.recipient || "이 고객"} 주문의 취소 사유를 입력하세요.`);
    if (reason === null) return;
    if (!reason.trim()) { toast("취소 사유를 입력하세요.", true); return; }
    if (!confirm(`주문 ${order.orderNumber || "#" + oid}을 취소할까요?\n매칭 자산은 판매 가능 상태로 돌아갑니다.`)) return;
    b.disabled = true;
    try {
      await api(`/api/orders/${oid}`, {
        method: "PATCH", body: { action: "cancel", reason: reason.trim() },
      });
      state.setupOrders = (state.setupOrders || []).filter((x) => x.id !== oid);
      renderSetupTagFilters(state.setupOrders, canWork);
      renderSetupRows(setupOrder(filterSetupOrders(state.setupOrders)), canWork);
      renderSetupKpi(state.setupOrders);
      toast("주문을 취소했습니다.");
    } catch (err) { toast(err.message, true); b.disabled = false; }
  }));
  // 챙길 옵션 칩 — 누르면 체크/해제. 이게 없으면 제작완료가 영영 안 눌린다.
  $$("button[data-prep]", host).forEach((b) => b.addEventListener("click", async () => {
    const oid = Number(b.dataset.prep);
    const optId = Number(b.dataset.opt);
    const on = b.classList.contains("chip-green");
    b.disabled = true;
    try {
      const r = await api(`/api/orders/${oid}/options/${optId}`, {
        method: "POST", body: { checked: !on } });
      // 부품 원가 자동 기입 결과를 알려 준다 — 작업자는 칩만 눌렀을 뿐이다
      if (r.partApplied) {
        toast(`부품 원가 ${(r.partCost || 0).toLocaleString("ko-KR")}원이 `
          + `자산 ${r.partApplied}대에 자동 기입되었습니다.`);
      } else if (r.partMessage) {
        toast(r.partMessage, r.partMessage.includes("단가가 없어"));
      }
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
      // ★제작완료 → 옵션라벨 자동인쇄(2026-08-31 대표). 설정 ▸ 라벨 ▸ 옵션 라벨에서
      //   끄면 체크만 된다. 이미 뽑은 주문은 setupOptLabel 이 "다시 출력할까요?"를 물어본다.
      if (cb.dataset.stage === "production" && cb.checked) {
        optLabelAutoOn().then((on) => { if (on) setupOptLabel(updated); });
      }
    } catch (err) {
      toast(err.message, true);
      if (err.data && err.data.order) replaceSetupOrder(err.data.order);
      else cb.checked = !cb.checked;
    } finally { cb.disabled = false; }
  }));
  $$("button[data-optlabel]", host).forEach((b) => b.addEventListener("click", () => {
    const o = (state.setupOrders || []).find((x) => x.id === Number(b.dataset.optlabel));
    if (o) setupOptLabel(o);
  }));
  $$("button[data-match]", host).forEach((b) => b.addEventListener("click", () => {
    renderMatchPanel(Number(b.dataset.match));
  }));
  $$("button[data-who]", host).forEach((b) => b.addEventListener("click", () => {
    showRecipient(Number(b.dataset.who));
  }));
  $$("button[data-force-oid]", host).forEach((b) => b.addEventListener("click", () => {
    forceMatchAsset(Number(b.dataset.forceOid), Number(b.dataset.forceAid));
  }));
  $$("button[data-units]", host).forEach((b) => b.addEventListener("click", () => {
    state.setupUnitsOpen = state.setupUnitsOpen || {};
    const oid = Number(b.dataset.units);
    // 기본이 펼침이므로 '명시적으로 접었나(false)'만 뒤집는다
    state.setupUnitsOpen[oid] = state.setupUnitsOpen[oid] === false;
    renderSetupRows(state.setupOrders ? setupOrder(filterSetupOrders(state.setupOrders)) : [], canWork);
  }));
  $$("button[data-contact]", host).forEach((b) => b.addEventListener("click", () => {
    openContactEdit(Number(b.dataset.contact));
  }));
  $$("input[data-prep-unit]", host).forEach((cb) => cb.addEventListener("change", async () => {
    const [oid, aid] = cb.dataset.prepUnit.split(":").map(Number);
    cb.disabled = true;
    try {
      const r = await api(`/api/orders/${oid}/assets/${aid}/prepared`,
                          { method: "POST", body: { value: cb.checked } });
      replaceSetupOrder(r.order);
    } catch (err) {
      toast(err.message, true);
      cb.checked = !cb.checked;
      cb.disabled = false;
    }
  }));
  $$("button[data-unship]", host).forEach((b) => b.addEventListener("click", () => {
    unshipOrder(Number(b.dataset.unship));
  }));
  $$("button[data-prebuild-rework]", host).forEach((b) => b.addEventListener("click", () => {
    const [oid, aid] = b.dataset.prebuildRework.split(":").map(Number);
    const order = (state.setupOrders || []).find((x) => x.id === oid);
    const asset = order && (order.assets || []).find((x) => x.assetId === aid);
    if (asset) showPrebuildReworkNotice(oid, asset);
  }));
  $$("button[data-extra]", host).forEach((b) => b.addEventListener("click", () => {
    openExtraCharge(Number(b.dataset.extra));
  }));
  $$("button[data-mergex]", host).forEach((b) => b.addEventListener("click", () => {
    const o = (state.setupOrders || []).find((x) => x.id === Number(b.dataset.mergex));
    if (o) openMergeExtra(o);
  }));
  $$("button[data-partuse]", host).forEach((b) => b.addEventListener("click", () => {
    const o = (state.setupOrders || []).find((x) => x.id === Number(b.dataset.partuse));
    if (o) openPartUse(o);
  }));
  // 차감 현황 — 옵션 칩으로 자동차감됐으면 '자동차감됨'을 적는다(대표 2026-08-25)
  $$("span[data-partuse-status]", host).forEach(async (el) => {
    try {
      const d = await api(`/api/orders/${el.dataset.partuseStatus}/part-uses`);
      if (!el.isConnected) return;
      el.innerHTML = d.items.length
        ? d.items.map((x) => `<span class="chip" style="font-size:11.5px;"
            title="${escapeHtml(x.assetNo)}">${escapeHtml(x.name)} ×${x.qty}${
            x.remove ? " 회수" : ""} · ${x.auto ? "✅ 자동차감됨" : "수동"}</span>`).join(" ")
        : '<span style="font-size:12px;">아직 차감 없음</span>';
    } catch (_e) { /* 표시는 장식 — 실패해도 보드는 산다 */ }
  });
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
  /* 행에는 [🧾 송장] 하나뿐이다 — 상자수·송장수는 미리보기 모달에서 정한다
     (행에 우겨넣으면 상태 칸 폭에 밀려 잘린다, 2026-08-24). */
  $$("button[data-wbpreview]", host).forEach((b) => b.addEventListener("click", () =>
    renderWaybillPreview(Number(b.dataset.wbpreview), 1)));
  /* 행의 [🧾 송장]은 미리보기를 연다 — 발급은 모달에서 눈으로 보고 누른다 */
  $$("button[data-wbprint]", host).forEach((b) => b.addEventListener("click", () =>
    window.open(`/api/waybills/${b.dataset.wbprint}/pdf`, "_blank")));
}

function replaceSetupOrder(updated) {
  const idx = (state.setupOrders || []).findIndex((x) => x.id === updated.id);
  if (idx >= 0) state.setupOrders[idx] = updated;
  renderSetupTagFilters(state.setupOrders || [], hasPerm("orders.work"));
  renderSetupRows(setupOrder(filterSetupOrders(state.setupOrders || [])), hasPerm("orders.work"));
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
  // ★번호 충돌로 강제로 넣은 자산이 하나라도 있으면 저장할 때 force 를 보낸다
  //   (대표 2026-08-18: "중복이라고 떠도 셋팅/QC에서는 입력은 되게").
  let forcedUsed = false;

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
      const fail = (text, hit) => {
        const m = $("#mt-scan-msg");
        if (m) {
          m.textContent = text;
          m.style.color = "var(--danger)";
          // TMS 오기입으로 막힌 경우 — 실물이 있으면 그대로 넣을 수 있게 해 준다
          if (hit && hit.forceable) {
            const b = document.createElement("button");
            b.className = "btn btn-sm";
            b.style.marginLeft = "8px";
            b.textContent = "⚠ 실물 있음 — 그대로 넣기";
            b.title = "TMS에서 번호를 잘못 적어 판매로 찍힌 경우입니다.\n"
                    + "넣고 저장하면 매입 ▸ ⚠ 번호 충돌 목록에 올라갑니다.";
            b.addEventListener("click", () => { forcedUsed = true; put(index, hit); });
            m.appendChild(b);
          }
        }
        const box = $(`.mt-slot[data-slot="${index}"]`);
        if (box) { box.value = ""; box.focus(); }
      };
      const put = (i, hit) => {
        const cell = { assetId: hit.assetId, assetNo: hit.assetNo, model: hit.model };
        if (i < slots.length) slots[i] = cell; else slots.push(cell);
        draw(firstEmpty());
        const m = $("#mt-scan-msg");
        if (m) {
          m.textContent = hit.prebuild && hit.prebuild.ready
            ? `✅ 선제작 완료 제품 ${hit.assetNo} 추가됨 — 저장하면 제작완료·SW검수를 자동 반영합니다.`
            : `${hit.assetNo} 추가됨`;
          m.style.color = "";
        }
      };
      try {
        const hit = (await api("/api/orders/asset-search?q=" + encodeURIComponent(no)))
          .find((r) => r.assetNo.toUpperCase() === no.toUpperCase());
        if (!hit) return fail(`'${no}' — 자산으로 등록되지 않은 번호입니다.`);
        // ★상태가 안 되는 자산은 그대로 막는다(A/S 회수품이 판매 재고로 섞이면 남의 물건을 판다).
        //   다만 '왜 안 되는지'는 알려 준다 — 예전엔 아무 말이 없어 번호를 계속 다시 찍었다.
        if (!hit.available) return fail(`${hit.assetNo} — ${hit.reason}`, hit);
        if (slots.some((x) => x && x.assetId === hit.assetId))
          return fail(`${hit.assetNo}는 이미 이 주문에 있습니다.`);
        put(index, hit);                          // 다시 그리면서 다음 빈 칸으로 커서 이동
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
            <td>${r.available || !r.forceable
              ? `<button class="btn btn-sm" data-add="${r.assetId}" data-no="${escapeHtml(r.assetNo)}"
                   ${!r.available || slots.some((c) => c && c.assetId === r.assetId) ? "disabled" : ""}
                   >추가</button>`
              : `<button class="btn btn-sm" data-force-add="${r.assetId}"
                   title="TMS 오기입으로 막힌 자산입니다 — 실물이 있으면 그대로 넣습니다"
                   ${slots.some((c) => c && c.assetId === r.assetId) ? "disabled" : ""}
                   >⚠ 그대로</button>`}</td></tr>`).join("")}
          </tbody></table></div>` : `<p class="muted">검색 결과가 없습니다. 자산으로 등록되지 않은 번호입니다.</p>`;
        $$("button[data-add]", host).forEach((b) => b.addEventListener("click", () => {
          const i = firstEmpty();
          putAt(i >= 0 ? i : slots.length, b.dataset.no);
        }));
        $$("button[data-force-add]", host).forEach((b) => b.addEventListener("click", () => {
          const r = rows.find((x) => x.assetId === Number(b.dataset.forceAdd));
          if (!r) return;
          forcedUsed = true;
          const i = firstEmpty();
          const cell = { assetId: r.assetId, assetNo: r.assetNo, model: r.model };
          if (i >= 0) slots[i] = cell; else slots.push(cell);
          draw(firstEmpty());
        }));
      } catch (err) { toast(err.message, true); }
    };
    $("#mt-search").addEventListener("click", search);
    autoSearch("#mt-q", search);

    $("#mt-save").addEventListener("click", async () => {
      if (forcedUsed && !confirm(
          "TMS에는 나간 것으로 적혀 있지만 실물이 여기 있다는 뜻입니다.\n\n"
          + "매입 ▸ ⚠ 번호 충돌 목록에 올라가고, TMS 자동반영이 이 자산을 못 건드리게 잠급니다.\n"
          + "나중에 매입에서 번호를 바로잡아 주세요.\n\n계속할까요?")) return;
      try {
        const updated = await api(`/api/orders/${oid}`, {
          method: "PATCH", body: { action: "assets", assetIds: filled().map((c) => c.assetId),
                                   ...(forcedUsed ? { force: true } : {}) },
        });
        const rework = (updated.assets || []).find((a) => a.isPrebuilt);
        host.innerHTML = "";
        replaceSetupOrder(updated);
        // ★주문 제품과 다른 자산을 붙였으면 알린다 — 막지는 않는다(대표 2026-09-03).
        //   대체 출고가 실제로 있으므로 '틀렸다'가 아니라 '확인하라'로 띄운다.
        showAssetWarnings(oid, updated.assetWarnings);
        if (rework) showPrebuildReworkNotice(oid, rework);
        else toast(forcedUsed ? "저장했습니다 — 매입 ▸ ⚠ 번호 충돌에서 번호를 바로잡아 주세요."
                              : "자산 매칭을 저장했습니다.");
      } catch (err) {
        toast(err.message, true);
        if (err.data && err.data.order) replaceSetupOrder(err.data.order);
      }
    });
  };
  draw();
}
