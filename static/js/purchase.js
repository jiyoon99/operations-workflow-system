/* HMS 매입 — 재고현황 / 자산목록·상세(스펙·이력) / 가입고·매입 전표 / 거래처 / TMS 이관
   TMS 구조 반영(2026-07-28): 가입고(V)→입고확인→매입(P), 스펙 7종, 등급·상태 분리 */
"use strict";

const ASSET_STATUS_CHIP = {
  in_stock: "chip-slate", refurbishing: "chip-blue", repair: "chip-blue", as: "chip-blue",
  painting: "chip-blue", defective: "chip-red", ready: "chip-green",
  reserved: "chip-violet", shipped: "chip-slate", returning: "chip-red", scrapped: "chip-red",
};
const SPEC_LABELS = {
  cpu: "CPU", gpu: "그래픽", ram: "RAM", ssd: "SSD",
  inch: "인치", battery: "배터리효율", charger: "충전기유무",
};

function fmtWon(n) { return (n == null ? 0 : n).toLocaleString("ko-KR") + "원"; }

function statusLabel(code) {
  const s = (state.meta && state.meta.statuses || []).find((x) => x.code === code);
  return s ? s.label : code;
}
function statusChip(code) {
  return `<span class="chip ${ASSET_STATUS_CHIP[code] || "chip-slate"}">${escapeHtml(statusLabel(code))}</span>`;
}
function specSummary(a) {
  return ["cpu", "ram", "ssd"].map((k) => a[k]).filter(Boolean).join(" / ");
}

async function ensureMeta() {
  if (!state.meta) state.meta = await api("/api/purchase-meta");
  if (!state.categories || !state.categories.length) state.categories = await api("/api/categories");
  return state.meta;
}

/* 전표 매입금액 ↔ 자산 매입가 합계 대조.
   TMS는 이 둘이 맞아야 정산이 맞는다. 지금까지는 흐린 글씨로만 적혀 있어
   담당자가 금액을 잘못 넣어도 아무도 모르고 지나갔다(2026-07-30 E2E 확인).
   자동으로 맞추지는 않는다 — 어느 쪽이 맞는지는 사람이 판단해야 한다. */
function amountGap(s) {
  const total = Number(s.totalAmount) || 0;
  const assigned = Number(s.assignedAmount) || 0;
  const n = Number(s.assetCount) || 0;
  if (!n) return null;                       // 자산을 아직 안 붙였으면 비교할 게 없다
  // ★'아직 안 적은 것'과 '틀린 것'은 다르다. 전표 금액이 비어 있으면 경고하지 않는다
  //   (이관 전표는 TMS 엑셀에 총액 칸이 없어 0으로 들어온다).
  if (!total) return null;
  const diff = assigned - total;
  return diff === 0 ? null : { diff, total, assigned };
}

function amountGapChip(s, big) {
  const g = amountGap(s);
  if (!g) return "";
  const over = g.diff > 0;
  return `<span class="chip chip-red" style="white-space:normal;${big ? "" : " font-size:11px;"}"
    title="전표 매입금액 ${fmtWon(g.total)} / 자산 매입가 합계 ${fmtWon(g.assigned)}">
    ⚠ ${over ? "자산 합계가" : "전표 금액이"} ${fmtWon(Math.abs(g.diff))} ${over ? "더 큼" : "더 큼"}</span>`;
}

/* 모델명을 치는 동안 그 모델의 '지금 상태'를 옆에 띄운다.
   대표 요청(2026-07-30): 매입하면서 재고를 대조하려고 탭을 오가는 게 번거롭다.
   → 화면을 옮기는 대신 필요한 답(재고·등급·최근 단가)을 그 자리로 가져온다.
   입력 중 매번 부르지 않도록 350ms 쉬었다 부르고, 같은 모델은 다시 안 묻는다. */
let _briefTimer = null;
let _briefLast = "";

function renderModelBrief(host, b) {
  if (!host) return;
  if (!b || b.reason) { host.innerHTML = ""; return; }
  const grades = (b.byGrade || []).slice(0, 4)
    .map((g) => `${escapeHtml(g.grade)} ${g.count}`).join(" · ");
  const recent = (b.recentBuys || []).slice(0, 3).map((r) => `
    <div style="display:flex; gap:8px; font-size:12px; padding:2px 0;">
      <span class="muted" style="min-width:74px;">${escapeHtml(r.date || "")}</span>
      <span style="flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;">${escapeHtml(r.supplier || "-")}</span>
      <b>${fmtWon(r.price)}</b>
    </div>`).join("") || `<div class="muted" style="font-size:12px;">최근 매입 기록 없음</div>`;
  host.innerHTML = `
    <div style="border:1px solid var(--border); border-radius:10px; padding:10px 12px;
                background:var(--bg); margin-top:6px;">
      <div class="inline-row" style="gap:6px; flex-wrap:wrap;">
        <b style="font-size:13px;">${escapeHtml(b.model)}</b>
        <span class="chip ${b.stock > 0 ? "chip-green" : "chip-slate"}">재고 ${b.stock}대</span>
        ${b.pending ? `<span class="chip chip-violet" title="가입고로 등록됐지만 아직 입고확인 전">입고대기 ${b.pending}대</span>` : ""}
        ${b.avgBuy ? `<span class="chip chip-slate">최근 평균 ${fmtWon(b.avgBuy)}</span>` : ""}
      </div>
      ${grades ? `<div class="muted" style="font-size:12px; margin-top:4px;">등급: ${grades}</div>` : ""}
      <div style="margin-top:6px; border-top:1px dashed var(--border); padding-top:4px;">
        <div class="muted" style="font-size:11px; margin-bottom:2px;">최근 매입</div>
        ${recent}
      </div>
    </div>`;
}

function watchModelBrief(inputId, makerId, hostId) {
  const el = $(inputId);
  const host = $(hostId);
  if (!el || !host) return;
  const run = () => {
    const model = (el.value || "").trim();
    const maker = makerId && $(makerId) ? $(makerId).value.trim() : "";
    if (model.length < 2) { host.innerHTML = ""; _briefLast = ""; return; }
    const key = model + "|" + maker;
    if (key === _briefLast) return;            // 같은 모델은 다시 묻지 않는다
    _briefLast = key;
    api(`/api/assets/model-brief?model=${encodeURIComponent(model)}&maker=${encodeURIComponent(maker)}`)
      .then((b) => renderModelBrief(host, b))
      .catch(() => { host.innerHTML = ""; });   // 실패해도 입력을 막지 않는다
  };
  el.addEventListener("input", () => {
    clearTimeout(_briefTimer);
    _briefTimer = setTimeout(run, 350);
  });
  el.addEventListener("blur", run);
  if ((el.value || "").trim().length >= 2) run();
}

function renderPurchaseView(main) {
  if (!hasPerm("purchase.view")) {
    main.innerHTML = `<h1 class="page-title">매입</h1><div class="card placeholder"><p>매입/자산 조회 권한이 없습니다.</p></div>`;
    return;
  }
  if (!state.purchaseTab) state.purchaseTab = "slips";   // 들어오면 바로 매입 작업
  // ★화면에 새로 들어올 때는 열려 있던 상세를 닫는다.
  //   안 지우면 마지막에 등록한 전표가 매번 팝업으로 다시 뜬다(2026-08-04 대표 지적).
  state.slipDetailId = undefined;
  state.assetDetailId = undefined;
  // 자산 상세에서 '이 전표 보기'로 넘어왔으면 그 전표를 연 채로 시작한다
  if (state.openSlipId) { state.slipDetailId = state.openSlipId; state.openSlipId = undefined; }
  const canEdit = hasPerm("purchase.edit");
  // ★탭을 4개로 묶었다(2026-08-04 대표: "TMS가 쓸데없이 여러 탭으로 나눠둬서 못 쓰겠다,
  //   최대한 간소화해서 필요한 데이터만 추려 쓰고 싶다").
  //   자산을 '보고 고치는' 화면 4개(재고 집계·자산 목록·제품코드·가재고 전환)는
  //   결국 같은 자산을 다르게 보는 것이라 [자산] 하나에 보기 전환으로 넣었고,
  //   어쩌다 쓰는 거래처/분류·TMS 이관은 [기준정보]로 묶었다.
  //   ★판매 전표는 설정 › 매출/실적으로 옮겼다 — 매입 화면에 매출이 있는 게 어색했다.
  const tabs = [["slips", "매입 작업"], ["assets", "자산"]];
  if (canEdit) tabs.push(["base", "기준정보"]);
  // 옛 탭 이름으로 들어오면(북마크·이전 상태) 새 구조로 옮겨 준다
  const MOVED = { summary: ["assets", "summary"], uncoded: ["assets", "uncoded"],
                  convert: ["assets", "convert"], suppliers: ["base", "suppliers"],
                  migrate: ["base", "migrate"] };
  if (MOVED[state.purchaseTab]) {
    const [tab, view] = MOVED[state.purchaseTab];
    state.purchaseTab = tab;
    if (tab === "assets") state.assetView = view; else state.baseView = view;
  }
  // 판매 전표를 찾아 들어오면 설정의 새 자리로 보낸다
  if (state.purchaseTab === "saleslips") {
    state.purchaseTab = "slips";          // 다음에 매입으로 올 때를 위해 되돌려 둔다
    state.settingsTab = "sales";
    state.salesView = "slips";
    return go("settings");
  }
  if (!tabs.some(([k]) => k === state.purchaseTab)) state.purchaseTab = "slips";
  main.innerHTML = `
    <h1 class="page-title">매입</h1>
    <p class="page-desc">관리번호(YYMMDD-NNNN) 기준 매입·재고·이력 관리</p>
    <div class="tabs">${tabs.map(([k, l]) =>
      `<button data-ptab="${k}" class="${k === state.purchaseTab ? "active" : ""}">${l}</button>`).join("")}</div>
    <div id="ptab-body"></div>`;
  $$("button[data-ptab]", main).forEach((b) => b.addEventListener("click", () => {
    state.purchaseTab = b.dataset.ptab;
    state.assetDetailId = undefined;
    state.slipDetailId = undefined;
    ++state.renderSeq;
    renderPurchaseView(main);
  }));
  const body = $("#ptab-body");
  ({ slips: renderSlips, assets: renderAssetsTab,
     base: renderBaseTab }[state.purchaseTab])(body);
}

/* 자산 탭 — 같은 자산을 네 가지로 본다. 탭을 늘리지 않고 보기만 바꾼다.
   ★'제품코드 없음'·'가재고'는 그냥 보기가 아니라 '치워야 할 일'이라
     남은 대수를 배지로 띄워 준다. 안 보이면 영영 안 치운다. */
const ASSET_VIEWS = [
  ["summary", "재고 집계", ""],
  ["list", "자산 목록", ""],
  ["uncoded", "🏷 제품코드 없음", "uncoded"],
  ["convert", "🔧 실재고 / 가재고", "convert"],
];

async function renderAssetsTab(body) {
  if (!ASSET_VIEWS.some(([k]) => k === state.assetView)) state.assetView = "summary";
  const canEdit = hasPerm("purchase.edit");
  const views = ASSET_VIEWS.filter(([k]) => canEdit || (k !== "uncoded" && k !== "convert"));
  body.innerHTML = `
    <div class="subtabs">${views.map(([k, l, badge]) =>
      `<button data-aview="${k}" class="${k === state.assetView ? "active" : ""}">${l}${
        badge ? `<span class="subtab-badge" data-badge="${badge}"></span>` : ""}</button>`).join("")}</div>
    <div id="aview-body"></div>`;
  $$("button[data-aview]", body).forEach((b) => b.addEventListener("click", () => {
    state.assetView = b.dataset.aview;
    state.assetDetailId = undefined;
    ++state.renderSeq;
    renderAssetsTab(body);
  }));
  const host = $("#aview-body", body);
  ({ summary: renderStockSummary, list: renderAssetList,
     uncoded: renderUncoded, convert: renderConvert }[state.assetView])(host);
  if (canEdit) fillAssetBadges(body);
}

/* 배지 숫자는 화면을 막지 않는다 — 늦게 와도 그때 붙인다 */
async function fillAssetBadges(root) {
  const put = (kind, n) => {
    const el = $(`[data-badge="${kind}"]`, root);
    if (!el) return;
    el.textContent = n ? n.toLocaleString("ko-KR") : "";
    el.style.display = n ? "" : "none";
  };
  try {
    const [u, c] = await Promise.all([
      api("/api/assets/uncoded").catch(() => null),
      api("/api/assets/to-convert").catch(() => null),
    ]);
    if (u) put("uncoded", u.total || 0);
    // ★가재고만 센다. 실재고는 출고가 되는 정상 재고라, total(1,160)을 빨간 배지로 띄우면
    //   '1,160대가 묶여 있다'로 읽히고 그 숫자는 0이 되지 않아 배지를 무시하게 된다.
    if (c) put("convert", (c.byTier && c.byTier["가재고"]) || 0);
  } catch { /* 배지는 없어도 화면은 돈다 */ }
}

/* 기준정보 탭 — 거래처/분류 + TMS 이관. 둘 다 어쩌다 쓰는 관리성 화면이다. */
const BASE_VIEWS = [["suppliers", "거래처 / 분류"], ["migrate", "TMS 이관 · 자동반영"]];

function renderBaseTab(body) {
  if (!BASE_VIEWS.some(([k]) => k === state.baseView)) state.baseView = "suppliers";
  body.innerHTML = `
    <div class="subtabs">${BASE_VIEWS.map(([k, l]) =>
      `<button data-bview="${k}" class="${k === state.baseView ? "active" : ""}">${l}</button>`).join("")}</div>
    <div id="bview-body"></div>`;
  $$("button[data-bview]", body).forEach((b) => b.addEventListener("click", () => {
    state.baseView = b.dataset.bview;
    ++state.renderSeq;
    renderBaseTab(body);
  }));
  ({ suppliers: renderSuppliers, migrate: renderMigrate }[state.baseView])($("#bview-body", body));
}

/* ---------------- 재고 현황 ---------------- */

async function renderStockSummary(body) {
  const seq = ++state.renderSeq;
  body.innerHTML = `<p class="muted">불러오는 중…</p>`;
  let summary, stock, dups;
  try {
    await ensureMeta();
    [summary, stock, dups] = await Promise.all([
      api("/api/assets/summary"), api("/api/assets/stock-by-model"),
      api("/api/assets/duplicates").catch(() => ({ groups: [], count: 0 }))]);
    if (seq !== state.renderSeq) return;
  } catch (err) {
    if (seq === state.renderSeq) body.innerHTML = `<p class="muted">${escapeHtml(err.message)}</p>`;
    return;
  }
  const statuses = state.meta.statuses.map((s) => s.code);
  // ★재고에서 빠진 것 — 취소·반품도 우리 물건이 아니다.
  //   빠뜨리면 같은 화면 위쪽 KPI는 '보유 0대'인데 아래 등급 분포는 'AA 2 · SA 3'이 되고,
  //   취소분이 '누적 출고'로 둔갑해 팔린 적 없는 모델에 품절 경고가 뜬다(2026-07-31 감사).
  const gone = ["shipped", "scrapped", "cancelled", "returned"];
  const catMap = new Map();
  for (const row of summary) {
    const key = row.categoryId == null ? "미지정" : (row.categoryName || `#${row.categoryId}`);
    if (!catMap.has(key)) catMap.set(key, { total: 0, byStatus: {}, byGrade: {} });
    const c = catMap.get(key);
    c.total += row.count;
    c.byStatus[row.status] = (c.byStatus[row.status] || 0) + row.count;
    if (!gone.includes(row.status)) c.byGrade[row.grade] = (c.byGrade[row.grade] || 0) + row.count;
  }
  const total = [...catMap.values()].reduce((s, c) => s + c.total, 0);
  const holding = [...catMap.values()].reduce((s, c) =>
    s + statuses.filter((x) => !gone.includes(x)).reduce((t, x) => t + (c.byStatus[x] || 0), 0), 0);
  const inWork = [...catMap.values()].reduce((s, c) =>
    s + ["refurbishing", "repair", "as", "painting"].reduce((t, x) => t + (c.byStatus[x] || 0), 0), 0);
  const used = statuses.filter((s) => [...catMap.values()].some((c) => c.byStatus[s]));
  const t = stock.totals;
  body.innerHTML = `
    <div class="kpi-row">
      <div class="kpi"><div class="kpi-label">지금 팔 수 있는 재고</div>
        <div class="kpi-value" style="color:var(--primary);">${t.ready}대</div>
        <div class="kpi-sub">제품 ${t.products}종</div></div>
      <div class="kpi"><div class="kpi-label">작업 중 (입고·정비·수리·도색)</div>
        <div class="kpi-value">${t.working}대</div>
        <div class="kpi-sub">손보면 판매 가능</div></div>
      <div class="kpi"><div class="kpi-label">보류 (주문매칭·A/S·회수·불량)</div>
        <div class="kpi-value">${t.held}대</div>
        ${t.lowStock ? `<div class="kpi-sub" style="color:var(--danger);">재고 부족 ${t.lowStock}종 · 품절 ${t.soldOut}종</div>`
          : t.soldOut ? `<div class="kpi-sub">품절 ${t.soldOut}종</div>` : ""}</div>
      <div class="kpi"><div class="kpi-label">보유 재고 자산가치</div>
        <div class="kpi-value">${fmtWon(t.stockValue)}</div>
        <div class="kpi-sub">보유 ${t.stock}대 · 매입가 기준</div></div>
    </div>
    ${dupCardHtml(dups)}
    ${stockTableHtml(stock)}
    <div class="card"><h3>카테고리 × 상태</h3>
      <div class="table-wrap"><table>
        <thead><tr><th>카테고리</th>${used.map((s) => `<th>${escapeHtml(statusLabel(s))}</th>`).join("")}<th>합계</th></tr></thead>
        <tbody>${[...catMap.entries()].map(([name, c]) => `
          <tr><td><b>${escapeHtml(name)}</b></td>
          ${used.map((s) => `<td>${c.byStatus[s] || ""}</td>`).join("")}
          <td><b>${c.total}</b></td></tr>`).join("") || `<tr><td colspan="${used.length + 2}" class="muted">등록된 자산이 없습니다.</td></tr>`}
        </tbody></table></div></div>
    <div class="card"><h3>보유 재고 등급 분포</h3>
      ${[...catMap.entries()].map(([name, c]) => {
        const chips = state.meta.grades.filter((g) => c.byGrade[g]).map((g) =>
          `<span class="chip chip-green" style="margin-right:6px;">${escapeHtml(g)} ${c.byGrade[g]}</span>`).join("");
        return chips ? `<p style="margin:6px 0;"><b>${escapeHtml(name)}</b> &nbsp; ${chips}</p>` : "";
      }).join("") || `<p class="muted">보유 재고가 없습니다.</p>`}
    </div>`;
  wireStockTable(body, stock);
  // 열고 닫은 상태를 기억한다 — 자산 하나 고치고 돌아올 때마다 다시 접히면 못 쓴다
  const dupCard = $(".dup-card", body);
  if (dupCard) dupCard.addEventListener("toggle", () => { state.dupOpen = dupCard.open; });
  // 중복 카드의 관리번호를 누르면 자산 목록 탭으로 가서 그 번호를 조회한다
  $$("button[data-asset]", body).forEach((b) => b.addEventListener("click", () => {
    const no = b.textContent.trim();
    state.purchaseTab = "assets";
    state.assetView = "list";        // 집계 화면이 아니라 개별 목록으로 가야 번호가 보인다
    state.assetFilter = { q: no, status: "", categoryId: "", grade: "" };
    state.assetDetailId = Number(b.dataset.asset);
    renderPurchaseView($("#main"));
  }));
}

