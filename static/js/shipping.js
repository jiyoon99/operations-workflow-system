/* OWS 배송/송장 — QC 완료 주문의 송장 선출력(자산번호 인쇄) + 출고 확인 + 송장 목록 */
"use strict";

/* 회수한 물건을 되돌릴 상태 — 화면에서 골라 쓰도록 한글 라벨을 붙여 둔다 */
const RECEIVE_STATUSES = [
  ["in_stock", "입고 (그대로 재고로)"],
  ["refurbishing", "정비중 (손봐서 다시 판매)"],
  ["repair", "수리"],
  ["defective", "불량"],
  ["scrapped", "폐기"],
];

/* 송장 칸 — 발급 조건·발급 중·재발급을 한자리에서 다룬다(2026-07-29 전수조사).
   ★예전엔 버튼이 항상 활성이라, 주소 없는 주문(실데이터 28건 중 23건)에서 누를 때마다
     '수취인과 주소를 먼저 입력하세요' 토스트만 뜨고 끝났다. 무엇이 빠졌는지 행에서 바로 보이게 한다. */
function waybillBlockers(o) {
  const out = [];
  if (!o.assets.length && !o.isReview) out.push("자산 미매칭");
  if (!(o.recipient || "").trim()) out.push("수취인 없음");
  if (!(o.address || "").trim()) out.push("주소 없음");
  return out;
}

function waybillCellHtml(o, activeWb, canShip) {
  if (activeWb && activeWb.status === "pending") {
    // 발급이 도중에 멈춘 상태 — PDF를 누르면 영문 오류만 떴다
    return `<span class="chip chip-violet">발급 중</span>
      <div class="muted" style="font-size:12px;">택배사 응답을 기다리는 중입니다.
        몇 분 지나도 그대로면 [송장 목록]에서 취소 후 다시 발급하세요.</div>`;
  }
  if (activeWb) {
    return `<b>${escapeHtml(activeWb.invoiceNo)}</b>${
        activeWb.status === "test" ? ' <span class="chip chip-slate">테스트</span>' : ""}
      <button class="btn btn-sm" data-pdf="${activeWb.wid}">PDF</button>
      ${canShip && activeWb.status !== "delivered"
        ? `<button class="btn btn-ghost btn-sm" data-reissue="${o.id}" data-wid="${activeWb.wid}"
             title="기존 송장을 취소하고 새로 발급합니다">재발급</button>` : ""}`;
  }
  // ★송장 발급은 셋팅/QC 한 곳에서만 한다(대표 2026-08-24: "기존 주문 건에 대해서
  //   송장 뽑는 기능은 QC 셋팅쪽 탭에서 진행되어야 함"). 발급 경로가 두 곳이면
  //   같은 주문에 두 사람이 동시에 눌러 중복 발급이 난다.
  //   여기서는 '왜 아직 송장이 없는지'만 알리고 셋팅으로 보낸다.
  const blockers = waybillBlockers(o);
  return `<span class="chip chip-slate">송장 없음</span>
    ${blockers.length
      ? `<div class="chip chip-red" style="margin-top:4px; white-space:normal;">${escapeHtml(blockers.join(" · "))}</div>`
      : ""}
    <div class="muted" style="font-size:11px; margin-top:4px;">
      발급은 <b>셋팅</b> 탭에서 합니다${blockers.length ? " — 위 사항을 먼저 채우세요" : ""}.</div>
    ${canSeeMenu("setup") ? `<button class="btn btn-ghost btn-sm" data-gosetup="${o.id}"
        style="margin-top:4px;" title="셋팅 작업보드로 이동합니다">셋팅에서 발급 →</button>` : ""}`;
}

/* 여러 송장(다매)이면 병합 PDF로, 한 장이면 단건 PDF로 연다 */
async function openIssuedPdf(res) {
  if ((res.qty || 1) > 1 && (res.wids || []).length > 1) {
    try {
      const r = await fetch("/api/waybills/print", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ wids: res.wids }),
      });
      if (!r.ok) throw new Error("병합 인쇄 실패 — 송장 목록에서 개별 인쇄하세요.");
      window.open(URL.createObjectURL(await r.blob()), "_blank");
      return;
    } catch (err) { toast(err.message, true); }
  }
  window.open(`/api/waybills/${res.wid}/pdf`, "_blank");
}

/* 🆕 송장 신규 등록 — 주문·A/S 없이 송장을 직접 만든다(RMS 송장관리 이식, 대표 2026-09-03).
   ·출고: 우리가 보낸다 — 송장번호가 바로 나온다
   ·회수: 고객에게서 받아 온다 — 번호는 기사가 집화할 때 CJ가 만든다(수거 희망일 지정 가능)
   ·예약: CJ를 부르지 않고 기록만 남긴다 — 나중에 실제 접수로 올린다 */
function openManualWaybill() {
  const host = document.createElement("div");
  host.innerHTML = `
    <div class="card" style="max-width:560px;">
      <h3>🆕 송장 신규 등록</h3>
      <p class="muted" style="font-size:12px;margin-top:-4px;">
        주문·A/S 건이 없는 발송에 씁니다(견본 발송, 부품만 보내기, 반품 회수 등).</p>
      <div class="form-row">
        <label>종류</label>
        <select id="mw-type">
          <option value="forward">🚚 출고 — 우리가 보냅니다</option>
          <option value="recall">↩️ 회수 — 고객에게서 받아 옵니다</option>
        </select>
      </div>
      <div class="form-row"><label>받는 분 <b style="color:var(--danger)">*</b></label>
        <input type="text" id="mw-name" data-autofocus placeholder="성함 또는 상호"></div>
      <div class="form-row"><label>연락처</label>
        <input type="text" id="mw-tel" placeholder="010-0000-0000"></div>
      <div class="form-row"><label>우편번호</label>
        <span class="inline-row" style="gap:6px;">
          <input type="text" id="mw-zip" style="max-width:110px;" placeholder="00000">
          <button class="btn btn-sm" id="mw-addr-find">🔍 주소 검색</button></span></div>
      <div class="form-row"><label>주소 <b style="color:var(--danger)">*</b></label>
        <input type="text" id="mw-addr" placeholder="도로명 주소"></div>
      <div class="form-row"><label>상세주소</label>
        <input type="text" id="mw-addr2" placeholder="동·호수 등"></div>
      <div class="form-row"><label>품목</label>
        <input type="text" id="mw-items" placeholder="예: 노트북 어댑터 1개"></div>
      <div class="form-row"><label>상자 수</label>
        <input type="number" id="mw-box" value="1" min="1" max="10" style="max-width:90px;"></div>
      <div class="form-row" id="mw-pick-row" style="display:none;">
        <label>수거 희망일</label>
        <input type="date" id="mw-pickup" style="max-width:170px;">
        <span class="muted" style="font-size:12px;"> 기사에게 전달됩니다</span></div>
      <div class="form-row"><label>메모</label>
        <input type="text" id="mw-memo" maxlength="60" placeholder="기사에게 보이는 문구(60자)"></div>
      <label class="check-line" style="margin-top:6px;">
        <input type="checkbox" id="mw-reserve">
        <span>예약만 하기 — <b>CJ에 접수하지 않고</b> 기록만 남깁니다(나중에 접수)</span></label>
      <div class="inline-row" style="margin-top:12px;">
        <button class="btn btn-primary" id="mw-save">등록</button>
        <button class="btn" id="mw-cancel">닫기</button>
      </div>
    </div>`;
  openModalWith(host);
  const $$$ = (id) => host.querySelector(id);
  // 회수일 때만 수거 희망일 칸을 보여 준다(출고에는 쓰지 않는 값이다)
  $$$("#mw-type").addEventListener("change", (e) => {
    $$$("#mw-pick-row").style.display = e.target.value === "recall" ? "" : "none";
  });
  // attachAddrSearch 는 '선택자 문자열'을 받는다(요소가 아니다)
  attachAddrSearch($$$("#mw-addr-find"),
                   { zip: "#mw-zip", addr: "#mw-addr", detail: "#mw-addr2" });
  $$$("#mw-cancel").addEventListener("click", () => closeModal());
  $$$("#mw-save").addEventListener("click", async () => {
    const btn = $$$("#mw-save");
    const body = {
      type: $$$("#mw-type").value,
      recipient: $$$("#mw-name").value.trim(),
      phone: $$$("#mw-tel").value.trim(),
      postalCode: $$$("#mw-zip").value.trim(),
      address: $$$("#mw-addr").value.trim(),
      addressDetail: $$$("#mw-addr2").value.trim(),
      items: $$$("#mw-items").value.trim(),
      boxQty: Number($$$("#mw-box").value) || 1,
      pickupDate: $$$("#mw-pickup") ? $$$("#mw-pickup").value : "",
      memo: $$$("#mw-memo").value.trim(),
      reserve: $$$("#mw-reserve").checked,
    };
    if (!body.recipient || !body.address) {
      toast("받는 분과 주소는 반드시 입력해야 합니다.", true);
      return;
    }
    if (!body.reserve && !confirm(
        body.type === "recall"
          ? "CJ대한통운에 회수(반품)를 접수합니다 — 기사가 방문합니다.\n계속할까요?"
          : "CJ대한통운에 출고를 접수하고 송장번호를 받습니다.\n계속할까요?")) return;
    btn.disabled = true;
    try {
      const r = await api("/api/waybills/manual", { method: "POST", body });
      toast(r.message || "등록했습니다.");
      closeModal();
      renderShippingView($("#main") || document.querySelector("main"));
    } catch (e) { toast(e.message, true); }
    finally { btn.disabled = false; }
  });
}

