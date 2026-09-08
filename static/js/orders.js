/* OWS 주문관리 — 주문 데이터 관리(엑셀 가져오기/수기 등록/수정/취소). QC 작업은 '셋팅' 탭. */
"use strict";

const CHANNEL_LIST = ["고도몰", "쿠팡", "카카오", "토스", "11번가", "롯데온", "지마켓", "옥션", "테무", "수기", "전화", "방문"];

/* 내부 작업 단계(셋팅 탭에서 씀) */
function orderStatusInfo(o) {
  if (o.cancelledAt) return { key: "cancelled", label: "취소", chip: "chip-red" };
  if (o.archivedAt) return { key: "archived", label: "보관", chip: "chip-slate" };
  if (o.shippingDone) return { key: "shipped", label: "출고 확인", chip: "chip-green" };
  // ★2026-08-18 대표 승인: 이름을 셋팅 보드에 맞춘다. 2026-08-05엔 3·4단계를 둘 다
  //   '출고 확인'으로 불렀는데, 그 뒤 SW 검수가 독립 칸으로 되살아나 같은 칩이 두 상태를
  //   가리키게 됐다 — 어디까지 갔는지 목록만 봐선 알 수 없었다.
  if (o.softwareInspectionDone) return { key: "inspected", label: "SW 검수 완료", chip: "chip-teal" };
  if (o.productionDone) return { key: "produced", label: "제작 완료", chip: "chip-violet" };
  if (o.preparing) return { key: "preparing", label: "준비 중", chip: "chip-blue" };
  return { key: "waiting", label: "제작 대기", chip: "chip-slate" };
}

/* 쇼핑몰 관점 상태(주문관리 탭에서 씀) */
const MALL_STATUS_CHIP = {
  unpaid: "chip-red", preparing: "chip-blue", shipping: "chip-violet",
  delivered: "chip-green", cancelled: "chip-slate",
};
function mallStatusChip(o) {
  return `<span class="chip ${MALL_STATUS_CHIP[o.mallStatus] || "chip-slate"}">${escapeHtml(o.mallStatusLabel || "-")}</span>`;
}

function isEditingInput() {
  const el = document.activeElement;
  return el && ["INPUT", "TEXTAREA", "SELECT"].includes(el.tagName);
}

/* 채널별 색(대표 2026-08-24: "실제 API 입력하는 몰마다 색상이 다르게").
   ★모든 화면(셋팅·주문·배송)이 이 함수 하나를 쓴다 — 색은 CSS .ch-* 에서만 정한다. */
const CHANNEL_CLASS = {
  "고도몰": "ch-godo", "쿠팡": "ch-coupang", "카카오": "ch-kakao",
  "스마트스토어": "ch-naver", "네이버": "ch-naver", "토스": "ch-toss",
  "11번가": "ch-st11", "롯데온": "ch-lotte", "지마켓": "ch-gmarket",
  "옥션": "ch-auction", "테무": "ch-temu", "수기": "ch-manual",
  "전화": "ch-manual", "방문": "ch-manual", "b2b": "ch-b2b", "B2B": "ch-b2b",
};
function chBadge(ch) {
  const cls = CHANNEL_CLASS[(ch || "").trim()] || "chip-blue";
  return `<span class="chip ${cls}">${escapeHtml(ch || "-")}</span>`;
}

/* ---------------- 여러 건 선택 → 일괄 처리 ---------------- */

function wireOrderPicks() {
  const rows = $$(".of-pick");
  rows.forEach((cb) => cb.addEventListener("click", (e) => {
    const id = Number(cb.dataset.pick);
    // Shift+클릭으로 범위 선택 — 스무 건을 하나씩 누르지 않게
    if (e.shiftKey && state.orderLastPick != null) {
      const ids = rows.map((x) => Number(x.dataset.pick));
      const a = ids.indexOf(state.orderLastPick), b = ids.indexOf(id);
      if (a >= 0 && b >= 0) {
        for (let i = Math.min(a, b); i <= Math.max(a, b); i++) {
          cb.checked ? state.orderPicked.add(ids[i]) : state.orderPicked.delete(ids[i]);
          rows[i].checked = cb.checked;
        }
      }
    } else {
      cb.checked ? state.orderPicked.add(id) : state.orderPicked.delete(id);
    }
    state.orderLastPick = id;
    renderBulkBar();
  }));
  const all = $("#of-all");
  if (all) {
    all.checked = rows.length > 0 && rows.every((cb) => cb.checked);
    all.onclick = () => {
      rows.forEach((cb) => {
        cb.checked = all.checked;
        all.checked ? state.orderPicked.add(Number(cb.dataset.pick))
                    : state.orderPicked.delete(Number(cb.dataset.pick));
      });
      renderBulkBar();
    };
  }
  renderBulkBar();
}