/* 중복 자산 — 같은 시리얼이 두 번 등록되면 재고와 자산가치가 부풀려진다 */
function dupCardHtml(dups) {
  if (!dups || !dups.count) return "";
  // 40건이 늘 펼쳐져 있으면 매입 화면 첫 화면이 경고표로 덮인다 — 접어 두고 필요할 때 연다.
  const open = state.dupOpen ? " open" : "";
  const dupes = dups.groups.reduce((n, g) => n + g.count, 0);
  return `
    <details class="card dup-card"${open} style="border-left:4px solid var(--danger);">
      <summary><b>⚠ 중복 의심 자산 ${dups.count}건</b>
        <span class="muted"> · 자산 ${dupes}대 · 같은 시리얼이 두 번 이상 등록됨</span></summary>
      <p class="muted" style="margin:8px 0;">실물이 하나면 재고 수와 자산가치가 부풀려집니다 —
      확인 후 잘못 등록된 쪽을 폐기 처리하세요. 관리번호를 누르면 그 자산으로 이동합니다.</p>
      <div class="table-wrap"><table>
        <thead><tr><th>시리얼</th><th>제품</th><th>등록 건수</th><th>관리번호</th></tr></thead>
        <tbody>${dups.groups.map((g) => `
          <tr><td><b>${escapeHtml(g.serial)}</b></td>
            <td>${escapeHtml([g.maker, g.model].filter(Boolean).join(" ") || "-")}</td>
            <td>${g.count}대</td>
            <td>${g.assets.map((a) =>
              `<button class="btn btn-sm" data-asset="${a.id}" title="${escapeHtml(a.statusLabel)}">${escapeHtml(a.assetNo)}</button>`).join(" ")}</td>
          </tr>`).join("")}</tbody>
      </table></div>
    </details>`;
}

/* 제품별 재고 표 — 모델 한 줄, 펼치면 스펙별 내역 */

function stockBar(p) {
  const w = (n) => (p.stock ? Math.round((n / p.stock) * 100) : 0);
  if (!p.stock) return `<span class="muted">-</span>`;
  return `<div style="display:flex; height:8px; border-radius:4px; overflow:hidden; background:var(--border); min-width:90px;"
       title="판매가능 ${p.ready} · 작업중 ${p.working} · 보류 ${p.held}">
      <div style="width:${w(p.ready)}%; background:var(--primary);"></div>
      <div style="width:${w(p.working)}%; background:var(--blue);"></div>
      <div style="width:${w(p.held)}%; background:var(--text-dim);"></div>
    </div>`;
}

function stockRowHtml(p, i) {
  const out = p.stock === 0;
  const gradeChips = p.gradeList.slice(0, 3).map(([g, n]) =>
    `<span class="chip" style="margin-right:4px;">${escapeHtml(g)} ${n}</span>`).join("");
  return `
    <tr data-sp="${i}" style="cursor:pointer;${out ? " opacity:.62;" : ""}">
      <td><span class="sp-caret" style="display:inline-block; width:12px;">▸</span>
          <b>${escapeHtml(p.product)}</b>
          ${out ? `<span class="chip chip-red" style="margin-left:6px;">재고 없음</span>`
            : p.lowStock ? `<span class="chip chip-red" style="margin-left:6px;">부족</span>` : ""}
          <div class="muted" style="font-size:12px;">${escapeHtml(p.categoryName)}${p.gone ? ` · 누적 출고 ${p.gone}대` : ""}</div></td>
      <td style="text-align:right;"><b style="color:${p.ready ? "var(--primary)" : "var(--text-dim)"}; font-size:15px;">${p.ready}</b></td>
      <td style="text-align:right;">${p.working || ""}</td>
      <td style="text-align:right;">${p.held || ""}</td>
      <td style="text-align:right;"><b>${p.stock}</b></td>
      <td>${stockBar(p)}</td>
      <td>${gradeChips || `<span class="muted">-</span>`}</td>
      <td style="text-align:right;">${p.avgBuy ? fmtWon(p.avgBuy) : "-"}
        ${p.buyMissing ? `<div class="muted" style="font-size:11px; white-space:nowrap;"
          title="매입가가 비어 있는 자산은 평균에서 제외했습니다(주로 TMS 이관분)">미입력 ${p.buyMissing}대</div>` : ""}</td>
      <td style="text-align:right;">${p.avgSale ? fmtWon(p.avgSale) : "-"}</td>
    </tr>
    <tr class="sp-detail" data-spd="${i}" style="display:none;">
      <td colspan="9" style="background:var(--bg); padding:0;">
        <table style="margin:0;"><tbody>
        ${p.variants.map((v) => `
          <tr><td style="padding-left:32px;">${escapeHtml(v.spec)}</td>
            <td style="text-align:right; width:70px;">${v.ready || ""}</td>
            <td style="text-align:right; width:70px;">${v.working || ""}</td>
            <td style="text-align:right; width:70px;">${v.held || ""}</td>
            <td style="text-align:right; width:70px;"><b>${v.stock}</b></td>
            <td style="width:100px;"></td><td></td>
            <td style="width:110px;"></td><td style="width:110px;"></td></tr>`).join("")}
        </tbody></table>
      </td>
    </tr>`;
}

function stockTableHtml(stock) {
  const f = state.stockFilter || (state.stockFilter = { q: "", only: "all" });
  const q = f.q.trim().toLowerCase();
  let list = stock.products;
  if (q) list = list.filter((p) => p.product.toLowerCase().includes(q));
  if (f.only === "ready") list = list.filter((p) => p.ready > 0);
  else if (f.only === "out") list = list.filter((p) => p.stock === 0);
  else if (f.only === "working") list = list.filter((p) => p.working > 0);
  const tabs = [["all", "전체"], ["ready", "판매가능만"], ["working", "작업중"], ["out", "재고 없음"]];
  const pgP = paged(list, "stock");
  return `
    <div class="card">
      <div class="inline-row">
        <h3 style="margin:0; flex:1;">제품별 재고</h3>
        <input type="text" id="sp-q" placeholder="제품명 검색" value="${escapeHtml(f.q)}" style="min-width:180px;">
        ${tabs.map(([k, l]) => `<button class="btn btn-sm ${f.only === k ? "btn-primary" : ""}" data-sponly="${k}">${l}</button>`).join("")}
        <span class="muted">${list.length}종</span>
      </div>
      <div class="table-wrap"><table>
        <thead><tr>
          <th>제품</th><th style="text-align:right;">판매가능</th><th style="text-align:right;">작업중</th>
          <th style="text-align:right;">보류</th><th style="text-align:right;">보유</th>
          <th>구성</th><th>등급</th><th style="text-align:right;">평균 매입가</th><th style="text-align:right;">평균 판매가</th>
        </tr></thead>
        <tbody>${pgP.rows.map((p) => stockRowHtml(p, stock.products.indexOf(p))).join("")
          || `<tr><td colspan="9" class="muted">해당하는 제품이 없습니다.</td></tr>`}</tbody>
      </table></div>
      ${pgP.bar}
    </div>`;
}

function wireStockTable(body, stock) {
  const f = state.stockFilter;
  const redraw = () => {
    const card = $("#sp-q")?.closest(".card");
    if (!card) return;
    card.outerHTML = stockTableHtml(stock);
    wireStockTable(body, stock);
    $("#sp-q")?.focus();
  };
  $("#sp-q", body)?.addEventListener("input", (e) => {
    f.q = e.target.value;
    resetPage("stock");                 // 검색하면 1쪽부터
    clearTimeout(state.stockTimer);
    state.stockTimer = setTimeout(redraw, 250);
  });
  $$("button[data-sponly]", body).forEach((b) => b.addEventListener("click", () => {
    f.only = b.dataset.sponly; resetPage("stock"); redraw();
  }));
  wirePager(body, "stock", redraw);
  $$("tr[data-sp]", body).forEach((tr) => tr.addEventListener("click", () => {
    const d = $(`tr[data-spd="${tr.dataset.sp}"]`, body);
    if (!d) return;
    const open = d.style.display !== "none";
    d.style.display = open ? "none" : "table-row";
    const caret = $(".sp-caret", tr);
    if (caret) caret.textContent = open ? "▸" : "▾";
  }));
}

/* ---------------- 자산 여러 대 선택 → 일괄 변경 ---------------- */

function wireAssetPicks(body) {
  const rows = $$(".af-pick", body);
  rows.forEach((cb) => cb.addEventListener("click", (e) => {
    const id = Number(cb.dataset.pick);
    if (e.shiftKey && state.assetLastPick != null) {     // Shift로 범위 선택
      const ids = rows.map((x) => Number(x.dataset.pick));
      const a = ids.indexOf(state.assetLastPick), b = ids.indexOf(id);
      if (a >= 0 && b >= 0) {
        for (let i = Math.min(a, b); i <= Math.max(a, b); i++) {
          cb.checked ? state.assetPicked.add(ids[i]) : state.assetPicked.delete(ids[i]);
          rows[i].checked = cb.checked;
        }
      }
    } else {
      cb.checked ? state.assetPicked.add(id) : state.assetPicked.delete(id);
    }
    state.assetLastPick = id;
    renderAssetBulkBar(body);
  }));
  const all = $("#af-all", body);
  if (all) {
    all.checked = rows.length > 0 && rows.every((cb) => cb.checked);
    all.onclick = () => {
      rows.forEach((cb) => {
        cb.checked = all.checked;
        all.checked ? state.assetPicked.add(Number(cb.dataset.pick))
                    : state.assetPicked.delete(Number(cb.dataset.pick));
      });
      renderAssetBulkBar(body);
    };
  }
  renderAssetBulkBar(body);
}

function renderAssetBulkBar(body) {
  const bar = $("#af-bulkbar", body);
  if (!bar) return;
  const n = state.assetPicked.size;
  if (!n || !hasPerm("purchase.edit")) { bar.style.display = "none"; bar.innerHTML = ""; return; }
  bar.style.display = "block";
  bar.innerHTML = `
    <div class="inline-row" style="background:var(--primary-soft); border-radius:8px; padding:10px 12px; margin:8px 0;">
      <b>${n}대 선택됨</b>
      <select id="ab-status"><option value="">상태 그대로</option>
        ${(state.meta.manualStatuses || []).map((s) =>
          `<option value="${s}">${escapeHtml(statusLabel(s))}</option>`).join("")}</select>
      <select id="ab-grade"><option value="">등급 그대로</option>
        ${state.meta.grades.map((g) => `<option>${escapeHtml(g)}</option>`).join("")}</select>
      <input type="text" id="ab-location" placeholder="보관위치(비우면 그대로)" style="min-width:150px;">
      <button class="btn btn-sm btn-primary" id="ab-apply">선택 ${n}대에 적용</button>
      <span style="flex:1"></span>
      <button class="btn btn-ghost btn-sm" id="ab-clear">선택 해제</button>
    </div>`;
  $("#ab-clear", bar).addEventListener("click", () => {
    state.assetPicked.clear();
    $$(".af-pick", body).forEach((cb) => { cb.checked = false; });
    renderAssetBulkBar(body);
  });
  $("#ab-apply", bar).addEventListener("click", async () => {
    const payload = { ids: [...state.assetPicked] };
    const st = $("#ab-status").value, gr = $("#ab-grade").value, loc = $("#ab-location").value.trim();
    if (st) payload.status = st;
    if (gr) payload.grade = gr;
    if (loc) payload.location = loc;
    if (!st && !gr && !loc) { toast("바꿀 항목을 하나 이상 고르세요.", true); return; }
    if (!confirm(`선택한 ${payload.ids.length}대를 바꿉니다. 계속할까요?`)) return;
    try {
      const r = await api("/api/assets/bulk", { method: "POST", body: payload });
      toast(`${r.ok}대 변경${r.failed.length ? ` · ${r.failed.length}대 실패` : ""}`);
      state.assetPicked = new Set(r.failed.map((x) => x.id));
      state.assetBulkResult = { ok: r.ok, failed: r.failed };   // 목록 재렌더 후에도 남긴다
      renderAssetList(body);
    } catch (err) { toast(err.message, true); }
  });
}

/* ---------------- 자산 목록 + 상세 ---------------- */