function renderShippingView(main) {
  // 배송 전용 사용자(shipping.view)도 들어올 수 있어야 한다
  if (!canSeeMenu("shipping")) {
    main.innerHTML = `<h1 class="page-title">배송 / 송장</h1><div class="card placeholder"><p>배송 메뉴 접근 권한이 없습니다.</p></div>`;
    return;
  }
  // ★8/31 재편(대표 "현황판처럼 메인으로") — 옛 탭 이름(북마크·대시보드 바로가기 'ready')은
  //   새 탭으로 별칭 매핑한다: 신규건+송장 목록 = [현황], 회수/반품 = [신규 접수].
  if (state.shipTab === "ready" || state.shipTab === "waybills") state.shipTab = "status";
  else if (state.shipTab === "recall") state.shipTab = "intake";
  if (!state.shipTab) state.shipTab = "status";
  main.innerHTML = `
    <h1 class="page-title">배송 / 송장</h1>
    <p class="page-desc"><b>송장 발급은 셋팅 탭에서</b> 합니다 —
      여기서는 오늘 나간 송장·배송 흐름을 한눈에 보고, 포장·출고 확인과 회수 접수를 합니다.</p>
    <div class="kpi-row" id="ship-board"></div>
    <div id="ship-board-detail"></div>
    <div class="tabs" style="align-items:center;">
      <button data-stab="status" class="${state.shipTab === "status" ? "active" : ""}">현황 (포장 · 출고 · 송장 조회)</button>
      <button data-stab="intake" class="${state.shipTab === "intake" ? "active" : ""}">신규 접수 (회수 · 반품)</button>
      <span style="flex:1"></span>
      ${hasPerm("orders.ship") || hasPerm("waybills.manage")
        ? `<button class="btn btn-sm btn-primary" id="wb-new"
             title="주문·A/S 없이 송장을 직접 만듭니다 — 견본 발송, 부품만 보내기, 반품 회수 등">🆕 신규 등록</button>`
        : ""}
    </div>
    <div id="stab-body"></div>`;
  const nb = $("#wb-new", main);
  if (nb) nb.addEventListener("click", () => openManualWaybill());
  $$("button[data-stab]", main).forEach((b) => b.addEventListener("click", () => {
    state.shipTab = b.dataset.stab;
    clearPollers();          // 탭 전환 시 이전 폴러 정리 (누적 방지)
    ++state.renderSeq;
    renderShippingView(main);
  }));
  loadShipBoard();
  renderShipBoardDetail();          // 카드를 눌러 열어 둔 근거 내역은 화면 갱신에도 유지
  const body = $("#stab-body");
  if (state.shipTab === "status") {
    body.innerHTML = `<div id="ship-ready-sec"></div><div id="ship-wb-sec" style="margin-top:18px;"></div>`;
    renderShipReady($("#ship-ready-sec"));
    renderWaybillList($("#ship-wb-sec"));
    // 폴러는 화면당 1회만 등록한다. renderShipReady 안에서 등록하면
    // 폴러가 다시 renderShipReady를 부르며 인터벌이 기하급수로 늘어난다.
    addPoller(() => {
      if (!isEditingInput() && state.view === "shipping" && state.shipTab === "status") {
        renderShipReady($("#ship-ready-sec"));
        loadShipBoard();
      }
    }, 15000);
  } else {
    renderRecallList(body);
  }
}

/* 현황판 카드 — 오늘 발행/출고·배송중·회수·발급 걸림·몰 미전송(2026-08-31 대표).
   숫자는 서버가 센다(/api/waybills/board — 보관 포함 등 잣대 통일). */
