/* OWS 매입 — 재고현황 / 자산목록·상세(스펙·이력) / 가입고·매입 전표 / 거래처 / TMS 이관
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

function fmtWon(n) {
  // ★null = 서버가 '금액 열람 권한 없음'으로 가린 값(2026-08-27) — 0원으로 속이지 않는다.
  //   undefined(칸 자체가 없음)는 예전처럼 0원.
  if (n === null) return "•••";
  return (n == null ? 0 : n).toLocaleString("ko-KR") + "원";
}

/* 금액 칸 — 천단위 콤마(대표 2026-08-14: "20000원 → 20,000원").
   ★쓰는 법: 금액 input에 data-money 를 달고 초기값은 fmtNum()으로 넣는다.
     제출할 때는 기존대로 콤마를 떼고 보낸다(모든 제출 경로가 이미 그렇게 한다).
   ★핸들러는 document에 '한 번만' 건다 — 화면을 다시 그려도 새 칸에 자동으로 붙는다
     (칸마다 리스너를 달면 다시 그릴 때마다 쌓여 30분이면 화면이 멎는다 — 2026-07-29 사고). */
function fmtNum(n) { return (Number(n) || 0).toLocaleString("ko-KR"); }

/* '재고반영'은 사내 재고와 무관하다 — 쇼핑몰에 재고 수를 밀어넣을 때의 전송 대상 표시다.
   사내 재고는 제품코드 + 상태 + 재고구분(가재고 제외)으로 이미 정해진다.
   그래서 몰 재고 연동을 켠 몰이 없으면 관련 버튼·칸을 통째로 감춘다
   (대표 2026-08-17: "재고 구분으로 재고가 정해지는데 재고반영 버튼이 왜 필요하냐").
   나중에 설정에서 몰 재고 연동을 켜면 자동으로 다시 나타난다 — 기능·데이터는 그대로 둔다. */
function stockSyncOn() { return !!(state.meta && state.meta.stockSyncOn); }

/* ★콤마 위에서 지우면 옆 숫자까지 함께 지운다(2026-08-14 검토).
   안 그러면 지워진 게 콤마뿐이라 아래 리스너가 콤마를 되살리고 커서까지 되돌려
   "백스페이스를 눌러도 아무 일이 없는" 제자리걸음이 된다. */
document.addEventListener("beforeinput", (e) => {
  const el = e.target;
  if (!el || typeof el.matches !== "function" || !el.matches("input[data-money]")) return;
  if (el.selectionStart !== el.selectionEnd) return;      // 범위 선택은 기본 동작이 맞다
  const v = String(el.value || "");
  const pos = el.selectionStart;
  let cut = null;
  if (e.inputType === "deleteContentBackward" && v[pos - 1] === ",") cut = [pos - 2, pos, pos - 2];
  else if (e.inputType === "deleteContentForward" && v[pos] === ",") cut = [pos, pos + 2, pos];
  if (!cut || cut[0] < 0) return;
  e.preventDefault();
  el.value = v.slice(0, cut[0]) + v.slice(cut[1]);
  el.selectionStart = el.selectionEnd = cut[2];
  el.dispatchEvent(new Event("input", { bubbles: true }));  // 콤마 재표기는 아래가 한다
});

document.addEventListener("input", (e) => {
  const el = e.target;
  if (!el || typeof el.matches !== "function" || !el.matches("input[data-money]")) return;
  const digits = String(el.value || "").replace(/[^0-9]/g, "");
  const next = digits ? Number(digits).toLocaleString("ko-KR") : "";
  if (next === el.value) return;
  // 커서가 끝에 있었으면 끝에 붙여 둔다(중간 편집은 콤마가 늘어난 만큼 밀어 준다)
  const atEnd = el.selectionStart === el.value.length;
  const before = el.value.length;
  const pos = el.selectionStart;
  el.value = next;
  if (atEnd) el.selectionStart = el.selectionEnd = next.length;
  else {
    const shift = next.length - before;
    const p = Math.max(0, Math.min(next.length, pos + shift));
    el.selectionStart = el.selectionEnd = p;
  }
});

function statusLabel(code) {
  const s = (state.meta && state.meta.statuses || []).find((x) => x.code === code);
  return s ? s.label : code;
}
function statusChip(code) {
  return `<span class="chip ${ASSET_STATUS_CHIP[code] || "chip-slate"}">${escapeHtml(statusLabel(code))}</span>`;
}
/* 렌탈 자산의 '진짜 상태' — OWS status 로는 알 수 없다(대표 2026-09-01).
   RMS가 밀어 넣어 둔 사본(rms_inventory)의 상태를 쓴다. 사본이 없으면 그냥 '렌탈'.
     rented  = 고객이 쓰는 중   holding = 예약(출고 준비)   available = 창고에 있음 */
const RMS_STATE = {
  rented: ["렌탈 대여중", "#fef3c7", "#92400e", "#fcd34d"],
  holding: ["렌탈 예약", "#fef3c7", "#92400e", "#fcd34d"],
  available: ["렌탈 보유", "#eef2ff", "#3730a3", "#c7d2fe"],
};
function rentalStateChip(a) {
  const [label, bg, fg, bd] = RMS_STATE[a.rmsStatus] || ["렌탈", "#eef2ff", "#3730a3", "#c7d2fe"];
  const tip = a.rmsStatus
    ? "렌탈 사업부(RMS)가 이 자산을 이렇게 보고 있습니다. 판매 재고가 아닙니다."
    : "렌탈 사업부(RMS) 귀속입니다. RMS에 실물 기록이 없어 상세 상태는 모릅니다.";
  return `<span class="chip" title="${tip}"
    style="background:${bg};color:${fg};border:1px solid ${bd};">${label}</span>`;
}

/* 상태 칸 — 목록·상세·전표가 같은 함수를 쓴다(따로 그리면 또 어긋난다).
   ★렌탈 자산의 OWS status 는 실상을 말해 주지 못한다(대표 2026-09-01):
     shipped(출고완료)는 TMS 이관 흔적이고, ready(판매가능)는 판매 재고에도 안 잡히니
     아예 틀린 말이다. 그래서 렌탈이면 RMS가 보는 상태를 앞세우고
     OWS status 는 '왜 그렇게 보이는지' 알 수 있게 작게 남긴다. */
function statusCell(a, extra) {
  if (a.division !== "rental") return statusChip(a.status) + (extra || "");
  return `${rentalStateChip(a)}${a.rmsRenter
      ? `<div class="muted" style="font-size:11.5px; white-space:nowrap;"
           title="렌탈 사업부(RMS)에서 이 자산을 쓰고 있는 고객입니다">🏬 ${escapeHtml(a.rmsRenter)}</div>`
      : ""}
    <div class="muted" style="font-size:11px; white-space:nowrap;"
      title="매입 시스템이 들고 있는 상태값입니다.
렌탈 자산은 이 값이 '판매가능'이어도 판매 재고에 잡히지 않습니다 — 사업부가 렌탈이라서입니다.">${escapeHtml(statusLabel(a.status))}</div>`;
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

/* 모델명을 치는 동안 그 모델의 '최근 매입 단가'를 옆에 띄운다.
   대표 요청(2026-07-30): 매입하면서 단가를 대조하러 탭을 오가는 게 번거롭다.

   ★재고 대수는 더 이상 보여주지 않는다(대표 지시 2026-08-07):
     "이름으로 유추하는 매입 제품 재고 안내는 아예 없애줘.
      앞으로 모든 재고 확인은 제품코드로 통일."
     모델명 유사검색은 '갤럭시탭 S6'에 'S6 Lite'까지 세는 식이라 숫자가 틀렸다.
     재고는 아래 제품코드 자동완성(출고가능/보유)이 코드 기준으로 보여 준다. */
let _briefTimer = null;
let _briefLast = "";

function renderModelBrief(host, b) {
  if (!host) return;
  if (!b || b.reason || !(b.recentBuys || []).length) { host.innerHTML = ""; return; }
  const recent = b.recentBuys.slice(0, 3).map((r) => `
    <div style="display:flex; gap:8px; font-size:12px; padding:2px 0;">
      <span class="muted" style="min-width:74px;">${escapeHtml(r.date || "")}</span>
      <span style="flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;">${escapeHtml(r.supplier || "-")}</span>
      <b>${fmtWon(r.price)}</b>
    </div>`).join("");
  host.innerHTML = `
    <div style="border:1px solid var(--border); border-radius:10px; padding:10px 12px;
                background:var(--bg); margin-top:6px;">
      <div class="inline-row" style="gap:6px; flex-wrap:wrap;">
        <b style="font-size:13px;">${escapeHtml(b.model)}</b>
        ${b.avgBuy ? `<span class="chip chip-slate">최근 평균 ${fmtWon(b.avgBuy)}</span>` : ""}
        <span class="muted" style="font-size:11px;">재고는 제품코드로 확인하세요</span>
      </div>
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

/* 모델 마스터 대조(2026-09-02) — 모델명을 치면 마스터에 있는지 칸 옆에 표시하고, 있으면 빈 브랜드(·아직 안 고른
   카테고리)를 채우며, 없으면 그 자리에서 [＋ 마스터에 등록]으로 올린다. 등록 자체는 막지 않는다(서버도 경고만 싣는다).
   catId 는 '사람이 아직 안 고른' select 만 맞춘다 — 수정 화면(기존 자산)에서는 null 로 넘겨 카테고리를 건드리지 않는다. */
function attachModelMaster(inputId, makerId, catId, hostId) {
  const el = $(inputId);
  const host = $(hostId);
  if (!el || !host) return;
  let last = "";
  let timer = null;
  const maker = () => (makerId ? $(makerId) : null);
  const cat = () => (catId ? $(catId) : null);
  const run = async () => {
    const name = (el.value || "").trim();
    if (name.length < 2) { host.innerHTML = ""; last = ""; return; }
    if (name === last) return;
    last = name;
    let r;
    try { r = await api(`/api/models/lookup?name=${encodeURIComponent(name)}`); }
    catch (_e) { host.innerHTML = ""; return; }        // 실패해도 입력을 막지 않는다
    if ((el.value || "").trim() !== name) return;       // 그 사이 다른 값을 쳤다
    if (r.found) {
      const m = r.model;
      const mk = maker();
      if (mk && !mk.disabled && !mk.value.trim() && m.brand) mk.value = m.brand;
      const ct = cat();
      if (ct && !ct.dataset.touched && (m.subcategory || m.category)) {
        // ★카테고리 축 = TMS 중분류(2026-09-03, A5) — 서버 complete_asset_body 와 같은 대응(버즈→웨어러블)
        const want = SUBCAT_TO_CATEGORY[m.subcategory] || SUBCAT_TO_CATEGORY[m.category] || m.category;
        const opt = [...ct.options].find((o) => o.textContent.trim() === want);
        if (opt) ct.value = opt.value;
      }
      host.innerHTML = `<span class="chip chip-green" style="font-size:11px;" title="모델 마스터에 있는 모델명입니다">✓ 마스터 ${
        escapeHtml([m.brand, [m.category, m.subcategory].filter(Boolean).join("/")].filter(Boolean).join(" · "))}${
        m.tmsDeletedAt ? " · TMS 삭제 표시" : ""}${m.enabled ? "" : " · 사용 안 함"}</span>`;
      return;
    }
    host.innerHTML = `<span class="chip chip-amber" style="font-size:11px;"
        title="등록은 됩니다. 마스터에 올려 두면 다음부터 자동완성·브랜드 보완이 됩니다">⚠ 모델 마스터에 없는 모델명</span>
      ${hasPerm("purchase.edit") ? `<button class="btn btn-ghost btn-sm" data-mm-add style="font-size:11px; padding:1px 6px;"
        title="이 모델명을 마스터에 등록합니다(브랜드·카테고리 칸의 값을 함께)">＋ 마스터에 등록</button>` : ""}`;
    const b = host.querySelector("[data-mm-add]");
    if (b) b.addEventListener("click", async () => {
      b.disabled = true;
      const mk = maker();
      const ct = cat();
      const picked = ct && ct.selectedIndex >= 0 ? ct.options[ct.selectedIndex].textContent.trim() : "";
      // 마스터는 TMS 모델분류 모양(대분류/중분류)으로 올린다 — 카테고리 이름(중분류 축)을 그대로 대분류 칸에 넣지 않는다
      const [category, subcategory] = CATEGORY_TO_MODEL_CLASS[picked] || [picked, ""];
      try {
        await api("/api/models", { method: "POST", body: { name, brand: mk ? mk.value.trim() : "", category, subcategory } });
        toast(`모델 마스터에 등록했습니다: ${name}`);
        last = "";
        run();
      } catch (err) { toast(err.message, true); b.disabled = false; }
    });
  };
  el.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(run, 350); });
  el.addEventListener("change", () => { clearTimeout(timer); run(); });
  const ct = cat();
  if (ct) ct.addEventListener("change", () => { ct.dataset.touched = "1"; });
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
  const canMoney = hasPerm("purchase.money");
  const tabs = [["slips", "매입 작업"], ["assets", "자산"],
                ...(canMoney ? [["payables", "💰 미지급금"]] : [])];
  if (canEdit) tabs.push(["base", "기준정보"]);
  // 옛 탭 이름으로 들어오면(북마크·이전 상태) 새 구조로 옮겨 준다
  // ★convert(🔧 실재고/가재고)는 없앴다(2026-08-24 — 재고 구분을 상태로 합쳤다).
  //   옛 북마크로 들어오면 빈 화면이 되므로 자산 목록으로 보낸다.
  const MOVED = { summary: ["assets", "summary"], uncoded: ["assets", "uncoded"],
                  convert: ["assets", "list"], suppliers: ["base", "suppliers"],
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
     payables: renderPayables,
     base: renderBaseTab }[state.purchaseTab])(body);
}

/* 자산 탭 — 같은 자산을 네 가지로 본다. 탭을 늘리지 않고 보기만 바꾼다.
   ★'제품코드 없음'·'가재고'는 그냥 보기가 아니라 '치워야 할 일'이라
     남은 대수를 배지로 띄워 준다. 안 보이면 영영 안 치운다. */
/* ★'uncoded' 보기가 판매불가 모아보기를 겸한다(대표 2026-08-07). 키를 안 바꾼 것은
   일부러다 — 화면·대시보드·시험 여러 곳이 이 키와 /api/assets/uncoded 주소를 알고
   있고, 오늘 실측으로 두 집합(코드 없음 / 판매불가)이 완전히 같아 화면을 하나 더
   만들면 똑같은 목록이 두 벌 생긴다. */
const ASSET_VIEWS = [
  ["summary", "재고 집계", ""],
  ["list", "자산 목록", ""],
  ["uncoded", "🚫 판매불가 · 제품코드", "uncoded"],
  ["revert", "↩ 복귀 후보", "revert"],
  ["conflict", "⚠ 번호 충돌", "conflict"],
];

async function renderAssetsTab(body) {
  // ★대시보드 딥링크로 바로 들어와도 메타(등급·재고구분 설명)가 먼저 있어야 한다 —
  //   없으면 tierHelpBox 등이 죽어 화면이 '불러오는 중'에 멈춘다(2026-08-09 E2E 발견).
  await ensureMeta();
  if (!ASSET_VIEWS.some(([k]) => k === state.assetView)) state.assetView = "summary";
  const canEdit = hasPerm("purchase.edit");
  const views = ASSET_VIEWS.filter(([k]) =>
    canEdit || (k !== "uncoded" && k !== "revert" && k !== "conflict"));
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
     uncoded: renderUncoded,
     revert: renderRevertQueue,
     conflict: renderConflictQueue }[state.assetView])(host);
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
    const [u, rv, cf] = await Promise.all([
      api("/api/assets/uncoded").catch(() => null),
      api("/api/assets/revert-candidates").catch(() => null),
      api("/api/assets/number-conflicts").catch(() => null),
    ]);
    if (rv) put("revert", rv.count || 0);
    if (cf) put("conflict", cf.count || 0);
    // ★배지는 '사람이 지금 손봐야 하는' 판매불가 대수만 센다. 전체(코드 미입력
    //   1,900여 대)를 빨갛게 띄우면 절대 0이 안 되는 숫자라 무시하게 된다 —
    //   convert 배지에서 이미 겪은 교훈(아래 주석).
    if (u) put("uncoded", u.unsellable || 0);
  } catch { /* 배지는 없어도 화면은 돈다 */ }
}

/* ---------------- 💰 미지급금 (대표 2026-08-25) ----------------
   거래처에 줄 돈 — 매입 확정 전표 총액에서 지급 누계를 뺀 잔액. 지급을 기록하면 준다. */
async function renderPayables(body) {
  const seq = ++state.renderSeq;
  body.innerHTML = `<div class="card placeholder"><p>불러오는 중…</p></div>`;
  const onlyOwed = state.payOnlyOwed !== false;   // 기본: 잔액 있는 곳만
  let d;
  try { d = await api("/api/purchase/payables?onlyOwed=" + (onlyOwed ? 1 : 0)); }
  catch (err) { body.innerHTML = `<div class="card"><p class="muted">${escapeHtml(err.message)}</p></div>`; return; }
  if (seq !== state.renderSeq) return;
  const canEdit = hasPerm("purchase.edit");
  body.innerHTML = `
    <div class="kpi-row">
      <div class="kpi" style="border-left-color:var(--danger);">
        <div class="kpi-label">미지급 잔액</div><div class="kpi-value">${fmtWon(d.totalUnpaid)}</div></div>
      <div class="kpi"><div class="kpi-label">매입 총액</div><div class="kpi-value">${fmtWon(d.totalPurchased)}</div></div>
      <div class="kpi"><div class="kpi-label">지급 완료</div><div class="kpi-value">${fmtWon(d.totalPaid)}</div></div>
    </div>
    <div class="card">
      <div class="inline-row" style="margin:0 0 10px;">
        <h3 style="margin:0; flex:1;">거래처별 미지급</h3>
        <label class="check-line" style="font-size:13px;">
          <input type="checkbox" id="pay-owed" ${onlyOwed ? "checked" : ""}>
          <span>잔액 있는 거래처만</span></label>
      </div>
      ${d.suppliers.length ? `<div class="table-wrap"><table>
        <thead><tr><th>거래처</th><th style="text-align:right;">매입 총액</th>
          <th style="text-align:right;">지급</th><th style="text-align:right;">미지급</th>
          <th>가장 오래된 미지급</th><th></th></tr></thead>
        <tbody>${d.suppliers.map((g, i) => `
          <tr>
            <td><b>${escapeHtml(g.supplier)}</b> <span class="muted">(${g.batches}건)</span></td>
            <td style="text-align:right;">${fmtWon(g.totalAmount)}</td>
            <td style="text-align:right;" class="muted">${fmtWon(g.paidAmount)}</td>
            <td style="text-align:right;"><b style="color:${g.unpaid ? "var(--danger)" : "var(--text-dim)"};">${fmtWon(g.unpaid)}</b></td>
            <td class="muted">${escapeHtml(g.oldestUnpaidDate || "-")}</td>
            <td><button class="btn btn-sm" data-paysup="${i}">${g.unpaid ? "전표 보기 →" : "내역"}</button></td>
          </tr>
          <tr class="pay-detail" data-payrow="${i}" style="display:none;"><td colspan="6" style="padding:0 12px 10px;">
            <div class="table-wrap"><table>
              <thead><tr><th>전표</th><th>매입일</th><th style="text-align:right;">총액</th>
                <th style="text-align:right;">지급</th><th style="text-align:right;">미지급</th><th></th></tr></thead>
              <tbody>${g.batchList.map((b) => `<tr>
                <td>${escapeHtml(b.slipNo || "#" + b.id)}</td>
                <td class="muted">${escapeHtml(b.date || "")}</td>
                <td style="text-align:right;">${fmtWon(b.total)}</td>
                <td style="text-align:right;" class="muted">${fmtWon(b.paid)}</td>
                <td style="text-align:right;">${b.settled ? '<span class="chip chip-green">완납</span>'
                    : `<b style="color:var(--danger);">${fmtWon(b.unpaid)}</b>`}</td>
                <td>${canEdit && !b.settled
                    ? `<button class="btn btn-sm btn-primary" data-paybatch="${b.id}" data-slip="${escapeHtml(b.slipNo || "#" + b.id)}" data-unpaid="${b.unpaid}">💰 지급 기록</button>`
                    : (canEdit ? `<button class="btn btn-ghost btn-sm" data-paybatch="${b.id}" data-slip="${escapeHtml(b.slipNo || "#" + b.id)}" data-unpaid="0">이력</button>` : "")}</td>
              </tr>`).join("")}</tbody></table></div>
          </td></tr>`).join("")}
        </tbody></table></div>`
        : `<p class="muted">${onlyOwed ? "미지급 잔액이 있는 거래처가 없습니다 — 전부 완납입니다." : "매입 전표가 없습니다."}</p>`}
    </div>`;
  $("#pay-owed", body)?.addEventListener("change", (e) => {
    state.payOnlyOwed = e.target.checked;
    renderPayables(body);
  });
  $$("button[data-paysup]", body).forEach((b) => b.addEventListener("click", () => {
    const row = $(`tr.pay-detail[data-payrow="${b.dataset.paysup}"]`, body);
    if (row) row.style.display = row.style.display === "none" ? "" : "none";
  }));
  $$("button[data-paybatch]", body).forEach((b) => b.addEventListener("click", () =>
    openPayment(Number(b.dataset.paybatch), b.dataset.slip, Number(b.dataset.unpaid),
                () => renderPayables(body))));
}

/* 💰 지급 기록 — 전표에 얼마를 냈다. 부분지급 여러 번 가능, 누계가 총액에 닿으면 완납. */
async function openPayment(bid, slip, unpaid, onDone) {
  let cur = { payments: [] };
  try { cur = await api(`/api/purchase-batches/${bid}/payments`); } catch (_e) {}
  const host = document.createElement("div");
  host.innerHTML = `
    <div class="card in-modal">
      <h3 style="margin-top:0;">💰 지급 기록 <span class="muted" style="font-weight:400;">${escapeHtml(slip)}</span></h3>
      ${unpaid ? `<p class="muted" style="margin:4px 0 10px;">남은 미지급 <b style="color:var(--danger);">${fmtWon(unpaid)}</b></p>` : ""}
      ${unpaid ? `<div class="form-grid">
        <label>지급액<input type="text" id="pm-amount" data-money placeholder="${fmtNum(unpaid)}"></label>
        <label>지급일<input type="date" id="pm-date" value="${ymd()}"></label>
        <label>수단<input type="text" id="pm-method" placeholder="예: 계좌이체, 현금"></label>
        <label>메모<input type="text" id="pm-memo" placeholder="선택"></label>
      </div>
      <div class="inline-row" style="margin-top:6px;">
        <button class="btn btn-sm" id="pm-full" type="button">남은 전액 ${fmtNum(unpaid)}원</button>
      </div>` : ""}
      ${(cur.payments || []).length ? `<div class="table-wrap" style="margin-top:10px;"><table>
        <thead><tr><th>지급일</th><th style="text-align:right;">금액</th><th>수단</th><th>메모</th><th>기록</th></tr></thead>
        <tbody>${cur.payments.map((x) => `<tr>
          <td class="muted">${escapeHtml(x.date || "")}</td>
          <td style="text-align:right;">${x.amount < 0 ? '<span style="color:var(--danger);">' + fmtWon(x.amount) + '</span>' : fmtWon(x.amount)}</td>
          <td>${escapeHtml(x.method || "-")}</td>
          <td class="muted">${escapeHtml(x.memo || "-")}</td>
          <td class="muted" style="font-size:12px;">${escapeHtml(x.by || "")}</td>
        </tr>`).join("")}</tbody></table></div>` : ""}
      <div class="editor-actions">
        <button class="btn" id="pm-cancel">닫기</button>
        ${unpaid ? `<button class="btn btn-primary" id="pm-save">지급 기록</button>` : ""}
      </div>
    </div>`;
  openModalWith(host);
  $("#pm-cancel", host).addEventListener("click", () => closeModal());
  $("#pm-full", host)?.addEventListener("click", () => { $("#pm-amount", host).value = fmtNum(unpaid); });
  $("#pm-save", host)?.addEventListener("click", async () => {
    const amt = ($("#pm-amount", host).value || "").replace(/[^0-9]/g, "") || String(unpaid);
    if (!Number(amt)) { toast("지급액을 입력하세요.", true); return; }
    const btn = $("#pm-save", host);
    btn.disabled = true;
    try {
      const r = await api(`/api/purchase-batches/${bid}/pay`, { method: "POST", body: {
        amount: amt, date: $("#pm-date", host).value,
        method: $("#pm-method", host).value, memo: $("#pm-memo", host).value } });
      toast(r.fully ? "완납 처리했습니다." : `지급 기록 — 남은 미지급 ${fmtNum(r.unpaid)}원`);
      closeModal();
      if (onDone) onDone();
    } catch (err) { toast(err.message, true); btn.disabled = false; }
  });
}

/* 기준정보 탭 — 거래처/분류 + 부품 단가표 + TMS 이관. */
// ★TMS 이관·자동반영은 설정 ▸ 데이터 이관으로 옮겼다(대표 2026-08-26) — 옛 링크는 아래서 이동.
const BASE_VIEWS = [["suppliers", "거래처 / 분류"], ["models", "💻 모델 마스터"], ["parts", "🔩 부품 단가표"],
                    ["repairs", "🛠 수리 단가표"], ["paints", "🎨 도색/시트지 단가표"]];

function renderBaseTab(body) {
  if (state.baseView === "migrate") {        // 옛 자리(기준정보)로 오면 새 집으로 보낸다
    state.baseView = "suppliers";
    state.settingsTab = "migrate";
    return go("settings");
  }
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
  ({ suppliers: renderSuppliers, models: renderModelMaster, parts: renderPartsBook,
     repairs: renderRepairBook, paints: renderPaintBook,
     migrate: renderMigrate }[state.baseView])($("#bview-body", body));
}

/* ---------------- 부품 단가표 ----------------

   대표 요청(2026-08-10): RAM·SSD 없이 들어온 노트북에 부품을 꽂아 파는데 단가가
   매일 바뀐다 — 매번 금액을 칠 수 없다. 여기 '오늘 단가'만 고쳐 두면, 자산에 부품을
   추가할 때 자동으로 이 단가가 원가(수리비)로 들어간다.
   ★이미 추가된 자산의 원가는 그때 단가로 동결 — 여기를 고쳐도 과거는 안 바뀐다. */
/* 수리 단가표 — 부품 단가표와 같은 화면을 그대로 쓴다(대표 2026-08-24:
   "부품단가표처럼 수리단가표 탭도 동일한 양식대로"). 다른 건 담기는 값뿐이다. */
function renderRepairBook(body) { return renderPartsBook(body, "repair"); }
function renderPaintBook(body) { return renderPartsBook(body, "paint"); }

/* 단가표 3분할(2026-08-25 대표) — 고르기 셀렉트를 부품/수리/도색·시트지 묶음으로 채운다.
   ★한 목록에 섞여 있으면 58종을 훑어야 한다 — 묶음 제목(optgroup)으로 가른다. */
const PART_KIND_LABEL = { part: "🔩 부품", repair: "🛠 수리", paint: "🎨 도색/시트지" };

function fillPartSelect(sel, parts, withGroup) {
  const byKind = { part: [], repair: [], paint: [] };
  parts.filter((p) => p.enabled).forEach((p) => (byKind[p.kind] || byKind.part).push(p));
  ["part", "repair", "paint"].forEach((k) => {
    if (!byKind[k].length) return;
    const og = document.createElement("optgroup");
    og.label = PART_KIND_LABEL[k];
    byKind[k].forEach((p) => {
      const o = document.createElement("option");
      o.value = p.id;
      o.textContent = `${withGroup && p.group ? p.group + " · " : ""}${p.name} — ${fmtWon(p.price)}`;
      og.appendChild(o);
    });
    sel.appendChild(og);
  });
}

/* ── 📦 부품 매입(대표 2026-08-25) — 램·SSD를 수량으로 사들인다 ──
   전표(purchase_type=부품매입)로 만들어 매입 실적·미지급금에 그대로 잡히고,
   단가표 단가가 이번 매입 단가로 갱신된다(옵션 칩이 기입하는 '오늘 단가'의 출처). */
async function renderPartForm(host) {
  // 부품 매입 등록 — 매입등록 팝업의 [🔩 부품 등록] 모드(대표 2026-08-26 통합).
  // 전표(purchase_type=부품매입)로 만들어 매입 실적·미지급금에 그대로 잡히고,
  // 단가표 단가가 이번 매입 단가로 갱신된다(옵션 칩이 기입하는 '오늘 단가'의 출처).
  let stocks = { items: [] };
  try { stocks = await api("/api/part-stocks"); } catch (_e) {}
  const opts = stocks.items.filter((x) => x.enabled);
  if (!opts.length) {
    toast("단가표에 부품이 없습니다 — 기준정보 ▸ 부품 단가표에서 먼저 추가하세요.", true);
    return;
  }
  const optHtml = opts.map((x) =>
    `<option value="${x.id}" data-price="${x.price}">${escapeHtml(x.name)} — 재고 ${x.onhand} · ${fmtWon(x.price)}</option>`).join("");
  const rowHtml = () => `<tr>
      <td><select class="pp-part" style="min-width:200px;">${optHtml}</select></td>
      <td><input type="number" class="pp-qty" value="1" min="1" style="width:70px;"></td>
      <td><input type="text" class="pp-amount" data-money placeholder="행 총액" style="width:110px; text-align:right;"></td>
      <td><button class="btn btn-ghost btn-sm pp-del" title="줄 삭제">✕</button></td>
    </tr>`;
  host.innerHTML = `
    <div class="slip-form">
      ${regModeBar("part")}
      <div class="sf-title">
        <b>부품 매입 등록</b>
        <span class="muted">— 램·SSD 같은 소진 부품을 수량으로 사들입니다. 전표(부품매입)로
        매입 실적·미지급금에 잡히고, 단가표 단가가 이번 매입 단가로 갱신됩니다.</span>
      </div>
      <section class="sf-step">
        <h4 class="sf-step-head"><span class="sf-no">1</span> 전표 정보</h4>
        <div class="form-grid">
          <label>거래처<input type="text" id="pp-sup" autocomplete="off" placeholder="거래처 검색"></label>
          <label>입고일<input type="date" id="pp-date" value="${ymd()}"></label>
          <label>총 매입금액 <span class="muted">(적으면 줄별로 자동 분배 · 비우면 합계 자동)</span>
            <input type="text" id="pp-total" data-money
              title="거래처에 준 총액을 적으면 아래 줄들의 금액이 수량 비율로 자동 분배됩니다.&#10;줄 금액을 직접 적으면 여기가 합계로 자동 채워집니다."></label>
        </div>
        <label class="check-line" style="margin-top:8px; font-size:13px;">
          <input type="checkbox" id="pp-unpaid">
          <span>미지급(외상) — 💰 미지급금 탭에 잔액으로 잡힙니다</span></label>
      </section>
      <section class="sf-step">
        <h4 class="sf-step-head"><span class="sf-no">2</span> 부품 담기</h4>
        <div class="table-wrap"><table>
          <thead><tr><th>부품</th><th>수량</th><th style="text-align:right;">금액(총액)</th><th></th></tr></thead>
          <tbody id="pp-rows">${rowHtml()}</tbody></table></div>
        <div class="inline-row" style="margin-top:6px;">
          <button class="btn btn-sm" id="pp-add-row">➕ 줄 추가</button>
        </div>
      </section>
      <div class="editor-actions sf-actions">
        <button class="btn btn-primary" id="pp-save">등록</button>
        <button class="btn" id="pp-cancel">닫기</button>
      </div>
    </div>`;
  revealPanel(host);
  wireRegMode(host, "purchased");
  attachAutocomplete($("#pp-sup", host), "supplier");
  const rows = $("#pp-rows", host);
  const totalEl = $("#pp-total", host);
  // ★총액 양방향(자산 매입금액과 같은 규칙, 대표 2026-08-26): 총액을 적으면 분배 모드,
  //   줄 금액을 직접 고치면 합계 모드로 돌아온다 — 마지막에 만진 쪽이 이긴다.
  let totalManual = false;
  const rowsOf = () => $$("tr", rows);
  const parseAmt = (el) => Number((el.value || "").replace(/[^0-9]/g, "")) || 0;
  const sumRows = () => rowsOf().reduce((n, tr) => n + parseAmt($("input.pp-amount", tr)), 0);
  const distribute = () => {
    // 총액을 수량 비율로 분배. 원 단위 나머지는 첫 줄에 얹는다 — 합계가 총액과 같아야 한다.
    const total = parseAmt(totalEl);
    const trs = rowsOf();
    const totalQty = trs.reduce((n, tr) => n + (Number($("input.pp-qty", tr).value) || 0), 0);
    if (!total || !totalQty) return;
    const per = Math.floor(total / totalQty);
    let used = 0;
    trs.forEach((tr) => {
      const q = Number($("input.pp-qty", tr).value) || 0;
      const amt = per * q;
      $("input.pp-amount", tr).value = amt ? amt.toLocaleString("ko-KR") : "";
      used += amt;
    });
    const rem = total - used;
    if (rem > 0 && trs.length) {
      const first = $("input.pp-amount", trs[0]);
      first.value = (parseAmt(first) + rem).toLocaleString("ko-KR");
    }
  };
  const syncTotal = () => { totalEl.value = fmtNum(sumRows()) === "0" ? "" : fmtNum(sumRows()); };
  const wireRow = (tr) => {
    const amt = $("input.pp-amount", tr);
    const fill = () => {                     // 손대기 전에는 단가×수량을 미리 채운다
      if (totalManual) { distribute(); return; }
      if (!amt.dataset.touched) {
        const price = Number($("select.pp-part", tr).selectedOptions[0]?.dataset.price) || 0;
        const q = Number($("input.pp-qty", tr).value) || 0;
        amt.value = price && q ? (price * q).toLocaleString("ko-KR") : "";
      }
      syncTotal();
    };
    amt.addEventListener("input", () => {
      amt.dataset.touched = "1";
      totalManual = false;                   // 줄을 직접 고치면 합계 모드
      syncTotal();
    });
    $("select.pp-part", tr).addEventListener("change", fill);
    $("input.pp-qty", tr).addEventListener("input", fill);
    $("button.pp-del", tr).addEventListener("click", () => {
      if (rowsOf().length > 1) {
        tr.remove();
        if (totalManual) distribute(); else syncTotal();
      }
    });
    fill();
  };
  rowsOf().forEach(wireRow);
  totalEl.addEventListener("input", () => {
    totalManual = parseAmt(totalEl) > 0;
    if (totalManual) distribute();
  });
  $("#pp-add-row", host).addEventListener("click", () => {
    rows.insertAdjacentHTML("beforeend", rowHtml());
    wireRow(rows.lastElementChild);
  });
  $("#pp-cancel", host).addEventListener("click", () => { host.innerHTML = ""; });
  $("#pp-save", host).addEventListener("click", async () => {
    const items = rowsOf().map((tr) => ({
      partId: Number($("select.pp-part", tr).value),
      qty: Number($("input.pp-qty", tr).value) || 0,
      amount: String(parseAmt($("input.pp-amount", tr))) }));
    const sup = $("#pp-sup", host).value.trim();
    if (!sup) { toast("거래처를 입력하세요.", true); return; }
    const btn = $("#pp-save", host);
    btn.disabled = true;
    try {
      const r = await api("/api/part-purchases", { method: "POST", body: {
        supplierName: sup, date: $("#pp-date", host).value,
        unpaid: $("#pp-unpaid", host).checked, rows: items } });
      toast(`부품 매입 등록 — 전표 ${r.slipNo} · 합계 ${fmtNum(r.total)}원`);
      host.innerHTML = "";
      const pb = $("#ptab-body");
      if (pb) renderSlips(pb);
    } catch (err) { toast(err.message, true); btn.disabled = false; }
  });
}

/* 📜 부품 입출 내역 */
async function openStockMoves(pid, name) {
  let d;
  try { d = await api(`/api/parts/${pid}/stock-moves`); }
  catch (err) { toast(err.message, true); return; }
  const R = { purchase: "📦 매입", use: "장착", return: "회수", adjust: "보정" };
  const host = document.createElement("div");
  host.innerHTML = `
    <div class="card in-modal">
      <h3 style="margin-top:0;">📜 입출 내역 <span class="muted" style="font-weight:400;">${escapeHtml(name)} — 현재고 ${d.onhand}개</span></h3>
      ${d.moves.length ? `<div class="table-wrap" style="max-height:50vh; overflow-y:auto;"><table>
        <thead><tr><th>일자</th><th>구분</th><th style="text-align:right;">수량</th>
          <th style="text-align:right;">단가</th><th>연결</th><th>기록</th></tr></thead>
        <tbody>${d.moves.map((m) => `<tr>
          <td class="muted">${escapeHtml(m.date || "")}</td>
          <td>${R[m.reason] || escapeHtml(m.reason)}</td>
          <td style="text-align:right;"><b style="${m.qty < 0 ? "color:var(--danger);" : ""}">${m.qty > 0 ? "+" : ""}${m.qty}</b></td>
          <td style="text-align:right;" class="muted">${m.unitCost ? fmtWon(m.unitCost) : "-"}</td>
          <td class="muted" style="font-size:12px;">${escapeHtml([m.supplier, m.slipNo, m.assetNo,
            m.memo].filter(Boolean).join(" · ")) || "-"}</td>
          <td class="muted" style="font-size:12px;">${escapeHtml(m.by || "")}</td>
        </tr>`).join("")}</tbody></table></div>` : `<p class="muted">아직 입출 기록이 없습니다.</p>`}
      <div class="editor-actions"><button class="btn" id="sm-close">닫기</button></div>
    </div>`;
  openModalWith(host);
  $("#sm-close", host).addEventListener("click", () => closeModal());
}

/* ± 재고 보정 — 실사 결과가 장부와 다를 때 */
function openStockAdjust(pid, name, onDone) {
  const host = document.createElement("div");
  host.innerHTML = `
    <div class="card in-modal">
      <h3 style="margin-top:0;">± 재고 보정 <span class="muted" style="font-weight:400;">${escapeHtml(name)}</span></h3>
      <div class="form-grid">
        <label>수량(±)<input type="number" id="sa-qty" placeholder="예: 3 또는 -2" style="width:110px;"></label>
        <label>사유<input type="text" id="sa-memo" placeholder="예: 실사 차이"></label>
      </div>
      <div class="editor-actions">
        <button class="btn" id="sa-cancel">닫기</button>
        <button class="btn btn-primary" id="sa-save">보정</button>
      </div>
    </div>`;
  openModalWith(host);
  $("#sa-cancel", host).addEventListener("click", () => closeModal());
  $("#sa-save", host).addEventListener("click", async () => {
    try {
      const r = await api(`/api/parts/${pid}/stock-adjust`, { method: "POST", body: {
        qty: Number($("#sa-qty", host).value) || 0, memo: $("#sa-memo", host).value } });
      toast(`보정 완료 — 현재고 ${r.onhand}개`);
      closeModal();
      if (onDone) onDone();
    } catch (err) { toast(err.message, true); }
  });
}

async function renderPartsBook(body, kind = "part") {
  const seq = ++state.renderSeq;
  let allParts;
  try {
    allParts = await api("/api/parts?kind=" + encodeURIComponent(kind));
    if (seq !== state.renderSeq) return;
  } catch (err) {
    if (seq === state.renderSeq) body.innerHTML = `<p class="muted">${escapeHtml(err.message)}</p>`;
    return;
  }
  const canEdit = hasPerm("purchase.edit");
  // 재고 열(2026-08-25 대표) — 부품(물건)만. 수리·도색은 공임이라 수량이 없다.
  let stockMap = {};
  if (kind === "part") {
    try {
      const st = await api("/api/part-stocks");
      if (seq !== state.renderSeq) return;
      st.items.forEach((x) => { stockMap[x.id] = x; });
    } catch (_e) { stockMap = null; }
  }
  const catLabel = { ram: "RAM", ssd: "SSD", "": "기타" };
  // ── 대제목(그룹)별로 묶는다(2026-08-13 대표 "대제목으로 접고 펴서 규격별로").
  //    그룹이 없는 부품은 '기타' 아래로. 그룹 순서는 취급 규격 → 나머지 가나다 → 기타.
  const PREFERRED = kind === "repair" ? ["수리"]
    : kind === "paint" ? ["도색/시트지", "시트지"]
    : ["RAM", "M.2 NVMe", "M.2 SATA", "2.5 SSD", "2.5 HDD", "CPU", "그래픽", "파워", "보드"];
  state.partsOpen = state.partsOpen || {};
  const rowHtml = (p) => `
          <tr style="${p.enabled ? "" : "opacity:.5;"}">
            <td><b>${escapeHtml(p.name)}</b></td>
            <td>${canEdit
              ? `<select class="pb-row-cat" data-pid="${p.id}" style="padding:3px 4px;">
                   <option value="ram" ${p.category === "ram" ? "selected" : ""}>RAM</option>
                   <option value="ssd" ${p.category === "ssd" ? "selected" : ""}>SSD·HDD</option>
                   <option value="" ${!p.category ? "selected" : ""}>기타</option></select>`
              : (catLabel[p.category] || "기타")}</td>
            <td>${canEdit
              ? `<input type="text" class="pb-row-grp" data-pid="${p.id}" list="pb-grp-list"
                   value="${escapeHtml(p.group || "")}" placeholder="기타"
                   title="대제목(묶음)을 바꾸고 💾를 누르면 그 묶음으로 이동합니다"
                   style="width:110px; padding:4px 6px; border:1px solid var(--border);
                          border-radius:6px; background:var(--bg);">`
              : escapeHtml(p.group || "기타")}</td>
            <td style="text-align:right;">${canEdit
              ? `<input type="text" class="pb-row-price" data-money data-pid="${p.id}" value="${fmtNum(p.price)}"
                   style="width:90px; text-align:right; padding:4px 6px; border:1px solid var(--border);
                          border-radius:6px; background:var(--bg);">`
              : fmtWon(p.price)}</td>
            ${kind === "part" && stockMap ? `<td style="text-align:right;">
              <b>${(stockMap[p.id] || {}).onhand || 0}</b>
              <button class="btn btn-ghost btn-sm pb-moves" data-pid="${p.id}"
                data-name="${escapeHtml(p.name)}" title="입출 내역">📜</button>${canEdit
              ? `<button class="btn btn-ghost btn-sm pb-adjust" data-pid="${p.id}"
                   data-name="${escapeHtml(p.name)}" title="실사 보정(±)">±</button>` : ""}</td>` : ""}
            <td style="text-align:right;" class="muted">${p.usedCount}</td>
            <td class="muted" style="font-size:12px;">${escapeHtml((p.updatedAt || "").slice(0, 10))}
              ${escapeHtml(p.updatedBy || "")}</td>
            <td>${canEdit ? `
              <button class="btn btn-sm pb-save" data-pid="${p.id}" title="단가·구분·대제목 저장">💾</button>
              <button class="btn btn-ghost btn-sm pb-toggle" data-pid="${p.id}" data-on="${p.enabled ? 1 : 0}"
                title="${p.enabled ? "끄면 부품 추가 목록에서 빠집니다(기록은 유지)" : "다시 켭니다"}">
                ${p.enabled ? "끄기" : "켜기"}</button>
              <button class="btn btn-ghost btn-sm pb-del" data-pid="${p.id}"
                data-name="${escapeHtml(p.name)}" data-used="${p.usedCount}"
                title="단가표에서 지웁니다. 이미 자산에 들어간 부품비는 그대로 남습니다(그때 단가로 동결).">🗑</button>` : ""}</td>
          </tr>`;
  // ── 검색 결과 영역만 만든다(★입력창은 여기 없다 — 글자마다 다시 그려도 조합이 안 끊기게).
  //    실시간 검색(대표 2026-08-25 "Enter 없이") + 한글 끊김 수리(2026-08-26 "글자가 뚝뚝").
  const buildGroupsHtml = () => {
    const q = (state.partsQuery || "").trim().toLowerCase();
    const list = !q ? allParts : allParts.filter((x) =>
      (x.name || "").toLowerCase().includes(q) || (x.group || "").toLowerCase().includes(q));
    const byGroup = new Map();
    list.forEach((p) => {
      const g = p.group || "기타";
      if (!byGroup.has(g)) byGroup.set(g, []);
      byGroup.get(g).push(p);
    });
    const groups = [...PREFERRED.filter((g) => byGroup.has(g)),
                    ...[...byGroup.keys()].filter((g) => !PREFERRED.includes(g) && g !== "기타").sort(),
                    ...(byGroup.has("기타") ? ["기타"] : [])];
    const groupCard = (g) => {
      const rows = byGroup.get(g);
      const noPrice = rows.filter((p) => p.enabled && !p.price).length;
      // 단가가 빈 규격이 있는 묶음은 처음에 펼쳐 보여 준다 — 채울 일이 있다는 신호
      // 검색 중에는 걸린 그룹을 무조건 펼친다 — 접혀 있으면 찾은 게 안 보인다
      const open = q ? true
        : (state.partsOpen[g] !== undefined ? state.partsOpen[g] : noPrice > 0);
      return `
      <details class="pb-group" data-grp="${escapeHtml(g)}"${open ? " open" : ""}
        style="border:1px solid var(--border); border-radius:10px; padding:6px 12px; margin:8px 0;">
        <summary style="cursor:pointer;"><b>${escapeHtml(g)}</b>
          <span class="muted"> · ${rows.length}종${noPrice
            ? ` · <span style="color:var(--danger);">단가 미입력 ${noPrice}종</span>` : ""}</span></summary>
        <div class="table-wrap" style="margin-top:6px;"><table>
          <thead><tr><th>부품</th><th>구분</th><th>대제목</th><th style="text-align:right;">오늘 단가</th>
            ${kind === "part" && stockMap ? '<th style="text-align:right;">재고</th>' : ""}
            <th style="text-align:right;">사용된 횟수</th><th>마지막 수정</th><th></th></tr></thead>
          <tbody>${rows.map(rowHtml).join("")}</tbody></table></div>
      </details>`;
    };
    return { count: list.length, q,
             html: groups.map(groupCard).join("") || `<p class="muted">${q
               ? "검색과 일치하는 부품이 없습니다."
               : "부품이 없습니다. 위에서 취급하는 규격(예: D4 8G / M.2 NVMe 512G)을 추가하세요."}</p>` };
  };
  body.innerHTML = `
    <div class="card">
      <h3>🔩 부품 단가표</h3>
      <p class="muted">단가는 <b>부가세 포함 총액</b>으로 넣으세요(공급가·부가세는 자동 분리).
        자산에 부품을 추가하는 순간의 단가가 그 자산 원가로 <b>동결</b>됩니다 —
        내일 단가를 바꿔도 오늘 추가한 원가는 그대로입니다. 단가 변경은 감사 로그에 남습니다.</p>
      <div class="inline-row" style="margin:0 0 10px;">
        <input type="text" id="pb-q" value="${escapeHtml(state.partsQuery || "")}"
               placeholder="🔍 이름·대제목 검색 — 치는 즉시 걸러집니다" style="min-width:240px;">
        <span class="muted" style="font-size:12px;" id="pb-q-count"></span>
        <button class="btn btn-ghost btn-sm" id="pb-q-clear" style="display:none;">✕</button>
      </div>
      ${canEdit ? `<div class="inline-row">
        <input type="text" id="pb-name" placeholder="부품 이름 * (예: D4 8G, M.2 NVMe 512G)" style="min-width:200px;">
        <select id="pb-cat"><option value="ram">RAM</option><option value="ssd">SSD·HDD</option>
          <option value="">기타(배터리·어댑터 등)</option></select>
        <input type="text" id="pb-grp" list="pb-grp-list" placeholder="대제목(예: M.2 NVMe)" style="width:140px;"
          title="같은 대제목끼리 접고 펴는 묶음이 됩니다. 비우면 RAM은 자동으로 RAM 묶음, 나머지는 '기타'.">
        <datalist id="pb-grp-list">${[...new Set([...PREFERRED, ...allParts.map((x) => x.group).filter(Boolean)])]
          .filter((g) => g !== "기타").map((g) => `<option value="${escapeHtml(g)}">`).join("")}</datalist>
        <input type="text" id="pb-price" data-money placeholder="오늘 단가(원)" style="width:110px;">
        <button class="btn btn-sm btn-primary" id="pb-add">추가</button>
        <span class="muted" style="font-size:12px;">이름을 스펙 표기와 맞추면 자산 스펙 칸도 그 이름으로 채워집니다</span>
      </div>` : ""}
      <div id="pb-groups"></div>
      <div class="muted" style="font-size:12.5px; border-top:1px dashed var(--border);
           margin-top:10px; padding-top:8px; line-height:1.7;">
        ℹ️ <b>전환 단가(4G→8G 24,000원 같은 것)는 일부러 등록하지 않습니다</b> —
        자산에서 4G를 <b>회수</b>(−20,000)하고 8G를 <b>장착</b>(+44,000)하면 차액 24,000원이
        자동으로 원가에 반영됩니다. 12G·24G처럼 조합 용량도 별도 등록 없이 모듈 두 개
        (4G+8G, 8G+16G)를 장착하면 됩니다. 같은 단가를 두 벌 관리하면 한쪽만 고쳐져
        어긋나므로, 여기엔 <b>모듈(실물 한 개) 단가만</b> 둡니다.<br>
        🖥️ CPU·그래픽·파워·보드는 데스크탑 일부 모델용 — 소켓·보드·파워 호환을 사람이
        판단해야 해서 옵션 <b>자동</b> 기입에는 안 붙습니다. 자산 상세나 전표의
        [🔩 부품 일괄]에서 <b>수동</b> 장착/회수로 반영하세요.
      </div>
    </div>`;
  // ── 결과 영역 갱신 + 그 안의 버튼 배선(★부분 갱신 때마다 다시 걸어야 산다 —
  //    body 에 한 번 걸고 끝내면 innerHTML 교체 후 전부 죽는다. 📜·± 무반응의 원인이었다.)
  const host = $("#pb-groups", body);
  const renderGroups = () => {
    const built = buildGroupsHtml();
    host.innerHTML = built.html;
    $("#pb-q-count", body).textContent = built.q ? `${built.count}종 일치` : "";
    $("#pb-q-clear", body).style.display = built.q ? "" : "none";
    // 접고 편 상태를 기억한다 — 단가 몇 개 고치는 동안 매번 다시 펼치게 하지 않는다
    $$("details.pb-group", host).forEach((d) => d.addEventListener("toggle", () => {
      state.partsOpen[d.dataset.grp] = d.open;
    }));
    $$(".pb-save", host).forEach((b) => b.addEventListener("click", async () => {
      const pid = b.dataset.pid;
      const price = Number(($(`.pb-row-price[data-pid="${pid}"]`, host).value || "")
        .replace(/[^0-9]/g, "")) || 0;
      // 단가와 함께 구분·대제목도 저장한다 — '기타'에 갇힌 기존 부품을 줄에서 바로 옮긴다
      try {
        await api(`/api/parts/${pid}`, { method: "PATCH", body: {
          price,
          category: $(`.pb-row-cat[data-pid="${pid}"]`, host).value,
          group: $(`.pb-row-grp[data-pid="${pid}"]`, host).value.trim() } });
        toast("저장했습니다. (이미 추가된 자산의 원가는 안 바뀝니다)");
        renderPartsBook(body, kind);
      } catch (err) { toast(err.message, true); }
    }));
    $$(".pb-toggle", host).forEach((b) => b.addEventListener("click", async () => {
      try {
        await api(`/api/parts/${b.dataset.pid}`, { method: "PATCH",
          body: { enabled: b.dataset.on !== "1" } });
        renderPartsBook(body, kind);
      } catch (err) { toast(err.message, true); }
    }));
    // 🗑 삭제 — 과거 원가는 스냅샷이라 안전하다는 걸 확인창에서 분명히 알린다
    $$(".pb-del", host).forEach((b) => b.addEventListener("click", async () => {
      const used = Number(b.dataset.used) || 0;
      if (!confirm(`「${b.dataset.name}」을(를) 단가표에서 지울까요?\n\n`
        + (used ? `이 부품은 자산 ${used}건에 이미 들어가 있습니다 — 그 원가는 그때 단가로\n`
                + "동결돼 있어 지워도 금액·이력이 바뀌지 않습니다.\n\n"
                : "")
        + "잠시 안 쓸 거라면 [끄기]가 더 안전합니다(기록·연결이 유지됩니다).")) return;
      try {
        await api(`/api/parts/${b.dataset.pid}`, { method: "DELETE" });
        toast(`${b.dataset.name} 삭제됨`);
        renderPartsBook(body, kind);
      } catch (err) { toast(err.message, true); }
    }));
    $$(".pb-moves", host).forEach((b) => b.addEventListener("click", () =>
      openStockMoves(Number(b.dataset.pid), b.dataset.name)));
    $$(".pb-adjust", host).forEach((b) => b.addEventListener("click", () =>
      openStockAdjust(Number(b.dataset.pid), b.dataset.name,
                      () => renderPartsBook(body, kind))));
  };
  renderGroups();
  // ── 실시간 검색 — ★입력창은 절대 다시 그리지 않는다(한글 조합이 끊긴다,
  //    2026-08-26 대표 "글자가 뚝뚝"). 결과 영역(#pb-groups)만 갈아끼운다.
  const qEl = $("#pb-q", body);
  let qTimer = null;
  qEl.addEventListener("input", () => {
    clearTimeout(qTimer);
    qTimer = setTimeout(() => {
      state.partsQuery = qEl.value;
      renderGroups();
    }, 150);
  });
  $("#pb-q-clear", body).addEventListener("click", () => {
    state.partsQuery = "";
    qEl.value = "";
    renderGroups();
    qEl.focus();
  });
  if (!canEdit) return;

  $("#pb-add").addEventListener("click", async () => {
    const name = $("#pb-name").value.trim();
    const price = Number(($("#pb-price").value || "").replace(/[^0-9]/g, "")) || 0;
    if (!name) { toast("부품 이름을 입력하세요.", true); return; }
    try {
      await api("/api/parts", { method: "POST",
        // ★수리 탭에서 만든 항목은 수리 단가표로 들어가야 한다 — 안 실어 보내면 부품으로 샌다
        body: { name, category: $("#pb-cat").value, price, kind,
                group: $("#pb-grp").value.trim() } });
      toast(`${name} 추가됨`);
      renderPartsBook(body);
    } catch (err) { toast(err.message, true); }
  });
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
    if (!catMap.has(key)) catMap.set(key, { id: row.categoryId, total: 0, byStatus: {}, byGrade: {} });
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
      <div class="kpi" data-kb="ready" style="cursor:pointer;" title="누르면 판매가능 자산 목록으로 갑니다">
        <div class="kpi-label">지금 팔 수 있는 재고</div>
        <div class="kpi-value" style="color:var(--primary);">${t.ready}대</div>
        <div class="kpi-sub">제품 ${t.products}종</div></div>
      <div class="kpi" data-kb="working" style="cursor:pointer;" title="누르면 작업 중 자산 목록으로 갑니다">
        <div class="kpi-label">작업 중 (입고·정비·수리·도색)</div>
        <div class="kpi-value">${t.working}대</div>
        <div class="kpi-sub">손보면 판매 가능</div></div>
      <div class="kpi" data-kb="held" style="cursor:pointer;" title="누르면 보류 자산 목록으로 갑니다">
        <div class="kpi-label">보류 (주문매칭·A/S·회수·불량)</div>
        <div class="kpi-value">${t.held}대</div>
        ${t.lowStock ? `<div class="kpi-sub" style="color:var(--danger);">재고 부족 ${t.lowStock}종 · 품절 ${t.soldOut}종</div>`
          : t.soldOut ? `<div class="kpi-sub">품절 ${t.soldOut}종</div>` : ""}</div>
      <div class="kpi" data-kb="stock" style="cursor:pointer;" title="누르면 보유 재고 전체 목록으로 갑니다">
        <div class="kpi-label">보유 재고 자산가치</div>
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
          ${used.map((s) => `<td>${!c.byStatus[s] ? ""
            : c.id == null ? c.byStatus[s]
            : `<button class="link-btn" data-cs="${c.id}|${escapeHtml(s)}"
                 title="${escapeHtml(statusLabel(s))} ${c.byStatus[s]}대 목록 보기">${c.byStatus[s]}</button>`}</td>`).join("")}
          <td><b>${c.total}</b></td></tr>`).join("") || `<tr><td colspan="${used.length + 2}" class="muted">등록된 자산이 없습니다.</td></tr>`}
        </tbody></table></div></div>
    <div class="card"><h3>보유 재고 등급 분포</h3>
      ${[...catMap.entries()].map(([name, c]) => {
        const chips = state.meta.grades.filter((g) => c.byGrade[g]).map((g) =>
          c.id == null
            ? `<span class="chip chip-green" style="margin-right:6px;">${escapeHtml(g)} ${c.byGrade[g]}</span>`
            : `<button class="chip chip-green" data-cg="${c.id}|${escapeHtml(g)}"
                 style="margin-right:6px; cursor:pointer; border:0;"
                 title="보유 중인 ${escapeHtml(g)}등급 ${c.byGrade[g]}대 목록 보기">${escapeHtml(g)} ${c.byGrade[g]}</button>`).join("");
        return chips ? `<p style="margin:6px 0;"><b>${escapeHtml(name)}</b> &nbsp; ${chips}</p>` : "";
      }).join("") || `<p class="muted">보유 재고가 없습니다.</p>`}
    </div>`;
  wireStockTable(body, stock);
  // ★재고집계의 숫자는 전부 눌러서 그 자산 목록으로 간다(2026-08-12 대표 지시).
  //   각 클릭이 거는 필터는 집계와 같은 기준이라 숫자가 목록 건수와 일치한다.
  $$(".kpi[data-kb]", body).forEach((k) => k.addEventListener("click", () =>
    goAssetList({ bucket: k.dataset.kb })));
  $$("button[data-cs]", body).forEach((b) => b.addEventListener("click", () => {
    const [cid, st] = b.dataset.cs.split("|");
    // 카테고리×상태 표는 '실물 받은 것'만 세므로 목록에도 같은 조건을 건다
    goAssetList({ categoryId: cid, status: st, received: "1" });
  }));
  $$("button[data-cg]", body).forEach((b) => b.addEventListener("click", () => {
    const [cid, g] = b.dataset.cg.split("|");
    // 등급 분포는 '보유 재고'(출고·폐기 제외) 기준 — bucket=stock이 같은 집합이다
    goAssetList({ bucket: "stock", categoryId: cid, grade: g });
  }));
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
    // ★한글 조합 중에 검색창까지 갈아끼우면 글자가 뚝뚝 끊긴다(2026-08-26 대표) —
    //   조합이 끝날 때까지 다시 그리기를 미룬다(입력 핸들러 쪽 가드와 한 짝).
    if (state.spqComposing) return;
    const card = $("#sp-q")?.closest(".card");
    if (!card) return;
    card.outerHTML = stockTableHtml(stock);
    wireStockTable(body, stock);
    const nq = $("#sp-q");
    if (nq) { nq.focus(); nq.selectionStart = nq.selectionEnd = nq.value.length; }
  };
  const spq = $("#sp-q", body);
  if (spq) {
    // 조합(한글 입력) 중엔 화면을 갈지 않는다 — 끝나는 순간 한 번만 그린다
    spq.addEventListener("compositionstart", () => { state.spqComposing = true; });
    spq.addEventListener("compositionend", () => {
      state.spqComposing = false;
      f.q = spq.value;
      resetPage("stock");
      clearTimeout(state.stockTimer);
      state.stockTimer = setTimeout(redraw, 250);
    });
    spq.addEventListener("input", (e) => {
      f.q = e.target.value;
      resetPage("stock");                 // 검색하면 1쪽부터
      clearTimeout(state.stockTimer);
      state.stockTimer = setTimeout(redraw, 250);
    });
  }
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
    <div class="inline-row" style="background:var(--primary-soft); border-radius:8px; padding:10px 12px; margin:8px 0; flex-wrap:wrap;">
      <b>${n}대 선택됨</b>
      <select id="ab-status"><option value="">상태 그대로</option>
        ${(state.meta.manualStatuses || []).map((s) =>
          `<option value="${s}">${escapeHtml(statusLabel(s))}</option>`).join("")}</select>
      <select id="ab-tier" title="가재고는 출고가 막힙니다"><option value="">재고구분 그대로</option>
        ${(state.meta.tiers || []).map((t) => `<option>${escapeHtml(t)}</option>`).join("")}</select>
      <select id="ab-grade"><option value="">등급 그대로</option>
        ${state.meta.grades.map((g) => `<option>${escapeHtml(g)}</option>`).join("")}</select>
      <input type="text" id="ab-location" placeholder="보관위치(비우면 그대로)" style="min-width:150px;">
      <button class="btn btn-sm btn-primary" id="ab-apply">선택 ${n}대에 적용</button>
      ${stockSyncOn() ? `
      <span style="border-left:1px solid var(--border); align-self:stretch;"></span>
      <button class="btn btn-sm" id="ab-stock-on"
        title="선택한 자산을 쇼핑몰 재고 전송 대상으로 켭니다(제품코드가 있는 것만).&#10;사내 재고와는 무관합니다 — 그건 제품코드·상태·재고구분으로 정해집니다.">📦 일괄 몰 재고 전송</button>
      <button class="btn btn-sm" id="ab-stock-off"
        title="선택한 자산을 쇼핑몰 재고 전송 대상에서 뺍니다">📴 일괄 전송 해제</button>` : ""}
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
    const tr = $("#ab-tier").value;
    if (st) payload.status = st;
    if (gr) payload.grade = gr;
    if (loc) payload.location = loc;
    if (tr) payload.tier = tr;
    if (!st && !gr && !loc && !tr) { toast("바꿀 항목을 하나 이상 고르세요.", true); return; }
    let msg = `선택한 ${payload.ids.length}대를 바꿉니다.`;
    if (tr && tr !== "가재고") msg += `\n\n※ 재고구분을 '${tr}'으로 올리면 그 자산들의 출고가 열립니다.`;
    if (!confirm(msg + "\n계속할까요?")) return;
    try {
      const r = await api("/api/assets/bulk", { method: "POST", body: payload });
      toast(`${r.ok}대 변경${r.failed.length ? ` · ${r.failed.length}대 실패` : ""}`);
      state.assetPicked = new Set(r.failed.map((x) => x.id));
      state.assetBulkResult = { title: "일괄 변경", ok: r.ok, failed: r.failed };
      renderAssetList(body);
    } catch (err) { toast(err.message, true); }
  });
  // ★일괄 재고반영/해제 — 대표 보고(2026-08-07) "전체선택 후 일괄 재고반영이 작동을
  //   안 하던데". 이 화면에는 버튼 자체가 없었다(전표 상세에만 있었다). 그리고 눌러도
  //   제품코드 없는 자산은 조용히 건너뛰어 '아무 일도 안 일어난' 것처럼 보였다.
  //   → 버튼을 만들고, 건너뛴 대수와 사유를 반드시 화면에 남긴다.
  const bulkStockList = async (on) => {
    const ids = [...state.assetPicked];
    if (!confirm(`선택한 ${ids.length}대를 ${on ? "몰 재고 전송 대상으로 켭니다" : "몰 재고 전송에서 뺍니다"}.\n`
        + (on ? "제품코드가 없는 자산은 건너뛰고 알려 드립니다.\n" : "") + "계속할까요?")) return;
    try {
      const r = await api("/api/assets/stock-listing", { method: "POST", body: { ids, on } });
      const sk = r.skipped || [];
      toast(`${on ? "몰 재고 전송" : "전송 해제"} ${r.ok}대` + (sk.length ? ` · 건너뜀 ${sk.length}대` : ""),
            r.ok === 0 && sk.length > 0);
      state.assetPicked = new Set(sk.map((x) => x.id));
      state.assetBulkResult = {
        title: on ? "일괄 몰 재고 전송" : "일괄 전송 해제", ok: r.ok,
        failed: sk.map((x) => ({ id: x.id, label: x.assetNo, reason: x.reason })),
      };
      renderAssetList(body);
    } catch (err) { toast(err.message, true); }
  };
  $("#ab-stock-on", bar)?.addEventListener("click", () => bulkStockList(true));
  $("#ab-stock-off", bar)?.addEventListener("click", () => bulkStockList(false));
}

/* ---------------- 사업부 이관 (렌탈 RMS ↔ 판매 OWS) ----------------
   ★RMS의 [↔ 사업부 이관]과 **같은 규격·같은 순서**로 만든다(대표 지시 2026-08-13).
     한 사람이 두 시스템을 오가며 쓰는 기능이라, 화면이 다르면 매번 다시 배워야 한다.
       ① 자산 목록 툴바에 버튼이 항상 있다(선택 여부와 무관)
       ② 목록에서 체크한 자산이 있으면 번호가 미리 채워진다
       ③ 연결상태 → 방향 → 번호입력 → 사유 → 확인하기 → 미리보기 → 이관 → 결과
   ★반드시 '제안(미리보기)'을 먼저 보여준다 — 이 단계에서는 아무것도 바뀌지 않는다. */
const TR = {
  dirLabel: (d) => (d === "rental->sale" ? "렌탈(RMS) → 판매(OWS)" : "판매(OWS) → 렌탈(RMS)"),
  nums: (t) => (String(t || "").match(/[^\\s,;]+/g) || []),
};

async function openTransfer(body) {
  const host = $("#ab-transfer-box", body);
  if (!host) return;
  // 목록에서 고른 자산이 있으면 번호를 미리 채운다(RMS는 붙여넣기가 기본이라 형태를 맞춘다)
  const picked = [...(state.assetPicked || [])]
    .map((id) => state.assetNoById?.get(id)).filter(Boolean);
  state.trUI = {
    text: picked.join(" "), reason: "", preview: null, result: null,
    peer: null, busy: false,
    // 이관하기 / 내역 — 탭을 새로 만들지 않고 이 모달 안에서 오간다(대표 지시)
    tab: "do", hist: null, histDir: "", histQ: "",
    // 지금 보고 있는 목록이 렌탈이면 판매로 보내는 게 자연스럽다
    dir: state.assetFilter?.division === "rental" ? "rental->sale" : "sale->rental",
  };
  renderTransfer(body, host);
  openModalWith(host);
  try {
    state.trUI.peer = await api("/api/transfers/peer-status");
  } catch (e) {
    state.trUI.peer = { ok: false, error: e.message };
  }
  renderTransfer(body, host);
}

function renderTransfer(body, host) {
  const u = state.trUI;
  const nums = TR.nums(u.text);
  const pv = u.preview;
  const items = (pv && pv.items) || [];
  const warned = items.filter((i) => i.result === "ok" && i.note);
  const bad = items.filter((i) => i.result === "blocked");
  const miss = items.filter((i) => i.result === "unmatched");
  const noop = items.filter((i) => i.result === "noop");
  const ok = pv ? (pv.summary?.ok || 0) : 0;
  const r = u.result;

  host.innerHTML = `
    <div class="card">
      <div class="inline-row" style="margin:0 0 10px;">
        <h3 style="margin:0;">↔ 사업부 이관</h3>
        <button class="btn btn-ghost btn-sm" id="tr-tab-do"
          title="자산번호를 넣어 새로 이관합니다 — 실행은 아래 [🔍 확인하기]입니다"
          style="border-bottom:2px solid ${u.tab === "do" ? "var(--primary)" : "transparent"};border-radius:0;font-weight:${u.tab === "do" ? "700" : "400"};">새 이관</button>
        <button class="btn btn-ghost btn-sm" id="tr-tab-hist"
          title="렌탈(RMS)과 판매(OWS) 사이를 오간 자산 목록입니다"
          style="border-bottom:2px solid ${u.tab === "hist" ? "var(--primary)" : "transparent"};border-radius:0;font-weight:${u.tab === "hist" ? "700" : "400"};">📋 내역</button>
        <span style="flex:1"></span>
        <button class="btn btn-ghost btn-sm" id="tr-close">닫기</button>
      </div>
      ${u.tab === "hist" ? renderTrHistory(u) : `
      ${u.peer ? `<div class="muted" style="border:1px solid ${u.peer.ok ? "var(--border)" : "var(--danger)"};
        border-radius:8px;padding:8px 10px;margin-bottom:8px;color:${u.peer.ok ? "" : "var(--danger)"};">
        ${u.peer.ok ? `🔗 렌탈 시스템(RMS) 연결 정상 — 자산 ${(u.peer.assets || 0).toLocaleString()}대 조회됨`
                    : `⛔ ${escapeHtml(u.peer.error || "RMS를 읽을 수 없습니다")} — 시리얼 대조 없이 진행되면 위험합니다.`}
      </div>` : `<div class="muted" style="margin-bottom:8px;">연결 확인 중…</div>`}

      <div class="inline-row" style="margin:0 0 6px;">
        <span>방향</span>
        <select id="tr-dir" ${pv ? "disabled" : ""}>
          <option value="rental->sale" ${u.dir === "rental->sale" ? "selected" : ""}>렌탈(RMS) → 판매(OWS)</option>
          <option value="sale->rental" ${u.dir === "sale->rental" ? "selected" : ""}>판매(OWS) → 렌탈(RMS)</option>
        </select>
      </div>
      <label class="muted" style="display:block;margin-bottom:4px;">
        자산번호 (공백·엔터·쉼표 구분) — 목록에서 고른 자산이 있으면 미리 채워집니다</label>
      <textarea id="tr-text" rows="5" ${pv ? "disabled" : ""}
        placeholder="관리번호를 여러 개 붙여넣기 — 예: 250513-0034 250513-0035"
        style="width:100%;padding:8px;border:1px solid var(--border);border-radius:8px;
               background:var(--bg);font-family:ui-monospace,monospace;">${escapeHtml(u.text)}</textarea>
      ${pv ? "" : `<input type="text" id="tr-reason" value="${escapeHtml(u.reason)}"
        placeholder="이관 사유(선택) — 이력에 남습니다"
        style="width:100%;margin-top:6px;padding:8px;border:1px solid var(--border);border-radius:8px;background:var(--bg);">`}
      <div class="inline-row" style="margin:8px 0 0;">
        <span class="muted">인식된 자산번호 <b>${nums.length}</b>개</span>
        <span style="flex:1"></span>
        ${pv ? `<button class="btn btn-sm" id="tr-again">다시 입력</button>
                <button class="btn btn-sm btn-primary" id="tr-commit" ${ok ? "" : "disabled"}>🏬 ${ok}대 이관</button>`
             : `<button class="btn btn-sm btn-primary" id="tr-preview" ${nums.length && !u.busy ? "" : "disabled"}>
                  ${u.busy ? "확인 중…" : `🔍 ${nums.length}건 확인하기`}</button>`}
      </div>

      ${pv ? `<div style="border-top:1px solid var(--border);margin-top:10px;padding-top:10px;">
        <div style="background:var(--primary-soft);border-radius:8px;padding:8px 10px;margin-bottom:8px;">
          아직 <b>아무것도 바뀌지 않았습니다</b> — 아래를 확인하고 [이관]을 눌러야 처리됩니다.
          <span class="muted">(${escapeHtml(pv.commitId)})</span></div>
        <div style="color:var(--success,#0a7);font-weight:600;">✅ 넘길 수 있는 자산 ${ok}대</div>
        ${warned.length ? `<details><summary style="color:#b45309;cursor:pointer;">⚠ 넘어가지만 확인이 필요한 ${warned.length}건</summary>
          <ul class="muted" style="margin:6px 0 8px 18px;">${warned.map((i) =>
            `<li><b>${escapeHtml(i.assetNo)}</b> — ${escapeHtml(i.note)}</li>`).join("")}</ul>
          <p class="muted" style="margin:0 0 8px;">RMS와 OWS가 서로 다른 정보를 들고 있습니다.
            표기 차이면 그대로 진행해도 됩니다.</p></details>` : ""}
        ${bad.length ? `<details open><summary style="color:var(--danger);cursor:pointer;">⛔ 넘길 수 없는 ${bad.length}건</summary>
          <ul class="muted" style="margin:6px 0 8px 18px;">${bad.map((i) =>
            `<li><b>${escapeHtml(i.assetNo)}</b> — ${escapeHtml(i.note)}</li>`).join("")}</ul>
          ${bad.filter((i) => i.overridable).length ? `<div class="inline-row" style="margin:0 0 8px;">
            <button class="btn btn-sm" id="tr-unhold"
              title="반품·오등록이라 그 기록이 틀린 경우에만 쓰세요. 누가 왜 풀었는지 남고,
확정 화면에도 '보류 해제됨'으로 표시됩니다.">🔓 ${bad.filter((i) => i.overridable).length}건 사유 풀고 다시 확인</button>
            <span class="muted" style="font-size:12px;">기록이 틀린 건(매입중복·판매기록)만 풀 수 있습니다</span>
          </div>` : ""}</details>` : ""}
        ${miss.length ? `<details><summary style="color:var(--danger);cursor:pointer;">❓ 번호를 찾을 수 없는 ${miss.length}건</summary>
          <ul class="muted" style="margin:6px 0 8px 18px;">${miss.map((i) =>
            `<li><b>${escapeHtml(i.assetNo)}</b> — ${escapeHtml(i.note)}</li>`).join("")}</ul></details>` : ""}
        ${noop.length ? `<div class="muted">· 이미 그쪽 사업부 ${noop.length}건</div>` : ""}
      </div>` : ""}

      ${r ? `<div style="border-top:1px solid var(--border);margin-top:10px;padding-top:10px;">
        <div style="color:var(--success,#0a7);font-weight:600;">✔ ${r.assetCount}대 이관 완료
          <span class="muted">(${escapeHtml(r.commitId)})</span></div>
        <div class="muted" style="margin-top:4px;">
          ${r.direction === "rental->sale"
            ? "판매 재고에 <b>검수 대기(입고·실재고)</b>로 도착했습니다. 확인 후 재고반영을 켜세요."
            : "렌탈 시스템(RMS)에서 [📥 자산 받기]를 누르면 렌탈 자산으로 들어갑니다."}</div>
        ${r.rmsAckedAt ? "" : `<div class="inline-row" style="margin:8px 0 0;">
          <button class="btn btn-sm" id="tr-rollback">되돌리기</button></div>`}
      </div>` : ""}`}
    </div>`;

  const close = async () => { await dropTransferPreview(); host.innerHTML = ""; };
  $("#tr-close", host).addEventListener("click", close);
  // ★이건 탭이지 실행 버튼이 아니다. 파란 [이관하기] 버튼으로 뒀더니 번호를 넣고
  //   그걸 누르는 분이 계셨다(대표 2026-09-01: "버튼 눌러도 아무 반응이 없어").
  //   실행은 아래 [🔍 N건 확인하기]다. 이름도 동사에서 명사('새 이관')로 바꿨다.
  $("#tr-tab-do", host)?.addEventListener("click", () => {
    u.tab = "do"; renderTransfer(body, host);
  });
  $("#tr-tab-hist", host)?.addEventListener("click", async () => {
    u.tab = "hist"; renderTransfer(body, host);
    await loadTrHistory(body, host);
  });
  $("#trh-dir", host)?.addEventListener("change", async (e) => {
    u.histDir = e.target.value; await loadTrHistory(body, host);
  });
  $("#trh-q", host)?.addEventListener("keydown", async (e) => {
    if (e.key === "Enter") { u.histQ = e.target.value; await loadTrHistory(body, host); }
  });
  $("#trh-search", host)?.addEventListener("click", async () => {
    u.histQ = $("#trh-q", host)?.value || ""; await loadTrHistory(body, host);
  });
  $("#tr-dir", host)?.addEventListener("change", (e) => { u.dir = e.target.value; });
  $("#tr-text", host)?.addEventListener("input", (e) => {
    u.text = e.target.value;
    const n = TR.nums(u.text).length;
    const btn = $("#tr-preview", host);
    if (btn) { btn.disabled = !n; btn.textContent = `🔍 ${n}건 확인하기`; }
  });
  $("#tr-reason", host)?.addEventListener("input", (e) => { u.reason = e.target.value; });

  $("#tr-preview", host)?.addEventListener("click", async () => {
    u.busy = true; u.result = null; renderTransfer(body, host);
    try {
      u.preview = await api("/api/transfers", { method: "POST",
        body: { direction: u.dir, assetNos: TR.nums(u.text), reason: u.reason } });
    } catch (err) { toast(err.message, true); }
    u.busy = false; renderTransfer(body, host);
  });
  // ★이관 창에서 바로 풀기(대표 2026-09-01: "3단계는 번거로워").
  //   푼 뒤에는 미리보기를 새로 만든다 — 옛 제안에는 차단으로 굳어 있어서다.
  $("#tr-unhold", host)?.addEventListener("click", async () => {
    const targets = ((u.preview && u.preview.items) || []).filter((i) => i.overridable);
    if (!targets.length) return;
    // ★한 자산에 두 사유가 겹칠 수 있어 서버가 'dup_buy,sold_rec' 처럼 묶어 보낸다.
    const kinds = [...new Set(targets.flatMap((i) => String(i.overridable).split(",")))]
      .filter(Boolean);
    const label = kinds.map((k) => (HOLD_LABEL[k] || [k])[0]).join("·");
    const note = prompt(
      `${targets.length}건의 [${label}] 보류를 풀고 다시 확인합니다.

${targets.slice(0, 8).map((i) => i.assetNo).join(" · ")}${targets.length > 8 ? ` 외 ${targets.length - 8}건` : ""}

왜 푸는지 적어 주세요 — 이력에 남고 확정 화면에도 표시됩니다.`,
      "TMS 반입처리 누락 — 실물 확인함");
    if (note === null || !note.trim()) { toast("사유를 적어야 합니다.", true); return; }
    u.busy = true; renderTransfer(body, host);
    try {
      await api("/api/assets/hold-override", { method: "POST",
        body: { assetNos: targets.map((i) => i.assetNo), kinds, note: note.trim() } });
      await dropTransferPreview();          // 옛 제안은 버리고
      u.preview = await api("/api/transfers", { method: "POST",   // 새로 판정한다
        body: { direction: u.dir, assetNos: TR.nums(u.text), reason: u.reason } });
      toast(`${targets.length}건 보류를 풀었습니다.`);
    } catch (err) { toast(err.message, true); }
    u.busy = false; renderTransfer(body, host);
  });

  $("#tr-again", host)?.addEventListener("click", async () => {
    await dropTransferPreview(); renderTransfer(body, host);
  });
  $("#tr-commit", host)?.addEventListener("click", async () => {
    if (!confirm(`${TR.dirLabel(u.dir)}\
${ok}대를 이관합니다. 계속할까요?`)) return;
    try {
      u.result = await api(`/api/transfers/${u.preview.commitId}/commit`, { method: "POST" });
      u.preview = null;
      toast(`${u.result.assetCount}대 이관 완료`);
      state.assetPicked.clear();
      renderTransfer(body, host);
    } catch (err) {
      // 409 = 제안 이후 상황이 바뀐 것. 바뀐 사유를 그대로 다시 그려 준다.
      toast(err.message, true);
      try {
        u.preview = await api(`/api/transfers/${u.preview.commitId}`);
        renderTransfer(body, host);
      } catch (_e) {}
    }
  });
  $("#tr-rollback", host)?.addEventListener("click", async () => {
    if (!confirm(`${u.result.commitId} 이관을 되돌립니다. 계속할까요?`)) return;
    try {
      await api(`/api/transfers/${u.result.commitId}/rollback`, { method: "POST" });
      toast("이관을 되돌렸습니다.");
      host.innerHTML = "";
      renderAssetList(body);
    } catch (err) { toast(err.message, true); }
  });
}

/* 이관 내역 — 자산 한 대가 한 줄이다. '커밋 장부'가 아니라 '이 물건이 어디로 갔나'를 본다.
   ★원장은 OWS 한 곳뿐이고 RMS 화면도 같은 창구를 읽는다 — 양쪽이 다른 말을 하면 안 된다. */
const TRH_DIR = {
  "rental->sale": ["렌탈 → 판매", "#eef2ff", "#3730a3"],
  "sale->rental": ["판매 → 렌탈", "#ecfdf5", "#065f46"],
  "ows->rms": ["매입 → RMS 등록", "#fff7ed", "#9a3412"],
};

function renderTrHistory(u) {
  const h = u.hist;
  if (!h) return `<div class="muted" style="padding:12px 2px;">내역을 불러오는 중…</div>`;
  if (h.error) {
    return `<div class="muted" style="color:var(--danger);padding:12px 2px;">
      ⛔ ${escapeHtml(h.error)}</div>`;
  }
  const rows = h.rows || [];
  return `
    <div class="inline-row" style="margin:0 0 8px;">
      <select id="trh-dir">
        <option value="" ${u.histDir === "" ? "selected" : ""}>전체 방향</option>
        <option value="rental->sale" ${u.histDir === "rental->sale" ? "selected" : ""}>렌탈 → 판매</option>
        <option value="sale->rental" ${u.histDir === "sale->rental" ? "selected" : ""}>판매 → 렌탈</option>
        <option value="ows->rms" ${u.histDir === "ows->rms" ? "selected" : ""}>매입 → RMS 등록</option>
      </select>
      <input type="text" id="trh-q" value="${escapeHtml(u.histQ)}" placeholder="자산번호·모델·커밋번호"
        style="flex:1;min-width:140px;padding:6px 8px;border:1px solid var(--border);
               border-radius:8px;background:var(--bg);">
      <button class="btn btn-sm" id="trh-search">조회</button>
      <span class="muted">${rows.length.toLocaleString("ko-KR")}건</span>
    </div>
    ${h.pending ? `<div class="muted" style="border:1px solid var(--warn,#f59e0b);border-radius:8px;
      padding:6px 10px;margin-bottom:8px;">⚠ RMS가 아직 받아가지 않은 이관 ${h.pending}건이 있습니다 —
      RMS 화면 상단의 [📥 자산 받기]로 받아야 양쪽 장부가 맞습니다.</div>` : ""}
    <div class="table-wrap" style="max-height:52vh;overflow:auto;">
      <table><thead><tr>
        <th>일시</th><th>관리번호</th><th>모델</th><th>방향</th><th>지금</th><th>처리</th><th>사유</th>
      </tr></thead><tbody>
      ${rows.length === 0 ? `<tr><td colspan="7" class="muted" style="padding:16px;text-align:center;">
        오간 자산이 없습니다.</td></tr>` : rows.map((r) => {
        const [lab, bg, fg] = TRH_DIR[r.direction] || [r.direction, "#f1f5f9", "#334155"];
        return `<tr${r.rolledBack ? ` style="opacity:.55;"` : ""}>
          <td class="muted" style="white-space:nowrap;">${escapeHtml((r.at || "").slice(0, 16).replace("T", " "))}</td>
          <td><b>${escapeHtml(r.assetNo)}</b></td>
          <td>${escapeHtml((r.model || "").slice(0, 26))}</td>
          <td><span class="chip" style="background:${bg};color:${fg};border:1px solid ${fg}33;
                font-size:11px;">${lab}</span></td>
          <td>${r.nowDivision === "rental" ? "렌탈" : r.nowDivision === "sale" ? "판매" : "-"}</td>
          <td style="white-space:nowrap;">
            ${r.rolledBack ? `<span class="chip" title="이관을 되돌렸습니다"
                style="background:#fee2e2;color:#991b1b;border:1px solid #fca5a5;font-size:11px;">되돌림</span>`
              : r.pending ? `<span class="chip" title="OWS는 적용했는데 RMS가 아직 안 받았습니다"
                style="background:#fef3c7;color:#92400e;border:1px solid #fcd34d;font-size:11px;">RMS 대기</span>`
              : `<span class="muted" style="font-size:11px;">완료</span>`}
            ${r.commitId ? `<div class="muted" style="font-size:11px;font-family:ui-monospace,monospace;">
              ${escapeHtml(r.commitId)}</div>` : ""}
          </td>
          <td class="muted" style="font-size:12px;">${escapeHtml(r.reason || r.note || "")}
            ${r.by ? `<div style="font-size:11px;">${escapeHtml(r.by)}</div>` : ""}</td>
        </tr>`;
      }).join("")}
      </tbody></table>
    </div>`;
}

async function loadTrHistory(body, host) {
  const u = state.trUI;
  if (!u) return;
  u.hist = null;
  renderTransfer(body, host);
  const p = new URLSearchParams();
  if (u.histDir) p.set("direction", u.histDir);
  if (u.histQ) p.set("q", u.histQ);
  try {
    u.hist = await api(`/api/transfers/history?${p}`);
  } catch (e) {
    u.hist = { error: e.message, rows: [] };
  }
  renderTransfer(body, host);
}

/* ★버린 미리보기는 서버에서도 취소해야 한다. 안 그러면 그 자산이 다음부터
   '이미 이관에 포함돼 있습니다'로 계속 막힌다(2026-08-13 검증에서 실제 발생). */
async function dropTransferPreview() {
  const cid = state.trUI?.preview?.commitId;
  if (state.trUI) state.trUI.preview = null;
  if (cid) { try { await api(`/api/transfers/${cid}/reject`, { method: "POST" }); } catch (e) {} }
}

/* ---------------- 자산 목록 + 상세 ---------------- */

/* 재고집계 숫자를 눌러 자산 목록으로(2026-08-12 대표: "판매가능·작업중·보류 전부
   눌러서 확인"). 묶음(bucket)·모델은 집계와 같은 기준으로 서버가 거른다.
   ★maker/model은 빈 문자열도 조건이다(메이커 없는 모델·'(모델 미입력)') —
     truthiness로 거르면 안 되고 null 여부로만 가른다. */
const BUCKET_LABELS = { ready: "판매가능", working: "작업중", held: "보류", stock: "보유 재고" };

/* 사업부 배지 — 렌탈 귀속분만 표시한다. 판매가 기본이라 전부 달면 눈만 피로하다. */
function divBadge(a) {
  if (!a || a.division !== "rental") return "";
  const lock = a.divisionLocked ? " 🔒" : "";
  const t = a.divisionNote
    ? ` title="${escapeHtml(a.divisionNote)}"`
    : ' title="렌탈 사업부(RMS) 귀속 — 판매 재고·몰 재고·주문 매칭에서 제외됩니다"';
  return ` <span class="chip"${t} style="background:#eef2ff;color:#3730a3;border:1px solid #c7d2fe;font-size:11px;">렌탈${lock}</span>`;
}

/* 이관 보류 배지 — 정리해야 넘길 수 있는 자산. 막히기만 하고 안 보이면 정리가 안 된다. */
const HOLD_LABEL = {
  dup_buy: ["매입중복", "같은 시리얼이 두 번 등록돼 있습니다 — 어느 쪽이 실물인지 정리해야 이관됩니다"],
  sold_rec: ["판매기록", "렌탈 귀속인데 OWS에 판매전표/주문 기록이 있습니다 — 확인해야 이관됩니다"],
};
/* 보류 해제 배지(2026-09-01) — 사람이 "이 사유는 틀렸다"고 판정해 푼 자산.
   ★풀었다는 사실이 안 보이면 나중에 "왜 이건 그냥 넘어갔지"가 된다. */
function overrideBadge(a) {
  const ks = a?.holdOverride || [];
  if (!ks.length) return "";
  const label = ks.map((k) => (HOLD_LABEL[k] || [k])[0]).join("·");
  const who = [a.holdOverrideBy, (a.holdOverrideAt || "").slice(0, 10)].filter(Boolean).join(" ");
  return ` <span class="chip" title="${escapeHtml(label)} 보류를 풀었습니다${who ? ` (${escapeHtml(who)})` : ""}
사유: ${escapeHtml(a.holdOverrideNote || "-")}"
    style="background:#ecfdf5;color:#065f46;border:1px solid #6ee7b7;font-size:11px;">🔓 ${escapeHtml(label)} 해제</span>`;
}

function holdBadge(a) {
  return (a?.holdKinds || []).map((k) => {
    const [t, tip] = HOLD_LABEL[k] || [k, ""];
    return ` <span class="chip" title="${escapeHtml(tip)}" style="background:#fef3c7;color:#92400e;border:1px solid #fcd34d;font-size:11px;">⚠ ${t}</span>`;
  }).join("");
}

/* RMS 미등록 배지 — 렌탈로 정해졌는데 RMS에 실물 기록이 없는 자산(대표 2026-08-24).
   매입 전표에는 남고 OWS에서도 렌탈로 보이지만, RMS에 없으면 렌탈팀이 운용할 수 없다.
   inRms 가 null 이면 판정하지 않는다 — 판매 귀속이거나 RMS 사본을 아직 못 받은 상태다. */
function rmsBadge(a) {
  if (!a || a.inRms !== false) return "";
  return ` <span class="chip" title="렌탈 사업부 귀속인데 RMS에 자산이 없습니다 —
RMS 화면의 [📥 자산 받기]에서 내려받아야 렌탈팀이 운용할 수 있습니다"
    style="background:#fee2e2;color:#991b1b;border:1px solid #fca5a5;font-size:11px;">RMS 없음</span>`;
}

function assetListParams(f) {
  const p = new URLSearchParams();
  ["q", "status", "categoryId", "grade", "bucket", "received",
   "division", "issue"].forEach((k) => {
    if (f[k]) p.set(k, f[k]);
  });
  if (f.maker != null || f.model != null) {
    p.set("maker", f.maker || "");
    p.set("model", f.model || "");
  }
  return p;
}

function goAssetList(filter) {
  state.purchaseTab = "assets";
  state.assetView = "list";
  // ★집계(재고현황)에서 눌러 들어온 것은 판매 기준으로 센 숫자다 — 그 목록까지 렌탈을
  //   섞으면 "판매가능 12를 눌렀는데 13대"가 된다. 그 경우에만 판매로 고정한다.
  const div = filter.division || (filter.bucket ? "sale" : "all");
  state.assetFilter = { q: "", status: "", categoryId: "", grade: "",
                        ...filter, division: div };
  state.assetPicked = new Set();   // 이전 화면의 선택이 새 목록의 일괄 처리로 넘어오면 사고다
  resetPage("assets");
  renderPurchaseView($("#main"));
}

/* 집계에서 걸려 넘어온 조건 표시줄 — 안 보이는 필터는 "왜 안 나오지?"가 된다 */
/* ---------------- 🗂 분류 재판정 (2026-09-03, A5 재고 카테고리 정합) ----------------
   자산 카테고리를 TMS 중분류 축(노트북·데스크탑·태블릿·모니터·미니PC·일체형PC·주변기기·웨어러블)으로 한 번에 맞춘다.
   미리보기(현재→목표 건수, 재고·작업중/출고 완료/잠금/보호) → 체크 2개 → [적용](확인창) → 결과 → [↩ 되돌리기].
   ★보호(사람이 고친 값)는 기본 제외 — 아래 목록의 개별 체크로만 포함. 적용·되돌리기는 관리자(settings.manage) 전용.
   ★허공 패널: document.createElement 로 만든 div 를 openModalWith 에 바로 넘긴다(2026-08-24 규칙 — 닫으면 통째로 지워진다).
   ★서버 미러(app/purchase/migration.py TMS_SUBCAT_TO_CATEGORY — 바꿀 때 같이): 마스터 중분류 → 카테고리 이름. */
const SUBCAT_TO_CATEGORY = { "노트북": "노트북", "데스크탑": "데스크탑", "태블릿": "태블릿", "모니터": "모니터",
  "미니PC": "미니PC", "일체형PC": "일체형PC", "주변기기": "주변기기", "버즈": "웨어러블" };
// 카테고리 이름 → 모델 마스터 (대분류, 중분류). [＋ 마스터에 등록]이 TMS 모델분류 8종 모양으로 올린다
const CATEGORY_TO_MODEL_CLASS = { "노트북": ["PC", "노트북"], "데스크탑": ["PC", "데스크탑"], "모니터": ["PC", "모니터"],
  "미니PC": ["PC", "미니PC"], "일체형PC": ["PC", "일체형PC"], "주변기기": ["PC", "주변기기"],
  "태블릿": ["태블릿", "태블릿"], "웨어러블": ["웨어러블", "버즈"], "PC": ["PC", ""], "올인원": ["PC", "일체형PC"] };
const RECLASS_PROTECT = { edited: "카테고리 수정 이력", manual: "OWS 수기 등록", band: "OWS 번호대", shadow: "자동 판정 뒤 사람이 바꿈" };

async function openReclass(onDone) {
  /* 표준 팝업 골격(판매 전표 상세·TMS 신원과 같은 모양 — 2026-09-03 대표 "계속 개선했던 팝업창 형식으로"):
     머리줄(제목+기준 칩+[닫기]) → 요약 칩 한 줄 → 한 줄 설명(+접힌 규칙) → 옵션 줄(체크 2개 + 이번에 적용 N대)
     → 변경 표(6열, 근거는 칸 아래 한 줄, 합계 행) → 카테고리별 지금/적용 뒤/증감 → 접힌 보호·판정 불가 목록 → 버튼 줄. */
  const host = document.createElement("div");
  const n = (x) => Number(x || 0).toLocaleString("ko-KR");
  const head = (chips) => `<div class="inline-row" style="margin-bottom:6px;">
      <h3 style="margin:0; flex:1;">🗂 분류 재판정 ${chips || ""}</h3>
      <button class="btn btn-sm" id="rc-close">닫기</button></div>`;
  const plain = (msg) => {
    host.innerHTML = `<div class="card in-modal"><div>${head()}<p class="muted" style="margin:8px 0 0;">${msg}</p></div></div>`;
    $("#rc-close", host).addEventListener("click", () => closeModal());
  };
  plain("불러오는 중… 연동 사본의 재고 표(15,000여 행)를 읽어 판정합니다.");
  openModalWith(host);
  let d;
  try { d = await api("/api/assets/reclass/preview"); }
  catch (err) { plain(escapeHtml(err.message)); return; }
  if (!host.isConnected) return;                       // 읽는 동안 [닫기]를 눌렀다
  const u = { shipped: d.includeShipped !== false, locked: !!d.includeLocked, prot: new Set(),
              result: null, changed: false, open: {} };
  const canApply = hasPerm("settings.manage");
  const chip = (label, val, cls, extra) => `<span class="chip ${cls || "chip-slate"}"${extra || ""}>${label} ${val}</span>`;
  // 서버가 준 서로 겹치지 않는 묶음(보호 > 잠금 > 출고 순)으로 체크 상태에 따른 '이번에 적용' 수를 화면에서 바로 센다
  const rowApply = (c) => c.alive + (u.shipped ? c.gone : 0) + (u.locked ? c.lockedAlive + (u.shipped ? c.lockedGone : 0) : 0);
  const protOk = (p) => u.prot.has(p.id) && (!p.locked || u.locked) && (!p.gone || u.shipped);
  const protPick = (c) => d.protected.filter((p) => protOk(p) && p.from === c.from && p.to === c.to).length;   // 이 줄에서 체크로 포함한 보호 자산
  const rowTotal = (c) => rowApply(c) + protPick(c);
  const willApply = () => d.changes.reduce((s, c) => s + rowApply(c), 0) + d.protected.filter(protOk).length;

  const draw = () => {
    const res = u.result;
    const before = (res && res.before) || d.before || {};
    let after;
    if (res && res.after) {
      after = res.after;
    } else {
      after = Object.assign({}, before);
      const shift = (from, to, k) => { if (!k) return; after[from] = (after[from] || 0) - k; after[to] = (after[to] || 0) + k; };
      d.changes.forEach((c) => shift(c.from, c.to, rowApply(c)));
      d.protected.forEach((p) => { if (protOk(p)) shift(p.from, p.to, 1); });
    }
    const cats = [...new Set([...(d.categories || []), ...Object.keys(before), ...Object.keys(after)])];
    const total = willApply();
    const t = d.totals;
    const last = d.lastApply;
    const dis = res ? "disabled" : "";
    const sum = d.changes.reduce((s, c) => { s.count += c.count; s.alive += c.alive + c.lockedAlive;
      s.gone += c.gone + c.lockedGone; s.apply += rowTotal(c); return s; }, { count: 0, alive: 0, gone: 0, apply: 0 });
    const delta = (k) => {
      const v = (after[k] || 0) - (before[k] || 0);
      return v ? `<b style="color:${v > 0 ? "var(--primary)" : "var(--danger)"};">${v > 0 ? "+" : "−"}${n(Math.abs(v))}</b>` : '<span class="muted">–</span>';
    };
    const basisChip = d.basis === "datalink"
      ? `<span class="chip chip-blue" title="연동 창구 사본 HB_TBL재고 ${n(d.tmsRows)}행의 중분류로 판정">TMS 사본 기준</span>`
      : `<span class="chip chip-amber" title="${escapeHtml(d.warning || "")}">모델 마스터만 · 경고</span>`;
    const stateChip = res ? (res.undoneAt ? '<span class="chip chip-slate">↩ 되돌림</span>' : '<span class="chip chip-green">적용 완료</span>') : "";
    const und = d.undecidedSample || [];
    // ★카드 직계 자식은 .in-modal > *:not(.card) 규칙으로 각각 한 블록이 된다 — 관련 요소를 <div>로 묶어 6블록만 남긴다
    host.innerHTML = `<div class="card in-modal">
      <div>
      ${head(basisChip + " " + stateChip)}
      ${d.warning ? `<p style="margin:8px 0 0; font-size:12.5px; color:var(--amber, #b45309);">⚠ ${escapeHtml(d.warning)}</p>` : ""}
      ${res ? `<div class="card" style="margin:8px 0 0; padding:12px 14px; background:var(--primary-soft);">
          <b>적용 완료</b> — ${n(res.applied)}대 바꿈${res.skipped ? ` · 그 사이 다른 곳에서 바뀐 ${n(res.skipped)}대 건너뜀` : ""}.
          자산마다 이력 '분류재판정'이 남았습니다.
          ${res.undoneAt ? `<div class="muted" style="margin-top:4px;">↩ 되돌렸습니다 (${n(res.restored)}대) — 그 뒤 사람이 따로 바꾼 자산은 건너뜁니다.</div>` : ""}
        </div>`
        : (last && last.applied ? `<p class="muted" style="font-size:12.5px; margin:8px 0 0;">마지막 적용 ${
            escapeHtml(String(last.ts || "").slice(0, 16).replace("T", " "))} · ${escapeHtml(last.by || "")} · ${n(last.applied)}대${
            last.undoneAt ? ` · <span class="chip chip-slate" style="font-size:11px;">↩ 되돌림 ${n(last.restored)}대</span>`
              : (canApply ? ` <button class="btn btn-sm btn-ghost" id="rc-undo" data-audit="${last.id}" title="마지막 적용을 되돌립니다">↩ 되돌리기</button>` : "")}</p>` : "")}
      </div>
      <div>
      <div class="inline-row" style="gap:6px; flex-wrap:wrap; margin-bottom:8px;">
        ${chip("전체", n(t.assets) + "대")}
        ${chip("TMS 대조", n(t.inTms), "chip-slate", ' title="연동 사본의 재고 표에 있는 자산"')}
        <span class="chip chip-blue" style="font-size:13px;">변경 <b>${n(t.change)}</b></span>
        ${chip("변경 없음", n(t.same))}
        ${chip("판정 불가", n(t.undecided), t.undecided ? "chip-amber" : "chip-slate", ' title="TMS 중분류·모델 마스터·대분류 어느 것으로도 못 정한 자산 — 그대로 둡니다"')}
        ${chip("보호", n(t.protected), t.protected ? "chip-red" : "chip-slate", ' title="사람이 고친 값 — 기본 제외, 아래 목록에서 체크한 것만 바뀝니다"')}
        ${chip("잠금", n(t.locked), "chip-slate", ' title="TMS 정정 잠금 자산 — 체크로만 포함"')}
      </div>
      <p class="muted" style="font-size:12.5px; margin:0 0 4px;">TMS 재고 표의 <b>중분류</b>(노트북·데스크탑·태블릿·모니터·미니PC·일체형PC·주변기기·웨어러블)를 기준으로
        자산 카테고리를 맞춥니다. 사람이 고친 값은 <b>보호</b>되어 기본 제외입니다.</p>
      <details id="rc-rules-det" style="margin:0;" ${u.open.rules ? "open" : ""}>
        <summary class="muted" style="font-size:12.5px; cursor:pointer;">규칙·보호 기준 자세히</summary>
        <div class="muted" style="font-size:12.5px; margin:6px 0 0 14px; line-height:1.7;">
          ① TMS 중분류 → ② 비어 있으면 모델 마스터 중분류 → ③ 그래도 없으면 대분류 표기 → ④ 못 정하면 그대로 둡니다(기본값으로 밀지 않음).<br>
          보호(기본 제외): 카테고리 수정 이력 · OWS 수기 등록 · OWS 번호대 · 자동 판정 뒤 사람이 바꾼 값 — 아래 '보호 자산' 목록에서 체크한 것만 바뀝니다.<br>
          잠금: TMS 정정 잠금(번호·상태를 바로잡은 자산) — 따로 세고 체크로만 포함. 출고 완료·폐기·취소분은 기본 포함(체크를 끄면 제외).<br>
          적용하면 자산마다 이력 '분류재판정'이 남고, 이 창의 [↩ 되돌리기]로 마지막 적용을 되돌릴 수 있습니다(그 뒤 사람이 바꾼 자산은 건너뜀).
        </div>
      </details>
      </div>
      <div>
      <div class="inline-row" style="gap:16px; flex-wrap:wrap; margin-bottom:8px;">
        <label class="check-line" style="font-size:13px;"><input type="checkbox" id="rc-shipped" ${u.shipped ? "checked" : ""} ${dis}> <span>출고 완료·폐기·취소 포함</span></label>
        <label class="check-line" style="font-size:13px;"><input type="checkbox" id="rc-locked" ${u.locked ? "checked" : ""} ${dis}> <span>TMS 정정 잠금 포함</span></label>
        <span style="margin-left:auto; font-size:14px;">${res ? "적용한 자산" : "이번에 적용"} <b id="rc-total" style="font-size:22px; color:var(--primary);">${n(res ? res.applied : total)}</b>대</span>
      </div>
      <div class="table-wrap"><table>
        <thead><tr><th>현재 → 목표</th><th style="text-align:right;">대수</th><th style="text-align:right;">재고·작업중</th>
          <th style="text-align:right;">출고 완료</th>
          <th style="text-align:right;" title="잠금·보호 자산과 체크를 끈 출고 완료분 — 이번에 안 바꿉니다">제외</th>
          <th style="text-align:right;">이번에 적용</th></tr></thead>
        <tbody>${d.changes.length ? d.changes.map((c) => {
          const ap = rowTotal(c);
          const lkEx = u.locked ? 0 : c.lockedAlive + c.lockedGone;
          const prEx = c.protected - protPick(c);
          const basis = Object.entries(c.basis || {}).sort((a, b) => b[1] - a[1]).slice(0, 2).map(([k, v]) => `${k || "근거 없음"} ${n(v)}`).join(" · ");
          return `<tr>
          <td><b>${escapeHtml(c.from)}</b> → <b>${escapeHtml(c.to)}</b>${basis ? `<div class="muted" style="font-size:12px;">${escapeHtml(basis)}</div>` : ""}</td>
          <td style="text-align:right;">${n(c.count)}</td>
          <td style="text-align:right;">${n(c.alive + c.lockedAlive)}</td>
          <td style="text-align:right;" class="muted">${n(c.gone + c.lockedGone)}</td>
          <td style="text-align:right;" class="muted">${n(c.count - ap)}${(lkEx || prEx) ? `<div style="font-size:11px;">${
            [lkEx ? `잠금 ${n(lkEx)}` : "", prEx ? `<span style="color:var(--danger);">보호 ${n(prEx)}</span>` : ""].filter(Boolean).join(" · ")}</div>` : ""}</td>
          <td style="text-align:right;"><b${ap ? "" : ' class="muted"'}>${n(ap)}</b></td>
        </tr>`; }).join("") + `<tr style="font-weight:700; background:var(--slate-soft);">
          <td>합계</td><td style="text-align:right;">${n(sum.count)}</td><td style="text-align:right;">${n(sum.alive)}</td>
          <td style="text-align:right;">${n(sum.gone)}</td><td style="text-align:right;">${n(sum.count - sum.apply)}</td>
          <td style="text-align:right; color:var(--primary);">${n(sum.apply)}</td></tr>`
          : `<tr><td colspan="6" class="muted">바꿀 자산이 없습니다 — 카테고리가 이미 TMS 중분류와 맞습니다.</td></tr>`}</tbody></table></div>
      </div>
      <div>
      <div style="margin:0 0 6px; font-weight:700;">카테고리별 대수 — 지금 / 적용 뒤${res ? "" : "(예상)"}</div>
      <div class="table-wrap"><table style="font-size:13px; max-width:520px;">
        <thead><tr><th>카테고리</th><th style="text-align:right;">지금</th><th style="text-align:right;">적용 뒤</th><th style="text-align:right;">증감</th></tr></thead>
        <tbody>${cats.map((k) => `<tr><td>${escapeHtml(k)}</td><td style="text-align:right;">${n(before[k])}</td>
          <td style="text-align:right;"><b>${n(after[k])}</b></td><td style="text-align:right;">${delta(k)}</td></tr>`).join("")}</tbody></table></div>
      </div>
      ${(d.protectedCount || t.undecided) ? "<div>" : ""}
      ${d.protectedCount ? `<details id="rc-prot-det" style="margin:0;" ${u.open.prot ? "open" : ""}>
        <summary style="cursor:pointer;">보호 자산 <b style="color:var(--danger);">${n(d.protectedCount)}</b>대 — 사람이 고친 값, 체크한 것만 바꿉니다${
          d.protectedCount > d.protected.length ? ` <span class="muted">(처음 ${n(d.protected.length)}대만 표시)</span>` : ""}${
          u.prot.size ? ` <span class="chip chip-blue" style="font-size:11px;">체크 ${n(u.prot.size)}</span>` : ""}</summary>
        <div class="table-wrap" style="max-height:260px; overflow:auto; margin-top:6px;"><table style="font-size:13px;">
          <thead><tr><th style="width:28px;"></th><th>관리번호</th><th>현재 → 목표</th><th>보호 사유</th><th>상태</th><th>근거</th></tr></thead>
          <tbody>${d.protected.map((p) => `<tr>
            <td><input type="checkbox" class="rc-prot" data-id="${p.id}" ${u.prot.has(p.id) ? "checked" : ""} ${dis}></td>
            <td><b>${escapeHtml(p.assetNo)}</b>${p.locked ? ' <span class="chip chip-slate" style="font-size:11px;">잠금</span>' : ""}</td>
            <td>${escapeHtml(p.from)} → ${escapeHtml(p.to)}</td>
            <td style="color:var(--danger);">${escapeHtml(RECLASS_PROTECT[p.protected] || p.protectedLabel || p.protected || "")}</td>
            <td class="muted">${escapeHtml(statusLabel(p.status))}</td>
            <td class="muted" style="font-size:12px;">${escapeHtml(p.basis || "")}</td>
          </tr>`).join("")}</tbody></table></div></details>` : ""}
      ${t.undecided ? `<details id="rc-und-det" style="margin:${d.protectedCount ? "8px" : "0"} 0 0;" ${u.open.und ? "open" : ""}>
        <summary style="cursor:pointer;">판정 불가 ${n(t.undecided)}대 — 예시${und.length < t.undecided ? ` <span class="muted">(처음 ${n(und.length)}대)</span>` : ""}</summary>
        <div class="table-wrap" style="max-height:220px; overflow:auto; margin-top:6px;"><table style="font-size:13px;">
          <thead><tr><th>관리번호</th><th>현재</th><th>상태</th><th>구분</th></tr></thead>
          <tbody>${und.map((p) => `<tr><td><b>${escapeHtml(p.assetNo)}</b></td><td>${escapeHtml(p.from)}</td>
            <td class="muted">${escapeHtml(statusLabel(p.status))}</td><td class="muted">${p.division === "rental" ? "렌탈" : "판매"}</td></tr>`).join("")}</tbody></table></div>
        <p class="muted" style="font-size:12px; margin:6px 0 0;">TMS 중분류·모델 마스터·대분류 어느 것으로도 못 정한 자산 — 그대로 둡니다. 모델 마스터에 중분류를 채우면 다음 미리보기에서 판정됩니다.</p>
      </details>` : ""}
      ${(d.protectedCount || t.undecided) ? "</div>" : ""}
      <div class="editor-actions">
        ${res ? `${!res.undoneAt && canApply ? `<button class="btn" id="rc-undo" data-audit="${res.auditId}" title="이번 적용을 되돌립니다">↩ 되돌리기</button>` : ""}
            <button class="btn btn-primary" id="rc-close2">닫기</button>`
          : `<button class="btn" id="rc-close2">닫기</button>
            ${canApply ? `<button class="btn btn-primary" id="rc-apply" ${total ? "" : "disabled"}>적용 ${n(total)}대</button>`
              : `<span class="muted" style="font-size:12px; align-self:center;">적용·되돌리기는 관리자만 할 수 있습니다</span>`}`}
      </div>
    </div>`;
    wire();
  };

  const wire = () => {
    const remember = () => { u.open = { rules: !!$("#rc-rules-det", host)?.open, prot: !!$("#rc-prot-det", host)?.open, und: !!$("#rc-und-det", host)?.open }; };
    const close = () => { closeModal(); if (u.changed && onDone) onDone(); };
    $("#rc-close", host).addEventListener("click", close);
    $("#rc-close2", host)?.addEventListener("click", close);
    $("#rc-shipped", host)?.addEventListener("change", (e) => { remember(); u.shipped = e.target.checked; draw(); });
    $("#rc-locked", host)?.addEventListener("change", (e) => { remember(); u.locked = e.target.checked; draw(); });
    $$(".rc-prot", host).forEach((cb) => cb.addEventListener("change", () => {
      remember();
      if (cb.checked) u.prot.add(Number(cb.dataset.id)); else u.prot.delete(Number(cb.dataset.id));
      draw();
    }));
    $("#rc-apply", host)?.addEventListener("click", async () => {
      const total = willApply();
      if (!total) return;
      // ★안내문은 역따옴표로 쓴다 — 줄바꿈 이스케이프가 도구를 거치며 깨진 적이 있다
      if (!confirm(`카테고리 ${n(total)}대를 바꿉니다.

※ 먼저 설정 ▸ 백업에서 [지금 백업]을 눌러 건수를 확인하셨나요?
바뀐 자산마다 이력 '분류재판정'이 남고, 이 창의 [↩ 되돌리기]로 되돌릴 수 있습니다.`)) return;
      const btn = $("#rc-apply", host);
      btn.disabled = true; btn.textContent = "적용 중…";
      try {
        const r = await api("/api/assets/reclass/apply", { method: "POST",
          body: { includeShipped: u.shipped, includeLocked: u.locked, protectedIds: [...u.prot] } });
        u.result = r; u.changed = true;
        toast(`카테고리 ${n(r.applied)}대를 바꿨습니다.`);
        remember(); draw();
      } catch (err) { toast(err.message, true); btn.disabled = false; btn.textContent = `적용 ${n(total)}대`; }
    });
    $("#rc-undo", host)?.addEventListener("click", async () => {
      const auditId = Number($("#rc-undo", host).dataset.audit);
      if (!confirm("마지막 적용을 되돌립니다. 그 뒤 사람이 따로 바꾼 자산은 건너뜁니다. 계속할까요?")) return;
      try {
        const r = await api("/api/assets/reclass/undo", { method: "POST", body: { auditId } });
        toast(`${n(r.restored)}대를 되돌렸습니다.` + (r.skipped ? ` (건너뜀 ${n(r.skipped)})` : ""));
        u.changed = true;
        const at = new Date().toISOString();
        if (u.result && u.result.auditId === auditId) { u.result.undoneAt = at; u.result.restored = r.restored; }
        if (d.lastApply && d.lastApply.id === auditId) { d.lastApply.undoneAt = at; d.lastApply.restored = r.restored; }
        remember(); draw();
      } catch (err) { toast(err.message, true); }
    });
  };
  draw();
}

function bucketChipHtml(f) {
  const chips = [];
  if (f.bucket) chips.push(`📦 ${BUCKET_LABELS[f.bucket] || f.bucket}`);
  if (f.maker != null || f.model != null)
    chips.push(`🏷 ${[f.maker, f.model].filter(Boolean).join(" ") || "(모델 미입력)"}`);
  if (f.received === "1") chips.push("📥 실물 입고분만");
  if (!chips.length) return "";
  return `<div class="inline-row" style="margin-top:6px;">
      <span class="muted">재고집계에서 넘어온 조건:</span>
      ${chips.map((c) => `<span class="chip chip-blue">${escapeHtml(c)}</span>`).join(" ")}
      <button class="btn btn-ghost btn-sm" id="af-bucket-clear">✕ 조건 해제</button>
    </div>`;
}

async function renderAssetList(body, loadMore) {
  const seq = ++state.renderSeq;
  const f = state.assetFilter || (state.assetFilter =
    // ★기본은 '사업부 전체'다. 매입은 렌탈·판매의 공통 앞단이라 여기서 렌탈 건이 숨으면
    //   담당자가 "매입에 없다"고 오해한다(대표 2026-08-24, P260805-001).
    //   판매 재고 숫자는 재고현황·집계가 따로 세므로 이 목록이 섞여도 흐려지지 않는다.
    { q: "", status: "", categoryId: "", grade: "", division: "all", issue: "" });
  if (!state.assetPicked) state.assetPicked = new Set();
  let assets, total;
  try {
    await ensureMeta();
    const params = assetListParams(f);
    // ★서버가 500대씩 준다. [더 보기]를 누르면 이어서 받아 붙인다 — 예전엔 최신
    //   500대에서 끊겨 판매완료 13,000여 대는 화면에서 영영 못 찾았다(2026-08-07).
    const key = params.toString();
    const prev = state.assetAcc && state.assetAcc.key === key ? state.assetAcc.rows : [];
    const offset = loadMore ? prev.length : 0;
    if (offset) params.set("offset", String(offset));
    const r = await api("/api/assets?" + params.toString());
    if (seq !== state.renderSeq) return;
    // 서버가 재시작 전(옛 코드)이면 배열이 온다 — 그때도 목록은 떠야 한다
    const page = Array.isArray(r) ? r : (r.rows || []);
    assets = (offset ? prev : []).concat(page);
    total = Array.isArray(r) ? assets.length : (r.total || assets.length);
    state.assetAcc = { key, rows: assets };
    // 일괄 선택은 id로 하는데 이관은 관리번호로 돈다(RMS와 맞춘 공통 키) — 짝을 기억해 둔다
    state.assetNoById = new Map(assets.map((a) => [a.id, a.assetNo]));
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
        <select id="af-issue" title="이관이 막히는 자산만 모아 봅니다 — 정리하면 넘길 수 있습니다">
          <option value="">전체</option>
          <option value="hold" ${f.issue === "hold" ? "selected" : ""}>⚠ 정리 대기(이관 막힘)</option>
          <option value="no-rms" ${f.issue === "no-rms" ? "selected" : ""}>⚠ 렌탈인데 RMS에 없음</option></select>
        <select id="af-div" title="렌탈 사업부(RMS) 귀속 자산은 판매 재고에 잡히지 않습니다">
          <option value="sale" ${f.division === "sale" ? "selected" : ""}>판매 사업부</option>
          <option value="rental" ${f.division === "rental" ? "selected" : ""}>렌탈 사업부</option>
          <option value="all" ${f.division === "all" ? "selected" : ""}>사업부 전체</option></select>
        ${hasPerm("purchase.edit") ? `<button class="btn btn-sm" id="af-restock"
          title="출고·폐기로 잡힌 자산을 입고·실재고로 되돌립니다.
반품돼 창고에 실물이 있는 것만 하세요 — 사유가 이력에 남습니다.
(TMS가 반입이라고 알려 준 건은 [↩ 복귀 후보] 탭에도 모여 있습니다)">↩ 재고 복귀</button>` : ""}
        ${hasPerm("purchase.edit") ? `<button class="btn btn-sm" id="af-unhold"
          title="고른 자산의 이관 보류(매입중복·판매기록)를 풉니다.
반품·오등록이라 기록이 틀린 경우에만 쓰세요 — 누가 왜 풀었는지 남습니다.">🔓 보류 해제</button>` : ""}
        <button class="btn btn-sm" id="af-transfer"
          title="렌탈 사업부(RMS)와 자산을 주고받습니다. 목록에서 고른 자산이 있으면 번호가 채워집니다.">↔ 사업부 이관</button>
        <button class="btn btn-sm" id="af-export">📤 엑셀 내보내기</button>
        <button class="btn btn-sm" id="af-reclass"
          title="자산 카테고리를 TMS 중분류(노트북·데스크탑·태블릿·모니터·미니PC·일체형PC·주변기기·웨어러블) 기준으로 다시 판정합니다.
먼저 '현재 → 목표' 건수를 보여 주고, 적용은 관리자만 할 수 있으며 되돌릴 수 있습니다.">🗂 분류 재판정</button>
        <span class="muted">${assets.length.toLocaleString("ko-KR")} / ${total.toLocaleString("ko-KR")}건</span>
        ${assets.length < total
          ? `<button class="btn btn-sm" id="af-more" title="다음 500대를 이어서 불러옵니다">＋ 더 보기</button>` : ""}
      </div>
      ${bucketChipHtml(f)}
      <div id="af-form"></div>
      <div id="af-bulkbar" style="display:none;"></div>
      <div id="ab-result"></div>
      <div class="table-wrap"><table>
        <thead><tr><th style="width:28px;"><input type="checkbox" id="af-all" title="이 목록 전체 선택"></th>
          <th>관리번호</th><th>분류</th><th>브랜드 / 모델</th><th>스펙</th><th>등급</th><th>상태</th><th>위치</th><th>매입가</th><th>판매가</th><th></th></tr></thead>
        <tbody>${pg.rows.map((a) => `
          <tr>
            <td><input type="checkbox" class="af-pick" data-pick="${a.id}" ${state.assetPicked.has(a.id) ? "checked" : ""}></td>
            <td><b>${escapeHtml(a.assetNo)}</b>${divBadge(a)}${holdBadge(a)}${overrideBadge(a)}${rmsBadge(a)}
              ${a.slipNo && a.batchId ? `<div><button class="link-btn af-slip" data-slip="${a.batchId}"
                title="이 전표의 전체 내용을 봅니다" style="font-size:11.5px;">${escapeHtml(a.slipNo)}</button>${
                  a.slipCancelled ? ` <span class="chip chip-red" style="font-size:10px;">🚫취소</span>`
                  : a.slipReturned ? ` <span class="chip chip-violet" style="font-size:10px;">↩반품</span>` : ""}</div>`
                : `<div class="muted" style="font-size:11.5px;">전표 없음</div>`}</td>
            <td>${escapeHtml(a.categoryName || "-")}</td>
            <td>${escapeHtml([a.maker, a.model].filter(Boolean).join(" ") || "-")}</td>
            <td class="muted" style="max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${escapeHtml(specSummary(a))}">${escapeHtml(specSummary(a) || "-")}</td>
            <td>${escapeHtml(a.grade)}</td>
            <td>${statusCell(a)}</td>
            <td>${escapeHtml(a.location || "-")}</td>
            <td>${fmtWon(a.purchasePrice)}</td>
            <td>${a.salePrice ? fmtWon(a.salePrice) : "-"}</td>
            <td><button class="btn btn-sm" data-asset="${a.id}">상세</button></td>
          </tr>`).join("") || `<tr><td colspan="11" class="muted">자산이 없습니다.${
            hasPerm("purchase.edit") ? " 오른쪽 위 [＋ 자산 등록]으로 등록하거나, [가입고 / 매입] 탭에서 전표를 만든 뒤 자산을 추가하세요." : ""}</td></tr>`}
        </tbody></table></div>
      ${pg.bar}
    </div>
    <div id="asset-detail"></div>
    <div id="ab-transfer-box"></div>`;
  wirePager(body, "assets", () => renderAssetList(body));
  const moreBtn = $("#af-more", body);
  if (moreBtn) moreBtn.addEventListener("click", () => renderAssetList(body, true));
  // 직전 일괄 처리 결과 — 왜 안 됐는지가 목록 새로고침으로 사라지면 같은 버튼을 반복하게 된다
  const res = state.assetBulkResult;
  if (res) {
    const host = $("#ab-result", body);
    if (host) {
      host.innerHTML = `
        <div style="border:1px solid ${res.failed.length ? "var(--danger)" : "var(--border)"};
                    border-radius:8px; padding:10px 12px; margin:8px 0;">
          <div class="inline-row" style="margin:0;">
            <b style="flex:1;">${escapeHtml(res.title || "일괄 변경")} — 성공 ${res.ok}대${res.failed.length
              ? ` · <span style="color:var(--danger)">건너뜀/실패 ${res.failed.length}대</span>` : ""}</b>
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
    f.division = $("#af-div").value;
    f.issue = $("#af-issue").value;
    // ★정리 대기는 렌탈·판매 양쪽에 걸쳐 있다 — 사업부를 좁혀 두면 절반만 보인다
    if (f.issue === "hold" && f.division !== "all") f.division = "all";
    // ★조건이 바뀌면 1쪽으로 — 3쪽을 보다 검색해 2건만 남으면 빈 화면이 된다
    resetPage("assets");
    renderAssetList(body);
  };
  $("#af-search").addEventListener("click", doSearch);
  $("#af-div").addEventListener("change", doSearch);
  $("#af-issue").addEventListener("change", doSearch);
  $("#af-transfer").addEventListener("click", () => openTransfer(body));
  // 재고 복귀(2026-09-01) — [↩ 복귀 후보] 탭은 TMS가 알려 준 것만 모은다.
  //   TMS 기록이 안 들어온 반품은 그 목록에 안 떠서 되돌릴 길이 없었다.
  $("#af-restock", body)?.addEventListener("click", async () => {
    const picked = [...(state.assetPicked || [])];
    if (!picked.length) { toast("먼저 목록에서 자산을 고르세요.", true); return; }
    const nos = picked.map((id) => state.assetNoById?.get(id)).filter(Boolean);
    const reason = prompt(
      `${picked.length}대를 입고·실재고로 되돌립니다.

${nos.slice(0, 8).join(" · ")}${nos.length > 8 ? ` 외 ${nos.length - 8}대` : ""}

창고에 실물이 있는지 확인하셨나요?
왜 되돌리는지 적어 주세요 — 이력에 남습니다.`,
      "반품 입고 — 실물 확인함");
    if (reason === null || !reason.trim()) { toast("사유를 적어야 합니다.", true); return; }
    try {
      const r = await api("/api/assets/restock", { method: "POST",
        body: { ids: picked, reason: reason.trim() } });
      let msg = `${r.ok}대를 입고·실재고로 되돌렸습니다.`;
      if (r.skipped.length) msg += ` (건너뜀 ${r.skipped.length})`;
      toast(msg);
      if (r.rentalKept && r.rentalKept.length) {
        // ★"재고복귀를 해도 재고 복귀가 안돼"의 정체 — 복귀는 됐는데 렌탈이라 판매 재고에서 빠진다.
        toast(`ℹ 그중 ${r.rentalKept.length}대는 렌탈 사업부라 판매 재고에는 안 잡힙니다. `
          + "팔 물건이면 [↔ 사업부 이관]까지 하세요.", true);
      }
      if (r.heldByOrder.length) {
        // ★아직 살아 있는 주문이 잡고 있으면 조용히 넘어가면 안 된다.
        toast(`⚠ 아직 주문이 잡고 있는 자산 ${r.heldByOrder.length}대 — `
          + r.heldByOrder.slice(0, 3).map((x) => `${x.assetNo}(${x.orderNo})`).join(", ")
          + " · 주문 쪽도 정리하세요.", true);
      }
      state.assetPicked.clear();
      renderAssetList(body);
    } catch (err) { toast(err.message, true); }
  });

  // 보류 해제(2026-09-01) — 반품·오등록이라 기록이 틀린 자산만 푼다. 사유는 필수.
  $("#af-unhold", body)?.addEventListener("click", async () => {
    const nos = [...(state.assetPicked || [])]
      .map((id) => state.assetNoById?.get(id)).filter(Boolean);
    if (!nos.length) { toast("먼저 목록에서 자산을 고르세요.", true); return; }
    // ★안내문은 역따옴표로 쓴다 — 줄바꿈 이스케이프가 도구를 거치며 깨진 적이 있다.
    const kind = prompt(
      `${nos.length}대의 이관 보류를 풉니다.

무엇을 풀까요? 번호를 입력하세요.
  1 = 판매기록 (반품·오등록이라 판매 기록이 틀린 경우)
  2 = 매입중복 (같은 시리얼이 두 번 등록된 경우)
  3 = 둘 다
  0 = 해제 취소(다시 막기)`, "1");
    if (kind === null) return;
    const map = { 1: ["sold_rec"], 2: ["dup_buy"], 3: ["sold_rec", "dup_buy"], 0: [] };
    const kinds = map[String(kind).trim()];
    if (!kinds) { toast("1 · 2 · 3 · 0 중에 고르세요.", true); return; }
    const undo = kinds.length === 0;
    const note = prompt(undo
      ? "다시 막는 이유를 적어 주세요."
      : `왜 푸는지 적어 주세요 — 나중에 이 기록이 근거가 됩니다.
예: TMS에서 반입처리 누락 — 실물은 창고에 있음(담당 확인)`);
    if (note === null || !note.trim()) { toast("사유를 적어야 합니다.", true); return; }
    try {
      const r = await api("/api/assets/hold-override", { method: "POST",
        body: { assetNos: nos, kinds, note: note.trim(), undo } });
      toast(`${r.doneCount}대 ${undo ? "다시 막았습니다" : "보류를 풀었습니다"}`
        + (r.unmatched.length ? ` (못 찾음 ${r.unmatched.length})` : ""));
      state.assetPicked.clear();
      renderAssetList(body);
    } catch (err) { toast(err.message, true); }
  });
  autoSearch("#af-q", doSearch);
  $("#af-bucket-clear", body)?.addEventListener("click", () => {
    delete f.bucket; delete f.maker; delete f.model; delete f.received;
    resetPage("assets");
    renderAssetList(body);
  });
  const newBtn = $("#af-new");
  if (newBtn) newBtn.addEventListener("click", async () => {
    try { state.slipsForPicker = await api("/api/purchase-batches"); }
    catch (_e) { state.slipsForPicker = []; }
    renderAssetForm(null, "#af-form", () => renderAssetList(body));
  });
  // 분류 재판정(2026-09-03, A5) — 미리보기 팝업. 적용·되돌리기를 했으면 닫을 때 목록을 다시 그린다
  $("#af-reclass", body)?.addEventListener("click", () => openReclass(() => renderAssetList(body)));
  $("#af-export").addEventListener("click", () => {
    // 화면에 보이는 조건 그대로 내려받는다(전에는 상태만 반영돼 전체가 나왔다)
    // — 재고집계에서 넘어온 묶음·모델 조건도 목록과 똑같이 탄다(assetListParams 공유)
    window.open("/api/assets/export?" + assetListParams(f).toString(), "_blank");
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
      <span>${escapeHtml(b.supplierName || "거래처 미지정")}${supplierOrigHint(b)}</span>
      <span class="muted">${escapeHtml(b.purchaseDate || "")}</span>
      <span style="flex:1"></span>
      <span class="muted">전표 금액 ${fmtWon(b.totalAmount)}</span>
    </div>`;
}

/* 별칭 거래처(2026-09-03, masters.py alias_of) — 서버가 supplierName 을 대표 이름으로 주고, 전표에 적힌 원문이 다르면
   supplierOrigName 에 담아 준다. 있을 때만 '(원 표기: …)' 를 곁들인다(전표 목록·전표 상세·자산 상세의 전표 줄 공통). */
function supplierOrigHint(b) {
  return b && b.supplierOrigName && b.supplierOrigName !== b.supplierName
    ? ` <span class="muted" style="font-size:11.5px;" title="전표에 적힌 거래처 표기(별칭). 거래처 마스터에서 대표로 묶여 있어 대표 이름으로 보입니다">(원 표기: ${escapeHtml(b.supplierOrigName)})</span>`
    : "";
}

/* 자산 상세/편집. 자산 목록뿐 아니라 전표 상세에서도 연다(대표 요청 2026-07-31:
   "전표 안 자산번호를 클릭하면 그 자리에서 수정하고 싶다").
     hostId : 그릴 위치(기본 자산 목록의 #asset-detail)
     onSaved: 저장 후 호출 — 부른 화면이 스스로 갱신하도록(전표 금액·재고가 같이 움직인다) */
/* 상태 선택지 — 사람이 고르는 건 넷뿐이다(대표 2026-08-24: 판매가능 / A·S / 수리 / 불량·부품용).
   ★옛 값(입고·정비중·도색대기)은 같은 그룹으로 접어 보여 주고, 저장하면 대표값으로 굳는다.
   ★주문매칭·출고완료 같은 자동 상태는 흐름이 정하는 값이라 고를 수 없게 둔다(현재 값만 표시). */
function statusOptionsHtml(cur) {
  const meta = state.meta || {};
  // ★서버가 아직 안 올라왔으면(재시작 전 캐시) 옛 목록으로 물러난다 —
  //   선택지가 통째로 비면 상태를 아예 못 바꾼다.
  const choices = (meta.statusChoices && meta.statusChoices.length)
    ? meta.statusChoices
    : (meta.statuses || []).filter((x) => (meta.manualStatuses || []).includes(x.code))
        .map((x) => ({ code: x.code, label: x.label, help: "" }));
  const group = (meta.statusGroup || {})[cur] || null;
  const rows = choices.map((c) => {
    const on = group ? c.code === group : c.code === cur;
    return `<option value="${c.code}" ${on ? "selected" : ""}>${escapeHtml(c.label)}</option>`;
  });
  if (!group) {                       // 자동 상태(주문매칭·출고완료·매입취소 등)
    const label = (meta.statuses || []).find((x) => x.code === cur);
    rows.unshift(`<option value="${escapeHtml(cur)}" selected disabled>${
      escapeHtml(label ? label.label : cur)} (흐름이 정한 상태 — 여기서 못 바꿉니다)</option>`);
  }
  return rows.join("");
}

/* 이력 detail → 사람이 읽는 한 줄(2026-09-03, A7).
   서버가 바뀐 칸을 {"from": 전, "to": 후}로 남기므로 '칸: 전 → 후'로 풀고, 나머지는 '칸: 값'.
   (예전처럼 JSON을 통째로 찍으면 {"maker":{"from":"삼성","to":"LG"}} 를 눈으로 읽어야 한다) */
function fmtEventDetail(detail) {
  if (detail == null || detail === "") return "";
  if (typeof detail !== "object" || Array.isArray(detail)) {
    return typeof detail === "string" ? detail : JSON.stringify(detail);
  }
  const show = (v) => (v == null || v === "" ? "(빈값)"
    : typeof v === "object" ? JSON.stringify(v) : typeof v === "boolean" ? (v ? "예" : "아니오") : String(v));
  const isFT = (v) => v && typeof v === "object" && !Array.isArray(v) && ("from" in v || "to" in v);
  const parts = [];
  // 상태변경·번호정정·등급처럼 detail 자체가 {from,to,…}인 옛 모양 — 맨 앞에 '전 → 후'
  if (isFT(detail)) parts.push(`${show(detail.from)} → ${show(detail.to)}`);
  Object.entries(detail).forEach(([k, v]) => {
    if (isFT(detail) && (k === "from" || k === "to")) return;
    parts.push(isFT(v) ? `${k}: ${show(v.from)} → ${show(v.to)}` : `${k}: ${show(v)}`);
  });
  return parts.join(" · ");
}

/* 자산 상세 💻 판매 정보(2026-09-03) — 회차 전부. rounds 는 GET /api/sale-slips/rounds?assetNo= 의 {count, rows}
   (권한·오류·판매 없음이면 null → 서버 tmsSale(취소 아닌 최신 1행) 카드만). 취소 행·전표째 취소된 행은 회차 없이 '취소'.
   마진 줄은 서버 tmsSale 의 계산(판매가 − 매입가·수리비)을 그대로 — 금액은 권한 없으면 서버가 null 로 준다. */
function assetSaleInfoHtml(a, rounds) {
  const rows = rounds && rounds.rows ? rounds.rows.slice().reverse() : [];      // 최근 회차 먼저
  const s = a.tmsSale;
  if (!rows.length && !s) return "";
  // ★돈 줄(2026-09-03 대표 "실제 TMS 기준에 맞춰져야 하는 건가?" → 대표 승인으로 판매 시점 원가 적재).
  //   예전에는 '판매가 − 지금 자산 매입가'를 마진이라 부르며 TMS 순이익과 나란히 뒀는데, 원가가
  //   달라서 라이브 5,901행 중 두 값이 맞는 행이 0건이었다. 이제 원가는 **판매하던 그 순간의
  //   제조원가**(TMS 판매상세 스냅샷)를 쓰고, 무엇을 뺀 값인지 화면이 직접 말한다.
  //   ★TMS 순이익이 있으면 그쪽이 정본이다 — 수수료·부가세까지 뺀 값이라 크게 보이는 쪽이 아니다.
  const costWord = { snapshot: "판매 당시 원가", slip: "명세 매입가", asset: "자산의 지금 매입가·수리비" };
  const marginLine = s ? `<div class="inline-row" style="gap:18px; flex-wrap:wrap; margin-top:6px;">
      <span class="muted">최근 판매(취소 제외) ${escapeHtml(s.slipNo || "")}</span>
      <span>판매가 <b>${fmtWon(s.salePrice)}</b></span>
      ${s.tmsProfit ? `<span>TMS 순이익 <b style="${s.tmsProfit < 0 ? "color:var(--danger);" : ""}">${fmtWon(s.tmsProfit)}</b>
        <span class="muted" style="font-size:12px;" title="TMS가 계산해 보내 준 값입니다 — 수수료·부가세까지 뺀 실제 남는 돈입니다.">TMS 값</span></span>` : ""}
      <span>판매가 − 원가 <b style="${s.margin < 0 ? "color:var(--danger);" : ""}">${fmtWon(s.margin)}</b>
        <span class="muted" style="font-size:12px;"
          title="${s.costSource === "snapshot"
            ? "판매하던 그 순간의 제조원가입니다(TMS 판매상세). 자산의 지금 매입가로 다시 계산하면 지난달 마진이 바뀝니다."
            : "판매 당시 원가가 아직 안 들어온 건이라 대신 쓰는 값입니다."}">원가 ${fmtWon(s.cost)} · ${escapeHtml(costWord[s.costSource] || "")}</span></span>
      ${s.tmsProfit ? `<span class="muted" style="font-size:12px;">— 수수료 ${fmtWon(s.saleFee)} · 실부가세 ${fmtWon(s.netVat)} 를 더 빼면 TMS 순이익이 됩니다</span>`
        : `<span class="muted" style="font-size:12px;">— 이 건은 TMS 순이익이 없습니다(수수료·부가세 미반영)</span>`}
    </div>` : "";
  if (!rows.length) {
    return `<div class="card" style="border-left:3px solid var(--primary);">
      <h3 style="margin-top:0;">💻 판매 정보 <span class="muted" style="font-weight:400; font-size:13px;">TMS ${escapeHtml(s.slipNo)}</span></h3>
      <div class="inline-row" style="gap:18px; flex-wrap:wrap;">
        <span>판매일 <b>${escapeHtml(s.saleDate)}</b></span>
        <span>고객 <b>${escapeHtml(s.customer || "-")}</b></span>
        <span>채널 ${escapeHtml(s.channel || "-")}</span>
      </div>${marginLine}
    </div>`;
  }
  const total = rounds.count || 0;
  const cancelled = rows.filter((r) => !r.round).length;
  return `<div class="card" style="border-left:3px solid var(--primary);">
      <h3 style="margin-top:0;">💻 판매 정보 <span class="muted" style="font-weight:400; font-size:13px;"
        title="같은 자산이 판매→반입→재판매로 돈 횟수. 취소 행은 회차에서 뺍니다.">회차 ${total}${cancelled ? ` · 취소 ${cancelled}` : ""} · 행 ${rows.length}</span></h3>
      <div class="table-wrap"><table>
        <thead><tr><th title="현재/전체 회차 — 취소 행은 회차 없이 '취소'">회차</th><th>전표</th><th>판매일</th><th>반입일</th><th>취소일</th>
          <th style="text-align:right;">판매가</th><th>상태</th><th>고객 · 채널</th></tr></thead>
        <tbody>${rows.map((r) => `<tr style="${r.round ? "" : "opacity:.6;"}">
          <td>${r.round ? `<b>${r.round}/${total}</b>` : '<span class="chip chip-red" style="font-size:11px;">취소</span>'}</td>
          <td>${escapeHtml(r.slipNo || "")}${r.source === "ows" ? ' <span class="chip chip-teal" style="font-size:10.5px;" title="OWS에서 등록한 전표">OWS</span>' : ""}</td>
          <td>${escapeHtml(r.saleDate || "")}</td>
          <td>${escapeHtml(r.returnDate || "")}</td>
          <td>${escapeHtml(r.cancelDate || "")}</td>
          <td style="text-align:right;">${fmtWon(r.salePrice)}</td>
          <td class="muted" style="font-size:12px;">${escapeHtml(r.stage || "")}${r.slipStage === "판매취소" ? " · 전표 취소" : ""}</td>
          <td class="muted" style="font-size:12px;">${escapeHtml([r.customer, r.channel].filter(Boolean).join(" · "))}</td>
        </tr>`).join("")}</tbody></table></div>${marginLine}
    </div>`;
}

async function renderAssetDetail(hostId, onSaved) {
  const host = $(hostId || "#asset-detail");
  if (!host || !state.assetDetailId) return;
  const reopen = () => renderAssetDetail(hostId, onSaved);
  host.innerHTML = `<div class="card"><p class="muted">불러오는 중…</p></div>`;
  revealPanel(host);
  let a;
  try { a = await api(`/api/assets/${state.assetDetailId}`); }
  catch (err) { host.innerHTML = `<div class="card"><p class="muted">${escapeHtml(err.message)}</p></div>`; return; }
  // 💻 판매 정보 = 회차 전부(2026-09-03) — 판매→반입→재판매가 같은 자산의 여러 행이라 최신 1행(tmsSale)만 보이면 회전을 놓친다.
  //   rounds 창구는 금액 계열 권한 문턱(purchase.money/reports.view/settings.manage)이라 없으면(또는 오류면) 예전처럼 tmsSale 카드로 폴백한다.
  let rounds = null;
  if (a.assetNo && ["purchase.money", "reports.view", "settings.manage"].some(hasPerm)) {
    try {
      const rr = await api(`/api/sale-slips/rounds?assetNo=${encodeURIComponent(a.assetNo)}`);
      rounds = (rr.assets || {})[a.assetNo] || null;
    } catch (_e) { rounds = null; }
  }
  if (String(state.assetDetailId) !== String(a.id)) return;   // 그 사이 다른 자산을 열었다(id 가 문자열로 올 수도 있다)
  const canEdit = hasPerm("purchase.edit");
  const locked = ["reserved", "shipped"].includes(a.status);
  const dis = canEdit ? "" : "disabled";
  host.innerHTML = `
  <div class="card">
    <div class="inline-row">
      <h3 style="margin:0; flex:1;">${escapeHtml(a.assetNo)} ${statusChip(a.status)}${
        a.tmsLock ? ` <span class="chip chip-amber" title="${escapeHtml(a.tmsLockNote || "")}\nTMS 엑셀이 이 자산의 값·상태를 바꾸지 않습니다.">🔒 TMS 반영 잠김</span>` : ""}</h3>
      ${canEdit ? `<button class="btn btn-sm" id="ad-numfix"
        title="TMS에서 번호를 잘못 적어 겹친 경우 — 맞바꾸거나 넘겨받습니다">🔁 번호 바로잡기</button>` : ""}
      <button class="btn btn-sm" id="ad-close">닫기</button>
    </div>
    ${a.division === "rental" ? `<div class="muted"
      style="border:1px solid #c7d2fe;background:#eef2ff;color:#3730a3;border-radius:8px;
             padding:8px 10px;margin:0 0 10px;font-size:12.5px;">
      🏬 <b>렌탈 사업부(RMS) 자산이라 판매 재고에 잡히지 않습니다.</b>
      상태가 '${escapeHtml(statusLabel(a.status))}'여도 재고현황·몰 재고·주문 매칭에서 빠집니다.
      ${a.rmsStatus === "rented" ? "지금 고객이 쓰는 중입니다 — 반납 후에 넘길 수 있습니다."
        : "팔 물건이면 <b>[↔ 사업부 이관]</b>으로 판매 사업부에 넘기세요."}
    </div>` : ""}
    ${slipBarHtml(a.batch)}
    <div class="form-grid" style="margin-top:8px;">
      <label>관리번호 (TMS 이관 시 수정)<input type="text" id="ad-no" value="${escapeHtml(a.assetNo)}" ${dis}></label>
      <label>카테고리<select id="ad-cat" ${dis}>
        ${state.categories.map((c) => `<option value="${c.id}" ${c.id === a.categoryId ? "selected" : ""}>${escapeHtml(c.name)}</option>`).join("")}
      </select></label>
      <label>브랜드<input type="text" id="ad-maker" value="${escapeHtml(a.maker)}" ${dis}></label>
      <label style="grid-column:1 / -1;">모델명
        <input type="text" id="ad-model" value="${escapeHtml(a.model)}" ${dis}>
        <div id="ad-master" style="margin-top:4px;"></div>
        <div id="ad-brief"></div>
      </label>
      <label>시리얼번호<input type="text" id="ad-serial" value="${escapeHtml(a.serial)}" ${dis}></label>
      <label>등급<select id="ad-grade" ${dis}>
        ${state.meta.grades.map((g) => `<option ${g === a.grade ? "selected" : ""}>${escapeHtml(g)}</option>`).join("")}
      </select></label>
      <label style="grid-column:1 / -1;">상태
        <select id="ad-status" ${canEdit && !locked ? "" : "disabled"}>
          ${statusOptionsHtml(a.status)}
        </select>
        <div class="muted" id="ad-status-help" style="font-size:12px; margin-top:3px;"></div>
      </label>
      <label>보관위치<input type="text" id="ad-location" value="${escapeHtml(a.location)}" ${dis}></label>
      ${a.purchasePrice === null
        ? `<label>매입가<input type="text" id="ad-price" value="•••" disabled
             title="금액 열람 권한이 없습니다"></label>
           <label>판매가<input type="text" id="ad-saleprice" value="•••" disabled></label>`
        : `<label>매입가<input type="text" id="ad-price" data-money value="${fmtNum(a.purchasePrice)}" ${dis}></label>
      <label>판매가<input type="text" id="ad-saleprice" data-money value="${fmtNum(a.salePrice)}" ${dis}></label>
      <label>제조원가 <span class="muted">(매입가 + 수리·부품비)</span>
        <input type="text" value="${fmtNum(a.costTotal || 0)}" disabled
               title="TMS 재고상세의 제조원가와 같은 뜻 — 매입가 ${fmtNum(a.purchasePrice)} + 수리·부품 기록 합 ${fmtNum(a.repairTotal || 0)}. 아래 수리 기록에서 바뀝니다."></label>`}
      <label>제품코드 <span class="muted">(쇼핑몰 재고의 축 — 직접 기입)</span>
        <input type="text" id="ad-productcode" value="${escapeHtml(a.productCode || "")}"
               placeholder="예: 840 G3_i7-6_내장" ${dis}></label>
      ${stockSyncOn() ? `
      <label class="check-line" style="align-items:center;">
        <input type="checkbox" id="ad-stocklisted" ${a.stockListed ? "checked" : ""} ${dis}>
        <span>몰 재고 전송 <span class="muted">(체크해야 쇼핑몰 재고 수에 올라갑니다)</span></span>
      </label>` : ""}
    </div>
    <h3 style="margin-top:16px;">스펙</h3>
    <div class="form-grid">
      ${Object.entries(SPEC_LABELS).map(([k, label]) =>
        `<label>${label}<input type="text" id="ad-${k}" value="${escapeHtml(a[k] || "")}" ${dis}></label>`).join("")}
    </div>
    <label style="display:block; margin-top:10px;" class="muted">특이사항
      <textarea id="ad-notes" rows="2" style="width:100%; margin-top:4px; border:1px solid var(--border); border-radius:8px; background:var(--bg); padding:8px;" ${dis}>${escapeHtml(a.notes)}</textarea>
    </label>
    <label style="display:block; margin-top:8px;" class="muted">재고비고
      <span class="muted">(TMS 재고상세의 재고비고 — 짧은 상태 메모: 새배터리교체, 액정불량 … · 특이사항과 별개)</span>
      <input type="text" id="ad-stocknote" value="${escapeHtml(a.stockNote || "")}" maxlength="200"
             style="width:100%; margin-top:4px;" ${dis}></label>
    <div id="ad-tasks" style="margin-top:10px;"></div>
    <p class="muted" style="margin:8px 0;">
      원가 <b>${fmtWon(a.costTotal)}</b> (매입 ${fmtWon(a.purchasePrice)} + 수리 ${fmtWon(a.repairTotal)})
      ${locked ? ' · <span style="color:var(--danger)">주문에 매칭/출고된 자산은 상태를 직접 바꿀 수 없습니다</span>' : ""}
    </p>
    ${canEdit ? `<div class="editor-actions"><button class="btn btn-primary" id="ad-save">저장</button></div>` : ""}
  </div>
  <div class="card">
    ${assetSaleInfoHtml(a, rounds)}
    <h3>수리 내역 <span class="muted">(합계 ${fmtWon(a.repairTotal)} — 원가에 합산)</span></h3>
    ${canEdit ? `
    <div class="inline-row">
      <input type="date" id="rp-date" value="${ymd()}">
      <input type="text" id="rp-desc" placeholder="수리 내용" style="flex:1; min-width:180px;">
      <input type="text" id="rp-cost" data-money placeholder="비용" style="width:100px;">
      <button class="btn btn-sm btn-primary" id="rp-add">추가</button>
    </div>
    <div class="inline-row" style="margin-top:4px;">
      <select id="rp-part" style="min-width:200px;"><option value="">🔩 단가표에서 부품 고르기…</option></select>
      <select id="rp-part-mode" title="회수(빼냄)는 단가표 단가만큼 원가에서 빠져서 순수익에 더해집니다 — 다운그레이드 출고용">
        <option value="add">장착 (원가에 더함)</option>
        <option value="remove">회수 (원가에서 뺌)</option>
      </select>
      <button class="btn btn-sm" id="rp-part-add"
        title="장착: 오늘 단가로 원가에 넣고 RAM/SSD 스펙 칸도 채웁니다.&#10;회수: 단가만큼 원가에서 빼고 스펙 칸에서 그 부품을 지웁니다(다운그레이드 차액=순수익 가산).">부품 반영</button>
      <span class="muted" style="font-size:12px;">단가는 기준정보 ▸ 부품 단가표에서 관리</span>
    </div>` : ""}
    <div class="table-wrap"><table>
      <thead><tr><th>일자</th><th>내용</th><th>비용</th><th>등록자</th><th></th></tr></thead>
      <tbody>${a.repairs.map((r) => `
        <tr><td>${escapeHtml(r.repairDate)}</td><td>${escapeHtml(r.description)}</td>
        <td>${fmtWon(r.cost)}</td><td>${escapeHtml(r.createdBy)}</td>
        <td>${r.createdBy === "TMS연동"
              ? `<span class="chip chip-slate" title="TMS 재고상세의 수리비·부품비 — 연동이 따라가므로 여기서 지워도 다음 반영에 되살아납니다. TMS 값이 0이 되면 사라집니다.">TMS</span>`
              : canEdit ? `<button class="btn btn-ghost btn-sm" data-delrepair="${r.id}">삭제</button>` : ""}</td></tr>`).join("")
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
  ${(a.asTickets || []).length ? `<div class="card"><h3>A/S 이력
      <span class="muted" style="font-size:12px; font-weight:400;">— 자세한 내용은 A/S 메뉴에서</span></h3>
    <div class="table-wrap"><table>
      <thead><tr><th>접수번호</th><th>구분</th><th>상태</th><th>접수일</th></tr></thead>
      <tbody>${a.asTickets.map((t) => `
        <tr><td><b>${escapeHtml(t.ticketNo)}</b></td>
        <td>${escapeHtml({ repair: "수리", exchange: "교환", refund: "환불", inspect: "점검" }[t.asType] || t.asType)}</td>
        <td>${escapeHtml(t.status)}</td>
        <td class="muted">${escapeHtml((t.receivedAt || "").slice(0, 10))}</td></tr>`).join("")}
      </tbody></table></div></div>` : ""}
  <div class="card">
    <h3>이력 타임라인</h3>
    <div class="timeline">${a.events.map((e) => `
      <div class="tl-item">
        <div class="tl-time">${escapeHtml(e.ts.replace("T", " ").slice(0, 19))}</div>
        <div class="tl-action"><span class="chip chip-slate">${escapeHtml(e.action)}</span> <b>${escapeHtml(e.actor)}</b></div>
        <div class="tl-detail muted">${escapeHtml(fmtEventDetail(e.detail))}</div>
      </div>`).join("") || `<p class="muted">이력이 없습니다.</p>`}
    </div>
  </div>`;

  $("#ad-close").addEventListener("click", () => { state.assetDetailId = undefined; host.innerHTML = ""; });
  // 모델 마스터 대조(2026-09-02) — 기존 자산이라 카테고리는 건드리지 않는다(null)
  attachModelMaster("#ad-model", "#ad-maker", null, "#ad-master");
  // 쓰던 코드를 그대로 다시 고르게 + 그 코드가 몰에 실제로 있는지 함께 확인
  attachCodeLookup("ad-productcode", null, { mall: true });
  // 전표번호를 누르면 그 전표의 전체를 본다 — [매입 작업] 탭의 전표 상세와 같은 화면이다.
  // ★새 화면을 따로 만들지 않는다(대표 지시: 같은 걸 두 벌로 만들지 마라).
  const slipBtn = $(".slip-open", host);
  if (slipBtn) slipBtn.addEventListener("click", () => openSlipFromAsset(Number(slipBtn.dataset.slip)));

  /* ---- 보수 체크 (대표 2026-08-08) — 실재고·가재고 자산에 무엇을 보수해야
     하는지, 하는 중인지(해야함/작업중/완료)를 항목별로 남긴다. 저장 버튼과 함께 저장. */
  const taskStates = ["todo", "doing", "done"];
  const taskLabel = { todo: "해야함", doing: "작업중", done: "완료" };
  const taskChip = { todo: "chip-red", doing: "chip-blue", done: "chip-green" };
  let tasks = ((a.tierTasks && a.tierTasks.items) || []).map((t) => ({ name: t.name, state: t.state }));
  let taskNote = (a.tierTasks && a.tierTasks.note) || "";
  const keepNote = () => { const n = $("#ad-task-note"); if (n) taskNote = n.value; };
  const drawTasks = () => {
    const box = $("#ad-tasks");
    if (!box) return;
    // ★상태가 '수리'일 때만 뜬다(대표 2026-08-24: "수리 누르면 하단에 시트지, 도색,
    //   짜깁기 이런식으로 뜨고"). 남은 기록이 있으면 상태가 바뀌어도 보여 준다.
    const st = $("#ad-status") ? $("#ad-status").value : "";
    if (!(st === "repair" || tasks.length || taskNote)) {
      box.innerHTML = "";
      return;
    }
    // ★수리 단가표(기준정보 ▸ 🛠 수리 단가표)가 후보와 금액의 출처다 — 화면이 짐작하지 않는다
    const book = (state.meta || {}).repairItems || [];
    const priceOf = (n) => {
      const hit = book.find((x) => (x.name || x) === n);
      return hit && hit.price ? hit.price : 0;
    };
    const kindOf = (n) => {
      const hit = book.find((x) => (x.name || x) === n);
      return (hit && hit.kind) || "repair";
    };
    const presets = book.map((x) => x.name || x);
    const names = [...new Set([...presets, ...tasks.map((t) => t.name)])];
    // 묶음별로 가른다(2026-08-25 대표: 수리 / 도색·시트지) — 자유 입력분은 수리 쪽에 둔다
    const grouped = { repair: names.filter((n) => kindOf(n) === "repair"),
                      paint: names.filter((n) => kindOf(n) === "paint") };
    // 지금 잡혀 있는(해야함·작업중·완료) 항목의 단가 합 — 무엇 때문에 돈이 드는지 바로 보이게
    const pickedSum = tasks.reduce((n, t) => n + priceOf(t.name), 0);
    box.innerHTML = `
      <div style="border:1px solid var(--border); border-radius:10px; padding:10px 12px; background:var(--bg);">
        <div class="inline-row" style="margin:0 0 6px;">
          <b>수리 항목</b>
          <span class="muted" style="font-size:12px;">항목을 누르면 해야함 → 작업중 → 완료 → 해제 순으로 바뀝니다
            · 수리 중에는 재고에 잡히지 않고 출고도 막힙니다</span>
        </div>
        <div style="max-height:150px; overflow-y:auto;">
        ${["repair", "paint"].map((kk) => grouped[kk].length ? `
        <div class="muted" style="font-size:11.5px; font-weight:600; margin:4px 0 2px;">
          ${kk === "repair" ? "🛠 수리" : "🎨 도색/시트지"}</div>
        <div class="inline-row" style="flex-wrap:wrap; gap:6px; margin:0; align-items:flex-start;">
          ${grouped[kk].map((n) => {
            const t = tasks.find((x) => x.name === n);
            const cls = t ? taskChip[t.state] : "chip-slate";
            const won = priceOf(n);
            const label = (t ? `${escapeHtml(n)} · ${taskLabel[t.state]}` : escapeHtml(n))
              + (won ? ` <span style="opacity:.75;">${fmtNum(won)}원</span>` : "");
            return `<button type="button" class="chip ${cls}" data-task="${escapeHtml(n)}"
              style="cursor:pointer; border:none;" ${dis}>${label}</button>`;
          }).join("")}
        </div>` : "").join("")}
        </div>
        ${canEdit ? `<div class="inline-row" style="margin:8px 0 0;">
          <input type="text" id="ad-task-new" placeholder="기타 항목 추가 (예: 힌지 교체)"
            style="flex:1; min-width:160px; padding:6px 8px; border:1px solid var(--border); border-radius:8px; background:var(--surface);">
          <button type="button" class="btn btn-sm" id="ad-task-add">＋ 추가</button>
        </div>` : ""}
        <div class="muted" style="font-size:12px; margin-top:8px;">
          ${tasks.length
            ? (pickedSum
                ? `잡힌 항목 ${tasks.length}건 · 수리 단가표 기준 <b>${fmtNum(pickedSum)}원</b>
                   <span style="opacity:.8;">— 아래 [수리 내역]에서 원가에 반영하세요</span>`
                : `잡힌 항목 ${tasks.length}건 — 수리 단가표에 금액이 없습니다
                   <span style="opacity:.8;">(기준정보 ▸ 🛠 수리 단가표에서 단가를 채우세요)</span>`)
            : "무엇을 손봐야 하는지 위에서 고르세요 — 금액은 수리 단가표를 따릅니다."}</div>
        <input type="text" id="ad-task-note" placeholder="무엇 때문에 비용이 드는지 적어 두세요 (자유 입력)" value="${escapeHtml(taskNote)}" ${dis}
          style="width:100%; margin-top:8px; padding:6px 8px; border:1px solid var(--border); border-radius:8px; background:var(--surface);">
      </div>`;
    $$("button[data-task]", box).forEach((b) => b.addEventListener("click", () => {
      if (!canEdit) return;
      const name = b.dataset.task;
      const i = tasks.findIndex((x) => x.name === name);
      if (i < 0) tasks.push({ name, state: "todo" });
      else {
        const next = taskStates.indexOf(tasks[i].state) + 1;
        if (next >= taskStates.length) tasks.splice(i, 1);   // 완료 다음 클릭 = 해제
        else tasks[i].state = taskStates[next];
      }
      keepNote();
      drawTasks();
    }));
    const add = $("#ad-task-add");
    if (add) add.addEventListener("click", () => {
      const v = $("#ad-task-new").value.trim().slice(0, 30);
      if (!v) return;
      if (!tasks.some((x) => x.name === v)) tasks.push({ name: v, state: "todo" });
      keepNote();
      drawTasks();
    });
    const newInp = $("#ad-task-new");
    if (newInp) newInp.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); if (add) add.click(); }
    });
  };
  drawTasks();
  // 상태를 바꾸면 '수리 항목'이 뜨고 사라진다 + 그 상태의 뜻을 바로 아래에 적어 준다
  const statusHelp = () => {
    const box = $("#ad-status-help");
    if (!box) return;
    const cur = $("#ad-status").value;
    const c = ((state.meta || {}).statusChoices || []).find((x) => x.code === cur);
    box.textContent = c ? c.help : "";
  };
  $("#ad-status").addEventListener("change", () => { keepNote(); drawTasks(); statusHelp(); });
  statusHelp();

  if (!canEdit) return;
  attachSpecAutocomplete("ad-");   // 자산 상세 수정에도 자동완성
  // 브랜드·모델·스펙 자동완성 — 이미 쓴 값과 카탈로그에서 후보를 띄운다(대표 2026-08-04)
  attachSpecAutocomplete("ad-");
  $("#ad-numfix")?.addEventListener("click", () =>
    openNumberFix(a.id, a.assetNo, () => { reopen(); if (onSaved) onSaved(); }));
  $("#ad-save").addEventListener("click", async () => {
    const body = {
      assetNo: $("#ad-no").value.trim(), categoryId: Number($("#ad-cat").value),
      maker: $("#ad-maker").value, model: $("#ad-model").value, serial: $("#ad-serial").value,
      // ★tier(재고구분)는 화면에서 없앴다(대표 2026-08-24 — 상태와 같은 말이었다).
      //   안 보내면 서버가 기존 값을 그대로 둔다. 재고·출고 동작은 상태가 정한다.
      grade: $("#ad-grade").value, location: $("#ad-location").value,
      // ★빈 칸은 0으로 — 칸을 통째로 지우면 ''가 되는데, 그대로 보내면 서버가 400을 내고
      //   같은 저장에 실린 등급·스펙·제품코드 수정까지 전부 되돌아간다(2026-08-14 검토).
      ...(hasPerm("purchase.money")
        ? { purchasePrice: $("#ad-price").value.replaceAll(",", "") || 0 } : {}),
      ...(hasPerm("purchase.money")
        ? { salePrice: $("#ad-saleprice").value.replaceAll(",", "") || 0 } : {}),
      notes: $("#ad-notes").value,
      stockNote: $("#ad-stocknote").value,
      productCode: $("#ad-productcode").value.trim(),
      // 칸이 숨겨져 있으면(몰 재고 연동 꺼짐) 아예 안 보낸다 — 서버가 지금 값을 그대로 둔다
      ...($("#ad-stocklisted") ? { stockListed: $("#ad-stocklisted").checked } : {}),
      // 수리 항목(옛 이름 tierTasks) — 상태가 '수리'일 때 쓰는 하위 목록이다
      tierTasks: { items: tasks, note: ($("#ad-task-note") ? $("#ad-task-note").value : taskNote) },
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
  // 🔩 단가표 부품 추가 — 금액은 서버가 단가표에서 읽는다(오늘 단가 자동)
  const partSel = $("#rp-part");
  if (partSel) {
    api("/api/parts").then((parts) => fillPartSelect(partSel, parts, false)).catch(() => {});
    $("#rp-part-add").addEventListener("click", async () => {
      const pid = Number(partSel.value);
      if (!pid) { toast("부품을 고르세요.", true); return; }
      const remove = $("#rp-part-mode").value === "remove";
      try {
        const r = await api(`/api/assets/${a.id}/repairs`, {
          method: "POST",
          body: { partIds: [{ id: pid, remove }], repairDate: $("#rp-date").value } });
        toast(remove
          ? `부품 회수 — ${fmtWon(-r.cost)}이 원가에서 빠졌습니다(순수익에 더해짐).`
          : `부품 추가 — ${fmtWon(r.cost)}이 원가에 더해졌습니다.`);
        if (onSaved) onSaved();
        reopen();
      } catch (err) { toast(err.message, true); }
    });
  }
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

/* 매입 작업 탭의 보기 전환 알약 — 전표 목록 ↔ 📊 매입 대시보드(2026-08-13 대표,
   매입대시보드.hwpx). 새 탭을 만들지 않고 같은 탭 안에서 보기만 바꾼다. */
function slipViewPills(active) {
  // ★자산·기준정보와 같은 알약형 서브탭으로(대표 2026-08-25: 세부 탭 디자인 통일).
  //   예전엔 초록/외곽선 버튼이라 다른 탭과 생김새가 달랐다.
  return `<div class="inline-row" style="margin:0 0 8px; gap:10px;">
    <div class="subtabs" style="max-width:max-content; margin:0;">
      <button class="${active === "list" ? "active" : ""}" data-slipview="list">📋 전표 목록</button>
      ${hasPerm("purchase.money")
        ? `<button class="${active === "dash" ? "active" : ""}" data-slipview="dash">📊 매입 대시보드</button>` : ""}
    </div>
    <span style="flex:1;"></span>
  </div>`;
}

function wireSlipViewPills(body) {
  $$("button[data-slipview]", body).forEach((b) => b.addEventListener("click", () => {
    if (state.slipView === b.dataset.slipview) return;
    state.slipView = b.dataset.slipview;
    renderSlips(body);
  }));
}

/* 📊 매입 대시보드 — 토스쇼핑 달력 참고(대표 문서). 매입일 기준 일별 코호트:
   한 달 범위면 '달력'(칸: 수량/매입합계/판매액/순수익), 그보다 길면 '월별 표'.
   판매액·순수익은 관리자 전용이라 서버가 권한 없으면 아예 안 준다(showProfit). */
async function renderPurchaseDash(body) {
  const seq = ++state.renderSeq;
  const first = (dt) => new Date(dt.getFullYear(), dt.getMonth(), 1);
  const lastDay = (y, m) => new Date(y, m + 1, 0).getDate();
  const f = state.pdash || (state.pdash = {
    from: ymd(first(new Date())), to: ymd(new Date()),
    supplierId: "", pmin: "", pmax: "" });
  body.innerHTML = `${slipViewPills("dash")}<p class="muted">불러오는 중…</p>`;
  wireSlipViewPills(body);
  let r, suppliers;
  try {
    const p = new URLSearchParams({ from: f.from, to: f.to });
    if (f.supplierId) p.set("supplierId", f.supplierId);
    if (f.pmin) p.set("priceMin", f.pmin);
    if (f.pmax) p.set("priceMax", f.pmax);
    [r, suppliers] = await Promise.all([
      api("/api/reports/purchase-dashboard?" + p.toString()),
      state.suppliers ? Promise.resolve(state.suppliers) : api("/api/suppliers")]);
    if (seq !== state.renderSeq) return;
    state.suppliers = suppliers;
  } catch (err) {
    if (seq === state.renderSeq) {
      body.innerHTML = `${slipViewPills("dash")}<p class="muted">${escapeHtml(err.message)}</p>`;
      wireSlipViewPills(body);
    }
    return;
  }
  const t = r.totals;
  const byDate = new Map(r.days.map((d) => [d.date, d]));
  const won = (n) => (n || 0).toLocaleString("ko-KR");
  const sameMonth = f.from.slice(0, 7) === f.to.slice(0, 7);

  // ── 달력(한 달 범위) 또는 월별 표(그 이상) ──────────────────────────
  let bodyHtml = "";
  if (sameMonth) {
    const [yy, mm] = f.from.split("-").map(Number);
    const startDow = new Date(yy, mm - 1, 1).getDay();
    const days = lastDay(yy, mm - 1);
    // ★칸 높이를 고정한다(2026-08-14 대표 "어디는 크고 어디는 작고") — 매입이 많은 날도
    //   칸이 안 늘어나고, 규모는 '색 강도'로 읽는다. 상세는 툴팁·클릭으로 본다.
    const maxBuy = Math.max(0, ...r.days.map((x) => x.buy || 0));
    const LEVELS = ["#e8f5e9", "#c8e6c9", "#a5d6a7", "#66bb6a", "#2e7d32"];
    const level = (v) => (!v || !maxBuy) ? -1
      : Math.min(LEVELS.length - 1, Math.floor((v / maxBuy) * LEVELS.length - 0.0001));
    const tip = (row) => {
      const head = `${row.date} · 매입 ${row.qty}대 · ${won(row.buy)}원`;
      const lines = (row.slips || []).map((sp) =>
        `· ${sp.supplier || "거래처 미지정"} ${sp.slipNo ? `(${sp.slipNo})` : ""} — ${sp.qty}대 ${won(sp.buy)}원`);
      return [head, ...lines, "", "누르면 이 날의 매입 목록이 아래에 열립니다"].join("\n");
    };
    let cells = [];
    for (let i = 0; i < startDow; i++) cells.push(`<td style="border:1px solid var(--border);"></td>`);
    for (let d = 1; d <= days; d++) {
      const key = `${yy}-${String(mm).padStart(2, "0")}-${String(d).padStart(2, "0")}`;
      const row = byDate.get(key);
      const dow = (startDow + d - 1) % 7;
      const lv = row ? level(row.buy) : -1;
      const on = state.pdDay === key;
      cells.push(`<td style="vertical-align:top; padding:0; border:1px solid var(--border);
          ${on ? "outline:2px solid var(--primary); outline-offset:-2px;" : ""}">
          <div ${row ? `class="pd-cell" data-pdday="${key}" title="${escapeHtml(tip(row))}"` : ""}
            style="height:92px; padding:6px; box-sizing:border-box; overflow:hidden;
                   ${row ? "cursor:pointer;" : ""}
                   ${lv >= 0 ? `background:${LEVELS[lv]};` : ""}">
            <div style="font-size:12px; ${dow === 0 ? "color:var(--danger);" : dow === 6 ? "color:var(--blue);" : ""}">${d}</div>
            ${row ? `<div style="font-size:13px;"><b>${row.qty}대</b>
                <span class="muted" style="font-size:11px;">${(row.slips || []).length}건</span></div>
              <div style="font-size:12.5px;">${won(row.buy)}</div>
              ${r.showProfit && row.soldQty ? `
                <div style="font-size:11.5px; color:${row.profit >= 0 ? "var(--primary)" : "var(--danger)"};">
                  순익 ${won(row.profit)}</div>` : ""}` : ""}
          </div>
        </td>`);
    }
    const weeks = [];
    for (let i = 0; i < cells.length; i += 7) {
      const wk = cells.slice(i, i + 7);
      while (wk.length < 7) wk.push(`<td style="border:1px solid var(--border);"></td>`);
      weeks.push(`<tr>${wk.join("")}</tr>`);
    }
    bodyHtml = `
      <div class="card">
        <div class="inline-row" style="margin-bottom:6px;">
          <button class="btn btn-sm" id="pd-prev">‹</button>
          <b style="font-size:15px;">${yy}년 ${mm}월</b>
          <button class="btn btn-sm" id="pd-next">›</button>
          <span class="muted" style="font-size:12px;">칸: 매입수량 / 매입합계${r.showProfit ? " / 순수익" : ""}
            — 색이 진할수록 그날 매입금액이 큽니다. 칸에 마우스를 올리면 매입처별 내역,
            누르면 아래에 그날 매입 목록이 열립니다.</span>
          <span style="flex:1"></span>
          <span class="muted" style="font-size:11.5px;">적음</span>
          ${LEVELS.map((c) => `<span style="display:inline-block; width:14px; height:14px;
            background:${c}; border:1px solid var(--border);"></span>`).join("")}
          <span class="muted" style="font-size:11.5px;">많음</span>
        </div>
        <div class="table-wrap"><table style="table-layout:fixed;">
          <thead><tr>${["일", "월", "화", "수", "목", "금", "토"].map((n, i) =>
            `<th style="${i === 0 ? "color:var(--danger);" : i === 6 ? "color:var(--blue);" : ""}">${n}</th>`).join("")}</tr></thead>
          <tbody>${weeks.join("")}</tbody></table></div>
      </div>
      <div id="pd-daylist"></div>
      <div id="slip-detail"></div>`;
  } else {
    const byMonth = new Map();
    r.days.forEach((d) => {
      const m = d.date.slice(0, 7);
      const acc = byMonth.get(m) || { qty: 0, buy: 0, soldQty: 0, sale: 0, profit: 0 };
      acc.qty += d.qty; acc.buy += d.buy;
      acc.soldQty += d.soldQty || 0; acc.sale += d.sale || 0; acc.profit += d.profit || 0;
      byMonth.set(m, acc);
    });
    bodyHtml = `
      <div class="card">
        <div class="table-wrap"><table>
          <thead><tr><th>월</th><th style="text-align:right;">매입수량</th><th style="text-align:right;">매입합계</th>
            ${r.showProfit ? `<th style="text-align:right;">판매수량</th><th style="text-align:right;">판매액</th>
            <th style="text-align:right;">순수익</th>` : ""}</tr></thead>
          <tbody>${[...byMonth.entries()].map(([m, a]) => `
            <tr><td><b>${m}</b></td><td style="text-align:right;">${a.qty}대</td>
              <td style="text-align:right;">${fmtWon(a.buy)}</td>
              ${r.showProfit ? `<td style="text-align:right;">${a.soldQty}대</td>
              <td style="text-align:right;">${fmtWon(a.sale)}</td>
              <td style="text-align:right; color:${a.profit >= 0 ? "inherit" : "var(--danger)"};">${fmtWon(a.profit)}</td>` : ""}</tr>`).join("")
            || `<tr><td colspan="6" class="muted">이 기간에 매입이 없습니다.</td></tr>`}
          </tbody></table></div>
      </div>`;
  }

  body.innerHTML = `
    ${slipViewPills("dash")}
    <div class="card">
      <div class="inline-row" style="flex-wrap:wrap;">
        ${[["this", "이번달"], ["prev", "지난달"], ["3m", "3개월"], ["6m", "6개월"], ["1y", "1년"]].map(([k, l]) =>
          `<button class="btn btn-sm" data-pdpre="${k}">${l}</button>`).join("")}
        <input type="date" id="pd-from" value="${f.from}"> ~ <input type="date" id="pd-to" value="${f.to}">
        <select id="pd-sup"><option value="">전체 거래처</option>
          ${(suppliers || []).map((s) => `<option value="${s.id}" ${String(f.supplierId) === String(s.id) ? "selected" : ""}>${escapeHtml(s.name)}</option>`).join("")}</select>
        <input type="text" id="pd-min" data-money placeholder="매입가 최소" value="${f.pmin ? fmtNum(f.pmin) : ""}" style="width:100px;">
        ~ <input type="text" id="pd-max" data-money placeholder="최대" value="${f.pmax ? fmtNum(f.pmax) : ""}" style="width:100px;">
        <button class="btn btn-sm btn-primary" id="pd-go">조회</button>
      </div>
      <div class="kpi-row" style="margin-top:8px;">
        <div class="kpi"><div class="kpi-label">매입수량</div>
          <div class="kpi-value">${t.qty}대</div>
          <div class="kpi-sub">${f.from} ~ ${f.to}</div></div>
        <div class="kpi"><div class="kpi-label">매입합계</div>
          <div class="kpi-value">${fmtWon(t.buy)}</div>
          <div class="kpi-sub">자산 매입가 기준</div></div>
        ${r.showProfit ? `
        <div class="kpi"><div class="kpi-label">판매액 (이 매입분의 실현)</div>
          <div class="kpi-value">${fmtWon(t.sale)}</div>
          <div class="kpi-sub">판매 ${t.soldQty}대 · 수수료·환불 차감 후</div></div>
        <div class="kpi"><div class="kpi-label">순수익</div>
          <div class="kpi-value" style="color:${t.profit >= 0 ? "var(--primary)" : "var(--danger)"};">${fmtWon(t.profit)}</div>
          <div class="kpi-sub">판매액 − 매입가 − 부품·수리 − 택배</div></div>` : ""}
      </div>
    </div>
    ${bodyHtml}`;

  wireSlipViewPills(body);
  const rerun = () => { resetPage("pdash"); renderPurchaseDash(body); };
  $("#pd-go", body).addEventListener("click", () => {
    f.from = $("#pd-from").value || f.from;
    f.to = $("#pd-to").value || f.to;
    f.supplierId = $("#pd-sup").value;
    // ★콤마를 떼고 담는다 — state에는 숫자 문자열만(다른 금액 칸과 같은 규칙)
    f.pmin = $("#pd-min").value.replace(/[^0-9]/g, "");
    f.pmax = $("#pd-max").value.replace(/[^0-9]/g, "");
    rerun();
  });
  $$("button[data-pdpre]", body).forEach((b) => b.addEventListener("click", () => {
    const now = new Date();
    const k = b.dataset.pdpre;
    if (k === "this") { f.from = ymd(first(now)); f.to = ymd(now); }
    else if (k === "prev") {
      const pm = new Date(now.getFullYear(), now.getMonth() - 1, 1);
      f.from = ymd(pm);
      f.to = ymd(new Date(pm.getFullYear(), pm.getMonth(), lastDay(pm.getFullYear(), pm.getMonth())));
    } else {
      const months = { "3m": 3, "6m": 6, "1y": 12 }[k];
      f.from = ymd(new Date(now.getFullYear(), now.getMonth() - months, now.getDate()));
      f.to = ymd(now);
    }
    rerun();
  }));
  const nav = (delta) => {
    const [yy, mm] = f.from.split("-").map(Number);
    const m0 = new Date(yy, mm - 1 + delta, 1);
    f.from = ymd(m0);
    f.to = ymd(new Date(m0.getFullYear(), m0.getMonth(), lastDay(m0.getFullYear(), m0.getMonth())));
    rerun();
  };
  $("#pd-prev", body)?.addEventListener("click", () => nav(-1));
  $("#pd-next", body)?.addEventListener("click", () => nav(1));

  // ── 날짜 칸 클릭 → 그날 매입 목록 → [열기]가 전표 상세를 띄운다.
  //    ★목록의 열·버튼과 상세 화면은 [전표 목록]과 똑같은 것을 쓴다(대표: 일체감).
  const paintDayList = () => {
    const host = $("#pd-daylist", body);
    if (!host) return;
    const key = state.pdDay;
    const row = key ? byDate.get(key) : null;
    if (!row) { host.innerHTML = ""; return; }
    host.innerHTML = `
      <div class="card">
        <div class="inline-row">
          <h3 style="margin:0; flex:1;">${escapeHtml(key)} 매입
            <span class="muted" style="font-weight:400;">${row.qty}대 · ${fmtWon(row.buy)}
              · 전표 ${(row.slips || []).length}건</span></h3>
          <button class="btn btn-sm" id="pd-day-close">닫기</button>
        </div>
        <div class="table-wrap"><table>
          <thead><tr><th>전표</th><th>거래처</th><th style="text-align:right;">수량</th>
            <th style="text-align:right;">매입금액</th>
            ${r.showProfit ? `<th style="text-align:right;">판매액</th>
            <th style="text-align:right;">순수익</th>` : ""}<th></th></tr></thead>
          <tbody>${(row.slips || []).map((sp) => `
            <tr><td><b>${escapeHtml(sp.slipNo || "(전표 없음)")}</b></td>
              <td>${escapeHtml(sp.supplier || "-")}</td>
              <td style="text-align:right;">${sp.qty}대</td>
              <td style="text-align:right;">${fmtWon(sp.buy)}</td>
              ${r.showProfit ? `<td style="text-align:right;">${sp.sale ? fmtWon(sp.sale) : "-"}</td>
              <td style="text-align:right; color:${(sp.profit || 0) >= 0 ? "inherit" : "var(--danger)"};">${sp.profit ? fmtWon(sp.profit) : "-"}</td>` : ""}
              <td>${sp.batchId
                ? `<button class="btn btn-sm" data-slip="${sp.batchId}">열기</button>`
                : `<span class="muted" style="font-size:12px;">전표 없이 등록된 자산</span>`}</td>
            </tr>`).join("")}
          </tbody></table></div>
      </div>`;
    $("#pd-day-close", host).addEventListener("click", () => {
      state.pdDay = undefined; state.slipDetailId = undefined;
      $("#slip-detail", body).innerHTML = "";
      renderPurchaseDash(body);
    });
    // 전표 목록과 같은 동작 — 상세는 renderSlipDetail이 그린다(자산 5:5 편집까지 동일)
    $$("button[data-slip]", host).forEach((b) => b.addEventListener("click", () => {
      state.slipDetailId = Number(b.dataset.slip);
      state.assetDetailId = undefined;
      renderSlipDetail();
    }));
  };
  $$(".pd-cell", body).forEach((c) => c.addEventListener("click", () => {
    const key = c.dataset.pdday;
    state.pdDay = state.pdDay === key ? undefined : key;
    state.slipDetailId = undefined;
    renderPurchaseDash(body);
  }));
  paintDayList();
}

async function renderSlips(body) {
  if (state.slipView === "dash") return renderPurchaseDash(body);
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
    ${slipViewPills("list")}
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
            <td>${escapeHtml(s.supplierName || "-")}${supplierOrigHint(s)}</td>
            <td>${s.assetCount}대${s.rentalCount ? `
              <span class="chip" title="이 전표의 자산 중 ${s.rentalCount}대가 렌탈 사업부(RMS)로 갔습니다.
매입 기록은 그대로 남습니다 — 사업부는 그 위에 얹히는 소유 표시일 뿐입니다."
                style="background:#eef2ff;color:#3730a3;border:1px solid #c7d2fe;font-size:11px;">렌탈 ${s.rentalCount}</span>` : ""}</td>
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
  wireSlipViewPills(body);
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
      <label>거래처
        <input type="text" id="sl-supplier" autocomplete="off"
               placeholder="거래처 검색"
               value="${escapeHtml(((state.suppliers || []).find((s) => s.id === v.supplierId) || {}).name || "")}"
               title="CPU·램처럼 치면 후보가 뜹니다. 목록에 없는 이름은 저장할 때 거래처로 새로 등록됩니다.">
      </label>
      <label>일자<input type="date" id="sl-date" value="${escapeHtml(v.purchaseDate || today)}"></label>
      <label>매입구분<select id="sl-type">
        ${state.meta.purchaseTypes.map((t) => `<option ${t === v.purchaseType ? "selected" : ""}>${escapeHtml(t)}</option>`).join("")}
      </select></label>
      <label>매입방법<input type="text" id="sl-channel" value="${escapeHtml(v.channel || "")}" placeholder="예: 직거래, 중고나라, 경매"></label>
      <label>매입금액 <span class="muted">(적으면 대당 균등분할 · 비우면 합계 자동)</span>
        ${!hasPerm("purchase.money") ? `<input type="text" id="sl-amount" value="•••" disabled
          title="금액 열람 권한이 없습니다 — 관리자에게 문의하세요">` :
        `<input type="text" id="sl-amount" data-money value="${fmtNum(v.totalAmount || 0)}"
          title="총액을 적으면 담긴 자산 수량만큼 대당가로 균등분할됩니다(나머지 원 단위는 첫 줄에).&#10;비워 두면 예전처럼 줄별 매입가의 합이 자동으로 채워집니다.">`}</label>
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
    // ★거래처는 이름으로 보낸다(자동완성 입력, 2026-08-24). 이름→id 는 서버가 푼다 —
    //   없는 이름이면 서버가 새 거래처로 만든다(TMS 이관과 같은 규칙, 한 곳에서만).
    supplierName: pick("#sl-supplier").value.trim(),
    purchaseDate: pick("#sl-date").value,
    purchaseType: pick("#sl-type").value,
    channel: pick("#sl-channel").value,

    // ★금액 권한 없으면 금액을 아예 안 보낸다 — •••를 저장해 원본을 깨면 안 된다
    ...(hasPerm("purchase.money")
      ? { totalAmount: pick("#sl-amount").value.replaceAll(",", "") || 0 } : {}),
    address: pick("#sl-address").value,
    paid: pick("#sl-paid").checked,
    memo: pick("#sl-memo").value,
  };
}

/* ---------------- 부가세 체크 (대표 2026-08-08) ----------------

   "부가세 포함이면 그대로 두고, 부가세 별도다 그러면 부가세 포함금액을
    자동으로 합계로 만들어주면 되잖아?"
   → 매입가 입력칸 옆에 포함/별도 선택을 두고, '별도'면 저장 직전에 ×1.1
     (원 단위 반올림)해서 포함가로 저장한다. 전표 매입금액은 자산 매입가
     합계로 자동 계산되므로, 여기서 포함가로 바꿔 두면 합계도 자연히 맞는다. */

function vatSelectHtml(prefix) {
  return `<label>부가세 <span class="muted">(매입가 기준)</span>
    <select id="${prefix}-vat"
      title="견적이 부가세 별도면 '별도'를 고르세요 — 입력한 금액에 10%를 더한 포함가로 저장됩니다.">
      <option value="incl" selected>포함 — 그대로 저장</option>
      <option value="excl">별도 — +10% 해서 저장</option>
    </select>
    <div id="${prefix}-vat-hint" class="muted" style="font-size:11px; margin-top:2px;"></div></label>`;
}

function priceWithVat(prefix, priceSel) {
  const raw = Number(($(priceSel).value || "").replaceAll(",", "")) || 0;
  const sel = $(`#${prefix}-vat`);
  return sel && sel.value === "excl" ? Math.round(raw * 1.1) : raw;
}

function attachVatHint(prefix, priceSel) {
  const price = $(priceSel);
  const sel = $(`#${prefix}-vat`);
  const hint = $(`#${prefix}-vat-hint`);
  if (!price || !sel || !hint) return;
  const draw = () => {
    const raw = Number((price.value || "").replaceAll(",", "")) || 0;
    hint.textContent = (sel.value === "excl" && raw > 0)
      ? `공급가 ${fmtWon(raw)} + 부가세 ${fmtWon(Math.round(raw * 0.1))} = ${fmtWon(Math.round(raw * 1.1))}로 저장`
      : "";
  };
  price.addEventListener("input", draw);
  sel.addEventListener("change", draw);
  draw();
}

/* 등록 팝업 상단의 [제품/부품] 전환(대표 2026-08-26 "굳이 별도로 둘 필요 없다").
   가입고(V)는 부품 개념이 없어 매입(P)에서만 보인다. */
function regModeBar(active) {
  return `<div class="subtabs" style="max-width:max-content; margin:0 0 12px;">
      <button type="button" class="${active === "asset" ? "active" : ""}" data-regmode="asset">💻 제품 등록</button>
      <button type="button" class="${active === "part" ? "active" : ""}" data-regmode="part">🔩 부품 등록</button>
    </div>`;
}

function wireRegMode(host, stage) {
  $$("button[data-regmode]", host).forEach((b) => b.addEventListener("click", () => {
    if (b.classList.contains("active")) return;
    if (b.dataset.regmode === "part") renderPartForm(host);
    else renderSlipForm(stage || "purchased");
  }));
}

function renderSlipForm(stage) {
  const host = $("#slip-form");
  host.innerHTML = `
    <div class="slip-form">
      ${stage === "purchased" ? regModeBar("asset") : ""}
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
            <div id="sa-master" style="margin-top:4px;"></div>
            <div id="sa-brief"></div>
          </label>
          <label>수량<input type="number" id="sa-qty" value="1" min="1" max="100"></label>
          <label>매입가(대당) <span class="muted">(한 대 값)</span>
            <input type="text" id="sa-price" data-money value="0"
              title="한 대당 값입니다. 10대 × 10만원이면 여기에 100,000을 넣으세요."></label>
          ${vatSelectHtml("sa")}
          <label>등급<select id="sa-grade">${(state.meta.grades || [])
            .map((g) => `<option ${g === "미정" ? "selected" : ""}>${escapeHtml(g)}</option>`).join("")}</select></label>
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

  revealPanel(host);                    // ★[열기]와 같은 팝업(대표 2026-08-26)
  wireRegMode(host, stage);

  // 쌓아 둔 자산 줄 — 저장 전까지는 화면 안에만 있다(서버에 아무것도 안 만든다)
  const lines = [];
  // ★매입금액 양방향(대표 2026-08-24): 총액을 적으면 분할 모드, 비우면 합계 모드.
  //   마지막에 만진 쪽이 이긴다 — 줄 금액을 직접 고치면 합계 모드로 돌아간다.
  let totalManual = false;
  const manualTotal = () => Number(($("#sl-amount")?.value || "").replace(/[^0-9]/g, "")) || 0;
  const splitTotal = () => {
    // 총액을 대당가로 균등분할. 나눠떨어지지 않는 원 단위는 수량 1짜리 첫 줄에 얹는다 —
    // 합계가 총액과 정확히 같아야 '전표 금액 불일치' 경고가 안 뜬다.
    const total = manualTotal();
    const totalQty = lines.reduce((n, l) => n + (Number(l.qty) || 1), 0);
    if (!total || !totalQty) return 0;
    const per = Math.floor(total / totalQty);
    let rem = total - per * totalQty;
    lines.forEach((l) => { l.purchasePrice = per; });
    if (rem > 0) {
      const one = lines.find((l) => (Number(l.qty) || 1) === 1);
      if (one) { one.purchasePrice = per + rem; rem = 0; }
    }
    return rem;                          // 0이 아니면 수량 1짜리 줄이 없어 못 얹은 나머지
  };
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
    // ★합계 모드(기본): 매입금액 = 줄별 합. 분할 모드(총액을 손으로 적음)에서는
    //   총액 칸을 덮지 않는다 — 사람이 적은 값이 기준이고 줄이 그걸 따라간다.
    const amt = $("#sl-amount");
    if (amt && !totalManual) amt.value = fmtNum(total);   // 콤마 표기(제출은 콤마를 떼고)
    const sumBox = $("#sa-sum");
    if (sumBox) {
      if (totalManual) {
        const t = manualTotal();
        const gap = t - total;
        sumBox.innerHTML = `<div class="inline-row" style="margin:8px 0 0;">
             <span>총액 <b>${fmtWon(t)}</b>을 ${cnt}대에 균등분할 — 줄 합계 ${fmtWon(total)}</span>
             ${gap ? `<span class="chip chip-red" title="수량 1짜리 줄이 없어 나머지를 못 얹었습니다 — 한 줄 금액을 직접 조정하세요">⚠ ${fmtWon(Math.abs(gap))} 차이</span>`
                   : `<span class="muted">— 줄 금액을 직접 고치면 합계 모드로 돌아갑니다</span>`}
           </div>`;
      } else {
        sumBox.innerHTML = total
          ? `<div class="inline-row" style="margin:8px 0 0;">
               <span>매입금액 <b>${fmtWon(total)}</b> = ${cnt}대 합계</span>
               <span class="muted">— 총액을 직접 적으면 대당 균등분할로 바뀝니다</span>
             </div>` : "";
      }
    }
    // ★✕(줄 빼기)·📷(연속 스캔) 연결 — 표를 다시 그릴 때마다 새로 단다.
    //   (2026-08-08 검토에서 발견: 버튼만 그려지고 연결이 없어 눌러도 반응이 없었다)
    $$("button[data-rm]", box).forEach((b) => b.addEventListener("click", () => {
      lines.splice(Number(b.dataset.rm), 1);
      snOpen = null;                       // 줄 번호가 밀리므로 열려 있던 스캔 패널은 닫는다
      if (totalManual) splitTotal();       // 분할 모드 — 남은 줄로 총액을 다시 나눈다
      drawLines();
    }));
    $$("button[data-sn]", box).forEach((b) => b.addEventListener("click", () => {
      snOpen = Number(b.dataset.sn);
      drawLines();
    }));
    if (snOpen !== null && lines[snOpen]) drawScanPanel(box);
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
      // ★재고 구분은 화면에서 없앴다(2026-08-24) — 등급에서 자동으로 정해진다
      grade: $("#sa-grade").value, serial,
      location: $("#sa-location").value.trim(),
      productCode: $("#sa-productcode").value.trim(),
      serials: [],                       // 여러 대일 때 연속 스캔으로 채운다
      purchasePrice: priceWithVat("sa", "#sa-price"),   // '부가세 별도'면 포함가(×1.1)로
    };
    // 사양(CPU·RAM·SSD…)도 함께 담는다 — 예전에는 등록 뒤 한 대씩 열어야만 넣을 수
    // 있었다(대표 지적 2026-08-04). 같은 모델을 연달아 담을 땐 값이 남아 있어 그대로 따라간다.
    Object.keys(SPEC_LABELS).forEach((k) => { line[k] = ($(`#sa-${k}`).value || "").trim(); });
    lines.push(line);
    if (totalManual) splitTotal();       // 분할 모드 — 새 줄까지 포함해 총액을 다시 나눈다
    // 다음 줄을 빨리 넣도록 모델·시리얼만 비우고 나머지는 남긴다(같은 거래처 물건은 대개 비슷하다)
    $("#sa-model").value = ""; $("#sa-serial").value = "";
    $("#sa-brief").innerHTML = "";
    _briefLast = "";
    $("#sa-model").focus();
    drawLines();
  };
  // 총액을 적는 순간 분할 모드 — 이미 담긴 줄도 그 자리에서 다시 나눈다
  $("#sl-amount")?.addEventListener("input", () => {
    totalManual = manualTotal() > 0;
    if (totalManual && lines.length) splitTotal();
    if (lines.length) drawLines();
  });
  attachSpecAutocomplete("sa-");
  attachAutocomplete($("#sl-supplier", host), "supplier");   // 거래처도 CPU·램처럼(2026-08-24)
  attachCodeLookup("sa-productcode", null, { mall: true });   // 전표에 자산 담을 때도
  attachVatHint("sa", "#sa-price");
  $("#sa-add").addEventListener("click", addLine);
  ["#sa-model", "#sa-serial", "#sa-price", "#sa-qty"].forEach((sel) => {
    const el = $(sel);
    if (el) el.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); addLine(); } });
  });
  _briefLast = "";
  watchModelBrief("#sa-model", "#sa-maker", "#sa-brief");   // 재고를 이 화면에서 바로 확인
  attachModelMaster("#sa-model", "#sa-maker", "#sa-cat", "#sa-master");   // 모델 마스터 대조·보완(2026-09-02)

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
      // ★전표 금액 = 분할 모드면 사람이 적은 총액(거래처에 준 돈), 합계 모드면 줄별 합.
      //   분할 모드의 줄들은 splitTotal 이 총액에 정확히 맞춰 놓았으므로 대조도 일치한다
      //   (수량 1짜리 줄이 없어 나머지를 못 얹은 드문 경우만 ⚠ 칩이 남는다).
      const slipVals = slipFormValues(host);
      const lineSum = lines.reduce(
        (n, l) => n + (Number(l.purchasePrice) || 0) * (Number(l.qty) || 1), 0);
      slipVals.totalAmount = (totalManual && manualTotal()) ? manualTotal() : lineSum;
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
  // ★좌우 5:5(2026-08-14 대표): 자산을 누르면 상세가 '아래'가 아니라 '오른쪽'에 뜬다.
  //   왼쪽=전표+자산 표, 오른쪽=자산 상세(sticky — 표를 스크롤해도 따라온다).
  //   자산 상세가 닫혀 있으면 오른쪽 칸을 숨겨 왼쪽이 전체 폭을 쓴다.
  //   좁은 화면에서는 flex-wrap으로 예전처럼 아래로 내려간다.
  // ★팝업을 넓게 쓴다 — 기본 900px에 묶이면 5:5가 안 들어가 오른쪽 칸이 아래로 접힌다.
  host.classList.add("wide");
  host.innerHTML = `
    <div style="display:flex; flex-wrap:wrap; gap:12px; align-items:flex-start;">
    <div style="flex:1 1 360px; min-width:0;">
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
        <h3 style="margin:0; flex:1;">자산 ${s.assets.length}대${(() => {
          // ★매입은 두 사업부의 공통 앞단이라 렌탈 물건도 전표에 그대로 남는다.
          //   다만 그중 몇 대가 '팔 수 있는 물건'인지는 전표를 열자마자 보여야 한다
          //   (대표 2026-08-31: "렌탈이라서 판매불가로 빼야 하는 거 아니냐").
          const rc = s.assets.filter((a) => a.division === "rental").length;
          return rc ? ` <span class="chip" id="sd-rentalcount"
            title="이 전표의 ${rc}대는 렌탈 사업부(RMS) 귀속이라 판매 재고에 잡히지 않습니다.
매입 기록·금액은 그대로 남습니다 — 사업부는 그 위에 얹히는 소유 표시일 뿐입니다."
            style="background:#eef2ff;color:#3730a3;border:1px solid #c7d2fe;font-weight:400;font-size:12px;"
            >판매 ${s.assets.length - rc}대 · 렌탈 ${rc}대</span>` : "";
        })()}
          <span class="muted" style="font-weight:400;">자산합 ${fmtWon(s.assignedAmount)} / 전표 ${fmtWon(s.totalAmount)}</span>
          ${s.repairTotal ? `<span class="chip chip-blue" style="font-weight:400;"
            title="이 전표 자산들에 붙은 부품·수리비 합계.&#10;전표 금액(거래처에 준 돈)과는 별개라 금액 대조에는 안 들어갑니다.">
            🔩 부품·수리 ${fmtWon(s.repairTotal)} · 총원가 ${fmtWon(s.costTotal)}</span>` : ""}
          ${amountGapChip(s, true)}
          ${canEdit && amountGap(s) ? `<button class="btn btn-sm" id="sd-syncamt"
            title="전표 매입금액을 자산 매입가 합계로 맞춥니다">금액 맞추기</button>` : ""}</h3>
        ${canEdit && !s.cancelledAt
          ? `<button class="btn btn-sm btn-primary" id="sd-add">＋ 자산 추가</button>` : ""}
      </div>
      <div id="sd-assetform"></div>
      <p class="muted" style="font-size:12px; margin:4px 0;">관리번호를 누르면 그 자리에서 바로 수정할 수 있습니다.
        ${stockSyncOn() ? "몰 재고 전송 체크 = 그 자산이 쇼핑몰 재고 수에 올라갑니다(제품코드 필요)."
          : "재고는 제품코드·상태·재고구분(가재고 제외)으로 정해집니다."}</p>
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
        ${stockSyncOn() ? `
        <button class="btn btn-sm" id="sd-stock-on"
          title="선택한 자산을 쇼핑몰 재고 수에 올립니다(제품코드가 있는 것만)">📦 일괄 몰 재고 전송</button>
        <button class="btn btn-sm" id="sd-stock-off"
          title="선택한 자산을 쇼핑몰 재고 수에서 뺍니다">📴 전송 해제</button>` : ""}
        <button class="btn btn-sm" id="sd-parts-add"
          title="선택한 자산들에 같은 부품(RAM·SSD)을 오늘 단가로 한 번에 장착(원가+)하거나 회수(원가− · 다운그레이드)합니다">🔩 부품 일괄</button>
        <button class="btn btn-sm" id="sd-base-btn"
          title="제품코드의 출고 기준 사양(예: DDR4 8GB·M.2 SATA 256GB)과 실물 스펙을 비교해&#10;부족한 부품을 오늘 단가로 채웁니다. 상위 용량이 꽂혀 왔으면 회수(−)도 제안합니다.&#10;확인창에서 자산별로 보고 적용합니다 — 두 번 눌러도 중복되지 않습니다.">🧬 기준사양 채우기</button>
      </div>
      <div id="sd-parts-box"></div>
      <div id="sd-base-box"></div>
      <div class="inline-row" style="margin:6px 0 0;">
        <button class="btn btn-sm" id="sd-cancel-assets" style="border-color:var(--danger);"
          title="고른 자산만 매입취소합니다 — 전표는 그대로 두고 그 제품만 무릅니다.&#10;재고·전표 금액에서 빠집니다. 주문에 나간 자산은 취소되지 않습니다.">🚫 선택 매입취소</button>
        <span class="muted" style="font-size:12px;">전표는 그대로 두고 고른 제품만 무릅니다</span>
      </div>` : ""}
      <div class="table-wrap"><table class="sd-assets">
        <thead><tr><th style="width:28px;"></th><th>관리번호</th><th>브랜드 / 모델</th><th>스펙</th>
          <th>등급</th><th>상태</th><th>매입가</th><th>부품·수리</th><th>제품코드</th>
          ${stockSyncOn() ? "<th>몰 재고 전송</th>" : ""}</tr></thead>
        <tbody>${s.assets.map((a) => `
          <tr><td><input type="checkbox" class="sd-pick" data-aid="${a.id}"></td>
          <td><button class="btn btn-ghost btn-sm" data-openasset="${a.id}"
                 style="font-weight:600; padding:2px 6px;"
                 title="이 자산을 여기서 수정합니다">${escapeHtml(a.assetNo)}</button>${divBadge(a)}</td>
          <td>${escapeHtml([a.maker, a.model].filter(Boolean).join(" "))}</td>
          <td class="muted sd-spec">${escapeHtml(specSummary(a))}</td>
          <td>${escapeHtml(a.grade)}</td><td>${statusCell(a, a.sale
            ? `<div class="muted" style="font-size:11.5px; white-space:nowrap;"
                 title="TMS 판매 ${escapeHtml(a.sale.slipNo || "")} — 자산 상세의 💻 판매 정보에서 마진까지 봅니다">
                 💻 ${escapeHtml(a.sale.channel || "")} · ${escapeHtml(a.sale.customer || "")} · ${escapeHtml(a.sale.date || "")}</div>`
            : "")}</td>
          <td>${fmtWon(a.purchasePrice)}</td>
          <td>${a.repairCost ? `<span title="부품·수리비 — 원가에 합산됩니다">${fmtWon(a.repairCost)}</span>`
                             : '<span class="muted">-</span>'}</td>
          <td class="muted sd-code" style="font-size:12px;">${escapeHtml(a.productCode || "-")}</td>
          ${stockSyncOn() ? `<td>${a.division === "rental"
            ? `<span class="chip" title="렌탈 사업부(RMS) 귀속이라 판매 재고가 아닙니다 —
쇼핑몰 재고·판매가능 수량 어디에도 잡히지 않습니다.
팔 물건이면 [↔ 사업부 이관]으로 판매 사업부에 먼저 넘기세요."
                 style="background:#eef2ff;color:#3730a3;border:1px solid #c7d2fe;font-size:11px;">판매불가</span>`
            : canEdit && !s.cancelledAt
            ? `<input type="checkbox" class="sd-stock" data-aid="${a.id}" ${a.stockListed ? "checked" : ""}
                 title="${a.productCode ? "체크하면 쇼핑몰 재고 수에 올라갑니다" : "제품코드를 먼저 넣어야 켤 수 있습니다"}">`
            : (a.stockListed ? '<span class="chip chip-green">전송</span>' : '<span class="muted">-</span>')}</td>` : ""}
          </tr>`).join("")
          || `<tr><td colspan="${stockSyncOn() ? 10 : 9}" class="muted">이 전표에 등록된 자산이 없습니다. 위 [＋ 자산 추가]를 누르세요.</td></tr>`}
        </tbody></table></div>
      ${(s.cancelledAssets || []).length ? `
      <details style="margin-top:10px;">
        <summary class="muted" style="cursor:pointer;">🚫 매입취소된 자산 ${s.cancelledAssets.length}대 — 되돌리려면 펼치세요</summary>
        <div class="table-wrap" style="margin-top:6px;"><table class="sd-assets">
          <thead><tr><th>관리번호</th><th>브랜드 / 모델</th><th>매입가</th><th>사유</th><th></th></tr></thead>
          <tbody>${s.cancelledAssets.map((a) => `<tr>
            <td><b>${escapeHtml(a.assetNo)}</b></td>
            <td>${escapeHtml([a.maker, a.model].filter(Boolean).join(" ") || "-")}</td>
            <td>${fmtWon(a.purchasePrice)}</td>
            <td class="muted sd-spec">${escapeHtml(a.cancelReason || "-")}</td>
            <td><button class="btn btn-sm" data-uncancel="${a.id}"
                  title="취소 직전 상태로 되돌립니다">↩ 되돌리기</button></td>
          </tr>`).join("")}</tbody></table></div>
      </details>` : ""}
    </div>
    </div>
    <div id="sd-assetdetail" style="flex:1 1 360px; min-width:0; display:none;
         position:sticky; top:8px; max-height:calc(100vh - 16px); overflow-y:auto;"></div>
    </div>`;
  $("#sd-close").addEventListener("click", () => { state.slipDetailId = undefined; host.innerHTML = ""; });

  // 전표 안에서 자산을 바로 열어 고친다 — 자산 목록으로 찾아가지 않아도 되게.
  // 저장하면 전표를 다시 그려 금액·상태가 즉시 맞춰진다(같은 데이터를 두 곳이 보므로).
  // ★상세는 오른쪽 5:5 칸에 뜬다(2026-08-14 대표). 닫으면 칸을 숨겨 왼쪽이 전체 폭.
  const adPane = $("#sd-assetdetail", host);
  const openAssetPane = () => {
    adPane.style.display = "block";
    renderAssetDetail("#sd-assetdetail", () => renderSlipDetail());
  };
  adPane.addEventListener("click", (e) => {
    if (e.target && e.target.id === "ad-close") adPane.style.display = "none";
  });
  $$("button[data-openasset]", host).forEach((b) => b.addEventListener("click", () => {
    state.assetDetailId = Number(b.dataset.openasset);
    openAssetPane();
  }));
  // 저장 후 전표를 다시 그려도 열어 둔 자산 상세를 유지한다(예전엔 저장하면 닫혔다)
  if (state.assetDetailId && s.assets.some((x) => x.id === state.assetDetailId)) {
    openAssetPane();
  }

  // 재고반영 — 개별 체크는 즉시 저장, 일괄 버튼은 선택분을 한 번에
  const bulkStock = async (ids, on) => {
    if (!ids.length) { toast("자산을 먼저 선택하세요.", true); return; }
    try {
      const r = await api("/api/assets/stock-listing", { method: "POST", body: { ids, on } });
      let msg = `${on ? "몰 재고 전송" : "전송 해제"} ${r.ok}대`;
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

  // 🚫 선택 매입취소 — 전표는 그대로 두고 고른 제품만 무른다(대표 2026-08-24)
  $("#sd-cancel-assets")?.addEventListener("click", async () => {
    const ids = pickIds();
    if (!ids.length) { toast("취소할 자산을 먼저 고르세요.", true); return; }
    const reason = prompt(`선택한 ${ids.length}대를 매입취소합니다.\n`
      + "재고와 전표 금액에서 빠집니다(전표 자체는 그대로 남습니다).\n\n"
      + "사유를 적어 주세요 — 나중에 이 기록만 남습니다.", "");
    if (reason === null) return;
    if (!reason.trim()) { toast("사유를 적어야 취소할 수 있습니다.", true); return; }
    try {
      const r = await api("/api/assets/purchase-cancel",
                          { method: "POST", body: { ids, reason } });
      const sk = r.skipped || [];
      toast(`매입취소 ${r.ok}대` + (sk.length ? ` · 건너뜀 ${sk.length}대` : ""),
            r.ok === 0 && sk.length > 0);
      if (sk.length) {
        alert("취소하지 못한 자산:\n"
          + sk.map((x) => `· ${x.assetNo || "#" + x.id} — ${x.reason}`).join("\n"));
      }
      renderSlipDetail();
    } catch (err) { toast(err.message, true); }
  });
  $$("button[data-uncancel]", host).forEach((b) => b.addEventListener("click", async () => {
    if (!confirm("매입취소를 되돌립니다. 취소 직전 상태로 돌아갑니다.\n계속할까요?")) return;
    try {
      const r = await api("/api/assets/purchase-uncancel",
                          { method: "POST", body: { ids: [Number(b.dataset.uncancel)] } });
      toast(r.ok ? "되돌렸습니다." : ((r.skipped || [{}])[0].reason || "되돌리지 못했습니다"), !r.ok);
      renderSlipDetail();
    } catch (err) { toast(err.message, true); }
  }));
  // 일괄 코드 입력칸에도 자동완성 — 여기가 한 번에 여러 대를 바꾸는 자리라 오타가 제일 아프다
  attachCodeLookup("sd-code-input", null, { mall: true });
  const pickIds = () => $$("input.sd-pick", host).filter((cb) => cb.checked).map((cb) => Number(cb.dataset.aid));
  const onBtn = $("#sd-stock-on"), offBtn = $("#sd-stock-off");
  if (onBtn) onBtn.addEventListener("click", () => bulkStock(pickIds(), true));
  if (offBtn) offBtn.addEventListener("click", () => bulkStock(pickIds(), false));
  // 🔩 부품 일괄 추가 — 단가는 화면이 아니라 서버가 단가표에서 읽는다(출처 단일화)
  const partsBtn = $("#sd-parts-add");
  if (partsBtn) partsBtn.addEventListener("click", async () => {
    const ids = pickIds();
    if (!ids.length) { toast("자산을 먼저 선택하세요.", true); return; }
    const box = $("#sd-parts-box");
    if (box.innerHTML) { box.innerHTML = ""; return; }     // 다시 누르면 접기
    let parts;
    try { parts = (await api("/api/parts")).filter((p) => p.enabled); }
    catch (err) { toast(err.message, true); return; }
    if (!parts.length) {
      toast("부품 단가표가 비어 있습니다 — 기준정보 ▸ 부품 단가표에서 먼저 등록하세요.", true);
      return;
    }
    box.innerHTML = `
      <div style="border:1px solid var(--border); border-radius:10px; padding:10px 12px; margin:6px 0;">
        <b style="font-size:13px;">선택 ${ids.length}대에 부품 반영</b>
        <select id="sd-parts-mode" style="font-size:13px; margin-left:6px;"
          title="회수(빼냄)는 단가만큼 원가에서 빠져 순수익에 더해집니다 — 다운그레이드 출고용">
          <option value="add">장착 (원가에 더함)</option>
          <option value="remove">회수 (원가에서 뺌)</option>
        </select>
        <span class="muted" style="font-size:12px;">— 오늘 단가가 각 자산 원가(수리비)에 반영되고, RAM/SSD 스펙 칸도 함께 바뀝니다.
          CPU·그래픽·파워·보드는 호환(소켓·보드·파워)을 직접 확인한 뒤 반영하세요 — 자동 기입은 안 붙습니다.</span>
        ${["part", "repair", "paint"].map((k) => {
          const ps = parts.filter((p) => (p.kind || "part") === k);
          return ps.length ? `<div style="margin-top:6px;">
            <div class="muted" style="font-size:12px; font-weight:600;">${PART_KIND_LABEL[k]}</div>
            <div class="inline-row" style="gap:10px; flex-wrap:wrap; margin-top:2px;">
              ${ps.map((p) => `<label class="check-line" style="font-size:13px;">
                <input type="checkbox" class="sd-part-pick" data-pid="${p.id}" data-price="${p.price}">
                ${escapeHtml(p.name)} <span class="muted">${fmtWon(p.price)}</span></label>`).join("")}
            </div></div>` : "";
        }).join("")}
        <div class="inline-row" style="margin-top:8px;">
          <span class="muted" id="sd-parts-sum" style="font-size:13px;">부품을 고르세요</span>
          <span style="flex:1"></span>
          <button class="btn btn-sm btn-primary" id="sd-parts-go">반영</button>
          <button class="btn btn-ghost btn-sm" id="sd-parts-cancel">닫기</button>
        </div>
      </div>`;
    const sum = () => {
      const picked = $$(".sd-part-pick", box).filter((c) => c.checked);
      const per = picked.reduce((t, c) => t + Number(c.dataset.price), 0);
      const rm = $("#sd-parts-mode").value === "remove";
      $("#sd-parts-sum").textContent = picked.length
        ? `대당 ${rm ? "−" : ""}${fmtWon(per)} × ${ids.length}대 = 총 ${rm ? "−" : ""}${fmtWon(per * ids.length)}${rm ? " (원가에서 빠짐)" : ""}`
        : "부품을 고르세요";
    };
    $$(".sd-part-pick", box).forEach((c) => c.addEventListener("change", sum));
    $("#sd-parts-mode").addEventListener("change", sum);
    $("#sd-parts-cancel").addEventListener("click", () => { box.innerHTML = ""; });
    $("#sd-parts-go").addEventListener("click", async () => {
      const remove = $("#sd-parts-mode").value === "remove";
      const partIds = $$(".sd-part-pick", box).filter((c) => c.checked)
        .map((c) => ({ id: Number(c.dataset.pid), remove }));
      if (!partIds.length) { toast("부품을 고르세요.", true); return; }
      const per = $$(".sd-part-pick", box).filter((c) => c.checked)
        .reduce((t, c) => t + Number(c.dataset.price), 0);
      if (!confirm(remove
          ? `선택한 ${ids.length}대에서 부품을 회수(빼냄) 처리합니다.\n\n`
            + `대당 −${fmtWon(per)} — 총 ${fmtWon(per * ids.length)}이 원가에서 빠져 순수익에 더해집니다.\n`
            + "스펙 칸에서 그 부품 표기도 지워집니다. 잘못 넣으면 각 자산의 수리 기록에서 지울 수 있습니다.\n\n계속할까요?"
          : `선택한 ${ids.length}대에 부품을 추가합니다.\n\n`
            + `대당 ${fmtWon(per)} — 총 ${fmtWon(per * ids.length)}이 원가에 더해집니다.\n`
            + "지금 단가로 동결되며, 잘못 넣으면 각 자산의 수리 기록에서 지울 수 있습니다.\n\n계속할까요?")) return;
      try {
        const r = await api("/api/assets/bulk-repairs", {
          method: "POST", body: { ids, partIds } });
        toast(`${r.ok}대에 부품 ${remove ? "회수" : "추가"} 완료 (총 ${fmtWon(r.totalCost)})`
          + (r.failed.length ? ` · 실패 ${r.failed.length}대` : ""));
        renderSlipDetail();
      } catch (err) { toast(err.message, true); }
    });
  });
  // 🧬 기준사양 채우기 — 코드 스펙(출고 기준)과 실물 스펙의 차이를 계산해 부품을 기입.
  //    서버가 적용 직전에 다시 계산하므로 미리보기가 낡아도, 두 번 눌러도 안전하다.
  const baseBtn = $("#sd-base-btn");
  if (baseBtn) baseBtn.addEventListener("click", async () => {
    const box = $("#sd-base-box");
    if (box.innerHTML) { box.innerHTML = ""; return; }     // 다시 누르면 접기
    let r;
    try { r = await api(`/api/purchase-batches/${s.id}/base-spec-gaps`); }
    catch (err) { toast(err.message, true); return; }
    const rows = r.assets || [];
    const doable = rows.filter((x) => (x.actions || []).length);
    const line = (x) => {
      if (x.reason) return `<span class="muted">${escapeHtml(x.reason)}</span>`;
      if (!(x.actions || []).length)
        return `<span class="chip chip-green">기준사양과 동일 — 채울 것 없음</span>`
          + (x.warns || []).map((w) => ` <span class="chip chip-red">${escapeHtml(w)}</span>`).join("");
      return x.actions.map((ac) => `<span class="chip ${ac.remove ? "chip-violet" : "chip-blue"}">
          ${ac.remove ? "회수" : "장착"} ${escapeHtml(ac.name)} ${ac.price.toLocaleString("ko-KR")}원</span>`).join(" ")
        + ` <b>= ${x.total.toLocaleString("ko-KR")}원</b>`
        + (x.warns || []).map((w) => ` <span class="chip chip-red">${escapeHtml(w)}</span>`).join("");
    };
    box.innerHTML = `
      <div style="border:1px solid var(--border); border-radius:10px; padding:10px 12px; margin:6px 0;">
        <b style="font-size:13px;">🧬 기준사양 채우기 — 전표 자산 ${rows.length}대 중 ${doable.length}대 적용 대상</b>
        <span class="muted" style="font-size:12px;">— 코드 스펙 기준, 오늘 단가로 원가·스펙 칸에 기입됩니다</span>
        <div class="table-wrap" style="margin-top:6px;"><table>
          <thead><tr><th style="width:28px;"><input type="checkbox" id="sd-base-all" checked></th>
            <th>관리번호</th><th>코드</th><th>실물(램/저장)</th><th>적용 내용</th></tr></thead>
          <tbody>${rows.map((x) => `
            <tr style="${(x.actions || []).length ? "" : "opacity:.6;"}">
              <td>${(x.actions || []).length
                ? `<input type="checkbox" class="sd-base-pick" data-aid="${x.assetId}" checked>` : ""}</td>
              <td><b>${escapeHtml(x.assetNo)}</b></td>
              <td>${escapeHtml(x.code || "-")}</td>
              <td class="muted">${escapeHtml(x.ram || "없음")} / ${escapeHtml(x.ssd || "없음")}</td>
              <td>${line(x)}</td></tr>`).join("")
            || `<tr><td colspan="5" class="muted">자산이 없습니다.</td></tr>`}
          </tbody></table></div>
        <div class="inline-row" style="margin-top:8px;">
          <span class="muted" id="sd-base-sum" style="font-size:13px;"></span>
          <span style="flex:1"></span>
          <button class="btn btn-sm btn-primary" id="sd-base-go" ${doable.length ? "" : "disabled"}>선택 자산에 적용</button>
          <button class="btn btn-ghost btn-sm" id="sd-base-close">닫기</button>
        </div>
      </div>`;
    const sum = () => {
      const picked = $$(".sd-base-pick", box).filter((c) => c.checked)
        .map((c) => rows.find((x) => x.assetId === Number(c.dataset.aid)));
      const t2 = picked.reduce((t3, x) => t3 + (x?.total || 0), 0);
      $("#sd-base-sum").textContent = picked.length
        ? `선택 ${picked.length}대 — 합계 ${t2.toLocaleString("ko-KR")}원이 원가에 반영됩니다`
        : "적용할 자산을 고르세요";
    };
    sum();
    $$(".sd-base-pick", box).forEach((c) => c.addEventListener("change", sum));
    $("#sd-base-all", box).addEventListener("change", (e) => {
      $$(".sd-base-pick", box).forEach((c) => { c.checked = e.target.checked; });
      sum();
    });
    $("#sd-base-close", box).addEventListener("click", () => { box.innerHTML = ""; });
    $("#sd-base-go", box).addEventListener("click", async () => {
      const ids = $$(".sd-base-pick", box).filter((c) => c.checked).map((c) => Number(c.dataset.aid));
      if (!ids.length) { toast("적용할 자산을 고르세요.", true); return; }
      if (!confirm(`선택한 ${ids.length}대에 기준사양 부족분을 기입합니다.\n\n`
          + "적용 직전 상태로 다시 계산해 들어가며, 자산 스펙 칸도 함께 갱신됩니다.\n"
          + "잘못 넣으면 각 자산의 수리 기록에서 지울 수 있습니다.\n\n계속할까요?")) return;
      try {
        const res = await api("/api/assets/base-spec-apply", { method: "POST", body: { ids } });
        const done = res.results.filter((x) => x.applied).length;
        const skipped = res.results.filter((x) => x.reason).length;
        toast(`${done}대 기입 완료 (합계 ${res.totalCost.toLocaleString("ko-KR")}원)`
          + (skipped ? ` · ${skipped}대 건너뜀` : ""));
        renderSlipDetail();
      } catch (err) { toast(err.message, true); }
    });
  });

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
  attachAutocomplete($("#sl-supplier", host), "supplier");   // 상세의 거래처 칸도 자동완성
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
      <b>자산 등록</b> <span class="muted">— 관리번호는 자동 발번(YYMMDD-NNNN, OWS 번호대 5000~)됩니다.
        TMS 번호(0001~4999)는 직접 입력하세요(1대만) — 연동 사본에 있는 번호만 받습니다.</span>
      <div class="form-grid" style="margin-top:8px;">
        ${slip ? `<label>전표<input type="text" value="${escapeHtml(slip.slipNo || "")} ${escapeHtml(slip.supplierName || "")}" disabled></label>`
          : `<label>매입 전표 (선택)<select id="ar-batch"><option value="">- 전표 없이 등록 -</option>
              ${slipOptions.map((s) => `<option value="${s.id}">${escapeHtml(`${s.slipNo || ""} ${s.stageLabel} ${s.purchaseDate} ${s.supplierName || ""}`)}</option>`).join("")}
            </select></label>`}
        <label>카테고리 *<select id="ar-cat">${state.categories.filter((c) => c.enabled).map((c) => `<option value="${c.id}">${escapeHtml(c.name)}</option>`).join("")}</select></label>
        <label>브랜드<input type="text" id="ar-maker" placeholder="예: LENOVO"></label>
        <label style="grid-column:1 / -1;">모델명
          <input type="text" id="ar-model" placeholder="예: L480">
          <div id="ar-master" style="margin-top:4px;"></div>
          <div id="ar-brief"></div>
        </label>
        <label>시리얼번호<input type="text" id="ar-serial"></label>
        <label>등급<select id="ar-grade">${state.meta.grades.map((g) => `<option ${g === "미정" ? "selected" : ""}>${escapeHtml(g)}</option>`).join("")}</select></label>
        <label>보관위치<input type="text" id="ar-location"></label>
        <label>매입가(대당)<input type="text" id="ar-price" data-money value="0"></label>
        ${vatSelectHtml("ar")}
        <label>판매가<input type="text" id="ar-saleprice" data-money value="0"></label>
        <label>수량<input type="number" id="ar-qty" value="1" min="1" max="100"></label>
        <label>관리번호 직접 지정
          <span style="display:flex; gap:6px; align-items:center;">
            <input type="text" id="ar-no" placeholder="비우면 자동" style="flex:1;">
            <button type="button" class="btn btn-sm" id="ar-autono" style="white-space:nowrap; margin-top:4px;"
              title="오늘 OWS 번호대(5000~)에서 번호를 하나 미리 받아 칸에 넣습니다 — 라벨을 먼저 붙일 때.&#10;받은 번호는 등록하지 않아도 다시 나가지 않습니다. 비워 두고 등록해도 같은 규칙으로 자동 발번됩니다.">자동 채번</button>
          </span></label>
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
  attachVatHint("ar", "#ar-price");
  _briefLast = "";
  watchModelBrief("#ar-model", "#ar-maker", "#ar-brief");   // 재고 대조를 이 화면 안에서
  attachModelMaster("#ar-model", "#ar-maker", "#ar-cat", "#ar-master");   // 모델 마스터 대조·보완(2026-09-02)
  $("#ar-close").addEventListener("click", () => {
    host.innerHTML = "";
    if (state.assetFormDirty) {          // 등록한 게 있으면 그때 목록을 새로 그린다
      state.assetFormDirty = false;
      if (onDone) onDone(); else renderSlipDetail();
    }
  });
  attachSpecAutocomplete("ar-");
  // [자동 채번](2026-09-03, A4) — OWS 번호대(5000~)에서 오늘 번호를 하나 받아 칸에 넣는다.
  //   409 = 오늘 TMS 사본에 5000번대가 있어 채번을 보류한 것 — 사본을 확인해야 한다(서버 메시지 그대로).
  $("#ar-autono").addEventListener("click", async () => {
    const btn = $("#ar-autono");
    btn.disabled = true;
    try {
      const r = await api("/api/assets/next-no", { method: "POST", body: {} });
      $("#ar-no").value = r.assetNo;
      $("#ar-qty").value = 1;                  // 번호를 지정하면 1대씩만 등록된다(서버 규칙)
      toast(`관리번호 ${r.assetNo} 받았습니다.` + (r.verified ? "" : ` (연동 사본과 대조 못 함: ${r.note || "창구 없음"})`));
    } catch (err) { toast(err.message, true); }
    finally { btn.disabled = false; }
  });
  $("#ar-save").addEventListener("click", async () => {
    const btn = $("#ar-save");
    btn.disabled = true;
    const pickedBatch = slip ? slip.id : ($("#ar-batch").value ? Number($("#ar-batch").value) : null);
    const body = {
      batchId: pickedBatch, categoryId: Number($("#ar-cat").value),
      maker: $("#ar-maker").value, model: $("#ar-model").value, serial: $("#ar-serial").value,
      grade: $("#ar-grade").value, location: $("#ar-location").value,
      purchasePrice: priceWithVat("ar", "#ar-price"),   // '부가세 별도'면 포함가(×1.1)로
      salePrice: $("#ar-saleprice").value.replaceAll(",", "") || 0,
      qty: Number($("#ar-qty").value) || 1,
      assetNo: $("#ar-no").value.trim(), notes: $("#ar-notes").value,
    };
    Object.keys(SPEC_LABELS).forEach((k) => { body[k] = $(`#ar-${k}`).value; });
    try {
      const created = await api("/api/assets", { method: "POST", body });
      toast(`${created.length}대 등록 완료`);
      // 마스터에 없는 모델명 — 등록은 됐다. 모델명 칸 옆 [＋ 마스터에 등록]으로 올려 두라고만 알린다(2026-09-02)
      if (created[0] && created[0].modelWarning) toast(`${created[0].modelWarning} — 등록은 됐습니다. 모델명 칸 옆 [＋ 마스터에 등록]으로 올려 두세요.`);
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
  return (state.meta.tiers || ["가용", "실재고", "가재고"]).map((t) =>
    `<option value="${escapeHtml(t)}" ${t === sel ? "selected" : ""}
       title="${escapeHtml(h[t] || "")}">${escapeHtml(t)}${h[t] ? " — " + escapeHtml(h[t]) : ""}</option>`
  ).join("");
}

function tierHelpBox() {
  const h = (state.meta || {}).tierHelp || {};
  const rows = ((state.meta || {}).tiers || []).map((t) =>
    `<div style="display:flex; gap:8px; align-items:baseline; padding:2px 0;">
       <b style="flex:0 0 52px;">${escapeHtml(t)}</b>
       <span class="muted">${escapeHtml(h[t] || "")}</span></div>`).join("");
  return rows ? `<div style="margin:6px 0; padding:8px 10px; background:var(--bg);
    border:1px solid var(--border); border-radius:8px; font-size:13px;">${rows}</div>` : "";
}

/* ---------------- 실재고 / 가재고 전환 ----------------
   대표 요청(2026-08-04): 가용이 아닌 자산은 누군가 손을 대야 나간다. 어디에 몇 대가
   묶여 있는지 카테고리별로 보여 주고, 그 자리에서 수리 내역(비용·교체부품)을 적고
   세 구분 중 하나로 올린다. 비용은 총액을 넣으면 공급가·부가세가 자동으로 갈린다. */
/* ---------------- ↩ 복귀 후보 (2026-08-09 대표 승인) ----------------

   TMS가 반입·반납·판매취소로 표시한 출고/폐기 자산.
   자동으로 되살리지 않고(유령 재고 방지), 사람이 실물을 확인한 뒤
   [검수 후 복귀] 또는 [무시]로 원클릭 처리한다. */

async function renderRevertQueue(body) {
  const seq = ++state.renderSeq;
  body.innerHTML = `<div class="card placeholder"><p>불러오는 중…</p></div>`;
  let d;
  try { d = await api("/api/assets/revert-candidates"); }
  catch (err) { body.innerHTML = `<div class="card"><p class="muted">${escapeHtml(err.message)}</p></div>`; return; }
  if (seq !== state.renderSeq) return;
  if (!d.count) {
    body.innerHTML = `<div class="card"><h3>↩ 복귀 후보</h3>
      <p class="muted">지금 처리할 복귀 후보가 없습니다 — TMS에서 반입·반납으로 표시된
      출고/폐기 자산이 생기면 여기에 나타납니다(2시간마다 자동 확인).</p></div>`;
    return;
  }
  const pg = paged(d.rows, "revertq");
  body.innerHTML = `
    <div class="card">
      <h3>↩ 복귀 후보 ${d.count.toLocaleString("ko-KR")}대</h3>
      <p class="muted" style="margin:4px 0 8px;">
        TMS에는 <b>돌아온 물건</b>으로 표시되는데 OWS에는 출고/폐기로 남아 있는 자산입니다.
        창고에 실물이 있는지 확인한 뒤 <b>[검수 후 복귀]</b>를 누르면
        입고·실재고로 들어옵니다.
        실물이 없으면 <b>[무시]</b> — 다시 알리지 않습니다.</p>
      <div class="table-wrap"><table>
        <thead><tr><th>관리번호</th><th>분류</th><th>브랜드/모델</th><th>시리얼</th>
          <th>TMS 표시</th><th>현재</th><th>표시된 때</th><th></th></tr></thead>
        <tbody>${pg.rows.map((a) => `
          <tr>
            <td><b>${escapeHtml(a.assetNo)}</b></td>
            <td class="muted">${escapeHtml(a.categoryName || "")}</td>
            <td>${escapeHtml([a.maker, a.model].filter(Boolean).join(" ") || "-")}</td>
            <td class="muted">${escapeHtml(a.serial || "-")}</td>
            <td><span class="chip chip-blue">${escapeHtml(a.tmsLabel || "반입")}</span></td>
            <td>${statusChip(a.status)}</td>
            <td class="muted">${escapeHtml((a.flaggedAt || "").slice(0, 16).replace("T", " "))}</td>
            <td style="white-space:nowrap;">
              <button class="btn btn-sm btn-primary" data-restock="${a.id}"
                title="입고·실재고로 되살립니다">검수 후 복귀</button>
              <button class="btn btn-sm btn-ghost" data-dismissrv="${a.id}"
                title="실물이 없으면 — 다시 알리지 않습니다">무시</button>
            </td>
          </tr>`).join("")}
        </tbody></table></div>
      ${pg.bar}
    </div>`;
  wirePager(body, "revertq", () => renderRevertQueue(body));
  const act = async (aid, action) => {
    try {
      const rr = await api(`/api/assets/${aid}/restock`, { method: "POST", body: { action } });
      toast(action === "restock" ? "입고·실재고로 복귀했습니다." : "무시 처리했습니다.");
      if (rr && rr.rentalKept) {
        toast("ℹ 렌탈 사업부 자산이라 판매 재고에는 안 잡힙니다. "
          + "팔 물건이면 [↔ 사업부 이관]까지 하세요.", true);
      }
      renderRevertQueue(body);
      const tab = body.closest("#ptab-body") || document;
      fillAssetBadges(tab);                    // 서브탭 배지 숫자도 맞춘다
    } catch (err) { toast(err.message, true); }
  };
  $$("button[data-restock]", body).forEach((b) =>
    b.addEventListener("click", () => act(Number(b.dataset.restock), "restock")));
  $$("button[data-dismissrv]", body).forEach((b) =>
    b.addEventListener("click", () => act(Number(b.dataset.dismissrv), "dismiss")));
}


/* ---------------- ⚠ 번호 충돌 (2026-08-18 대표 지시) ----------------

   TMS에서 번호를 잘못 적어 '판매'로 찍힌 탓에, 실물이 멀쩡한데 셋팅 스캔이 막히는
   일이 있다. 셋팅에서 [⚠ 실물 있음]으로 붙인 자산이 여기에 쌓인다.
   여기서 번호를 맞바꾸거나 넘겨받아 바로잡는다.

   ★바로잡은 자산은 TMS 자동반영이 못 건드리게 잠긴다(assets.tms_lock).
     안 잠그면 2시간 뒤 다음 수집이 다시 '출고'로 되돌린다. */

async function renderConflictQueue(body) {
  const seq = ++state.renderSeq;
  body.innerHTML = `<div class="card placeholder"><p>불러오는 중…</p></div>`;
  let d;
  try { d = await api("/api/assets/number-conflicts"); }
  catch (err) { body.innerHTML = `<div class="card"><p class="muted">${escapeHtml(err.message)}</p></div>`; return; }
  if (seq !== state.renderSeq) return;
  const help = `<p class="muted" style="margin:4px 0 8px;">
      TMS에는 <b>나간 물건</b>으로 적혀 있는데 실물이 여기 있어, 셋팅에서 그대로 붙인 자산입니다.
      번호가 겹친 채로 두면 다음에 또 막히니 여기서 정리하세요.<br>
      · <b>맞바꾸기</b> — 두 실물의 번호가 서로 뒤바뀐 경우<br>
      · <b>넘겨받기</b> — 상대가 TMS 오입력으로 생긴 잘못된 기록인 경우(상대는 임시 번호로 물러납니다)<br>
      · <b>확인만</b> — 번호는 그대로 두고 닫습니다(TMS 쪽에서 고치기로 한 경우)</p>`;
  if (!d.count) {
    body.innerHTML = `<div class="card"><h3>⚠ 번호 충돌</h3>
      <p class="muted">지금 정리할 번호 충돌이 없습니다 — 셋팅에서 [⚠ 실물 있음]으로 붙인
      자산이 생기면 여기에 나타납니다.</p></div>`;
    return;
  }
  const pg = paged(d.rows, "conflictq");
  body.innerHTML = `
    <div class="card">
      <h3>⚠ 번호 충돌 ${d.count.toLocaleString("ko-KR")}대</h3>
      ${help}
      <div class="table-wrap"><table>
        <thead><tr><th>관리번호</th><th>브랜드/모델</th><th>시리얼</th><th>주문</th>
          <th>TMS가 말한 상태</th><th>붙인 사람</th><th>붙인 때</th><th></th></tr></thead>
        <tbody>${pg.rows.map((a) => `
          <tr>
            <td><b>${escapeHtml(a.assetNo)}</b></td>
            <td>${escapeHtml([a.maker, a.model].filter(Boolean).join(" ") || "-")}</td>
            <td class="muted">${escapeHtml(a.serial || "-")}</td>
            <td class="muted">${escapeHtml(a.orderNo || "-")}</td>
            <td>${statusChip(a.wasStatus || "shipped")}</td>
            <td class="muted">${escapeHtml(a.flagBy || "-")}</td>
            <td class="muted">${escapeHtml((a.flaggedAt || "").slice(0, 16).replace("T", " "))}</td>
            <td style="white-space:nowrap;">
              <button class="btn btn-sm btn-primary" data-numfix="${a.id}"
                data-no="${escapeHtml(a.assetNo)}" title="번호를 맞바꾸거나 넘겨받습니다">🔁 번호 바로잡기</button>
              <button class="btn btn-sm btn-ghost" data-cfclose="${a.id}"
                title="번호는 그대로 두고 닫습니다">확인만</button>
            </td>
          </tr>`).join("")}
        </tbody></table></div>
      ${pg.bar}
    </div>`;
  wirePager(body, "conflictq", () => renderConflictQueue(body));
  $$("button[data-numfix]", body).forEach((b) => b.addEventListener("click", () =>
    openNumberFix(Number(b.dataset.numfix), b.dataset.no, () => renderConflictQueue(body))));
  $$("button[data-cfclose]", body).forEach((b) => b.addEventListener("click", async () => {
    const reason = prompt("번호는 그대로 두고 닫습니다. 사유를 적어 주세요.", "TMS에서 수정하기로 함");
    if (reason === null) return;
    try {
      await api(`/api/assets/${b.dataset.cfclose}/number-conflict/close`,
                { method: "POST", body: { reason } });
      toast("닫았습니다.");
      renderConflictQueue(body);
      fillAssetBadges(body.closest("#ptab-body") || document);
    } catch (err) { toast(err.message, true); }
  }));
}

/* 🔁 번호 바로잡기 — 맞바꾸기(swap) / 넘겨받기(take).
   ★UNIQUE(관리번호) 때문에 곧바로 맞바꿀 수 없어 서버가 임시 번호를 한 번 거친다. */
function openNumberFix(assetId, myNo, onDone) {
  const host = document.createElement("div");
  host.innerHTML = `
    <div class="card in-modal">
      <h3 style="margin-top:0;">🔁 번호 바로잡기 <span class="muted" style="font-weight:400;">${escapeHtml(myNo)}</span></h3>
      <p class="muted" style="margin:4px 0 10px;">
        TMS에서 번호를 잘못 적어 겹친 경우를 정리합니다.
        바로잡은 자산은 <b>TMS 자동반영이 못 건드리게 잠깁니다</b> —
        안 잠그면 다음 수집(2시간)에 되돌아갑니다.</p>
      <label>겹친 관리번호 (넘겨받을 번호)
        <input type="text" id="nf-target" placeholder="예: 260701-0012" autocomplete="off"></label>
      <div id="nf-peek" class="muted" style="font-size:12px; min-height:18px; margin:2px 0 8px;"></div>
      <label>방식
        <select id="nf-mode">
          <option value="take">넘겨받기 — 상대는 임시 번호로 물러납니다</option>
          <option value="swap">맞바꾸기 — 두 번호를 통째로 맞바꿉니다</option>
        </select></label>
      <label id="nf-move-wrap">상대가 받을 번호 <span class="muted">(비우면 자동: 겹친번호-오류1)</span>
        <input type="text" id="nf-moveto" placeholder="비워 두셔도 됩니다" autocomplete="off"></label>
      <label>사유 <span class="muted">(기록에 남습니다)</span>
        <input type="text" id="nf-reason" placeholder="예: TMS에서 다른 기계 번호로 판매 처리됨"></label>
      <div class="editor-actions">
        <button class="btn" id="nf-cancel">취소</button>
        <button class="btn btn-primary" id="nf-go">바로잡기</button>
      </div>
    </div>`;
  openModalWith(host);
  const modeSel = $("#nf-mode", host);
  const syncMode = () => {
    $("#nf-move-wrap", host).style.display = modeSel.value === "take" ? "" : "none";
  };
  modeSel.addEventListener("change", syncMode);
  syncMode();

  // 겹친 번호의 주인이 누구인지 미리 보여 준다 — 엉뚱한 자산을 밀어내지 않게
  let peekTimer = null;
  $("#nf-target", host).addEventListener("input", () => {
    clearTimeout(peekTimer);
    peekTimer = setTimeout(async () => {
      const no = $("#nf-target", host).value.trim();
      const box = $("#nf-peek", host);
      if (no.length < 2) { box.textContent = ""; return; }
      try {
        const rows = await api("/api/orders/asset-search?q=" + encodeURIComponent(no));
        const hit = rows.find((r) => r.assetNo.toUpperCase() === no.toUpperCase());
        box.textContent = hit
          ? `지금 이 번호를 쓰는 자산: ${[hit.maker, hit.model].filter(Boolean).join(" ")} `
            + `· ${hit.statusLabel}${hit.spec ? " · " + hit.spec : ""}`
          : "이 번호를 쓰는 자산이 없습니다 — 그냥 넘겨받으면 됩니다.";
      } catch { box.textContent = ""; }
    }, 250);
  });

  $("#nf-cancel", host).addEventListener("click", () => closeModal());
  $("#nf-go", host).addEventListener("click", async () => {
    const targetNo = $("#nf-target", host).value.trim();
    const reason = $("#nf-reason", host).value.trim();
    if (!targetNo) { toast("겹친 관리번호를 적어 주세요.", true); return; }
    if (!reason) { toast("사유를 적어 주세요 — 나중에 이 기록만 남습니다.", true); return; }
    const btn = $("#nf-go", host);
    btn.disabled = true;
    try {
      const r = await api("/api/assets/number-fix", {
        method: "POST",
        body: { assetId, targetNo, mode: modeSel.value, reason,
                moveToNo: $("#nf-moveto", host).value.trim() },
      });
      toast(r.message);
      closeModal();
      if (onDone) onDone();
    } catch (err) { toast(err.message, true); btn.disabled = false; }
  });
}


async function openRepairPanel(aid, onDone, keep) {
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
      <label>교체한 부품<input type="text" id="cr-parts" placeholder="예: 액정, 키보드 (단가표 부품은 아래에서 고르세요)"></label>
      <label style="grid-column:1 / -1;">수리 내용
        <input type="text" id="cr-desc" placeholder="예: 액정 교체, 시트지 재작업"></label>
      <label>수리비 총액 <span class="muted">(부가세 포함)</span>
        <input type="text" id="cr-cost" data-money value="0"></label>

    </div>
    <div class="inline-row" style="margin-top:6px; flex-wrap:wrap;">
      <span style="font-size:13px;"
        title="시트지·배터리·RAM·SSD처럼 단가가 정해진 것은 여기서 고르세요 — 금액을 손으로 안 적어도 오늘 단가가 들어갑니다.">
        🔩 단가표에서 고르기</span>
      <select id="cr-part"><option value="">부품 선택…</option></select>
      <button class="btn btn-sm" id="cr-part-add"
        title="고른 부품을 이 자산에 장착 기록으로 넣습니다(오늘 단가·스펙 칸 갱신). 아래 수리비와는 별개로 기록됩니다.">부품 장착</button>
      <span class="muted" style="font-size:12px;">단가는 기준정보 ▸ 🔩 부품 단가표에서 관리</span>
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
  // 부품 장착으로 패널을 다시 그렸다면 적고 있던 값을 되돌려 놓는다
  if (keep) {
    if (keep.date) $("#cr-date").value = keep.date;
    $("#cr-desc").value = keep.desc || "";
    $("#cr-parts").value = keep.parts || "";
    $("#cr-cost").value = keep.cost || "0";
  }
  $("#cr-cost").addEventListener("input", showVat);
  showVat();
  $("#cr-close").addEventListener("click", () => { host.innerHTML = ""; });
  // 🔩 단가표 부품 — 시트지·배터리도 여기서(대표 2026-08-14: 단가표로 통합).
  //    금액은 서버가 단가표에서 읽는다(화면이 보낸 값 불신 — 출처 단일화).
  const crPart = $("#cr-part");
  if (crPart) {
    api("/api/parts").then((parts) => fillPartSelect(crPart, parts, true)).catch(() => {});
    const addBtn = $("#cr-part-add");
    addBtn.addEventListener("click", async () => {
      const pid = Number(crPart.value);
      if (!pid) { toast("부품을 고르세요.", true); return; }
      addBtn.disabled = true;                        // ★연타 방지 — 두 번 들어가면 원가가 두 배
      // 적고 있던 내용을 잃지 않게 들고 간다(패널을 다시 그리므로)
      const keep = {
        desc: $("#cr-desc").value, parts: $("#cr-parts").value,
        cost: $("#cr-cost").value, date: $("#cr-date").value,
      };
      try {
        const r = await api(`/api/assets/${aid}/repairs`, { method: "POST", body: {
          partIds: [{ id: pid }], repairDate: $("#cr-date").value } });
        toast(`부품 장착 — ${fmtWon(r.cost)}이 원가에 더해졌습니다.`);
        if (onDone) onDone();                        // 뒤 목록의 수리비·원가도 새로 읽는다
        openRepairPanel(aid, onDone, keep);          // 이력·합계를 새로 그린다
      } catch (err) { toast(err.message, true); addBtn.disabled = false; }
    });
  }
  $("#cr-save").addEventListener("click", async () => {
    const btn = $("#cr-save");
    const desc = $("#cr-desc").value.trim();
    const parts = $("#cr-parts").value.trim();
    const tier = "";   // 재고 구분은 화면에서 없앴다 — 상태가 그 일을 한다(2026-08-24)
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
/* 유사 제품코드 감지·병합(2026-08-09 대표 승인) — 띄어쓰기·대소문자 오타가
   같은 상품 재고를 두 코드로 쪼개는 것을 캠페인 전에 잡는다. */
async function drawCodeVariants() {
  const host = $("#uc-variants");
  if (!host) return;
  let d;
  try { d = await api("/api/assets/code-variants"); } catch { return; }
  if (!$("#uc-variants") || !d.count) return;
  host.innerHTML = `
    <div class="card" style="border-left:3px solid var(--warning, #d97706);">
      <h3>⚠ 비슷한 제품코드 ${d.count}묶음
        <span class="muted" style="font-size:13px; font-weight:400;">
          — 띄어쓰기·대소문자만 다른 코드는 같은 상품 재고를 둘로 쪼갭니다</span></h3>
      <div class="table-wrap"><table>
        <thead><tr><th>대표 코드(가장 많이 쓰인 것)</th><th>다른 표기</th><th></th></tr></thead>
        <tbody>${d.groups.slice(0, 20).map((g, i) => `
          <tr>
            <td><b>${escapeHtml(g.canonical)}</b>
              <span class="muted">(${g.variants[0].count}대)</span></td>
            <td>${g.variants.slice(1).map((v) =>
              `<span class="chip chip-amber">${escapeHtml(v.code)} · ${v.count}대</span>`).join(" ")}</td>
            <td><button class="btn btn-sm" data-mergecode="${i}"
              title="다른 표기 자산을 전부 대표 코드로 바꿉니다">대표 코드로 병합</button></td>
          </tr>`).join("")}
        </tbody></table></div>
      ${d.count > 20 ? `<p class="muted">외 ${d.count - 20}묶음 — 병합할수록 줄어듭니다.</p>` : ""}
    </div>`;
  $$("button[data-mergecode]", host).forEach((b) => b.addEventListener("click", async () => {
    const g = d.groups[Number(b.dataset.mergecode)];
    b.disabled = true;
    try {
      let n = 0;
      for (const v of g.variants.slice(1)) {
        // 서버는 한 번에 500대까지 받는다 — 큰 묶음은 쪼개 보낸다
        for (let i = 0; i < v.ids.length; i += 500) {
          const r = await api("/api/assets/product-code", {
            method: "POST", body: { ids: v.ids.slice(i, i + 500), productCode: g.canonical } });
          n += r.ok || 0;
        }
      }
      toast(`${n}대를 ${g.canonical}(으)로 병합했습니다.`);
      drawCodeVariants();
    } catch (err) { toast(err.message, true); b.disabled = false; }
  }));
}

/* 재고 숫자 정합 점검(2026-08-09) — 몰에 보내는 숫자의 모순을 한 줄로 보여 준다. */
async function drawIntegrityChips() {
  const host = $("#uc-integrity");
  if (!host) return;
  let d;
  try { d = await api("/api/assets/integrity"); } catch { return; }
  if (!$("#uc-integrity")) return;
  const chips = [];
  // 몰 재고 전송을 안 쓰면(전 몰 꺼짐) 그 축의 경고는 뜻이 없다 — 화면에서 뺀다
  if (stockSyncOn() && d.ghostListed) chips.push(`<span class="chip chip-red"
    title="판매 불가능한 상태인데 몰 재고 전송이 켜져 있음 — 자동 해제가 도는데도 남아 있으면 알려 주세요">
    ⚠ 유령 전송 ${d.ghostListed}대</span>`);
  if (stockSyncOn() && d.listedNoCode) chips.push(`<span class="chip chip-red"
    title="몰 재고 전송이 켜져 있는데 제품코드가 없음 — 몰 집계에서 조용히 빠집니다">
    ⚠ 코드 없이 전송 ${d.listedNoCode}대</span>`);
  if (d.unreceivedReady) chips.push(`<span class="chip chip-amber"
    title="입고 확인 전인데 판매가능 상태 — 실물 확인이 필요합니다">
    미입고인데 판매가능 ${d.unreceivedReady}대</span>`);
  if (stockSyncOn() && d.codedButOff) chips.push(`<span class="chip chip-slate"
    title="판매가능 + 코드 있음 + 몰 재고 전송 꺼짐 — 검수가 끝났다면 올릴 수 있는 재고입니다">
    전송 대기 ${d.codedButOff.toLocaleString()}대</span>`);
  if (d.shippedUnmatched) chips.push(`<span class="chip chip-amber"
    title="자산 매칭 없이 출고 확인된 주문 — 어느 기계가 나갔는지 이력이 없습니다(백필 대상)">
    미매칭 출고 ${d.shippedUnmatched.toLocaleString()}건</span>`);
  host.innerHTML = chips.length
    ? `<span class="muted" style="font-size:12px;">정합 점검:</span> ` + chips.join(" ")
    : "";
}


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
      <p>판매불가 자산도, 제품코드가 필요한 자산도 없습니다.</p>
      <p class="muted">출고완료·폐기·매입취소·거래처반품은 대상에서 제외됩니다.</p></div>`;
    return;
  }
  // ★두 숫자를 반드시 갈라 보여 준다 — '판매불가'와 '코드만 아직 없음'은 다른 얘기다.
  //   합쳐 세면 멀쩡한 재고 1,900여 대가 판매불가로 읽힌다(2026-08-07 검증 지적).
  const flt = state.ucFilter || "";
  const fchip = (key, label, n, cls) => !n ? "" :
    `<button class="chip ${flt === key ? "chip-blue" : cls} uc-flt" data-flt="${key}"
       style="cursor:pointer; border:none;">${label} ${n.toLocaleString()}</button>`;
  body.innerHTML = `
    <div class="card">
      <h3>🚫 판매불가 ${(d.unsellable || 0).toLocaleString()}대 ·
          🏷 제품코드 미입력 ${(d.noCode || 0).toLocaleString()}대
          <span class="muted" style="font-size:13px; font-weight:400;">(모델 ${d.models}종)</span></h3>
      <p class="muted">판매불가(수리·A/S·불량·도색대기·가재고·미입고)는 여기서 상태를 바꿔 되살립니다.
        제품코드를 넣어야 셋팅/QC 화면에서 재고로 잡힙니다 — 같은 모델은 모델을 열어 한 번에.
        <b>${escapeHtml((d.excluded || []).join(" · "))}</b> 상태는 대상에서 빠집니다.</p>
      ${tierHelpBox()}
      <div class="inline-row" style="flex-wrap:wrap;">
        <input type="text" id="uc-find" placeholder="모델 검색" style="min-width:200px;">
        ${fchip("", "전체", d.total, "chip-slate")}
        ${fchip("unsell", "판매불가만", d.unsellable || 0, "chip-red")}
        ${fchip("nocode", "코드 없음만", d.noCode || 0, "chip-amber")}
        <span class="muted" id="uc-picked">선택 0대</span>
      </div>
      <div id="uc-integrity" class="inline-row" style="flex-wrap:wrap; margin-top:8px;"></div>
    </div>
    <div id="uc-variants"></div>
    <div id="uc-groups"></div>`;
  drawCodeVariants();                     // 유사 코드 묶음 — 늦게 와도 그때 그린다
  drawIntegrityChips();

  const match = (a) => flt === "unsell" ? (a.reasons || []).length > 0
    : flt === "nocode" ? !(a.productCode || "").trim()
    : true;

  const draw = () => {
    const kw = ($("#uc-find").value || "").trim().toUpperCase();
    const gs = d.groups
      .map((g) => ({ ...g, shown: g.assets.filter(match) }))
      .filter((g) => g.shown.length && (!kw || g.model.toUpperCase().includes(kw)));
    $("#uc-groups").innerHTML = gs.map((g) => {
      const open = !!state.uncodedOpen[g.model];
      const chips = (obj, cls) => Object.entries(obj)
        .map(([k, n]) => `<span class="chip ${cls}">${escapeHtml(k)} ${n}</span>`).join(" ");
      const pgU = paged(g.shown, "uc:" + g.model);
      return `<div class="card uc-group" data-model="${escapeHtml(g.model)}">
        <div class="inline-row uc-head" style="cursor:pointer;">
          <b style="font-size:16px;">${escapeHtml(g.model)}</b>
          <span class="chip chip-blue">${g.shown.length}대</span>
          ${g.unsellable ? `<span class="chip chip-red">판매불가 ${g.unsellable}</span>` : ""}
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
          <select class="uc-status" title="판매불가 자산을 되살리거나(입고·판매가능) 사유를 바꿉니다">
            <option value="">상태 그대로</option>
            ${(state.meta.manualStatuses || []).map((s) =>
              `<option value="${s}">${escapeHtml(statusLabel(s))}</option>`).join("")}</select>
          <button class="btn btn-sm btn-primary uc-apply">선택분에 적용</button>
          <span style="flex:1"></span>
          <label class="check-line" style="font-size:13px;">
            <input type="checkbox" class="uc-all"> 이 모델 전체</label>
        </div>
        <div class="table-wrap"><table><thead><tr>
          <th style="width:34px;"></th><th>관리번호</th><th>시리얼</th><th>등급</th>
          <th>재고 구분</th><th>상태</th><th>사유</th><th>스펙</th></tr></thead>
          <tbody>${pgU.rows.map((a) => `<tr>
            <td><input type="checkbox" class="uc-pick" data-aid="${a.id}"></td>
            <td><button class="link-btn" data-ucopen="${a.id}">${escapeHtml(a.assetNo)}</button></td>
            <td class="muted">${escapeHtml(a.serial || "-")}</td>
            <td>${escapeHtml(a.grade)}</td>
            <td>${escapeHtml(a.tier || "가용")}</td>
            <td>${escapeHtml(a.statusLabel)}</td>
            <td>${(a.reasons || []).map((x) =>
              `<span class="chip chip-red" style="font-size:11px;">${escapeHtml(x)}</span>`).join(" ")
              || `<span class="chip chip-amber" style="font-size:11px;">코드 없음</span>`}</td>
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
        attachCodeLookup(codeIn.id, null, { mall: true });
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
  $$(".uc-flt", body).forEach((b) => b.addEventListener("click", () => {
    state.ucFilter = b.dataset.flt;
    renderUncoded(body);
  }));
  draw();
}

async function applyUncoded(card, btn) {
  const ids = $$("input.uc-pick", card).filter((c) => c.checked).map((c) => Number(c.dataset.aid));
  if (!ids.length) { toast("자산을 먼저 선택하세요.", true); return; }
  const pc = $(".uc-code", card).value.trim();
  const grade = $(".uc-grade", card).value;
  const tier = "";        // 재고 구분은 화면에서 없앴다(2026-08-24) — 서버 계약은 그대로 둔다
  const status = $(".uc-status", card) ? $(".uc-status", card).value : "";
  if (!pc && !grade && !status) {
    toast("제품코드·등급·상태 중 하나 이상을 입력하세요.", true); return;
  }
  const what = [pc && `제품코드 ${pc}`, grade && `등급 ${grade}`,
                status && `상태 ${statusLabel(status)}`]
    .filter(Boolean).join(" · ");
  if (!confirm(`선택한 ${ids.length}대에 적용합니다.

  ${what}

계속할까요?`)) return;
  btn.disabled = true;
  try {
    let ok = ids.length;
    const skipped = [];
    // 상태는 bulk 라우트가 맡는다(매칭·출고된 자산은 그쪽 규칙대로 거른다)
    if (status) {
      const rb = await api("/api/assets/bulk", { method: "POST", body: { ids, status } });
      (rb.failed || []).forEach((x) => skipped.push(x.reason ? `${x.label || x.id}: ${x.reason}` : x));
      ok = Math.min(ok, rb.ok);
    }
    if (pc || grade || tier) {
      const payload = { ids };
      if (pc) payload.productCode = pc;
      if (grade) payload.grade = grade;
      if (tier) payload.tier = tier;
      const r = await api("/api/assets/product-code", { method: "POST", body: payload });
      (r.skipped || []).forEach((x) => skipped.push(typeof x === "string" ? x : JSON.stringify(x)));
      ok = Math.min(ok, r.ok);
    }
    toast(`${ok}대에 적용했습니다.` + (skipped.length ? ` (건너뜀/실패 ${skipped.length}대)` : ""));
    // ★#tab-body는 설정 화면의 id다. 매입은 #aview-body(자산 탭 안)라, 예전 코드는
    //   fallback(card.parentElement=#uc-groups)으로 떨어져 제목 카드가 안 바뀌었다.
    //   그래서 코드를 채워도 '남은 대수'가 그대로여서 진척이 안 보였다(2026-08-04 감사).
    renderUncoded($("#aview-body") || $("#ptab-body") || card.parentElement);
    fillAssetBadges($("#ptab-body") || document);   // 배지도 같이 줄여 준다
  } catch (err) { toast(err.message, true); btn.disabled = false; }
}

/* ---------------- 거래처 (마스터, 2026-09-02) ----------------
   TMS MST거래처의 업무 거래처(매입·판매·AS업체)가 연동 창구로 들어오고, 여기서 직접 등록·수정도 한다.
   ★같은 이름의 TMS 신원이 둘 이상이면 자동으로 합치지 않는다 — '🔗 TMS 신원 N' 칩에서 사람이 대표를
     고르거나 별도 거래처로 분리한다. 표기가 다른 같은 업체(⚠ 이름 중복 후보)는 [대표로]로 묶는다(alias_of).
   서버: app/purchase/masters.py (list/create/update_supplier, canonical, tms-primary, tms-split). */
const SUPPLIER_KINDS = ["매입", "판매", "AS업체"];

function supplierKindChip(k) {
  const cls = k === "판매" ? "chip-blue" : k === "AS업체" ? "chip-violet" : "chip-slate";
  return `<span class="chip ${cls}" style="font-size:11px;">${escapeHtml(k)}</span>`;
}

function supplierSourceChip(s) {
  if (s.aliasOf) {
    return `<span class="chip chip-slate" style="font-size:11px;" title="별칭 — 조회에서는 대표 거래처로 표시됩니다">→ ${escapeHtml(s.aliasOfName || "대표")}</span>`;
  }
  const bits = [s.source === "tms"
    ? `<span class="chip chip-slate" style="font-size:11px;" title="TMS 연동 행 — OWS에서 고치면 연동이 그 값을 덮지 않습니다">TMS 연동${s.owsEditedAt ? " · OWS 수정" : ""}</span>`
    : `<span class="chip chip-teal" style="font-size:11px;" title="OWS에서 등록한 거래처${s.tmsKeyId ? " — 같은 이름의 TMS 거래처가 연결돼 빈 칸만 채워집니다" : ""}">OWS 등록${s.tmsKeyId ? " · TMS 연결" : ""}</span>`];
  if (s.tmsDeletedAt) bits.push(`<span class="chip chip-red" style="font-size:11px;" title="TMS 업무 거래처 목록에서 빠졌습니다(지우지 않음)">TMS 이탈</span>`);
  return bits.join(" ");
}

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
  const canEdit = hasPerm("purchase.edit");
  state.supplierFilter = state.supplierFilter || { q: "", kind: "", showAlias: true };
  const f = state.supplierFilter;
  const dupCount = suppliers.filter((s) => (s.dupIds || []).length && !s.aliasOf).length;
  const linkDupCount = suppliers.reduce((n, s) => n + (s.tmsLinks || []).filter((l) => !l.isPrimary).length, 0);
  const rowHtml = (s) => {
    const dupLinks = (s.tmsLinks || []).filter((l) => !l.isPrimary);
    return `
      <tr class="${s.enabled ? "" : "muted"}" style="${s.aliasOf ? "opacity:.75;" : ""}">
        <td><b>${escapeHtml(s.name)}</b>
          <div style="margin-top:2px; display:flex; gap:4px; flex-wrap:wrap; align-items:center;">
            ${(s.kinds || [s.kind]).map(supplierKindChip).join("")}
            ${supplierSourceChip(s)}
            ${!s.aliasOf && (s.dupIds || []).length ? `<span class="chip chip-amber" style="font-size:11px;"
              title="공백·대소문자만 다른 같은 구분의 거래처가 ${s.dupIds.length}곳 더 있습니다 — [대표로]로 묶으세요">⚠ 이름 중복 후보 ${s.dupIds.length}</span>` : ""}
            ${dupLinks.length ? `<button class="btn btn-ghost btn-sm" data-splinks="${s.id}" style="font-size:11px; padding:1px 6px;"
              title="같은 이름으로 TMS에 등록된 신원이 ${dupLinks.length + 1}개입니다 — 대표를 고르거나 분리합니다">🔗 TMS 신원 ${dupLinks.length + 1}</button>` : ""}
            ${s.aliasCount ? `<span class="chip chip-green" style="font-size:11px;" title="이 거래처를 대표로 둔 별칭 ${s.aliasCount}곳">대표 · 별칭 ${s.aliasCount}</span>` : ""}
          </div></td>
        <td class="muted" style="font-size:12px;">${escapeHtml(s.code || "")}</td>
        <td class="muted" style="font-size:12px;">${escapeHtml(s.bizNo || "")}</td>
        <td>${escapeHtml(s.contact || "")}${s.ceo ? `<div class="muted" style="font-size:11.5px;">대표 ${escapeHtml(s.ceo)}</div>` : ""}</td>
        <td>${escapeHtml(s.phone || "")}${s.email ? `<div class="muted" style="font-size:11.5px;">${escapeHtml(s.email)}</div>` : ""}</td>
        <td>${s.batchCount}회${s.aliasCount ? ` <span class="muted" style="font-size:11px;">(별칭 포함 ${s.groupBatchCount}회)</span>` : ""}</td>
        <td>${fmtWon(s.totalAmount)}</td>
        <td class="muted">${escapeHtml(s.memo || "")}</td>
        <td style="white-space:nowrap;">${canEdit ? `
          <button class="btn btn-sm" data-spedit="${s.id}">수정</button>
          ${!s.aliasOf && (s.dupIds || []).length ? `<button class="btn btn-sm" data-spcanon="${s.id}"
            title="중복 후보들을 이 거래처의 별칭으로 묶습니다(전표는 그대로, 표시만 대표로)">대표로</button>` : ""}
          ${s.aliasOf ? `<button class="btn btn-ghost btn-sm" data-spdetach="${s.id}" title="별칭을 풀어 독립 거래처로 되돌립니다">별칭 해제</button>` : ""}` : ""}</td>
      </tr>`;
  };
  const filtered = () => {
    const q = (f.q || "").trim().toLowerCase().replace(/\s+/g, "");
    return suppliers.filter((s) => {
      if (f.kind && !(s.kinds || [s.kind]).includes(f.kind)) return false;
      if (!f.showAlias && s.aliasOf) return false;
      if (!q) return true;
      return [s.name, s.code, s.bizNo, s.contact, s.ceo, s.phone]
        .some((v) => (v || "").toLowerCase().replace(/\s+/g, "").includes(q));
    });
  };
  body.innerHTML = `
    <div class="card">
      <h3>거래처 <span class="muted" style="font-size:12px; font-weight:400;">— ${suppliers.length}곳${
        dupCount ? ` · <span style="color:var(--danger);">이름 중복 후보 ${dupCount}</span>` : ""}${
        linkDupCount ? ` · TMS 신원 중복 ${linkDupCount}` : ""}</span></h3>
      <p class="muted" style="margin:0 0 8px;">TMS의 업무 거래처(매입·판매·AS업체)는 연동 창구로 들어오고, 여기서 직접 등록·수정도 합니다.
        OWS에서 고친 값은 연동이 덮지 않습니다. 같은 이름이 TMS에 둘 이상이면 합치지 않고 🔗 칩에서 사람이 정합니다.</p>
      <div class="inline-row" style="margin:0 0 8px;">
        <input type="text" id="sp-q" value="${escapeHtml(f.q || "")}" placeholder="🔍 이름·코드·사업자번호·연락처" style="min-width:220px;">
        <select id="sp-kind"><option value="">전체 구분</option>${SUPPLIER_KINDS.map((k) => `<option ${f.kind === k ? "selected" : ""}>${k}</option>`).join("")}</select>
        <label class="check-line"><input type="checkbox" id="sp-alias" ${f.showAlias ? "checked" : ""}> 별칭 행 보기</label>
        <span class="muted" style="font-size:12px;" id="sp-count"></span>
      </div>
      ${canEdit ? `<div class="inline-row" style="flex-wrap:wrap;">
        <input type="text" id="sp-name" placeholder="거래처명 *" style="min-width:160px;">
        <select id="sp-newkind" title="거래처 구분">${SUPPLIER_KINDS.map((k) => `<option>${k}</option>`).join("")}</select>
        <input type="text" id="sp-code" placeholder="거래처코드" style="width:110px;">
        <input type="text" id="sp-bizno" placeholder="사업자번호" style="width:120px;">
        <input type="text" id="sp-contact" placeholder="담당자" style="width:100px;">
        <input type="text" id="sp-phone" placeholder="연락처" style="width:130px;">
        <input type="text" id="sp-memo" placeholder="메모" style="flex:1; min-width:120px;">
        <button class="btn btn-sm btn-primary" id="sp-add">추가</button>
      </div>` : ""}
      <div class="table-wrap" id="sp-list"></div>
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
  // ── 목록만 다시 그린다(검색 칸은 그대로 — 글자마다 다시 그리면 한글 조합이 끊긴다)
  const drawList = () => {
    const list = filtered();
    $("#sp-count", body).textContent = list.length === suppliers.length ? "" : `${list.length}곳 표시`;
    $("#sp-list", body).innerHTML = `<table>
      <thead><tr><th>거래처</th><th>코드</th><th>사업자번호</th><th>담당자</th><th>연락처</th>
        <th>매입 횟수</th><th>누적 매입금액</th><th>메모</th><th></th></tr></thead>
      <tbody>${list.map(rowHtml).join("") || `<tr><td colspan="9" class="muted">${
        suppliers.length ? "검색과 일치하는 거래처가 없습니다." : "거래처가 없습니다."}</td></tr>`}</tbody></table>`;
    const byId = (b, key) => suppliers.find((x) => x.id === Number(b.dataset[key]));
    $$("button[data-spedit]", body).forEach((b) => b.addEventListener("click", () =>
      openSupplierEditor(byId(b, "spedit"), () => renderSuppliers(body))));
    $$("button[data-splinks]", body).forEach((b) => b.addEventListener("click", () =>
      openSupplierTmsLinks(byId(b, "splinks"), () => renderSuppliers(body))));
    $$("button[data-spcanon]", body).forEach((b) => b.addEventListener("click", async () => {
      const s = byId(b, "spcanon");
      const others = (s.dupIds || []).map((id) => (suppliers.find((x) => x.id === id) || {}).name).filter(Boolean);
      if (!confirm(`'${s.name}'을(를) 대표로 두고 아래를 별칭으로 묶습니다.\n\n${others.join("\n")}\n\n전표·자산의 거래처는 그대로이고 조회에서 대표로 표시됩니다. 진행할까요?`)) return;
      try {
        await api(`/api/suppliers/${s.id}/canonical`, { method: "POST", body: { aliasIds: s.dupIds } });
        toast("대표 거래처로 묶었습니다.");
        renderSuppliers(body);
      } catch (err) { toast(err.message, true); }
    }));
    $$("button[data-spdetach]", body).forEach((b) => b.addEventListener("click", async () => {
      const s = byId(b, "spdetach");
      try {
        await api(`/api/suppliers/${s.id}`, { method: "PATCH", body: { aliasOf: null } });
        toast("별칭을 풀었습니다.");
        renderSuppliers(body);
      } catch (err) { toast(err.message, true); }
    }));
  };
  drawList();
  $("#sp-q", body).addEventListener("input", () => { f.q = $("#sp-q", body).value; drawList(); });
  $("#sp-kind", body).addEventListener("change", () => { f.kind = $("#sp-kind", body).value; drawList(); });
  $("#sp-alias", body).addEventListener("change", () => { f.showAlias = $("#sp-alias", body).checked; drawList(); });
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
  const addBtn = $("#sp-add", body);
  if (addBtn) addBtn.addEventListener("click", async () => {
    addBtn.disabled = true;
    try {
      await api("/api/suppliers", { method: "POST", body: {
        name: $("#sp-name").value.trim(), kind: $("#sp-newkind").value, code: $("#sp-code").value,
        bizNo: $("#sp-bizno").value, contact: $("#sp-contact").value, phone: $("#sp-phone").value,
        memo: $("#sp-memo").value,
      } });
      toast("거래처를 추가했습니다.");
      renderSuppliers(body);
    } catch (err) { toast(err.message, true); addBtn.disabled = false; }
  });
}

/* 거래처 수정 팝업 — 구분·코드·사업자번호·연락처·주소·사용 여부. 서버가 바뀐 칸만 저장하고 ows_edited 표시를 남긴다. */
function openSupplierEditor(s, onDone) {
  const host = document.createElement("div");
  const v = (k) => escapeHtml(s[k] == null ? "" : s[k]);
  host.innerHTML = `
    <div class="card in-modal">
      <div class="inline-row" style="margin-bottom:6px;">
        <h3 style="margin:0; flex:1;">거래처 수정 — ${escapeHtml(s.name)}</h3>
        ${supplierSourceChip(s)}
        <button class="btn btn-sm" id="spe-close">닫기</button>
      </div>
      ${s.source === "tms" && !s.owsEditedAt ? `<p class="muted" style="font-size:12px; margin:0 0 8px;">
        TMS 연동 행입니다. 여기서 저장하면 이 거래처는 'OWS 수정'으로 표시되고 그 뒤로는 연동이 값을 덮지 않습니다.</p>` : ""}
      <div class="form-grid">
        <label>거래처명 *<input type="text" id="spe-name" value="${v("name")}"></label>
        <label>구분<select id="spe-kind">${SUPPLIER_KINDS.map((k) => `<option ${s.kind === k ? "selected" : ""}>${k}</option>`).join("")}</select></label>
        <label>거래처코드<input type="text" id="spe-code" value="${v("code")}"></label>
        <label>사업자번호<input type="text" id="spe-bizno" value="${v("bizNo")}"></label>
        <label>대표<input type="text" id="spe-ceo" value="${v("ceo")}"></label>
        <label>담당자<input type="text" id="spe-contact" value="${v("contact")}"></label>
        <label>연락처<input type="text" id="spe-phone" value="${v("phone")}"></label>
        <label>FAX<input type="text" id="spe-fax" value="${v("fax")}"></label>
        <label>이메일<input type="text" id="spe-email" value="${v("email")}"></label>
        <label>부서<input type="text" id="spe-dept" value="${v("dept")}"></label>
        <label style="grid-column:1 / -1;">주소<input type="text" id="spe-address" value="${v("address")}"></label>
        <label>상세주소<input type="text" id="spe-address2" value="${v("addressDetail")}"></label>
        <label>우편번호<input type="text" id="spe-zip" value="${v("zip")}"></label>
        <label style="grid-column:1 / -1;">메모<input type="text" id="spe-memo" value="${v("memo")}"></label>
        <label class="check-line" style="align-self:end;"><input type="checkbox" id="spe-enabled" ${s.enabled ? "checked" : ""}> 사용 중
          <span class="muted" style="font-size:12px;">(끄면 자동완성에서 빠집니다 — 기록은 유지)</span></label>
      </div>
      <div class="editor-actions">
        <button class="btn btn-primary" id="spe-save">저장</button>
        <button class="btn" id="spe-cancel">취소</button>
      </div>
    </div>`;
  openModalWith(host);
  $("#spe-close", host).addEventListener("click", () => closeModal());
  $("#spe-cancel", host).addEventListener("click", () => closeModal());
  $("#spe-save", host).addEventListener("click", async () => {
    const btn = $("#spe-save", host);
    btn.disabled = true;
    const val = (id) => $(id, host).value.trim();
    try {
      await api(`/api/suppliers/${s.id}`, { method: "PATCH", body: {
        name: val("#spe-name"), kind: $("#spe-kind", host).value, code: val("#spe-code"), bizNo: val("#spe-bizno"),
        ceo: val("#spe-ceo"), contact: val("#spe-contact"), phone: val("#spe-phone"), fax: val("#spe-fax"),
        email: val("#spe-email"), dept: val("#spe-dept"), address: val("#spe-address"),
        addressDetail: val("#spe-address2"), zip: val("#spe-zip"), memo: val("#spe-memo"),
        enabled: $("#spe-enabled", host).checked,
      } });
      toast("저장했습니다.");
      closeModal();
      if (onDone) onDone();
    } catch (err) {
      // '변경할 항목이 없습니다'는 실패가 아니다 — 그냥 닫는다
      if (/변경할 항목/.test(err.message)) { closeModal(); return; }
      toast(err.message, true); btn.disabled = false;
    }
  });
}

/* 같은 이름으로 TMS에 둘 이상 등록된 신원 — 대표를 고르거나(값은 안 바뀜) 사람이 이름을 정해 별도 거래처로 분리한다. */
function openSupplierTmsLinks(s, onDone) {
  const host = document.createElement("div");
  const links = s.tmsLinks || [];
  const p = (l, k) => escapeHtml((l.payload || {})[k] || "");
  host.innerHTML = `
    <div class="card in-modal">
      <div class="inline-row" style="margin-bottom:6px;">
        <h3 style="margin:0; flex:1;">🔗 TMS 신원 — ${escapeHtml(s.name)}</h3>
        <button class="btn btn-sm" id="spl-close">닫기</button>
      </div>
      <p class="muted" style="font-size:12.5px; margin:0 0 8px;">TMS 거래처 마스터에 같은 이름이 여러 줄 있습니다. OWS는 한 이름에 한 거래처만 두므로
        자동으로 합치지 않고 여기에 남겨 두었습니다. <b>대표</b>는 이 거래처 칸에 값을 채우는 신원이고(연동이 따라가는 쪽), 나머지는 기록으로만 보입니다.
        다른 업체라면 [별도 거래처로 분리]에서 구분되는 이름을 직접 정해 주세요(시스템이 이름을 지어내지 않습니다).</p>
      <div class="table-wrap"><table>
        <thead><tr><th>키ID</th><th>구분</th><th>코드</th><th>대표자</th><th>전화</th><th>주소</th><th>상태</th><th></th></tr></thead>
        <tbody>${links.map((l) => `
          <tr>
            <td>${l.keyId}${l.isPrimary ? ' <span class="chip chip-green" style="font-size:11px;">대표</span>' : ""}</td>
            <td>${supplierKindChip(l.kind || "-")}</td>
            <td class="muted">${escapeHtml(l.code || "")}</td>
            <td>${p(l, "대표")}</td>
            <td>${p(l, "전화번호")}</td>
            <td class="muted" style="font-size:12px;">${p(l, "주소")} ${p(l, "상세주소")}</td>
            <td>${l.deletedAt ? '<span class="chip chip-red" style="font-size:11px;">TMS 이탈</span>' : '<span class="chip chip-slate" style="font-size:11px;">TMS 목록에 있음</span>'}</td>
            <td style="white-space:nowrap;">${hasPerm("purchase.edit") && !l.isPrimary ? `
              <button class="btn btn-sm" data-primary="${l.keyId}" title="이 신원을 대표로 — 이 신원의 값이 거래처 칸에 반영됩니다(OWS에서 고친 칸은 유지)">대표로</button>
              <button class="btn btn-ghost btn-sm" data-split="${l.keyId}" title="이 신원으로 별도 거래처를 만듭니다(이름을 직접 입력)">별도 거래처로 분리</button>` : ""}</td>
          </tr>`).join("")}</tbody></table></div>
    </div>`;
  openModalWith(host);
  $("#spl-close", host).addEventListener("click", () => closeModal());
  $$("button[data-primary]", host).forEach((b) => b.addEventListener("click", async () => {
    try {
      await api(`/api/suppliers/${s.id}/tms-primary`, { method: "POST", body: { keyId: Number(b.dataset.primary) } });
      toast("대표 신원을 바꿨습니다.");
      closeModal();
      if (onDone) onDone();
    } catch (err) { toast(err.message, true); }
  }));
  $$("button[data-split]", host).forEach((b) => b.addEventListener("click", async () => {
    const name = prompt("새 거래처 이름(기존과 구분되게)", "");
    if (name == null || !name.trim()) return;
    try {
      const r = await api(`/api/suppliers/${s.id}/tms-split`, { method: "POST", body: { keyId: Number(b.dataset.split), name: name.trim() } });
      toast(`별도 거래처로 분리했습니다: ${r.name}`);
      closeModal();
      if (onDone) onDone();
    } catch (err) { toast(err.message, true); }
  }));
}

/* ---------------- 모델 마스터 (2026-09-02) ----------------
   자산 모델명·브랜드·대분류/중분류의 기준표. 자산 등록 칸의 자동완성이 여기서 나오고, 마스터에 없는 모델명은
   등록을 막지 않되 ⚠ 표시가 뜬다. TMS 모델관리에 등록된 모델은 연동 창구로 들어오고, OWS에서 고친 행은 연동이 덮지 않는다.
   서버: app/purchase/masters.py (/api/models, /api/model-categories, /api/masters/status·sync). */
function modelSourceChip(m) {
  const bits = [m.source === "tms"
    ? `<span class="chip ${m.owsEditedAt ? "chip-amber" : "chip-slate"}" style="font-size:11px;"
        title="TMS 모델관리에서 온 행${m.owsEditedAt ? " — OWS에서 고쳐 연동이 덮지 않습니다" : ""}">TMS 연동${m.owsEditedAt ? " · OWS 수정" : ""}</span>`
    : `<span class="chip chip-teal" style="font-size:11px;" title="OWS에서 등록한 모델${m.tmsKeyId ? " — 같은 이름의 TMS 모델이 연결됨" : ""}">OWS 등록${m.tmsKeyId ? " · TMS 연결" : ""}</span>`];
  if (m.tmsDeletedAt) bits.push('<span class="chip chip-red" style="font-size:11px;" title="TMS에서 삭제됨(지우지 않고 표시만)">TMS 삭제</span>');
  if (!m.enabled) bits.push('<span class="chip chip-slate" style="font-size:11px;">사용 안 함</span>');
  return bits.join(" ");
}

async function renderModelMaster(body) {
  const seq = ++state.renderSeq;
  state.modelFilter = state.modelFilter || { q: "", category: "", all: false, missing: false };
  const f = state.modelFilter;
  let cats, status, miss;
  try {
    // miss = 마스터에 없는 모델 수(정비 캠페인 배지, 2026-09-03) — 실패해도 마스터 화면은 떠야 한다
    [cats, status, miss] = await Promise.all([api("/api/model-categories"), api("/api/masters/status"),
                                              api("/api/models/missing?limit=1").catch(() => ({ total: 0 }))]);
    if (seq !== state.renderSeq) return;
  } catch (err) {
    if (seq === state.renderSeq) body.innerHTML = `<p class="muted">${escapeHtml(err.message)}</p>`;
    return;
  }
  const canEdit = hasPerm("purchase.edit");
  const catNames = [...new Set(cats.items.map((c) => c.category))];
  const comboOptions = (sel) => `<option value="">분류 -</option>` + cats.items.map((c) => {
    const val = c.category + "|" + (c.subcategory || "");
    return `<option value="${escapeHtml(val)}" ${sel === val ? "selected" : ""}>${escapeHtml(c.category)} / ${escapeHtml(c.subcategory || "-")}</option>`;
  }).join("");
  const last = (status.last && status.last.models) || {};
  body.innerHTML = `
    <div class="card">
      <h3>💻 모델 마스터 <span class="muted" style="font-size:12px; font-weight:400;">— 전체 ${status.counts.models}종
        · TMS 연동 ${status.counts.modelsTms} · OWS 등록 ${status.counts.modelsOws}${
        status.cursor ? ` · 마지막 연동 ${escapeHtml(String(status.cursor).slice(0, 16))}` : ""}${
        last.error ? ` · <span style="color:var(--danger);">연동 오류: ${escapeHtml(last.error)}</span>` : ""}</span></h3>
      <p class="muted" style="margin:0 0 8px;">자산의 모델명·브랜드·분류 기준표입니다. 자산 등록 칸에서 모델명을 치면 여기 후보가 '마스터' 꼬리표로 뜨고,
        마스터에 있으면 브랜드·카테고리가 자동으로 채워집니다. 없는 모델명도 등록은 되지만 ⚠ 표시가 뜨니 여기(또는 그 자리의 [＋ 마스터에 등록])에 올려 두세요.
        TMS 모델관리에 등록된 모델은 연동 창구로 들어오며, OWS에서 고친 행은 연동이 덮지 않습니다.${
        status.linkEnabled ? "" : ' <span style="color:var(--danger);">연동 창구 설정이 없어 TMS 모델은 들어오지 않습니다(설정 ▸ 데이터 연동).</span>'}</p>
      <div class="inline-row" style="margin:0 0 8px;">
        <input type="text" id="mm-q" value="${escapeHtml(f.q || "")}" placeholder="🔍 모델명·브랜드·펫네임 (공백·대소문자 무시)" style="min-width:240px;">
        <select id="mm-cat"><option value="">전체 대분류</option>${catNames.map((c) => `<option ${f.category === c ? "selected" : ""}>${escapeHtml(c)}</option>`).join("")}</select>
        <label class="check-line"><input type="checkbox" id="mm-all" ${f.all ? "checked" : ""}> 사용 안 함·TMS 삭제 포함</label>
        <button class="btn btn-sm${f.missing ? " btn-primary" : ""}" id="mm-missing"
          title="살아 있는 자산에 적힌 모델명 가운데 마스터에 없는 것 — 행마다 [마스터에 등록], 여러 행을 체크해 한 번에 등록할 수도 있습니다. 다시 누르면 마스터 목록으로">⚠ 마스터에 없는 모델 ${miss.total || 0}</button>
        <span class="muted" style="font-size:12px;" id="mm-count"></span>
        ${canEdit && status.linkEnabled ? `<button class="btn btn-sm" id="mm-sync" title="연동 창구에서 모델·거래처 마스터를 지금 받아옵니다(평소엔 2분마다 자동)">↻ 지금 연동</button>` : ""}
      </div>
      ${canEdit ? `<div class="inline-row" style="flex-wrap:wrap;">
        <input type="text" id="mm-name" placeholder="모델명 *" style="min-width:160px;">
        <input type="text" id="mm-brand" placeholder="브랜드" style="width:120px;">
        <select id="mm-combo">${comboOptions("")}</select>
        <input type="text" id="mm-pet" placeholder="펫네임" style="width:120px;">
        <input type="text" id="mm-spec" placeholder="사양(예: I7-8,16G,512G)" style="width:170px;">
        <input type="text" id="mm-memo" placeholder="메모" style="flex:1; min-width:100px;">
        <button class="btn btn-sm btn-primary" id="mm-add">등록</button>
      </div>` : ""}
      <div class="table-wrap" id="mm-list"><p class="muted">불러오는 중…</p></div>
    </div>`;
  const brandEl = $("#mm-brand", body);
  if (brandEl) attachAutocomplete(brandEl, "maker");
  let loadSeq = 0;
  const load = async () => {
    if (f.missing) return loadMissing();
    const my = ++loadSeq;
    const qs = new URLSearchParams({ q: f.q || "", category: f.category || "", all: f.all ? "1" : "0",
                                     withCounts: "1", limit: "300" });
    let r;
    try { r = await api("/api/models?" + qs.toString()); } catch (err) { toast(err.message, true); return; }
    if (my !== loadSeq || seq !== state.renderSeq) return;
    $("#mm-count", body).textContent = r.total > r.count ? `${r.count}종 표시 / 전체 ${r.total}종` : `${r.count}종`;
    $("#mm-list", body).innerHTML = `<table>
      <thead><tr><th>모델명</th><th>브랜드</th><th>분류</th><th>펫네임 · 사양</th><th style="text-align:right;">자산</th>
        <th>출처</th><th>수정</th>${canEdit ? "<th></th>" : ""}</tr></thead>
      <tbody>${r.items.map((m) => `
        <tr style="${m.enabled && !m.tmsDeletedAt ? "" : "opacity:.55;"}">
          <td><b>${escapeHtml(m.name)}</b></td>
          <td>${escapeHtml(m.brand || "")}</td>
          <td class="muted">${escapeHtml([m.category, m.subcategory].filter(Boolean).join(" / "))}</td>
          <td class="muted" style="font-size:12px;">${escapeHtml([m.petName, m.spec].filter(Boolean).join(" · "))}${
            m.memo ? `<div style="font-size:11.5px;">📌 ${escapeHtml(m.memo)}</div>` : ""}</td>
          <td style="text-align:right;">${m.assetCount || 0}</td>
          <td>${modelSourceChip(m)}</td>
          <td class="muted" style="font-size:12px;">${escapeHtml((m.updatedAt || "").slice(0, 10))} ${escapeHtml(m.updatedBy || "")}</td>
          ${canEdit ? `<td style="white-space:nowrap;">
            <button class="btn btn-sm" data-mmedit="${m.id}">수정</button>
            <button class="btn btn-ghost btn-sm" data-mmtoggle="${m.id}" data-on="${m.enabled ? 1 : 0}"
              title="${m.enabled ? "끄면 자동완성·보완에서 빠집니다(기록 유지)" : "다시 켭니다"}">${m.enabled ? "끄기" : "켜기"}</button></td>` : ""}
        </tr>`).join("") || `<tr><td colspan="8" class="muted">${f.q || f.category ? "검색과 일치하는 모델이 없습니다." : "등록된 모델이 없습니다."}</td></tr>`}
      </tbody></table>`;
    $$("button[data-mmedit]", body).forEach((b) => b.addEventListener("click", () =>
      openModelEditor(r.items.find((x) => x.id === Number(b.dataset.mmedit)), cats.items, load)));
    $$("button[data-mmtoggle]", body).forEach((b) => b.addEventListener("click", async () => {
      try {
        await api(`/api/models/${b.dataset.mmtoggle}`, { method: "PATCH", body: { enabled: b.dataset.on !== "1" } });
        load();
      } catch (err) { toast(err.message, true); }
    }));
  };
  /* 마스터에 없는 모델(정비 캠페인, 2026-09-03) — GET /api/models/missing: 살아 있는 자산(매입취소·거래처반품 제외)의 모델명을
     공백·대소문자 무시로 묶어 마스터에 없는 것만. 행마다 [마스터에 등록], 체크 후 [선택 일괄 등록] — 둘 다 openModelRegister 로
     같은 팝업이고 등록은 기존 POST /api/models 를 행마다 부른다. 검색칸(mm-q)은 이 표에서도 이름·브랜드로 거른다. */
  const norm = (s) => String(s || "").replace(/\s+/g, "").toUpperCase();
  const loadMissing = async () => {
    const my = ++loadSeq;
    let r;
    try { r = await api("/api/models/missing?limit=1000"); } catch (err) { toast(err.message, true); return; }
    if (my !== loadSeq || seq !== state.renderSeq) return;
    const q = norm(f.q);
    const items = r.items.filter((it) => !q || norm(it.name).includes(q) || norm(it.brand).includes(q));
    const missBtn = $("#mm-missing", body);
    if (missBtn) missBtn.textContent = `⚠ 마스터에 없는 모델 ${r.total}`;
    $("#mm-count", body).textContent = `마스터에 없는 모델 ${r.total}종${items.length !== r.total ? ` (표시 ${items.length})` : ""}`
      + ` · 살아 있는 자산의 모델 ${r.liveModels}종`;
    $("#mm-list", body).innerHTML = `
      <div class="inline-row" style="margin:6px 0; flex-wrap:wrap;">
        ${canEdit ? `<label class="check-line"><input type="checkbox" id="mmx-all"> 전체 선택</label>
        <button class="btn btn-sm btn-primary" id="mmx-bulk" disabled>☑ 선택 일괄 등록</button>` : ""}
        <span class="muted" style="font-size:12px;">자산에 적힌 모델명(가장 많이 쓴 표기)·대수·브랜드 추정(그 자산들의 브랜드 최다)·최근 등록일.
          매입취소·거래처반품 자산은 뺐고 판매된 자산은 셉니다.</span>
      </div>
      <table>
        <thead><tr>${canEdit ? "<th></th>" : ""}<th>모델명</th><th style="text-align:right;">대수</th><th>브랜드 추정</th>
          <th>카테고리(최다)</th><th>최근 등록</th>${canEdit ? "<th></th>" : ""}</tr></thead>
        <tbody>${items.map((it, i) => `
          <tr>
            ${canEdit ? `<td><input type="checkbox" data-mmx="${i}"></td>` : ""}
            <td><b>${escapeHtml(it.name)}</b>${it.spellings > 1 ? ` <span class="chip chip-amber" style="font-size:11px;"
              title="공백·대소문자만 다른 표기가 ${it.spellings}가지 — 마스터에 올리면 한 모델로 대조됩니다">표기 ${it.spellings}</span>` : ""}</td>
            <td style="text-align:right;">${it.count}</td>
            <td>${escapeHtml(it.brand || "")}</td>
            <td class="muted">${escapeHtml(it.category || "")}</td>
            <td class="muted" style="font-size:12px;">${escapeHtml(it.lastAt || "")}</td>
            ${canEdit ? `<td style="white-space:nowrap;"><button class="btn btn-sm" data-mmreg="${i}">마스터에 등록</button></td>` : ""}
          </tr>`).join("") || `<tr><td colspan="7" class="muted">${r.total ? "검색과 일치하는 모델이 없습니다." : "살아 있는 자산의 모델명이 모두 마스터에 있습니다."}</td></tr>`}
        </tbody></table>`;
    const done = () => {
      loadMissing();
      api("/api/masters/status").then((st) => {           // 머리줄 숫자만 갱신
        const h = $("h3 .muted", body);
        if (h && seq === state.renderSeq) h.innerHTML = h.innerHTML.replace(/전체 \d+종/, `전체 ${st.counts.models}종`).replace(/OWS 등록 \d+/, `OWS 등록 ${st.counts.modelsOws}`);
      }).catch(() => {});
    };
    $$("button[data-mmreg]", body).forEach((b) => b.addEventListener("click", () =>
      openModelRegister([items[Number(b.dataset.mmreg)]], cats.items, done)));
    const bulk = $("#mmx-bulk", body);
    const boxes = $$("input[data-mmx]", body);
    const syncBulk = () => {
      const n = boxes.filter((x) => x.checked).length;
      if (bulk) { bulk.disabled = !n; bulk.textContent = n ? `☑ 선택 ${n}개 일괄 등록` : "☑ 선택 일괄 등록"; }
    };
    boxes.forEach((x) => x.addEventListener("change", syncBulk));
    const all = $("#mmx-all", body);
    if (all) all.addEventListener("change", () => { boxes.forEach((x) => { x.checked = all.checked; }); syncBulk(); });
    if (bulk) bulk.addEventListener("click", () =>
      openModelRegister(boxes.filter((x) => x.checked).map((x) => items[Number(x.dataset.mmx)]), cats.items, done));
  };
  load();
  let timer = null;
  $("#mm-missing", body).addEventListener("click", () => {
    f.missing = !f.missing;
    $("#mm-missing", body).classList.toggle("btn-primary", f.missing);
    load();
  });
  $("#mm-q", body).addEventListener("input", () => { f.q = $("#mm-q", body).value; clearTimeout(timer); timer = setTimeout(load, 200); });
  $("#mm-cat", body).addEventListener("change", () => { f.category = $("#mm-cat", body).value; load(); });
  $("#mm-all", body).addEventListener("change", () => { f.all = $("#mm-all", body).checked; load(); });
  const syncBtn = $("#mm-sync", body);
  if (syncBtn) syncBtn.addEventListener("click", async () => {
    syncBtn.disabled = true;
    try {
      const r = await api("/api/masters/sync", { method: "POST" });
      const m = (r.result || {}).models || {}, p = (r.result || {}).partners || {};
      toast(m.error || p.error ? `연동 오류: ${m.error || p.error}` :
        `모델 신규 ${m.created || 0}·갱신 ${m.updated || 0}·연결 ${m.attached || 0} / 거래처 신규 ${p.created || 0}·연결 ${p.attached || 0}·갱신 ${p.updated || 0}`,
        !!(m.error || p.error));
      renderModelMaster(body);
    } catch (err) { toast(err.message, true); syncBtn.disabled = false; }
  });
  const addBtn = $("#mm-add", body);
  if (addBtn) addBtn.addEventListener("click", async () => {
    addBtn.disabled = true;
    const combo = ($("#mm-combo", body).value || "").split("|");
    try {
      await api("/api/models", { method: "POST", body: {
        name: $("#mm-name", body).value.trim(), brand: $("#mm-brand", body).value.trim(),
        category: combo[0] || "", subcategory: combo[1] || "", petName: $("#mm-pet", body).value.trim(),
        spec: $("#mm-spec", body).value.trim(), memo: $("#mm-memo", body).value.trim(),
      } });
      toast("모델을 등록했습니다.");
      ["#mm-name", "#mm-pet", "#mm-spec", "#mm-memo"].forEach((id) => { $(id, body).value = ""; });
      load();
      api("/api/masters/status").then((st) => {           // 머리줄 숫자만 갱신
        const h = $("h3 .muted", body);
        if (h && seq === state.renderSeq) h.innerHTML = h.innerHTML.replace(/전체 \d+종/, `전체 ${st.counts.models}종`).replace(/OWS 등록 \d+/, `OWS 등록 ${st.counts.modelsOws}`);
      }).catch(() => {});
    } catch (err) { toast(err.message, true); }
    finally { addBtn.disabled = false; }
  });
}

function openModelEditor(m, catItems, onDone) {
  const host = document.createElement("div");
  const v = (k) => escapeHtml(m[k] == null ? "" : m[k]);
  const cur = m.category + "|" + (m.subcategory || "");
  const options = `<option value="">분류 -</option>` + catItems.map((c) => {
    const val = c.category + "|" + (c.subcategory || "");
    return `<option value="${escapeHtml(val)}" ${cur === val ? "selected" : ""}>${escapeHtml(c.category)} / ${escapeHtml(c.subcategory || "-")}</option>`;
  }).join("") + (catItems.some((c) => c.category + "|" + (c.subcategory || "") === cur) || !m.category
    ? "" : `<option value="${escapeHtml(cur)}" selected>${escapeHtml(m.category)} / ${escapeHtml(m.subcategory || "-")}</option>`);
  host.innerHTML = `
    <div class="card in-modal">
      <div class="inline-row" style="margin-bottom:6px;">
        <h3 style="margin:0; flex:1;">모델 수정 — ${escapeHtml(m.name)}</h3>
        ${modelSourceChip(m)}
        <button class="btn btn-sm" id="mme-close">닫기</button>
      </div>
      ${m.source === "tms" && !m.owsEditedAt ? `<p class="muted" style="font-size:12px; margin:0 0 8px;">
        TMS 연동 행입니다. 여기서 저장하면 'OWS 수정'으로 표시되고 그 뒤로는 연동이 이 행의 값을 덮지 않습니다(TMS 삭제 표시만 따라옵니다).</p>` : ""}
      <div class="form-grid">
        <label>모델명 *<input type="text" id="mme-name" value="${v("name")}"></label>
        <label>브랜드<input type="text" id="mme-brand" value="${v("brand")}"></label>
        <label>분류<select id="mme-combo">${options}</select></label>
        <label>펫네임<input type="text" id="mme-pet" value="${v("petName")}"></label>
        <label>사양<input type="text" id="mme-spec" value="${v("spec")}"></label>
        <label>메모<input type="text" id="mme-memo" value="${v("memo")}"></label>
        <label class="check-line" style="align-self:end;"><input type="checkbox" id="mme-enabled" ${m.enabled ? "checked" : ""}> 사용 중</label>
      </div>
      <div class="editor-actions">
        <button class="btn btn-primary" id="mme-save">저장</button>
        <button class="btn" id="mme-cancel">취소</button>
      </div>
    </div>`;
  openModalWith(host);
  attachAutocomplete($("#mme-brand", host), "maker");
  $("#mme-close", host).addEventListener("click", () => closeModal());
  $("#mme-cancel", host).addEventListener("click", () => closeModal());
  $("#mme-save", host).addEventListener("click", async () => {
    const btn = $("#mme-save", host);
    btn.disabled = true;
    const combo = ($("#mme-combo", host).value || "").split("|");
    try {
      await api(`/api/models/${m.id}`, { method: "PATCH", body: {
        name: $("#mme-name", host).value.trim(), brand: $("#mme-brand", host).value.trim(),
        category: combo[0] || "", subcategory: combo[1] || "", petName: $("#mme-pet", host).value.trim(),
        spec: $("#mme-spec", host).value.trim(), memo: $("#mme-memo", host).value.trim(),
        enabled: $("#mme-enabled", host).checked,
      } });
      toast("저장했습니다.");
      closeModal();
      if (onDone) onDone();
    } catch (err) {
      if (/변경할 항목/.test(err.message)) { closeModal(); return; }
      toast(err.message, true); btn.disabled = false;
    }
  });
}

/* 마스터에 없는 모델 → 등록 팝업(2026-09-03). rows = /api/models/missing 의 items(한 개든 여러 개든 같은 팝업).
   행마다 이름·브랜드(추정값 미리 채움)·분류(자산 카테고리 최다 → CATEGORY_TO_MODEL_CLASS 로 TMS 모델분류 모양)를 고쳐서
   [N개 등록] — 기존 POST /api/models 를 행마다 차례로 부른다(409 등 실패는 그 행에 사유를 남기고 계속, 성공한 행은 재시도에서 건너뜀). */
function openModelRegister(rows, catItems, onDone) {
  if (!rows || !rows.length) return;
  const host = document.createElement("div");
  const options = (sel) => `<option value="">분류 -</option>` + catItems.map((c) => {
    const val = c.category + "|" + (c.subcategory || "");
    return `<option value="${escapeHtml(val)}" ${sel === val ? "selected" : ""}>${escapeHtml(c.category)} / ${escapeHtml(c.subcategory || "-")}</option>`;
  }).join("");
  const guess = (it) => { const m = CATEGORY_TO_MODEL_CLASS[it.category]; return m ? m[0] + "|" + (m[1] || "") : ""; };
  host.innerHTML = `
    <div class="card in-modal" style="max-width:900px;">
      <div class="inline-row" style="margin-bottom:6px;">
        <h3 style="margin:0; flex:1;">모델 마스터에 등록 — ${rows.length}개</h3>
        <button class="btn btn-sm" id="mmr-close">닫기</button>
      </div>
      <p class="muted" style="font-size:12px; margin:0 0 8px;">브랜드는 그 자산들의 브랜드 가운데 가장 많은 값, 분류는 카테고리 최다값에서 추정했습니다 —
        틀리면 고치고 등록하세요. 모델명을 바꾸면 자산에 적힌 표기와 달라져 대조가 안 되니 그대로 두는 게 좋습니다.</p>
      <div class="table-wrap"><table>
        <thead><tr><th>모델명</th><th style="text-align:right;">대수</th><th>브랜드</th><th>분류</th><th>결과</th></tr></thead>
        <tbody>${rows.map((it, i) => `<tr data-mmr="${i}">
          <td><input type="text" data-mmr-name value="${escapeHtml(it.name)}" style="min-width:160px;"></td>
          <td style="text-align:right;">${it.count}</td>
          <td><input type="text" data-mmr-brand value="${escapeHtml(it.brand || "")}" style="width:120px;"></td>
          <td><select data-mmr-combo>${options(guess(it))}</select></td>
          <td class="muted" style="font-size:12px;" data-mmr-result></td>
        </tr>`).join("")}</tbody></table></div>
      <div class="editor-actions">
        <button class="btn btn-primary" id="mmr-save">${rows.length}개 등록</button>
        <button class="btn" id="mmr-cancel">취소</button>
      </div>
    </div>`;
  openModalWith(host);
  $$("input[data-mmr-brand]", host).forEach((el) => attachAutocomplete(el, "maker"));
  $("#mmr-close", host).addEventListener("click", () => closeModal());
  $("#mmr-cancel", host).addEventListener("click", () => closeModal());
  $("#mmr-save", host).addEventListener("click", async () => {
    const btn = $("#mmr-save", host);
    btn.disabled = true;
    let ok = 0, fail = 0;
    for (const tr of $$("tr[data-mmr]", host)) {
      if (tr.dataset.done === "1") continue;
      const res = $("[data-mmr-result]", tr);
      const combo = ($("select[data-mmr-combo]", tr).value || "").split("|");
      const body = { name: $("input[data-mmr-name]", tr).value.trim(), brand: $("input[data-mmr-brand]", tr).value.trim(),
                     category: combo[0] || "", subcategory: combo[1] || "" };
      try {
        await api("/api/models", { method: "POST", body });
        tr.dataset.done = "1";
        ok += 1;
        res.innerHTML = '<span class="chip chip-green" style="font-size:11px;">등록</span>';
      } catch (err) {
        fail += 1;
        res.innerHTML = `<span class="chip chip-red" style="font-size:11px;">실패</span> ${escapeHtml(err.message)}`;
      }
    }
    toast(`모델 마스터 등록 ${ok}개${fail ? ` · 실패 ${fail}개(표의 사유 확인)` : ""}`, !ok && !!fail);
    if (fail) { btn.disabled = false; btn.textContent = "실패한 행 다시 등록"; }
    else closeModal();
    if (ok && onDone) onDone();
  });
}

/* ---------------- TMS 이관 ---------------- */

/* TMS 자동 반영 현황 — 폴더에 엑셀이 들어오면 OWS가 알아서 넣는다.
   엑셀을 TMS에서 받아오는 일은 185의 tools/tms_export 가 2시간마다 대신한다.
   그쪽이 멈춰도 여기는 그대로 돌고, 손으로 파일을 넣어도 똑같이 동작한다. */
function exporterLine(x) {
  if (!x) return "";
  if (x.loginNeeded) {
    return `<div class="chip chip-red" style="margin-bottom:8px;">
      ⚠ TMS 로그인이 풀렸습니다 — 185 서버에서 <b>TMS-EXPORT.bat</b> → [2] 를 한 번 눌러 주세요</div>`;
  }
  if (!x.installed) {
    return `<div class="muted" style="font-size:12px;margin-bottom:8px;">
      TMS에서 받아오는 자동화는 아직 안 켰습니다 — 185 서버의 TMS-EXPORT.bat 참고</div>`;
  }
  const when = x.lastRun ? escapeHtml(String(x.lastRun).slice(0, 16).replace("T", " ")) : "아직 없음";
  const fail = (x.failed || []).length
    ? ` <span class="chip chip-red">실패 ${x.failed.length}: ${escapeHtml(x.failed.slice(0, 3).join(", "))}</span>` : "";
  return `<div class="muted" style="font-size:12px;margin-bottom:8px;">
    TMS에서 받아오기: 화면 <b>${x.screens}</b>개 · 2시간마다 · 마지막 <b>${when}</b>${fail}</div>`;
}

async function renderAutoSync() {
  const host = $("#mg-auto-body");
  if (!host) return;
  let d;
  try { d = await api("/api/tms-sync/status"); }
  catch (err) { host.innerHTML = `<span class="muted">${escapeHtml(err.message)}</span>`; return; }
  const last = (d.history || [])[0];
  host.innerHTML = `
    <p class="muted" style="margin:0 0 8px;">
      TMS 엑셀이 <b>${escapeHtml(d.folder)}</b> 에 들어오면
      <b>${d.intervalMinutes}분마다</b> 확인해 새 파일만 반영합니다.
      이미 넣은 파일은 다시 넣지 않고, <b>값이 있는 칸은 건드리지 않습니다.</b></p>
    ${exporterLine(d.exporter)}
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
        <th style="text-align:right;">채움</th><th style="text-align:right;">오류</th><th>시각</th><th>비고</th></tr></thead>
      <tbody>${d.history.map((h) => `<tr>
        <td>${escapeHtml(h.file)}</td>
        <td style="text-align:right;">${(h.rows||0).toLocaleString()}</td>
        <td style="text-align:right;">${(h.created||0).toLocaleString()}</td>
        <td style="text-align:right;">${(h.updated||0).toLocaleString()}</td>
        <td style="text-align:right;">${h.errors ? `<span class="chip chip-red">${h.errors}</span>` : "0"}</td>
        <td class="muted">${escapeHtml((h.syncedAt||"").slice(0,16).replace("T"," "))}</td>
        <td class="muted" style="max-width:260px;" title="${escapeHtml(h.note||"")}">${escapeHtml((h.note||"").slice(0,60))}${(h.note||"").length > 60 ? "…" : ""}</td></tr>`).join("")}
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

/* ---------------- 🚧 창구 동시 마감 — 이중입력 경보(2026-09-03, docs/CUTOVER_PLAN.md §5) ----------------
   마감일(D)을 저장하면 연동 사본 변경 피드에서 D 이후 TMS 판매H·매입H·재고 '추가'만 골라 둔다(2분 틱).
   '수정'(입금 확인·반입·정정)은 위반이 아니다. 판정은 사람: 정상(되돌리기·정리) / OWS 재입력 완료. */
/* ─────────── ⤴ OWS → TMS 되돌려 쓰기 큐(2026-09-08 대표 "tms와 ows 그대로 연동해서 쓸거야") ───────────
   OWS에서 자산 수리비가 바뀌면 여기 한 줄이 쌓이고, 연동 틱(2분)이 창구로 밀어 넣는다.
   ★큐로 두는 이유: 창구가 죽어 있거나 아직 무장 전이어도 '바뀐 사실'이 사라지지 않는다.
     켜는 순간 밀린 것부터 순서대로 나간다 — 그래서 무엇이 밀려 있는지 여기서 보여야 한다. */
async function renderTmsOutbox() {
  const host = $("#mg-outbox-body");
  if (!host) return;
  let d;
  try { d = await api(`/api/tms-outbox${state.outboxAll ? "?all=1" : ""}`); }
  catch (err) { host.innerHTML = `<span class="muted">${escapeHtml(err.message)}</span>`; return; }
  const c = d.counts || {};
  const gate = d.gate || {};
  const last = d.last || {};
  const canEdit = hasPerm("purchase.edit");
  const ts = (s) => escapeHtml((s || "").slice(0, 16).replace("T", " "));
  const won = (v) => (v === null || v === undefined ? "-" : fmtWon(Number(v) || 0));
  // 창구 무장 상태 — 꺼져 있으면 큐는 쌓이기만 한다. 사람이 그걸 알아야 한다.
  const gateLine = !d.linkEnabled
    ? `<span class="chip chip-slate">연동 창구 설정 없음</span>`
    : !gate.reachable
      ? `<span class="chip chip-red" title="${escapeHtml(gate.error || "")}">창구에 연결 안 됨</span>`
      : gate.armed
        ? `<span class="chip chip-green">쓰기 무장됨 — 큐가 TMS로 나갑니다</span>`
        : `<span class="chip chip-amber" title="D:\\data-bridge\\.env 에 TMS_WRITE_ARMED=1 을 넣고 DataBridge 작업을 다시 시작하면 켜집니다">쓰기 무장 안 됨 — 쌓이기만 합니다</span>`;
  host.innerHTML = `
    <div class="inline-row" style="flex-wrap:wrap; align-items:center;">
      ${gateLine}
      <span class="chip ${c.open ? "chip-amber" : "chip-slate"}">보낼 것 ${c.open || 0}건</span>
      <span class="chip chip-green">반영됨 ${c.applied || 0}건</span>
      ${c.conflict ? `<span class="chip chip-red" title="그 사이 TMS 쪽에서 먼저 바뀐 건 — 덮어쓰지 않고 물러났습니다">TMS가 먼저 바뀜 ${c.conflict}</span>` : ""}
      ${c.failed ? `<span class="chip chip-red">실패 ${c.failed}</span>` : ""}
      ${c.stuck ? `<span class="chip chip-red" title="여러 번 실패해 자동 재시도를 멈췄습니다 — [다시 보내기]를 눌러야 움직입니다">멈춤 ${c.stuck}</span>` : ""}
      <span style="flex:1"></span>
      ${last.applied !== undefined ? `<span class="muted" style="font-size:12px;">마지막 틱: 반영 ${last.applied || 0} · 대기 ${last.skipped || 0} · 충돌 ${last.conflict || 0} · 실패 ${last.failed || 0}</span>` : ""}
      <label class="check-line" style="margin:0;"><input type="checkbox" id="ob-all" ${state.outboxAll ? "checked" : ""}> 보낸 것도 보기</label>
      ${canEdit ? `<button class="btn btn-sm" id="ob-push" title="2분 틱을 기다리지 않고 지금 밀어 넣습니다">↗ 지금 보내기</button>` : ""}
    </div>
    <p class="muted" style="font-size:12px; margin:8px 0 0;">
      자산 수리비(A/S·수리 등록·부품)가 바뀌면 TMS 재고내역의 <b>수리비·제조원가</b>에 되돌려 넣습니다.
      TMS에 없는 자산(OWS 채번 5000번대)과 번호가 잠긴 자산은 보내지 않습니다.
      TMS 변경 이력에는 <b>…_OWS연동_사람이름</b> 도장이 남습니다.</p>
    ${d.items.length ? `<div class="table-wrap" style="margin-top:8px;"><table>
      <thead><tr><th>관리번호</th><th>보낼 값</th><th>바뀐 이유</th><th>상태</th><th>넣은 사람</th><th></th></tr></thead>
      <tbody>${d.items.map((x) => `<tr>
        <td><b>${escapeHtml(x.assetNo || "-")}</b>
          <div class="muted" style="font-size:12px;">${escapeHtml(x.model || "")} · TMS 키 ${x.tmsKeyId}</div></td>
        <td>${Object.entries(x.values || {}).map(([k, v]) =>
              `${escapeHtml(k)} <b>${won(v)}</b>`).join("<br>")}
          ${x.before ? `<div class="muted" style="font-size:12px;">TMS 이전: ${Object.entries(x.before).map(([k, v]) => `${escapeHtml(k)} ${won(v)}`).join(" · ")}</div>` : ""}</td>
        <td class="muted" style="font-size:12px; max-width:200px;">${escapeHtml(x.reason || "")}</td>
        <td><span class="chip ${x.status === "applied" ? "chip-green"
              : x.status === "queued" ? "chip-amber"
              : x.status === "cancelled" ? "chip-slate" : "chip-red"}">${escapeHtml(x.statusLabel)}</span>
          ${x.attempts ? `<span class="muted" style="font-size:12px;"> ${x.attempts}회 시도</span>` : ""}
          ${x.lastError ? `<div class="muted" style="font-size:12px;" title="${escapeHtml(x.lastError)}">${escapeHtml(x.lastError.slice(0, 40))}</div>` : ""}
          ${x.appliedAt ? `<div class="muted" style="font-size:12px;" title="${escapeHtml(x.actionId || "")}">${ts(x.appliedAt)}</div>` : ""}</td>
        <td class="muted" style="font-size:12px;">${escapeHtml(x.createdBy || "")}<div>${ts(x.createdAt)}</div></td>
        <td style="white-space:nowrap;">${canEdit && x.status !== "applied" && x.status !== "cancelled"
            ? `<button class="btn btn-ghost btn-sm" data-obact="retry" data-obid="${x.id}"
                 title="시도 횟수를 지우고 다시 보냅니다">↻ 다시</button>
               <button class="btn btn-ghost btn-sm" data-obact="cancel" data-obid="${x.id}"
                 title="안 보내기로 합니다 — 이미 TMS에 반영된 값을 되돌리지는 않습니다">✕ 접기</button>` : ""}</td>
      </tr>`).join("")}</tbody></table></div>`
      : `<p class="muted" style="margin-top:8px;">${state.outboxAll ? "기록이 없습니다." : "보낼 것이 없습니다 👍"}</p>`}`;
  $("#ob-all", host).addEventListener("change", (e) => {
    state.outboxAll = e.target.checked;
    renderTmsOutbox();
  });
  const push = $("#ob-push", host);
  if (push) push.addEventListener("click", async () => {
    push.disabled = true;
    try {
      const r = await api("/api/tms-outbox/push", { method: "POST", body: {} });
      toast(r.applied ? `TMS에 ${r.applied}건 반영했습니다.`
        : r.skipped ? "창구 쓰기가 아직 무장되지 않아 그대로 남겨 뒀습니다."
        : "보낼 것이 없습니다.", !r.applied && !!r.failed);
      renderTmsOutbox();
    } catch (err) { toast(err.message, true); push.disabled = false; }
  });
  $$("button[data-obact]", host).forEach((b) => b.addEventListener("click", async () => {
    if (b.dataset.obact === "cancel" &&
        !confirm("이 줄을 안 보내기로 합니다.\n이미 TMS에 반영된 값은 되돌아가지 않습니다.\n\n계속할까요?")) return;
    try {
      await api(`/api/tms-outbox/${b.dataset.obid}/${b.dataset.obact}`, { method: "POST", body: {} });
      renderTmsOutbox();
    } catch (err) { toast(err.message, true); }
  }));
}

async function renderCutover() {
  const host = $("#mg-cutover-body");
  if (!host) return;
  let d;
  try { d = await api(`/api/cutover${state.cutoverAll ? "?all=1" : ""}`); }
  catch (err) { host.innerHTML = `<span class="muted">${escapeHtml(err.message)}</span>`; return; }
  const cfg = d.config || {};
  const inv = d.invariants || {};
  const last = d.last || {};
  const canSet = hasPerm("settings.manage");
  const canEdit = hasPerm("purchase.edit");
  const ts = (s) => escapeHtml((s || "").slice(0, 16).replace("T", " "));
  const invBad = (inv.asset_no_ge_5000 || 0) + (inv.slip_no_ge_500 || 0);
  host.innerHTML = `
    <div class="inline-row" style="align-items:center; gap:8px; margin-bottom:6px;">
      <label>마감일 <input type="date" id="co-date" value="${escapeHtml(cfg.date || "")}" ${canSet ? "" : "disabled"} style="width:150px;"></label>
      <label class="check-line" style="margin:0;"><input type="checkbox" id="co-enabled" ${cfg.enabled === false ? "" : "checked"} ${canSet ? "" : "disabled"}> 감시 켜기</label>
      ${canSet ? `<button class="btn btn-sm btn-primary" id="co-save">저장</button>` : ""}
      ${d.active ? `<span class="chip chip-green">감시 중 — ${escapeHtml(cfg.date)}부터</span>`
                 : `<span class="chip">마감일 없음 — 감시 안 함</span>`}
      ${!d.linkEnabled ? `<span class="chip chip-red">연동 창구 설정 없음</span>` : ""}
    </div>
    <p class="muted" style="margin:0 0 8px; font-size:12px;">
      마감일을 저장하는 순간의 연동 사본 위치가 기준선이 됩니다(기준선 ${escapeHtml(String(cfg.baselineCursor || 0))}${cfg.baselineAt ? ` · ${ts(cfg.baselineAt)} · ${escapeHtml(cfg.setBy || "")}` : ""}).
      마감일 <b>이후</b>에 TMS에 <b>새로 만든</b> 판매·매입 전표와 재고 행만 잡습니다 — 마감 전 전표의 입금 확인·반입·정정(수정)은 잡지 않습니다.
      ${d.active ? `마지막 점검 ${last.at ? ts(last.at) : "아직 없음"}(2분마다 자동).` : ""}
    </p>
    <div class="inline-row" style="margin-bottom:8px; align-items:center;">
      ${canEdit && d.linkEnabled ? `<button class="btn btn-sm" id="co-scan">지금 점검</button>` : ""}
      <button class="btn btn-sm btn-ghost" id="co-refresh">새로고침</button>
      <label class="check-line" style="margin:0;"><input type="checkbox" id="co-all" ${state.cutoverAll ? "checked" : ""}> 처리한 것도 보기</label>
      ${invBad ? `<span class="chip chip-red" title="TMS 손입력이 OWS 예약 번호대(자산 5000~ · 전표 500~)에 들어왔습니다 — TMS에서 번호를 바로잡아야 합니다(사본은 TMS를 고치지 않음)">번호대 침범 자산 ${inv.asset_no_ge_5000 || 0} · 전표 ${inv.slip_no_ge_500 || 0}</span>`
              : `<span class="chip chip-green" title="TMS에 OWS 예약 번호대(자산 5000~ · 전표 500~) 침범 없음${inv.checked_at ? ` · ${inv.checked_at}` : ""}">번호대 침범 0</span>`}
      <span class="chip ${d.open.total ? "chip-red" : "chip-green"}">확인 필요 ${d.open.total}건${d.open.total ? ` (판매 ${d.open["판매"]} · 매입 ${d.open["매입"]} · 재고 ${d.open["재고"]})` : ""}</span>
    </div>
    ${(d.items || []).length ? `<div class="table-wrap"><table>
      <thead><tr><th>구분</th><th>전표 · 관리번호</th><th>거래처</th><th style="text-align:right;">수량</th><th style="text-align:right;">금액</th>
        <th>TMS 입력자</th><th>TMS 시각</th><th>표시</th><th>상태</th>${canEdit ? "<th></th>" : ""}</tr></thead>
      <tbody>${d.items.map((it) => `<tr data-id="${it.id}" ${it.status !== "open" ? 'class="muted"' : ""}>
        <td>${escapeHtml(it.kind)}</td>
        <td><b>${escapeHtml(it.slipNo || "-")}</b>${it.assetNo ? ` <span class="muted">${escapeHtml(it.assetNo)}</span>` : ""}</td>
        <td>${escapeHtml(it.party || "")}</td>
        <td style="text-align:right;">${it.qty || 0}</td>
        <td style="text-align:right;">${it.amount ? fmtWon(it.amount) : "-"}</td>
        <td>${escapeHtml(it.actor || "")}</td>
        <td class="muted">${ts(it.changedAt)}</td>
        <td>${it.pattern ? `<span class="chip chip-violet">${escapeHtml(it.pattern)}</span>` : ""}</td>
        <td>${it.status === "open" ? `<span class="chip chip-red">확인 필요</span>`
              : `<span class="chip chip-green" title="${escapeHtml(it.resolvedBy || "")} ${ts(it.resolvedAt)}">${escapeHtml(it.statusLabel)}</span>${it.note ? `<div class="muted" style="font-size:11px;">${escapeHtml(it.note)}</div>` : ""}`}</td>
        ${canEdit ? `<td style="white-space:nowrap;">${it.status === "open"
          ? `<button class="btn btn-sm" data-co-status="reentered" title="같은 전표를 OWS에 다시 입력했다">OWS 재입력 완료</button>
             <button class="btn btn-sm btn-ghost" data-co-status="ok" title="되돌리기 중이거나 마감 전 정리 작업이라 정상">정상</button>`
          : `<button class="btn btn-sm btn-ghost" data-co-status="open">다시 열기</button>`}</td>` : ""}
      </tr>`).join("")}</tbody></table></div>`
      : `<p class="muted">${d.active ? "마감일 뒤 TMS에 새로 들어온 전표·자산이 없습니다." : "마감일을 저장하면 감시가 시작됩니다."}</p>`}`;
  $("#co-refresh").addEventListener("click", renderCutover);
  $("#co-all").addEventListener("change", (e) => { state.cutoverAll = e.target.checked; renderCutover(); });
  $("#co-save")?.addEventListener("click", async () => {
    const date = $("#co-date").value;
    const enabled = $("#co-enabled").checked;
    if (date && date !== (cfg.date || "") &&
        !confirm(`마감일을 ${date}(으)로 저장합니다.\n이 순간의 연동 사본 위치가 기준선이 되고, 그 뒤 TMS에 새로 만든 판매·매입 전표를 경보로 띄웁니다.\n계속할까요?`)) return;
    if (!date && cfg.date && !confirm("마감일을 지우면 감시가 멈춥니다. 계속할까요?")) return;
    try {
      const r = await api("/api/cutover", { method: "POST", body: { date, enabled } });
      toast(r.changed ? (date ? `마감일 ${date} 저장 — 기준선 ${r.config.baselineCursor}` : "마감일을 지웠습니다") : "저장했습니다");
    } catch (err) { toast(err.message, true); }
    renderCutover();
  });
  $("#co-scan")?.addEventListener("click", async () => {
    const b = $("#co-scan"); b.disabled = true; b.textContent = "점검 중…";
    try {
      const r = await api("/api/cutover/scan", { method: "POST", body: {} });
      const res = r.result || {};
      toast(res.skipped ? `점검 건너뜀 — ${res.skipped}` : `점검 완료 — 새 경보 ${res.new || 0}건 (본 변경 ${res.checked || 0}건)`);
    } catch (err) { toast(err.message, true); }
    renderCutover();
  });
  $$("button[data-co-status]", host).forEach((b) => b.addEventListener("click", async () => {
    const id = b.closest("tr").dataset.id;
    const status = b.dataset.coStatus;
    let note = "";
    if (status !== "open") {
      note = prompt(status === "reentered" ? "OWS에 입력한 전표번호나 메모(선택)"
                                            : "정상으로 보는 이유(선택 — 예: 되돌리기 중, 마감 전 정리)", "");
      if (note === null) return;
    }
    try {
      await api(`/api/cutover/entries/${id}/status`, { method: "POST", body: { status, note } });
      toast(status === "open" ? "다시 열었습니다" : "처리했습니다");
    } catch (err) { toast(err.message, true); }
    renderCutover();
  }));
}

/* ---------------- 판매 전표 (TMS 이관분) ----------------

   TMS 판매전표는 '고객 주문 1건'이 아니라 '하루치 채널별 묶음'이다
   (S260804-001 하나에 방문구매 26대). 그래서 주문관리가 아니라 여기에 둔다.
   자산별로 누가 사 갔는지는 자산 상세의 이력에 남아 있다. */

/* ---------------- 💻 판매 자산(TMS) (대표 2026-08-25) ----------------
   TMS 판매현황(자산 한 대 = 한 줄)을 자산번호로 붙인 원장 — 자산별 마진.
   원가는 OWS(매입가+수리비)가 있으면 그걸, 없으면 TMS 매입가(원가출처 표시). */
async function renderSaleAssets(body) {
  const seq = ++state.renderSeq;
  const f = state.saleAssetFilter || (state.saleAssetFilter = {
    from: ymd(new Date(new Date().getFullYear(), new Date().getMonth(), 1)), to: ymd(), q: "", cancelled: false });
  body.innerHTML = `<div class="card placeholder"><p>불러오는 중…</p></div>`;
  let d, rounds = {};
  try {
    d = await api(`/api/sale-assets?from=${f.from}&to=${f.to}&q=${encodeURIComponent(f.q)}${f.cancelled ? "&includeCancelled=1" : ""}`);
    // 회전 이력(2026-09-03) — 판매→반입→재판매가 같은 자산의 여러 행이라 회차 N/M 과 반입일·취소일을 같이 붙인다.
    //   집계(판매액·원가·마진)는 손대지 않는다.
    const nos = [...new Set(d.items.map((x) => x.assetNo).filter(Boolean))];
    if (nos.length) rounds = (await api("/api/sale-slips/rounds", { method: "POST", body: { assetNos: nos } }).catch(() => ({}))).assets || {};
  } catch (err) {
    body.innerHTML = `<div class="card"><p class="muted">${escapeHtml(err.message)}</p></div>`;
    return;
  }
  if (seq !== state.renderSeq) return;
  const canEdit = hasPerm("purchase.edit");
  const roundCell = (x) => {
    const h = rounds[x.assetNo];
    const rr = h && h.rows.find((r) => r.id === x.id);
    if (!h || !rr) return '<span class="muted">-</span>';
    const others = h.rows.filter((r) => r.id !== x.id)
      .map((r) => `${r.round ? r.round + "회차" : "취소"} ${r.saleDate} ${r.slipNo}${r.returnDate ? " 반입 " + r.returnDate : ""}${r.cancelDate ? " 취소 " + r.cancelDate : ""}`);
    if (rr.round == null) return `<span class="chip chip-red" style="font-size:11px;" title="${escapeHtml(others.join("\n"))}">취소</span>`;
    if (h.count <= 1 && h.rows.length <= 1) return '<span class="muted">1/1</span>';
    return `<span class="chip ${rr.round === h.count ? "chip-blue" : "chip-violet"}" style="font-size:11px;"
      title="같은 자산의 판매 회전 — 다른 회차:&#10;${escapeHtml(others.join("\n"))}">회차 ${rr.round}/${h.count}</span>`;
  };
  const stageCell = (x) => {
    const h = rounds[x.assetNo];
    const rr = h && h.rows.find((r) => r.id === x.id);
    const dates = rr ? [rr.returnDate ? `반입 ${rr.returnDate}` : "", rr.cancelDate ? `취소 ${rr.cancelDate}` : ""].filter(Boolean) : [];
    return `${saleStageChip(x.stage)}${dates.length ? `<div class="muted" style="font-size:11px;">${escapeHtml(dates.join(" · "))}</div>` : ""}`;
  };
  body.innerHTML = `
    <div class="kpi-row">
      <div class="kpi"><div class="kpi-label">판매 대수</div>
        <div class="kpi-value">${d.total.toLocaleString("ko-KR")}</div></div>
      <div class="kpi"><div class="kpi-label">판매액</div>
        <div class="kpi-value">${fmtWon(d.saleSum)}</div></div>
      <div class="kpi"><div class="kpi-label">원가</div>
        <div class="kpi-value">${fmtWon(d.costSum)}</div></div>
      <div class="kpi" style="border-left-color:var(--primary);"><div class="kpi-label">마진</div>
        <div class="kpi-value">${fmtWon(d.marginSum)}</div>
        ${d.zeroPriceCount ? `<div class="muted" style="font-size:11.5px;"
          title="TMS에 판매가가 0으로 적힌 행 — 금액이 판매등록 칸에만 있어 합계에서 뺐습니다">판매가 미기재 ${d.zeroPriceCount}대 제외</div>` : ""}</div>
    </div>
    <div class="card">
      <div class="inline-row" style="margin:0 0 10px;">
        <input type="date" id="sa2-from" value="${f.from}">
        <span class="muted">~</span>
        <input type="date" id="sa2-to" value="${f.to}">
        <input type="text" id="sa2-q" value="${escapeHtml(f.q)}"
          placeholder="자산번호/고객/모델/전표/채널" style="min-width:200px;">
        <label class="check-line" style="font-size:12.5px;"><input type="checkbox" id="sa2-cancelled" ${f.cancelled ? "checked" : ""}> 취소 포함</label>
        <button class="btn btn-sm btn-primary" id="sa2-go">조회</button>
        <span style="flex:1;"></span>
        <span class="muted" style="font-size:12px;"
          title="TMS 판매 원장 전체 중 OWS 자산과 자산번호로 이어진 수">
          자산 매칭 ${d.matchedAll.toLocaleString("ko-KR")}/${d.totalAll.toLocaleString("ko-KR")}</span>
        <span class="muted" style="font-size:12px;"
          title="TMS 판매현황이 자동 반영됩니다 — 반영 시각은 설정 ▸ 데이터 이관에서 봅니다">🔄 자동 동기화</span>
      </div>
      ${d.items.length ? `<div class="table-wrap"><table>
        <thead><tr><th>판매일</th><th>전표</th><th>자산번호</th>
          <th title="같은 자산이 판매→반입→재판매로 돈 회차 (현재/전체). 취소 행은 회차에서 뺍니다.">회차</th>
          <th>상태</th><th>모델</th><th>고객</th>
          <th>채널</th><th style="text-align:right;">판매가</th>
          <th style="text-align:right;">원가</th><th style="text-align:right;">마진</th><th></th></tr></thead>
        <tbody>${d.items.map((x) => `<tr style="${x.stage === "반입" || (x.stage || "").includes("취소") ? "opacity:.65;" : ""}">
          <td class="muted">${escapeHtml(x.saleDate)}</td>
          <td class="muted" style="font-size:12px;">${escapeHtml(x.slipNo)}</td>
          <td><b>${escapeHtml(x.assetNo)}</b></td>
          <td>${roundCell(x)}</td>
          <td>${stageCell(x)}</td>
          <td>${escapeHtml(x.model || "")}</td>
          <td>${escapeHtml(x.customer || "")}</td>
          <td class="muted" style="font-size:12px;">${escapeHtml(x.channel || "")}</td>
          <td style="text-align:right;">${fmtWon(x.salePrice)}</td>
          <td style="text-align:right;" class="muted"
            title="${x.costSrc === "ows" ? "OWS 매입가+수리비" : "TMS 매입가(OWS 매입가 없음)"}">${fmtWon(x.cost)}${x.costSrc === "tms" ? "*" : ""}</td>
          <td style="text-align:right;"><b style="${x.margin < 0 ? "color:var(--danger);" : ""}">${fmtWon(x.margin)}</b></td>
          <td>${x.order
            ? `<span class="chip chip-violet" style="font-size:11px;"
                 title="OWS 주문 ${escapeHtml(x.order.no || "#" + x.order.id)} · 주문금액 ${fmtNum(x.order.amount)}원(${x.order.qty}대)${x.order.qty === 1 && x.order.amount !== x.salePrice ? `&#10;⚠ TMS 판매가와 ${fmtNum(Math.abs(x.order.amount - x.salePrice))}원 차이` : ""}">주문 ${escapeHtml((x.order.no || "").slice(-8) || "#" + x.order.id)}${x.order.qty === 1 && x.order.amount !== x.salePrice ? " ⚠" : ""}</span>`
            : (x.hasOrder ? '<span class="chip chip-violet" style="font-size:11px;">주문연결</span>' : "")}</td>
        </tr>`).join("")}</tbody></table></div>
      ${d.shown < d.total ? `<p class="muted" style="font-size:12px;">최근 ${d.shown.toLocaleString("ko-KR")}건만 표시 — 기간·검색으로 좁혀 주세요(합계는 표시분 기준).</p>` : ""}
      <p class="muted" style="font-size:12px; margin-top:6px;">원가에 *가 붙으면 OWS 매입가가 없어
        TMS 매입가를 쓴 것입니다. 업그레이드1·2/부가세 분리 금액은 TMS 판매등록 추출이 붙으면 반영됩니다.
        회차는 같은 자산이 판매→반입→재판매로 돈 횟수입니다(회전 있는 자산은 행이 여러 줄 — 마진 열은 행마다 그대로).</p>`
      : `<p class="muted">이 기간에 판매 기록이 없습니다.${d.totalAll === 0
          ? " TMS 판매현황이 자동으로 들어옵니다 — 잠시 뒤 새로고침해 보세요." : ""}</p>`}
    </div>`;
  $("#sa2-go", body).addEventListener("click", () => {
    f.from = $("#sa2-from", body).value;
    f.to = $("#sa2-to", body).value;
    f.q = $("#sa2-q", body).value.trim();
    f.cancelled = $("#sa2-cancelled", body).checked;
    renderSaleAssets(body);
  });
  $("#sa2-q", body).addEventListener("keydown", (e) => {
    if (e.key === "Enter") $("#sa2-go", body).click();
  });

}

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
    if (f.customerExact) qs.set("customerExact", f.customerExact);   // 미수금 → 전표 보기
    let d, rc = null;
    try {
      d = await api("/api/sale-slips?" + qs.toString());
      // 미수금 집계(2026-08-09) — 500건 캡이 있는 목록으로 합산하면 틀려서 서버가 준다
      rc = await api("/api/sale-slips/receivables").catch(() => null);
    } catch (err) {
      body.innerHTML = `<div class="card"><p class="muted">${escapeHtml(err.message)}</p></div>`;
      return;
    }
    if (seq !== state.renderSeq) return;   // 탭을 옮겼으면 늦게 온 응답을 그리지 않는다
    draw(d, rc);
  };

  const draw = (d, rc) => {
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
          ${rc && rc.total ? chip("미수금", fmtWon(rc.total) + " · " + rc.count + "곳", "chip-red") : ""}
        </div>
        ${(d.excluded || []).length ? `
        <p style="margin:8px 0 0; font-size:13px;">
          위 금액에서 뺀 것 — ${d.excluded.map((e) =>
            `<span class="chip ${e.stage === "판매취소" ? "chip-red" : "chip-violet"}">${
              escapeHtml(e.stage)} ${e.count}건 ${fmtWon(e.sale)}</span>`).join(" ")}
        </p>` : ""}
        <p class="muted" style="margin:8px 0 0;">
          판매 전표 — <b>[＋ 새 전표]</b>로 OWS에서 직접 등록하고, TMS 연동으로 들어온 전표도 함께 봅니다
          (전표번호를 누르면 상세·수정). 전표 하나가 그날 그 거래의 판매 묶음이라 자산 여러 대가 들어 있고,
          어떤 자산이 누구에게 나갔는지는 자산 상세의 이력에도 남습니다.
          ${d.capped ? "" : `표에 ${(d.shown || 0).toLocaleString("ko-KR")}줄이 모두 나와 있습니다.`}</p>
      </div>

      ${rc && rc.total ? `
      <div class="card">
        <details>
          <summary style="cursor:pointer;"><b>💰 거래처별 미수금 ${fmtWon(rc.total)}</b>
            <span class="muted">· ${rc.count}곳 — 눌러서 펼치기 (OWS 전표의 입금은 전표 상세에서 입력, TMS 전표는 연동으로 자동 반영)</span></summary>
          <div class="table-wrap" style="margin-top:8px;"><table>
            <thead><tr><th>거래처</th><th style="text-align:right;">전표</th>
              <th style="text-align:right;">잔액</th>
              <th style="text-align:right;">30일 내</th><th style="text-align:right;">31~60일</th>
              <th style="text-align:right;">61~90일</th><th style="text-align:right;">90일 초과</th>
              <th>가장 오래된 판매일</th><th></th></tr></thead>
            <tbody>${rc.customers.slice(0, 40).map((c) => `
              <tr>
                <td><b>${escapeHtml(c.customer)}</b></td>
                <td style="text-align:right;">${c.slips}건</td>
                <td style="text-align:right;"><b>${fmtWon(c.balance)}</b></td>
                <td style="text-align:right;">${c.d30 ? fmtWon(c.d30) : "-"}</td>
                <td style="text-align:right;">${c.d60 ? fmtWon(c.d60) : "-"}</td>
                <td style="text-align:right;">${c.d90 ? fmtWon(c.d90) : "-"}</td>
                <td style="text-align:right;">${c.over90
                  ? `<span class="chip chip-red">${fmtWon(c.over90)}</span>` : "-"}</td>
                <td class="muted">${escapeHtml(c.oldest)}</td>
                <td><button class="btn btn-sm btn-ghost" data-rcq="${escapeHtml(c.customer)}"
                  title="이 거래처의 미입금 전표만 보기">전표 보기</button></td>
              </tr>`).join("")}
            </tbody></table></div>
          ${rc.count > 40 ? `<p class="muted">외 ${rc.count - 40}곳 — 잔액이 큰 순서입니다.</p>` : ""}
        </details>
      </div>` : ""}

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
          ${hasPerm("purchase.edit") ? `<span style="flex:1;"></span>
          <button class="btn btn-sm btn-primary" id="ss-new"
            title="판매 전표를 OWS에서 직접 등록합니다(전표번호 S날짜-500부터)">＋ 새 전표</button>` : ""}
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
              <td style="white-space:nowrap;"><button class="btn btn-sm btn-ghost" data-ssopen="${escapeHtml(s.slipNo)}"
                  style="font-weight:700; padding:2px 6px;" title="전표 상세 · 수정">${escapeHtml(s.slipNo)}</button>
                ${slipSourceChip(s)}</td>
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
    wirePager(body, "saleslips", () => draw(d, rc));
    // 전표 직접 등록·상세(2026-09-02) — 목록은 저장 뒤 다시 불러온다
    const nb = $("#ss-new", body);
    if (nb) nb.addEventListener("click", () => openSaleSlipForm(() => load()));
    $$("button[data-ssopen]", body).forEach((b) => b.addEventListener("click", () =>
      openSaleSlipDetail(b.dataset.ssopen, () => load())));
    // 거래처별 미수금 → 그 거래처의 미입금 전표만 필터.
    // ★검색어(LIKE)가 아니라 정확 일치로 거른다 — '(미지정)'(거래처명 빈칸)도 찾아진다.
    $$("button[data-rcq]", body).forEach((b) => b.addEventListener("click", () => {
      f.q = "";
      f.customerExact = b.dataset.rcq;
      f.unpaid = true;
      resetPage("saleslips");
      load();
    }));

    const apply = () => {
      f.q = $("#ss-q").value.trim();
      f.from = $("#ss-from").value;
      f.to = $("#ss-to").value;
      f.channel = $("#ss-channel").value;
      f.unpaid = $("#ss-unpaid").checked;
      f.customerExact = "";              // 필터를 손대면 '전표 보기' 고정을 푼다
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

/* ---------------- 판매 전표 직접 등록·수정 (2026-09-02 대표 방침) ----------------
   "앞으로 모든 데이터는 OWS·RMS에서 직접 등록·관리한다." — 전표 단위 판매(전표 + 자산별
   판매가·옵션가·정산·입금)를 TMS 화면 대신 여기서 넣는다. 저장 원장은 지금까지 연동으로
   쌓던 같은 표(sale_slips / tms_sales)라 목록·미수금·리포트가 그대로 이어진다.
   ★번호는 S날짜-500부터(TMS 001~499와 분리). 자산은 살아 있는 다른 전표·주문에 있으면 거부.
   ★반입·취소는 재고를 자동으로 되살리지 않는다 — '복귀 후보'로만 남기고, 실물을 확인했을 때
     체크를 켜면 바로 재고(입고·실재고)로 돌아간다. 라인 제거는 '판매취소' 표시(지우지 않음). */
/* ★순이익 산식 = TMS 판매상세 실측(2026-09-03, docs/SALE_ENTRY.md §1-6).
   업그레이드·탈거·기타구성·충전기·포장료는 매출이 아니라 '부품 원가'다 — 판매가가 그 자산의 매출 전부(옵션 포함가).
   탈거가는 회수 원가(TMS 관례: 양수 = 원가에서 뺌 → 순이익이 커짐). 부호 선택은 남겨 두되 기본이 회수다. */
const SALE_OPT_FIELDS = [
  ["upgrade1Price", "업그레이드1 원가", "upgrade1Item", "업그레이드1 항목"],
  ["upgrade2Price", "업그레이드2 원가", "upgrade2Item", "업그레이드2 항목"],
  ["removalPrice", "탈거가(회수)", "removalItem", "탈거 항목"],   // －/＋ 선택(대표 2026-09-02: 더하거나 뺄 수 있게) — 저장은 부호 붙은 값
  ["extraPrice", "기타구성 원가", "extraItems", "기타 구성품"],
  ["chargerPrice", "충전기 원가", null, null],
  ["packingFee", "포장료", null, null],
];
// 옵션 원가에 '더하는' 칸 — 서버(sale_entry.LINE_OPTION_COST_COLS)와 같은 목록(시험이 비교). 탈거가는 뺀다.
const SALE_OPTION_COST_KEYS = ["upgrade1Price", "upgrade2Price", "extraPrice", "chargerPrice"];
// TMS 실측 상수 — 서버(sale_entry.SALE_VAT_RATE / SALE_FEE_RATE_DEFAULT)와 같아야 한다(시험이 비교)
const SALE_VAT_RATE = 10;
const SALE_FEE_RATE = 3;
const PAID_STATUSES = ["미수", "선수금", "중도금", "완납"];

function halfUp(x) { return Math.floor(x + 0.5); }     // TMS 반올림(.5 올림)
/* 라인 순이익 미리보기 — 서버 sale_entry._line_calc 와 같은 식(저장 뒤 서버 값과 같아야 한다).
     부품옵션합계 = 업1+업2+기타구성+충전기 − 탈거 / 총원가 = 자산 원가(매입가+수리·부품비) + 옵션 + 포장료
     실부가세 = 판매부가세(판매가×10%) − 매입부가세(총원가×10%) / 판매수수료 = 판매가×수수율(기본 3%)
     순이익 = 판매가 − 총원가 − 판매수수료 − 실부가세
   정산 칸(판매부가세·판매수수료)은 사람이 직접 고쳤을 때(…Manual)만 그 값, 아니면 규칙값. */
function saleLineCalc(l, assetCost) {
  const price = Number(l.salePrice) || 0;
  const option = SALE_OPTION_COST_KEYS.reduce((s, k) => s + (Number(l[k]) || 0), 0) - (Number(l.removalPrice) || 0);
  const packing = Number(l.packingFee) || 0;
  const cost = Number(assetCost) || 0;
  const totalCost = cost + option + packing;
  const feeRate = (l.feeRate == null || l.feeRate === "") ? SALE_FEE_RATE : (Number(l.feeRate) || 0);
  const saleVat = l.saleVatManual ? (Number(l.saleVat) || 0) : halfUp(price * SALE_VAT_RATE / 100);
  const saleFee = l.saleFeeManual ? (Number(l.saleFee) || 0) : halfUp(price * feeRate / 100);
  const buyVat = halfUp(totalCost * SALE_VAT_RATE / 100);
  const netVat = saleVat - buyVat;
  return { price, option, packing, cost, totalCost, feeRate, saleVat, saleFee, buyVat, netVat,
           profit: price - totalCost - saleFee - netVat };
}
/* 서버로 보낼 라인 — 화면 전용 칸을 떼고, 정산 칸은 사람이 직접 고쳤을 때만 보낸다(안 보내면 서버가 규칙으로 센다) */
function saleLineBody(l) {
  const { open, maker, model, statusLabel, warning, assetCost, saleVatManual, saleFeeManual,
          saleVat, saleFee, ...rest } = l;
  if (saleVatManual) rest.saleVat = saleVat;
  if (saleFeeManual) rest.saleFee = saleFee;
  return rest;
}
function saleCalcLine(c) {
  return `옵션 원가 ${fmtWon(c.option)} · 총원가 <b>${fmtWon(c.totalCost)}</b>(자산 ${fmtWon(c.cost)} + 옵션 + 포장 ${fmtWon(c.packing)})
    · 실부가세 ${fmtWon(c.netVat)}(판매 ${fmtWon(c.saleVat)} − 매입 ${fmtWon(c.buyVat)}) · 수수료 ${fmtWon(c.saleFee)}
    → 순이익 <b style="${c.profit < 0 ? "color:var(--danger);" : ""}">${fmtWon(c.profit)}</b>`;
}
function slipSourceChip(s) {
  if (s.isOws || s.source === "ows") {
    return `<span class="chip chip-teal" style="font-size:11px;" title="OWS에서 직접 등록한 전표${s.owsEditedBy ? " · " + escapeHtml(s.owsEditedBy) : ""}">OWS 등록</span>`;
  }
  if (s.owsEditedAt) {
    return `<span class="chip chip-amber" style="font-size:11px;"
      title="TMS 연동 전표를 OWS에서 고쳤습니다 — 연동이 고친 칸을 덮지 않습니다(입금 4칸은 TMS 원본 유지)&#10;${escapeHtml((s.owsEditedAt || "").slice(0, 16).replace("T", " "))} ${escapeHtml(s.owsEditedBy || "")}">OWS 수정</span>`;
  }
  return `<span class="chip chip-slate" style="font-size:11px;" title="TMS 연동으로 들어온 전표">TMS 연동</span>`;
}
function saleStageChip(stage) {
  if (!stage) return "-";
  const cls = stage === "판매취소" ? "chip-red" : stage === "반입" ? "chip-violet" : "chip-green";
  return `<span class="chip ${cls}">${escapeHtml(stage)}</span>`;
}
function parseMoneyEl(el) {
  const v = String(el && el.value || "").replace(/,/g, "").trim();
  return v === "" ? 0 : Number(v) || 0;
}
/* 스캔 칸에 붙여넣은 여러 자산번호 — 줄바꿈·쉼표·공백·탭으로 나눈다 */
function splitAssetNos(text) {
  return String(text || "").split(/[\s,;]+/).map((x) => x.trim()).filter(Boolean);
}
/* 바코드 스캐너는 번호 뒤에 Enter 를 보낸다 — key 이름이 다른 드라이버도 있어 keyCode 로도 본다 */
function isEnterKey(e) {
  return (e.key === "Enter" || e.keyCode === 13) && !e.isComposing;
}

/* 전표 헤더 입력 칸(새 전표·상세 수정 공용). s 가 있으면 값을 채운다. */
function saleHeadFormHtml(s, channels, opts) {
  const v = (k) => escapeHtml(s && s[k] != null ? s[k] : "");
  const m = (k) => s && s[k] ? fmtNum(s[k]) : "";
  const isOws = !s || s.isOws;
  const paidLock = !isOws;   // 연동 전표의 입금 4칸은 TMS가 원본 — 여기서 못 고친다
  return `
    <div class="form-grid">
      <label>판매채널<input type="text" id="se-channel" list="se-channels" value="${v("channel")}"
        placeholder="예: 방문구매 · 자사몰 · B2B" autocomplete="off">
        <datalist id="se-channels">${(channels || []).map((c) => `<option value="${escapeHtml(c)}">`).join("")}</datalist></label>
      <label>판매처명(거래처)<input type="text" id="se-customer" value="${v("customer")}" placeholder="거래처·구매자"></label>
      <label>판매일<input type="date" id="se-date" value="${v("saleDate") || (s ? "" : ymd())}"></label>
      <label>출고일<input type="date" id="se-ship" value="${v("shipDate")}"></label>
      <label>송장번호<input type="text" id="se-waybill" value="${v("waybill")}"></label>
      <label>부가세<input type="text" data-money id="se-vat" value="${m("vat")}" placeholder="0"></label>
      <label>수수료<input type="text" data-money id="se-fee" value="${m("fee")}" placeholder="0"></label>
      <label>배송비<input type="text" data-money id="se-shipping" value="${m("shipping")}" placeholder="0"></label>
      <label>착불<input type="text" data-money id="se-cod" value="${m("cod")}" placeholder="0"></label>
      <label>메모<input type="text" id="se-memo" value="${v("memo")}"></label>
    </div>
    <details style="margin-top:8px;" ${s && (s.paidAmount || s.paidStatus) ? "open" : ""}>
      <summary style="cursor:pointer;"><b>💰 입금</b>
        <span class="muted">${paidLock
          ? "— TMS 연동 전표의 입금은 TMS가 원본이라 연동으로만 바뀝니다"
          : "— 미수금 집계에 쓰입니다(입금확인을 켜면 미수에서 빠집니다)"}</span></summary>
      <div class="form-grid" style="margin-top:6px;">
        <label>납부확인<select id="se-paidstatus" ${paidLock ? "disabled" : ""}>
          <option value="">-</option>
          ${PAID_STATUSES.map((p) => `<option ${s && s.paidStatus === p ? "selected" : ""}>${p}</option>`).join("")}
          ${s && s.paidStatus && !PAID_STATUSES.includes(s.paidStatus) ? `<option selected>${escapeHtml(s.paidStatus)}</option>` : ""}
        </select></label>
        <label>입금일<input type="date" id="se-paidat" value="${escapeHtml((s && s.paidAt || "").slice(0, 10))}" ${paidLock ? "disabled" : ""}></label>
        <label>입금액<input type="text" data-money id="se-paidamt" value="${m("paidAmount")}" placeholder="0" ${paidLock ? "disabled" : ""}></label>
        <label class="check-line" style="align-self:end;"><input type="checkbox" id="se-paidok"
          ${s && s.paidConfirmed ? "checked" : ""} ${paidLock ? "disabled" : ""}> 입금확인(완납)</label>
      </div>
    </details>
    ${opts && opts.manualTotal ? `
    <div class="form-grid" style="margin-top:8px;">
      <label>판매금액 직접 지정 <span class="muted">(비우면 라인 합계 그대로 · 적으면 차액이 '판매차이금액'으로 남습니다)</span>
        <input type="text" data-money id="se-total" value="${s && s.diffAmount ? fmtNum(s.saleAmount) : ""}" placeholder="라인 합계"></label>
    </div>` : ""}`;
}
function readSaleHead(host, isOws) {
  const out = {
    channel: $("#se-channel", host).value.trim(), customer: $("#se-customer", host).value.trim(),
    saleDate: $("#se-date", host).value, shipDate: $("#se-ship", host).value,
    waybill: $("#se-waybill", host).value.trim(), memo: $("#se-memo", host).value.trim(),
    vat: parseMoneyEl($("#se-vat", host)), fee: parseMoneyEl($("#se-fee", host)),
    shipping: parseMoneyEl($("#se-shipping", host)), cod: parseMoneyEl($("#se-cod", host)),
  };
  if (isOws !== false) {
    out.paidStatus = $("#se-paidstatus", host).value;
    out.paidAt = $("#se-paidat", host).value;
    out.paidAmount = parseMoneyEl($("#se-paidamt", host));
    out.paidConfirmed = $("#se-paidok", host).checked;
  }
  const tot = $("#se-total", host);
  if (tot && String(tot.value).trim() !== "") out.saleAmount = parseMoneyEl(tot);
  return out;
}

/* 라인 편집 칸 — 옵션 원가 6칸 + 항목 + 정산(수수율·판매부가세·판매수수료) + 수령자/메모.
   새 전표와 상세 라인 수정이 같은 칸을 쓴다. calc = saleLineCalc(l, 자산 원가). */
function saleLineEditHtml(l, idx, calc) {
  const num = (k) => `<input type="number" step="1" data-lk="${k}" data-li="${idx}" value="${l[k] != null && l[k] !== "" ? l[k] : ""}"
    placeholder="0" style="width:110px; text-align:right;">`;
  const txt = (k, ph) => `<input type="text" data-lk="${k}" data-li="${idx}" value="${escapeHtml(l[k] || "")}" placeholder="${ph}" style="min-width:150px;">`;
  // 탈거가: 기본은 TMS 관례(회수 = 원가에서 뺌, 양수 저장). '원가에 더함'을 고르면 음수로 저장한다.
  const signed = (k) => {
    const v = Number(l[k]) || 0;
    return `<span style="display:flex; gap:4px; align-items:center;">
      <select data-lsign="${k}" data-li="${idx}" style="width:150px;" title="TMS 관례: 탈거(회수)한 부품값은 원가에서 뺍니다">
        <option value="1"${v >= 0 ? " selected" : ""}>－ 회수(원가에서 뺌)</option>
        <option value="-1"${v < 0 ? " selected" : ""}>＋ 원가에 더함</option>
      </select>
      <input type="number" step="1" min="0" data-lk="${k}" data-li="${idx}" value="${v ? Math.abs(v) : ""}" placeholder="0" style="width:110px; text-align:right;"></span>`;
  };
  const c = calc || saleLineCalc(l, l.assetCost != null ? l.assetCost : l.cost);
  const col = (label, inner) => `<label class="muted" style="display:flex; flex-direction:column; gap:2px;">${label}${inner}</label>`;
  return `<div style="display:grid; grid-template-columns:repeat(auto-fit, minmax(230px, 1fr)); gap:6px 12px; font-size:12.5px;">
    ${SALE_OPT_FIELDS.map(([pk, pl, ik, il]) => `
      ${col(pl, pk === "removalPrice" ? signed(pk) : num(pk))}
      ${ik ? col(il, txt(ik, "예: RAM 16G")) : ""}`).join("")}
    ${col("판매수수율(%) <span style=\"font-weight:400;\">기본 3 · B2B/방문은 0</span>",
      `<input type="number" step="0.1" min="0" max="100" data-lk="feeRate" data-li="${idx}" value="${c.feeRate}" style="width:110px; text-align:right;">`)}
    ${col(`판매수수료 <span style="font-weight:400;">= 판매가×수수율${l.saleFeeManual ? " · <b>직접 지정</b>" : ""}</span>`,
      `<input type="number" step="1" min="0" data-lk="saleFee" data-li="${idx}" data-settle="1" value="${c.saleFee}" style="width:110px; text-align:right;">`)}
    ${col(`판매부가세 <span style="font-weight:400;">= 판매가×${SALE_VAT_RATE}%${l.saleVatManual ? " · <b>직접 지정</b>" : ""}</span>`,
      `<input type="number" step="1" min="0" data-lk="saleVat" data-li="${idx}" data-settle="1" value="${c.saleVat}" style="width:110px; text-align:right;">`)}
    ${col("수령자", txt("recipient", "수령자 이름"))}
    ${col("라인 메모", txt("memo", "판매상세비고"))}
  </div>
  <div class="muted" style="font-size:12px; margin-top:6px;" data-lcalc="${idx}">${saleCalcLine(c)}</div>
  <div class="muted" style="font-size:11.5px; margin-top:2px;">매입부가세는 총원가×${SALE_VAT_RATE}%로 자동 계산됩니다(TMS 산식). 정산 칸을 직접 고치면 그 값이 저장되고,
    <button class="btn btn-sm btn-ghost" data-lauto="${idx}" type="button" style="font-size:11.5px; padding:0 6px;">규칙값으로 되돌리기</button></div>`;
}
/* 편집 칸 → 라인 객체. 정산 칸(판매부가세·판매수수료)은 값만 담는다 — '직접 지정' 표시는 입력 이벤트가 켠다. */
function readSaleLineEdits(root, l) {
  $$("input[data-lk]", root).forEach((inp) => {
    const k = inp.dataset.lk;
    if (k === "feeRate") l.feeRate = inp.value === "" ? SALE_FEE_RATE : Number(inp.value) || 0;
    else if (inp.type === "number") l[k] = inp.value === "" ? 0 : Number(inp.value) || 0;
    else l[k] = inp.value.trim();
  });
  // 부호 선택 칸(탈거가): 금액은 절대값으로 받았으니 －(회수, 양수 저장)/＋(원가에 더함, 음수 저장)를 곱한다
  $$("select[data-lsign]", root).forEach((sel) => {
    const k = sel.dataset.lsign;
    l[k] = Math.abs(Number(l[k]) || 0) * (Number(sel.value) || 1);
  });
  return l;
}
/* 라인 편집 칸의 공통 배선 — 값 받기·정산 직접지정 표시·규칙값 되돌리기. onChange(l) 가 미리보기를 고친다. */
function wireSaleLineEdits(root, lines, onChange) {
  $$("input[data-lk]", root).forEach((inp) => inp.addEventListener("input", () => {
    const l = lines[Number(inp.dataset.li)];
    if (!l) return;
    if (inp.dataset.settle) l[inp.dataset.lk + "Manual"] = true;     // 사람이 직접 고친 정산 칸
    readSaleLineEdits(inp.closest("td") || root, l);
    onChange(l, Number(inp.dataset.li));
  }));
  $$("select[data-lsign]", root).forEach((sel) => sel.addEventListener("change", () => {
    const l = lines[Number(sel.dataset.li)];
    if (!l) return;
    readSaleLineEdits(sel.closest("td") || root, l);
    onChange(l, Number(sel.dataset.li));
  }));
  $$("button[data-lauto]", root).forEach((b) => b.addEventListener("click", () => {
    const l = lines[Number(b.dataset.lauto)];
    if (!l) return;
    l.saleVatManual = l.saleFeeManual = false;
    onChange(l, Number(b.dataset.lauto));
  }));
}
/* 미리보기 갱신 — 정산 칸이 규칙값이면 입력칸 값도 따라 바꾼다(다시 그리지 않는다: 커서 보존) */
function refreshSaleLineCalc(root, l, idx, assetCost) {
  const c = saleLineCalc(l, assetCost);
  const box = $(`[data-lcalc="${idx}"]`, root);
  if (box) box.innerHTML = saleCalcLine(c);
  const vat = $(`input[data-lk="saleVat"][data-li="${idx}"]`, root);
  const fee = $(`input[data-lk="saleFee"][data-li="${idx}"]`, root);
  if (vat && !l.saleVatManual && document.activeElement !== vat) vat.value = c.saleVat;
  if (fee && !l.saleFeeManual && document.activeElement !== fee) fee.value = c.saleFee;
  return c;
}

/* 자산번호 스캔/붙여넣기 → 서버 검사 → 넣을 수 있는 것만 돌려준다(거부 사유는 토스트) */
async function checkSaleAssets(nos, slipNo) {
  const r = await api("/api/sale-slips/check-assets", { method: "POST", body: { assetNos: nos, slipNo: slipNo || "" } });
  const bad = r.items.filter((x) => !x.ok);
  if (bad.length) toast(bad.map((x) => `${x.assetNo}: ${x.reason}`).join("\n"), true);
  return r.items.filter((x) => x.ok);
}

/* ＋ 새 전표 */
async function openSaleSlipForm(onDone) {
  let channels = [];
  try {
    await ensureMeta();                       // 자산 상태 칩 라벨(statusChip)용
    channels = (await api("/api/sale-slips?from=" + ymd(new Date(Date.now() - 86400000 * 365)))).channels || [];
  } catch (_e) { /* 채널 후보는 없어도 된다 */ }
  const host = document.createElement("div");
  host.classList.add("wide");
  const lines = [];      // {assetNo, model, statusLabel, warning, salePrice, ...옵션, open}
  host.innerHTML = `
    <div class="card in-modal">
      <div class="inline-row" style="margin-bottom:6px;">
        <h3 style="margin:0; flex:1;">＋ 판매 전표 등록
          <span class="muted" style="font-weight:400; font-size:13px;">전표번호는 저장할 때 자동(S날짜-500부터)</span></h3>
        <button class="btn btn-sm" id="se-close">닫기</button>
      </div>
      ${saleHeadFormHtml(null, channels, { manualTotal: true })}
      <div style="margin-top:14px; padding-top:10px; border-top:1px dashed var(--border);">
        <div class="inline-row" style="margin-bottom:6px;">
          <b>자산</b>
          <input type="text" id="se-scan" placeholder="자산번호 스캔·입력 후 Enter (여러 개는 붙여넣기)"
            style="min-width:280px;" autocomplete="off">
          <button class="btn btn-sm" id="se-add">추가</button>
          <span class="muted" style="font-size:12px;">렌탈 귀속·다른 전표/주문에 있는 자산은 거부됩니다</span>
        </div>
        <div id="se-lines"></div>
      </div>
      <div class="editor-actions">
        <span id="se-sum" class="muted" style="flex:1; font-size:13px;"></span>
        <button class="btn" id="se-cancel">취소</button>
        <button class="btn btn-primary" id="se-save">저장</button>
      </div>
    </div>`;
  openModalWith(host);
  attachAutocomplete($("#se-customer", host), "customer");   // 판매처 = 거래처 마스터의 '판매' 구분(자유 입력 유지, 2026-09-02)
  // 합계 칸만 제자리에서 고친다 — 옵션 칸을 옮겨 다니는 중에 표를 다시 그리면 커서가 사라진다
  const sumText = () => {
    if (!lines.length) return "";
    const cs = lines.map((l) => saleLineCalc(l, l.assetCost));
    return `${lines.length}대 · 판매가 합계 ${fmtWon(cs.reduce((s, c) => s + c.price, 0))}`
      + ` · 순이익 합계 ${fmtWon(cs.reduce((s, c) => s + c.profit, 0))}`;
  };
  const refreshSums = () => {
    lines.forEach((l, i) => {
      const tr = $(`tr[data-row="${i}"]`, host);
      if (!tr) return;
      const c = refreshSaleLineCalc(host, l, i, l.assetCost);
      tr.children[3].textContent = fmtWon(c.totalCost);
      tr.children[4].innerHTML = `<b style="${c.profit < 0 ? "color:var(--danger);" : ""}">${fmtWon(c.profit)}</b>`;
    });
    $("#se-sum", host).textContent = sumText();
  };
  const drawLines = () => {
    const box = $("#se-lines", host);
    $("#se-sum", host).textContent = sumText();
    if (!lines.length) { box.innerHTML = `<p class="muted" style="font-size:13px;">아직 자산이 없습니다 — 위 칸에 자산번호를 찍으세요.</p>`; return; }
    box.innerHTML = `<div class="table-wrap"><table>
      <thead><tr><th>자산번호</th><th>모델 · 상태</th><th style="text-align:right;">판매가</th>
        <th style="text-align:right;" title="자산 원가(매입가+수리·부품비) + 옵션 원가 + 포장료">총원가</th>
        <th style="text-align:right;" title="판매가 − 총원가 − 판매수수료 − 실부가세 (TMS 산식)">순이익</th><th></th></tr></thead>
      <tbody>${lines.map((l, i) => { const c = saleLineCalc(l, l.assetCost); return `
        <tr data-row="${i}">
          <td><b>${escapeHtml(l.assetNo)}</b></td>
          <td>${escapeHtml([l.maker, l.model].filter(Boolean).join(" ") || "-")}
            <span class="muted" style="font-size:12px;">${escapeHtml(l.statusLabel || "")} · 원가 ${fmtWon(l.assetCost)}</span>
            ${l.warning ? `<div class="chip chip-amber" style="font-size:11px;" title="${escapeHtml(l.warning)}">⚠ ${escapeHtml(l.warning.slice(0, 28))}${l.warning.length > 28 ? "…" : ""}</div>` : ""}</td>
          <td style="text-align:right;"><input type="text" data-money data-sp="${i}" value="${l.salePrice ? fmtNum(l.salePrice) : ""}"
            placeholder="0" style="width:120px; text-align:right;"></td>
          <td style="text-align:right;" class="muted">${fmtWon(c.totalCost)}</td>
          <td style="text-align:right;"><b style="${c.profit < 0 ? "color:var(--danger);" : ""}">${fmtWon(c.profit)}</b></td>
          <td style="white-space:nowrap;">
            <button class="btn btn-sm btn-ghost" data-opt="${i}" title="업그레이드·탈거·기타구성·충전기·포장료(원가) · 수수율·부가세">${l.open ? "옵션 ▴" : "옵션 ▾"}</button>
            <button class="btn btn-sm btn-ghost" data-rm="${i}" title="목록에서 뺍니다(아직 저장 전)">✕</button></td>
        </tr>
        ${l.open ? `<tr><td colspan="6" style="background:var(--bg);">${saleLineEditHtml(l, i, c)}</td></tr>` : ""}`; }).join("")}
      </tbody></table></div>`;
    $$("input[data-sp]", box).forEach((inp) => inp.addEventListener("input", () => {
      lines[Number(inp.dataset.sp)].salePrice = parseMoneyEl(inp);
      refreshSums();
    }));
    // 옵션 칸은 값만 받아 두고 합계만 고친다(다시 그리지 않는다 — 다음 칸으로 옮긴 커서를 지키기 위해)
    wireSaleLineEdits(box, lines, () => refreshSums());
    $$("button[data-opt]", box).forEach((b) => b.addEventListener("click", () => {
      lines[Number(b.dataset.opt)].open = !lines[Number(b.dataset.opt)].open; drawLines(); }));
    $$("button[data-rm]", box).forEach((b) => b.addEventListener("click", () => {
      lines.splice(Number(b.dataset.rm), 1); drawLines(); }));
  };
  drawLines();
  const addNos = async (nos) => {
    const fresh = nos.filter((n) => !lines.some((l) => l.assetNo === n));
    if (!fresh.length) { if (nos.length) toast("이미 목록에 있는 자산입니다.", true); return; }
    let ok;
    try { ok = await checkSaleAssets(fresh); } catch (err) { toast(err.message, true); return; }
    ok.forEach((a) => lines.push({
      assetNo: a.assetNo, maker: a.maker, model: a.model, statusLabel: a.statusLabel,
      warning: a.warning || "", salePrice: a.salePrice || 0,
      assetCost: (Number(a.purchasePrice) || 0) + (Number(a.repairSum) || 0),   // 미리보기용 자산 원가(서버가 다시 센다)
      upgrade1Price: 0, upgrade2Price: 0, removalPrice: 0, extraPrice: 0, chargerPrice: 0,
      packingFee: 0, feeRate: SALE_FEE_RATE, saleVat: 0, saleFee: 0, saleVatManual: false, saleFeeManual: false,
      upgrade1Item: "", upgrade2Item: "", removalItem: "",
      extraItems: "", recipient: "", memo: "", open: false }));
    drawLines();
  };
  const scan = $("#se-scan", host);
  const takeScan = () => { const nos = splitAssetNos(scan.value); scan.value = ""; if (nos.length) addNos(nos); scan.focus(); };
  scan.addEventListener("keydown", (e) => { if (isEnterKey(e)) { e.preventDefault(); takeScan(); } });
  scan.addEventListener("paste", () => setTimeout(takeScan, 0));
  $("#se-add", host).addEventListener("click", takeScan);
  $("#se-close", host).addEventListener("click", () => closeModal());
  $("#se-cancel", host).addEventListener("click", () => closeModal());
  $("#se-save", host).addEventListener("click", async () => {
    const head = readSaleHead(host, true);
    if (!lines.length && head.saleAmount == null) { toast("자산을 한 대 이상 넣거나 판매금액을 적어 주세요.", true); return; }
    if (lines.some((l) => !(Number(l.salePrice) > 0)) && !confirm("판매가가 0원인 자산이 있습니다. 그대로 저장할까요?")) return;
    const btn = $("#se-save", host);
    btn.disabled = true;
    try {
      const body = { ...head, lines: lines.map(saleLineBody) };
      const r = await api("/api/sale-slips", { method: "POST", body });
      toast(`판매 전표 ${r.slipNo} 등록 — ${r.slip.qty}대 · ${fmtWon(r.slip.saleAmount)}`
        + (r.warnings && r.warnings.length ? `\n⚠ ${r.warnings.join(" / ")}` : ""));
      closeModal();
      if (onDone) onDone();
      openSaleSlipDetail(r.slipNo, onDone);
    } catch (err) { toast(err.message, true); btn.disabled = false; }
  });
}

/* 반입·취소 확인창(2층 팝업) — 사유 + '실물 확인했으니 바로 재고로' 체크 */
function askSaleRelease(title, hint, onOk, needReason) {
  const host = document.createElement("div");
  host.innerHTML = `
    <div class="card in-modal" style="max-width:520px;">
      <h3 style="margin-top:0;">${title}</h3>
      <p class="muted" style="margin:4px 0 10px; font-size:13px;">${hint}</p>
      <label>사유${needReason ? " <span class=\"muted\">(필수 — 기록에 남습니다)</span>" : " <span class=\"muted\">(선택)</span>"}
        <input type="text" id="sr-reason" data-autofocus placeholder="예: 고객 변심 반품 / 오등록"></label>
      <label class="check-line" style="margin-top:8px;"><input type="checkbox" id="sr-restock">
        <span>실물을 확인했습니다 — <b>바로 재고(입고·실재고)로 되돌리기</b>
          <span class="muted">(끄면 '↩ 복귀 후보'에만 올라가고 사람이 검수 후 복귀합니다)</span></span></label>
      <div class="editor-actions">
        <button class="btn" id="sr-cancel">취소</button>
        <button class="btn btn-primary" id="sr-ok">확인</button>
      </div>
    </div>`;
  openModalOver(host);
  $("#sr-cancel", host).addEventListener("click", () => closeModalOver());
  $("#sr-ok", host).addEventListener("click", async () => {
    const reason = $("#sr-reason", host).value.trim();
    if (needReason && !reason) { toast("사유를 적어 주세요.", true); return; }
    const restock = $("#sr-restock", host).checked;
    $("#sr-ok", host).disabled = true;
    try { await onOk({ reason, restock }); closeModalOver(); }
    catch (err) { toast(err.message, true); $("#sr-ok", host).disabled = false; }
  });
}
function releaseToast(rel, what) {
  const parts = [what];
  if (rel && rel.restocked) parts.push(`${rel.assetNo} 재고 복귀(입고·실재고)`);
  else if (rel && rel.candidate) parts.push(`${rel.assetNo} ↩ 복귀 후보로 올렸습니다(검수 후 복귀)`);
  if (rel && rel.heldOrder) parts.push(`⚠ 주문 ${rel.heldOrder}이(가) 잡고 있어 상태는 그대로`);
  toast(parts.join(" · "));
}

/* 전표 상세 — 헤더·라인 편집, 반입/취소, 라인 추가 */
async function openSaleSlipDetail(slipNo, onDone) {
  const host = document.createElement("div");
  host.classList.add("wide");
  host.innerHTML = `<div class="card in-modal"><p class="muted">불러오는 중…</p></div>`;
  openModalWith(host);
  let channels = [];
  const draw = async () => {
    let s;
    try {
      await ensureMeta().catch(() => {});      // 자산 상태 칩 라벨(statusChip)용
      s = await api(`/api/sale-slips/${encodeURIComponent(slipNo)}`);
      if (!channels.length) channels = (await api("/api/sale-slips?from=" + ymd(new Date(Date.now() - 86400000 * 365))).catch(() => ({}))).channels || [];
    } catch (err) { host.innerHTML = `<div class="card in-modal"><p class="muted">${escapeHtml(err.message)}</p><div class="editor-actions"><button class="btn" id="sd2-close">닫기</button></div></div>`; $("#sd2-close", host).addEventListener("click", () => closeModal()); return; }
    const canEdit = hasPerm("purchase.edit") && s.stage !== "판매취소";
    const canUndoSlip = hasPerm("purchase.edit") && s.owsUndoable;   // OWS에서 취소한 전표만(연동 취소는 TMS 정본)
    const chip = (label, val, cls) => `<span class="chip ${cls || "chip-slate"}">${label} ${val}</span>`;
    // 상세 라인 편집 상태 — 저장된 정산 칸이 규칙값과 다르면 '직접 지정'으로 본다(판매가를 고쳐도 그 값은 지킨다)
    const edits = s.lines.map((l) => ({
      ...l, assetCost: l.cost,
      saleVatManual: l.saleVat !== halfUp((l.salePrice || 0) * SALE_VAT_RATE / 100),
      saleFeeManual: l.saleFee !== halfUp((l.salePrice || 0) * (Number(l.feeRate) || 0) / 100),
    }));
    host.innerHTML = `
      <div class="card in-modal">
        <div class="inline-row" style="margin-bottom:6px;">
          <h3 style="margin:0; flex:1;">${escapeHtml(s.slipNo)} ${saleStageChip(s.stage)} ${slipSourceChip(s)}
            ${s.owsEditedAt ? `<span class="muted" style="font-size:12px; font-weight:400;">OWS 수정 ${escapeHtml(s.owsEditedAt.slice(0, 16).replace("T", " "))} ${escapeHtml(s.owsEditedBy || "")}</span>` : ""}
            ${s.cancelDate ? `<span class="chip chip-red">🚫 취소 ${escapeHtml(s.cancelDate)}${s.cancelReason ? " · " + escapeHtml(s.cancelReason) : ""}</span>` : ""}
          </h3>
          ${canUndoSlip ? `<button class="btn btn-sm" id="sd2-uncancel-slip" title="전표 취소를 되돌립니다 — 함께 취소된 라인이 살아나고 자산은 판매 상태로 돌아갑니다">↩ 취소 되돌리기</button>` : ""}
          <button class="btn btn-sm" id="sd2-close">닫기</button>
        </div>
        ${s.stage === "판매취소" && !s.owsUndoable ? `<p class="muted" style="font-size:12.5px; margin:0 0 8px;">TMS에서 취소된 연동 전표입니다 — TMS 값이 정본이라 OWS에서 되돌리지 않습니다.</p>` : ""}
        <div class="inline-row" style="gap:6px; flex-wrap:wrap; margin-bottom:8px;">
          ${chip("수량", (s.qty || 0) + "대", "chip-blue")}
          ${chip("판매금액", fmtWon(s.saleAmount), "chip-green")}
          ${chip("명세합계", fmtWon(s.itemSaleSum))}
          ${s.diffAmount ? chip("차액", fmtWon(s.diffAmount), "chip-amber") : ""}
          ${chip("매입", fmtWon(s.purchaseAmount))}
          ${chip("순이익", fmtWon(s.profit))}
          ${s.paidConfirmed ? chip("입금", (s.paidStatus || "완납"), "chip-green")
            : chip("입금", `${fmtWon(s.paidAmount)} / 미수 ${fmtWon(Math.max(0, (s.saleAmount || 0) - (s.paidAmount || 0)))}`, "chip-red")}
        </div>
        ${canEdit ? saleHeadFormHtml(s, channels, { manualTotal: s.isOws }) : `
          <div class="muted" style="font-size:13px;">${escapeHtml([s.channel, s.customer, s.saleDate, s.waybill].filter(Boolean).join(" · "))}
            ${s.memo ? `<div>📌 ${escapeHtml(s.memo)}</div>` : ""}</div>`}
        ${canEdit ? `<div class="editor-actions">
          <button class="btn btn-primary" id="sd2-save">헤더 저장</button>
          <button class="btn btn-ghost" id="sd2-cancel-slip" title="전표를 취소로 표시합니다(지우지 않습니다)">🚫 전표 취소</button>
        </div>` : ""}
      </div>
      <div class="card in-modal">
        <div class="inline-row" style="margin-bottom:6px;">
          <h3 style="margin:0; flex:1;">자산 ${s.liveCount}대
            <span class="muted" style="font-weight:400; font-size:13px;">${s.lines.length - s.liveCount ? `· 반입/취소 ${s.lines.length - s.liveCount}대` : ""}</span></h3>
          ${canEdit ? `<input type="text" id="sd2-scan" placeholder="자산번호 추가 (Enter)" style="min-width:220px;" autocomplete="off">
            <button class="btn btn-sm" id="sd2-add">추가</button>` : ""}
        </div>
        ${s.lines.length ? `<div class="table-wrap"><table>
          <thead><tr><th>자산번호</th><th>모델</th><th>자산 상태</th>
            <th style="text-align:right;">판매가</th>
            <th style="text-align:right;" title="업1+업2+기타구성+충전기 − 탈거(회수)">옵션 원가</th>
            <th style="text-align:right;" title="자산 원가(매입가+수리·부품비) + 옵션 원가 + 포장료">총원가</th>
            <th style="text-align:right;" title="판매수수료 + 실부가세(판매부가세 − 매입부가세)">수수료·부가세</th>
            <th style="text-align:right;" title="판매가 − 총원가 − 판매수수료 − 실부가세 (TMS 산식)">순이익</th><th>라인 상태</th>${canEdit ? "<th></th>" : ""}</tr></thead>
          <tbody>${s.lines.map((l, i) => {
            const dead = l.stage === "판매취소" || l.stage === "반입";
            const undo = canEdit && dead && l.owsUndoable
              ? (l.stage === "반입"
                ? `<button class="btn btn-sm btn-ghost" data-lunret="${escapeHtml(l.assetNo)}" title="반입 표시를 해제하고 판매 상태로 되돌립니다">↩ 반입 취소</button>`
                : `<button class="btn btn-sm btn-ghost" data-luncancel="${escapeHtml(l.assetNo)}" title="판매취소 표시를 되돌립니다">↩ 취소 되돌리기</button>`)
              : "";
            return `<tr style="${dead ? "opacity:.6;" : ""}">
              <td><b>${escapeHtml(l.assetNo)}</b>${l.recipient ? `<div class="muted" style="font-size:11.5px;">${escapeHtml(l.recipient)}</div>` : ""}</td>
              <td>${escapeHtml(l.model || "-")}${l.grade ? ` <span class="muted" style="font-size:11.5px;">${escapeHtml(l.grade)}</span>` : ""}</td>
              <td>${l.status ? statusChip(l.status) : '<span class="muted">미매칭</span>'}
                ${l.heldOrder ? `<div class="chip chip-violet" style="font-size:11px;">주문 ${escapeHtml(l.heldOrder)}</div>` : ""}
                ${l.tmsDeletedAt ? '<div class="chip chip-red" style="font-size:11px;">TMS 삭제</div>' : ""}</td>
              <td style="text-align:right;"><b>${fmtWon(l.salePrice)}</b></td>
              <td style="text-align:right;" class="muted">${fmtWon(l.optionCost)}${l.packingFee ? `<div style="font-size:11px;">포장 ${fmtWon(l.packingFee)}</div>` : ""}</td>
              <td style="text-align:right;" class="muted" title="자산 원가 ${fmtWon(l.cost)}">${fmtWon(l.totalCost)}</td>
              <td style="text-align:right;" class="muted" title="판매수수료 ${fmtWon(l.saleFee)}(${l.feeRate}%) · 판매부가세 ${fmtWon(l.saleVat)} − 매입부가세 ${fmtWon(l.buyVat)}">${fmtWon((l.saleFee || 0) + (l.netVat || 0))}</td>
              <td style="text-align:right;"><b style="${l.profit < 0 ? "color:var(--danger);" : ""}" title="${l.profitSrc === "tms" ? "TMS 순이익(정본)" : "OWS 계산(TMS 산식)"}">${fmtWon(l.profit)}</b>${l.profitSrc === "tms" ? '<div class="muted" style="font-size:10.5px;">TMS</div>' : ""}</td>
              <td>${saleStageChip(l.stage)}${l.returnDate ? `<div class="muted" style="font-size:11px;">반입 ${escapeHtml(l.returnDate)}</div>` : ""}${l.cancelDate ? `<div class="muted" style="font-size:11px;">취소 ${escapeHtml(l.cancelDate)}</div>` : ""}</td>
              ${canEdit ? `<td style="white-space:nowrap;">${dead ? undo : `
                <button class="btn btn-sm btn-ghost" data-ledit="${i}" title="판매가·옵션 원가·정산 수정">✏️</button>
                <button class="btn btn-sm btn-ghost" data-lret="${escapeHtml(l.assetNo)}" title="반입(고객이 돌려보냄)">↩ 반입</button>
                <button class="btn btn-sm btn-ghost" data-lcancel="${escapeHtml(l.assetNo)}" title="이 라인을 판매취소로 표시">✕</button>`}</td>` : ""}
            </tr>
            <tr data-lform="${i}" hidden><td colspan="${canEdit ? 10 : 9}" style="background:var(--bg);">
              <div class="inline-row" style="margin:0 0 8px;">
                <label class="muted" style="font-size:12.5px;">판매가 <input type="text" data-money data-lsp="${i}" value="${fmtNum(l.salePrice)}" style="width:120px; text-align:right;"></label>
                <span class="muted" style="font-size:12px;">자산 원가 ${fmtWon(l.cost)} (매입 ${fmtWon(l.purchasePrice)} + 수리·부품 ${fmtWon(l.repairSum)})</span>
              </div>
              ${saleLineEditHtml(edits[i], i)}
              <div class="inline-row" style="margin:8px 0 0;">
                <button class="btn btn-sm btn-primary" data-lsave="${i}" data-no="${escapeHtml(l.assetNo)}">라인 저장</button>
                <button class="btn btn-sm" data-lclose="${i}">닫기</button>
              </div></td></tr>`; }).join("")}
          </tbody></table></div>` : `<p class="muted">자산 라인이 없습니다${canEdit ? " — 위 칸에 자산번호를 찍어 추가하세요" : ""}.</p>`}
        ${s.isOws ? `<p class="muted" style="font-size:12px; margin:8px 0 0;">OWS 전표는 수량·명세합계(Σ판매가)·매입금액·순이익이 살아 있는 라인에서 자동으로 계산됩니다(TMS 산식).
          라인 순이익 = 판매가 − 총원가(자산 원가 + 옵션 원가 + 포장료) − 판매수수료(판매가×수수율) − 실부가세(판매가×10% − 총원가×10%).
          전표 순이익 = 라인 순이익 합 − 헤더 부가세·수수료·배송비(판매차이금액은 TMS처럼 넣지 않습니다). 반입·취소 라인은 합계에서 빠집니다.</p>`
          : `<p class="muted" style="font-size:12px; margin:8px 0 0;">TMS 연동 전표입니다 — 헤더 금액은 TMS가 정한 값이라 라인에서 다시 세지 않습니다. 고친 칸은 연동이 덮지 않습니다.</p>`}
      </div>`;
    $("#sd2-close", host).addEventListener("click", () => closeModal());
    const reload = async () => { await draw(); if (onDone) onDone(); };
    // ↩ 전표 취소 되돌리기(취소된 전표에서만 보인다) — 그 사이 자산이 다른 전표·주문에 잡혔으면 서버가 409로 막는다
    $("#sd2-uncancel-slip", host)?.addEventListener("click", async () => {
      const n = s.lines.filter((l) => l.stage === "판매취소" && l.cancelKind !== "line").length;
      if (!confirm(`전표 ${slipNo}의 취소를 되돌립니다.\n함께 취소된 라인 ${n}대가 살아나고 자산은 판매(출고완료) 상태로 돌아갑니다. 계속할까요?`)) return;
      try {
        const r = await api(`/api/sale-slips/${encodeURIComponent(slipNo)}/uncancel`, { method: "POST", body: {} });
        const warn = r.assets.filter((a) => a.warning).map((a) => `${a.assetNo}: ${a.warning}`);
        toast(`전표 ${slipNo} 취소 되돌림 — 라인 ${r.assets.length}대` + (warn.length ? `\n⚠ ${warn.join(" / ")}` : ""));
        reload();
      } catch (err) { toast(err.message, true); }
    });
    if (!canEdit) return;
    attachAutocomplete($("#se-customer", host), "customer");   // 판매처 자동완성(거래처 마스터 '판매' 구분)
    $("#sd2-save", host).addEventListener("click", async () => {
      const body = readSaleHead(host, s.isOws);
      try {
        const r = await api(`/api/sale-slips/${encodeURIComponent(slipNo)}`, { method: "PATCH", body });
        toast(r.changed && r.changed.length ? `저장했습니다 (${r.changed.length}칸)` : "바뀐 내용이 없습니다.");
        reload();
      } catch (err) { toast(err.message, true); }
    });
    $("#sd2-cancel-slip", host).addEventListener("click", () => askSaleRelease(
      `🚫 전표 취소 — ${escapeHtml(slipNo)}`,
      "살아 있는 자산 라인이 전부 '판매취소'로 표시됩니다. 전표는 지우지 않고 취소로 남습니다(금액은 취소분으로 따로 보입니다).",
      async ({ reason, restock }) => {
        const r = await api(`/api/sale-slips/${encodeURIComponent(slipNo)}/cancel`, { method: "POST", body: { reason, restock } });
        toast(`전표 ${slipNo} 취소 — 라인 ${r.assets.length}대` + (restock ? " · 재고 복귀" : (r.assets.length ? " · ↩ 복귀 후보" : "")));
        reload();
      }, true));
    const addLine = async () => {
      const inp = $("#sd2-scan", host);
      const nos = splitAssetNos(inp.value);
      inp.value = "";
      if (!nos.length) return;
      let ok;
      try { ok = await checkSaleAssets(nos, slipNo); } catch (err) { toast(err.message, true); return; }
      if (!ok.length) return;
      try {
        const r = await api(`/api/sale-slips/${encodeURIComponent(slipNo)}/lines`, { method: "POST",
          body: { lines: ok.map((a) => ({ assetNo: a.assetNo, salePrice: a.salePrice || 0 })) } });
        toast(`${r.added.length}대 추가 — 판매가는 ✏️로 고치세요` + (r.warnings.length ? `\n⚠ ${r.warnings.join(" / ")}` : ""));
        reload();
      } catch (err) { toast(err.message, true); }
    };
    $("#sd2-add", host).addEventListener("click", addLine);
    $("#sd2-scan", host).addEventListener("keydown", (e) => { if (isEnterKey(e)) { e.preventDefault(); addLine(); } });
    $$("button[data-ledit]", host).forEach((b) => b.addEventListener("click", () => {
      const row = $(`tr[data-lform="${b.dataset.ledit}"]`, host); row.hidden = !row.hidden; }));
    $$("button[data-lclose]", host).forEach((b) => b.addEventListener("click", () => {
      $(`tr[data-lform="${b.dataset.lclose}"]`, host).hidden = true; }));
    // 라인 편집 칸 — 값이 바뀌면 순이익 미리보기를 고친다(정산 칸은 규칙값이면 자동 갱신)
    wireSaleLineEdits(host, edits, (l, i) => refreshSaleLineCalc(host, l, i, l.assetCost));
    $$("input[data-lsp]", host).forEach((inp) => inp.addEventListener("input", () => {
      const i = Number(inp.dataset.lsp);
      edits[i].salePrice = parseMoneyEl(inp);
      refreshSaleLineCalc(host, edits[i], i, edits[i].assetCost);
    }));
    $$("button[data-lsave]", host).forEach((b) => b.addEventListener("click", async () => {
      const i = Number(b.dataset.lsave);
      const row = $(`tr[data-lform="${i}"]`, host);
      readSaleLineEdits(row, edits[i]);
      edits[i].salePrice = parseMoneyEl($(`input[data-lsp="${i}"]`, host));
      const { id, assetNo, assetId, model, grade, status, statusLabel, division, tmsDeletedAt, heldOrder, stage,
              saleDate, shipDate, returnDate, cancelDate, cancelKind, prevStage, owsUndoable, buyVat, netVat,
              optionCost, cost, totalCost, purchasePrice, repairSum, profit, profitSrc, source, owsEditedAt,
              ...rest } = edits[i];
      const body = saleLineBody(rest);
      try {
        const r = await api(`/api/sale-slips/${encodeURIComponent(slipNo)}/lines/${encodeURIComponent(b.dataset.no)}`, { method: "PATCH", body });
        toast(r.changed.length ? `라인 저장 (${r.changed.length}칸)` : "바뀐 내용이 없습니다.");
        reload();
      } catch (err) { toast(err.message, true); }
    }));
    // ↩ 반입 취소 / 취소 되돌리기 — OWS에서 한 반입·취소만 버튼이 뜬다(owsUndoable)
    $$("button[data-lunret]", host).forEach((b) => b.addEventListener("click", async () => {
      if (!confirm(`${b.dataset.lunret}의 반입 표시를 해제하고 판매 상태로 되돌립니다. 계속할까요?`)) return;
      try {
        const r = await api(`/api/sale-slips/${encodeURIComponent(slipNo)}/lines/${encodeURIComponent(b.dataset.lunret)}/unreturn`, { method: "POST", body: {} });
        toast(`${b.dataset.lunret} 반입 취소 — 자산 ${r.asset.statusChanged ? "출고완료로 복원" : "상태 그대로"}` + (r.asset.warning ? `\n⚠ ${r.asset.warning}` : ""));
        reload();
      } catch (err) { toast(err.message, true); }
    }));
    $$("button[data-luncancel]", host).forEach((b) => b.addEventListener("click", async () => {
      if (!confirm(`${b.dataset.luncancel}의 판매취소를 되돌립니다. 계속할까요?`)) return;
      try {
        const r = await api(`/api/sale-slips/${encodeURIComponent(slipNo)}/lines/${encodeURIComponent(b.dataset.luncancel)}/uncancel`, { method: "POST", body: {} });
        toast(`${b.dataset.luncancel} 취소 되돌림 — 자산 ${r.asset.statusChanged ? "출고완료로 복원" : "상태 그대로"}` + (r.asset.warning ? `\n⚠ ${r.asset.warning}` : ""));
        reload();
      } catch (err) { toast(err.message, true); }
    }));
    $$("button[data-lret]", host).forEach((b) => b.addEventListener("click", () => askSaleRelease(
      `↩ 반입 — ${escapeHtml(b.dataset.lret)}`,
      "고객이 돌려보낸 기계입니다. 라인은 '반입'으로 표시되고 전표 합계에서 빠집니다. 재고는 검수 전에는 자동으로 되살리지 않습니다.",
      async ({ reason, restock }) => {
        const r = await api(`/api/sale-slips/${encodeURIComponent(slipNo)}/lines/${encodeURIComponent(b.dataset.lret)}/return`,
          { method: "POST", body: { reason, restock, returnDate: ymd() } });
        releaseToast(r.asset, `${b.dataset.lret} 반입 처리`);
        reload();
      })));
    $$("button[data-lcancel]", host).forEach((b) => b.addEventListener("click", () => askSaleRelease(
      `✕ 라인 판매취소 — ${escapeHtml(b.dataset.lcancel)}`,
      "이 자산을 전표에서 뺍니다. 지우지 않고 '판매취소'로 표시되며 전표 합계에서 빠집니다.",
      async ({ reason, restock }) => {
        const r = await api(`/api/sale-slips/${encodeURIComponent(slipNo)}/lines/${encodeURIComponent(b.dataset.lcancel)}`,
          { method: "DELETE", body: { reason, restock } });
        releaseToast(r.asset, `${b.dataset.lcancel} 판매취소`);
        reload();
      })));
  };
  await draw();
}

function renderMigrate(body) {
  // ★자동 반영이 주인공(대표 2026-08-26 "스케줄러가 돌고 있으니 버튼은 필요없어,
  //   마지막 반영이 언제인지만 확인하고 누르면 되게") — 수동 업로드는 접이식으로 격하.
  body.innerHTML = `
    <div class="card" style="max-width:820px;" id="mg-auto">
      <h3>TMS 자동 반영 <span class="muted" style="font-weight:400;">— 185 추출(2시간)과 폴더 감시(10분)가 알아서 최신화합니다</span></h3>
      <div id="mg-auto-body" class="muted">불러오는 중…</div>
    </div>
    <div class="card" style="max-width:820px;" id="mg-cutover">
      <h3>🚧 판매·매입 창구 마감 — 이중입력 경보
        <span class="muted" style="font-weight:400;">— 마감일 뒤 TMS에 새 전표·자산이 들어오면 여기와 첫 화면 '오늘 할 일'에 뜹니다</span></h3>
      <div id="mg-cutover-body" class="muted">불러오는 중…</div>
    </div>
    <div class="card" style="max-width:820px;" id="mg-outbox">
      <h3>⤴ OWS → TMS 되돌려 쓰기
        <span class="muted" style="font-weight:400;">— OWS에서 고친 자산 수리비를 TMS 재고내역에 넣습니다</span></h3>
      <div id="mg-outbox-body" class="muted">불러오는 중…</div>
    </div>
    <details class="card" style="max-width:820px;">
      <summary style="cursor:pointer;"><b>수동 엑셀 이관</b>
        <span class="muted">— 평소엔 쓸 일 없습니다(자동 반영이 대신합니다). 특별한 파일을 손으로 넣을 때만.</span></summary>
      <div style="margin-top:10px;">
      <h3 style="display:none;">TMS 자산 데이터 이관</h3>
      <p class="muted">TMS에서 매입내역/재고를 엑셀로 내보낸 뒤 올리면 관리번호와 스펙을 그대로 가져옵니다.
      이미 등록된 관리번호는 건너뛰므로 여러 번 실행해도 중복되지 않습니다.</p>
      <label class="check-line" style="margin-top:8px;">
        <input type="checkbox" id="mg-fill">
        <span><b>이미 있는 자산의 빈 칸 채우기</b>
          <span class="muted">— 관리번호만 먼저 등록돼 매입가·스펙이 비어 있는 자산에 엑셀 값을 넣습니다.
          <b>이미 값이 있는 칸은 건드리지 않습니다.</b></span></span>
      </label>
      <p class="muted">TMS가 등급 칸에 넣어둔 <b>수리·A/S·불량·도색대기</b>는 OWS에서 자산 <b>상태</b>로 옮기고,
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
    </details>`;
  renderCutover();
  renderTmsOutbox();
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
      : "엑셀의 자산을 OWS에 등록합니다. 계속할까요?";
    if (!confirm(msg)) return;
    send("/api/assets/migrate");
  });
  ensureMeta();
}