async function renderAssetList(body) {
  const seq = ++state.renderSeq;
  const f = state.assetFilter || (state.assetFilter = { q: "", status: "", categoryId: "", grade: "" });
  if (!state.assetPicked) state.assetPicked = new Set();
  let assets;
  try {
    await ensureMeta();
    const params = new URLSearchParams();
    ["q", "status", "categoryId", "grade"].forEach((k) => { if (f[k]) params.set(k, f[k]); });
    assets = await api("/api/assets?" + params.toString());
    if (seq !== state.renderSeq) return;
  } catch (err) {
    if (seq === state.renderSeq) body.innerHTML = `<p class="muted">${escapeHtml(err.message)}</p>`;
    return;
  }
  const pg = paged(assets, "assets");
  body.innerHTML = `
    <div class="card">
      <div class="inline-row">
        <input type="text" id="af-q" placeholder="관리번호/모델/시리얼/CPU/위치" value="${escapeHtml(f.q)}" style="min-width:220px;">
        <select id="af-status"><option value="">전체 상태</option>
          ${state.meta.statuses.map((s) => `<option value="${s.code}" ${f.status === s.code ? "selected" : ""}>${escapeHtml(s.label)}</option>`).join("")}</select>
        <select id="af-grade"><option value="">전체 등급</option>
          ${state.meta.grades.map((g) => `<option ${f.grade === g ? "selected" : ""}>${escapeHtml(g)}</option>`).join("")}</select>
        <select id="af-cat"><option value="">전체 카테고리</option>
          ${state.categories.map((c) => `<option value="${c.id}" ${String(f.categoryId) === String(c.id) ? "selected" : ""}>${escapeHtml(c.name)}</option>`).join("")}</select>
        <button class="btn btn-sm btn-primary" id="af-search">조회</button>
        <span style="flex:1"></span>
        ${hasPerm("purchase.edit") ? `<button class="btn btn-sm btn-primary" id="af-new">＋ 자산 등록</button>` : ""}
        <button class="btn btn-sm" id="af-export">📤 엑셀 내보내기</button>
        <span class="muted">${assets.length.toLocaleString("ko-KR")}건${
          assets.length >= 500 ? " (서버 상한 500 — 조건을 좁혀 주세요)" : ""}</span>
      </div>
      <div id="af-form"></div>
      <div id="af-bulkbar" style="display:none;"></div>
      <div id="ab-result"></div>
      <div class="table-wrap"><table>
        <thead><tr><th style="width:28px;"><input type="checkbox" id="af-all" title="이 목록 전체 선택"></th>
          <th>관리번호</th><th>분류</th><th>브랜드 / 모델</th><th>스펙</th><th>등급</th><th>상태</th><th>위치</th><th>매입가</th><th>판매가</th><th></th></tr></thead>
        <tbody>${pg.rows.map((a) => `
          <tr>
            <td><input type="checkbox" class="af-pick" data-pick="${a.id}" ${state.assetPicked.has(a.id) ? "checked" : ""}></td>
            <td><b>${escapeHtml(a.assetNo)}</b>
              ${a.slipNo && a.batchId ? `<div><button class="link-btn af-slip" data-slip="${a.batchId}"
                title="이 전표의 전체 내용을 봅니다" style="font-size:11.5px;">${escapeHtml(a.slipNo)}</button>${
                  a.slipCancelled ? ` <span class="chip chip-red" style="font-size:10px;">🚫취소</span>`
                  : a.slipReturned ? ` <span class="chip chip-violet" style="font-size:10px;">↩반품</span>` : ""}</div>`
                : `<div class="muted" style="font-size:11.5px;">전표 없음</div>`}</td>
            <td>${escapeHtml(a.categoryName || "-")}</td>
            <td>${escapeHtml([a.maker, a.model].filter(Boolean).join(" ") || "-")}</td>
            <td class="muted" style="max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${escapeHtml(specSummary(a))}">${escapeHtml(specSummary(a) || "-")}</td>
            <td>${escapeHtml(a.grade)}</td>
            <td>${statusChip(a.status)}</td>
            <td>${escapeHtml(a.location || "-")}</td>
            <td>${fmtWon(a.purchasePrice)}</td>
            <td>${a.salePrice ? fmtWon(a.salePrice) : "-"}</td>
            <td><button class="btn btn-sm" data-asset="${a.id}">상세</button></td>
          </tr>`).join("") || `<tr><td colspan="11" class="muted">자산이 없습니다.${
            hasPerm("purchase.edit") ? " 오른쪽 위 [＋ 자산 등록]으로 등록하거나, [가입고 / 매입] 탭에서 전표를 만든 뒤 자산을 추가하세요." : ""}</td></tr>`}
        </tbody></table></div>
      ${pg.bar}
    </div>
    <div id="asset-detail"></div>`;
  wirePager(body, "assets", () => renderAssetList(body));
  // 직전 일괄 처리 결과 — 왜 안 됐는지가 목록 새로고침으로 사라지면 같은 버튼을 반복하게 된다
  const res = state.assetBulkResult;
  if (res) {
    const host = $("#ab-result", body);
    if (host) {
      host.innerHTML = `
        <div style="border:1px solid ${res.failed.length ? "var(--danger)" : "var(--border)"};
                    border-radius:8px; padding:10px 12px; margin:8px 0;">
          <div class="inline-row" style="margin:0;">
            <b style="flex:1;">일괄 변경 — 성공 ${res.ok}대${res.failed.length
              ? ` · <span style="color:var(--danger)">실패 ${res.failed.length}대</span>` : ""}</b>
            <button class="btn btn-ghost btn-sm" id="ab-result-close">닫기</button>
          </div>
          ${res.failed.length ? `<ul class="muted" style="margin:6px 0 0 18px;">${res.failed.map((x) =>
            `<li>${escapeHtml(x.label || `#${x.id}`)} — ${escapeHtml(x.reason)}</li>`).join("")}</ul>` : ""}
        </div>`;
      $("#ab-result-close", body).addEventListener("click", () => {
        state.assetBulkResult = null;
        host.innerHTML = "";
      });
    }
  }
  // 조회 조건이 바뀌면 선택도 현재 목록 기준으로 정리한다(안 보이는 자산 일괄 변경 방지)
  const visibleAssets = new Set(assets.map((a) => a.id));
  const droppedAssets = [...state.assetPicked].filter((id) => !visibleAssets.has(id));
  if (droppedAssets.length) {
    droppedAssets.forEach((id) => state.assetPicked.delete(id));
    toast(`조회 조건이 바뀌어 선택 ${droppedAssets.length}대가 해제되었습니다.`);
  }
  wireAssetPicks(body);
  const doSearch = () => {
    f.q = $("#af-q").value.trim(); f.status = $("#af-status").value;
    f.grade = $("#af-grade").value; f.categoryId = $("#af-cat").value;
    // ★조건이 바뀌면 1쪽으로 — 3쪽을 보다 검색해 2건만 남으면 빈 화면이 된다
    resetPage("assets");
    renderAssetList(body);
  };
  $("#af-search").addEventListener("click", doSearch);
  autoSearch("#af-q", doSearch);
  const newBtn = $("#af-new");
  if (newBtn) newBtn.addEventListener("click", async () => {
    try { state.slipsForPicker = await api("/api/purchase-batches"); }
    catch (_e) { state.slipsForPicker = []; }
    renderAssetForm(null, "#af-form", () => renderAssetList(body));
  });
  $("#af-export").addEventListener("click", () => {
    // 화면에 보이는 조건 그대로 내려받는다(전에는 상태만 반영돼 전체가 나왔다)
    const p = new URLSearchParams();
    ["q", "status", "categoryId", "grade"].forEach((k) => { if (f[k]) p.set(k, f[k]); });
    window.open("/api/assets/export?" + p.toString(), "_blank");
  });
  $$("button[data-asset]", body).forEach((b) => b.addEventListener("click", () => {
    state.assetDetailId = Number(b.dataset.asset);
    renderAssetDetail();
  }));
  // 목록에서 전표번호를 바로 눌러도 그 전표로 간다 — 상세를 안 열어도 된다
  $$("button.af-slip", body).forEach((b) => b.addEventListener("click", (e) => {
    e.stopPropagation();
    openSlipFromAsset(Number(b.dataset.slip));
  }));
  if (state.assetDetailId) renderAssetDetail();
}

/* 자산 상세에서 '이 전표 전체 보기'로 넘어간다.
   ★전표 화면을 새로 만들지 않고 [매입 작업] 탭의 그 화면을 그대로 연다 —
     같은 걸 두 벌로 만들면 한쪽만 고쳐져 서로 다른 말을 하게 된다(대표 지시).
   ★renderPurchaseView가 진입할 때 slipDetailId를 지우므로(팝업 잔류 방지)
     openSlipId에 실어 보내고 거기서 넘겨받는다. */
function openSlipFromAsset(bid) {
  if (!bid) return;
  closeModal();                       // 자산 상세가 팝업이면 먼저 닫는다
  state.openSlipId = bid;
  state.purchaseTab = "slips";
  ++state.renderSeq;
  renderPurchaseView($("#main"));
}

/* 자산 상세 맨 위의 '어느 전표로 들어왔는지' 줄 (대표 요청 2026-08-04).
   ★예전에는 특이사항 아래 흐린 글씨로 묻혀 있어서 눈에 안 띄었다.
     맨 위로 올리고, 누르면 그 전표의 전체를 볼 수 있게 한다. */
function slipBarHtml(b) {
  if (!b) {
    return `<div class="slip-bar slip-bar-none">전표 없음
      <span class="muted">— 이관·수기 등록 자산이라 매입 전표에 묶여 있지 않습니다.</span></div>`;
  }
  // ★취소·반품된 전표를 살아있는 매입으로 보이면 안 된다 — 그 금액으로 원가를 판단한다
  const dead = b.cancelledAt || b.returnedAt;
  const chip = b.cancelledAt
    ? `<span class="chip chip-red">🚫 취소됨${b.cancelReason ? " · " + escapeHtml(b.cancelReason) : ""}</span>`
    : b.returnedAt
      ? `<span class="chip chip-violet">↩ 반품${b.returnReason ? " · " + escapeHtml(b.returnReason) : ""}</span>`
      : b.stage === "provisional"
        ? `<span class="chip chip-blue">가입고</span>` : `<span class="chip chip-green">매입</span>`;
  return `<div class="slip-bar${dead ? " slip-bar-dead" : ""}">
      <span class="muted">매입 전표</span>
      <button class="link-btn slip-open" data-slip="${b.id}"
        title="이 전표의 전체 내용을 봅니다">${escapeHtml(b.slipNo || "(번호 없음)")}</button>
      ${chip}
      <span>${escapeHtml(b.supplierName || "거래처 미지정")}</span>
      <span class="muted">${escapeHtml(b.purchaseDate || "")}</span>
      <span style="flex:1"></span>
      <span class="muted">전표 금액 ${fmtWon(b.totalAmount)}</span>
    </div>`;
}

/* 자산 상세/편집. 자산 목록뿐 아니라 전표 상세에서도 연다(대표 요청 2026-07-31:
   "전표 안 자산번호를 클릭하면 그 자리에서 수정하고 싶다").
     hostId : 그릴 위치(기본 자산 목록의 #asset-detail)
     onSaved: 저장 후 호출 — 부른 화면이 스스로 갱신하도록(전표 금액·재고가 같이 움직인다) */
async function renderAssetDetail(hostId, onSaved) {
  const host = $(hostId || "#asset-detail");
  if (!host || !state.assetDetailId) return;
  const reopen = () => renderAssetDetail(hostId, onSaved);
  host.innerHTML = `<div class="card"><p class="muted">불러오는 중…</p></div>`;
  revealPanel(host);
  let a;
  try { a = await api(`/api/assets/${state.assetDetailId}`); }
  catch (err) { host.innerHTML = `<div class="card"><p class="muted">${escapeHtml(err.message)}</p></div>`; return; }
  const canEdit = hasPerm("purchase.edit");
  const locked = ["reserved", "shipped"].includes(a.status);
  const dis = canEdit ? "" : "disabled";
  host.innerHTML = `
  <div class="card">
    <div class="inline-row">
      <h3 style="margin:0; flex:1;">${escapeHtml(a.assetNo)} ${statusChip(a.status)}</h3>
      <button class="btn btn-sm" id="ad-close">닫기</button>
    </div>
    ${slipBarHtml(a.batch)}
    <div class="form-grid" style="margin-top:8px;">
      <label>관리번호 (TMS 이관 시 수정)<input type="text" id="ad-no" value="${escapeHtml(a.assetNo)}" ${dis}></label>
      <label>카테고리<select id="ad-cat" ${dis}>
        ${state.categories.map((c) => `<option value="${c.id}" ${c.id === a.categoryId ? "selected" : ""}>${escapeHtml(c.name)}</option>`).join("")}
      </select></label>
      <label>브랜드<input type="text" id="ad-maker" value="${escapeHtml(a.maker)}" ${dis}></label>
      <label style="grid-column:1 / -1;">모델명
        <input type="text" id="ad-model" value="${escapeHtml(a.model)}" ${dis}>
        <div id="ad-brief"></div>
      </label>
      <label>시리얼번호<input type="text" id="ad-serial" value="${escapeHtml(a.serial)}" ${dis}></label>
      <label>등급<select id="ad-grade" ${dis}>
        ${state.meta.grades.map((g) => `<option ${g === a.grade ? "selected" : ""}>${escapeHtml(g)}</option>`).join("")}
      </select></label>
      <label>재고 구분
        <select id="ad-tier" ${dis} title="${escapeHtml(tierHelpText())}">
          ${tierOptions(a.tier || "양품")}</select></label>
      <label>상태<select id="ad-status" ${canEdit && !locked ? "" : "disabled"}>
        ${state.meta.statuses.map((s) => {
          const manual = state.meta.manualStatuses.includes(s.code);
          return `<option value="${s.code}" ${s.code === a.status ? "selected" : ""} ${manual || s.code === a.status ? "" : "disabled"}>${escapeHtml(s.label)}</option>`;
        }).join("")}
      </select></label>
      <label>보관위치<input type="text" id="ad-location" value="${escapeHtml(a.location)}" ${dis}></label>
      <label>매입가<input type="text" id="ad-price" value="${a.purchasePrice}" ${dis}></label>
      <label>판매가<input type="text" id="ad-saleprice" value="${a.salePrice}" ${dis}></label>
      <label>제품코드 <span class="muted">(쇼핑몰 재고의 축 — 직접 기입)</span>
        <input type="text" id="ad-productcode" value="${escapeHtml(a.productCode || "")}"
               placeholder="예: 840 G3_i7-6_내장" ${dis}></label>
      <label class="check-line" style="align-items:center;">
        <input type="checkbox" id="ad-stocklisted" ${a.stockListed ? "checked" : ""} ${dis}>
        <span>재고반영 <span class="muted">(체크해야 쇼핑몰 재고에 세어집니다)</span></span>
      </label>
    </div>
    <h3 style="margin-top:16px;">스펙</h3>
    <div class="form-grid">
      ${Object.entries(SPEC_LABELS).map(([k, label]) =>
        `<label>${label}<input type="text" id="ad-${k}" value="${escapeHtml(a[k] || "")}" ${dis}></label>`).join("")}
    </div>
    <label style="display:block; margin-top:10px;" class="muted">특이사항
      <textarea id="ad-notes" rows="2" style="width:100%; margin-top:4px; border:1px solid var(--border); border-radius:8px; background:var(--bg); padding:8px;" ${dis}>${escapeHtml(a.notes)}</textarea>
    </label>
    <p class="muted" style="margin:8px 0;">
      원가 <b>${fmtWon(a.costTotal)}</b> (매입 ${fmtWon(a.purchasePrice)} + 수리 ${fmtWon(a.repairTotal)})
      ${locked ? ' · <span style="color:var(--danger)">주문에 매칭/출고된 자산은 상태를 직접 바꿀 수 없습니다</span>' : ""}
    </p>
    ${canEdit ? `<div class="editor-actions"><button class="btn btn-primary" id="ad-save">저장</button></div>` : ""}
  </div>
  <div class="card">
    <h3>수리 내역 <span class="muted">(합계 ${fmtWon(a.repairTotal)})</span></h3>
    ${canEdit ? `
    <div class="inline-row">
      <input type="date" id="rp-date" value="${ymd()}">
      <input type="text" id="rp-desc" placeholder="수리 내용" style="flex:1; min-width:180px;">
      <input type="text" id="rp-cost" placeholder="비용" style="width:100px;">
      <button class="btn btn-sm btn-primary" id="rp-add">추가</button>
    </div>` : ""}
    <div class="table-wrap"><table>
      <thead><tr><th>일자</th><th>내용</th><th>비용</th><th>등록자</th><th></th></tr></thead>
      <tbody>${a.repairs.map((r) => `
        <tr><td>${escapeHtml(r.repairDate)}</td><td>${escapeHtml(r.description)}</td>
        <td>${fmtWon(r.cost)}</td><td>${escapeHtml(r.createdBy)}</td>
        <td>${canEdit ? `<button class="btn btn-ghost btn-sm" data-delrepair="${r.id}">삭제</button>` : ""}</td></tr>`).join("")
        || `<tr><td colspan="5" class="muted">수리 기록이 없습니다.</td></tr>`}
      </tbody></table></div>
  </div>
  ${a.orders.length ? `<div class="card"><h3>연결 주문</h3>
    <div class="table-wrap"><table>
      <thead><tr><th>주문</th><th>채널</th><th>수취인</th><th>매칭</th><th>상태</th></tr></thead>
      <tbody>${a.orders.map((o) => `
        <tr><td>#${o.orderId} ${escapeHtml(o.orderNo || "")}</td><td>${escapeHtml(o.channel)}</td>
        <td>${escapeHtml(o.recipient)}</td><td class="muted">${escapeHtml(o.matchedBy)} ${escapeHtml((o.matchedAt || "").slice(0, 16).replace("T", " "))}</td>
        <td>${o.shipped ? '<span class="chip chip-green">출고완료</span>' : '<span class="chip chip-blue">진행 중</span>'}</td></tr>`).join("")}
      </tbody></table></div></div>` : ""}
  <div class="card">
    <h3>이력 타임라인</h3>
    <div class="timeline">${a.events.map((e) => `
      <div class="tl-item">
        <div class="tl-time">${escapeHtml(e.ts.replace("T", " ").slice(0, 19))}</div>
        <div class="tl-action"><span class="chip chip-slate">${escapeHtml(e.action)}</span> <b>${escapeHtml(e.actor)}</b></div>
        <div class="tl-detail muted">${escapeHtml(e.detail ? JSON.stringify(e.detail) : "")}</div>
      </div>`).join("") || `<p class="muted">이력이 없습니다.</p>`}
    </div>
  </div>`;

  $("#ad-close").addEventListener("click", () => { state.assetDetailId = undefined; host.innerHTML = ""; });
  attachCodeLookup("ad-productcode");        // 쓰던 코드를 그대로 다시 고르게
  // 전표번호를 누르면 그 전표의 전체를 본다 — [매입 작업] 탭의 전표 상세와 같은 화면이다.
  // ★새 화면을 따로 만들지 않는다(대표 지시: 같은 걸 두 벌로 만들지 마라).
  const slipBtn = $(".slip-open", host);
  if (slipBtn) slipBtn.addEventListener("click", () => openSlipFromAsset(Number(slipBtn.dataset.slip)));
  if (!canEdit) return;
  attachSpecAutocomplete("ad-");   // 자산 상세 수정에도 자동완성
  // 브랜드·모델·스펙 자동완성 — 이미 쓴 값과 카탈로그에서 후보를 띄운다(대표 2026-08-04)
  attachSpecAutocomplete("ad-");
  $("#ad-save").addEventListener("click", async () => {
    const body = {
      assetNo: $("#ad-no").value.trim(), categoryId: Number($("#ad-cat").value),
      maker: $("#ad-maker").value, model: $("#ad-model").value, serial: $("#ad-serial").value,
      grade: $("#ad-grade").value, tier: $("#ad-tier").value, location: $("#ad-location").value,
      purchasePrice: $("#ad-price").value.replaceAll(",", ""),
      salePrice: $("#ad-saleprice").value.replaceAll(",", ""),
      notes: $("#ad-notes").value,
      productCode: $("#ad-productcode").value.trim(),
      stockListed: $("#ad-stocklisted").checked,
    };
    Object.keys(SPEC_LABELS).forEach((k) => { body[k] = $(`#ad-${k}`).value; });
    if (!locked) body.status = $("#ad-status").value;
    try {
      await api(`/api/assets/${a.id}`, { method: "PATCH", body });
      toast("저장했습니다.");
      // ★부른 화면이 스스로 다시 그린다 — 전표 안에서 고쳤으면 전표 금액·자산 목록이,
      //   자산 목록에서 고쳤으면 목록이 즉시 맞춰진다(따로 새로고침할 필요 없이).
      // 자산 목록은 이제 [자산] 탭 안의 보기라 #aview-body에 그려진다
      if (onSaved) onSaved(); else renderAssetList($("#aview-body") || $("#ptab-body"));
      reopen();
    } catch (err) { toast(err.message, true); }
  });
  const rpAdd = $("#rp-add");
  if (rpAdd) rpAdd.addEventListener("click", async () => {
    try {
      await api(`/api/assets/${a.id}/repairs`, { method: "POST", body: {
        repairDate: $("#rp-date").value, description: $("#rp-desc").value.trim(),
        cost: $("#rp-cost").value.replaceAll(",", "") || 0,
      } });
      toast("수리 기록을 추가했습니다.");
      if (onSaved) onSaved();
      reopen();
    } catch (err) { toast(err.message, true); }
  });
  $$("button[data-delrepair]", host).forEach((b) => b.addEventListener("click", async () => {
    if (!confirm("이 수리 기록을 삭제할까요?")) return;
    try {
      await api(`/api/assets/${a.id}/repairs/${b.dataset.delrepair}`, { method: "DELETE" });
      if (onSaved) onSaved();
      reopen();
    } catch (err) { toast(err.message, true); }
  }));
}

/* ---------------- 가입고 / 매입 전표 ---------------- */

async function renderSlips(body) {
  const seq = ++state.renderSeq;
  const f = state.slipFilter || (state.slipFilter = { stage: "", q: "", unpaid: false, cancelled: false });
  let slips, suppliers;
  try {
    await ensureMeta();
    const p = new URLSearchParams();
    if (f.stage) p.set("stage", f.stage);
    if (f.q) p.set("q", f.q);
    if (f.unpaid) p.set("unpaid", "1");
    if (f.cancelled) p.set("includeCancelled", "1");
    [slips, suppliers] = await Promise.all([
      api("/api/purchase-batches?" + p.toString()),
      api("/api/suppliers"),
    ]);
    if (seq !== state.renderSeq) return;
    state.suppliers = suppliers;
  } catch (err) {
    if (seq === state.renderSeq) body.innerHTML = `<p class="muted">${escapeHtml(err.message)}</p>`;
    return;
  }
  const canEdit = hasPerm("purchase.edit");
  const pgSl = paged(slips, "pslips");
  body.innerHTML = `
    <div class="card">
      <div class="inline-row">
        <select id="sf-stage"><option value="">전체</option>
          <option value="provisional" ${f.stage === "provisional" ? "selected" : ""}>가입고</option>
          <option value="purchased" ${f.stage === "purchased" ? "selected" : ""}>매입</option></select>
        <input type="text" id="sf-q" placeholder="전표번호/거래처/송장/메모" value="${escapeHtml(f.q)}" style="min-width:200px;">
        <label class="check-line"><input type="checkbox" id="sf-unpaid" ${f.unpaid ? "checked" : ""}> 미지급만</label>
        <label class="check-line" title="취소한 전표는 기본으로 숨깁니다. 되돌리려면 이걸 켜서 찾으세요.">
          <input type="checkbox" id="sf-cancelled" ${f.cancelled ? "checked" : ""}> 취소 포함</label>
        <button class="btn btn-sm btn-primary" id="sf-search">조회</button>
        <span style="flex:1"></span>
        ${canEdit ? `<button class="btn btn-sm" id="sf-new-v">＋ 가입고 등록</button>
        <button class="btn btn-sm btn-primary" id="sf-new-p">＋ 매입 등록</button>` : ""}
      </div>
      <div id="slip-form"></div>
      <div class="table-wrap"><table>
        <thead><tr><th>전표</th><th>단계</th><th>일자</th><th>거래처</th><th>수량</th><th>매입금액</th><th>납부</th><th>메모</th><th></th></tr></thead>
        <tbody>${pgSl.rows.map((s) => `
          <tr>
            <td><b>${escapeHtml(s.slipNo || "-")}</b>
              ${s.cancelledAt ? `<div><span class="chip chip-red" style="font-size:11px;">🚫 취소</span></div>` : ""}
              ${s.returnedAt ? `<div><span class="chip chip-violet" style="font-size:11px;">↩ 반품</span></div>` : ""}</td>
            <td><span class="chip ${s.stage === "provisional" ? "chip-blue" : "chip-green"}">${escapeHtml(s.stageLabel)}</span></td>
            <td>${escapeHtml(s.purchaseDate)}</td>
            <td>${escapeHtml(s.supplierName || "-")}</td>
            <td>${s.assetCount}대</td>
            <td>${fmtWon(s.totalAmount)}
              ${s.assetCount ? `<div class="muted" style="font-size:11px;">자산합 ${fmtWon(s.assignedAmount)}</div>` : ""}
              ${amountGapChip(s)}</td>
            <td>${s.stage === "purchased" ? (s.paid ? '<span class="chip chip-green">완납</span>' : '<span class="chip chip-red">미지급</span>') : "-"}</td>
            <td class="muted" style="max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${escapeHtml(s.memo)}</td>
            <td><button class="btn btn-sm" data-slip="${s.id}">열기</button></td>
          </tr>`).join("") || `<tr><td colspan="9" class="muted">전표가 없습니다.</td></tr>`}
        </tbody></table></div>
      ${pgSl.bar}
    </div>
    <div id="slip-detail"></div>`;
  const doSearch = () => {
    f.stage = $("#sf-stage").value; f.q = $("#sf-q").value.trim();
    f.unpaid = $("#sf-unpaid").checked; f.cancelled = $("#sf-cancelled").checked;
    resetPage("pslips");
    renderSlips(body);
  };
  wirePager(body, "pslips", () => renderSlips(body));
  $("#sf-search").addEventListener("click", doSearch);
  $("#sf-stage").addEventListener("change", doSearch);
  $("#sf-unpaid").addEventListener("change", doSearch);
  $("#sf-cancelled").addEventListener("change", doSearch);
  autoSearch("#sf-q", doSearch);
  if (canEdit) {
    $("#sf-new-v").addEventListener("click", () => renderSlipForm("provisional"));
    $("#sf-new-p").addEventListener("click", () => renderSlipForm("purchased"));
  }
  $$("button[data-slip]", body).forEach((b) => b.addEventListener("click", () => {
    // 등록 창을 열어 둔 채 기존 전표를 열면 어느 값이 저장될지 헷갈린다 — 먼저 닫는다
    const form = $("#slip-form", body);
    if (form) form.innerHTML = "";
    state.slipDetailId = Number(b.dataset.slip);
    renderSlipDetail();
  }));
  if (state.slipDetailId) renderSlipDetail();
}

function slipFieldsHtml(v) {
  v = v || {};
  const today = ymd();
  return `
    <div class="form-grid">
      <label>거래처<select id="sl-supplier"><option value="">- 선택 -</option>
        ${(state.suppliers || []).filter((s) => s.enabled).map((s) =>
          `<option value="${s.id}" ${s.id === v.supplierId ? "selected" : ""}>${escapeHtml(s.name)}</option>`).join("")}
      </select></label>
      <label>일자<input type="date" id="sl-date" value="${escapeHtml(v.purchaseDate || today)}"></label>
      <label>매입구분<select id="sl-type">
        ${state.meta.purchaseTypes.map((t) => `<option ${t === v.purchaseType ? "selected" : ""}>${escapeHtml(t)}</option>`).join("")}
      </select></label>
      <label>매입방법<input type="text" id="sl-channel" value="${escapeHtml(v.channel || "")}" placeholder="예: 직거래, 중고나라, 경매"></label>
      <label>매입금액 <span class="muted">(자산 매입가 합계 — 자동)</span>
        <input type="text" id="sl-amount" value="${v.totalAmount || 0}" readonly
          title="아래에 담은 자산의 '매입가(대당) × 수량' 합계입니다. 자산을 담으면 자동으로 채워집니다."
          style="background:var(--slate-soft); cursor:not-allowed;"></label>
      <label>주소<input type="text" id="sl-address" value="${escapeHtml(v.address || "")}"></label>
    </div>
    ${v.createdBy ? `<p class="muted" style="margin:8px 0 0;">등록자 ${escapeHtml(v.createdBy)}${v.createdAt ? " · " + escapeHtml(v.createdAt.slice(0, 16).replace("T", " ")) : ""}${v.confirmedBy ? ` · 입고확인 ${escapeHtml(v.confirmedBy)}` : ""}</p>` : ""}
    <div style="margin-top:8px;">

      <label class="check-line"><input type="checkbox" id="sl-paid" ${v.paid === false ? "" : "checked"}> 완납 (해제하면 미지급)</label>
    </div>
    <label class="muted" style="display:block; margin-top:8px;">메모
      <input type="text" id="sl-memo" value="${escapeHtml(v.memo || "")}" style="width:100%; margin-top:4px; padding:8px 10px; border:1px solid var(--border); border-radius:8px; background:var(--bg);">
    </label>`;
}

/** 전표 입력값 읽기 — 반드시 그 폼(scope) 안에서만 찾는다.
    등록 창과 상세 창이 같은 id를 쓰기 때문에, 범위를 안 주면 화면에 먼저 있는
    등록 창의 값을 읽어 엉뚱한 전표에 덮어쓴다(2026-07-29 전수조사 확인). */
function slipFormValues(scope) {
  const pick = (sel) => (scope || document).querySelector(sel);
  return {
    supplierId: pick("#sl-supplier").value || null,
    purchaseDate: pick("#sl-date").value,
    purchaseType: pick("#sl-type").value,
    channel: pick("#sl-channel").value,

    totalAmount: pick("#sl-amount").value.replaceAll(",", "") || 0,
    address: pick("#sl-address").value,
    paid: pick("#sl-paid").checked,
    memo: pick("#sl-memo").value,
  };
}

function renderSlipForm(stage) {
  const host = $("#slip-form");
  host.innerHTML = `
    <div class="slip-form">
      <div class="sf-title">
        <b>${stage === "provisional" ? "가입고 등록 (V전표)" : "매입 등록 (P전표)"}</b>
        <span class="muted">— ${stage === "provisional"
          ? "물건만 먼저 받은 상태입니다. 거래처·금액은 나중에 매입 확정 시 채울 수 있습니다."
          : "거래처와 금액을 확정해 등록합니다."}</span>
      </div>

      <section class="sf-step">
        <h4 class="sf-step-head"><span class="sf-no">1</span> 전표 정보</h4>
        ${slipFieldsHtml({})}
      </section>

      <section class="sf-step">
        <h4 class="sf-step-head"><span class="sf-no">2</span> 자산 담기
          <span class="chip chip-green" id="sa-count">0대</span></h4>
        <p class="muted" style="margin:0 0 10px;">
          줄을 쌓아 두고 아래 <b>[등록]</b>을 한 번 누르면 전표와 자산이 함께 만들어집니다 —
          전표를 먼저 만들고 다시 들어올 필요가 없습니다.
          수량을 10으로 넣으면 10대가 한 번에 생깁니다.</p>
        <div class="form-grid">
          <label>카테고리<select id="sa-cat">${state.categories.filter((c) => c.enabled)
            .map((c) => `<option value="${c.id}">${escapeHtml(c.name)}</option>`).join("")}</select></label>
          <label>브랜드<input type="text" id="sa-maker" placeholder="예: LENOVO"></label>
          <label style="grid-column:1 / -1;">모델명
            <input type="text" id="sa-model" placeholder="예: L480">
            <div id="sa-brief"></div>
          </label>
          <label>수량<input type="number" id="sa-qty" value="1" min="1" max="100"></label>
          <label>매입가(대당) <span class="muted">(한 대 값)</span>
            <input type="text" id="sa-price" value="0"
              title="한 대당 값입니다. 10대 × 10만원이면 여기에 100000을 넣으세요."></label>
          <label>등급<select id="sa-grade">${(state.meta.grades || [])
            .map((g) => `<option ${g === "미정" ? "selected" : ""}>${escapeHtml(g)}</option>`).join("")}</select></label>
          <label>재고 구분<select id="sa-tier" title="${escapeHtml(tierHelpText())}">
            ${tierOptions("양품")}</select></label>
          <label>제품코드 <span class="muted">(넣으면 바로 재고로 잡힘)</span>
            <input type="text" id="sa-productcode" placeholder="예: 840 G3_i7-6_내장"></label>
          <label>시리얼<input type="text" id="sa-serial" placeholder="1대일 때만 — 여러 대는 아래에서 스캔"></label>
          <label>보관위치<input type="text" id="sa-location"></label>
        </div>
        <details class="sf-spec" open>
          <summary>사양 <span class="muted">— 같은 모델이면 한 번만 넣으면 담는 줄마다 따라갑니다</span></summary>
          <div class="form-grid" style="margin-top:8px;">
            ${Object.entries(SPEC_LABELS).map(([k, label]) =>
              `<label>${label}<input type="text" id="sa-${k}"></label>`).join("")}
          </div>
        </details>
        <div class="inline-row" style="margin-top:8px;">
          <button class="btn btn-sm btn-primary" id="sa-add">＋ 줄 담기</button>
          <span class="muted" id="sa-hint">모델명 칸에서 엔터를 쳐도 담깁니다</span>
        </div>
      </section>

      <section class="sf-step" id="sa-list-step">
        <h4 class="sf-step-head"><span class="sf-no">3</span> 담긴 자산</h4>
        <div id="sa-list"></div>
        <div id="sa-sum"></div>
      </section>

      <div class="editor-actions sf-actions">
        <button class="btn btn-primary" id="sl-save">등록</button>
        <button class="btn" id="sl-cancel">닫기</button>
      </div>
    </div>`;

  // 쌓아 둔 자산 줄 — 저장 전까지는 화면 안에만 있다(서버에 아무것도 안 만든다)
  const lines = [];
  const drawLines = () => {
    const box = $("#sa-list");
    if (!lines.length) { box.innerHTML = `<p class="muted">아직 추가한 자산이 없습니다. 전표만 먼저 만들 수도 있습니다.</p>`; return; }
    const total = lines.reduce((n, l) => n + (Number(l.purchasePrice) || 0) * (Number(l.qty) || 1), 0);
    const cnt = lines.reduce((n, l) => n + (Number(l.qty) || 1), 0);
    box.innerHTML = `<div class="table-wrap"><table>
      <thead><tr><th>#</th><th>브랜드/모델</th><th>등급</th><th>시리얼</th>
        <th style="text-align:right;">수량</th><th style="text-align:right;">매입가</th><th></th></tr></thead>
      <tbody>${lines.map((l, i) => {
        const sn = l.serials || [];
        const need = Number(l.qty) || 1;
        return `<tr>
        <td>${i + 1}</td>
        <td>${escapeHtml([l.maker, l.model].filter(Boolean).join(" ")) || "-"}</td>
        <td>${escapeHtml(l.grade || "")}<div class="muted" style="font-size:11px;">${
          escapeHtml([l.cpu, l.ram, l.ssd].filter(Boolean).join(" / "))}</div></td>
        <td class="muted">${need > 1
          ? `<button class="btn btn-sm ${sn.length === need ? "" : "btn-primary"}" data-sn="${i}"
               title="바코드로 연속 스캔">📷 ${sn.length}/${need}</button>`
          : escapeHtml(l.serial || "-")}</td>
        <td style="text-align:right;">${need}</td>
        <td style="text-align:right;">${fmtWon(l.purchasePrice)}</td>
        <td><button class="btn btn-ghost btn-sm" data-rm="${i}" title="이 줄 빼기">✕</button></td></tr>`;
      }).join("")}
      </tbody>
      <tfoot><tr><th colspan="4">합계</th>
        <th style="text-align:right;">${cnt}대</th>
        <th style="text-align:right;">${fmtWon(total)}</th><th></th></tr></tfoot>
    </table></div>`;
    const badge = $("#sa-count");
    if (badge) badge.textContent = `${cnt}대`;
    // ★매입금액은 담은 자산의 합계로 자동 채운다(대표 2026-08-04).
    //   손으로 적으면 자산 합계와 어긋나 '전표 금액 불일치' 경고가 계속 뜬다.
    const amt = $("#sl-amount");
    if (amt) amt.value = total;
    const sumBox = $("#sa-sum");
    if (sumBox) {
      sumBox.innerHTML = total
        ? `<div class="inline-row" style="margin:8px 0 0;">
             <span>매입금액 <b>${fmtWon(total)}</b> = ${cnt}대 합계</span>
             <span class="muted">— 자산을 담으면 자동으로 맞춰집니다</span>
           </div>` : "";
    }
  };

  /* 시리얼 연속 스캔 패널 — 담긴 줄 아래에 붙는다. */
  let snOpen = null;
  const drawScanPanel = (box) => {
    const i = snOpen;
    const l = lines[i];
    const need = Number(l.qty) || 1;
    l.serials = l.serials || [];
    const panel = document.createElement("div");
    panel.className = "sn-scan";
    panel.innerHTML = `
      <div class="inline-row" style="margin:0;">
        <b>${escapeHtml([l.maker, l.model].filter(Boolean).join(" "))} 시리얼 스캔</b>
        <span class="chip ${l.serials.length === need ? "chip-green" : "chip-slate"}"
          >${l.serials.length} / ${need}</span>
        <span style="flex:1"></span>
        <button class="btn btn-sm" id="sn-close">닫기</button>
      </div>
      <div class="inline-row" style="margin:8px 0 0;">
        <input type="text" id="sn-input" placeholder="📷 바코드를 찍거나 시리얼을 입력하고 엔터"
          style="flex:1; min-width:240px; padding:8px 10px; border:1px solid var(--border);
                 border-radius:8px; background:var(--surface);">
        ${l.serials.length ? `<button class="btn btn-sm btn-ghost" id="sn-clear">전부 지우기</button>` : ""}
      </div>
      <div class="sn-list">${l.serials.map((v, k) =>
        `<span class="chip chip-green">${k + 1}. ${escapeHtml(v)}
           <button class="btn btn-ghost btn-sm" data-snrm="${k}" title="빼기">✕</button></span>`).join("")
        || `<span class="muted">아직 찍은 시리얼이 없습니다. 찍는 순서대로 자산에 하나씩 들어갑니다.</span>`}</div>
      <p class="muted" style="margin:8px 0 0;">
        ${need}대를 다 안 찍어도 됩니다 — 찍은 만큼만 들어가고 나머지는 비워 둡니다.</p>`;
    box.appendChild(panel);
    const inp = $("#sn-input");
    inp.focus();
    inp.addEventListener("keydown", (e) => {
      if (e.key !== "Enter") return;
      e.preventDefault();
      const v = inp.value.trim();
      if (!v) return;
      if (l.serials.includes(v)) { toast(`이미 찍은 시리얼입니다: ${v}`, true); inp.select(); return; }
      // 다른 줄에 이미 있는 시리얼도 막는다 — 저장 시 서버가 거절하면 전부 되돌아간다
      if (lines.some((x, k) => k !== i && (x.serials || []).includes(v))) {
        toast(`다른 줄에 이미 찍힌 시리얼입니다: ${v}`, true); inp.select(); return;
      }
      if (l.serials.length >= need) { toast(`${need}대를 다 찍었습니다.`, true); return; }
      l.serials.push(v);
      drawLines();
    });
    $("#sn-close").addEventListener("click", () => { snOpen = null; drawLines(); });
    const clr = $("#sn-clear");
    if (clr) clr.addEventListener("click", () => { l.serials = []; drawLines(); });
    $$("button[data-snrm]", panel).forEach((b) => b.addEventListener("click", () => {
      l.serials.splice(Number(b.dataset.snrm), 1);
      drawLines();
    }));
  };
  drawLines();

  const addLine = () => {
    const model = $("#sa-model").value.trim();
    const maker = $("#sa-maker").value.trim();
    if (!model && !maker) { toast("브랜드나 모델명을 입력하세요.", true); return; }
    const qty = Math.max(1, Math.min(Number($("#sa-qty").value) || 1, 100));
    const serial = $("#sa-serial").value.trim();
    if (serial && qty > 1) { toast("여러 대는 시리얼을 비워 두세요(나중에 각각 입력).", true); return; }
    const line = {
      categoryId: Number($("#sa-cat").value), maker, model, qty,
      grade: $("#sa-grade").value, tier: $("#sa-tier").value, serial,
      location: $("#sa-location").value.trim(),
      productCode: $("#sa-productcode").value.trim(),
      serials: [],                       // 여러 대일 때 연속 스캔으로 채운다
      purchasePrice: Number($("#sa-price").value.replaceAll(",", "")) || 0,
    };
    // 사양(CPU·RAM·SSD…)도 함께 담는다 — 예전에는 등록 뒤 한 대씩 열어야만 넣을 수
    // 있었다(대표 지적 2026-08-04). 같은 모델을 연달아 담을 땐 값이 남아 있어 그대로 따라간다.
    Object.keys(SPEC_LABELS).forEach((k) => { line[k] = ($(`#sa-${k}`).value || "").trim(); });
    lines.push(line);
    // 다음 줄을 빨리 넣도록 모델·시리얼만 비우고 나머지는 남긴다(같은 거래처 물건은 대개 비슷하다)
    $("#sa-model").value = ""; $("#sa-serial").value = "";
    $("#sa-brief").innerHTML = "";
    _briefLast = "";
    $("#sa-model").focus();
    drawLines();
  };
  attachSpecAutocomplete("sa-");
  attachCodeLookup("sa-productcode");   // 전표에 자산 담을 때도 쓰던 코드로
  $("#sa-add").addEventListener("click", addLine);
  ["#sa-model", "#sa-serial", "#sa-price", "#sa-qty"].forEach((sel) => {
    const el = $(sel);
    if (el) el.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); addLine(); } });
  });
  _briefLast = "";
  watchModelBrief("#sa-model", "#sa-maker", "#sa-brief");   // 재고를 이 화면에서 바로 확인

  $("#sl-cancel").addEventListener("click", () => { host.innerHTML = ""; });
  $("#sl-save").addEventListener("click", async () => {
    // ★연타하면 같은 전표가 2건 생기고 전표번호도 2개 소비된다.
    //   삭제 기능이 없어 빈 전표가 영구히 남으므로 버튼을 잠근다(자산 등록은 원래 잠그고 있었다).
    const btn = $("#sl-save");
    btn.disabled = true;
    try {
      // ★시리얼을 찍은 줄은 1대짜리로 쪼개 보낸다.
      //   서버는 '여러 대 등록에는 시리얼을 비워라'고 막는데(개체마다 다르니 당연),
      //   연속 스캔으로 받은 시리얼은 개체별로 확정된 값이라 이렇게 풀어 주면 그대로 들어간다.
      const payload = [];
      for (const l of lines) {
        const sn = (l.serials || []).filter(Boolean);
        const need = Number(l.qty) || 1;
        if (!sn.length || need <= 1) { payload.push(l); continue; }
        sn.forEach((v) => payload.push(Object.assign({}, l, { qty: 1, serial: v, serials: undefined })));
        const rest = need - sn.length;      // 못 찍은 나머지는 시리얼 없이
        if (rest > 0) payload.push(Object.assign({}, l, { qty: rest, serial: "", serials: undefined }));
      }
      // 매입금액은 담은 자산 합계로 확정한다 — readonly 칸이라 화면 값과 어긋날 일이 없지만,
      // 저장 시점에 한 번 더 계산해 두면 전표 금액 ↔ 자산 합 대조가 항상 일치한다.
      const slipVals = slipFormValues(host);
      slipVals.totalAmount = lines.reduce(
        (n, l) => n + (Number(l.purchasePrice) || 0) * (Number(l.qty) || 1), 0);
      const res = await api("/api/purchase-batches", {
        method: "POST",
        body: Object.assign({ stage, assets: payload }, slipVals),
      });
      toast(`${res.slipNo} 등록 완료` + (res.assetCount
        ? ` · 자산 ${res.assetCount}대가 함께 만들어졌습니다` : ""));
      host.innerHTML = "";
      state.slipDetailId = res.id;      // 저장 직후 그 전표를 바로 연다(자산 목록이 보인다)
      renderSlips($("#ptab-body"));
    } catch (err) { toast(err.message, true); btn.disabled = false; }
  });
}