async function loadShipBoard() {
  const host = $("#ship-board");
  if (!host) return;
  let b = null, pushCnt = null;
  try { b = await api("/api/waybills/board"); } catch (_e) {}
  try { pushCnt = (await api("/api/orders/invoice-push/pending")).length; } catch (_e) {}
  if (!$("#ship-board")) return;
  if (!b) { host.innerHTML = ""; return; }
  // pending 이 10분 넘게 남아 있으면 CJ 응답 유실 — 빨갛게 알린다
  let pendWarn = false;
  if (b.pending && b.pendingOldest) {
    const ageMin = (Date.now() - new Date(b.pendingOldest).getTime()) / 60000;
    pendWarn = ageMin > 10;
  }
  // ★카드는 전부 누를 수 있다(2026-08-31 대표) — 누르면 그 숫자의 근거 내역이 바로 밑에
  //   열린다(같은 조건을 서버가 그대로 되돌려 준다). 다시 누르면 닫힌다.
  const on = (k) => state.shipBoardDetail === k
    ? " outline:2px solid var(--accent, #2563eb);" : "";
  const card = (k, label, value, sub, extraStyle) => `
    <div class="kpi" data-bd="${k}" title="누르면 이 숫자의 근거 내역이 아래에 열립니다"
         style="cursor:pointer;${extraStyle || ""}${on(k)}">
      <div class="kpi-label">${label}</div>
      <div class="kpi-value">${value}<span class="muted" style="font-size:13px;">건</span></div>
      ${sub || ""}</div>`;
  host.innerHTML =
    card("issuedToday", "오늘 발행 송장", b.issuedToday,
         b.testToday ? `<div class="muted" style="font-size:12px;">테스트 ${b.testToday}건 별도</div>` : "")
    + card("shippedToday", "오늘 출고 확인", b.shippedToday,
           `<div class="muted" style="font-size:12px;">보관 처리분 포함</div>`)
    + card("inTransit", "배송중", b.inTransit,
           `<div class="muted" style="font-size:12px;">발행됐고 아직 배송완료 아님</div>`)
    + card("recallActive", "회수 진행", b.recallActive,
           `<div class="muted" style="font-size:12px;">접수~수거 중</div>`,
           "border-left-color:var(--violet, #7c3aed);")
    + `${b.pending ? `<div class="kpi" style="border-left-color:${pendWarn ? "var(--danger)" : "var(--border)"};">
      <div class="kpi-label">발급 진행 중</div>
      <div class="kpi-value" ${pendWarn ? 'style="color:var(--danger);"' : ""}>${b.pending}건</div>
      ${pendWarn ? `<div class="muted" style="font-size:12px; color:var(--danger);">10분 넘게 걸려 있음 — CJ 응답 확인 필요</div>` : ""}</div>` : ""}
    ${pushCnt ? `<div class="kpi" style="border-left-color:var(--blue);"><div class="kpi-label">몰에 송장 미전송</div>
      <div class="kpi-value">${pushCnt}건</div>
      <div class="muted" style="font-size:12px;">[현황] 탭 아래 배너에서 보내기</div></div>` : ""}`;
  $$("[data-bd]", host).forEach((el) => el.addEventListener("click", () => {
    state.shipBoardDetail = state.shipBoardDetail === el.dataset.bd ? "" : el.dataset.bd;
    loadShipBoard();
    renderShipBoardDetail();
  }));
}

/* 카드 클릭 근거 내역(2026-08-31 대표) — 카드를 만든 조건 그대로 서버(?detail=)가 준다 */
const BD_TITLES = {
  issuedToday: "오늘 발행 송장", shippedToday: "오늘 출고 확인",
  inTransit: "배송중", recallActive: "회수 진행",
};

async function renderShipBoardDetail() {
  const host = $("#ship-board-detail");
  if (!host) return;
  const key = state.shipBoardDetail;
  if (!key) { host.innerHTML = ""; return; }
  host.innerHTML = `<div class="card"><p class="muted">불러오는 중…</p></div>`;
  let d;
  try { d = await api("/api/waybills/board?detail=" + encodeURIComponent(key)); }
  catch (err) { host.innerHTML = `<div class="card"><p class="muted">${escapeHtml(err.message)}</p></div>`; return; }
  if (!$("#ship-board-detail") || state.shipBoardDetail !== key) return;
  const stLabel = { issued: "발행", test: "테스트", canceled: "취소",
                    delivered: "배송완료", pending: "발급 중" };
  const head = `<div class="inline-row">
      <h3 style="margin:0; flex:1;">${BD_TITLES[key] || key} <span class="muted" style="font-size:13px; font-weight:400;">${d.rows.length}건</span></h3>
      <button class="btn btn-sm" id="bd-close">닫기</button></div>`;
  if (key === "shippedToday") {
    host.innerHTML = `<div class="card">${head}
      <div class="table-wrap"><table>
        <thead><tr><th>주문번호</th><th>채널</th><th>수취인</th><th>상품</th><th>출고 확인</th><th></th></tr></thead>
        <tbody>${d.rows.map((r) => `<tr>
          <td><b>${escapeHtml(r.orderNo || "#" + r.orderId)}</b></td>
          <td>${escapeHtml(r.channel)}</td>
          <td>${escapeHtml(r.recipient)}</td>
          <td style="max-width:260px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;" title="${escapeHtml(r.productName)}">${escapeHtml(r.productName)}</td>
          <td>${escapeHtml(r.by)}<span class="muted" style="font-size:12px;"> · ${escapeHtml((r.at || "").replace("T", " ").slice(5, 16))}</span></td>
          <td>${r.archived ? '<span class="chip chip-slate">보관됨</span>' : ""}</td>
        </tr>`).join("") || `<tr><td colspan="6" class="muted">해당 건이 없습니다.</td></tr>`}</tbody>
      </table></div></div>`;
  } else {
    host.innerHTML = `<div class="card">${head}
      <div class="table-wrap"><table>
        <thead><tr><th>송장 ID</th><th>송장번호</th><th>수취인</th><th>상품명(라벨)</th><th>상태</th><th>배송 단계</th><th>발행</th></tr></thead>
        <tbody>${d.rows.map((r) => `<tr>
          <td>${escapeHtml(r.wid)}</td>
          <td><button class="link-btn" data-wbpop="${escapeHtml(r.wid)}" style="font-weight:700;"
                title="누르면 실시간 추적·메모 팝업이 열립니다">${escapeHtml(r.invoiceNo || "ℹ 정보/메모")}</button></td>
          <td>${escapeHtml(r.recipient)}</td>
          <td style="max-width:240px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;" title="${escapeHtml(r.items)}">${escapeHtml(r.items)}</td>
          <td>${escapeHtml(stLabel[r.status] || r.status)}</td>
          <td>${escapeHtml(r.stage || "-")}${key === "recallActive" && r.scheduledDate ? `<div class="muted" style="font-size:12px;">수거 희망 ${escapeHtml(r.scheduledDate)}</div>` : ""}</td>
          <td class="muted" style="font-size:12px;">${escapeHtml((r.createdAt || "").replace("T", " ").slice(5, 16))}</td>
        </tr>`).join("") || `<tr><td colspan="7" class="muted">해당 건이 없습니다.</td></tr>`}</tbody>
      </table></div></div>`;
    $$("button[data-wbpop]", host).forEach((btn) => btn.addEventListener("click", () => {
      openWaybillPopup(btn.dataset.wbpop);
    }));
  }
  const close = $("#bd-close");
  if (close) close.addEventListener("click", () => {
    state.shipBoardDetail = "";
    renderShipBoardDetail();
    loadShipBoard();
  });
}

/* ---------------- 회수 / 반품 ---------------- */