function renderBulkBar() {
  const bar = $("#of-bulkbar");
  if (!bar) return;
  const n = state.orderPicked.size;
  if (!n) {
    bar.style.display = "none"; bar.className = ""; bar.innerHTML = "";
    document.body.classList.remove("has-bulkbar");
    return;
  }
  const canWork = hasPerm("orders.work"), canEdit = hasPerm("orders.edit");
  bar.style.display = "block";
  // ★화면 아래에 붙여 둔다 — 목록 위에만 그리면 아래쪽 주문을 체크했을 때
  //   버튼이 통째로 화면 밖에 있어 '아무 일도 안 일어난' 것처럼 보인다.
  bar.className = "bulk-dock";
  document.body.classList.add("has-bulkbar");
  // ★2026-08-31 대표: 일괄 단계변경·송장·배송완료·보관·취소 버튼은 여기서 뺐다 —
  //   "그 외 기능은 셋팅/QC쪽에서 쓰니까". 백엔드 /api/orders/bulk 액션은 그대로 있다.
  bar.innerHTML = `
    <div class="inline-row" style="background:var(--primary-soft); border-radius:8px; padding:10px 12px; margin:8px 0;">
      <b>${n}건 선택됨</b>
      ${canWork || canEdit ? `<button class="btn btn-sm btn-primary" data-bulk="optlabel"
        title="선택 중 아직 안 뽑은 주문만 옵션라벨(제품코드·주문자·모델명·옵션표·제공옵션)을 인쇄합니다.
전부 이미 뽑은 선택이면 다시 인쇄할지 물어봅니다(라벨을 잃어버린 경우)">🏷 옵션라벨</button>` : ""}
      ${canEdit ? `<button class="btn btn-sm" data-bulk="review" title="체험단·리뷰용 출고로 표시합니다. 셋팅·QC 화면에 크게 뜹니다">🎁 리뷰어 지정</button>` : ""}
      <span style="flex:1"></span>
      <button class="btn btn-ghost btn-sm" data-bulk="clear">선택 해제</button>
    </div>`;
  $$("button[data-bulk]", bar).forEach((b) =>
    b.addEventListener("click", () => runBulk(b.dataset.bulk)));
}

/* 처리 결과는 일괄바 '바깥'에 그린다 — 목록이 새로 그려져도 왜 안 됐는지가 남아 있어야
   같은 버튼을 반복해 누르지 않는다. 닫기 전까지 유지된다. */
function bulkReport(title, done, failed) {
  const host = $("#of-bulkresult");
  if (!host) return;
  host.innerHTML = `
    <div style="border:1px solid ${failed.length ? "var(--danger)" : "var(--border)"};
                border-radius:8px; padding:10px 12px; margin:8px 0;">
      <div class="inline-row" style="margin:0;">
        <b style="flex:1;">${escapeHtml(title)} — 성공 ${done}건${failed.length ? ` · <span style="color:var(--danger)">실패 ${failed.length}건</span>` : ""}</b>
        <button class="btn btn-ghost btn-sm" id="of-bulkresult-close">닫기</button>
      </div>
      ${failed.length ? `<ul style="margin:6px 0 0 18px;">${failed.map((f) =>
        `<li class="muted">${escapeHtml(f.label || `#${f.id}`)} — ${escapeHtml(f.reason)}</li>`).join("")}</ul>` : ""}
    </div>`;
  $("#of-bulkresult-close").addEventListener("click", () => { host.innerHTML = ""; });
}

async function runBulk(action) {
  const ids = [...state.orderPicked];
  if (action === "clear") {
    state.orderPicked.clear();
    $$(".of-pick").forEach((cb) => { cb.checked = false; });
    renderBulkBar();
    return;
  }
  if (!ids.length) return;

  if (action === "optlabel") return bulkOptionLabels(ids);

  let body = { action, ids, value: true };
  if (action === "review") {
    // ★[취소]는 '그만두기'여야 한다. 예전엔 [취소]가 곧 '해제'라서, 잘못 눌러 빠져나오려다
    //   멀쩡한 리뷰어 표시가 풀렸다(2026-07-29 전수조사). 지정/해제는 선택한 주문 상태로 판단한다.
    const picked = (state.orders || []).filter((o) => ids.includes(o.id));
    const allReview = picked.length > 0 && picked.every((o) => o.isReview);
    if (allReview) {
      if (!confirm(`선택한 ${ids.length}건을 리뷰어 출고에서 해제할까요?`)) return;
      body.value = false;
    } else {
      if (!confirm(`선택한 ${ids.length}건을 리뷰어 출고로 지정할까요?\n`
                   + "셋팅·QC 화면에 크게 표시되고, 자산 매칭 없이도 송장을 뽑을 수 있습니다.")) return;
      body.value = true;
      const note = prompt("셋팅·QC에 함께 띄울 안내(선택)", "제품X 빈박스출고");
      if (note === null) return;
      body.reason = note.trim();
    }
  }
  try {
    const r = await api("/api/orders/bulk", { method: "POST", body });
    bulkReport(`일괄 ${action}`, r.ok, r.failed);
    state.orderPicked = new Set(r.failed.map((f) => f.id));   // 실패분만 남겨 재시도하기 쉽게
    $("#of-search").click();
  } catch (err) { toast(err.message, true); }
}

/* 옵션라벨 일괄 인쇄(대표 2026-08-31) — 셋팅·QC가 작업대에 붙이는 옵션표.
   기본은 '아직 안 뽑은 주문만'(한 번 뽑은 걸 또 뽑을 필요는 없다). 선택이 전부
   이미 뽑은 주문이면(라벨 분실 등) 물어보고 다시 인쇄한다. 인쇄창이 실제로 열렸을
   때만 인쇄 기록을 남긴다 — 팝업이 막혔는데 '뽑음'으로 남으면 영영 안 나온다. */
async function bulkOptionLabels(ids) {
  const picked = (state.orders || []).filter((o) => ids.includes(o.id));
  if (!picked.length) return;
  let targets = picked.filter((o) => !o.optLabelAt);
  const skipped = picked.length - targets.length;
  if (!targets.length) {
    if (!confirm(`선택한 ${picked.length}건 모두 이미 옵션라벨을 뽑은 주문입니다.\n다시 인쇄할까요?`)) return;
    targets = picked;
  }
  const opened = await printOptionLabels(targets);   // app.js — 레이아웃 로드 + 인쇄창
  if (!opened) return;
  // ★인쇄창을 띄운 것과 실제로 뽑힌 것은 다르다(2026-09-03 대표) — 사람에게 확인받는다.
  //   [취소]면 기록하지 않아 '안 뽑은 것만' 인쇄에서 다시 잡힌다.
  //   장수는 주문 수가 아니라 '대수 합'이다(한 사람이 4대면 4장 나간다).
  const sheets = targets.reduce(
    (n, o) => n + Math.max(1, (o.assets || []).length || Number(o.quantity) || 1), 0);
  if (!(await confirmPrinted(sheets))) {
    toast("표시하지 않았습니다 — 목록에는 '안 뽑음'으로 남습니다.");
    return;
  }
  try {
    const r = await api("/api/orders/bulk", { method: "POST",
      body: { action: "optlabel", ids: targets.map((o) => o.id), value: true } });
    bulkReport(`옵션라벨 인쇄${skipped ? ` (이미 뽑은 ${skipped}건 건너뜀)` : ""}`, r.ok, r.failed);
    state.orderPicked = new Set(r.failed.map((f) => f.id));   // 실패분만 남긴다(runBulk 관례)
  } catch (err) { toast(err.message, true); }
  $("#of-search").click();
}

/* 조회 조건 → 쿼리스트링. 목록과 엑셀 내보내기가 같은 조건을 쓰게 한 곳에서 만든다. */
function orderQuery(f, extra) {
  const p = new URLSearchParams(Object.assign({ view: f.view }, extra || {}));
  // '진행중' 기본 보기는 출고 전 주문만. 배송중은 상단 배송중 카드를 눌러 따로 본다.
  if (f.view === "active" && !f.mallStatus) p.set("progressOnly", "1");
  if (f.q) p.set("q", f.q);
  if (f.channel) p.set("channel", f.channel);
  if (f.mallStatus) p.set("mallStatus", f.mallStatus);
  if (f.from) p.set("from", f.from);
  if (f.to) p.set("to", f.to);
  return p;
}

/* 보기 — 끝난 주문은 '보관'으로 치워 오늘 할 일만 남긴다 */
const VIEW_TABS = [["active", "진행 중"], ["archived", "보관"],
                   ["cancelled", "취소"], ["all", "전체"]];

/* 기간 프리셋 — 매번 달력을 두 번 클릭하지 않도록 */
const DATE_PRESETS = [["today", "오늘"], ["yesterday", "어제"], ["7d", "최근7일"],
                      ["month", "이번달"], ["", "전체"]];

function presetRange(key) {
  const d = new Date();
  const iso = (x) => `${x.getFullYear()}-${String(x.getMonth() + 1).padStart(2, "0")}-${String(x.getDate()).padStart(2, "0")}`;
  if (key === "today") return [iso(d), iso(d)];
  if (key === "yesterday") { d.setDate(d.getDate() - 1); return [iso(d), iso(d)]; }
  if (key === "7d") { const e = iso(d); d.setDate(d.getDate() - 6); return [iso(d), e]; }
  if (key === "month") return [iso(new Date(d.getFullYear(), d.getMonth(), 1)), iso(d)];
  return ["", ""];
}

/* 내부 메모 표시 — 작업 지시가 목록에서 보이지 않으면 있으나 마나다 */
function memoBadge(o) {
  const m = (o.memo || "").trim();
  if (!m) return "";
  return `<span class="chip chip-red" style="margin-left:4px;" title="${escapeHtml(m)}">📌 ${escapeHtml(m.length > 18 ? m.slice(0, 18) + "…" : m)}</span>`;
}

async function fetchOrders(params) {
  const p = new URLSearchParams(params || {});
  const res = await api("/api/orders?" + p.toString());
  return res.orders;
}

function renderOrdersView(main) {
  if (!hasPerm("orders.view")) {
    main.innerHTML = `<h1 class="page-title">주문관리</h1><div class="card placeholder"><p>주문 조회 권한이 없습니다.</p></div>`;
    return;
  }
  const f = state.orderFilter || (state.orderFilter = {
    // 기본은 '진행 중' — 626건이 한 화면에 다 나오면 오늘 할 일이 안 보인다
    q: "", channel: "", view: "active", mallStatus: "", from: "", to: "",
  });
  if (!state.orderPicked) state.orderPicked = new Set();
  main.innerHTML = `
    <h1 class="page-title">주문관리</h1>
    <p class="page-desc">쇼핑몰별 주문을 입금대기 → 준비중 → 배송중 → 배송완료로 관리합니다</p>
    <div class="kpi-row" id="of-status-cards"></div>
    <div class="card" style="padding:12px 16px;">
      <div class="inline-row" style="margin:0; flex-wrap:wrap;">
        <b class="muted" style="font-size:13px;">보기</b>
        <button class="btn btn-sm ${f.mallStatus === "preparing" ? "btn-primary" : ""}"
          data-preparing-only title="표의 주문상태가 준비중인 주문만 봅니다">준비중</button>
        <button class="btn btn-sm ${f.view === "active" && f.mallStatus !== "preparing" ? "btn-primary" : ""}"
          data-oview="active">진행중</button>
        ${VIEW_TABS.slice(1).map(([k, l]) =>
          `<button class="btn btn-sm ${f.view === k ? "btn-primary" : ""}" data-oview="${k}">${l}</button>`).join(" ")}
        <span style="width:14px;"></span>
        <b class="muted" style="font-size:13px;">쇼핑몰</b>
        <span id="of-mall-buttons"></span>
      </div>
    </div>
    <div class="card">
      <div class="inline-row">
        <input type="text" id="of-q" placeholder="주문번호/수취인/상품 검색" value="${escapeHtml(f.q)}" style="min-width:220px;">
        <select id="of-status"><option value="">전체 상태</option>
          <option value="unpaid" ${f.mallStatus === "unpaid" ? "selected" : ""}>입금대기</option>
          <option value="preparing" ${f.mallStatus === "preparing" ? "selected" : ""}>준비중(결제완료)</option>
          <option value="shipping" ${f.mallStatus === "shipping" ? "selected" : ""}>배송중</option>
          <option value="delivered" ${f.mallStatus === "delivered" ? "selected" : ""}>배송완료</option>
          <option value="cancelled" ${f.mallStatus === "cancelled" ? "selected" : ""}>취소</option>
        </select>
        <input type="date" id="of-from" value="${escapeHtml(f.from)}" title="주문일 시작">
        <span class="muted">~</span>
        <input type="date" id="of-to" value="${escapeHtml(f.to)}" title="주문일 끝">
        ${DATE_PRESETS.map(([k, l]) => `<button class="btn btn-ghost btn-sm" data-dpreset="${k}">${l}</button>`).join("")}
        <button class="btn btn-sm btn-primary" id="of-search">조회</button>
        <span class="muted" id="of-count"></span>
        <span style="flex:1"></span>
        <button class="btn btn-sm" id="of-export" title="지금 조회 조건 그대로 엑셀로 받습니다">📤 엑셀</button>
        ${hasPerm("orders.edit") ? `<button class="btn btn-sm" id="of-new">＋ 수기 주문</button>` : ""}
        ${hasPerm("orders.import") ? `<button class="btn btn-sm btn-primary" id="of-refresh" title="키가 등록된 모든 쇼핑몰에서 지금 바로 주문을 가져옵니다(평소에는 몰별 주기에 맞춰 자동 수집됩니다)">🔄 쇼핑몰 새로고침</button>
        <button class="btn btn-sm" id="of-collect" title="몰별 상태 확인·기간 지정 수집·미리보기">⚙ 수집 상세</button>
        <button class="btn btn-sm" id="of-import">📥 엑셀 가져오기</button>` : ""}
      </div>
      <div id="of-bulkbar" style="display:none;"></div>
      <div id="of-bulkresult"></div>
      <div id="collect-panel"></div>
      <div id="import-panel"></div>
      <div id="neworder-panel"></div>
      <div class="table-wrap"><table>
        <thead><tr><th style="width:28px;"><input type="checkbox" id="of-all" title="이 목록 전체 선택"></th>
          <th>쇼핑몰</th><th>주문번호</th><th>주문일</th><th>상품</th><th>수량</th><th>금액</th><th>수취인</th><th>주문상태</th><th>작업</th><th></th></tr></thead>
        <tbody id="order-rows"><tr><td colspan="11" class="muted">불러오는 중…</td></tr></tbody>
      </table></div>
      <div id="of-pager"></div>
    </div>
    <div id="order-detail"></div>`;

  const loadSummary = async () => {
    const seq = state.renderSeq;
    try {
      // 목록과 같은 조건을 보낸다 — 카드 숫자와 눌렀을 때 나오는 목록이 어긋나면 안 된다
      const s = await api("/api/orders/summary?" + orderQuery(f).toString());
      if (seq !== state.renderSeq || !$("#of-status-cards")) return;
      state.orderSummary = s;
      $("#of-status-cards").innerHTML = s.statuses.map((st) => `
        <div class="kpi" style="cursor:pointer; border-left-color:${
          st.code === "unpaid" ? "var(--danger)" : st.code === "delivered" ? "var(--primary)"
          : st.code === "cancelled" ? "var(--text-dim)" : "var(--blue)"};"
          data-statuscard="${st.code}">
          <div class="kpi-label">${escapeHtml(st.label)}</div>
          <div class="kpi-value">${st.count}건</div>
          <div class="muted" style="font-size:12px;">${fmtWon(st.amount)}</div>
        </div>`).join("");
      $$("[data-statuscard]").forEach((el) => el.addEventListener("click", () => {
        f.mallStatus = f.mallStatus === el.dataset.statuscard ? "" : el.dataset.statuscard;
        const sel = $("#of-status"); if (sel) sel.value = f.mallStatus;
        loadList();
      }));
      // 쇼핑몰 버튼(실제 주문이 있는 몰만).
      // ★[전체] 버튼도 여기서 함께 그린다 — 고정 HTML에 두면 갱신 때마다 리스너가 쌓여
      //   30분쯤 켜두면 한 번 눌렀을 때 요청이 수십 배로 늘어 화면이 멎는다.
      const host = $("#of-mall-buttons");
      if (!host) return;
      host.innerHTML = [
        `<button class="btn btn-sm ${f.channel === "" ? "btn-primary" : ""}" data-mall="">전체</button>`,
        ...s.malls.map((m) => `
          <button class="btn btn-sm ${f.channel === m.channel ? "btn-primary" : ""}" data-mall="${escapeHtml(m.channel)}">
            ${escapeHtml(m.channel)} <span class="muted">${m.total}</span>
          </button>`),
      ].join(" ");
      $$("button[data-mall]", host).forEach((b) => b.addEventListener("click", () => {
        // '(미지정)'은 쇼핑몰이 비어 있는 건만 보는 필터다(예전엔 전체가 나왔다)
        f.channel = b.dataset.mall;
        resetPage("orders"); loadList(); loadSummary();
      }));
    } catch (_e) { /* 집계 실패는 목록을 막지 않는다 */ }
  };

  const loadList = async () => {
    const seq = state.renderSeq;
    try {
      const res = await api("/api/orders?" + orderQuery(f, { limit: "1000" }).toString());
      const orders = res.orders || [];
      if (seq !== state.renderSeq || !$("#order-rows")) return;
      state.orders = orders;
      const cnt = $("#of-count");
      if (cnt) cnt.textContent = `${res.shown}건${res.total > res.shown ? ` / 전체 ${res.total}` : ""}`;
      // ★한 화면에 1,000줄을 그리면 느리고 스크롤이 끝없다 — 잘라서 화살표로 넘긴다
      const pgO = paged(orders, "orders");
      $("#order-rows").innerHTML = pgO.rows.map((o) => {
        const work = orderStatusInfo(o);
        return `<tr>
          <td><input type="checkbox" class="of-pick" data-pick="${o.id}" ${state.orderPicked.has(o.id) ? "checked" : ""}></td>
          <td>${chBadge(o.channel)}</td>
          <td>${escapeHtml(o.orderNumber || "-")}${o.pendingShippingUpdate ? ' <span class="chip chip-red" title="배송지 변경 대기">배송지!</span>' : ""}</td>
          <td class="muted">${escapeHtml((o.orderedAt || "").slice(0, 10))}</td>
          <td style="max-width:320px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${escapeHtml([o.productCode, o.productName, o.optionName].filter(Boolean).join(" "))}">${o.productCode ? `<span class="order-product-code" title="자체 상품코드">${escapeHtml(o.productCode)}</span> ` : ""}${o.isReview ? '<span class="chip chip-violet" style="font-size:11px;">🎁 리뷰어</span> ' : ""}${o.optLabelAt ? `<span class="chip chip-green" style="font-size:11px;" title="옵션라벨 인쇄됨 · ${escapeHtml(String(o.optLabelAt).slice(0, 16).replace("T", " "))}${o.optLabelBy ? ` · ${escapeHtml(o.optLabelBy)}` : ""}">🏷</span> ` : ""}${escapeHtml(o.productName)}${o.optionName ? ` <span class="muted">${escapeHtml(o.optionName)}</span>` : ""}
            ${memoBadge(o)}</td>
          <td>${o.quantity}</td>
          <td>${fmtWon(o.amount)}</td>
          <td>${escapeHtml(o.recipient)}${o.sameCustomerCount > 0
            ? ` <span class="chip chip-blue" style="font-size:11px;" title="같은 연락처의 다른 주문이 ${o.sameCustomerCount}건 있습니다 — 엑셀 업로드분과 API 수집분도 같은 고객으로 묶어 셉니다. 상세의 [👤 이 고객의 다른 주문]에서 확인하세요.">👤${o.sameCustomerCount}</span>` : ""}</td>
          <td>${mallStatusChip(o)}</td>
          <td><span class="chip ${work.chip}" style="font-size:11px;">${work.label}</span></td>
          <td><button class="btn btn-sm" data-odetail="${o.id}">상세</button></td>
        </tr>`;
      }).join("") || `<tr><td colspan="11" class="muted">해당 조건의 주문이 없습니다.</td></tr>`;
      const pgHost = $("#of-pager");
      if (pgHost) { pgHost.innerHTML = pgO.bar; wirePager(pgHost, "orders", () => loadList()); }
      $$("button[data-odetail]").forEach((b) => b.addEventListener("click", () => {
        state.orderDetailId = Number(b.dataset.odetail);
        renderOrderDetail();
      }));
      // ★조회 조건이 바뀌면 선택도 현재 목록 기준으로 정리한다.
      //   안 그러면 화면에 없는 주문까지 일괄 취소·송장 발급 대상이 된다.
      const visible = new Set(orders.map((o) => o.id));
      const dropped = [...state.orderPicked].filter((id) => !visible.has(id));
      if (dropped.length) {
        dropped.forEach((id) => state.orderPicked.delete(id));
        toast(`조회 조건이 바뀌어 선택 ${dropped.length}건이 해제되었습니다.`);
      }
      wireOrderPicks();
    } catch (err) {
      const el = $("#order-rows");
      if (seq === state.renderSeq && el) el.innerHTML = `<tr><td colspan="10" class="muted">${escapeHtml(err.message)}</td></tr>`;
    }
  };
  const doSearch = () => {
    f.q = $("#of-q").value.trim(); f.mallStatus = $("#of-status").value;
    f.from = $("#of-from").value; f.to = $("#of-to").value;
    loadList();
    loadSummary();     // 카드 숫자 = 눌렀을 때 나오는 목록 — 같이 갱신해야 어긋나지 않는다
  };
  loadSummary();
  $("#of-search").addEventListener("click", doSearch);
  autoSearch("#of-q", doSearch);
  $("#of-status").addEventListener("change", doSearch);   // 상태는 셀렉트/카드 양쪽에서 바꿀 수 있다
  $$("button[data-oview]", main).forEach((b) => b.addEventListener("click", () => {
    f.view = b.dataset.oview;
    // '준비중'은 진행중 보기의 세부 필터다. 진행중 버튼을 직접 누르면 전체 진행 건으로 돌아간다.
    if (f.view === "active" && f.mallStatus === "preparing") f.mallStatus = "";
    state.orderPicked.clear();
    resetPage("orders");
    renderOrdersView(main);
  }));
  $$("button[data-preparing-only]", main).forEach((b) => b.addEventListener("click", () => {
    f.view = "active";
    f.mallStatus = f.mallStatus === "preparing" ? "" : "preparing";
    state.orderPicked.clear();
    resetPage("orders");
    renderOrdersView(main);
  }));
  $$("button[data-dpreset]", main).forEach((b) => b.addEventListener("click", () => {
    const [from, to] = presetRange(b.dataset.dpreset);
    $("#of-from").value = from; $("#of-to").value = to;
    doSearch();
  }));
  $("#of-export").addEventListener("click", () => {
    window.open("/api/orders/export?" + orderQuery(f).toString(), "_blank");
  });
  const importBtn = $("#of-import");
  if (importBtn) importBtn.addEventListener("click", renderImportPanel);
  const collectBtn = $("#of-collect");
  if (collectBtn) collectBtn.addEventListener("click", renderCollectPanel);
  // 🔄 새로고침 — 준비된 몰 전부 즉시 동기화. 평소에는 몰별 주기로 자동 수집되므로
  // 이 버튼은 '지금 확인하고 싶을 때'용이다.
  const refreshBtn = $("#of-refresh");
  if (refreshBtn) refreshBtn.addEventListener("click", async () => {
    refreshBtn.disabled = true;
    const prev = refreshBtn.textContent;
    refreshBtn.textContent = "🔄 수집 중…";
    try {
      const r = await api("/api/malls/collect-all", { method: "POST", body: {} });
      const parts = (r.results || []).map((x) => x.ok
        ? `${x.mall} +${x.added}건${x.skippedRental ? ` (렌탈 제외 ${x.skippedRental})` : ""}`
        : `${x.mall} 실패`);
      const failed = (r.results || []).filter((x) => !x.ok);
      toast(parts.join(" · ") || "수집할 몰이 없습니다.", failed.length > 0);
      if (failed.length) {
        // 실패 사유는 상세 패널에서 그대로 보여준다.
        // ★await 없이 쓰면 패널이 아직 '확인하는 중…'이라 사유가 조용히 사라진다.
        await renderCollectPanel();
        const out = $("#cl-result");
        if (out) out.innerHTML = failed.map((x) =>
          `<span style="color:var(--danger)">${escapeHtml(x.mall)}: ${escapeHtml(x.error || "")}</span>`).join("<br>");
      }
      $("#of-search") && $("#of-search").click();
    } catch (err) { toast(err.message, true); }
    finally { refreshBtn.disabled = false; refreshBtn.textContent = prev; }
  });
  const newBtn = $("#of-new");
  if (newBtn) newBtn.addEventListener("click", renderNewOrderPanel);
  loadList();
  // 주문이 수백 건이면 전체 재렌더링이 부담이라 30초로 둔다(셋팅 탭이 5초로 촘촘히 본다)
  addPoller(() => {
    // ★여기서는 페이지를 되돌리지 않는다 — 30초마다 1쪽으로 튕기면 3쪽을 볼 수가 없다
    if (!isEditingInput() && state.view === "orders") { loadList(); loadSummary(); }
  }, 30000);
  if (state.orderDetailId) renderOrderDetail();
}

/* ---------------- 쇼핑몰 주문 수집 ---------------- */

async function renderCollectPanel() {
  const host = $("#collect-panel");
  revealPanel(host);   // 결과 줄이 화면 밖에 그려져 '수집 중…'조차 안 보이던 문제
  host.innerHTML = `<div style="border:1px dashed var(--border); border-radius:8px; padding:14px; margin:8px 0;">
    <p class="muted">몰 연동 상태를 확인하는 중…</p></div>`;
  let malls;
  try { malls = await api("/api/mall-status"); }
  catch (err) {
    host.innerHTML = `<div style="border:1px dashed var(--border); border-radius:8px; padding:14px; margin:8px 0;">
      <p class="muted">${escapeHtml(err.message)} — 설정 > API 관리 권한이 있어야 몰 상태를 볼 수 있습니다.</p></div>`;
    return;
  }
  const ready = malls.filter((m) => m.ready);
  host.innerHTML = `
    <div style="border:1px dashed var(--border); border-radius:8px; padding:14px; margin:8px 0;">
      <b>쇼핑몰 주문 수집</b>
      <span class="muted">— 키가 등록되고 [이 몰 사용]이 켜진 몰은 몰별 주기에 맞춰 자동 수집됩니다.
      여기서는 기간을 지정해 수동으로 가져오거나 미리 볼 수 있습니다.</span>
      <div class="inline-row" style="margin-top:8px;">
        <label class="muted">기간</label>
        <select id="cl-days">
          <option value="1">최근 1일</option>
          <option value="3" selected>최근 3일</option>
          <option value="7">최근 7일</option>
          <option value="14">최근 14일</option>
        </select>
        <button class="btn btn-ghost btn-sm" id="cl-close">닫기</button>
      </div>
      <div class="table-wrap" style="margin-top:8px;"><table>
        <thead><tr><th>몰</th><th>상태</th><th>마지막 수집</th><th></th></tr></thead>
        <tbody>${malls.map((m) => `
          <tr>
            <td><b>${escapeHtml(m.name)}</b></td>
            <td>${m.ready ? `<span class="chip chip-green">수집 가능</span> <span class="muted" style="font-size:12px;">자동 ${m.intervalMin || "-"}분마다</span>`
                : `<span class="chip chip-slate">대기</span> <span class="muted" style="font-size:12px;">${escapeHtml(m.implemented ? m.reason : "어댑터 준비 중")}</span>`}</td>
            <td class="muted" style="font-size:12px;">${m.lastSync
              ? escapeHtml((m.lastSync.at || "").slice(5, 16).replace("T", " ")) +
                (m.lastSync.ok ? ` · 추가 ${m.lastSync.added || 0}건` : ` · <span style="color:var(--danger)">실패</span>`)
              : "-"}</td>
            <td>${m.ready ? `<button class="btn btn-sm" data-preview="${m.code}">미리보기</button>
              <button class="btn btn-sm btn-primary" data-collect="${m.code}">수집</button>
              <button class="btn btn-sm" data-fillamt="${m.code}"
                title="이 몰에서 금액이 0원으로 들어온 주문을 다시 조회해 금액을 채웁니다&#10;(값이 있는 주문은 건드리지 않습니다)">💰 0원 채우기</button>` : ""}</td>
          </tr>`).join("")}
        </tbody></table></div>
      <div id="cl-result" class="muted" style="margin-top:8px;"></div>
      ${ready.length ? "" : `<p class="muted" style="margin-top:8px;">
        아직 수집 가능한 몰이 없습니다. 설정 > API 관리에서 키를 입력하고 [이 몰 사용]을 켜세요.</p>`}
    </div>`;
  $("#cl-close").addEventListener("click", () => { host.innerHTML = ""; });

  // ★금액이 0원으로 들어온 주문 고치기(대표 2026-08-05).
  //   쿠팡이 금액을 Money 객체로 바꿔 보내며 7/28부터 38건이 0원이 됐다.
  //   파서는 고쳤지만 이미 들어온 건은 다시 받아 채워야 한다.
  //   ★값이 있는 주문은 건드리지 않는다 — 손으로 고친 금액을 몰 값으로 되돌리면 안 된다.
  $$("button[data-fillamt]").forEach((b) => b.addEventListener("click", async () => {
    const code = b.dataset.fillamt;
    $("#cl-result").innerHTML = "0원 주문을 다시 조회하는 중…";
    try {
      const pv = await api(`/api/malls/${code}/backfill-amounts`,
                           { method: "POST", body: { dryRun: true } });
      if (!pv.filled) {
        $("#cl-result").innerHTML = `0원인 주문 ${pv.checked}건 중 몰에서 금액을 받은 건이 없습니다.`
          + (pv.errors && pv.errors.length ? `<br><span class="muted">${escapeHtml(pv.errors[0])}</span>` : "");
        return;
      }
      if (!confirm(`0원으로 들어온 주문 ${pv.checked}건 중 ${pv.filled}건의 금액을 몰에서 받았습니다.\n`
        + `합계 ${fmtWon(pv.amount)}\n\n`
        + "이 금액으로 채울까요? (이미 금액이 있는 주문은 건드리지 않습니다)")) {
        $("#cl-result").innerHTML = "";
        return;
      }
      const r = await api(`/api/malls/${code}/backfill-amounts`, { method: "POST", body: {} });
      $("#cl-result").innerHTML = `<b>금액 ${r.filled}건 채움</b> — 합계 ${fmtWon(r.amount)}`;
      toast(`${r.filled}건의 금액을 채웠습니다 (${fmtWon(r.amount)}).`);
      $("#of-search") && $("#of-search").click();
    } catch (err) { $("#cl-result").innerHTML = `<span style="color:var(--danger)">${escapeHtml(err.message)}</span>`; }
  }));
  const run = async (code, dry) => {
    const days = Number($("#cl-days").value) || 3;
    $$("#collect-panel button").forEach((b) => { b.disabled = true; });
    $("#cl-result").innerHTML = "수집 중…";
    try {
      const res = await api(`/api/malls/${code}/collect`, { method: "POST", body: { days, dryRun: dry } });
      $("#cl-result").innerHTML = `<b>${escapeHtml(res.mall)} ${dry ? "미리보기" : "수집 완료"}</b> —
        읽음 ${res.fetched}건 · <b style="color:var(--primary)">${dry ? "추가 예정" : "추가"} ${res.added}건</b> ·
        중복 ${res.duplicates}건 · 배송지변경 ${res.shippingUpdates}건
        ${dry && res.preview && res.preview.length ? `<br>${res.preview.slice(0, 5).map((p) =>
          escapeHtml(`${p.orderNumber} ${p.productName} (${p.recipient})`)).join("<br>")}` : ""}`;
      if (!dry) { toast(`${res.mall} 주문 ${res.added}건을 가져왔습니다.`); $("#of-search").click(); }
    } catch (err) {
      $("#cl-result").innerHTML = `<span style="color:var(--danger)">${escapeHtml(err.message)}</span>`;
    } finally {
      $$("#collect-panel button").forEach((b) => { b.disabled = false; });
    }
  };
  $$("button[data-collect]", host).forEach((b) => b.addEventListener("click", () => run(b.dataset.collect, false)));
  $$("button[data-preview]", host).forEach((b) => b.addEventListener("click", () => run(b.dataset.preview, true)));
}

/* ---------------- 엑셀 가져오기 ---------------- */

function renderImportPanel() {
  const host = $("#import-panel");
  revealPanel(host);        // 다른 패널이 열려 있으면 화면 밖에 그려진다 — 눌렀으면 보여야 한다
  host.innerHTML = `
    <div style="border:1px dashed var(--border); border-radius:8px; padding:14px; margin:8px 0;">
      <b>엑셀 가져오기</b> <span class="muted">— 주문수집/고도몰/카카오/쿠팡/테무/ESM(옥션·G마켓) 양식 자동 감지 (xlsx, zip · 최대 10개/30MB)</span>
      <div class="inline-row" style="margin-top:8px;">
        <input type="file" id="imp-files" multiple accept=".xlsx,.xls,.zip">
        <button class="btn btn-sm" id="imp-preview">미리보기</button>
        <button class="btn btn-sm btn-primary" id="imp-run">가져오기</button>
        <button class="btn btn-ghost btn-sm" id="imp-close">닫기</button>
      </div>
      <div id="imp-result" class="muted" style="margin-top:8px;"></div>
    </div>`;
  const send = async (path) => {
    const files = $("#imp-files").files;
    if (!files.length) { toast("파일을 선택하세요.", true); return; }
    const fd = new FormData();
    for (const fl of files) fd.append("files", fl);
    $("#imp-preview").disabled = $("#imp-run").disabled = true;
    try {
      const res = await fetch(path, { method: "POST", body: fd });
      const data = await res.json().catch(() => null);
      if (!res.ok) throw new Error((data && data.error) || `요청 실패 (${res.status})`);
      const isPreview = path.includes("preview");
      $("#imp-result").innerHTML = `
        <b>${isPreview ? "미리보기" : "가져오기 완료"}</b> —
        해석 ${data.parsed}건 · <b style="color:var(--primary)">추가 ${data.added}건</b> ·
        중복 ${data.duplicates}건 · 배송지변경 감지 ${data.shippingUpdates}건
        ${data.errors && data.errors.length ? `<br><span style="color:var(--danger)">${data.errors.map(escapeHtml).join("<br>")}</span>` : ""}
        ${isPreview && data.preview && data.preview.length ? `<br>추가 예정: ${data.preview.slice(0, 5).map((p) => escapeHtml(`[${p.channel}] ${p.productName} (${p.recipient}) ${Number(p.amount || 0).toLocaleString()}원`)).join(" / ")}${data.added > 5 ? " …" : ""}` : ""}`;
      if (!isPreview) { toast(`주문 ${data.added}건을 가져왔습니다.`); $("#of-search").click(); }
    } catch (err) { toast(err.message, true); }
    finally { $("#imp-preview").disabled = $("#imp-run").disabled = false; }
  };
  $("#imp-preview").addEventListener("click", () => send("/api/orders/import/preview"));
  $("#imp-run").addEventListener("click", () => send("/api/orders/import"));
  $("#imp-close").addEventListener("click", () => { host.innerHTML = ""; });
}

/* ---------------- 고도몰 상품 찾기 ----------------
   수기 주문은 고도몰 자체상품코드를 그대로 쓰는 경우가 많다. 상품명·가격은
   몰에서 수시로 바뀌므로 OWS에 저장해 두지 않고, 입력하는 그 순간 몰에 물어본다. */

const GOODS_DEBOUNCE_MS = 400;

function goodsRowHtml(g, active) {
  const price = g.priceText || (g.price ? `${g.price.toLocaleString()}원` : "가격 없음");
  const tags = [g.stateLabel, g.soldOut ? "품절" : "", g.modelNo].filter(Boolean).join(" · ");
  return `<div class="goods-item${active ? " active" : ""}" data-no="${escapeHtml(g.goodsNo)}"
       style="padding:8px 10px; cursor:pointer; border-bottom:1px solid var(--border); ${active ? "background:var(--slate-soft);" : ""}">
      <div style="display:flex; gap:8px; align-items:baseline;">
        <b style="color:var(--primary); white-space:nowrap;">${escapeHtml(g.goodsCd || "-")}</b>
        <span style="flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;">${escapeHtml(g.goodsNm)}</span>
        <b style="white-space:nowrap;">${escapeHtml(price)}</b>
      </div>
      ${g.shortDescription ? `<div class="muted" style="font-size:12px; margin-top:2px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;">${escapeHtml(g.shortDescription)}</div>` : ""}
      ${tags ? `<div class="muted" style="font-size:11px;">${escapeHtml(tags)}</div>` : ""}
    </div>`;
}

/** 상품코드 입력칸에 고도몰 실시간 검색 드롭다운을 붙인다. */
function attachGoodsLookup(ids) {
  const input = $("#" + ids.code);
  const box = $("#" + ids.code + "-list");
  if (!input || !box) return;
  let timer = null, items = [], cursor = -1, seq = 0;

  const close = () => { box.style.display = "none"; box.innerHTML = ""; items = []; cursor = -1; };
  const paint = () => {
    box.innerHTML = items.length
      ? items.map((g, i) => goodsRowHtml(g, i === cursor)).join("")
      : `<div class="muted" style="padding:8px 10px;">검색 결과가 없습니다.</div>`;
    box.style.display = "block";
    box.querySelectorAll(".goods-item").forEach((el, i) => {
      el.addEventListener("mousedown", (e) => { e.preventDefault(); pick(items[i]); });
    });
  };
  const note = (msg) => {
    box.innerHTML = `<div class="muted" style="padding:8px 10px;">${escapeHtml(msg)}</div>`;
    box.style.display = "block";
  };
  const pick = (g) => {
    if (!g) return;
    const amount = $("#" + ids.amount);
    // ★이미 결제가 끝난 주문의 금액을 오늘 판매가로 덮어쓰면 매출·마진·정산이 통째로 어긋난다.
    //   기존 금액이 있으면 반드시 물어본다(신규 등록은 0이라 그냥 채워진다).
    const qty = Math.max(1, Number(($("#" + ids.qty)?.value || "1").replaceAll(",", "")) || 1);
    const newAmount = g.price ? g.price * qty : 0;
    const oldAmount = Number((amount?.value || "0").replaceAll(",", "")) || 0;
    if (amount && newAmount && oldAmount && oldAmount !== newAmount) {
      if (!confirm(`금액을 ${oldAmount.toLocaleString()}원 → ${newAmount.toLocaleString()}원으로 바꿉니다.\n`
                   + `(오늘 몰에 걸린 판매가${qty > 1 ? ` × 수량 ${qty}` : ""})\n\n계속할까요?`)) {
        close();
        return;
      }
    }
    input.value = g.goodsCd || "";
    const name = $("#" + ids.name);
    if (name) name.value = g.goodsNm || "";
    // 짧은 설명은 옵션칸으로 — 송장 상품명 레이아웃에 함께 찍혀 검수에 쓰인다
    const opt = $("#" + ids.option);
    if (opt && g.shortDescription) opt.value = g.shortDescription;
    if (amount && newAmount) amount.value = newAmount.toLocaleString();
    close();
    toast(`고도몰 상품을 불러왔습니다: ${g.goodsNm}`);
  };

  const search = async () => {
    const q = input.value.trim();
    if (q.length < 2) return close();
    const my = ++seq;
    note("고도몰에서 찾는 중…");
    try {
      const r = await api(`/api/malls/godomall/goods?q=${encodeURIComponent(q)}`);
      if (my !== seq) return;                     // 늦게 온 응답은 버린다
      items = r.goods || []; cursor = -1;
      if (r.message && !items.length) return note(r.message);
      paint();
    } catch (err) {
      if (my !== seq) return;
      note(err.message || "고도몰 상품 조회에 실패했습니다.");
    }
  };

  input.setAttribute("autocomplete", "off");
  input.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(search, GOODS_DEBOUNCE_MS); });
  input.addEventListener("keydown", (e) => {
    if (box.style.display !== "block" || !items.length) {
      if (e.key === "Enter") { e.preventDefault(); clearTimeout(timer); search(); }
      return;
    }
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      cursor = (cursor + (e.key === "ArrowDown" ? 1 : items.length - 1) + items.length) % items.length;
      paint();
    } else if (e.key === "Enter") {
      // ★Enter로 '첫 결과'를 자동 선택하지 않는다. 상품코드를 확인하려다 습관적으로
      //   Enter를 치면 결제금액이 오늘 판매가로 바뀌는 사고가 난다. 골라야 반영된다.
      e.preventDefault();
      if (cursor >= 0) pick(items[cursor]);
    } else if (e.key === "Escape") close();
  });
  input.addEventListener("blur", () => setTimeout(close, 150));
}

/* 제품코드로 고도몰 상품을 찾아 상품명·옵션·금액을 채운다.
   ★대표가 고도몰 상품명에 제품코드를 넣어 등록하므로 상품명 검색으로 걸린다
     (예: "840 G3_i7-6_내장 AA급"). 못 찾으면 조용히 넘어간다 — 수기로 적으면 된다. */
async function lookupGoodsByCode(code) {
  if (!code) return null;
  try {
    const r = await api("/api/malls/godomall/goods?q=" + encodeURIComponent(code));
    const g = (r.goods || [])[0];
    if (!g) return null;
    const name = $("#no-product");
    if (name && !name.value.trim()) name.value = g.goodsNm || "";
    const opt = $("#no-option");
    if (opt && !opt.value.trim() && g.shortDescription) opt.value = g.shortDescription;
    const amt = $("#no-amount");
    if (amt && (!amt.value || amt.value === "0") && g.price) {
      const qty = Math.max(1, Number(($("#no-qty")?.value || "1")) || 1);
      amt.value = (g.price * qty).toLocaleString("ko-KR");
    }
    toast(`고도몰에서 상품을 불러왔습니다: ${g.goodsNm}`);
    return g;
  } catch (_e) {
    return null;   // 고도몰 키가 없거나 못 찾은 것 — 수기 입력을 막지 않는다
  }
}

function goodsLookupField(id, label, value = "", disabled = false) {
  return `<label style="position:relative;">${label}
      <input type="text" id="${id}" value="${escapeHtml(value)}" ${disabled ? "disabled" : ""}
             placeholder="고도몰 자체상품코드/상품명 입력">
      <div id="${id}-list" style="display:none; position:absolute; z-index:40; left:0; right:0; top:100%;
           max-height:280px; overflow:auto; background:var(--surface); border:1px solid var(--border);
           border-radius:8px; box-shadow:0 6px 18px rgba(0,0,0,.18);"></div>
    </label>`;
}

/* 기존 고객 자동완성 — 재구매 고객을 매번 새로 타이핑하지 않게 한다 */
function attachCustomerLookup() {
  const input = $("#no-recipient");
  const box = $("#no-recipient-list");
  if (!input || !box) return;
  let timer = null, items = [], cursor = -1, seq = 0;

  const close = () => { box.style.display = "none"; box.innerHTML = ""; items = []; cursor = -1; };
  const pick = (c) => {
    if (!c) return;
    input.value = c.recipient;
    $("#no-phone").value = c.phone || "";
    $("#no-zip").value = c.postalCode || "";
    $("#no-addr").value = c.address || "";
    close();
    toast(`${c.recipient} 고객 정보를 불러왔습니다(최근 주문 ${c.lastAt}).`);
  };
  const paint = () => {
    box.innerHTML = items.map((c, i) => `
      <div class="cust-item" style="padding:8px 10px; cursor:pointer; border-bottom:1px solid var(--border);
           ${i === cursor ? "background:var(--slate-soft);" : ""}">
        <div><b>${escapeHtml(c.recipient)}</b> <span class="muted">${escapeHtml(c.phone || "번호 없음")}</span>
          <span class="chip" style="margin-left:4px;">${c.count}회</span></div>
        <div class="muted" style="font-size:12px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;">
          ${escapeHtml(c.address || "주소 없음")} · 최근 ${escapeHtml(c.lastAt)}</div>
      </div>`).join("") || `<div class="muted" style="padding:8px 10px;">이전 주문에 없는 고객입니다.</div>`;
    box.style.display = "block";
    box.querySelectorAll(".cust-item").forEach((el, i) =>
      el.addEventListener("mousedown", (e) => { e.preventDefault(); pick(items[i]); }));
  };
  const search = async () => {
    const q = input.value.trim();
    if (q.length < 2) return close();
    const my = ++seq;
    try {
      const r = await api(`/api/orders/customer-search?q=${encodeURIComponent(q)}`);
      if (my !== seq) return;
      items = r; cursor = -1; paint();
    } catch (_e) { close(); }
  };
  input.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(search, 300); });
  input.addEventListener("keydown", (e) => {
    if (box.style.display !== "block" || !items.length) return;
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      cursor = (cursor + (e.key === "ArrowDown" ? 1 : items.length - 1) + items.length) % items.length;
      paint();
    } else if (e.key === "Enter" && cursor >= 0) { e.preventDefault(); pick(items[cursor]); }
    else if (e.key === "Escape") close();
  });
  input.addEventListener("blur", () => setTimeout(close, 150));
}

/* ---------------- 수기 주문 ---------------- */

/* 수령방식 — 택배가 아닌 건이 있다(대표 2026-08-05: 방문수령·퀵).
   ★택배가 아니면 송장이 필요 없다. 셋팅·배송 화면이 이 값을 보고
     '송장 발급 대기'로 잡을지 말지를 정한다. */
const RECEIVE_METHODS = ["택배", "방문수령", "퀵", "직접배송"];
const NO_WAYBILL = ["방문수령", "퀵", "직접배송"];   // 송장이 필요 없는 방식

function needsWaybill(o) {
  return !NO_WAYBILL.includes((o.receiveMethod || "").trim());
}

function receiveChip(o) {
  const m = (o.receiveMethod || "").trim();
  if (!m || m === "택배") return "";
  return `<span class="chip chip-violet" style="font-size:11px;"
    title="택배가 아니라 송장이 필요 없습니다">🚶 ${escapeHtml(m)}</span>`;
}

function renderNewOrderPanel() {
  const host = $("#neworder-panel");
  revealPanel(host);
  host.innerHTML = `
    <div style="border:1px dashed var(--border); border-radius:8px; padding:14px; margin:8px 0;">
      <b>수기 주문 등록</b>
      <div class="form-grid" style="margin-top:8px;">
        <label>채널<select id="no-channel"><option>수기</option><option>전화</option><option>방문</option></select></label>
        <label>수령방식<select id="no-recv">${RECEIVE_METHODS.map((m) =>
          `<option>${m}</option>`).join("")}</select></label>
        <label>주문번호 (선택)<input type="text" id="no-orderno"></label>
        ${codeLookupField("no-sku", "제품코드 🔍")}
        ${goodsLookupField("no-code", "고도몰에서 상품 찾기 🔍")}
        <label>상품명 *<input type="text" id="no-product"></label>
        <label>옵션 / 짧은설명<input type="text" id="no-option"></label>
        <label>내부 메모<input type="text" id="no-memo" placeholder="작업 지시·특이사항"></label>
        <label>수량<input type="number" id="no-qty" value="1" min="1"></label>
        <label>금액<input type="text" id="no-amount" value="0"></label>
        <label style="position:relative;">수취인 * 🔍
          <input type="text" id="no-recipient" autocomplete="off" placeholder="이름 또는 전화번호로 기존 고객 찾기">
          <div id="no-recipient-list" style="display:none; position:absolute; z-index:40; left:0; right:0; top:100%;
               max-height:240px; overflow:auto; background:var(--surface); border:1px solid var(--border);
               border-radius:8px; box-shadow:0 6px 18px rgba(0,0,0,.18);"></div>
        </label>
        <label>연락처<input type="text" id="no-phone"></label>
        <label>우편번호<div class="inline-row" style="gap:4px; margin:0;">
          <input type="text" id="no-zip" style="flex:1;">
          <button class="btn btn-sm" id="no-zip-find" type="button" title="우편번호·주소를 검색해 채웁니다">🔍 주소검색</button></div></label>
      </div>
      <label class="muted" style="display:block; margin-top:8px;">주소<input type="text" id="no-addr" style="width:100%; margin-top:4px; padding:8px 10px; border:1px solid var(--border); border-radius:8px; background:var(--bg);"></label>
      <label class="muted" style="display:block; margin-top:8px;">배송메시지<input type="text" id="no-msg" style="width:100%; margin-top:4px; padding:8px 10px; border:1px solid var(--border); border-radius:8px; background:var(--bg);"></label>
      <div class="editor-actions">
        <button class="btn btn-primary" id="no-save">등록</button>
        <button class="btn" id="no-close">닫기</button>
      </div>
    </div>`;
  attachGoodsLookup({ code: "no-code", name: "no-product", option: "no-option",
                      amount: "no-amount", qty: "no-qty" });
  attachAddrSearch($("#no-zip-find"), { zip: "#no-zip", addr: "#no-addr" });
  // ★제품코드를 고르면 상품명·옵션을 그 코드로 팔던 지난 주문에서 그대로 가져온다.
  //   그래야 등록한 수기 주문이 셋팅/QC에서 몰 주문과 똑같이 보인다(대표 요청 2026-08-05).
  attachCodeLookup("no-sku", (c) => {
    const name = $("#no-product");
    if (name && !name.value.trim() && c.productName) name.value = c.productName;
    const opt = $("#no-option");
    if (opt && !opt.value.trim() && c.options.length) opt.value = c.options[0];
    // 상품명이 아직 비었으면 고도몰에서 그 코드로 찾아 채운다
    if (name && !name.value.trim()) lookupGoodsByCode(c.code);
    toast(`제품코드 ${c.code} — 출고가능 ${c.shippable}대 / 보유 ${c.total}대`);
  });
  attachCustomerLookup();
  $("#no-close").addEventListener("click", () => { host.innerHTML = ""; });
  $("#no-save").addEventListener("click", async () => {
    try {
      await api("/api/orders", { method: "POST", body: {
        channel: $("#no-channel").value, orderNo: $("#no-orderno").value,
        productName: $("#no-product").value.trim(), optionName: $("#no-option").value,
        // ★제품코드를 우선 저장한다 — 셋팅/QC의 재고 대조가 이 값으로 돈다.
        //   비어 있을 때만 고도몰 자체코드를 넣는다(예전 동작 유지).
        productCode: $("#no-sku").value.trim() || $("#no-code").value,
        receiveMethod: $("#no-recv").value,
        quantity: Number($("#no-qty").value) || 1,
        amount: $("#no-amount").value.replaceAll(",", "") || 0,
        recipient: $("#no-recipient").value.trim(), phone: $("#no-phone").value,
        postalCode: $("#no-zip").value, address: $("#no-addr").value,
        deliveryMessage: $("#no-msg").value, memo: $("#no-memo").value,
      } });
      toast("수기 주문을 등록했습니다.");
      host.innerHTML = "";
      $("#of-search").click();
    } catch (err) { toast(err.message, true); }
  });
}

/* 같은 고객의 다른 주문 — 재구매·재발송·클레임 이력을 상세에서 바로 본다 */
async function renderCustomerHistory(o) {
  const host = $("#od-history-panel");
  if (!host) return;
  if (host.innerHTML) { host.innerHTML = ""; return; }
  host.innerHTML = `<p class="muted" style="margin-top:10px;">찾는 중…</p>`;
  let r;
  try {
    r = await api(`/api/orders/customer-history?orderId=${o.id}`);
  } catch (err) { host.innerHTML = `<p class="muted">${escapeHtml(err.message)}</p>`; return; }
  if (!r.count && !r.asTickets.length) {
    host.innerHTML = `<p class="muted" style="margin-top:10px;">이 고객의 다른 주문·A/S 기록이 없습니다.</p>`;
    return;
  }
  host.innerHTML = `
    <div style="border:1px dashed var(--border); border-radius:8px; padding:12px; margin-top:10px;">
      <b>${escapeHtml(o.recipient)} 고객의 다른 기록</b>
      <span class="muted">— ${escapeHtml(r.matchedBy)}로 찾음 · 주문 ${r.count}건 · A/S ${r.asTickets.length}건</span>
      ${r.count ? `<div class="table-wrap" style="margin-top:8px;"><table>
        <thead><tr><th>주문일</th><th>쇼핑몰</th><th>주문번호</th><th>상품</th><th>금액</th><th>상태</th><th></th></tr></thead>
        <tbody>${r.orders.map((x) => `<tr>
          <td class="muted">${escapeHtml((x.orderedAt || "").slice(0, 10))}</td>
          <td>${chBadge(x.channel)}</td>
          <td>${escapeHtml(x.orderNumber || `#${x.id}`)}</td>
          <td style="max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${escapeHtml(x.productName)}
            ${x.memo ? `<span class="chip chip-red" title="${escapeHtml(x.memo)}">📌</span>` : ""}</td>
          <td>${fmtWon(x.amount)}</td>
          <td><span class="chip ${MALL_STATUS_CHIP[x.mallStatus] || "chip-slate"}">${escapeHtml(x.mallStatusLabel)}</span></td>
          <td><button class="btn btn-sm" data-hopen="${x.id}">열기</button></td>
        </tr>`).join("")}</tbody></table></div>` : ""}
      ${r.asTickets.length ? `<div style="margin-top:8px;"><b class="muted">A/S 이력</b>
        ${r.asTickets.map((t) => `<div class="muted" style="font-size:13px;">
          ${escapeHtml(t.receivedAt || "")} · ${escapeHtml(t.ticketNo)} · ${escapeHtml(t.symptom)}</div>`).join("")}</div>` : ""}
    </div>`;
  $$("button[data-hopen]", host).forEach((b) => b.addEventListener("click", () => {
    state.orderDetailId = Number(b.dataset.hopen);
    renderOrderDetail();
  }));
}

/* ---------------- 주문 상세 ---------------- */

async function renderOrderDetail() {
  const host = $("#order-detail");
  if (!host || !state.orderDetailId) return;
  let o;
  try {
    // 카테고리 목록이 없으면 먼저 받는다 — 없으면 select가 비어 저장 시 배정이 지워진다
    if (!state.categories || !state.categories.length) state.categories = await api("/api/categories");
    o = await api(`/api/orders/${state.orderDetailId}`);
  } catch (err) {
    // ★오류 경로도 팝업으로 띄운다. 예전엔 페이지 맨 아래에 조용히 그려져서
    //   대표에게는 '[상세]를 눌렀는데 아무 일도 안 일어난다'로 보였다(2026-07-29 전수조사).
    revealPanel(host);
    host.innerHTML = `<div class="card">
      <div class="inline-row"><h3 style="margin:0; flex:1;">주문을 불러오지 못했습니다</h3>
        <button class="btn btn-sm" id="od-errclose">닫기</button></div>
      <p class="muted">${escapeHtml(err.message)}</p>
      <p class="muted">잠시 후 [상세]를 다시 눌러 주세요. 계속 같으면 새로고침(F5) 후 시도하세요.</p>
    </div>`;
    const cb = $("#od-errclose");
    if (cb) cb.addEventListener("click", () => { state.orderDetailId = null; host.innerHTML = ""; });
    return;
  }
  const canEdit = hasPerm("orders.edit");
  const st = orderStatusInfo(o);
  const cats = state.categories || [];
  const pu = o.pendingShippingUpdate;
  revealPanel(host);   // 긴 목록에서 상세가 화면 밖(아래)에 그려진다 — 눌렀으면 보여야 한다
  host.innerHTML = `
  <div class="card">
    <div class="inline-row">
      <h3 style="margin:0; flex:1;">주문 상세 — ${chBadge(o.channel)} ${escapeHtml(o.orderNumber || `#${o.id}`)} <span class="chip ${st.chip}">${st.label}</span></h3>
      <button class="btn btn-sm" id="od-close">닫기</button>
    </div>
    ${pu ? `<div style="background:var(--danger-soft); border-radius:8px; padding:10px 12px; margin:8px 0;">
      <b>배송지 변경 감지</b> — ${Object.entries(pu.changed || {}).map(([k, v]) =>
        escapeHtml(`${k}: ${v.current || "(없음)"} → ${v.incoming}`)).join(" · ")}
      ${canEdit ? `<div style="margin-top:6px;"><button class="btn btn-sm btn-primary" id="od-applypu">변경 반영</button>
      <button class="btn btn-sm" id="od-dismisspu">무시</button></div>` : ""}
    </div>` : ""}
    <div class="form-grid" style="margin-top:8px;">
      <label>수취인<input type="text" id="od-recipient" value="${escapeHtml(o.recipient)}" ${canEdit ? "" : "disabled"}></label>
      <label>연락처<input type="text" id="od-phone" value="${escapeHtml(o.phone)}" ${canEdit ? "" : "disabled"}></label>
      <label>우편번호<div class="inline-row" style="gap:4px; margin:0;">
        <input type="text" id="od-zip" value="${escapeHtml(o.postalCode)}" ${canEdit ? "" : "disabled"} style="flex:1;">
        ${canEdit ? `<button class="btn btn-sm" id="od-zip-find" type="button" title="우편번호·주소를 검색해 채웁니다">🔍</button>` : ""}</div></label>
      ${goodsLookupField("od-code", "제품코드 / 상품코드 🔍", o.productCode, !canEdit)}
      <label>수령방식<select id="od-recv" ${canEdit ? "" : "disabled"}>${RECEIVE_METHODS.map((m) =>
        `<option ${(o.receiveMethod || "택배") === m ? "selected" : ""}>${m}</option>`).join("")}</select></label>
      <label>상품명<input type="text" id="od-product" value="${escapeHtml(o.productName)}" ${canEdit ? "" : "disabled"}></label>
      <label>옵션 / 짧은설명<input type="text" id="od-option" value="${escapeHtml(o.optionName)}" ${canEdit ? "" : "disabled"}></label>
      <label>주문일시
        <input type="text" id="od-ordered" value="${escapeHtml(o.orderedAt || "")}"
               placeholder="2026-07-31 14:30" ${canEdit ? "" : "disabled"}
               title="어제 받은 전화 주문을 오늘 입력했을 때 실제 주문일로 고칩니다(기간 조회·매출 귀속 기준)."></label>
      <label>주문번호<input type="text" id="od-orderno" value="${escapeHtml(o.orderNumber || o.orderNo || "")}" ${canEdit ? "" : "disabled"}></label>
      <label>채널<input type="text" id="od-channel" value="${escapeHtml(o.channel || "")}" ${canEdit ? "" : "disabled"}
               placeholder="예: 고도몰 / 쿠팡 / 수기"></label>
      <label>수량<input type="number" id="od-qty" value="${o.quantity}" ${canEdit ? "" : "disabled"}></label>
      <label>금액<input type="text" id="od-amount" value="${o.amount}" ${canEdit ? "" : "disabled"}></label>
      <label>카테고리<select id="od-cat" ${canEdit ? "" : "disabled"}>
        <option value="">- 미지정 -</option>
        ${cats.map((c) => `<option value="${c.id}" ${c.id === o.categoryId ? "selected" : ""}>${escapeHtml(c.name)}</option>`).join("")}
      </select></label>
    </div>
    <label class="muted" style="display:block; margin-top:8px;">주소<input type="text" id="od-addr" value="${escapeHtml(o.address)}" style="width:100%; margin-top:4px; padding:8px 10px; border:1px solid var(--border); border-radius:8px; background:var(--bg);" ${canEdit ? "" : "disabled"}></label>
    <label class="muted" style="display:block; margin-top:8px;">배송메시지<input type="text" id="od-msg" value="${escapeHtml(o.deliveryMessage)}" style="width:100%; margin-top:4px; padding:8px 10px; border:1px solid var(--border); border-radius:8px; background:var(--bg);" ${canEdit ? "" : "disabled"}></label>
    <label class="muted" style="display:block; margin-top:8px;">내부 메모 <span style="font-weight:400;">(작업 지시·특이사항 — 셋팅/배송 담당자에게 보입니다)</span>
      <input type="text" id="od-memo" value="${escapeHtml(o.memo || "")}" placeholder="예: 웹캠 불가 모델 — 고객 고지 후 USB웹캠 증정" style="width:100%; margin-top:4px; padding:8px 10px; border:1px solid var(--border); border-radius:8px; background:var(--bg);" ${canEdit ? "" : "disabled"}></label>
    ${canEdit ? `<div style="border:1px solid var(--border); border-radius:8px; padding:10px 12px; margin-top:10px;">
      <b>정산</b> <span class="muted">— 실제로 남는 돈 기준으로 마진을 잡습니다</span>
      <div class="form-grid" style="margin-top:6px;">
        <label>판매수수료${o.feeRate ? ` <span class="muted">(${o.feeRate}% 자동)</span>` : ""}
          <input type="text" id="od-fee" value="${o.feeAmount || 0}"></label>
        <label>출고 택배비<input type="text" id="od-shipcost" value="${o.shippingCost || 0}"></label>
        <label>환불액<input type="text" id="od-refund" value="${o.refundAmount || 0}"></label>
        <label>환불 사유<input type="text" id="od-refundreason" value="${escapeHtml(o.refundReason || "")}"></label>
      </div>
      <div class="inline-row" style="margin:8px 0 0;">
        <span class="muted">실입금 <b>${fmtWon(o.netAmount != null ? o.netAmount : o.amount)}</b>
        — 아래 [저장]을 누르면 위 내용과 함께 저장됩니다.</span>
      </div>
    </div>` : ""}
    <p class="muted" style="margin:10px 0 0;">
      단계: 준비 ${o.preparingBy ? escapeHtml(o.preparingBy) : "-"} · 제작 ${o.productionBy ? escapeHtml(`${o.productionBy} (${o.productionAt.slice(5, 16).replace("T", " ")})`) : "-"} ·
      출고확인 ${o.softwareInspectionBy ? escapeHtml(`${o.softwareInspectionBy} (${o.softwareInspectionAt.slice(5, 16).replace("T", " ")})`) : "-"} ·
      출고 ${o.shippingBy ? escapeHtml(o.shippingBy) : "-"}
      ${o.assets.length ? `<br>매칭 자산: ${o.assets.map((a) => `<span class="chip chip-green">${escapeHtml(a.assetNo)}</span>`).join(" ")}` : ""}
      ${o.trackingNumber ? `<br>송장: <b>${escapeHtml(o.trackingNumber)}</b>` : ""}
      ${(o.waybills || []).filter((w) => w.status !== "canceled").length
        ? `<br>발행 송장: ${(o.waybills || []).filter((w) => w.status !== "canceled").map((w) =>
            `<span class="chip ${w.type === "recall" ? "chip-violet" : "chip-blue"}">${escapeHtml(w.wid)}${
              w.invoiceNo ? " / " + escapeHtml(w.invoiceNo) : ""}${w.type === "recall" ? " (회수)" : ""}</span>`).join(" ")
          } <span class="muted" style="font-size:12px;">— 취소·재발행은 [배송 / 송장] 화면에서</span>`
        : ""}
      ${o.cancelledAt ? `<br><span style="color:var(--danger)">취소: ${escapeHtml(o.cancelledBy)} — ${escapeHtml(o.cancelReason)}</span>` : ""}
    </p>
    <div class="editor-actions">
      ${canEdit ? `<button class="btn btn-primary" id="od-save">저장</button>` : ""}
      ${hasPerm("orders.cancel") && !o.cancelledAt ? `<button class="btn btn-danger" id="od-cancel">주문 취소</button>` : ""}
      ${hasPerm("orders.cancel") && o.cancelledAt ? `<button class="btn" id="od-restore">취소 복구</button>` : ""}
      <button class="btn btn-ghost" id="od-history" title="같은 고객의 다른 주문·A/S를 찾습니다">👤 이 고객의 다른 주문</button>
    </div>
    <div id="od-history-panel"></div>
  </div>`;
  $("#od-close").addEventListener("click", () => { state.orderDetailId = undefined; host.innerHTML = ""; });
  $("#od-history").addEventListener("click", () => renderCustomerHistory(o));
  if (canEdit) attachGoodsLookup({ code: "od-code", name: "od-product", option: "od-option",
                                   amount: "od-amount", qty: "od-qty" });

  const patch = async (body) => {
    try {
      const updated = await api(`/api/orders/${o.id}`, { method: "PATCH", body });
      state.orderDetailId = updated.id;
      renderOrderDetail();
      $("#of-search") && $("#of-search").click();
      return updated;
    } catch (err) {
      toast(err.message, true);
      if (err.data && err.data.order) renderOrderDetail();
      throw err;
    }
  };
  const saveBtn = $("#od-save");
  // 저장 버튼은 하나다 — 상세와 정산을 함께 저장한다.
  // (전에는 큰 [저장]이 정산을 버리고, [정산 저장]이 상세를 버려서 값이 사라진 것처럼 보였다)
  if (saveBtn) saveBtn.addEventListener("click", async () => {
    const settle = canEdit ? {
      feeAmount: $("#od-fee").value.replaceAll(",", "") || 0,
      shippingCost: $("#od-shipcost").value.replaceAll(",", "") || 0,
      refundAmount: $("#od-refund").value.replaceAll(",", "") || 0,
      refundReason: $("#od-refundreason").value,
    } : null;
    const changedSettle = settle && (
      Number(settle.feeAmount) !== (o.feeAmount || 0)
      || Number(settle.shippingCost) !== (o.shippingCost || 0)
      || Number(settle.refundAmount) !== (o.refundAmount || 0)
      || settle.refundReason !== (o.refundReason || ""));
    try {
      // ★상세와 정산을 '한 번의 요청'으로 보낸다.
      //   두 번 나눠 보내면 앞의 저장이 수정시각을 바꿔서, 뒤의 저장이
      //   '다른 사용자가 먼저 수정했습니다'로 막히고 고친 내용이 사라졌다.
      await patchDetails(changedSettle ? settle : null);
      toast(changedSettle ? "저장했습니다(정산 포함)." : "저장했습니다.");
    } catch (err) { /* 오류 안내는 patch()가 이미 띄운다 — 여기서 또 띄우면 두 번 뜬다 */ }
  });

  const patchDetails = (settlement) => patch({
    action: "details", expectedUpdatedAt: o.updatedAt,
    ...(settlement ? { settlement } : {}),
    fields: {
      recipient: $("#od-recipient").value, phone: $("#od-phone").value,
      postalCode: $("#od-zip").value, address: $("#od-addr").value,
      deliveryMessage: $("#od-msg").value, productName: $("#od-product").value,
      optionName: $("#od-option").value, productCode: $("#od-code").value,
      receiveMethod: ($("#od-recv") || {}).value || "",
      quantity: $("#od-qty").value, amount: $("#od-amount").value.replaceAll(",", ""),
      categoryId: $("#od-cat").value || null, memo: $("#od-memo").value,
      // ★서버는 원래 받고 있었는데 화면에 칸이 없어 못 고쳤다(2026-07-31 감사).
      //   전화·방문 주문을 다음 날 입력하면 주문일이 굳어 기간 조회가 어긋난다.
      orderedAt: $("#od-ordered") ? $("#od-ordered").value.trim() : undefined,
      orderNo: $("#od-orderno") ? $("#od-orderno").value.trim() : undefined,
      channel: $("#od-channel") ? $("#od-channel").value.trim() : undefined,
    },
  });
  const cancelBtn = $("#od-cancel");
  if (cancelBtn) cancelBtn.addEventListener("click", () => {
    // ★출고 확인·송장 발행 뒤에도 취소할 수 있다(2026-09-03 대표) — 취소하면 매칭 자산이
    //   자동으로 빠진다. 다만 '아직 안 움직인 송장'이면 서버가 되물어 온다(기사 헛걸음 방지).
    const reason = prompt("취소 사유를 입력하세요.");
    if (!reason) return;
    const run = (force) => patch({ action: "cancel", reason, ...(force ? { force: true } : {}) })
      .then(() => toast("주문을 취소했습니다 — 매칭된 자산은 재고로 돌아갑니다."))
      .catch((err) => {
        if (err.data && err.data.code === "waybill_open"
            && confirm(`${err.message}\n\n그래도 이 주문을 취소할까요?\n`
                       + "(송장은 배송/송장 화면에서 따로 취소해야 기사가 오지 않습니다)")) {
          run(true);
        }
      });
    run(false);
  });
  const restoreBtn = $("#od-restore");
  if (restoreBtn) restoreBtn.addEventListener("click", () =>
    patch({ action: "restoreCancel" }).then(() => toast("취소를 복구했습니다.")).catch(() => {}));
  attachAddrSearch($("#od-zip-find"), { zip: "#od-zip", addr: "#od-addr" });
  const applyBtn = $("#od-applypu");
  if (applyBtn) applyBtn.addEventListener("click", () =>
    patch({ action: "applyShippingUpdate" }).then(() => toast("배송지 변경을 반영했습니다.")).catch(() => {}));
  const dismissBtn = $("#od-dismisspu");
  if (dismissBtn) dismissBtn.addEventListener("click", async () => {
    // ★되돌릴 수 없는 삭제다 — 고객이 바꾼 주소를 다시 확인할 방법이 없어진다.
    //   예전엔 확인창도 성공 안내도 없이 바로 지워졌다(2026-07-29 전수조사).
    if (!confirm("몰에서 들어온 배송지 변경을 버립니다.\n되돌릴 수 없습니다 — 계속할까요?")) return;
    try {
      await patch({ action: "dismissShippingUpdate" });
      toast("배송지 변경을 무시했습니다.");
    } catch (_e) { /* 오류 안내는 patch()가 띄운다 */ }
  });
}