async function renderSlipDetail() {
  const host = $("#slip-detail");
  if (!host || !state.slipDetailId) return;
  host.innerHTML = `<div class="card"><p class="muted">불러오는 중…</p></div>`;
  revealPanel(host);
  let s;
  try { s = await api(`/api/purchase-batches/${state.slipDetailId}`); }
  catch (err) { host.innerHTML = `<div class="card"><p class="muted">${escapeHtml(err.message)}</p></div>`; return; }
  const canEdit = hasPerm("purchase.edit");
  host.innerHTML = `
    <div class="card">
      <div class="inline-row">
        <h3 style="margin:0; flex:1;">${escapeHtml(s.slipNo || "")}
          <span class="chip ${s.stage === "provisional" ? "chip-blue" : "chip-green"}">${escapeHtml(s.stageLabel)}</span>
          ${s.confirmedAt ? `<span class="muted" style="font-size:13px;">입고확인 ${escapeHtml(s.confirmedAt.slice(0, 10))} ${escapeHtml(s.confirmedBy)}</span>` : ""}
          ${s.cancelledAt ? `<span class="chip chip-red">🚫 취소됨 ${escapeHtml(s.cancelledAt.slice(0, 10))} ${escapeHtml(s.cancelledBy)}${s.cancelReason ? " · " + escapeHtml(s.cancelReason) : ""}</span>` : ""}
          ${s.returnedAt ? `<span class="chip chip-violet">↩ 반품 ${escapeHtml(s.returnedAt)}${s.returnReason ? " · " + escapeHtml(s.returnReason) : ""}</span>` : ""}
        </h3>
        <button class="btn btn-sm" id="sd-close">닫기</button>
      </div>
      ${canEdit ? slipFieldsHtml(s) : ""}
      ${canEdit ? `<div class="editor-actions">
        <button class="btn btn-primary" id="sd-save">저장</button>
        ${s.stage === "provisional" && !s.cancelledAt
          ? `<button class="btn btn-primary" id="sd-confirm">✔ 입고확인 → 매입 확정</button>` : ""}
        ${s.cancelledAt
          ? `<button class="btn" id="sd-uncancel">↩ 취소 되돌리기</button>`
          : `<button class="btn btn-ghost" id="sd-cancel-slip" title="전표를 취소로 표시합니다(지우지 않습니다)">🚫 전표 취소</button>
             ${s.returnedAt ? "" : `<button class="btn btn-ghost" id="sd-return" title="거래처로 되돌려 보냈을 때">↩ 거래처 반품</button>`}`}
      </div>` : ""}
    </div>
    <div class="card">
      <div class="inline-row">
        <h3 style="margin:0; flex:1;">자산 ${s.assets.length}대
          <span class="muted" style="font-weight:400;">자산합 ${fmtWon(s.assignedAmount)} / 전표 ${fmtWon(s.totalAmount)}</span>
          ${amountGapChip(s, true)}
          ${canEdit && amountGap(s) ? `<button class="btn btn-sm" id="sd-syncamt"
            title="전표 매입금액을 자산 매입가 합계로 맞춥니다">금액 맞추기</button>` : ""}</h3>
        ${canEdit && !s.cancelledAt
          ? `<button class="btn btn-sm btn-primary" id="sd-add">＋ 자산 추가</button>` : ""}
      </div>
      <div id="sd-assetform"></div>
      <p class="muted" style="font-size:12px; margin:4px 0;">관리번호를 누르면 그 자리에서 바로 수정할 수 있습니다.
        재고반영 체크 = 그 자산이 쇼핑몰 재고에 세어집니다(제품코드 필요).</p>
      ${canEdit && !s.cancelledAt ? `
      <div class="inline-row" style="gap:6px; margin:0 0 6px; flex-wrap:wrap;">
        <label class="check-line" style="font-size:13px;"><input type="checkbox" id="sd-checkall"> 전체 선택</label>
        <input type="text" id="sd-code-input" placeholder="제품코드 (예: 840 G3_i7-6_내장)"
          title="한 전표에 여러 모델이 섞일 수 있습니다 — 같은 모델만 체크한 뒤 넣으세요"
          style="min-width:220px; padding:5px 8px; border:1px solid var(--border);
                 border-radius:8px; background:var(--bg); font-size:13px;">
        <button class="btn btn-sm" id="sd-code-apply"
          title="체크한 자산에만 이 제품코드를 넣습니다. 개별 수정은 관리번호를 누르세요">✏ 선택 자산에 코드 입력</button>
        <span style="flex:1"></span>
        <button class="btn btn-sm" id="sd-stock-on"
          title="선택한 자산을 쇼핑몰 재고로 셉니다(제품코드가 있는 것만)">📦 일괄 재고반영</button>
        <button class="btn btn-sm" id="sd-stock-off"
          title="선택한 자산을 쇼핑몰 재고에서 뺍니다">📴 일괄 재고해제</button>
      </div>` : ""}
      <div class="table-wrap"><table>
        <thead><tr><th style="width:28px;"></th><th>관리번호</th><th>브랜드 / 모델</th><th>스펙</th>
          <th>등급</th><th>상태</th><th>매입가</th><th>제품코드</th><th>재고반영</th></tr></thead>
        <tbody>${s.assets.map((a) => `
          <tr><td><input type="checkbox" class="sd-pick" data-aid="${a.id}"></td>
          <td><button class="btn btn-ghost btn-sm" data-openasset="${a.id}"
                 style="font-weight:600; padding:2px 6px;"
                 title="이 자산을 여기서 수정합니다">${escapeHtml(a.assetNo)}</button></td>
          <td>${escapeHtml([a.maker, a.model].filter(Boolean).join(" "))}</td>
          <td class="muted">${escapeHtml(specSummary(a))}</td>
          <td>${escapeHtml(a.grade)}</td><td>${statusChip(a.status)}</td>
          <td>${fmtWon(a.purchasePrice)}</td>
          <td class="muted" style="font-size:12px;">${escapeHtml(a.productCode || "-")}</td>
          <td>${canEdit && !s.cancelledAt
            ? `<input type="checkbox" class="sd-stock" data-aid="${a.id}" ${a.stockListed ? "checked" : ""}
                 title="${a.productCode ? "체크하면 쇼핑몰 재고에 세어집니다" : "제품코드를 먼저 넣어야 켤 수 있습니다"}">`
            : (a.stockListed ? '<span class="chip chip-green">반영</span>' : '<span class="muted">-</span>')}</td>
          </tr>`).join("")
          || `<tr><td colspan="9" class="muted">이 전표에 등록된 자산이 없습니다. 위 [＋ 자산 추가]를 누르세요.</td></tr>`}
        </tbody></table></div>
      <div id="sd-assetdetail"></div>
    </div>`;
  $("#sd-close").addEventListener("click", () => { state.slipDetailId = undefined; host.innerHTML = ""; });

  // 전표 안에서 자산을 바로 열어 고친다 — 자산 목록으로 찾아가지 않아도 되게.
  // 저장하면 전표를 다시 그려 금액·상태가 즉시 맞춰진다(같은 데이터를 두 곳이 보므로).
  $$("button[data-openasset]", host).forEach((b) => b.addEventListener("click", () => {
    state.assetDetailId = Number(b.dataset.openasset);
    renderAssetDetail("#sd-assetdetail", () => renderSlipDetail());
  }));

  // 재고반영 — 개별 체크는 즉시 저장, 일괄 버튼은 선택분을 한 번에
  const bulkStock = async (ids, on) => {
    if (!ids.length) { toast("자산을 먼저 선택하세요.", true); return; }
    try {
      const r = await api("/api/assets/stock-listing", { method: "POST", body: { ids, on } });
      let msg = `${on ? "재고반영" : "재고해제"} ${r.ok}대`;
      if (r.skipped && r.skipped.length) msg += ` (건너뜀 ${r.skipped.length}대 — 제품코드 없음 등)`;
      toast(msg, r.ok === 0);
      renderSlipDetail();
    } catch (err) { toast(err.message, true); }
  };
  $$("input.sd-stock", host).forEach((cb) => cb.addEventListener("change", () =>
    bulkStock([Number(cb.dataset.aid)], cb.checked)));
  const checkAll = $("#sd-checkall");
  if (checkAll) checkAll.addEventListener("change", () => {
    $$("input.sd-pick", host).forEach((cb) => { cb.checked = checkAll.checked; });
  });
  attachShiftPick(host, "input.sd-pick");
  attachShiftPick(host, "input.sd-stock");   // 재고반영도 여러 대를 한 번에
  const pickIds = () => $$("input.sd-pick", host).filter((cb) => cb.checked).map((cb) => Number(cb.dataset.aid));
  const onBtn = $("#sd-stock-on"), offBtn = $("#sd-stock-off");
  if (onBtn) onBtn.addEventListener("click", () => bulkStock(pickIds(), true));
  if (offBtn) offBtn.addEventListener("click", () => bulkStock(pickIds(), false));
  // 제품코드 일괄 입력 — 체크한 자산에만. 한 전표에 모델이 섞이므로 '전표 전체'는 없다.
  const codeBtn = $("#sd-code-apply");
  if (codeBtn) codeBtn.addEventListener("click", async () => {
    const ids = pickIds();
    if (!ids.length) { toast("자산을 먼저 선택하세요.", true); return; }
    const pc = $("#sd-code-input").value.trim();
    if (!pc) { toast("제품코드를 입력하세요. (지우려면 각 자산을 열어 지워 주세요)", true); return; }
    if (!confirm(`선택한 ${ids.length}대에 제품코드를 넣습니다.\n\n  ${pc}\n\n`
        + "다른 모델이 섞여 있지 않은지 확인하세요. 계속할까요?")) return;
    codeBtn.disabled = true;
    try {
      const r = await api("/api/assets/product-code", { method: "POST", body: { ids, productCode: pc } });
      toast(`제품코드 입력 ${r.ok}대` + (r.skipped.length ? ` (건너뜀 ${r.skipped.length}대)` : ""));
      renderSlipDetail();
    } catch (err) { toast(err.message, true); }
    finally { codeBtn.disabled = false; }
  });

  if (!canEdit) return;
  const syncBtn = $("#sd-syncamt");
  if (syncBtn) syncBtn.addEventListener("click", async () => {
    syncBtn.disabled = true;
    try {
      const r = await api(`/api/purchase-batches/${state.slipDetailId}/sync-amount`, { method: "POST" });
      toast(r.changed ? `매입금액을 ${r.totalAmount.toLocaleString()}원으로 맞췄습니다.`
                      : "이미 맞아 있습니다.");
      renderSlipDetail();
      renderSlips($("#ptab-body"));
    } catch (err) { toast(err.message, true); syncBtn.disabled = false; }
  });
  $("#sd-save").addEventListener("click", async () => {
    const btn = $("#sd-save");
    btn.disabled = true;
    try {
      await api(`/api/purchase-batches/${s.id}`, { method: "PATCH", body: slipFormValues(host) });
      toast("저장했습니다.");
      renderSlips($("#ptab-body"));
    } catch (err) { toast(err.message, true); }
    finally { btn.disabled = false; }
  });
  const cancelBtn = $("#sd-cancel-slip");
  if (cancelBtn) cancelBtn.addEventListener("click", async () => {
    const n = s.assets.length;
    const msg = [
      `전표 ${s.slipNo}을(를) 취소합니다.`,
      n ? `딸린 자산 ${n}대도 함께 '매입취소'가 되어 재고에서 빠집니다.` : "",
      "", "지우는 게 아니라 취소로 표시만 하며, 되돌릴 수 있습니다.", "", "계속할까요?",
    ].join(String.fromCharCode(10));
    if (!confirm(msg)) return;
    const reason = prompt("취소 사유 (선택)") || "";
    cancelBtn.disabled = true;
    try {
      const r = await api(`/api/purchase-batches/${s.id}/cancel`, { method: "POST", body: { reason } });
      toast(`취소 완료` + (r.cancelledAssets ? ` · 자산 ${r.cancelledAssets}대 함께 취소` : ""));
      renderSlips($("#ptab-body"));
      renderSlipDetail();
    } catch (err) { toast(err.message, true); cancelBtn.disabled = false; }
  });

  const unBtn = $("#sd-uncancel");
  if (unBtn) unBtn.addEventListener("click", async () => {
    if (!confirm("전표 취소를 되돌립니다. 자산도 입고 상태로 돌아갑니다. 계속할까요?")) return;
    unBtn.disabled = true;
    try {
      const r = await api(`/api/purchase-batches/${s.id}/uncancel`, { method: "POST", body: {} });
      toast(`되돌렸습니다` + (r.restoredAssets ? ` · 자산 ${r.restoredAssets}대 복구` : ""));
      renderSlips($("#ptab-body"));
      renderSlipDetail();
    } catch (err) { toast(err.message, true); unBtn.disabled = false; }
  });

  const retBtn = $("#sd-return");
  if (retBtn) retBtn.addEventListener("click", async () => {
    const n = s.assets.length;
    if (!confirm(`거래처로 되돌려 보낸 것으로 기록합니다.
자산 ${n}대가 재고에서 빠집니다.

계속할까요?`)) return;
    const reason = prompt("반품 사유 (선택)") || "";
    retBtn.disabled = true;
    try {
      const r = await api(`/api/purchase-batches/${s.id}/return`, { method: "POST", body: { reason } });
      toast(`반품 처리 · 자산 ${r.returnedAssets}대`);
      renderSlips($("#ptab-body"));
      renderSlipDetail();
    } catch (err) { toast(err.message, true); retBtn.disabled = false; }
  });

  const conf = $("#sd-confirm");
  if (conf) conf.addEventListener("click", async () => {
    // ★범위를 지정하지 않으면 화면 어딘가에 열려 있는 '자산 등록' 폼의 빈 칸을 읽어
    //   거래처가 제대로 선택돼 있어도 확정이 막힌다(바로 아래 slipFormValues(host)는 범위를 준다).
    const supEl = $("#sl-supplier", host);
    if (supEl && !supEl.value) { toast("매입 확정에는 거래처가 필요합니다.", true); return; }
    if (!confirm("이 가입고를 매입으로 확정할까요? 매입 전표번호(P)가 새로 부여됩니다.")) return;
    try {
      const r = await api(`/api/purchase-batches/${s.id}/confirm`, { method: "POST", body: slipFormValues(host) });
      toast(`매입 확정: ${r.slipNo}`);
      renderSlips($("#ptab-body"));
    } catch (err) { toast(err.message, true); }
  });
  const addBtn = $("#sd-add");
  if (addBtn) addBtn.addEventListener("click", () => renderAssetForm(s));
}