async function renderRecallList(body) {
  const seq = state.renderSeq;
  const canShip = hasPerm("orders.ship");
  body.innerHTML = `<p class="muted">불러오는 중…</p>`;
  let rows, orders;
  try {
    [rows, orders] = await Promise.all([
      api("/api/waybills?type=recall"),
      // ★출고된 주문은 대개 '보관' 처리되므로 진행 중만 보면 선택칸이 비어 버린다.
      //   반품은 이미 나간 주문에 거는 것이라 전체에서 찾아야 한다.
      api("/api/orders?view=all&limit=1000").then((r) => r.orders || []),
    ]);
    if (seq !== state.renderSeq) return;
  } catch (err) {
    if (seq === state.renderSeq) body.innerHTML = `<p class="muted">${escapeHtml(err.message)}</p>`;
    return;
  }
  const recalls = rows.filter((w) => w.type === "recall");
  // 이미 회수를 걸어 둔 주문은 목록에서 뺀다(중복 예약은 서버도 막지만 고를 이유가 없다)
  const recalled = new Set(recalls.filter((w) => !["canceled"].includes(w.status)).map((w) => w.orderId));
  const shipped = orders.filter((o) => o.shippingDone && !o.cancelledAt && !recalled.has(o.id));
  body.innerHTML = `
    <div class="card">
      <h3>회수 예약</h3>
      <p class="muted">반품·교환으로 물건을 돌려받을 때 예약합니다.
      회수 송장번호는 기사가 집화할 때 CJ가 부여하므로 예약 직후에는 비어 있는 것이 정상입니다.</p>
      ${canShip ? `<div class="inline-row">
        <select id="rc-order" style="min-width:280px;">
          <option value="">- 출고 완료 주문 선택 -</option>
          ${shipped.map((o) => `<option value="${o.id}">${escapeHtml(`${o.orderNumber || "#" + o.id} ${o.recipient} · ${o.productName}`)}</option>`).join("")}
        </select>
        <input type="date" id="rc-date" title="수거 희망일">
        <input type="text" id="rc-reason" placeholder="회수 사유(예: 단순 변심 반품)" style="flex:1; min-width:160px;">
        <button class="btn btn-sm btn-primary" id="rc-add" ${shipped.length ? "" : "disabled"}>회수 예약</button>
      </div>` : ""}
      <div class="table-wrap"><table>
        <thead><tr><th>회수 ID</th><th>고객</th><th>내용</th><th>송장번호</th><th>상태</th><th>수거 희망일</th><th>예약</th><th></th></tr></thead>
        <tbody>${recalls.map((w) => `
          <tr>
            <td>${escapeHtml(w.wid)}</td>
            <td>${escapeHtml(w.recipient)}<div class="muted" style="font-size:12px;">${escapeHtml(w.phone)}</div></td>
            <td style="max-width:280px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${escapeHtml(w.items)}">${escapeHtml(w.items)}</td>
            <td>${w.invoiceNo ? escapeHtml(w.invoiceNo) : '<span class="muted">집화 시 부여</span>'}</td>
            <td>${w.status === "delivered" ? '<span class="chip chip-green">회수 완료</span>'
                 : w.status === "canceled" ? '<span class="chip chip-red">취소</span>'
                 : w.status === "test" ? '<span class="chip chip-slate">테스트</span>'
                 : '<span class="chip chip-blue">회수 중</span>'}</td>
            <td>${w.scheduledDate
              ? escapeHtml(w.scheduledDate)
              : '<span class="muted">미지정</span>'}</td>
            <td class="muted" style="font-size:12px;">${escapeHtml(w.createdBy)}<br>${escapeHtml((w.createdAt || "").slice(5, 16).replace("T", " "))}</td>
            <td>${canShip && !["delivered", "canceled"].includes(w.status)
              ? `<button class="btn btn-sm btn-primary" data-received="${w.wid}">입고 처리</button>
                 <button class="btn btn-ghost btn-sm" data-rcancel="${w.wid}">취소</button>` : ""}</td>
          </tr>
          ${canShip && !["delivered", "canceled"].includes(w.status) ? `
          <tr class="rc-recv" data-recvrow="${w.wid}" style="display:none;">
            <td colspan="8" style="background:var(--bg);">
              <div class="inline-row" style="margin:0;">
                ${(w.items || "").includes("A/S회수")
                  ? `<b>A/S 회수품입니다</b>
                     <span class="muted">— 고객 물건이라 판매 재고로 잡지 않고 'A/S' 상태로 입고됩니다.</span>`
                  : `<b>회수한 물건을 어떤 상태로 둘까요?</b>
                     <select data-recvsel="${w.wid}">
                       ${RECEIVE_STATUSES.map(([v, l], i) =>
                         `<option value="${v}" ${i === 1 ? "selected" : ""}>${l}</option>`).join("")}
                     </select>`}
                <button class="btn btn-sm btn-primary" data-recvok="${w.wid}">입고 확정</button>
                <button class="btn btn-ghost btn-sm" data-recvno="${w.wid}">닫기</button>
              </div>
            </td>
          </tr>` : ""}`).join("") || `<tr><td colspan="8" class="muted">회수 건이 없습니다.</td></tr>`}
        </tbody></table></div>
    </div>`;
  if (!canShip) return;
  const addBtn = $("#rc-add");
  if (addBtn) addBtn.addEventListener("click", async () => {
    const oid = $("#rc-order").value;
    if (!oid) { toast("주문을 선택하세요.", true); return; }
    addBtn.disabled = true;
    try {
      const res = await api(`/api/orders/${oid}/recall`, { method: "POST", body: {
        reason: $("#rc-reason").value.trim(), pickupDate: $("#rc-date").value } });
      toast(res.simulated ? `테스트 회수 예약: ${res.wid}` : `회수 예약 완료: ${res.wid}`);
      renderRecallList(body);
    } catch (err) { toast(err.message, true); addBtn.disabled = false; }
  });
  // 입고 처리 — 영문 코드를 외워 타이핑하지 않도록 그 자리에서 골라 확정한다
  $$("button[data-received]", body).forEach((b) => b.addEventListener("click", () => {
    const row = $(`tr[data-recvrow="${b.dataset.received}"]`, body);
    if (row) row.style.display = row.style.display === "none" ? "table-row" : "none";
  }));
  $$("button[data-recvno]", body).forEach((b) => b.addEventListener("click", () => {
    const row = $(`tr[data-recvrow="${b.dataset.recvno}"]`, body);
    if (row) row.style.display = "none";
  }));
  $$("button[data-recvok]", body).forEach((b) => b.addEventListener("click", async () => {
    const wid = b.dataset.recvok;
    const back = $(`select[data-recvsel="${wid}"]`, body)?.value || "refurbishing";
    b.disabled = true;
    try {
      const r = await api(`/api/waybills/${wid}/received`, { method: "POST", body: { status: back } });
      toast(`입고 처리 완료 — 자산 ${r.assets.length}대를 '${statusLabel(r.status || back)}' 상태로 넣었습니다.`);
      renderRecallList(body);
    } catch (err) { toast(err.message, true); b.disabled = false; }
  }));
  $$("button[data-rcancel]", body).forEach((b) => b.addEventListener("click", async () => {
    if (!confirm("이 회수 예약을 취소할까요?")) return;
    try { await api(`/api/waybills/${b.dataset.rcancel}/cancel`, { method: "POST" }); renderRecallList(body); }
    catch (err) { toast(err.message, true); }
  }));
}