/* slip: 전표 객체(전표 상세에서 호출) 또는 null(자산 목록에서 직접 등록)
   hostId: 폼을 그릴 위치, onDone: 등록 후 콜백 */
function renderAssetForm(slip, hostId, onDone) {
  const host = $(hostId || "#sd-assetform");
  const slipOptions = (state.slipsForPicker || []);
  host.innerHTML = `
    <div style="border:1px dashed var(--border); border-radius:8px; padding:14px; margin:8px 0;">
      <b>자산 등록</b> <span class="muted">— 관리번호는 자동 발번(YYMMDD-NNNN)됩니다. TMS 번호는 직접 입력하세요(1대만).</span>
      <div class="form-grid" style="margin-top:8px;">
        ${slip ? `<label>전표<input type="text" value="${escapeHtml(slip.slipNo || "")} ${escapeHtml(slip.supplierName || "")}" disabled></label>`
          : `<label>매입 전표 (선택)<select id="ar-batch"><option value="">- 전표 없이 등록 -</option>
              ${slipOptions.map((s) => `<option value="${s.id}">${escapeHtml(`${s.slipNo || ""} ${s.stageLabel} ${s.purchaseDate} ${s.supplierName || ""}`)}</option>`).join("")}
            </select></label>`}
        <label>카테고리 *<select id="ar-cat">${state.categories.filter((c) => c.enabled).map((c) => `<option value="${c.id}">${escapeHtml(c.name)}</option>`).join("")}</select></label>
        <label>브랜드<input type="text" id="ar-maker" placeholder="예: LENOVO"></label>
        <label style="grid-column:1 / -1;">모델명
          <input type="text" id="ar-model" placeholder="예: L480">
          <div id="ar-brief"></div>
        </label>
        <label>시리얼번호<input type="text" id="ar-serial"></label>
        <label>등급<select id="ar-grade">${state.meta.grades.map((g) => `<option ${g === "미정" ? "selected" : ""}>${escapeHtml(g)}</option>`).join("")}</select></label>
        <label>재고 구분<select id="ar-tier" title="${escapeHtml(tierHelpText())}">
          ${tierOptions("양품")}</select></label>
        <label>보관위치<input type="text" id="ar-location"></label>
        <label>매입가(대당)<input type="text" id="ar-price" value="0"></label>
        <label>판매가<input type="text" id="ar-saleprice" value="0"></label>
        <label>수량<input type="number" id="ar-qty" value="1" min="1" max="100"></label>
        <label>관리번호 직접 지정<input type="text" id="ar-no" placeholder="비우면 자동"></label>
      </div>
      <h3 style="margin-top:12px;">스펙</h3>
      <div class="form-grid">
        ${Object.entries(SPEC_LABELS).map(([k, label]) => `<label>${label}<input type="text" id="ar-${k}"></label>`).join("")}
      </div>
      <label class="muted" style="display:block; margin-top:8px;">특이사항
        <input type="text" id="ar-notes" style="width:100%; margin-top:4px; padding:8px 10px; border:1px solid var(--border); border-radius:8px; background:var(--bg);">
      </label>
      <div class="editor-actions">
        <button class="btn btn-primary" id="ar-save">등록</button>
        <button class="btn" id="ar-close">닫기</button>
      </div>
      <div id="ar-result"></div>
    </div>`;
  attachSpecAutocomplete("ar-");   // CPU/GPU/RAM/SSD 등 자동완성
  _briefLast = "";
  watchModelBrief("#ar-model", "#ar-maker", "#ar-brief");   // 재고 대조를 이 화면 안에서
  $("#ar-close").addEventListener("click", () => {
    host.innerHTML = "";
    if (state.assetFormDirty) {          // 등록한 게 있으면 그때 목록을 새로 그린다
      state.assetFormDirty = false;
      if (onDone) onDone(); else renderSlipDetail();
    }
  });
  attachSpecAutocomplete("ar-");
  $("#ar-save").addEventListener("click", async () => {
    const btn = $("#ar-save");
    btn.disabled = true;
    const pickedBatch = slip ? slip.id : ($("#ar-batch").value ? Number($("#ar-batch").value) : null);
    const body = {
      batchId: pickedBatch, categoryId: Number($("#ar-cat").value),
      maker: $("#ar-maker").value, model: $("#ar-model").value, serial: $("#ar-serial").value,
      grade: $("#ar-grade").value, tier: $("#ar-tier").value, location: $("#ar-location").value,
      purchasePrice: $("#ar-price").value.replaceAll(",", "") || 0,
      salePrice: $("#ar-saleprice").value.replaceAll(",", "") || 0,
      qty: Number($("#ar-qty").value) || 1,
      assetNo: $("#ar-no").value.trim(), notes: $("#ar-notes").value,
    };
    Object.keys(SPEC_LABELS).forEach((k) => { body[k] = $(`#ar-${k}`).value; });
    try {
      const created = await api("/api/assets", { method: "POST", body });
      toast(`${created.length}대 등록 완료`);
      $("#ar-result").innerHTML = `<p style="margin-top:10px;">발번된 관리번호: ${created.map((c) => `<span class="chip chip-green" style="margin:2px;">${escapeHtml(c.assetNo)}</span>`).join(" ")}</p>`;
      $("#ar-no").value = ""; $("#ar-serial").value = "";
      // ★폼을 유지한다 — 스펙이 다른 여러 대를 연이어 넣을 때 매번 처음부터
      //   다시 입력하지 않도록. 목록은 [닫기]를 누를 때 갱신한다.
      state.assetFormDirty = true;
    } catch (err) { toast(err.message, true); }
    finally { btn.disabled = false; }
  });
}

/* 재고 구분 — 담당자가 셋 중 무엇인지 헷갈리지 않게 설명을 항상 함께 띄운다(대표 2026-08-04).
   문구는 서버(app/purchase/__init__.py TIER_HELP)에서 내려온다 — 한곳만 고치면 전 화면이 바뀐다. */
function tierHelpText() {
  const h = state.meta.tierHelp || {};
  return (state.meta.tiers || []).map((t) => `${t} — ${h[t] || ""}`).join(String.fromCharCode(10));
}

function tierOptions(sel) {
  const h = state.meta.tierHelp || {};
  return (state.meta.tiers || ["양품", "실재고", "가재고"]).map((t) =>
    `<option value="${escapeHtml(t)}" ${t === sel ? "selected" : ""}
       title="${escapeHtml(h[t] || "")}">${escapeHtml(t)}${h[t] ? " — " + escapeHtml(h[t]) : ""}</option>`
  ).join("");
}

function tierHelpBox() {
  const h = state.meta.tierHelp || {};
  const rows = (state.meta.tiers || []).map((t) =>
    `<div style="display:flex; gap:8px; align-items:baseline; padding:2px 0;">
       <b style="flex:0 0 52px;">${escapeHtml(t)}</b>
       <span class="muted">${escapeHtml(h[t] || "")}</span></div>`).join("");
  return rows ? `<div style="margin:6px 0; padding:8px 10px; background:var(--bg);
    border:1px solid var(--border); border-radius:8px; font-size:13px;">${rows}</div>` : "";
}

/* ---------------- 실재고 / 가재고 전환 ----------------
   대표 요청(2026-08-04): 양품이 아닌 자산은 누군가 손을 대야 나간다. 어디에 몇 대가
   묶여 있는지 카테고리별로 보여 주고, 그 자리에서 수리 내역(비용·교체부품)을 적고
   세 구분 중 하나로 올린다. 비용은 총액을 넣으면 공급가·부가세가 자동으로 갈린다. */
async function renderConvert(body) {
  const seq = ++state.renderSeq;
  body.innerHTML = `<p class="muted">불러오는 중…</p>`;
  let d;
  try { d = await api("/api/assets/to-convert"); }
  catch (err) { body.innerHTML = `<p class="muted">${escapeHtml(err.message)}</p>`; return; }
  if (seq !== state.renderSeq) return;
  state.convertOpen = state.convertOpen || {};

  if (!d.total) {
    body.innerHTML = `<div class="card placeholder">
      <div class="ph-badge">전환할 자산이 없습니다</div>
      <p>모든 자산이 <b>양품</b>입니다 — 바로 출고할 수 있습니다.</p></div>`;
    return;
  }
  const tierChips = Object.entries(d.byTier)
    .map(([t, n]) => `<span class="chip ${t === "가재고" ? "chip-red" : "chip-amber"}">${escapeHtml(t)} ${n}대</span>`).join(" ");

  body.innerHTML = `
    <div class="card">
      <h3>전환이 필요한 자산 ${d.total.toLocaleString()}대 ${tierChips}</h3>
      <p class="muted">가재고는 <b>출고가 막혀 있습니다</b> — 수리를 마치고 양품 또는 실재고로 올려야 나갑니다.
        수리비를 적으면 그 자산의 원가에 더해지고, 부가세는 매입세액으로 재무에 잡힙니다.</p>
      ${tierHelpBox()}
      <div class="inline-row">
        <input type="text" id="cv-find" placeholder="자산번호 · 모델 · 전표번호 검색" style="min-width:240px;">
        <span class="muted">수리비 누계 <b>${fmtWon(d.repairCost)}</b></span>
      </div>
    </div>
    <div id="cv-groups"></div>`;

  const draw = () => {
    const kw = ($("#cv-find").value || "").trim().toUpperCase();
    const match = (a) => !kw || [a.assetNo, a.model, a.maker, a.slipNo]
      .some((v) => String(v || "").toUpperCase().includes(kw));
    $("#cv-groups").innerHTML = d.groups.map((g) => {
      const list = g.assets.filter(match);
      if (!list.length) return "";
      const open = !!state.convertOpen[g.category];
      const pgC = paged(list, "cv:" + g.category);
      return `<div class="card cv-group" data-cat="${escapeHtml(g.category)}">
        <div class="inline-row cv-head" style="cursor:pointer;">
          <b style="font-size:16px;">${escapeHtml(g.category)}</b>
          <span class="chip chip-blue">${list.length}대</span>
          ${Object.entries(g.byTier).map(([t, n]) =>
            `<span class="chip ${t === "가재고" ? "chip-red" : "chip-amber"}">${escapeHtml(t)} ${n}</span>`).join(" ")}
          ${g.repairCost ? `<span class="muted">수리비 ${fmtWon(g.repairCost)}</span>` : ""}
          <span style="flex:1"></span>
          <span class="muted">${open ? "▲ 접기" : "▼ 펼치기"}</span>
        </div>
        ${open ? `<div class="table-wrap" style="margin-top:10px;"><table><thead><tr>
          <th>자산번호</th><th>모델</th><th>등급</th><th>구분</th><th>전표</th>
          <th style="text-align:right;">수리비</th><th></th></tr></thead>
          <tbody>${pgC.rows.map((a) => `<tr>
            <td><b>${escapeHtml(a.assetNo)}</b></td>
            <td>${escapeHtml([a.maker, a.model].filter(Boolean).join(" "))}
              <div class="muted" style="font-size:11px;">${escapeHtml(
                [a.cpu, a.ram, a.ssd].filter(Boolean).join(" / "))}</div></td>
            <td>${escapeHtml(a.grade)}</td>
            <td><span class="chip ${a.tier === "가재고" ? "chip-red" : "chip-amber"}">${escapeHtml(a.tier)}</span></td>
            <td class="muted">${escapeHtml(a.slipNo || "-")}</td>
            <td style="text-align:right;">${a.repairCost ? fmtWon(a.repairCost) : "-"}
              ${a.repairCount ? `<div class="muted" style="font-size:11px;">${a.repairCount}건</div>` : ""}</td>
            <td><button class="btn btn-sm btn-primary" data-cvfix="${a.id}">🔧 수리·전환</button></td>
          </tr>`).join("")}</tbody></table></div>${pgC.bar}` : ""}
      </div>`;
    }).join("") || `<div class="card placeholder"><p>검색 결과가 없습니다.</p></div>`;

    $$(".cv-head").forEach((h) => h.addEventListener("click", () => {
      const c = h.closest(".cv-group").dataset.cat;
      state.convertOpen[c] = !state.convertOpen[c];
      draw();
    }));
    $$("button[data-cvfix]").forEach((b) => b.addEventListener("click", () =>
      openRepairPanel(Number(b.dataset.cvfix), () => renderConvert(body))));
    // 묶음 안 화살표
    $$(".cv-group").forEach((card) => wirePager(card, "cv:" + card.dataset.cat, draw));
  };

  $("#cv-find").addEventListener("input", () => { state.pages = state.pages || {}; draw(); });
  draw();
}

/* 수리 등록 + 구분 전환 패널. 총액만 넣으면 공급가·부가세가 자동으로 갈린다.
   자산번호 하나로 입고부터 판매까지 이력이 이어져 보인다. */
async function openRepairPanel(aid, onDone) {
  let host = $("#cv-repair");
  if (!host) {
    host = document.createElement("div");
    host.id = "cv-repair";
    ($("#aview-body") || $("#ptab-body") || document.body).appendChild(host);
  }
  let a;
  try { a = await api(`/api/assets/${aid}`); }
  catch (err) { toast(err.message, true); return; }

  const today = ymd();
  host.innerHTML = `<div class="card">
    <div class="inline-row">
      <h3 style="margin:0; flex:1;">🔧 ${escapeHtml(a.assetNo)}
        <span class="chip ${a.tier === "가재고" ? "chip-red" : "chip-amber"}">${escapeHtml(a.tier)}</span>
        <span class="muted" style="font-size:13px;">${escapeHtml([a.maker, a.model].filter(Boolean).join(" "))}</span>
      </h3>
      <button class="btn btn-sm" id="cr-close">닫기</button>
    </div>
    <div class="form-grid">
      <label>수리일<input type="date" id="cr-date" value="${today}"></label>
      <label>교체한 부품<input type="text" id="cr-parts" placeholder="예: SSD 512GB, 배터리"></label>
      <label style="grid-column:1 / -1;">수리 내용
        <input type="text" id="cr-desc" placeholder="예: 액정 교체, 시트지 재작업"></label>
      <label>수리비 총액 <span class="muted">(부가세 포함)</span>
        <input type="text" id="cr-cost" value="0"></label>
      <label>전환할 구분
        <select id="cr-tier" title="${escapeHtml(tierHelpText())}">${tierOptions(a.tier)}</select></label>
    </div>
    <div id="cr-vat" class="muted" style="margin:6px 0;"></div>
    <div class="editor-actions">
      <button class="btn btn-primary" id="cr-save">수리 기록 + 전환</button>
    </div>
    <h3 style="margin-top:16px;">이 자산의 이력 <span class="muted" style="font-weight:400;">— 입고부터 판매까지</span></h3>
    ${a.repairs && a.repairs.length ? `<div class="table-wrap"><table>
      <thead><tr><th>수리일</th><th>내용</th><th>교체부품</th>
        <th style="text-align:right;">공급가</th><th style="text-align:right;">부가세</th>
        <th style="text-align:right;">총액</th><th>담당</th></tr></thead>
      <tbody>${a.repairs.map((r) => `<tr>
        <td>${escapeHtml(r.repairDate)}</td>
        <td>${escapeHtml(r.description)}</td>
        <td class="muted">${escapeHtml(r.parts || "-")}</td>
        <td style="text-align:right;">${fmtWon(r.net || (r.cost - (r.vat || 0)))}</td>
        <td style="text-align:right;" class="muted">${fmtWon(r.vat || 0)}</td>
        <td style="text-align:right;"><b>${fmtWon(r.cost)}</b></td>
        <td class="muted">${escapeHtml(r.createdBy)}</td></tr>`).join("")}</tbody>
      <tfoot><tr><th colspan="5">수리비 합계</th>
        <th style="text-align:right;">${fmtWon(a.repairs.reduce((n, r) => n + r.cost, 0))}</th><th></th></tr></tfoot>
    </table></div>` : `<p class="muted">아직 수리 기록이 없습니다.</p>`}
    <div class="timeline" style="margin-top:12px;">${(a.events || []).slice(0, 20).map((e) => `
      <div class="tl-item"><b>${escapeHtml(e.action)}</b>
        <span class="tl-time">${escapeHtml(String(e.createdAt || "").slice(0, 16))}
          ${escapeHtml(e.createdBy || "")}</span>
        ${e.detail ? `<div class="tl-detail muted">${escapeHtml(
          typeof e.detail === "string" ? e.detail : JSON.stringify(e.detail))}</div>` : ""}
      </div>`).join("")}</div>
  </div>`;
  revealPanel(host);

  // 총액 → 공급가 + 부가세 (서버와 같은 계산: 부가세 = 총액 × 10/110, 반올림)
  const showVat = () => {
    const total = Number(($("#cr-cost").value || "0").replaceAll(",", "")) || 0;
    const vat = Math.round(total * 10 / 110);
    $("#cr-vat").innerHTML = total
      ? `공급가 <b>${fmtWon(total - vat)}</b> + 부가세 <b>${fmtWon(vat)}</b> = 총액 ${fmtWon(total)}
         <span class="muted">— 부가세는 매입세액으로 재무에 잡힙니다</span>`
      : "";
  };
  $("#cr-cost").addEventListener("input", showVat);
  showVat();
  $("#cr-close").addEventListener("click", () => { host.innerHTML = ""; });
  $("#cr-save").addEventListener("click", async () => {
    const btn = $("#cr-save");
    const desc = $("#cr-desc").value.trim();
    const parts = $("#cr-parts").value.trim();
    const tier = $("#cr-tier").value;
    if (!desc && !parts && tier === a.tier) {
      toast("수리 내용이나 교체 부품을 적거나, 구분을 바꿔 주세요.", true); return;
    }
    btn.disabled = true;
    try {
      if (desc || parts) {
        await api(`/api/assets/${aid}/repairs`, { method: "POST", body: {
          description: desc, parts, tier,
          cost: Number(($("#cr-cost").value || "0").replaceAll(",", "")) || 0,
          repairDate: $("#cr-date").value,
        }});
      } else {
        await api(`/api/assets/${aid}`, { method: "PATCH", body: { tier } });
      }
      toast(`${a.assetNo} 기록했습니다.` + (tier !== a.tier ? ` → ${tier}` : ""));
      host.innerHTML = "";
      if (onDone) onDone();
    } catch (err) { toast(err.message, true); btn.disabled = false; }
  });
}