/* 쇼핑몰에 아직 안 올라간 송장 — 여기서 다시 보낸다(전에는 대표가 몰 관리자에서 손으로 입력) */
async function renderInvoicePush(body) {
  const host = $("#wf-push", body);
  if (!host) return;
  let rows;
  try {
    rows = await api("/api/orders/invoice-push/pending");
  } catch (_e) { return; }
  if (!rows.length) { host.innerHTML = ""; return; }
  host.innerHTML = `
    <div style="border:1px solid var(--border); border-left:4px solid var(--blue);
                border-radius:8px; padding:10px 12px; margin:8px 0;">
      <div class="inline-row" style="margin:0;">
        <b style="flex:1;">쇼핑몰에 송장번호가 아직 안 올라간 주문 ${rows.length}건</b>
        <button class="btn btn-sm btn-primary" id="wf-push-all">모두 보내기</button>
      </div>
      <div class="table-wrap" style="margin-top:6px;"><table>
        <thead><tr><th>쇼핑몰</th><th>주문번호</th><th>수취인</th><th>송장번호</th><th>사유</th><th></th></tr></thead>
        <tbody>${rows.map((r) => `<tr>
          <td>${chBadge(r.channel)}</td>
          <td>${escapeHtml(r.orderNumber || "-")}</td>
          <td>${escapeHtml(r.recipient)}</td>
          <td>${escapeHtml(r.invoiceNo)}</td>
          <td class="muted" style="max-width:280px;">${escapeHtml(r.error || "아직 보내지 않음")}</td>
          <td><button class="btn btn-sm" data-push="${r.id}">보내기</button></td>
        </tr>`).join("")}</tbody>
      </table></div>
    </div>`;
  const send = async (ids) => {
    try {
      const r = await api("/api/orders/invoice-push", { method: "POST", body: { ids } });
      toast(r.failed.length ? `${r.ok}건 전송 · ${r.failed.length}건 실패` : `${r.ok}건 전송 완료`);
      if (r.failed.length) toast(r.failed[0].message, true);
      renderInvoicePush(body);
    } catch (err) { toast(err.message, true); }
  };
  $("#wf-push-all", host).addEventListener("click", () => {
    if (confirm(`${rows.length}건의 송장번호를 쇼핑몰에 보냅니다.\n고객에게 배송 안내가 나갈 수 있습니다. 계속할까요?`)) {
      send(rows.map((r) => r.id));
    }
  });
  $$("button[data-push]", host).forEach((b) => b.addEventListener("click", () =>
    send([Number(b.dataset.push)])));
}

/* ---------------- 발급 대기 / 출고 ---------------- */