/* ---------------- 제품코드 입력 안내 ----------------
   대표 요청(2026-08-04): 코드가 없는 자산을 모델별로 모아 보여 주고, 코드만 넣으면
   재고로 잡히게. 등급·재고구분도 같은 자리에서 바꾼다. 개별 수정과 Shift 다중선택 둘 다.
   ★이미 판매된 것(출고완료)과 다시 안 팔 것(폐기·매입취소·거래처반품)은 서버가 뺀다. */
async function renderUncoded(body) {
  const seq = ++state.renderSeq;
  body.innerHTML = `<p class="muted">불러오는 중…</p>`;
  let d;
  try { d = await api("/api/assets/uncoded"); }
  catch (err) { body.innerHTML = `<p class="muted">${escapeHtml(err.message)}</p>`; return; }
  if (seq !== state.renderSeq) return;
  state.uncoded = d;
  state.uncodedOpen = state.uncodedOpen || {};

  if (!d.total) {
    body.innerHTML = `<div class="card placeholder">
      <div class="ph-badge">다 끝났습니다</div>
      <p>제품코드가 필요한 자산이 없습니다.</p>
      <p class="muted">출고완료·폐기·매입취소·거래처반품은 대상에서 제외됩니다.</p></div>`;
    return;
  }
  body.innerHTML = `
    <div class="card">
      <h3>제품코드가 없는 자산 ${d.total.toLocaleString()}대 · 모델 ${d.models}종</h3>
      <p class="muted">제품코드를 넣어야 셋팅/QC 화면에서 재고로 잡힙니다.
        같은 모델은 대개 같은 코드이니 모델을 열어 한 번에 넣으세요.
        <b>${escapeHtml((d.excluded || []).join(" · "))}</b> 상태는 대상에서 빠집니다.</p>
      ${tierHelpBox()}
      <div class="inline-row">
        <input type="text" id="uc-find" placeholder="모델 검색" style="min-width:200px;">
        <span class="muted" id="uc-picked">선택 0대</span>
      </div>
    </div>
    <div id="uc-groups"></div>`;

  const draw = () => {
    const kw = ($("#uc-find").value || "").trim().toUpperCase();
    const gs = d.groups.filter((g) => !kw || g.model.toUpperCase().includes(kw));
    $("#uc-groups").innerHTML = gs.map((g) => {
      const open = !!state.uncodedOpen[g.model];
      const chips = (obj, cls) => Object.entries(obj)
        .map(([k, n]) => `<span class="chip ${cls}">${escapeHtml(k)} ${n}</span>`).join(" ");
      const pgU = paged(g.assets, "uc:" + g.model);
      return `<div class="card uc-group" data-model="${escapeHtml(g.model)}">
        <div class="inline-row uc-head" style="cursor:pointer;">
          <b style="font-size:16px;">${escapeHtml(g.model)}</b>
          <span class="chip chip-blue">${g.count}대</span>
          ${g.maker ? `<span class="muted">${escapeHtml(g.maker)}</span>` : ""}
          <span style="flex:1"></span>
          ${chips(g.byStatus, "chip-slate")}
          <span class="muted">${open ? "▲ 접기" : "▼ 펼치기"}</span>
        </div>
        ${open ? `
        <div class="inline-row" style="margin:10px 0 6px; gap:6px; flex-wrap:wrap;">
          <input type="text" class="uc-code" placeholder="제품코드 (예: 840 G3_i7-6_내장)"
            style="min-width:230px; padding:6px 9px; border:1px solid var(--border);
                   border-radius:8px; background:var(--bg);">
          <select class="uc-grade"><option value="">등급 그대로</option>
            ${(state.meta.grades || []).map((x) => `<option>${escapeHtml(x)}</option>`).join("")}</select>
          <select class="uc-tier" title="${escapeHtml(tierHelpText())}">
            <option value="">재고 구분 그대로</option>${tierOptions("")}</select>
          <button class="btn btn-sm btn-primary uc-apply">선택분에 적용</button>
          <span style="flex:1"></span>
          <label class="check-line" style="font-size:13px;">
            <input type="checkbox" class="uc-all"> 이 모델 전체</label>
        </div>
        <div class="table-wrap"><table><thead><tr>
          <th style="width:34px;"></th><th>관리번호</th><th>시리얼</th><th>등급</th>
          <th>재고 구분</th><th>상태</th><th>스펙</th></tr></thead>
          <tbody>${pgU.rows.map((a) => `<tr>
            <td><input type="checkbox" class="uc-pick" data-aid="${a.id}"></td>
            <td><button class="link-btn" data-ucopen="${a.id}">${escapeHtml(a.assetNo)}</button></td>
            <td class="muted">${escapeHtml(a.serial || "-")}</td>
            <td>${escapeHtml(a.grade)}</td>
            <td>${escapeHtml(a.tier || "양품")}</td>
            <td>${escapeHtml(a.statusLabel)}</td>
            <td class="muted" style="font-size:12px;">${escapeHtml(
              [a.cpu, a.ram, a.ssd].filter(Boolean).join(" / "))}</td>
          </tr>`).join("")}</tbody></table></div>${pgU.bar}` : ""}
      </div>`;
    }).join("") || `<div class="card placeholder"><p>검색 결과가 없습니다.</p></div>`;
    bind();
  };

  const countPicked = () => {
    const n = $$("input.uc-pick").filter((c) => c.checked).length;
    $("#uc-picked").textContent = `선택 ${n}대`;
  };

  const bind = () => {
    $$(".uc-head").forEach((h) => h.addEventListener("click", () => {
      const m = h.closest(".uc-group").dataset.model;
      state.uncodedOpen[m] = !state.uncodedOpen[m];
      draw();
    }));
    // Shift 다중선택 — 모델 묶음 '안에서만' 이어진다(번지면 남의 모델에 코드가 들어간다)
    $$(".uc-group").forEach((card) => {
      const boxes = $$("input.uc-pick", card);
      attachShiftPick(card, "input.uc-pick", countPicked);
      wirePager(card, "uc:" + card.dataset.model, draw);   // 묶음 안 화살표
      const all = $(".uc-all", card);
      if (all) all.addEventListener("change", () => {
        boxes.forEach((cb) => { cb.checked = all.checked; });
        countPicked();
      });
      // 묶음마다 코드칸이 있어 id를 붙여 자동완성을 건다
      const codeIn = $(".uc-code", card);
      if (codeIn && !codeIn.id) {
        codeIn.id = "uc-code-" + (card.dataset.model || "").replace(/[^\w가-힣]/g, "_");
        attachCodeLookup(codeIn.id);
      }
      const btn = $(".uc-apply", card);
      if (btn) btn.addEventListener("click", () => applyUncoded(card, btn));
    });
    // 관리번호를 누르면 그 자산만 따로 연다 — 개별 수정(등급·스펙·코드) 경로
    $$("button[data-ucopen]").forEach((b) => b.addEventListener("click", () => {
      if (!$("#uc-detail")) {
        const host = document.createElement("div");
        host.id = "uc-detail";
        body.appendChild(host);
      }
      state.assetDetailId = Number(b.dataset.ucopen);
      // ★'#'이 빠지면 querySelector가 <uc-detail> 태그를 찾아 항상 null이 되고,
      //   renderAssetDetail이 조용히 아무 것도 안 한다(자산번호를 눌러도 반응 없음).
      renderAssetDetail("#uc-detail", () => renderUncoded(body));
    }));
    countPicked();
  };

  $("#uc-find").addEventListener("input", draw);
  draw();
}

async function applyUncoded(card, btn) {
  const ids = $$("input.uc-pick", card).filter((c) => c.checked).map((c) => Number(c.dataset.aid));
  if (!ids.length) { toast("자산을 먼저 선택하세요.", true); return; }
  const pc = $(".uc-code", card).value.trim();
  const grade = $(".uc-grade", card).value;
  const tier = $(".uc-tier", card).value;
  if (!pc && !grade && !tier) { toast("제품코드나 등급·재고 구분을 입력하세요.", true); return; }
  const what = [pc && `제품코드 ${pc}`, grade && `등급 ${grade}`, tier && `재고 구분 ${tier}`]
    .filter(Boolean).join(" · ");
  if (!confirm(`선택한 ${ids.length}대에 적용합니다.

  ${what}

계속할까요?`)) return;
  btn.disabled = true;
  try {
    const payload = { ids };
    if (pc) payload.productCode = pc;
    if (grade) payload.grade = grade;
    if (tier) payload.tier = tier;
    const r = await api("/api/assets/product-code", { method: "POST", body: payload });
    toast(`${r.ok}대에 적용했습니다.` + (r.skipped.length ? ` (건너뜀 ${r.skipped.length}대)` : ""));
    // ★#tab-body는 설정 화면의 id다. 매입은 #aview-body(자산 탭 안)라, 예전 코드는
    //   fallback(card.parentElement=#uc-groups)으로 떨어져 제목 카드가 안 바뀌었다.
    //   그래서 코드를 채워도 '남은 대수'가 그대로여서 진척이 안 보였다(2026-08-04 감사).
    renderUncoded($("#aview-body") || $("#ptab-body") || card.parentElement);
    fillAssetBadges($("#ptab-body") || document);   // 배지도 같이 줄여 준다
  } catch (err) { toast(err.message, true); btn.disabled = false; }
}

/* ---------------- 거래처 ---------------- */

async function renderSuppliers(body) {
  const seq = ++state.renderSeq;
  let suppliers, cats;
  try {
    [suppliers, cats] = await Promise.all([api("/api/suppliers"), api("/api/categories")]);
    if (seq !== state.renderSeq) return;
    state.suppliers = suppliers;
    state.categories = cats;
  } catch (err) {
    if (seq === state.renderSeq) body.innerHTML = `<p class="muted">${escapeHtml(err.message)}</p>`;
    return;
  }
  body.innerHTML = `
    <div class="card">
      <h3>거래처</h3>
      <div class="inline-row">
        <input type="text" id="sp-name" placeholder="거래처명 *">
        <input type="text" id="sp-contact" placeholder="담당자">
        <input type="text" id="sp-phone" placeholder="연락처">
        <input type="text" id="sp-memo" placeholder="메모" style="flex:1; min-width:120px;">
        <button class="btn btn-sm btn-primary" id="sp-add">추가</button>
      </div>
      <div class="table-wrap"><table>
        <thead><tr><th>거래처</th><th>담당자</th><th>연락처</th><th>매입 횟수</th><th>누적 매입금액</th><th>메모</th><th></th></tr></thead>
        <tbody>${suppliers.map((s) => `
          <tr class="${s.enabled ? "" : "muted"}"><td><b>${escapeHtml(s.name)}</b></td><td>${escapeHtml(s.contact)}</td>
          <td>${escapeHtml(s.phone)}</td><td>${s.batchCount}회</td><td>${fmtWon(s.totalAmount)}</td>
          <td class="muted">${escapeHtml(s.memo)}</td>
          <td><button class="btn btn-sm" data-spedit="${s.id}">수정</button></td></tr>`).join("")
          || `<tr><td colspan="7" class="muted">거래처가 없습니다.</td></tr>`}
        </tbody></table></div>
    </div>
    <div class="card">
      <h3>제품 분류</h3>
      <p class="muted">자산을 등록할 때 고르는 분류입니다(TMS의 대분류에 해당).</p>
      <div class="inline-row">
        <input type="text" id="cat-new" placeholder="새 분류 이름">
        <button class="btn btn-sm btn-primary" id="cat-add">추가</button>
      </div>
      <div class="table-wrap"><table>
        <thead><tr><th style="width:80px;">순서</th><th>이름</th><th>등록 자산</th><th>상태</th><th></th></tr></thead>
        <tbody>${cats.map((c, i) => `
          <tr>
            <td>
              <button class="btn btn-ghost btn-sm" data-catup="${c.id}" ${i === 0 ? "disabled" : ""}>▲</button>
              <button class="btn btn-ghost btn-sm" data-catdown="${c.id}" ${i === cats.length - 1 ? "disabled" : ""}>▼</button>
            </td>
            <td><b>${escapeHtml(c.name)}</b></td>
            <td class="muted" id="cat-count-${c.id}">-</td>
            <td>${c.enabled ? '<span class="chip chip-green">사용 중</span>' : '<span class="chip chip-slate">비활성</span>'}</td>
            <td>
              <button class="btn btn-sm" data-catrename="${c.id}">이름 변경</button>
              <button class="btn btn-sm" data-cattoggle="${c.id}">${c.enabled ? "비활성화" : "활성화"}</button>
            </td>
          </tr>`).join("") || `<tr><td colspan="5" class="muted">분류가 없습니다.</td></tr>`}
        </tbody></table></div>
    </div>`;
  // 분류별 자산 수 표시
  api("/api/assets/summary").then((rows) => {
    const cnt = {};
    rows.forEach((r) => { if (r.categoryId != null) cnt[r.categoryId] = (cnt[r.categoryId] || 0) + r.count; });
    cats.forEach((c) => {
      const el = document.getElementById(`cat-count-${c.id}`);
      if (el) el.textContent = (cnt[c.id] || 0) + "대";
    });
  }).catch(() => {});
  $("#cat-add").addEventListener("click", async () => {
    const name = $("#cat-new").value.trim();
    if (!name) return;
    try { await api("/api/categories", { method: "POST", body: { name } }); toast("추가했습니다."); renderSuppliers(body); }
    catch (err) { toast(err.message, true); }
  });
  $$("button[data-catrename]", body).forEach((b) => b.addEventListener("click", async () => {
    const c = cats.find((x) => x.id === Number(b.dataset.catrename));
    const name = prompt("새 이름", c.name);
    if (!name || name.trim() === c.name) return;
    try { await api(`/api/categories/${c.id}`, { method: "PATCH", body: { name: name.trim() } }); renderSuppliers(body); }
    catch (err) { toast(err.message, true); }
  }));
  $$("button[data-cattoggle]", body).forEach((b) => b.addEventListener("click", async () => {
    const c = cats.find((x) => x.id === Number(b.dataset.cattoggle));
    try { await api(`/api/categories/${c.id}`, { method: "PATCH", body: { enabled: !c.enabled } }); renderSuppliers(body); }
    catch (err) { toast(err.message, true); }
  }));
  const moveCat = async (id, dir) => {
    const idx = cats.findIndex((x) => x.id === id);
    if (idx < 0 || !cats[idx + dir]) return;
    const ids = cats.map((x) => x.id);
    [ids[idx], ids[idx + dir]] = [ids[idx + dir], ids[idx]];
    try { await api("/api/categories/reorder", { method: "POST", body: { ids } }); renderSuppliers(body); }
    catch (err) { toast(err.message, true); }
  };
  $$("button[data-catup]", body).forEach((b) => b.addEventListener("click", () => moveCat(Number(b.dataset.catup), -1)));
  $$("button[data-catdown]", body).forEach((b) => b.addEventListener("click", () => moveCat(Number(b.dataset.catdown), +1)));
  $("#sp-add").addEventListener("click", async () => {
    try {
      await api("/api/suppliers", { method: "POST", body: {
        name: $("#sp-name").value.trim(), contact: $("#sp-contact").value,
        phone: $("#sp-phone").value, memo: $("#sp-memo").value,
      } });
      toast("거래처를 추가했습니다.");
      renderSuppliers(body);
    } catch (err) { toast(err.message, true); }
  });
  $$("button[data-spedit]", body).forEach((b) => b.addEventListener("click", async () => {
    const s = suppliers.find((x) => x.id === Number(b.dataset.spedit));
    const name = prompt("거래처명", s.name); if (name == null) return;
    const contact = prompt("담당자", s.contact); if (contact == null) return;
    const phone = prompt("연락처", s.phone); if (phone == null) return;
    try {
      await api(`/api/suppliers/${s.id}`, { method: "PATCH", body: { name: name.trim(), contact, phone } });
      renderSuppliers(body);
    } catch (err) { toast(err.message, true); }
  }));
}

/* ---------------- TMS 이관 ---------------- */

/* TMS 자동 반영 현황 — 대표가 엑셀만 폴더에 떨궈 두면 HMS가 알아서 넣는다.
   내보내기까지 자동화하지 않는 이유는 autosync.py 주석 참고(TMS를 건드리지 않기 위해). */