async function renderShipReady(body) {
  const seq = state.renderSeq;
  const canShip = hasPerm("orders.ship");
  // 출고 확인은 배송 담당의 일 — 셋팅 작업 권한이 없어도 체크할 수 있어야 한다
  const canWork = hasPerm("orders.work") || hasPerm("orders.ship");
  body.innerHTML = `<p class="muted">불러오는 중…</p>`;
  let orders;
  try {
    orders = (await fetchOrders({ view: "active" }))
      .filter((o) => o.productionDone && o.softwareInspectionDone);
    if (seq !== state.renderSeq) return;
  } catch (err) {
    if (seq === state.renderSeq) body.innerHTML = `<p class="muted">${escapeHtml(err.message)}</p>`;
    return;
  }
  // ★방문수령·퀵은 택배가 아니라 송장이 필요 없다(대표 2026-08-05).
  //   '송장 발급 대기'에 섞어 두면 매번 왜 송장이 없냐고 확인하게 된다 — 따로 뺀다.
  const notShipped = orders.filter((o) => !o.shippingDone);
  const waiting = notShipped.filter(needsWaybill);
  const pickup = notShipped.filter((o) => !needsWaybill(o));
  const shipped = orders.filter((o) => o.shippingDone);
  // ★출고완료는 예전에 앞 50건만 잘라 보여 나머지는 볼 방법이 없었다 — 이제 넘길 수 있다
  const pgW = paged(waiting, "shipWait");
  const pgK = paged(pickup, "shipPickup");
  const pgD = paged(shipped, "shipDone");
  const rowHtml = (o) => {
    // 회수 송장은 배송/송장 > 회수·반품 탭에서 따로 본다 — 출고 송장만 여기서 다룬다
    const fwd = (o.waybills || []).filter((w) => w.type !== "recall" && w.status !== "canceled");
    const hasWb = fwd.length > 0;
    const activeWb = hasWb ? fwd[0] : null;
    return `<tr>
      <td>${chBadge(o.channel)}${receiveChip(o)}<div class="muted" style="font-size:12px;">${escapeHtml(o.orderNumber || `#${o.id}`)}</div></td>
      <td style="max-width:240px;"><b>${escapeHtml(o.productName)}</b>
        ${o.optionName ? `<div class="muted" style="font-size:12px;">${escapeHtml(o.optionName)}</div>` : ""}
        ${o.memo ? `<div class="chip chip-red" style="margin-top:4px; white-space:normal;">📌 ${escapeHtml(o.memo)}</div>` : ""}</td>
      <td>${escapeHtml(o.recipient)}<div class="muted" style="font-size:12px;">${escapeHtml(o.phone)}</div></td>
      <td>${o.assets.map((a) => `<span class="chip chip-green">${escapeHtml(a.assetNo)}</span>`).join(" ") || '<span class="chip chip-red">자산 미매칭</span>'}</td>
      <td>${waybillCellHtml(o, activeWb, canShip)}</td>
      <td class="stage-cell">
        <label class="check-line" style="justify-content:center;">
          <input type="checkbox" data-ship="${o.id}" ${o.shippingDone ? "checked" : ""} ${canWork ? "" : "disabled"}>
        </label>
        ${o.shippingBy ? `<div class="stage-by">${escapeHtml(o.shippingBy)}</div>` : ""}
      </td>
    </tr>`;
  };
  body.innerHTML = `
    <div class="kpi-row">
      <div class="kpi"><div class="kpi-label">송장 대기 (셋팅에서 발급)</div><div class="kpi-value">${waiting.filter((o) => !o.waybills.some((w) => w.type !== "recall" && w.status !== "canceled")).length}</div></div>
      <div class="kpi"><div class="kpi-label">포장 대기 (송장 출력됨)</div><div class="kpi-value">${waiting.filter((o) => o.waybills.some((w) => w.type !== "recall" && w.status !== "canceled")).length}</div></div>
    </div>
    <div class="card">
      <h3>신규건 <span class="muted" style="font-weight:400; font-size:13px;">— 셋팅에서 송장이 나온 건을 포장하고 출고 확인합니다</span></h3>
      <div class="table-wrap"><table>
        <thead><tr><th>채널 / 주문</th><th>상품</th><th>수취인</th><th>자산번호</th><th>송장</th><th class="stage-th">출고 확인</th></tr></thead>
        <tbody>${pgW.rows.map(rowHtml).join("") || `<tr><td colspan="6" class="muted">QC 완료된 대기 주문이 없습니다. (셋팅 탭에서 제작·검수를 완료하면 여기에 나타납니다)</td></tr>`}</tbody>
      </table></div>
      ${pgW.bar}
    </div>
    ${pickup.length ? `<div class="card" style="border-left:4px solid var(--violet, #7c3aed);">
      <h3>🚶 택배 아님 — 방문수령 · 퀵 <span class="muted" style="font-size:13px;">${pickup.length}건</span></h3>
      <p class="muted" style="margin:0 0 8px;">송장을 뽑지 않습니다. 물건을 건네고 [출고 확인]만 눌러 주세요.</p>
      <div class="table-wrap"><table>
        <thead><tr><th>채널 / 주문</th><th>상품</th><th>수취인</th><th>자산번호</th><th>송장</th><th class="stage-th">출고 확인</th></tr></thead>
        <tbody>${pgK.rows.map(rowHtml).join("")}</tbody>
      </table></div>
      ${pgK.bar}
    </div>` : ""}
    ${shipped.length ? `<div class="card"><h3>출고 확인됨</h3>
      <div class="table-wrap"><table>
        <thead><tr><th>채널 / 주문</th><th>상품</th><th>수취인</th><th>자산번호</th><th>송장</th><th class="stage-th">출고 확인</th></tr></thead>
        <tbody>${pgD.rows.map(rowHtml).join("")}</tbody>
      </table></div>
      ${pgD.bar}</div>` : ""}`;
  wirePager(body, "shipWait", () => renderShipReady(body));
  wirePager(body, "shipPickup", () => renderShipReady(body));
  wirePager(body, "shipDone", () => renderShipReady(body));

  // 발급은 셋팅 한 곳에서만 — 여기서는 그 화면으로 보내 준다
  $$("button[data-gosetup]", body).forEach((b) => b.addEventListener("click", () => {
    go("setup");
  }));
  // 재발급 = [기존 송장 취소] + [새로 발급]을 한 번에. 예전엔 다른 탭에 가서
  // 손으로 취소한 뒤 돌아와야 했고, 그 경로 안내조차 없었다(2026-07-29 전수조사).
  $$("button[data-reissue]", body).forEach((b) => b.addEventListener("click", async () => {
    if (!confirm("기존 송장을 취소하고 새 송장을 발급합니다.\n"
                 + "이미 붙여서 내보낸 송장이면 새 번호로 다시 붙여야 합니다.\n\n계속할까요?")) return;
    b.disabled = true;
    try {
      // ★취소가 실패해도 재발급 길을 막지 않는다 — CJ 쪽에서 이미 취소·집화된 건은
      //   우리 취소 호출이 거절되는데, 그때 새 발급까지 못 하면 그 주문은 영영 갇힌다.
      //   (RMS의 skip_cancel 탈출구와 같은 취지. forceLocal=우리 기록만 취소)
      try {
        await api(`/api/waybills/${b.dataset.wid}/cancel`, { method: "POST", body: {} });
      } catch (cerr) {
        if (!confirm(`기존 송장 취소가 실패했습니다:\n${cerr.message}\n\n`
            + "CJ에서 이미 취소·집화됐다면 그대로 새 송장을 발급해도 됩니다.\n"
            + "우리 기록만 취소하고 새로 발급할까요?\n"
            + "(기존 예약이 CJ에 살아 있으면 이중 집화가 될 수 있습니다)")) {
          throw cerr;
        }
        await api(`/api/waybills/${b.dataset.wid}/cancel`,
                  { method: "POST", body: { forceLocal: true } });
      }
      const res = await api(`/api/orders/${b.dataset.reissue}/waybill`,
                            // 상자 수는 서버가 지난 송장에서 물려받는다(같은 짐을 다시 부친다)
                            { method: "POST", body: {} });
      toast(`재발급 완료: ${res.invoiceNo || res.wid}`);
      window.open(`/api/waybills/${res.wid}/pdf`, "_blank");
      renderShipReady(body);
    } catch (err) { toast(err.message, true); b.disabled = false; }
  }));
  $$("button[data-pdf]", body).forEach((b) => b.addEventListener("click", () =>
    window.open(`/api/waybills/${b.dataset.pdf}/pdf`, "_blank")));
  $$("input[data-ship]", body).forEach((cb) => cb.addEventListener("change", async () => {
    cb.disabled = true;
    try {
      await api(`/api/orders/${cb.dataset.ship}`, {
        method: "PATCH", body: { action: "shipping", value: cb.checked },
      });
    } catch (err) {
      toast(err.message, true);
    }
    renderShipReady(body); // 성공/실패 모두 서버 상태로 다시 그린다
  }));
}

/* ---------------- 송장 목록 ---------------- */

/* 송장번호 클릭 팝업 — 실시간 추적 타임라인 + 운영 메모 (RMS 이식, 대표 2026-08-10).
   고객이 송장번호만 들고 전화했을 때 이 팝업 하나로 답한다. */
function copyText(text) {
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(text).then(() => toast("복사했습니다."));
    return;
  }
  // http(비보안) 환경 폴백 — 사내망 OWS는 http라 이 경로를 탄다
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.style.position = "fixed";
  ta.style.opacity = "0";
  document.body.appendChild(ta);
  ta.select();
  try { document.execCommand("copy"); toast("복사했습니다."); }
  catch { toast("복사에 실패했습니다 — 길게 눌러 직접 복사하세요.", true); }
  ta.remove();
}

async function openWaybillPopup(wid) {
  let host = document.getElementById("wb-popup-host");
  if (!host) {
    host = document.createElement("div");
    host.id = "wb-popup-host";
    document.body.appendChild(host);
  }
  host.innerHTML = `<div class="card" style="min-width:340px;"><p class="muted">송장 정보를 여는 중…</p></div>`;
  openModalWith(host);
  let d;
  try { d = await api(`/api/waybills/${encodeURIComponent(wid)}/trace`); }
  catch (err) {
    host.innerHTML = `<div class="card"><p class="muted">${escapeHtml(err.message)}</p>
      <div class="editor-actions"><button class="btn" id="wbp-close">닫기</button></div></div>`;
    $("#wbp-close").addEventListener("click", () => { host.innerHTML = ""; });
    return;
  }
  const canShip = hasPerm("orders.ship");
  const inv = d.invoiceNo || "";
  const digits = inv.replace(/[^0-9]/g, "");
  const fmtD = (s) => (s && s.length === 8 ? `${s.slice(0, 4)}-${s.slice(4, 6)}-${s.slice(6, 8)}` : s || "");
  const fmtT = (s) => (s && s.length >= 4 ? `${s.slice(0, 2)}:${s.slice(2, 4)}` : s || "");
  const tl = (d.timeline || []).slice().reverse();       // 최신이 위로
  host.innerHTML = `
    <div class="card" style="min-width:360px; max-width:600px;">
      <div class="inline-row">
        <h3 style="margin:0; flex:1;">🚚 ${inv ? escapeHtml(inv) : escapeHtml(d.wid)}
          <span class="muted" style="font-size:12px; font-weight:400;">${escapeHtml(d.wid)}</span></h3>
        <button class="btn btn-ghost btn-sm" id="wbp-close">✕</button>
      </div>
      <div class="inline-row" style="gap:6px; flex-wrap:wrap; margin-top:4px;">
        ${inv ? `<button class="btn btn-sm" id="wbp-copy">📋 번호 복사</button>
          <a class="btn btn-sm" target="_blank"
             href="https://trace.cjlogistics.com/next/tracking.html?wblNo=${encodeURIComponent(digits)}">🌐 CJ 웹조회</a>` : ""}
        ${d.stageName ? `<span class="chip ${d.stageCode === "91" ? "chip-green" : "chip-blue"}">🚚 ${escapeHtml(d.stageName)}</span>` : ""}
      </div>
      <p class="muted" style="margin:8px 0 0; font-size:13px;">
        ${escapeHtml(d.recipient)} · ${escapeHtml(d.phone || "-")}<br>
        ${escapeHtml(d.address || "-")}<br>
        ${escapeHtml(d.items || "")}${d.boxQty > 1 ? ` · 박스 ${d.boxQty}개` : ""}
        ${d.orderId ? ` · 주문 #${d.orderId}` : ""}</p>
      <h3 style="margin-top:12px; font-size:14px;">배송 이력</h3>
      ${tl.length ? `<div class="timeline" style="max-height:220px; overflow:auto;">${tl.map((p) => `
        <div class="tl-item">
          <div class="tl-time">${escapeHtml(fmtD(p.date))} ${escapeHtml(fmtT(p.time))}</div>
          <div class="tl-action"><span class="chip ${p.code === "91" ? "chip-green" : "chip-slate"}">${escapeHtml(p.name || p.code)}</span>
            <span class="muted" style="font-size:12px;">${escapeHtml(p.branch || "")}</span></div>
        </div>`).join("")}</div>`
        : `<p class="muted" style="font-size:13px;">${escapeHtml(d.trackError || "추적 이력이 아직 없습니다.")}</p>`}
      <h3 style="margin-top:12px; font-size:14px;">운영 메모 <span class="muted" style="font-weight:400; font-size:12px;">— 통화 내용·재배송 약속 등</span></h3>
      <textarea id="wbp-note" style="width:100%; min-height:64px; padding:8px 10px; border:1px solid var(--border);
        border-radius:8px; background:var(--bg);" ${canShip ? "" : "disabled"}>${escapeHtml(d.note || "")}</textarea>
      <div class="editor-actions">
        ${canShip ? `<button class="btn btn-primary btn-sm" id="wbp-notesave">메모 저장</button>` : ""}
        <button class="btn btn-sm" id="wbp-close2">닫기</button>
      </div>
    </div>`;
  const close = () => { host.innerHTML = ""; };
  $("#wbp-close").addEventListener("click", close);
  $("#wbp-close2").addEventListener("click", close);
  const copyBtn = $("#wbp-copy");
  if (copyBtn) copyBtn.addEventListener("click", () => copyText(inv));
  const saveBtn = $("#wbp-notesave");
  if (saveBtn) saveBtn.addEventListener("click", async () => {
    saveBtn.disabled = true;
    try {
      await api(`/api/waybills/${encodeURIComponent(wid)}/note`,
                { method: "POST", body: { note: $("#wbp-note").value } });
      toast("메모를 저장했습니다.");
    } catch (err) { toast(err.message, true); }
    saveBtn.disabled = false;
  });
}

/* 배송단계 배지 — CJ 추적이 채운 중간 단계(집화완료·간선상차 …)를 보여 준다.
   ★추적 데몬이 3시간마다 DB에 적어 두는데 예전엔 화면이 그 값을 안 그렸다(2026-08-10).
     배송완료(91)만 상태로 보이고 중간 과정이 전부 안 보였던 이유다. */
function wbStageChip(w) {
  if (!w.stageName) return "";
  const code = w.stageCode || "";
  const cls = code === "91" ? "chip-green" : ["03", "82"].includes(code) ? "chip-red" : "chip-blue";
  const at = (w.stageAt || "").slice(5, 16).replace("T", " ");
  return `<span class="chip ${cls}" style="font-size:11px;" title="CJ 추적 ${escapeHtml(at)}">🚚 ${escapeHtml(w.stageName)}</span>`;
}

async function renderWaybillList(body) {
  const seq = state.renderSeq;
  const canShip = hasPerm("orders.ship");
  body.innerHTML = `<p class="muted">불러오는 중…</p>`;
  let rows;
  try {
    const f = state.wbFilter || (state.wbFilter = { q: "", status: "", type: "", from: "", to: "" });
    f.type = f.type || ""; f.from = f.from || ""; f.to = f.to || "";
    const p = new URLSearchParams();
    if (f.q) p.set("q", f.q);
    if (f.status) p.set("status", f.status);
    if (f.type) p.set("type", f.type);
    if (f.from) p.set("from", f.from);
    if (f.to) p.set("to", f.to);
    rows = await api("/api/waybills?" + p.toString());
    if (seq !== state.renderSeq) return;
  } catch (err) {
    if (seq === state.renderSeq) body.innerHTML = `<p class="muted">${escapeHtml(err.message)}</p>`;
    return;
  }
  const f = state.wbFilter;
  if (!state.wbPicked) state.wbPicked = new Set();
  state.wbPicked = new Set([...state.wbPicked].filter((id) => rows.some((w) => w.wid === id)));
  const stChip = { issued: "chip-green", test: "chip-slate", canceled: "chip-red",
                   delivered: "chip-blue", pending: "chip-violet" };
  const stLabel = { issued: "발행", test: "테스트", canceled: "취소",
                    delivered: "배송완료", pending: "발급 중" };
  // ★as_return 은 DB에 없는 유령 값이었다(A/S 반송은 forward+as_ticket_id) — 필터에서 제거(8/31)
  const tyLabel = { forward: "출고", recall: "회수" };
  body.innerHTML = `
    <div class="card">
      <div class="inline-row" style="flex-wrap:wrap;">
        <input type="text" id="wf-q" placeholder="송장번호/수취인/상품/송장ID" value="${escapeHtml(f.q)}" style="min-width:220px;">
        <select id="wf-status"><option value="">전체 상태</option>
          ${Object.entries(stLabel).map(([k, l]) => `<option value="${k}" ${f.status === k ? "selected" : ""}>${l}</option>`).join("")}</select>
        <select id="wf-type"><option value="">전체 종류</option>
          ${Object.entries(tyLabel).map(([k, l]) => `<option value="${k}" ${f.type === k ? "selected" : ""}>${l}</option>`).join("")}</select>
        <input type="date" id="wf-from" value="${escapeHtml(f.from)}" title="발행일 시작">
        <span class="muted">~</span>
        <input type="date" id="wf-to" value="${escapeHtml(f.to)}" title="발행일 끝">
        <button class="btn btn-sm btn-primary" id="wf-search">조회</button>
        <span class="muted">${rows.length}건${rows.length >= 300 ? " (최근 300 — 기간을 좁혀 주세요)" : ""}</span>
        <span style="flex:1"></span>
        ${canShip ? `<button class="btn btn-sm" id="wf-recallno" title="회수 송장번호는 기사가 집화할 때 CJ가 매깁니다.
CJ에 예약 기준으로 물어봐 비어 있던 회수 번호를 받아옵니다(최근 14일).">📥 회수 송장번호 받기</button>` : ""}
        ${canShip ? `<button class="btn btn-sm" id="wf-track" title="CJ에 지금 배송상태를 물어봅니다(3시간마다 자동 갱신)">📡 배송추적 새로고침</button>` : ""}
      </div>
      <div id="wf-bulkbar" style="display:none;"></div>
      <div id="wf-push"></div>
      <div class="table-wrap"><table>
        <thead><tr><th style="width:28px;"><input type="checkbox" id="wf-all" title="보이는 송장 전체 선택"></th>
          <th>송장 ID</th><th>종류</th><th>송장번호</th><th>수취인</th><th>상품명(라벨)</th><th>상태</th><th>발행</th><th></th></tr></thead>
        <tbody>${rows.map((w) => `
          <tr>
            <td><input type="checkbox" class="wf-pick" data-wid="${escapeHtml(w.wid)}" ${state.wbPicked.has(w.wid) ? "checked" : ""}></td>
            <td>${escapeHtml(w.wid)}</td>
            <td>${escapeHtml(tyLabel[w.type] || w.type)}</td>
            <td><button class="link-btn" data-wbpop="${escapeHtml(w.wid)}"
                  title="누르면 실시간 추적·메모 팝업이 열립니다"
                  style="font-weight:700;">${escapeHtml(w.invoiceNo || "ℹ 정보/메모")}</button></td>
            <td>${escapeHtml(w.recipient)}</td>
            <td style="max-width:300px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${escapeHtml(w.items)}">${escapeHtml(w.items)}</td>
            <td><span class="chip ${stChip[w.status] || "chip-slate"}">${stLabel[w.status] || escapeHtml(w.status)}</span> ${wbStageChip(w)}</td>
            <td class="muted">${escapeHtml(w.createdBy)}<br>${escapeHtml((w.createdAt || "").slice(5, 16).replace("T", " "))}</td>
            <td>
              ${w.type === "recall" && !w.invoiceNo
                ? '<span class="muted" title="회수는 기사가 집화할 때 CJ가 번호를 매깁니다 — [📥 회수 송장번호 받기]로 수집됩니다">집화 시 번호 부여</span>'
                : `<button class="btn btn-sm" data-pdf="${w.wid}">PDF</button>`}
              ${canShip && !["canceled", "delivered"].includes(w.status)
                ? `<button class="btn btn-ghost btn-sm" data-wbcancel="${w.wid}">취소</button>`
                : (w.status === "delivered"
                    ? '<span class="muted" style="font-size:12px;" title="이미 고객에게 도착했습니다. 취소해도 택배사에는 반영되지 않습니다">배송 완료</span>'
                    : "")}
            </td>
          </tr>`).join("") || `<tr><td colspan="9" class="muted">송장이 없습니다.</td></tr>`}
        </tbody></table></div>
    </div>`;
  const doSearch = () => {
    f.q = $("#wf-q").value.trim(); f.status = $("#wf-status").value;
    f.type = $("#wf-type").value; f.from = $("#wf-from").value; f.to = $("#wf-to").value;
    renderWaybillList(body);
  };
  $("#wf-search").addEventListener("click", doSearch);
  ["#wf-status", "#wf-type", "#wf-from", "#wf-to"].forEach((sel) => {
    const el = $(sel, body);
    if (el) el.addEventListener("change", doSearch);
  });
  autoSearch("#wf-q", doSearch);
  if (canShip) renderInvoicePush(body);

  // ── 선택 일괄바 — 여러 건 골라 한 번에 인쇄/취소 ──────────────────────
  const drawBulk = () => {
    const bar = $("#wf-bulkbar", body);
    if (!bar) return;
    const n = state.wbPicked.size;
    if (!n || !canShip) { bar.style.display = "none"; bar.innerHTML = ""; return; }
    bar.style.display = "block";
    bar.innerHTML = `
      <div class="inline-row" style="background:var(--primary-soft); border-radius:8px; padding:8px 12px; margin:8px 0;">
        <b>${n}건 선택됨</b>
        <button class="btn btn-sm" id="wb-bulk-print" title="선택한 송장을 한 PDF로 합쳐 출력합니다(라벨 있는 건만)">🖨 선택 인쇄</button>
        <button class="btn btn-sm" id="wb-bulk-cancel">⛔ 선택 취소</button>
        <span style="flex:1"></span>
        <button class="btn btn-ghost btn-sm" id="wb-bulk-clear">선택 해제</button>
      </div>`;
    $("#wb-bulk-clear", bar).addEventListener("click", () => {
      state.wbPicked.clear();
      $$(".wf-pick", body).forEach((cb) => { cb.checked = false; });
      drawBulk();
    });
    $("#wb-bulk-print", bar).addEventListener("click", async () => {
      // 라벨 없는 건(집화 전 회수 등)을 섞어 보내면 전체가 404로 거절된다 — 미리 거른다
      const all = [...state.wbPicked];
      const wids = all.filter((id) => (rows.find((x) => x.wid === id) || {}).hasLabel);
      const skipped = all.length - wids.length;
      if (!wids.length) { toast("인쇄할 라벨이 있는 송장이 없습니다.", true); return; }
      if (skipped) toast(`라벨 없는 ${skipped}건은 빼고 인쇄합니다.`);
      try {
        const res = await fetch("/api/waybills/print", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ wids }),
        });
        if (!res.ok) throw new Error((await res.json().catch(() => ({}))).error || "인쇄 실패");
        const blob = await res.blob();
        window.open(URL.createObjectURL(blob), "_blank");
      } catch (err) { toast(err.message, true); }
    });
    $("#wb-bulk-cancel", bar).addEventListener("click", async () => {
      const wids = [...state.wbPicked].filter((id) => {
        const w = rows.find((x) => x.wid === id);
        return w && !["canceled", "delivered"].includes(w.status);
      });
      if (!wids.length) { toast("취소할 수 있는 송장이 없습니다.", true); return; }
      if (!confirm(`선택한 ${wids.length}건을 취소합니다. 계속할까요?`)) return;
      let ok = 0, fail = 0;
      for (const id of wids) {
        try { await api(`/api/waybills/${id}/cancel`, { method: "POST" }); ok++; }
        catch { fail++; }
      }
      toast(`취소 ${ok}건${fail ? ` · 실패 ${fail}건` : ""}`, ok === 0);
      state.wbPicked.clear();
      renderWaybillList(body);
    });
  };
  $$(".wf-pick", body).forEach((cb) => cb.addEventListener("change", () => {
    if (cb.checked) state.wbPicked.add(cb.dataset.wid);
    else state.wbPicked.delete(cb.dataset.wid);
    drawBulk();
  }));
  const allCb = $("#wf-all", body);
  if (allCb) allCb.addEventListener("change", () => {
    $$(".wf-pick", body).forEach((cb) => {
      cb.checked = allCb.checked;
      if (allCb.checked) state.wbPicked.add(cb.dataset.wid);
      else state.wbPicked.delete(cb.dataset.wid);
    });
    drawBulk();
  });
  drawBulk();

  const trackBtn = $("#wf-track");
  if (trackBtn) trackBtn.addEventListener("click", async () => {
    trackBtn.disabled = true;
    trackBtn.textContent = "조회 중…";
    try {
      const r = await api("/api/waybills/track-sync", { method: "POST" });
      toast(r.changed ? `${r.checked}건 확인 · ${r.changed}건 상태가 바뀌었습니다.`
                      : `${r.checked}건 확인 · 바뀐 건이 없습니다.`);
      renderWaybillList(body);
    } catch (err) {
      toast(err.message, true);
      trackBtn.disabled = false;
      trackBtn.textContent = "📡 배송추적 새로고침";
    }
  });
  // 회수 송장번호 수집 — CJ가 집화 때 채번한 번호를 예약 기준으로 받아온다
  const recallBtn = $("#wf-recallno");
  if (recallBtn) recallBtn.addEventListener("click", async () => {
    recallBtn.disabled = true;
    recallBtn.textContent = "수집 중…";
    try {
      const r = await api("/api/waybills/recall-invoice-sync", { method: "POST", body: { days: 14 } });
      toast(r.filled || r.staged
        ? `송장번호 ${r.filled}건 수집 · 배송단계 ${r.staged}건 갱신`
        : "새로 받아올 번호가 없습니다.");
      renderWaybillList(body);
    } catch (err) {
      toast(err.message, true);
      recallBtn.disabled = false;
      recallBtn.textContent = "📥 회수 송장번호 받기";
    }
  });
  $$("button[data-pdf]", body).forEach((b) => b.addEventListener("click", () =>
    window.open(`/api/waybills/${b.dataset.pdf}/pdf`, "_blank")));
  $$("button[data-wbpop]", body).forEach((b) => b.addEventListener("click", () =>
    openWaybillPopup(b.dataset.wbpop)));
  $$("button[data-wbcancel]", body).forEach((b) => b.addEventListener("click", async () => {
    if (!confirm(`송장 ${b.dataset.wbcancel}을(를) 취소할까요?`)) return;
    try {
      await api(`/api/waybills/${b.dataset.wbcancel}/cancel`, { method: "POST" });
      toast("송장을 취소했습니다.");
      renderWaybillList(body);
    } catch (err) { toast(err.message, true); }
  }));
}