async function renderAutoSync() {
  const host = $("#mg-auto-body");
  if (!host) return;
  let d;
  try { d = await api("/api/tms-sync/status"); }
  catch (err) { host.innerHTML = `<span class="muted">${escapeHtml(err.message)}</span>`; return; }
  const last = (d.history || [])[0];
  host.innerHTML = `
    <p class="muted" style="margin:0 0 8px;">
      TMS에서 내보낸 엑셀을 <b>${escapeHtml(d.folder)}</b> 에 저장해 두면
      <b>${d.intervalMinutes}분마다</b> 확인해 새 파일만 반영합니다.
      이미 넣은 파일은 다시 넣지 않고, <b>값이 있는 칸은 건드리지 않습니다.</b></p>
    <div class="inline-row" style="margin-bottom:8px;">
      <button class="btn btn-sm" id="mg-auto-now">지금 반영</button>
      <button class="btn btn-sm btn-ghost" id="mg-auto-refresh">새로고침</button>
      ${d.waiting && d.waiting.length
        ? `<span class="chip chip-violet">대기 ${d.waiting.length}개: ${escapeHtml(d.waiting.slice(0,3).join(", "))}</span>`
        : `<span class="chip chip-green">대기 없음 — 최신</span>`}
    </div>
    ${last ? `<div class="muted" style="font-size:12px;">마지막 반영 ${escapeHtml((last.syncedAt||"").slice(0,16).replace("T"," "))}</div>` : ""}
    ${(d.history || []).length ? `<div class="table-wrap"><table>
      <thead><tr><th>파일</th><th style="text-align:right;">읽은 행</th><th style="text-align:right;">신규</th>
        <th style="text-align:right;">채움</th><th style="text-align:right;">오류</th><th>시각</th></tr></thead>
      <tbody>${d.history.map((h) => `<tr>
        <td>${escapeHtml(h.file)}</td>
        <td style="text-align:right;">${(h.rows||0).toLocaleString()}</td>
        <td style="text-align:right;">${(h.created||0).toLocaleString()}</td>
        <td style="text-align:right;">${(h.updated||0).toLocaleString()}</td>
        <td style="text-align:right;">${h.errors ? `<span class="chip chip-red">${h.errors}</span>` : "0"}</td>
        <td class="muted">${escapeHtml((h.syncedAt||"").slice(0,16).replace("T"," "))}</td></tr>`).join("")}
      </tbody></table></div>` : `<p class="muted">아직 자동 반영된 파일이 없습니다.</p>`}`;
  $("#mg-auto-refresh").addEventListener("click", renderAutoSync);
  $("#mg-auto-now").addEventListener("click", async () => {
    const b = $("#mg-auto-now");
    b.disabled = true; b.textContent = "반영 중…";
    try {
      const r = await api("/api/tms-sync/run", { method: "POST", body: {} });
      toast(r.applied ? `${r.applied}개 파일 반영 완료` : "새로 반영할 파일이 없습니다.");
    } catch (err) { toast(err.message, true); }
    b.disabled = false; b.textContent = "지금 반영";
    renderAutoSync();
  });
}

/* ---------------- 판매 전표 (TMS 이관분) ----------------

   TMS 판매전표는 '고객 주문 1건'이 아니라 '하루치 채널별 묶음'이다
   (S260804-001 하나에 방문구매 26대). 그래서 주문관리가 아니라 여기에 둔다.
   자산별로 누가 사 갔는지는 자산 상세의 이력에 남아 있다. */

function renderSaleSlips(body) {
  const seq = ++state.renderSeq;
  const f = state.saleSlipFilter || (state.saleSlipFilter = {
    q: "", from: "", to: "", channel: "", unpaid: false });
  body.innerHTML = `<div class="card placeholder"><p>불러오는 중…</p></div>`;

  const load = async () => {
    const qs = new URLSearchParams();
    if (f.q) qs.set("q", f.q);
    if (f.from) qs.set("from", f.from);
    if (f.to) qs.set("to", f.to);
    if (f.channel) qs.set("channel", f.channel);
    if (f.unpaid) qs.set("unpaid", "1");
    let d;
    try {
      d = await api("/api/sale-slips?" + qs.toString());
    } catch (err) {
      body.innerHTML = `<div class="card"><p class="muted">${escapeHtml(err.message)}</p></div>`;
      return;
    }
    if (seq !== state.renderSeq) return;   // 탭을 옮겼으면 늦게 온 응답을 그리지 않는다
    draw(d);
  };

  const draw = (d) => {
    const t = d.total;
    const pgSS = paged(d.slips, "saleslips");
    const chip = (label, val, cls) =>
      `<span class="chip ${cls || "chip-slate"}">${label} ${val}</span>`;
    body.innerHTML = `
      <div class="card">
        <div class="inline-row" style="gap:8px; flex-wrap:wrap; align-items:center;">
          <b style="font-size:16px;">판매 전표 ${t.count.toLocaleString("ko-KR")}건</b>
          ${chip("판매수량", t.qty.toLocaleString("ko-KR") + "대", "chip-blue")}
          ${chip("판매금액", fmtWon(t.sale), "chip-green")}
          ${chip("매입금액", fmtWon(t.purchase))}
          ${chip("순이익", fmtWon(t.profit))}
          ${t.vat ? chip("부가세", fmtWon(t.vat)) : ""}
          ${t.fee ? chip("수수료", fmtWon(t.fee)) : ""}
          ${t.shipping ? chip("배송비", fmtWon(t.shipping)) : ""}
        </div>
        ${(d.excluded || []).length ? `
        <p style="margin:8px 0 0; font-size:13px;">
          위 금액에서 뺀 것 — ${d.excluded.map((e) =>
            `<span class="chip ${e.stage === "판매취소" ? "chip-red" : "chip-violet"}">${
              escapeHtml(e.stage)} ${e.count}건 ${fmtWon(e.sale)}</span>`).join(" ")}
        </p>` : ""}
        <p class="muted" style="margin:8px 0 0;">
          TMS에서 넘어온 판매 전표입니다. 전표 하나가 그날 그 채널의 판매 묶음이라
          자산 여러 대가 들어 있습니다 — 어떤 자산이 누구에게 나갔는지는 자산 상세의 이력에 있습니다.
          ${d.capped ? "" : `표에 ${(d.shown || 0).toLocaleString("ko-KR")}줄이 모두 나와 있습니다.`}</p>
      </div>

      <div class="card">
        <div class="inline-row" style="gap:6px; flex-wrap:wrap;">
          <input type="text" id="ss-q" placeholder="전표번호 · 거래처 · 채널" value="${escapeHtml(f.q)}"
            style="min-width:200px; padding:6px 9px; border:1px solid var(--border);
                   border-radius:8px; background:var(--bg);">
          <input type="date" id="ss-from" value="${escapeHtml(f.from)}">
          <span class="muted">~</span>
          <input type="date" id="ss-to" value="${escapeHtml(f.to)}">
          <select id="ss-channel"><option value="">채널 전체</option>
            ${d.channels.map((c) =>
              `<option ${c === f.channel ? "selected" : ""}>${escapeHtml(c)}</option>`).join("")}</select>
          <label class="check-line" style="font-size:13px;">
            <input type="checkbox" id="ss-unpaid" ${f.unpaid ? "checked" : ""}> 입금 미확인만</label>
          <button class="btn btn-sm btn-primary" id="ss-go">조회</button>
          <button class="btn btn-sm" id="ss-reset">초기화</button>
        </div>
        ${hasPerm("purchase.edit") ? `
        <div class="inline-row" style="gap:6px; margin-top:10px; padding-top:10px;
             border-top:1px dashed var(--border); flex-wrap:wrap;">
          <span class="muted" style="font-size:13px;">TMS 판매내역 · 판매미수금관리 엑셀 올리기</span>
          <input type="file" id="ss-file" multiple accept=".xlsx,.xls">
          <button class="btn btn-sm" id="ss-upload">가져오기</button>
          <span id="ss-upmsg" class="muted" style="font-size:13px;"></span>
        </div>` : ""}
      </div>

      ${d.capped ? `<div class="card" style="border-left:4px solid var(--warn);">
        <p style="margin:0;">조건에 맞는 전표 <b>${(d.allCount || 0).toLocaleString("ko-KR")}건</b> 중
        최근 <b>500건</b>만 표에 보입니다. <b>위 합계 금액은 ${(d.allCount || 0).toLocaleString("ko-KR")}건 전부</b>를
        더한 값입니다 — 표 줄 수와 달라도 정상입니다.<br>
        전부 보시려면 기간을 좁히거나 채널을 골라 주세요.</p></div>` : ""}

      <div class="card">
        <div class="table-wrap"><table>
          <thead><tr>
            <th>전표</th><th>판매일</th><th>채널</th><th>거래처</th>
            <th style="text-align:right;">대수</th>
            <th style="text-align:right;">판매금액</th>
            <th style="text-align:right;">명세합계</th>
            <th style="text-align:right;">차액</th>
            <th style="text-align:right;">매입</th>
            <th style="text-align:right;">순이익</th>
            <th>상태</th><th>입금</th>
          </tr></thead>
          <tbody>${pgSS.rows.map((s) => `
            <tr>
              <td><b>${escapeHtml(s.slipNo)}</b></td>
              <td>${escapeHtml(s.saleDate || "-")}</td>
              <td>${escapeHtml(s.channel || "-")}</td>
              <td>${escapeHtml(s.customer || "-")}</td>
              <td style="text-align:right;">${s.qty || 0}</td>
              <td style="text-align:right;">${fmtWon(s.saleAmount)}</td>
              <td style="text-align:right;" class="muted">${fmtWon(s.itemSaleSum)}</td>
              <td style="text-align:right;" class="${s.diffAmount ? "" : "muted"}">${fmtWon(s.diffAmount)}</td>
              <td style="text-align:right;" class="muted">${fmtWon(s.purchaseAmount)}</td>
              <td style="text-align:right;">${fmtWon(s.profit)}</td>
              <td>${s.stage ? `<span class="chip ${
                s.stage === "판매취소" ? "chip-red" : s.stage === "반입" ? "chip-violet" : "chip-green"
              }">${escapeHtml(s.stage)}</span>` : "-"}</td>
              <td>${s.paidConfirmed
                ? `<span class="chip chip-green">${escapeHtml(s.paidStatus || "완납")}</span>`
                : `<span class="chip chip-red">${escapeHtml(s.paidStatus || "미확인")}</span>`}</td>
            </tr>`).join("") || `<tr><td colspan="12" class="muted">조회된 전표가 없습니다.</td></tr>`}
          </tbody>
        </table></div>
        ${pgSS.bar}
      </div>`;
    wirePager(body, "saleslips", () => draw(d));

    const apply = () => {
      f.q = $("#ss-q").value.trim();
      f.from = $("#ss-from").value;
      f.to = $("#ss-to").value;
      f.channel = $("#ss-channel").value;
      f.unpaid = $("#ss-unpaid").checked;
      resetPage("saleslips");
      load();
    };
    $("#ss-go").addEventListener("click", apply);
    autoSearch("#ss-q", apply);
    $("#ss-reset").addEventListener("click", () => {
      state.saleSlipFilter = { q: "", from: "", to: "", channel: "", unpaid: false };
      renderSaleSlips(body);
    });

    const up = $("#ss-upload");
    if (up) up.addEventListener("click", async () => {
      const files = $("#ss-file").files;
      if (!files.length) { toast("엑셀 파일을 고르세요.", true); return; }
      const fd = new FormData();
      for (const f of files) fd.append("files", f);
      up.disabled = true;
      $("#ss-upmsg").textContent = "읽는 중…";
      try {
        const r = await fetch("/api/sale-slips/import", { method: "POST", body: fd });
        const d = await r.json();
        if (!r.ok) throw new Error(d.error || "가져오지 못했습니다.");
        // ★한 건도 안 들어갔으면 그렇다고 말해야 한다 — 예전 이관 화면은
        //   '오류 0건'만 띄워서 성공한 줄 알고 넘어갔다(2026-08-04 감사).
        const skipped = (d.files || []).filter((f) => f.skipped);
        $("#ss-upmsg").textContent =
          `신규 ${d.created}건 · 채움 ${d.updated}건 · 그대로 ${d.skipped}건`
          + (skipped.length ? ` / 건너뜀: ${skipped.map((f) => f.name).join(", ")}` : "");
        if (!d.created && !d.updated) toast("새로 들어간 전표가 없습니다.", true);
        else { toast(`전표 ${d.created + d.updated}건을 반영했습니다.`); renderSaleSlips(body); }
      } catch (err) {
        $("#ss-upmsg").textContent = "";
        toast(err.message, true);
      }
      up.disabled = false;
    });
  };

  load();
}

function renderMigrate(body) {
  body.innerHTML = `
    <div class="card" style="max-width:820px;">
      <h3>TMS 자산 데이터 이관</h3>
      <p class="muted">TMS에서 매입내역/재고를 엑셀로 내보낸 뒤 올리면 관리번호와 스펙을 그대로 가져옵니다.
      이미 등록된 관리번호는 건너뛰므로 여러 번 실행해도 중복되지 않습니다.</p>
      <label class="check-line" style="margin-top:8px;">
        <input type="checkbox" id="mg-fill">
        <span><b>이미 있는 자산의 빈 칸 채우기</b>
          <span class="muted">— 관리번호만 먼저 등록돼 매입가·스펙이 비어 있는 자산에 엑셀 값을 넣습니다.
          <b>이미 값이 있는 칸은 건드리지 않습니다.</b></span></span>
      </label>
      <p class="muted">TMS가 등급 칸에 넣어둔 <b>수리·A/S·불량·도색대기</b>는 HMS에서 자산 <b>상태</b>로 옮기고,
      등급은 '미정'으로 두면서 원본 값을 특이사항에 남깁니다.</p>
      <div class="inline-row" style="margin-top:10px;">
        <button class="btn btn-sm" id="mg-template">📄 엑셀 양식 받기</button>
      </div>
      <div class="inline-row">
        <input type="file" id="mg-files" multiple accept=".xlsx,.xls">
        <button class="btn btn-sm" id="mg-preview">미리보기</button>
        <button class="btn btn-sm btn-primary" id="mg-run">이관 실행</button>
      </div>
      <div id="mg-result" class="muted" style="margin-top:10px;"></div>
    </div>
    <div class="card" style="max-width:820px;" id="mg-auto">
      <h3>자동 반영 <span class="muted" style="font-weight:400;">— 폴더에 새 엑셀이 들어오면 알아서 최신화</span></h3>
      <div id="mg-auto-body" class="muted">불러오는 중…</div>
    </div>`;
  renderAutoSync();
  $("#mg-template").addEventListener("click", () => window.open("/api/assets/migrate/template", "_blank"));
  const send = async (path) => {
    const files = $("#mg-files").files;
    if (!files.length) { toast("엑셀 파일을 선택하세요.", true); return; }
    const fd = new FormData();
    for (const f of files) fd.append("files", f);
    if ($("#mg-fill") && $("#mg-fill").checked) fd.append("fillBlanks", "1");
    $("#mg-preview").disabled = $("#mg-run").disabled = true;
    try {
      const res = await fetch(path, { method: "POST", body: fd });
      const data = await res.json().catch(() => null);
      if (!res.ok) throw new Error((data && data.error) || `요청 실패 (${res.status})`);
      const isPreview = path.includes("preview");
      $("#mg-result").innerHTML = `
        <b>${isPreview ? "미리보기" : "이관 완료"}</b> — 읽은 행 ${data.parsed}건 ·
        <b style="color:var(--primary)">${isPreview ? "등록 예정" : "등록됨"} ${isPreview ? data.toCreate : data.created}건</b> ·
        ${data.fillBlanks ? `· <b style="color:var(--primary)">${isPreview ? "채울 예정" : "채움"} ${isPreview ? data.toUpdate : data.updated}건</b>` : ""}
        · 이미 있음(변경 없음) ${data.duplicates}건 · 오류 ${data.errorCount}건
        ${data.fillBlanks && data.fillPurchaseSum ? `<div style="margin-top:4px;">채워질 매입가 합계 <b>${fmtWon(data.fillPurchaseSum)}</b></div>` : ""}
        ${data.errors && data.errors.length ? `<br><span style="color:var(--danger)">${data.errors.map(escapeHtml).join("<br>")}</span>` : ""}
        ${isPreview && data.updateSample && data.updateSample.length ? `<div class="table-wrap" style="margin-top:8px;">
          <div class="muted" style="font-size:12px;">채워질 자산(최대 20건)</div>
          <table><thead><tr><th>관리번호</th><th>채울 항목</th><th style="text-align:right;">매입가</th></tr></thead>
          <tbody>${data.updateSample.map((u) => `<tr><td>${escapeHtml(u.assetNo)}</td>
            <td class="muted">${escapeHtml(u.fields)}</td>
            <td style="text-align:right;">${u.purchasePrice ? fmtWon(u.purchasePrice) : "-"}</td></tr>`).join("")}</tbody></table></div>` : ""}
        ${isPreview && data.sample && data.sample.length ? `<div class="table-wrap" style="margin-top:8px;"><table>
          <thead><tr><th>관리번호</th><th>브랜드</th><th>모델</th><th>등급</th><th>상태</th><th>스펙</th></tr></thead>
          <tbody>${data.sample.map((s) => `<tr><td>${escapeHtml(s.assetNo)}</td><td>${escapeHtml(s.maker)}</td>
            <td>${escapeHtml(s.model)}</td><td>${escapeHtml(s.grade)}</td><td>${escapeHtml(statusLabel(s.status))}</td>
            <td class="muted">${escapeHtml(s.spec)}</td></tr>`).join("")}</tbody></table></div>` : ""}`;
      if (!isPreview) {
        toast(`자산 ${data.created}대 등록` + (data.updated ? ` · ${data.updated}대 보완 완료` : " 완료"));
      }
    } catch (err) { toast(err.message, true); }
    finally { $("#mg-preview").disabled = $("#mg-run").disabled = false; }
  };
  $("#mg-preview").addEventListener("click", () => send("/api/assets/migrate/preview"));
  $("#mg-run").addEventListener("click", () => {
    const fill = $("#mg-fill") && $("#mg-fill").checked;
    const msg = fill
      ? ["엑셀의 자산을 등록하고, 이미 있는 자산의 빈 칸도 채웁니다.",
         "(이미 값이 있는 칸은 그대로 둡니다)", "", "계속할까요?"].join("\n")
      : "엑셀의 자산을 HMS에 등록합니다. 계속할까요?";
    if (!confirm(msg)) return;
    send("/api/assets/migrate");
  });
  ensureMeta();
}
