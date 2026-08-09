/* HMS 프론트엔드 — Phase 0: 인증 / 쉘 / 설정(사용자·권한·카테고리·감사로그·백업) */
"use strict";

const $ = (sel, el) => (el || document).querySelector(sel);
const $$ = (sel, el) => Array.from((el || document).querySelectorAll(sel));

const state = {
  user: null,
  view: "dashboard",
  settingsTab: "users",
  registry: null, // 권한 레지스트리 {menus:[...], flat:[...]}
  categories: [],
  users: [],
  editingUserId: undefined, // undefined=닫힘, null=신규, number=수정
  renderSeq: 0,   // 늦게 도착한 비동기 응답이 다른 화면에 쓰는 것을 차단
  pages: {},      // 목록별 현재 페이지 (공용 페이지 넘김)
};

/* ---------------- 제품코드 자동완성 ----------------

   대표 요청(2026-08-05): "모든 제품코드가 들어가는 곳에" 자동완성.

   ★같은 물건에 코드를 조금씩 다르게 적으면(840 G3_i7-6_내장 / 840G3_i7-6_내장)
     셋팅 화면의 재고 대조가 통째로 어긋난다. 쓰던 코드를 그대로 다시 고르게 하는 게 핵심.

   쓰는 법:
     attachCodeLookup("no-sku", (c) => { ...고른 코드로 할 일... });
   입력칸 옆에 목록 상자를 만들려면 codeLookupField(id, label)로 마크업을 찍는다. */
function codeLookupField(id, label, value = "", ph = "예: 840 G3_i7-6_내장") {
  return `<label style="position:relative;">${label}
      <input type="text" id="${id}" value="${escapeHtml(value)}" autocomplete="off"
             placeholder="${escapeHtml(ph)}">
      <div id="${id}-list" class="code-list"></div>
    </label>`;
}

function attachCodeLookup(id, onPick) {
  const input = $("#" + id);
  if (!input) return;
  let box = $("#" + id + "-list");
  if (!box) {                     // 마크업을 못 넣는 자리(표 안 등)는 상자를 만들어 붙인다
    box = document.createElement("div");
    box.id = id + "-list";
    box.className = "code-list";
    const host = input.parentElement;
    if (getComputedStyle(host).position === "static") host.style.position = "relative";
    host.appendChild(box);
  }
  let timer = null, items = [], cursor = -1, seq = 0;
  const close = () => { box.style.display = "none"; items = []; cursor = -1; };
  const paint = () => {
    box.innerHTML = items.length ? items.map((c, i) => `
      <div class="code-item${i === cursor ? " on" : ""}">
        <b>${escapeHtml(c.code)}</b>
        <span class="chip ${c.shippable ? "chip-green" : "chip-slate"}">출고가능 ${c.shippable}</span>
        <span class="muted">보유 ${c.total}</span>
        ${c.model ? `<div class="muted" style="font-size:12px;">${escapeHtml(c.model)}</div>` : ""}
        ${c.options.length ? `<div class="muted" style="font-size:11.5px;">옵션: ${
          escapeHtml(c.options.slice(0, 2).join(" · "))}</div>` : ""}
      </div>`).join("")
      : `<div class="muted" style="padding:8px 10px;">쓰고 있는 코드가 없습니다 — 새로 적으셔도 됩니다.</div>`;
    box.style.display = "block";
    $$(".code-item", box).forEach((el, i) =>
      el.addEventListener("mousedown", (e) => { e.preventDefault(); pick(items[i]); }));
  };
  const pick = (c) => {
    if (!c) return;
    input.value = c.code;
    close();
    if (onPick) onPick(c);
  };
  const search = async () => {
    const my = ++seq;
    try {
      const r = await api("/api/product-codes?q=" + encodeURIComponent(input.value.trim()));
      if (my !== seq) return;              // 늦게 온 응답이 최신 목록을 덮지 않게
      items = r.codes || []; cursor = -1;
      paint();
    } catch { close(); }
  };
  input.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(search, 200); });
  input.addEventListener("focus", () => { clearTimeout(timer); timer = setTimeout(search, 120); });
  input.addEventListener("blur", () => setTimeout(close, 150));
  input.addEventListener("keydown", (e) => {
    if (box.style.display !== "block" || !items.length) return;
    if (e.key === "ArrowDown") { e.preventDefault(); cursor = Math.min(items.length - 1, cursor + 1); paint(); }
    else if (e.key === "ArrowUp") { e.preventDefault(); cursor = Math.max(0, cursor - 1); paint(); }
    else if (e.key === "Enter" && cursor >= 0) { e.preventDefault(); pick(items[cursor]); }
    else if (e.key === "Escape") close();
  });
}

/* ---------------- 긴 목록 페이지 넘김 ----------------

   대표 지시(2026-08-05): "너무 길면 아래로 스크롤 및 리소스 낭비가 크니
   적당한 선에서 잘라주고 앞뒤로 이동할 수 있는 화살표로 전부 바꿔줘."

   쓰는 법 — 목록마다 고유한 key를 준다(화면이 여러 개 떠 있어도 안 섞이게):
     const pg = paged("assets", rows);
     body.innerHTML = `... ${pg.rows.map(...)} ... ${pg.bar}`;
     wirePager(body, "assets", () => 다시그리기());

   ★필터·검색을 바꿨으면 resetPage(key)를 먼저 불러야 한다.
     안 그러면 3페이지를 보던 중 검색해서 2건만 남았을 때 빈 화면이 나온다. */
const PAGE_SIZE = 50;

function resetPage(key) { state.pages[key] = 0; }

function paged(items, key, size) {
  const list = items || [];
  const per = size || PAGE_SIZE;
  const total = list.length;
  const last = Math.max(0, Math.ceil(total / per) - 1);
  const page = Math.min(Math.max(0, state.pages[key] || 0), last);
  state.pages[key] = page;
  return {
    rows: list.slice(page * per, page * per + per),
    total, page, last,
    bar: pagerBar(key, page, last, total, per),
  };
}

function pagerBar(key, page, last, total, per) {
  if (total <= per) return "";          // 한 쪽에 다 들어가면 아무것도 안 그린다
  const from = page * per + 1;
  const to = Math.min(total, (page + 1) * per);
  const b = (act, label, on, title) =>
    `<button class="btn btn-sm" data-pg="${act}" ${on ? "" : "disabled"}
       title="${title}">${label}</button>`;
  return `<div class="pager" data-pager="${key}" data-last="${last}">
      ${b("first", "«", page > 0, "맨 앞")}
      ${b("prev", "◀", page > 0, "이전")}
      <span class="pager-at">${from.toLocaleString("ko-KR")}–${to.toLocaleString("ko-KR")}
        <span class="muted">/ ${total.toLocaleString("ko-KR")}건</span></span>
      ${b("next", "▶", page < last, "다음")}
      ${b("last", "»", page < last, "맨 뒤")}
    </div>`;
}

function wirePager(root, key, redraw) {
  const bar = $(`[data-pager="${key}"]`, root);
  if (!bar) return;
  const last = Number(bar.dataset.last) || 0;
  $$("button[data-pg]", bar).forEach((btn) => btn.addEventListener("click", () => {
    const p = state.pages[key] || 0;
    state.pages[key] = { first: 0, prev: Math.max(0, p - 1),
                         next: Math.min(last, p + 1), last }[btn.dataset.pg];
    redraw();
  }));
}

/* ---------------- 공통 ---------------- */

/* 로컬(한국) 날짜를 YYYY-MM-DD로.
   toISOString()은 UTC로 바꿔서 한국 시간 오전 9시 이전이면 하루 전 날짜가 나온다 —
   기간 조회가 통째로 하루씩 밀리므로 날짜 계산에는 반드시 이 함수를 쓴다. */
function ymd(d) {
  const x = d || new Date();
  const p = (n) => String(n).padStart(2, "0");
  return `${x.getFullYear()}-${p(x.getMonth() + 1)}-${p(x.getDate())}`;
}

/* 검색창 — 엔터를 안 눌러도 타이핑을 멈추면 알아서 조회한다(대표 2026-08-05:
   "엔터 치는 것은 번거롭다").

   ★글자마다 부르지 않는다. 잠깐(기본 400ms) 멈춘 뒤 한 번만 부른다 —
     '노트북'을 치면 조회가 3번이 아니라 1번 나간다.
   ★**한글은 조합 중에 값이 계속 바뀐다**(ㅅ → 사 → 삼). 조합이 끝나기 전에 부르면
     엉뚱한 낱자로 검색되고 결과가 깜빡인다. isComposing 중에는 미루고,
     compositionend에서 다시 잡는다.
   ★엔터는 그대로 동작한다(기다리지 않고 즉시). 값이 그대로면 부르지 않는다. */
function autoSearch(sel, run, ms) {
  const el = typeof sel === "string" ? $(sel) : sel;
  if (!el) return;
  let timer = null;
  let last = el.value;
  const fire = () => {
    if (el.value === last) return;         // 안 바뀌었으면 헛호출하지 않는다
    last = el.value;
    run();
  };
  const bump = (e) => {
    if (e && e.isComposing) return;        // 한글 조합 중 — 끝나면 다시 온다
    clearTimeout(timer);
    timer = setTimeout(fire, ms || 400);
  };
  el.addEventListener("input", bump);
  el.addEventListener("compositionend", () => bump());
  el.addEventListener("keydown", (e) => {
    if (e.key !== "Enter" || e.isComposing) return;
    clearTimeout(timer);
    last = el.value;
    run();
  });
}

/* 목록 체크박스에 Shift 다중선택을 붙인다(대표 요청 2026-08-04: 선택되는 곳은 전부).

   attachShiftPick(root, ".af-pick", onChange)
   - 한 칸 누른 뒤 다른 칸을 Shift+클릭하면 그 사이가 전부 눌린 칸과 같은 상태가 된다.
   - 기준점(anchor)은 '마지막으로 Shift 없이 누른 칸'이다. 화면을 다시 그리면 초기화된다.
   - ★root 안에서만 이어진다. 표가 여러 개인 화면에서 전역으로 잡으면 다른 표까지
     번져서 남의 자산이 선택된다.
   - onChange는 범위 선택으로 값이 바뀐 뒤 한 번만 부른다(칸마다 부르면 목록이 여러 번 다시 그려진다). */
function attachShiftPick(root, selector, onChange) {
  const boxes = $$(selector, root);
  if (!boxes.length) return;
  let anchor = null;
  boxes.forEach((cb, i) => cb.addEventListener("click", (e) => {
    if (e.shiftKey && anchor !== null && anchor !== i) {
      const [from, to] = [Math.min(anchor, i), Math.max(anchor, i)];
      for (let k = from; k <= to; k++) {
        if (boxes[k].disabled || boxes[k].checked === cb.checked) continue;
        boxes[k].checked = cb.checked;
        // change 이벤트를 듣는 화면(주문·자산 목록)이 있어 직접 알려 준다
        boxes[k].dispatchEvent(new Event("change", { bubbles: true }));
      }
    }
    anchor = i;
    if (onChange) onChange();
  }));
}

function escapeHtml(s) {
  return String(s == null ? "" : s)
    .replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;").replaceAll("'", "&#39;");
}

function toast(msg, isErr) {
  const el = document.createElement("div");
  el.className = "toast" + (isErr ? " err" : "");
  el.textContent = msg;
  $("#toasts").appendChild(el);
  setTimeout(() => el.remove(), 5000);
}

async function api(path, opts) {
  const o = Object.assign({ headers: {} }, opts || {});
  if (o.body !== undefined && typeof o.body !== "string") {
    o.body = JSON.stringify(o.body);
    o.headers["Content-Type"] = "application/json";
  }
  const res = await fetch(path, o);
  let data = null;
  try { data = await res.json(); } catch (_e) { /* 비JSON 응답 */ }
  if (res.status === 401 && path !== "/api/auth/login") {
    state.user = null;
    showAuth("login");
    const e401 = new Error((data && data.error) || "로그인이 필요합니다.");
    e401.status = 401;
    throw e401;
  }
  if (!res.ok) {
    const err = new Error((data && data.error) || `요청 실패 (${res.status})`);
    err.status = res.status;
    err.data = data; // 409 응답의 최신 order 등을 호출부가 활용
    throw err;
  }
  return data;
}

function hasPerm(p) {
  return !!state.user && (state.user.isAdmin || state.user.perms.includes(p));
}

/* ---------------- 인증 화면 ---------------- */

function showAuth(mode) {
  $("#app").classList.add("hidden");
  $("#auth-screen").classList.remove("hidden");
  $("#login-form").classList.toggle("hidden", mode !== "login");
  $("#setup-form").classList.toggle("hidden", mode !== "setup");
  const first = mode === "login" ? $("#login-username") : $("#setup-username");
  if (first) setTimeout(() => first.focus(), 50);
}

function canSeeMenu(menu) {
  if (!state.user) return false;
  if (state.user.isAdmin) return true;
  return (state.user.menus || []).includes(menu);
}

/* 권한 없는 메뉴는 숨기고, 현재 화면이 접근 불가면 첫 허용 메뉴로 보낸다 */
function applyMenuPermissions() {
  let firstAllowed = "dashboard";
  $$("#nav a[data-view]").forEach((a) => {
    const v = a.dataset.view;
    const ok = v === "dashboard" || canSeeMenu(v);
    a.classList.toggle("hidden", !ok);
    if (ok && firstAllowed === "dashboard" && v !== "dashboard") firstAllowed = firstAllowed;
  });
  const allowed = $$("#nav a[data-view]").filter((a) => !a.classList.contains("hidden"));
  if (!allowed.some((a) => a.dataset.view === state.view)) {
    state.view = allowed.length ? allowed[0].dataset.view : "dashboard";
  }
  $$("#nav a[data-view]").forEach((a) => a.classList.toggle("active", a.dataset.view === state.view));
}

function showApp() {
  $("#auth-screen").classList.add("hidden");
  $("#app").classList.remove("hidden");
  $("#chip-name").textContent = state.user.displayName;
  $("#chip-role").textContent = state.user.isAdmin
    ? "관리자"
    : `메뉴 ${(state.user.menus || []).length}개 · 권한 ${state.user.perms.length}개`;
  applyMenuPermissions();
  startGlobalNotifications();
  renderView();
}

/* 다른 사용자의 작업(체크/취소/가져오기/송장)을 토스트로 알림 — 감사로그 파생 */
const NOTIF_TEXT = {
  order_created: "주문 등록", order_updated: "주문 수정", order_stage: "단계 변경",
  order_cancelled: "주문 취소", order_restored: "취소 복구",
  order_assets_matched: "자산 매칭", orders_imported: "주문 가져오기", waybill_issued: "송장 발행",
};

function startGlobalNotifications() {
  if (state.notifTimer) clearInterval(state.notifTimer);
  // 주문 데이터를 보는 화면이 하나라도 열려 있으면 알림을 받는다(셋팅·배송 담당자 포함)
  if (!["orders", "setup", "shipping"].some(canSeeMenu)) return;
  state.lastNotifTs = null;
  const poll = async () => {
    try {
      const rows = await api("/api/notifications");
      if (!rows.length) return;
      if (state.lastNotifTs === null) { state.lastNotifTs = rows[0].ts; return; } // 첫 폴링은 기준점만
      const fresh = rows.filter((r) => r.ts > state.lastNotifTs && r.username !== state.user.displayName);
      if (rows[0].ts > state.lastNotifTs) state.lastNotifTs = rows[0].ts;
      fresh.slice(0, 3).forEach((r) =>
        toast(`${r.username} — ${NOTIF_TEXT[r.action] || r.action}: ${r.target || ""}`));
    } catch (_e) { /* 폴링 오류 무시 */ }
  };
  poll();
  state.notifTimer = setInterval(poll, 10000);
}

$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    state.user = await api("/api/auth/login", {
      method: "POST",
      body: { username: $("#login-username").value.trim(), password: $("#login-password").value },
    });
    $("#login-password").value = "";
    showApp();
  } catch (err) { toast(err.message, true); }
});

$("#setup-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const pw = $("#setup-password").value, pw2 = $("#setup-password2").value;
  if (pw !== pw2) { toast("비밀번호 확인이 일치하지 않습니다.", true); return; }
  try {
    state.user = await api("/api/auth/setup", {
      method: "POST",
      body: {
        username: $("#setup-username").value.trim(),
        displayName: $("#setup-displayname").value.trim(),
        password: pw,
      },
    });
    toast("관리자 계정이 생성되었습니다.");
    showApp();
  } catch (err) {
    toast(err.message, true);
    // 이미 다른 곳에서 초기 설정이 끝난 경우 — 로그인 화면으로 전환(데드엔드 방지)
    if (err.status === 409) showAuth("login");
  }
});

$("#logout-btn").addEventListener("click", async () => {
  try { await api("/api/auth/logout", { method: "POST" }); } catch (_e) { /* 무시 */ }
  state.user = null;
  showAuth("login");
});

/* ---------------- 테마 ---------------- */

function applyTheme(t) {
  document.documentElement.setAttribute("data-theme", t === "dark" ? "dark" : "");
  localStorage.setItem("hms-theme", t);
}
$("#theme-toggle").addEventListener("click", () => {
  applyTheme(localStorage.getItem("hms-theme") === "dark" ? "light" : "dark");
});
applyTheme(localStorage.getItem("hms-theme") || "light");

/* ---------------- 내비게이션 ---------------- */

$("#nav").addEventListener("click", (e) => {
  const a = e.target.closest("a[data-view]");
  if (!a) return;
  e.preventDefault();
  state.view = a.dataset.view;
  $$("#nav a").forEach((x) => x.classList.toggle("active", x === a));
  renderView();
});

/** 다른 화면으로 이동 — 사이드바를 누른 것과 똑같이 동작한다(활성 표시 포함). */
function go(view) {
  const a = $(`#nav a[data-view="${view}"]`);
  if (a) a.click();
}

function clearPollers() {
  (state.pollers || []).forEach(clearInterval);
  state.pollers = [];
  // 화면을 옮기면 주문 일괄바도 사라진다 — 아래 여백만 남아 있으면 안 된다
  document.body.classList.remove("has-bulkbar");
}
function addPoller(fn, ms) {
  (state.pollers = state.pollers || []).push(setInterval(fn, ms));
}

function renderView() {
  clearPollers();
  ++state.renderSeq;
  const main = $("#main");
  switch (state.view) {
    case "dashboard": return renderDashboard(main);
    case "purchase": return renderPurchaseView(main);
    case "orders": return renderOrdersView(main);
    case "setup": return renderSetupView(main);
    case "shipping": return renderShippingView(main);
    case "as": return renderAsView(main);
    case "reports": return renderReportsView(main);
    case "settings": return renderSettings(main);
  }
}

function renderPlaceholder(main, title, phase, desc) {
  main.innerHTML = `
    <h1 class="page-title">${escapeHtml(title)}</h1>
    <p class="page-desc">아직 열리지 않은 메뉴입니다.</p>
    <div class="card placeholder">
      <div class="ph-badge">${escapeHtml(phase)} 개발 예정</div>
      <p>${escapeHtml(desc)}</p>
    </div>`;
}

/* ---------------- 대시보드 ---------------- */

async function renderDashboard(main) {
  main.innerHTML = `
    <h1 class="page-title">대시보드</h1>
    <p class="page-desc">환영합니다, ${escapeHtml(state.user.displayName)}님</p>
    <div class="kpi-row" id="kpi-row">
      <div class="kpi" data-kpi="purchase" style="cursor:pointer;" title="매입 ▸ 자산(재고)으로 이동">
        <div class="kpi-label">보유 재고</div><div class="kpi-value" id="kpi-stock">-</div></div>
      <div class="kpi" data-kpi="orders" style="cursor:pointer;" title="주문관리로 이동">
        <div class="kpi-label">진행 중 주문</div><div class="kpi-value" id="kpi-orders">-</div></div>
      <div class="kpi" data-kpi="shipping" style="cursor:pointer;" title="배송 / 송장으로 이동">
        <div class="kpi-label">오늘 출고</div><div class="kpi-value" id="kpi-shipped">-</div></div>
      <div class="kpi"><div class="kpi-label">서버 상태</div><div class="kpi-value" id="kpi-health">확인 중…</div></div>
    </div>
    <div class="card" id="dash-todo"><h3>오늘 할 일</h3><p class="muted">불러오는 중…</p></div>
    ${state.user.isAdmin || hasPerm("users.manage") ? `
    <div class="card">
      <h3>시작하기</h3>
      <p class="muted">설정 → 사용자 관리에서 직원 계정을 만들고, 각자 사용할 <b>메뉴</b>를 지정하세요.
      체크하지 않은 메뉴는 그 사람 화면에 나타나지 않습니다.</p>
    </div>` : ""}`;
  // 숫자를 누르면 그 숫자가 어디서 나온 화면으로 간다 — 예전엔 눌러도 아무 일이 없었다
  $$("[data-kpi]", main).forEach((el) => el.addEventListener("click", () => {
    const target = el.dataset.kpi;
    if (!canSeeMenu(target)) { toast("이 메뉴를 볼 권한이 없습니다.", true); return; }
    go(target);
  }));
  try {
    const h = await api("/api/health");
    $("#kpi-health").textContent = h.ok ? "정상" : "오류";
  } catch (_e) {
    const el = $("#kpi-health");
    if (el) el.textContent = "오류";
  }
  loadDashboardStats();
}

async function loadDashboardStats() {
  const todo = [];
  // 재고
  if (hasPerm("purchase.view")) {
    try {
      const rows = await api("/api/assets/summary");
      // 재고에서 빠진 것 — 취소·반품도 우리 물건이 아니다(매입 화면과 같은 기준).
      const gone = ["shipped", "scrapped", "cancelled", "returned"];
      const stock = rows.filter((r) => !gone.includes(r.status)).reduce((s, r) => s + r.count, 0);
      const el = $("#kpi-stock"); if (el) el.textContent = stock + "대";
      const ready = rows.filter((r) => r.status === "ready").reduce((s, r) => s + r.count, 0);
      const work = rows.filter((r) => ["refurbishing", "repair", "as", "painting"].includes(r.status))
        .reduce((s, r) => s + r.count, 0);
      // 자산 탭은 보기가 넷이라 지정하지 않으면 집계 화면이 열려 개별 자산이 안 보인다
      if (work) todo.push({ text: `정비·수리 중인 자산 <b>${work}대</b>`,
                            view: "purchase", tab: "assets", assetView: "list" });
      if (!ready && stock) todo.push({ text: "판매가능 상태인 자산이 없습니다 — 상태를 확인하세요.",
                                       view: "purchase", tab: "assets", assetView: "list" });
    } catch (_e) { /* 권한/오류 시 표시 생략 */ }
  }
  // 주문·출고
  if (["orders", "setup", "shipping"].some(canSeeMenu)) {
    try {
      const res = await api("/api/orders?view=active");
      const orders = res.orders || [];
      const active = orders.filter((o) => !o.shippingDone);
      const elO = $("#kpi-orders"); if (elO) elO.textContent = active.length + "건";
      // ★오늘 출고는 서버에 따로 묻는다. 위 목록은 진행 중(view=active)만이라
      //   출고 직후 '보관'으로 정리한 건이 빠져 하루 종일 0건으로 보였다(2026-07-31).
      try {
        const t = await api("/api/orders/today");
        const elS = $("#kpi-shipped"); if (elS) elS.textContent = (t.shippedToday || 0) + "건";
      } catch (_e) { /* 못 세면 이전 값을 그대로 둔다 — 0으로 덮어쓰지 않는다 */ }
      const waiting = active.filter((o) => !o.productionDone).length;
      const qcDone = active.filter((o) => o.productionDone && o.softwareInspectionDone
        && !(o.waybills || []).some((w) => w.status !== "canceled")).length;
      const unmatched = active.filter((o) => o.productionDone && !o.assets.length).length;
      const pendingAddr = orders.filter((o) => o.pendingShippingUpdate).length;
      if (waiting) todo.push({ text: `제작 대기 주문 <b>${waiting}건</b>`, view: "setup" });
      if (unmatched) todo.push({ text: `자산 미매칭 주문 <b>${unmatched}건</b>`, view: "setup" });
      if (qcDone) todo.push({ text: `송장 발급 대기 <b>${qcDone}건</b>`,
                              view: "shipping", shipTab: "ready" });
      if (pendingAddr) todo.push({ text: `배송지 변경 확인 필요 <b>${pendingAddr}건</b>`,
                                   view: "orders" });
    } catch (_e) { /* 권한 없으면 생략 */ }
  }
  const host = $("#dash-todo");
  if (host) {
    // ★항목을 누르면 그 일을 하는 화면으로 바로 간다.
    //   글자만 있으면 직원이 매번 메뉴를 찾아 들어가 조건을 다시 걸어야 한다.
    host.innerHTML = `<h3>오늘 할 일</h3>` + (todo.length
      ? `<ul style="margin:6px 0 0 18px; line-height:1.9;">${todo.map((t, i) =>
          `<li><a href="#" data-todo="${i}" style="color:var(--primary); text-decoration:none;">${t.text} <span class="muted" style="font-size:12px;">→ 바로가기</span></a></li>`).join("")}</ul>`
      : `<p class="muted">지금 처리할 일이 없습니다.</p>`);
    $$("a[data-todo]", host).forEach((a) => a.addEventListener("click", (e) => {
      e.preventDefault();
      const t = todo[Number(a.dataset.todo)];
      if (!t || !t.view) return;
      if (t.tab) state.purchaseTab = t.tab;
      if (t.assetView) state.assetView = t.assetView;
      // 배송 화면은 탭이 셋이라, 지정하지 않으면 직전에 보던 [회수/반품]이 열려
      // '송장 발급 대기 28건'이 어디에도 없는 상태가 된다(2026-07-29 전수조사)
      if (t.shipTab) state.shipTab = t.shipTab;
      go(t.view);
    }));
  }
}

/* ---------------- 설정 ---------------- */

function settingsTabs() {
  const tabs = [];
  if (hasPerm("users.manage")) tabs.push(["users", "사용자 관리"]);
  if (hasPerm("settings.manage")) {
    // ★탭을 줄인다(대표 지시 2026-08-04).
    //   · 제품 분류: 매입 › 기준정보 › 거래처 / 분류와 같은 화면이라 없앴다.
    //   · 제공 옵션: 몰에서 파는 옵션이라 [API 관리] 안으로 넣었다.
    tabs.push(["api", "API 관리"], ["stocksync", "📦 재고연동"], ["settlement", "정산"],
              ["migrate", "데이터 이관"]);
  }
  // 실적은 작업자 본인도 봐야 한다 — 설정 권한이 없어도 들어올 수 있게
  if (hasPerm("settings.manage") || hasPerm("reports.view") || hasPerm("orders.work")) {
    tabs.push(["sales", "매출 / 실적"]);
  }
  if (hasPerm("audit.view")) tabs.push(["audit", "감사 로그"]);
  if (hasPerm("settings.manage")) tabs.push(["backup", "백업"]);
  tabs.push(["account", "내 계정"]);
  return tabs;
}

function renderSettings(main) {
  const tabs = settingsTabs();
  // 옛 탭 이름으로 들어와도(북마크·이전 상태) 길을 잃지 않게 옮겨 준다
  const MOVED = { setupstats: ["sales", "stats"], prep: ["api", "prep"],
                  cats: ["api", "shop"] };
  if (MOVED[state.settingsTab]) {
    const [tab, view] = MOVED[state.settingsTab];
    state.settingsTab = tab;
    if (tab === "sales") state.salesView = view; else state.apiView = view;
  }
  if (!tabs.some(([k]) => k === state.settingsTab)) state.settingsTab = tabs[0][0];
  main.innerHTML = `
    <h1 class="page-title">설정</h1>
    <p class="page-desc">계정·권한·카테고리·시스템 관리</p>
    <div class="tabs">${tabs.map(([k, label]) =>
      `<button data-tab="${k}" class="${k === state.settingsTab ? "active" : ""}">${escapeHtml(label)}</button>`).join("")}
    </div>
    <div id="tab-body"></div>`;
  $$(".tabs button", main).forEach((b) => b.addEventListener("click", () => {
    state.settingsTab = b.dataset.tab;
    state.editingUserId = undefined;
    renderSettings(main);
  }));
  const body = $("#tab-body");
  switch (state.settingsTab) {
    case "users": return renderUsersTab(body);
    case "api": return renderApiTab(body);
    case "stocksync": return renderStockSyncTab(body);
    case "settlement": return renderSettlementTab(body);
    case "sales": return renderSalesTab(body);
    case "migrate": return renderMigrateTab(body);
    case "audit": return renderAuditTab(body);
    case "backup": return renderBackupTab(body);
    case "account": return renderAccountTab(body);
  }
}

/* 매출 / 실적 — 판매 전표(얼마 팔렸나)와 셋팅 실적(누가 몇 대 했나)을 한자리에.
   ★판매 전표는 원래 매입 탭에 있었는데, 매입 화면에 매출이 있는 게 어색하고
     탭만 늘어서 여기로 합쳤다(대표 지시 2026-08-04). */
const SALES_VIEWS = [["slips", "판매 전표"], ["stats", "셋팅 실적"]];

function renderSalesTab(body) {
  // 실적만 볼 수 있는 작업자에게 판매 금액까지 보여줄 이유는 없다
  const canMoney = hasPerm("settings.manage") || hasPerm("reports.view");
  const views = SALES_VIEWS.filter(([k]) => canMoney || k !== "slips");
  if (!views.some(([k]) => k === state.salesView)) state.salesView = views[0][0];
  body.innerHTML = `
    <div class="subtabs">${views.map(([k, l]) =>
      `<button data-sview="${k}" class="${k === state.salesView ? "active" : ""}">${l}</button>`).join("")}</div>
    <div id="sview-body"></div>`;
  $$("button[data-sview]", body).forEach((b) => b.addEventListener("click", () => {
    state.salesView = b.dataset.sview;
    renderSalesTab(body);
  }));
  const host = $("#sview-body", body);
  if (state.salesView === "slips") renderSaleSlips(host);   // purchase.js
  else renderSetupStatsTab(host);
}

/* ----- 셋팅 실적 -----
   "제품을 준비해서 검수완료까지 끝낸 것"을 한 대로 센다(대표 정의 2026-07-29).
   담당자별로 일·주·월·분기·연도 실적을 보고, 캘린더에서 날짜별 대수를 확인한다. */

const SETUP_PERIODS = [["day", "일"], ["week", "주"], ["month", "월"],
                       ["quarter", "분기"], ["year", "연도"]];

function shiftAnchor(kind, iso, dir) {
  const d = new Date(iso + "T12:00:00");
  if (kind === "day") { d.setDate(d.getDate() + dir); return ymd(d); }
  if (kind === "week") { d.setDate(d.getDate() + 7 * dir); return ymd(d); }
  // ★월·분기·연도는 '며칠'을 먼저 1일로 내려놓고 옮긴다.
  //   31일에 setMonth를 그냥 부르면 '6월 31일'이 7월 1일로 넘어가, 7/31에 [이전]을 눌러도
  //   6월이 아니라 다시 7월이 떴다(1/31에 [다음]은 2월을 건너뛰고 3월). 2026-07-31 감사.
  //   기간 계산에는 그 달의 아무 날이나 있으면 되므로 1일로 맞춰도 결과는 같다.
  d.setDate(1);
  if (kind === "month") d.setMonth(d.getMonth() + dir);
  else if (kind === "quarter") d.setMonth(d.getMonth() + 3 * dir);
  else d.setFullYear(d.getFullYear() + dir);
  return ymd(d);   // ★toISOString은 UTC라 오전 9시 이전에는 하루 밀린다
}

async function renderSetupStatsTab(body) {
  const seq = ++state.renderSeq;
  if (!state.setupStats) {
    state.setupStats = { period: "month", anchor: ymd() };   // ★UTC 변환 금지 — 오전 9시 이전에 하루 밀린다
  }
  const { period, anchor } = state.setupStats;
  body.innerHTML = `<p class="muted">불러오는 중…</p>`;
  let d;
  try {
    d = await api(`/api/reports/setup-stats?period=${period}&date=${anchor}`);
    if (seq !== state.renderSeq) return;
  } catch (err) {
    if (seq === state.renderSeq) body.innerHTML = `<p class="muted">${escapeHtml(err.message)}</p>`;
    return;
  }

  const diff = d.previous.diff;
  const diffHtml = diff === 0 ? '<span class="muted">지난 기간과 같음</span>'
    : `<span style="color:${diff > 0 ? "var(--primary)" : "var(--danger)"};">
         ${diff > 0 ? "▲" : "▼"} ${Math.abs(diff)}대</span>
       <span class="muted">(${escapeHtml(d.previous.label)} ${d.previous.units}대)</span>`;

  body.innerHTML = `
    <div class="card">
      <div class="inline-row" style="margin:0;">
        <h3 style="margin:0; flex:1;">📅 셋팅 실적</h3>
        <div class="tabs" style="border:0; margin:0;">
          ${SETUP_PERIODS.map(([k, label]) =>
            `<button data-sp="${k}" class="${k === period ? "active" : ""}">${label}</button>`).join("")}
        </div>
      </div>
      <p class="muted" style="margin:6px 0 0;">
        제품을 준비해서 <b>출고 확인까지 끝낸 것</b>을 한 대로 셉니다. 대수는 매칭한 자산 수 기준입니다.</p>
      <div class="inline-row" style="margin-top:10px;">
        <button class="btn btn-sm" id="ss-prev">◀ 이전</button>
        <b style="font-size:15px;">${escapeHtml(d.period.label)}</b>
        <button class="btn btn-sm" id="ss-next">다음 ▶</button>
        <button class="btn btn-ghost btn-sm" id="ss-today">오늘</button>
      </div>
      <div class="kpi-row" style="margin-top:12px;">
        <div class="kpi"><div class="kpi-label">셋팅 대수</div>
          <div class="kpi-value">${d.total.units}대</div>
          <div class="kpi-sub">${diffHtml}</div></div>
        <div class="kpi"><div class="kpi-label">주문 건수</div>
          <div class="kpi-value">${d.total.orders}건</div></div>
        <div class="kpi"><div class="kpi-label">참여 담당자</div>
          <div class="kpi-value">${d.total.staffCount}명</div></div>
      </div>
    </div>

    <div class="card">
      <h3>담당자별</h3>
      ${d.staff.length ? `<div class="table-wrap"><table>
        <thead><tr><th>담당자</th><th>셋팅 대수</th><th>주문 건수</th><th>검수</th><th>비중</th></tr></thead>
        <tbody>${d.staff.map((x) => {
          const pct = d.total.units ? Math.round(x.units / d.total.units * 100) : 0;
          return `<tr>
            <td><b>${escapeHtml(x.name)}</b></td>
            <td><b style="color:var(--primary);">${x.units}대</b></td>
            <td>${x.orders}건</td>
            <td class="muted">${x.inspected ? x.inspected + "대" : "-"}</td>
            <td><div style="background:var(--slate-soft); border-radius:6px; height:14px; min-width:80px;">
              <div style="background:var(--primary); height:14px; border-radius:6px; width:${pct}%;"></div>
            </div><span class="muted" style="font-size:11px;">${pct}%</span></td>
          </tr>`;
        }).join("")}</tbody>
        <tfoot><tr><th>합계</th><th>${d.total.units}대</th><th>${d.total.orders}건</th><th></th><th></th></tr></tfoot>
      </table></div>`
        : `<p class="muted">이 기간에 출고 확인된 셋팅이 없습니다.</p>`}
      <p class="muted" style="margin-top:8px; font-size:12px;">
        '셋팅 대수'는 제작을 완료한 사람 기준입니다. 검수만 맡은 경우는 [검수] 칸에 따로 표시되며
        합계에는 더하지 않습니다(같은 제품을 두 번 세지 않기 위해서입니다).</p>
    </div>

    <div class="card">
      <h3>날짜별 <span class="muted" style="font-size:13px;">— 날짜를 누르면 그날 목록이 열립니다</span></h3>
      <div id="ss-cal"></div>
      <div id="ss-day"></div>
    </div>`;

  $$("button[data-sp]", body).forEach((b) => b.addEventListener("click", () => {
    state.setupStats.period = b.dataset.sp;
    renderSetupStatsTab(body);
  }));
  $("#ss-prev").addEventListener("click", () => {
    state.setupStats.anchor = shiftAnchor(period, anchor, -1);
    renderSetupStatsTab(body);
  });
  $("#ss-next").addEventListener("click", () => {
    state.setupStats.anchor = shiftAnchor(period, anchor, 1);
    renderSetupStatsTab(body);
  });
  $("#ss-today").addEventListener("click", () => {
    state.setupStats.anchor = ymd();
    renderSetupStatsTab(body);
  });
  renderSetupCalendar(d);
}

/* 달력 — 기간이 한 달을 넘으면(분기·연도) 날짜 대신 '월별 막대'로 보여 준다 */
function renderSetupCalendar(d) {
  const host = $("#ss-cal");
  if (!host) return;
  const byDate = {};
  (d.calendar || []).forEach((c) => { byDate[c.date] = c; });
  const from = new Date(d.period.from + "T12:00:00");
  const to = new Date(d.period.to + "T12:00:00");
  const days = Math.round((to - from) / 86400000) + 1;

  if (days > 31) {                       // 분기·연도 → 월별 요약
    const byMonth = {};
    (d.calendar || []).forEach((c) => {
      const m = c.date.slice(0, 7);
      byMonth[m] = (byMonth[m] || 0) + c.units;
    });
    const months = Object.keys(byMonth).sort();
    const max = Math.max(1, ...Object.values(byMonth));
    host.innerHTML = months.length ? `<div class="table-wrap"><table>
      <thead><tr><th>월</th><th>셋팅 대수</th><th></th></tr></thead>
      <tbody>${months.map((m) => `<tr>
        <td>${escapeHtml(m.replace("-", "년 "))}월</td>
        <td><b>${byMonth[m]}대</b></td>
        <td><div style="background:var(--primary); height:14px; border-radius:6px;
             width:${Math.round(byMonth[m] / max * 100)}%; min-width:4px;"></div></td>
      </tr>`).join("")}</tbody></table></div>`
      : `<p class="muted">기록이 없습니다.</p>`;
    return;
  }

  // 한 달 이하 → 달력 격자. ★기간 그대로 그린다(주가 달을 넘으면 8/1·8/2가 사라졌었다).
  //   달 단위일 때는 1일부터, 주·일 단위일 때는 조회 시작일부터.
  const monthly = d.period.type === "month";
  const gridFrom = monthly ? new Date(from.getFullYear(), from.getMonth(), 1) : from;
  const gridTo = monthly
    ? new Date(from.getFullYear(), from.getMonth() + 1, 0)
    : to;
  const total = Math.round((gridTo - gridFrom) / 86400000) + 1;
  const pad = gridFrom.getDay();
  const cells = [];
  for (let i = 0; i < pad; i++) cells.push('<div></div>');
  for (let n = 0; n < total; n++) {
    const cur = new Date(gridFrom.getFullYear(), gridFrom.getMonth(), gridFrom.getDate() + n);
    const day = cur.getDate();
    const iso = `${cur.getFullYear()}-${String(cur.getMonth() + 1).padStart(2, "0")}-${String(day).padStart(2, "0")}`;
    const hit = byDate[iso];
    const inRange = iso >= d.period.from && iso <= d.period.to;
    const units = hit ? hit.units : 0;
    cells.push(`<div data-ssday="${iso}" style="border:1px solid var(--border); border-radius:8px;
      padding:6px 4px; min-height:52px; cursor:${units ? "pointer" : "default"};
      background:${units ? "var(--primary-soft)" : "var(--surface)"};
      opacity:${inRange ? 1 : 0.35};">
      <div style="font-size:11px; color:var(--text-dim);">${day}</div>
      ${units ? `<div style="font-weight:700; color:var(--primary);">${units}대</div>
        <div style="font-size:10px; color:var(--text-dim); overflow:hidden;">
          ${escapeHtml(Object.keys(hit.byStaff).slice(0, 2).join(", "))}</div>` : ""}
    </div>`);
  }
  host.innerHTML = `
    <div style="display:grid; grid-template-columns:repeat(7,1fr); gap:4px; margin-bottom:6px;">
      ${["일", "월", "화", "수", "목", "금", "토"].map((w) =>
        `<div class="muted" style="text-align:center; font-size:12px;">${w}</div>`).join("")}
    </div>
    <div style="display:grid; grid-template-columns:repeat(7,1fr); gap:4px;">${cells.join("")}</div>`;

  $$("div[data-ssday]", host).forEach((el) => el.addEventListener("click", async () => {
    const iso = el.dataset.ssday;
    if (!byDate[iso]) return;
    const out = $("#ss-day");
    out.innerHTML = `<p class="muted" style="margin-top:10px;">불러오는 중…</p>`;
    try {
      const r = await api(`/api/reports/setup-day?date=${iso}`);
      revealPanel(out);      // 아래에 그려져 몇 픽셀만 보이던 문제
      out.innerHTML = `
        <div style="border-top:1px solid var(--border); margin-top:12px; padding-top:10px;">
          <div class="inline-row" style="margin:0;">
            <b style="flex:1;">${escapeHtml(iso)} — ${r.units}대</b>
            <button class="btn btn-ghost btn-sm" id="ss-dayclose">닫기</button>
          </div>
          <div class="table-wrap" style="margin-top:8px;"><table>
            <thead><tr><th>시각</th><th>쇼핑몰</th><th>상품</th><th>수취인</th><th>대수</th><th>제작</th><th>검수</th></tr></thead>
            <tbody>${r.orders.map((o) => `<tr>
              <td class="muted">${escapeHtml(o.at)}</td>
              <td>${escapeHtml(o.channel || "-")}</td>
              <td style="max-width:220px;">${escapeHtml((o.productName || "").slice(0, 40))}</td>
              <td>${escapeHtml(o.recipient || "")}</td>
              <td><b>${o.units}대</b></td>
              <td>${escapeHtml(o.productionBy || "-")}</td>
              <td class="muted">${escapeHtml(o.inspectionBy || "-")}</td>
            </tr>`).join("")}</tbody>
          </table></div>
        </div>`;
      $("#ss-dayclose").addEventListener("click", () => { out.innerHTML = ""; });
    } catch (err) { out.innerHTML = `<p class="muted">${escapeHtml(err.message)}</p>`; }
  }));
}

/* ----- 제공 옵션 -----
   쇼핑몰이 옵션을 못 거는 경우(카카오쇼핑 등) 우리가 직접 「이 주문에는 이것도 챙긴다」를
   지정한다. 셋팅·QC 화면에 체크 목록으로 뜨고, 다 체크해야 제작 완료로 넘어간다.
   송장에도 함께 찍혀 포장 담당이 한 번 더 대조한다. */

async function renderPrepTab(body) {
  const seq = ++state.renderSeq;
  body.innerHTML = `<p class="muted">불러오는 중…</p>`;
  let data, channels;
  try {
    [data, channels] = await Promise.all([
      api("/api/prep-options"), api("/api/order-channels").catch(() => [])]);
    if (seq !== state.renderSeq) return;
  } catch (err) {
    if (seq === state.renderSeq) body.innerHTML = `<p class="muted">${escapeHtml(err.message)}</p>`;
    return;
  }
  const chNames = (channels || []).map((c) => c.channel).filter(Boolean);
  const matchTypes = data.matchTypes || [];

  body.innerHTML = `
    <div class="card">
      <h3>🧩 제공 옵션</h3>
      <p class="muted" style="line-height:1.7;">
        고도몰처럼 옵션을 걸 수 있는 몰은 주문서에 옵션이 실려 옵니다. 그런데
        <b>카카오쇼핑처럼 옵션 자체를 만들 수 없는 몰</b>은 우리가 제공하는 옵션인데도
        주문서에 아무 표시가 없어 셋팅·QC가 놓치기 쉽습니다.<br>
        여기서 지정해 두면 <b>셋팅 화면에 체크 목록</b>으로 뜨고,
        <b>다 체크해야 제작 완료</b>로 넘어갑니다. <b>송장에도 함께 인쇄</b>돼 포장할 때 한 번 더 대조합니다.
      </p>
      <div class="inline-row" style="margin-top:14px;">
        <input type="text" id="po-name" placeholder="옵션 이름 (예: 리브레오피스 설치)" style="min-width:240px;">
        <input type="text" id="po-note" placeholder="작업자용 안내 (선택)" style="min-width:220px;">
        <button class="btn btn-primary btn-sm" id="po-add">옵션 추가</button>
      </div>
    </div>
    <div id="po-list"></div>`;

  const renderList = (options) => {
    const host = $("#po-list");
    if (!host) return;
    if (!options.length) {
      host.innerHTML = `<div class="card placeholder">
        <p>아직 만든 옵션이 없습니다.</p>
        <p class="muted">위에서 「리브레오피스 설치」처럼 옵션을 먼저 만들고,<br>
           그 아래에 어느 쇼핑몰 주문에 붙일지 조건을 더하면 됩니다.</p></div>`;
      return;
    }
    host.innerHTML = options.map((o) => `
      <div class="card" data-opt="${o.id}">
        <div class="inline-row" style="justify-content:space-between;">
          <div>
            <b style="font-size:15px;">${escapeHtml(o.name)}</b>
            ${o.enabled ? "" : ' <span class="chip chip-slate">사용 안 함</span>'}
            ${o.note ? `<div class="muted" style="font-size:13px;">${escapeHtml(o.note)}</div>` : ""}
          </div>
          <div class="inline-row" style="margin:0;">
            <button class="btn btn-sm" data-toggle="${o.id}">${o.enabled ? "사용 안 함" : "다시 사용"}</button>
            <button class="btn btn-sm btn-danger" data-del="${o.id}">삭제</button>
          </div>
        </div>
        <div style="margin-top:10px;">
          ${o.rules.length ? o.rules.map((r) => `
            <div class="inline-row" style="margin-bottom:6px;">
              <span class="chip ${r.enabled ? "chip-green" : "chip-slate"}">${escapeHtml(r.label)}</span>
              <button class="btn btn-ghost btn-sm" data-rule-del="${r.id}">조건 삭제</button>
            </div>`).join("")
            : `<p class="muted" style="margin:0 0 8px;">조건이 없어 아직 어느 주문에도 붙지 않습니다. 아래에서 추가하세요.</p>`}
        </div>
        <div class="inline-row" style="border-top:1px dashed var(--border); padding-top:10px;">
          <select data-ch="${o.id}">
            <option value="">쇼핑몰 선택…</option>
            ${chNames.map((c) => `<option value="${escapeHtml(c)}">${escapeHtml(c)}</option>`).join("")}
          </select>
          <select data-mt="${o.id}">
            ${matchTypes.map((m) => `<option value="${m.value}">${escapeHtml(m.label)}</option>`).join("")}
          </select>
          <input type="text" data-mv="${o.id}" placeholder="포함될 값 (예: RAM16)" style="min-width:150px;">
          <button class="btn btn-sm" data-preview="${o.id}">몇 건에 붙나 보기</button>
          <button class="btn btn-sm btn-primary" data-rule-add="${o.id}">조건 추가</button>
        </div>
        <div class="muted" data-pv="${o.id}" style="margin-top:6px;"></div>
      </div>`).join("");
    bind(options);
  };

  const ruleBody = (id) => ({
    channel: $(`[data-ch="${id}"]`).value,
    matchType: $(`[data-mt="${id}"]`).value,
    matchValue: $(`[data-mv="${id}"]`).value.trim(),
  });

  const bind = (options) => {
    $$("[data-toggle]").forEach((b) => b.addEventListener("click", async () => {
      const o = options.find((x) => x.id === Number(b.dataset.toggle));
      try {
        await api(`/api/prep-options/${o.id}`, { method: "PATCH", body: { enabled: !o.enabled } });
        renderPrepTab(body);
      } catch (err) { toast(err.message, true); }
    }));
    $$("[data-del]").forEach((b) => b.addEventListener("click", async () => {
      const o = options.find((x) => x.id === Number(b.dataset.del));
      if (!confirm(`「${o.name}」을(를) 삭제할까요?\n\n`
        + "지금까지 누가 챙겼는지 체크한 기록도 함께 사라집니다.\n"
        + "잠시 안 쓸 거라면 [사용 안 함]으로 꺼 두는 쪽이 안전합니다.")) return;
      try {
        const r = await api(`/api/prep-options/${o.id}`, { method: "DELETE" });
        toast(r.removedChecks ? `삭제했습니다(체크 기록 ${r.removedChecks}건 포함).` : "삭제했습니다.");
        renderPrepTab(body);
      } catch (err) { toast(err.message, true); }
    }));
    $$("[data-rule-del]").forEach((b) => b.addEventListener("click", async () => {
      try {
        await api(`/api/prep-option-rules/${Number(b.dataset.ruleDel)}`, { method: "DELETE" });
        renderPrepTab(body);
      } catch (err) { toast(err.message, true); }
    }));
    $$("[data-preview]").forEach((b) => b.addEventListener("click", async () => {
      const id = Number(b.dataset.preview);
      const out = $(`[data-pv="${id}"]`);
      out.textContent = "확인 중…";
      try {
        const r = await api("/api/prep-options/preview", { method: "POST", body: ruleBody(id) });
        out.innerHTML = r.total === 0
          ? "이 조건에 걸리는 주문이 없습니다."
          : `전체 <b>${r.total}건</b>에 해당하고, 그중 <b>아직 제작 전인 ${r.pending}건</b>이 바로 체크 대상이 됩니다.`
            + (r.samples.length ? `<div style="margin-top:4px;">예: ${r.samples.map((s) =>
                escapeHtml(`${s.channel} ${s.productName}`.trim().slice(0, 32))).join(" · ")}</div>` : "");
      } catch (err) { out.textContent = err.message; }
    }));
    $$("[data-rule-add]").forEach((b) => b.addEventListener("click", async () => {
      const id = Number(b.dataset.ruleAdd);
      try {
        await api(`/api/prep-options/${id}/rules`, { method: "POST", body: ruleBody(id) });
        toast("조건을 추가했습니다.");
        renderPrepTab(body);
      } catch (err) { toast(err.message, true); }
    }));
  };

  $("#po-add").addEventListener("click", async () => {
    const name = $("#po-name").value.trim();
    if (!name) { toast("옵션 이름을 입력하세요.", true); return; }
    try {
      await api("/api/prep-options", {
        method: "POST", body: { name, note: $("#po-note").value.trim() } });
      toast("옵션을 만들었습니다. 이제 어느 쇼핑몰에 붙일지 조건을 더하세요.");
      renderPrepTab(body);
    } catch (err) { toast(err.message, true); }
  });
  renderList(data.options || []);
}

/* ----- 정산 (판매수수료·택배비) ----- */

async function renderSettlementTab(body) {
  const seq = ++state.renderSeq;
  body.innerHTML = `<p class="muted">불러오는 중…</p>`;
  let settings, channels;
  try {
    // ★요율은 '주문에 실제로 들어 있는 채널 값' 기준이어야 걸린다.
    //   설정 화면의 몰 표시이름(예: '고도몰5')으로 만들면 주문 채널('고도몰')과 안 맞아
    //   요율을 넣어도 수수료가 0으로 남는다.
    //   채널 목록은 설정 권한만으로 볼 수 있는 전용 API를 쓴다(주문 조회 권한이 없어도 되게).
    [settings, channels] = await Promise.all([
      api("/api/settings"), api("/api/order-channels").catch(() => [])]);
    if (seq !== state.renderSeq) return;
  } catch (err) {
    if (seq === state.renderSeq) body.innerHTML = `<p class="muted">${escapeHtml(err.message)}</p>`;
    return;
  }
  const s = settings.settlement || {};
  const rates = s.rates || {};
  const counts = {};
  (channels || []).forEach((m) => { counts[m.channel] = m.count; });
  const names = [...new Set([...(channels || []).map((m) => m.channel).filter(Boolean),
                             ...Object.keys(rates).filter((k) => k !== "_default")])];
  body.innerHTML = `
    <div class="card">
      <h3 style="margin-top:0;">판매수수료율</h3>
      <p class="muted">쇼핑몰·PG가 떼어 가는 비율입니다. 넣어 두면 <b>출고 확인할 때 자동으로</b> 주문에 붙고,
      리포트의 마진이 판매가가 아니라 <b>실제로 남는 돈</b> 기준이 됩니다. 비워 두면 0%로 아무것도 깎지 않습니다.</p>
      <div class="form-grid">
        ${names.map((n) => `<label>${escapeHtml(n)} (%)${counts[n] ? ` <span class="muted" style="font-weight:400;">주문 ${counts[n]}건</span>` : ""}
          <input type="text" data-rate="${escapeHtml(n)}" value="${rates[n] != null ? rates[n] : ""}" placeholder="예: 10.8"></label>`).join("")}
        <label>그 외 채널 기본 (%)
          <input type="text" data-rate="_default" value="${rates._default != null ? rates._default : ""}" placeholder="예: 0"></label>
        <label>출고 택배비 (원)
          <input type="text" id="st-ship" value="${s.shippingCost || ""}" placeholder="예: 3000"></label>
      </div>
      <div class="editor-actions"><button class="btn btn-primary" id="st-save">저장</button></div>
    </div>
    <div class="card">
      <h3 style="margin-top:0;">과거 주문에 소급 적용</h3>
      <p class="muted">요율을 처음 넣으셨다면, 이미 출고된 주문에는 수수료가 0으로 남아 있습니다.
      먼저 [계산해 보기]로 금액을 확인한 뒤 적용하세요. 손으로 넣은 값은 건드리지 않습니다.</p>
      <div class="inline-row">
        <label class="muted">기간 <input type="date" id="st-from"> ~ <input type="date" id="st-to"></label>
        <button class="btn btn-sm" id="st-dry">계산해 보기</button>
        <button class="btn btn-sm btn-primary" id="st-apply" disabled>적용</button>
      </div>
      <div id="st-result"></div>
    </div>`;

  $("#st-save").addEventListener("click", async () => {
    const out = {};
    $$("input[data-rate]", body).forEach((i) => {
      const v = i.value.trim();
      if (v !== "") out[i.dataset.rate] = Number(v);
    });
    const bad = Object.entries(out).find(([, v]) => !Number.isFinite(v) || v < 0 || v > 100);
    if (bad) { toast(`${bad[0]} 요율이 올바르지 않습니다(0~100).`, true); return; }
    try {
      await api("/api/settings", { method: "PUT", body: { settlement: {
        rates: out, shippingCost: Number($("#st-ship").value.replaceAll(",", "")) || 0 } } });
      toast("정산 설정을 저장했습니다.");
      renderSettlementTab(body);
    } catch (err) { toast(err.message, true); }
  });

  // 확인한 범위와 실제 적용 범위가 어긋나면 안 된다 — 기간을 바꾸면 [적용]을 다시 잠근다
  const lockApply = () => {
    const btn = $("#st-apply");
    if (btn) btn.disabled = true;
    const host = $("#st-result");
    if (host && host.innerHTML) {
      host.innerHTML = `<p class="muted" style="margin-top:8px;">기간이 바뀌었습니다 — [계산해 보기]를 다시 눌러 주세요.</p>`;
    }
  };
  ["#st-from", "#st-to"].forEach((id) => $(id).addEventListener("change", lockApply));

  const runBackfill = async (dry) => {
    const asked = { from: $("#st-from").value, to: $("#st-to").value };
    try {
      const r = await api("/api/orders/settlement-backfill", { method: "POST", body: {
        dryRun: dry, from: asked.from, to: asked.to } });
      state.backfillChecked = dry ? asked : null;
      $("#st-result").innerHTML = `
        <div style="border:1px solid var(--border); border-radius:8px; padding:10px 12px; margin-top:8px;">
          <b>${dry ? "계산 결과(아직 반영 안 함)" : "적용 완료"}</b> —
          대상 ${r.scanned}건 중 <b>${r.changed}건</b>,
          수수료 합계 ${fmtWon(r.feeTotal)}${r.shippingTotal ? ` · 택배비 ${fmtWon(r.shippingTotal)}` : ""}
          ${r.byChannel.length ? `<ul style="margin:6px 0 0 18px;">${r.byChannel.map((c) =>
            `<li class="muted">${escapeHtml(c.channel)} — ${c.count}건 · ${fmtWon(c.fee)}</li>`).join("")}</ul>` : ""}
        </div>`;
      $("#st-apply").disabled = !dry || !r.changed;
      if (!dry) toast(`${r.changed}건에 반영했습니다.`);
    } catch (err) { toast(err.message, true); }
  };
  $("#st-dry").addEventListener("click", () => runBackfill(true));
  $("#st-apply").addEventListener("click", () => {
    const c = state.backfillChecked;
    if (!c || c.from !== $("#st-from").value || c.to !== $("#st-to").value) {
      toast("기간이 바뀌었습니다. [계산해 보기]를 다시 눌러 주세요.", true);
      lockApply();
      return;
    }
    if (confirm("계산한 대로 과거 주문에 수수료·택배비를 반영합니다. 계속할까요?")) runBackfill(false);
  });
}

/* ----- API 관리 (택배 + 쇼핑몰) ----- */

const MASK = "••••••••••••";

/* 상세 패널을 '팝업(모달)'로 띄운다.
   원래는 페이지 맨 아래에 그렸는데, 목록이 길면 수천 px 아래에 생겨 눌러도 '무반응'으로
   보였다. 스크롤로 데려가는 방식도 목록을 잃어버려 불편하다는 대표 지적(2026-07-29)에 따라
   팝업으로 바꿨다. 패널을 그리는 코드는 그대로 두고 '담는 그릇'만 모달로 만든다 —
   화면마다 흩어진 렌더 함수를 건드리지 않으려는 것이다.

   닫기: 각 패널의 [닫기] 버튼이 host.innerHTML=""로 비우므로, 비어 있으면 모달도 자동으로
   사라지게 감시한다(기존 닫기 코드를 하나도 안 고쳐도 되게). ESC·배경 클릭도 같은 효과. */
function revealPanel(sel) {
  setTimeout(() => {
    const el = typeof sel === "string" ? document.querySelector(sel) : sel;
    if (!el || !el.firstElementChild) return;
    openModalWith(el);
  }, 30);
}

function closeModal() {
  const back = document.getElementById("modal-back");
  if (!back) return;
  const host = back._host;
  if (host) {
    // 패널을 원래 자리로 돌려놓고 비운다 — 다음에 다시 그릴 때 그대로 쓰인다
    if (back._home && back._home.parentNode) back._home.parentNode.insertBefore(host, back._home);
    host.innerHTML = "";
    host.classList.remove("in-modal");
  }
  if (back._obs) back._obs.disconnect();
  back.remove();
  document.body.classList.remove("modal-open");
}

function openModalWith(host) {
  if (host.classList.contains("in-modal")) return;      // 이미 팝업으로 떠 있다
  // ★이미 열려 있는 팝업 '안쪽'을 다시 팝업으로 올리려는 경우다. 그대로 두면
  //   아래 closeModal()이 바깥 팝업의 innerHTML을 비워 버려, 방금 보던 화면이 통째로 사라진다.
  //   (전표 상세에서 관리번호를 누르면 전표가 사라지고, 셋팅 실적 달력에서 날짜를 누르면
  //    달력이 사라지던 문제 — 2026-07-31.) 이미 화면에 보이는 자리이므로 그 자리에 그대로 펼친다.
  if (host.closest("#modal-back")) return;
  closeModal();
  const back = document.createElement("div");
  back.id = "modal-back";
  back.className = "modal-back";
  // 원래 위치를 기억해 둔다(닫을 때 되돌리기 위해)
  const home = document.createComment("panel-home");
  host.parentNode.insertBefore(home, host);
  back._home = home;
  back._host = host;
  host.classList.add("in-modal");
  back.appendChild(host);
  document.body.appendChild(back);
  document.body.classList.add("modal-open");
  back.addEventListener("mousedown", (e) => { if (e.target === back) closeModal(); });
  // ★패널을 모달로 옮기면 DOM에서 한 번 떨어져 나가 포커스가 풀린다.
  //   스캔 칸처럼 '바로 찍어야 하는' 입력이 있으면 커서를 되돌려 준다
  //   (안 그러면 바코드를 찍어도 아무 칸에도 안 들어간다).
  const focusTarget = host.querySelector("[data-autofocus], .mt-slot, input:not([type=checkbox]):not([disabled])");
  if (focusTarget) focusTarget.focus();
  // 패널이 스스로 비워지면([닫기] 버튼) 팝업도 닫는다
  const obs = new MutationObserver(() => {
    if (!host.firstElementChild) closeModal();
  });
  obs.observe(host, { childList: true });
  back._obs = obs;
}

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && document.getElementById("modal-back")) closeModal();
});

// 저장된 비밀키의 화면 표시 — RMS처럼 검정 원으로 쭉 보여준다(대표 지시 2026-07-29)
const SECRET_DOTS = "●●●●●●●●●●●●";

// ★비밀 칸 공통 규칙:
//   보일 때  = ●●●(값이 아니라 '저장돼 있다'는 표시)
//   클릭하면 = 그 자리에서 통째로 비워진다 — 점 사이에 커서가 들어가 키가 섞이는 사고 방지
//              (실제로 마스크 조각이 키에 섞여 쿠팡 인증이 깨졌던 날의 재발 방지)
//   저장할 때 = 비어 있거나 점만 남아 있으면 '기존 값 유지'로 보낸다
function _clearSecretDots(e) {
  const el = e.target;
  if (el && el.matches && el.matches('input[data-secret="1"]') && /[●•]/.test(el.value)) {
    el.value = "";
  }
}
document.addEventListener("focusin", _clearSecretDots);
document.addEventListener("click", _clearSecretDots);      // 이미 포커스된 채 클릭해도
// 붙여넣기·타이핑으로 점 사이에 값이 들어가 버린 경우 — 점만 즉시 걷어낸다
document.addEventListener("input", (e) => {
  const el = e.target;
  if (el && el.matches && el.matches('input[data-secret="1"]') && /[●•]/.test(el.value)) {
    el.value = el.value.replace(/[●•]/g, "");
  }
});

function secretVal(el) {
  const v = el.value.trim();
  if (el.dataset.saved === "1" && (v === "" || /^[●•]+$/.test(v))) return MASK;
  return v.replace(/[●•]/g, "");   // 어떤 경로로든 점이 섞였으면 걷어낸다
}

/* API 관리 — 쇼핑몰·택배 연결과 '제공 옵션'을 한 탭 안에서 본다.
   ★제공 옵션은 몰에서 파는 상품 옵션이라 API 설정과 같은 자리에 있는 게 맞다.
     별도 탭으로 빼 두니 탭만 늘고 찾기 어려웠다(대표 지시 2026-08-04). */
const API_VIEWS = [["shop", "쇼핑몰 · 택배 API"], ["prep", "제공 옵션"]];

async function renderApiTab(body) {
  if (!API_VIEWS.some(([k]) => k === state.apiView)) state.apiView = "shop";
  body.innerHTML = `
    <div class="subtabs">${API_VIEWS.map(([k, l]) =>
      `<button data-aptab="${k}" class="${k === state.apiView ? "active" : ""}">${l}</button>`).join("")}</div>
    <div id="apiview-body"></div>`;
  $$("button[data-aptab]", body).forEach((b) => b.addEventListener("click", () => {
    state.apiView = b.dataset.aptab;
    renderApiTab(body);
  }));
  const host = $("#apiview-body", body);
  if (state.apiView === "prep") return renderPrepTab(host);
  return renderApiSettings(host);
}

async function renderApiSettings(body) {
  const seq = ++state.renderSeq;
  body.innerHTML = `<p class="muted">불러오는 중…</p>`;
  let settings, registry;
  try {
    [settings, registry] = await Promise.all([api("/api/settings"), api("/api/mall-registry")]);
    if (seq !== state.renderSeq) return;
  } catch (err) {
    if (seq === state.renderSeq) body.innerHTML = `<p class="muted">${escapeHtml(err.message)}</p>`;
    return;
  }
  state.mallRegistry = registry;
  const malls = settings.malls || {};
  if (!state.apiSection) state.apiSection = "cj";
  const configured = (m) => {
    const v = malls[m.code] || {};
    return m.fields.some((f) => f.type === "secret" && v[f.key]);
  };
  body.innerHTML = `
    <div class="card" style="padding:14px 16px;">
      <div class="inline-row" style="margin:0; flex-wrap:wrap;">
        <button class="btn btn-sm ${state.apiSection === "cj" ? "btn-primary" : ""}" data-api="cj">🚚 CJ대한통운</button>
        <button class="btn btn-sm ${state.apiSection === "sms" ? "btn-primary" : ""}" data-api="sms">💬 고객 안내 문자</button>
        ${registry.map((m) => `
          <button class="btn btn-sm ${state.apiSection === m.code ? "btn-primary" : ""}" data-api="${m.code}">
            ${escapeHtml(m.name)} ${configured(m) ? "🔑" : ""}${(malls[m.code] || {}).enabled ? " ✅" : ""}
          </button>`).join("")}
      </div>
      <p class="muted" style="margin:10px 0 0;">🔑 키 입력됨 · ✅ 사용 중 — 키를 넣어도 [이 몰 사용]을 켜야 수집 대상이 됩니다.</p>
    </div>
    <div id="api-body"></div>`;
  $$("button[data-api]", body).forEach((b) => b.addEventListener("click", () => {
    state.apiSection = b.dataset.api;
    renderApiTab(body);
  }));
  if (state.apiSection === "cj") renderCJSection($("#api-body"), settings.cj || {});
  else if (state.apiSection === "sms") renderSmsSection($("#api-body"), settings.sms || {});
  else {
    const m = registry.find((x) => x.code === state.apiSection);
    if (m) renderMallSection($("#api-body"), m, malls[m.code] || {});
  }
}

/* ----- 고객 안내 문자 (A/S 단계별) ----- */

async function renderSmsSection(host, v) {
  host.innerHTML = `<p class="muted">불러오는 중…</p>`;
  let cfg;
  try { cfg = await api("/api/sms/config"); }
  catch (err) { host.innerHTML = `<p class="muted">${escapeHtml(err.message)}</p>`; return; }

  host.innerHTML = `
    <div class="card">
      <h3 style="margin-top:0;">고객 안내 문자</h3>
      <p class="muted">A/S 진행 상황을 고객 휴대폰으로 자동 안내합니다(비즈고 문자 서비스).
      <b>키를 넣고 [실제 발송]까지 켜야</b> 실제로 나갑니다. 그 전까지는 보낸 내용만 이력에 남습니다.</p>
      <div style="background:${cfg.live ? "var(--danger-soft)" : "var(--slate-soft)"};
                  border-radius:8px; padding:10px 12px; margin:8px 0;">
        <b>${cfg.live ? "🔴 실제 발송 중 — 고객에게 문자가 나갑니다"
                      : "⚪ 지금은 실제로 보내지 않습니다"}</b>
        ${cfg.live ? "" : `<div class="muted" style="margin-top:4px;">${escapeHtml(cfg.reason || "")}</div>`}
      </div>
      <div class="form-grid">
        <label>문자 API 키
          <input type="text" id="sms-key" placeholder="${cfg.hasApiKey ? "입력됨 — 바꿀 때만 새로 입력" : "비즈고에서 발급받은 통합 API 키"}"></label>
        <label>발신번호
          <input type="text" id="sms-sender" value="${escapeHtml(cfg.sender || "")}" placeholder="예: 0212345678"></label>
        <label>시험 받을 번호 (대표님 휴대폰)
          <input type="text" id="sms-test" value="${escapeHtml(cfg.testPhone || "")}" placeholder="010-0000-0000"></label>
        <label>문의 전화번호 <span class="muted" style="font-weight:400;">문구의 {문의처}에 들어감</span>
          <input type="text" id="sms-contact" value="${escapeHtml(cfg.contact || "")}"></label>
        <label>하루 최대 발송
          <input type="text" id="sms-cap" value="${cfg.dailyCap}"></label>
        <label>보내지 않는 시간
          <input type="text" id="sms-quiet" value="${cfg.quietFrom}-${cfg.quietTo}" placeholder="21-8"></label>
        <label>서버 주소 (기본값 그대로 두세요)
          <input type="text" id="sms-base" value="${escapeHtml(cfg.baseUrl || "")}"></label>
      </div>
      <p class="muted" style="margin:8px 0 0;">※ 비즈고 콘솔에서 <b>이 PC의 IP 등록</b>과 <b>발신번호 사전등록</b>을
      마쳐야 발송됩니다. 발신번호는 사전등록한 번호만 쓸 수 있습니다.</p>
      <div class="editor-actions">
        <button class="btn btn-primary" id="sms-save">저장</button>
        <button class="btn" id="sms-test-btn">📱 내 번호로 시험 발송</button>
        <label class="check-line" style="margin-left:8px;">
          <input type="checkbox" id="sms-armed" ${cfg.armed ? "checked" : ""}>
          <b style="color:var(--danger);">실제 발송 켜기</b>
          <span class="muted">— 켜면 고객에게 진짜 문자가 갑니다</span>
        </label>
      </div>
      <p class="muted" style="margin:8px 0 0;">
        ${cfg.confirmed ? "✅ 시험 발송을 확인했습니다."
          : "① 키·발신번호 저장 → ② [내 번호로 시험 발송] 성공 → ③ [실제 발송 켜기] 순서로 진행하세요. "
            + "시험을 성공하기 전에는 고객에게 나가지 않습니다."}</p>
    </div>

    <div class="card">
      <h3 style="margin-top:0;">언제 무엇을 보낼지</h3>
      <p class="muted">체크를 끄면 그 단계에서는 자동으로 보내지 않습니다(A/S 화면에서 손으로는 보낼 수 있습니다).
      문구에 <code>{고객명}</code>처럼 넣으면 실제 값으로 바뀝니다 —
      쓸 수 있는 값: ${cfg.vars.map((x) => `<code>{${x}}</code>`).join(" ")}</p>
      ${cfg.events.map((e) => `
        <div style="border:1px solid var(--border); border-radius:8px; padding:10px 12px; margin:8px 0;">
          <label class="check-line">
            <input type="checkbox" data-smson="${e.code}" ${e.on ? "checked" : ""}>
            <b>${escapeHtml(e.label)}</b>
            <span class="muted">— ${escapeHtml(e.when)}</span>
          </label>
          <textarea data-smstext="${e.code}" rows="4"
            style="width:100%; margin-top:6px; padding:8px; border:1px solid var(--border);
                   border-radius:8px; background:var(--bg); font-family:inherit;">${escapeHtml(e.text)}</textarea>
          <div class="inline-row" style="margin:6px 0 0;">
            <button class="btn btn-sm" data-smsprev="${e.code}">미리보기</button>
            <button class="btn btn-ghost btn-sm" data-smsreset="${e.code}">기본 문구로</button>
            <span class="muted" data-smsinfo="${e.code}"></span>
          </div>
        </div>`).join("")}
      <div class="editor-actions"><button class="btn btn-primary" id="sms-save-events">문구 저장</button></div>
    </div>

    <div class="card">
      <div class="inline-row"><h3 style="margin:0; flex:1;">보낸 내역</h3>
        <button class="btn btn-sm" id="sms-reload">새로고침</button></div>
      <div id="sms-log"><p class="muted">불러오는 중…</p></div>
    </div>`;

  const defaults = {};
  cfg.events.forEach((e) => { defaults[e.code] = e.default; });

  $("#sms-save").addEventListener("click", async () => {
    const numOr = (v, dflt) => {
      const n = Number(String(v).trim());
      return Number.isFinite(n) && String(v).trim() !== "" ? n : dflt;
    };
    const quiet = ($("#sms-quiet").value || "21-8").split("-");
    const payload = { sender: $("#sms-sender").value.trim(), baseUrl: $("#sms-base").value.trim(),
                      armed: $("#sms-armed").checked,
                      testPhone: $("#sms-test").value.trim(), contact: $("#sms-contact").value.trim(),
                      // ★|| 를 쓰면 0이 falsy라 무시된다. 자정(0시)부터 막고 싶어도 21시가 되고,
                      //   발송을 잠시 전면 중지하려고 0을 넣어도 30으로 되돌아간다.
                      dailyCap: numOr($("#sms-cap").value, 30),
                      quietFrom: numOr(quiet[0], 21), quietTo: numOr(quiet[1], 8) };
    const key = $("#sms-key").value.trim();
    if (key) payload.apiKey = key;
    if (payload.armed && !(key || cfg.hasApiKey)) { toast("API 키를 먼저 입력하세요.", true); return; }
    if (payload.armed && !confirm("실제 발송을 켭니다.\n이제부터 A/S 단계마다 고객 휴대폰으로 문자가 나갑니다.\n\n계속할까요?")) return;
    try {
      await api("/api/settings", { method: "PUT", body: { sms: payload } });
      toast("저장했습니다.");
      renderSmsSection(host, {});
    } catch (err) { toast(err.message, true); }
  });

  $("#sms-test-btn").addEventListener("click", async (e) => {
    const btn = e.currentTarget;
    if (!confirm("설정에 저장된 대표님 번호로 시험 문자 1건을 보냅니다.\n계속할까요?")) return;
    btn.disabled = true;
    try {
      const r = await api("/api/sms/test", { method: "POST", body: {} });
      toast(r.message, !r.ok);
      if (r.ok) renderSmsSection(host, {});
    } catch (err) { toast(err.message, true); }
    finally { btn.disabled = false; }
  });

  $("#sms-save-events").addEventListener("click", async () => {
    const events = {};
    cfg.events.forEach((e) => {
      events[e.code] = { on: $(`input[data-smson="${e.code}"]`).checked,
                         text: $(`textarea[data-smstext="${e.code}"]`).value };
    });
    try {
      await api("/api/settings", { method: "PUT", body: { sms: { events } } });
      toast("문구를 저장했습니다.");
    } catch (err) { toast(err.message, true); }
  });

  $$("button[data-smsprev]", host).forEach((b) => b.addEventListener("click", async () => {
    const code = b.dataset.smsprev;
    try {
      const r = await api("/api/sms/preview", { method: "POST",
        body: { text: $(`textarea[data-smstext="${code}"]`).value } });
      $(`[data-smsinfo="${code}"]`).textContent = `${r.msgType} · ${r.bytes}바이트`;
      alert(`이렇게 나갑니다 (${r.msgType}, ${r.bytes}바이트)\n\n${r.text}\n\n${r.note}`);
    } catch (err) { toast(err.message, true); }
  }));
  $$("button[data-smsreset]", host).forEach((b) => b.addEventListener("click", () => {
    const code = b.dataset.smsreset;
    const box = $(`textarea[data-smstext="${code}"]`);
    // 눌렀는데 아무 일도 안 일어나면 고장으로 보인다 — 결과를 말로 알려 준다
    if ((box.value || "").trim() === (defaults[code] || "").trim()) {
      toast("이미 기본 문구입니다.");
      return;
    }
    box.value = defaults[code] || "";
    toast("기본 문구로 되돌렸습니다. 저장하려면 아래 [문구 저장]을 눌러 주세요.");
  }));

  const loadLog = async () => {
    try {
      const rows = await api("/api/sms/log");
      const el = $("#sms-log");
      if (!el) return;
      el.innerHTML = rows.length ? `<div class="table-wrap"><table>
        <thead><tr><th>시각</th><th>접수번호</th><th>고객</th><th>종류</th><th>결과</th><th>내용</th></tr></thead>
        <tbody>${rows.map((r) => `<tr>
          <td class="muted">${escapeHtml((r.sentAt || "").replace("T", " ").slice(5, 16))}</td>
          <td>${escapeHtml(r.ticketNo || "-")}</td>
          <td>${escapeHtml(r.customer || "")}<div class="muted" style="font-size:12px;">${escapeHtml(r.phone)}</div></td>
          <td>${escapeHtml(r.event)}</td>
          <td>${r.status === "sent" ? '<span class="chip chip-green">발송</span>'
               : r.status === "simulated" ? '<span class="chip chip-slate">미발송(기록만)</span>'
               : r.status === "skipped" ? '<span class="chip chip-slate">건너뜀</span>'
               : '<span class="chip chip-red">실패</span>'}
            ${r.reason ? `<div class="muted" style="font-size:12px;">${escapeHtml(r.reason.slice(0, 60))}</div>` : ""}</td>
          <td style="max-width:320px;"><span class="muted" style="font-size:12px; white-space:pre-wrap;">${escapeHtml((r.text || "").slice(0, 90))}</span></td>
        </tr>`).join("")}</tbody></table></div>`
        : `<p class="muted">보낸 내역이 없습니다.</p>`;
    } catch (err) { $("#sms-log").innerHTML = `<p class="muted">${escapeHtml(err.message)}</p>`; }
  };
  $("#sms-reload").addEventListener("click", loadLog);
  loadLog();
}

function renderMallSection(host, m, v) {
  const fieldHtml = (f) => {
    const id = `mf-${f.key}`;
    const val = v[f.key];
    if (f.type === "bool") {
      return `<label class="check-line"><input type="checkbox" id="${id}" ${val ? "checked" : ""}> ${escapeHtml(f.label)}</label>`;
    }
    const type = f.type === "date" ? "date" : "text";
    // ★저장된 비밀값은 칸 '안'에 넣지 않는다.
    //   ●●● 를 값으로 넣어 두면, 그 뒤에 키를 붙여넣었을 때 '●●●실제키'가 저장돼
    //   인증 헤더가 깨진다(2026-07-29 쿠팡 연결 실패의 원인). 안내는 흐린 글씨로만 띄운다.
    const saved = f.type === "secret" && val === MASK;
    // 저장된 키는 ●●● 로 표시. 클릭하면 전역 focusin 핸들러가 통째로 비운다.
    // ★data-saved: 저장할 때 이 칸이 빈 채(또는 점만)면 '지워라'가 아니라 '그대로 둬라'로
    //   보낸다 — 안 그러면 키를 하나씩 입력할 때마다 먼저 넣은 키가 사라진다(2026-07-29 실제 발생).
    const shown = saved ? SECRET_DOTS : (val || "");
    const ph = f.placeholder && !saved ? ` placeholder="${escapeHtml(f.placeholder)}"` : "";
    return `<label>${escapeHtml(f.label)}${f.type === "secret" ? ' <span class="muted">(비밀)</span>' : ""}
      <input type="${type}" id="${id}" value="${escapeHtml(shown)}"${ph} ${f.type === "secret" ? `autocomplete="off" data-secret="1" data-saved="${saved ? 1 : 0}"` : ""}>
      ${saved ? '<span class="muted" style="font-size:12px;">✓ 저장됨 — 바꾸려면 클릭하고 새 키를 붙여넣으세요</span>' : ""}
      ${f.help ? `<span class="muted" style="font-size:12px;">${escapeHtml(f.help)}</span>` : ""}</label>`;
  };
  const bools = m.fields.filter((f) => f.type === "bool");
  const inputs = m.fields.filter((f) => f.type !== "bool");
  host.innerHTML = `
    <div class="card" style="max-width:860px;">
      <h3>${escapeHtml(m.name)} <span class="muted" style="font-size:13px;">${escapeHtml(m.vendor)} · ${escapeHtml(m.auth)}</span></h3>
      <p class="muted">${escapeHtml(m.note)}
        ${m.docs ? `<br><a href="${escapeHtml(m.docs)}" target="_blank" rel="noopener" style="color:var(--primary);">개발자 문서 열기 ↗</a>` : ""}</p>
      <div class="form-grid">${inputs.map(fieldHtml).join("")}</div>
      <div style="margin-top:10px;">${bools.map(fieldHtml).join("")}</div>
      <div class="editor-actions">
        <button class="btn btn-primary" id="mf-save">저장</button>
        <button class="btn" id="mf-test" ${m.implemented ? "" : "disabled"}>🔌 연결 테스트</button>
        <button class="btn btn-ghost" id="mf-clear">키 지우기</button>
      </div>
      <div id="mf-result" style="margin-top:10px;"></div>
      <p class="muted" style="margin-top:8px;">
        비밀 항목은 저장 후 ●●●로 표시됩니다. 그대로 두고 저장하면 기존 값이 유지됩니다.<br>
        ${m.implemented
          ? "연결 테스트는 최근 1일 주문을 <b>읽어보기만</b> 합니다 — 주문이 저장되거나 몰에 아무 변경도 생기지 않습니다."
          : "이 몰은 아직 수집 어댑터가 없어 연결 테스트를 할 수 없습니다. 키는 지금 저장해 두어도 됩니다."}
      </p>
    </div>`;
  $("#mf-save").addEventListener("click", async () => {
    const out = {};
    m.fields.forEach((f) => {
      const el = document.getElementById(`mf-${f.key}`);
      if (!el) return;
      // 비밀 칸: 비어 있거나 ●점만 남았으면 '유지'로 보낸다(secretVal).
      // 지우고 싶으면 [키 지우기] 버튼 — 그쪽은 명시적으로 빈 값을 보낸다.
      out[f.key] = f.type === "bool" ? el.checked
                 : f.type === "secret" ? secretVal(el) : el.value.trim();
    });
    try {
      await api("/api/settings", { method: "PUT", body: { malls: { [m.code]: out } } });
      toast(`${m.name} 설정을 저장했습니다.`);
      renderApiTab($("#tab-body"));
    } catch (err) { toast(err.message, true); }
  });
  const testBtn = $("#mf-test");
  if (testBtn) testBtn.addEventListener("click", async () => {
    const res = $("#mf-result");
    testBtn.disabled = true;
    res.innerHTML = `<span class="muted">${escapeHtml(m.name)}에 연결하는 중…</span>`;
    try {
      const r = await api(`/api/malls/${m.code}/test`, { method: "POST" });
      res.innerHTML = `<div style="background:var(--primary-soft); border-radius:8px; padding:10px 12px;">
        ✅ <b>${escapeHtml(r.mall)} 연결 성공</b><br>
        <span class="muted">${escapeHtml(r.message)}</span></div>`;
      toast(`${r.mall} 연결 성공`);
    } catch (err) {
      res.innerHTML = `<div style="background:var(--danger-soft); border-radius:8px; padding:10px 12px;">
        ❌ <b>연결 실패</b><br><span style="color:var(--danger)">${escapeHtml(err.message)}</span><br>
        <span class="muted">키 값과 호출 IP 등록(대부분의 몰이 IP 화이트리스트를 씁니다)을 확인하세요.</span></div>`;
    } finally { testBtn.disabled = false; }
  });
  $("#mf-clear").addEventListener("click", async () => {
    if (!confirm(`${m.name}의 저장된 '비밀 키'만 지웁니다.\n`
                 + "판매자ID·메모·택배사 코드와 [이 몰 사용] 설정은 그대로 둡니다.\n\n계속할까요?")) return;
    // ★예전엔 모든 칸을 비워서 자동수집까지 조용히 꺼졌다(2026-07-29 전수조사).
    //   서버가 부분 저장을 지원하므로 비밀 필드만 보낸다.
    const out = {};
    m.fields.filter((f) => f.type === "secret").forEach((f) => { out[f.key] = ""; });
    try {
      await api("/api/settings", { method: "PUT", body: { malls: { [m.code]: out } } });
      toast("지웠습니다.");
      renderApiTab($("#tab-body"));
    } catch (err) { toast(err.message, true); }
  });
}

function renderCJSection(host, cj) {
  const sender = cj.sender || {};
  const label = cj.label || {};
  host.innerHTML = `
    <div class="card" style="max-width:860px;">
      <h3>CJ대한통운 연동</h3>
      <p class="muted">고객코드는 하프전자 주식회사 명의(30516776)를 사용합니다.
      <b>운영(prod) + 실발행 무장이 모두 켜져야 실제 송장이 접수</b>되며, 그 전에는 테스트 발행(999 번호)으로 출력 흐름만 검증됩니다.</p>
      <div class="form-grid">
        <label>환경<select id="cj-env">
          <option value="dev" ${cj.env !== "prod" ? "selected" : ""}>개발(dev) — 실배송 없음</option>
          <option value="prod" ${cj.env === "prod" ? "selected" : ""}>운영(prod) — 실배송</option>
        </select></label>
        <label class="check-line" style="align-self:end;"><input type="checkbox" id="cj-armed" ${cj.armed ? "checked" : ""}> 실발행 무장(armed)</label>
        <label>고객코드 (CUST_ID)<input type="text" id="cj-custid" value="${escapeHtml(cj.cust_id || "30516776")}"></label>
        <label>사업자등록번호<input type="text" id="cj-bizreg"
          value="${cj.biz_reg_num === MASK ? SECRET_DOTS : escapeHtml(cj.biz_reg_num || "")}"
          placeholder="숫자만" data-secret="1" data-saved="${cj.biz_reg_num === MASK ? 1 : 0}"></label>
      </div>
      <h3 style="margin-top:16px;">보내는 분 (출고지)</h3>
      <div class="form-grid">
        <label>이름<input type="text" id="cj-sname" value="${escapeHtml(sender.name || "")}" placeholder="예: 하프북"></label>
        <label>전화<input type="text" id="cj-stel" value="${escapeHtml(sender.tel || "")}"></label>
        <label>우편번호<input type="text" id="cj-szip" value="${escapeHtml(sender.zip || "")}"></label>
        <label>주소<input type="text" id="cj-saddr" value="${escapeHtml(sender.addr || "")}"></label>
        <label>상세주소<input type="text" id="cj-saddr2" value="${escapeHtml(sender.addr_detail || "")}"></label>
      </div>
      <h3 style="margin-top:16px;">운송장 인쇄 보정 <span class="muted">(레이아웃은 동결 — 위치 이슈는 보정값으로만)</span></h3>
      <div class="form-grid">
        <label>가로 오프셋 ox(mm)<input type="text" id="cj-ox" value="${label.ox ?? 0}"></label>
        <label>세로 오프셋 oy(mm)<input type="text" id="cj-oy" value="${label.oy ?? 0}"></label>
        <label>배율 sc(%)<input type="text" id="cj-sc" value="${label.sc ?? 100}"></label>
      </div>
      <div class="editor-actions">
        <button class="btn btn-primary" id="cj-save">저장</button>
        <button class="btn" id="cj-test">🔌 연결 테스트</button>
      </div>
      <div id="cj-result" style="margin-top:10px;"></div>
      <p class="muted" style="margin-top:8px;">연결 테스트는 <b>토큰 발급만</b> 확인합니다 — 배송 예약이 생기지 않습니다.</p>
    </div>`;
  const bindCJ = () => {
  $("#cj-save").addEventListener("click", async () => {
    try {
      await api("/api/settings", { method: "PUT", body: { cj: {
        env: $("#cj-env").value,
        armed: $("#cj-armed").checked,
        cust_id: $("#cj-custid").value.trim(),
        biz_reg_num: secretVal($("#cj-bizreg")),
        sender: {
          name: $("#cj-sname").value.trim(), tel: $("#cj-stel").value.trim(),
          zip: $("#cj-szip").value.trim(), addr: $("#cj-saddr").value.trim(),
          addr_detail: $("#cj-saddr2").value.trim(),
        },
        label: { ox: Number($("#cj-ox").value) || 0, oy: Number($("#cj-oy").value) || 0,
                 sc: Number($("#cj-sc").value) || 100 },
      } } });
      toast("CJ 설정을 저장했습니다.");
    } catch (err) { toast(err.message, true); }
  });
  $("#cj-test").addEventListener("click", async () => {
    const btn = $("#cj-test");
    const res = $("#cj-result");
    btn.disabled = true;
    res.innerHTML = `<span class="muted">CJ대한통운에 연결하는 중…</span>`;
    try {
      const r = await api("/api/cj/test", { method: "POST" });
      res.innerHTML = `<div style="background:var(--primary-soft); border-radius:8px; padding:10px 12px;">
        ✅ <b>CJ 연결 성공</b> <span class="chip ${r.env === "prod" ? "chip-green" : "chip-slate"}">${r.env === "prod" ? "운영" : "개발"}</span>
        ${r.armed ? '<span class="chip chip-red">실발행 무장</span>' : '<span class="chip chip-slate">테스트 발행</span>'}<br>
        <span class="muted">고객코드 ${escapeHtml(r.custId)} · ${escapeHtml(r.host)}<br>${escapeHtml(r.message)}</span></div>`;
      toast("CJ 연결 성공");
    } catch (err) {
      res.innerHTML = `<div style="background:var(--danger-soft); border-radius:8px; padding:10px 12px;">
        ❌ <b>연결 실패</b><br><span style="color:var(--danger)">${escapeHtml(err.message)}</span><br>
        <span class="muted">고객코드·사업자등록번호를 저장한 뒤 다시 시도하세요.</span></div>`;
    } finally { btn.disabled = false; }
  });
  };
  bindCJ();
}

/* ----- 데이터 이관 (기존 QC 프로그램) ----- */

function renderMigrateTab(body) {
  body.innerHTML = `
    <div class="card" style="max-width:860px;">
      <h3>기존 QC 프로그램 데이터 가져오기</h3>
      <p class="muted">NAS에 백업된 주문 워크플로 프로그램의 <b>주문·사용자·관리번호</b>를 HMS로 옮깁니다.
      비밀번호 저장 방식이 같아서 <b>직원들은 쓰던 비밀번호 그대로</b> 로그인할 수 있습니다.
      이미 들어온 주문·계정은 건너뛰므로 여러 번 실행해도 중복되지 않습니다.</p>

      <div class="form-grid" style="margin-top:10px;">
        <label style="grid-column:1/-1;">프로그램 폴더 경로 (data 폴더가 있는 위치)
          <input type="text" id="qc-path" placeholder="예: \\\\Arthurrental\\Halfbook\\...\\order-workflow-sample">
        </label>
      </div>
      <p class="muted" style="margin:6px 0;">또는 <b>orders.json / users.json 파일을 직접 올려도</b> 됩니다.</p>
      <div class="inline-row">
        <input type="file" id="qc-files" multiple accept=".json">
        <button class="btn btn-sm" id="qc-preview">미리보기</button>
        <button class="btn btn-sm btn-primary" id="qc-run">가져오기 실행</button>
      </div>
      <div id="qc-result" style="margin-top:12px;"></div>
      <p class="muted" style="margin-top:10px;">
        옮겨지는 것: 주문(단계별 담당자·시각 그대로) · 사용자(구 역할을 메뉴 권한으로 환산) ·
        관리번호(TMS 형식이면 자산으로 만들고 주문에 매칭).<br>
        실행 전에 <b>설정 &gt; 백업</b>에서 [지금 백업]을 한 번 눌러두시길 권합니다.
      </p>
    </div>
    <div class="card" style="max-width:860px;" id="qcw-card">
      <h3>🔄 QC 폴더 실시간 연동</h3>
      <p class="muted">QC 프로그램이 쓰는 폴더를 <b>20초마다</b> 들여다보고,
      셋팅·제작완료·SW검수·출고 단계가 더 진행됐으면 자동으로 맞춥니다.
      <b>끝난 단계를 되돌리지는 않습니다.</b> 파일이 안 바뀌었으면 읽지도 않습니다.</p>
      <div class="inline-row" style="gap:6px; flex-wrap:wrap;">
        <label class="check-line"><input type="checkbox" id="qcw-on"> 사용</label>
        <input type="text" id="qcw-path" style="flex:1; min-width:280px;"
               placeholder="\\192.168.0.185\order-data">
        <button class="btn btn-sm" id="qcw-save">저장</button>
        <button class="btn btn-sm btn-primary" id="qcw-run">지금 반영</button>
      </div>
      <div id="qcw-status" class="muted" style="margin-top:8px; font-size:13px;">확인 중…</div>
    </div>`;

  const qcwPaint = (d) => {
    const el = $("#qcw-status");
    if (!el) return;
    $("#qcw-on").checked = !!d.enabled;
    $("#qcw-path").value = d.folder || "";
    el.innerHTML = d.error
      ? `<span style="color:var(--danger)">⚠ ${escapeHtml(d.error)}</span>`
      : !d.found
        ? `<span style="color:var(--danger)">⚠ 폴더에서 orders.json을 찾지 못했습니다.</span>`
        : `연결됨 · 파일 ${escapeHtml(d.file)}<br>`
          + `마지막 확인 ${escapeHtml((d.lastAt || "-").replace("T", " ").slice(0, 19))}`
          + ` · 읽은 주문 ${(d.rows || 0).toLocaleString("ko-KR")}건`
          + ` · 마지막에 맞춘 단계 ${d.lastApplied || 0}건`
          + (d.newOrders ? `<br><span style="color:var(--text-dim)">폴더에만 있는 주문 ${d.newOrders}건 —`
             + ` 단계만 따라가고 주문을 새로 만들지는 않습니다(위 [가져오기 실행]으로 넣으세요).</span>` : "");
  };
  const qcwLoad = async () => {
    try { qcwPaint(await api("/api/qc-watch/status")); }
    catch (err) { const el = $("#qcw-status"); if (el) el.textContent = err.message; }
  };
  $("#qcw-save").addEventListener("click", async () => {
    try {
      await api("/api/qc-watch", { method: "POST",
        body: { enabled: $("#qcw-on").checked, path: $("#qcw-path").value.trim() } });
      toast("저장했습니다.");
      qcwLoad();
    } catch (err) { toast(err.message, true); }
  });
  $("#qcw-run").addEventListener("click", async () => {
    const b = $("#qcw-run");
    b.disabled = true; b.textContent = "반영 중…";
    try {
      const r = await api("/api/qc-watch/run", { method: "POST", body: {} });
      toast(r.advanced ? `주문 ${r.advanced}건의 단계를 맞췄습니다.` : "새로 맞출 단계가 없습니다.");
    } catch (err) { toast(err.message, true); }
    b.disabled = false; b.textContent = "지금 반영";
    qcwLoad();
  });
  qcwLoad();

  const send = async (path) => {
    const files = $("#qc-files").files;
    const p = $("#qc-path").value.trim();
    if (!files.length && !p) { toast("경로를 입력하거나 파일을 선택하세요.", true); return; }
    $("#qc-preview").disabled = $("#qc-run").disabled = true;
    $("#qc-result").innerHTML = `<span class="muted">읽는 중…</span>`;
    try {
      let res, data;
      if (files.length) {
        const fd = new FormData();
        for (const f of files) fd.append("files", f);
        if (p) fd.append("path", p);
        res = await fetch(path, { method: "POST", body: fd });
        data = await res.json().catch(() => null);
      } else {
        data = await api(path, { method: "POST", body: { path: p } });
        res = { ok: true };
      }
      if (res.ok === false || (res.status && res.status >= 400)) {
        throw new Error((data && data.error) || "요청 실패");
      }
      const isPreview = path.includes("preview");
      if (isPreview) {
        $("#qc-result").innerHTML = `
          <div style="background:var(--primary-soft); border-radius:8px; padding:12px 14px;">
            <b>미리보기</b><br>
            주문 <b>${data.orders.toCreate}건</b> 추가 예정 (전체 ${data.orders.total} · 그대로 둠 ${data.orders.duplicates})<br>
            ${data.orders.toAdvance
              ? `이미 있는 주문 <b style="color:var(--danger)">${data.orders.toAdvance}건</b>의 진행 단계가 최신으로 바뀝니다<br>`
              : ""}
            사용자 <b>${data.users.toCreate}명</b> 추가 예정 (이미 있음 ${data.users.duplicates})<br>
            관리번호로 만들 자산 <b>${data.assets.toCreate}대</b>
          </div>
          ${(data.orders.advanceSample || []).length ? `
          <p class="muted" style="margin:8px 0 4px;">바뀔 주문 (앞 ${data.orders.advanceSample.length}건)</p>
          <div class="table-wrap"><table>
            <thead><tr><th>주문번호</th><th>수취인</th><th>새로 켜질 단계</th></tr></thead>
            <tbody>${data.orders.advanceSample.map((a) => `<tr>
              <td>${escapeHtml(a.orderNumber)}</td><td>${escapeHtml(a.recipient)}</td>
              <td>${escapeHtml(a["단계"])}</td></tr>`).join("")}</tbody>
          </table></div>` : ""}
          ${data.users.list.length ? `<div class="table-wrap" style="margin-top:8px;"><table>
            <thead><tr><th>아이디</th><th>이름</th><th>기존 역할</th><th>부여될 권한</th></tr></thead>
            <tbody>${data.users.list.map((u) => `<tr><td>${escapeHtml(u.username)}</td>
              <td>${escapeHtml(u.displayName)}</td><td class="muted">${escapeHtml(u.role)}</td>
              <td>${escapeHtml(u.grant)}</td></tr>`).join("")}</tbody></table></div>` : ""}
          ${data.sample.length ? `<div class="table-wrap" style="margin-top:8px;"><table>
            <thead><tr><th>채널</th><th>주문번호</th><th>상품</th><th>수취인</th><th>관리번호</th></tr></thead>
            <tbody>${data.sample.map((s) => `<tr><td>${escapeHtml(s.channel)}</td>
              <td>${escapeHtml(s.orderNumber)}</td><td>${escapeHtml(s.productName)}</td>
              <td>${escapeHtml(s.recipient)}</td><td>${escapeHtml(s["관리번호"])}</td></tr>`).join("")}</tbody></table></div>` : ""}`;
      } else {
        $("#qc-result").innerHTML = `
          <div style="background:var(--primary-soft); border-radius:8px; padding:12px 14px;">
            ✅ <b>가져오기 완료</b><br>${escapeHtml(data.message)}<br>
            <span class="muted">주문 ${data.orders.created} · 사용자 ${data.users.created} ·
            자산 ${data.assets.created} · 주문-자산 매칭 ${data.assets.matched}</span>
          </div>`;
        toast("데이터를 가져왔습니다.");
      }
    } catch (err) {
      $("#qc-result").innerHTML = `<div style="background:var(--danger-soft); border-radius:8px; padding:12px 14px;">
        ❌ <span style="color:var(--danger)">${escapeHtml(err.message)}</span></div>`;
    } finally {
      $("#qc-preview").disabled = $("#qc-run").disabled = false;
    }
  };
  $("#qc-preview").addEventListener("click", () => send("/api/migrate/qc/preview"));
  $("#qc-run").addEventListener("click", () => {
    // ★'건너뜁니다'는 사실이 아니다 — 이미 있는 주문도 진행 단계가 더 나갔으면 갱신된다.
    //   실측: 745건 파일에서 기존 주문 37건이 바뀌고 지난달 매출이 259만원 늘었다.
    if (!confirm("기존 프로그램의 주문·사용자·관리번호를 HMS로 가져옵니다.\n\n"
                 + "· 없는 주문은 새로 추가합니다.\n"
                 + "· 이미 있는 주문도 셋팅·제작·검수·출고가 더 진행됐으면 그 단계를 최신으로 맞춥니다.\n"
                 + "  (끝난 단계를 되돌리지는 않습니다)\n\n"
                 + "무엇이 바뀌는지는 [미리보기]로 먼저 확인하실 수 있습니다. 계속할까요?")) return;
    send("/api/migrate/qc");
  });
}


/* ----- 재고연동 (쇼핑몰별 On/Off, 기본 전부 꺼짐) -----
   제품코드가 붙고 [재고반영]이 체크된 자산 수를 몰에 보내는 기능의 관제판.
   ★켜기 전에는 어떤 몰에도 아무것도 보내지 않는다. 꺼진 몰의 재고값은 몰에 있던 그대로다. */
async function renderStockSyncTab(body) {
  body.innerHTML = `<p class="muted">불러오는 중…</p>`;
  let d;
  try { d = await api("/api/stock-sync"); }
  catch (err) { body.innerHTML = `<p class="muted">${escapeHtml(err.message)}</p>`; return; }
  body.innerHTML = `
    <div class="card">
      <h3>HMS 기준 재고 <span class="muted" style="font-weight:400;">제품코드 ${d.totalCodes}종 · ${d.totalUnits}대</span></h3>
      <p class="muted">매입에서 <b>제품코드</b>를 기입하고 <b>[재고반영]</b>을 체크한 자산만 여기에 세어집니다.
        체크가 없으면 0으로 시작하는 게 정상입니다.</p>
      <div class="table-wrap" style="max-height:300px; overflow-y:auto;"><table>
        <thead><tr><th>제품코드</th><th>재고</th></tr></thead>
        <tbody>${(d.codes || []).map((c) => `
          <tr><td>${escapeHtml(c.productCode)}</td><td><b>${c.count}대</b></td></tr>`).join("")
          || `<tr><td colspan="2" class="muted">재고반영된 자산이 아직 없습니다 — 매입 화면에서 제품코드를 넣고 [재고반영]을 체크하세요.</td></tr>`}
        </tbody></table></div>
    </div>
    <div class="card">
      <h3>쇼핑몰별 전송 스위치 <span class="chip chip-red">기본 전부 꺼짐</span></h3>
      <p class="muted">꺼진 몰은 HMS가 아무것도 보내지 않습니다 — 몰에 등록된 기존 재고값이 그대로 유지됩니다.</p>
      <div class="table-wrap"><table>
        <thead><tr><th>쇼핑몰</th><th>상태</th><th></th></tr></thead>
        <tbody>${d.malls.map((m) => `
          <tr><td>${escapeHtml(m.name)}</td>
          <td>${m.enabled ? '<span class="chip chip-green">켜짐</span>'
                : (m.supported ? '<span class="chip chip-slate">꺼짐</span>'
                               : '<span class="chip chip-slate" title="전송 전제조건이 아직 준비되지 않았습니다">준비 중</span>')}</td>
          <td>${m.supported ? `<button class="btn btn-sm" data-sync-toggle="${m.code}" data-on="${m.enabled ? 0 : 1}">
                 ${m.enabled ? "끄기" : "켜기"}</button>` : ""}</td></tr>`).join("")}
        </tbody></table></div>
      <div class="inline-row" style="margin-top:10px;">
        <button class="btn" id="ss-preview" title="어느 몰에 어떤 숫자를 보낼지 계산만 합니다 — 아무것도 전송하지 않습니다">👁 시험 계산 (전송 없음)</button>
        <div id="ss-preview-out" style="flex:1;"></div>
      </div>
    </div>`;
  $$("button[data-sync-toggle]", body).forEach((b) => b.addEventListener("click", async () => {
    const mall = b.dataset.syncToggle, on = b.dataset.on === "1";
    if (on && !confirm("이 쇼핑몰에 HMS 기준 재고를 보내기 시작합니다.\n"
        + "몰에 등록된 재고 숫자가 HMS 값으로 바뀝니다. 켤까요?")) return;
    try {
      await api(`/api/stock-sync/${mall}`, { method: "PUT", body: { enabled: on } });
      toast(on ? "켰습니다 — 이제 이 몰에 재고가 전송됩니다." : "껐습니다 — 이 몰은 더 이상 건드리지 않습니다.");
      renderStockSyncTab(body);
    } catch (err) { toast(err.message, true); }
  }));
  $("#ss-preview").addEventListener("click", async () => {
    const out = $("#ss-preview-out");
    out.innerHTML = `<span class="muted">계산 중…</span>`;
    try {
      const r = await api("/api/stock-sync/preview", { method: "POST", body: {} });
      const on = (r.preview || []).filter((p) => p.enabled);
      out.innerHTML = on.length
        ? on.map((p) => `<span class="chip chip-blue">${escapeHtml(p.name)} ${p.items.length}종</span>`).join(" ")
          + ` <span class="muted">— ${escapeHtml(r.note)}</span>`
        : `<span class="muted">켜진 몰이 없습니다 — 켜기 전에는 아무것도 전송되지 않습니다.</span>`;
    } catch (err) { out.innerHTML = `<span class="muted">${escapeHtml(err.message)}</span>`; }
  });
}

/* ----- 사용자 관리 ----- */

async function renderUsersTab(body) {
  const seq = ++state.renderSeq;
  body.innerHTML = `<p class="muted">불러오는 중…</p>`;
  try {
    const [registry, categories, users] = await Promise.all([
      state.registry && state.registry.menus ? state.registry : api("/api/perm-registry"),
      api("/api/categories"),
      api("/api/users"),
    ]);
    if (seq !== state.renderSeq) return; // 다른 화면으로 이동함 — 늦은 응답 폐기
    state.registry = registry; state.categories = categories; state.users = users;
  } catch (err) {
    if (seq !== state.renderSeq) return;
    body.innerHTML = `<p class="muted">${escapeHtml(err.message)}</p>`; return;
  }

  const menus = (state.registry && state.registry.menus) || [];
  const userMenus = (u) => {
    if (u.isAdmin) return "전체";
    const on = menus.filter((m) => u.perms.includes(m.view) || m.perms.some((p) => u.perms.includes(p.code)));
    return on.length
      ? on.map((m) => `<span class="chip chip-green" style="margin:1px;">${escapeHtml(m.label)}</span>`).join(" ")
      : '<span class="muted">없음 (대시보드만)</span>';
  };

  body.innerHTML = `
    <div class="card">
      <div class="inline-row">
        <h3 style="margin:0; flex:1;">사용자 목록</h3>
        <button class="btn btn-primary btn-sm" id="new-user-btn">+ 새 사용자</button>
      </div>
      <p class="muted">사용자를 [관리]로 열어 접근할 메뉴를 지정합니다. 체크하지 않은 메뉴는 그 사람 화면에 나타나지 않습니다.</p>
      <div class="table-wrap"><table>
        <thead><tr><th>아이디</th><th>이름</th><th>유형</th><th>상태</th><th>접근 메뉴</th><th></th></tr></thead>
        <tbody>${state.users.map((u) => `
          <tr>
            <td>${escapeHtml(u.username)}</td>
            <td>${escapeHtml(u.displayName)}</td>
            <td>${u.isAdmin ? '<span class="chip chip-green">관리자</span>' : '<span class="chip chip-slate">일반</span>'}</td>
            <td>${u.enabled ? '<span class="chip chip-blue">사용 중</span>' : '<span class="chip chip-red">비활성</span>'}</td>
            <td>${userMenus(u)}</td>
            <td><button class="btn btn-sm" data-edit="${u.id}">관리</button></td>
          </tr>`).join("")}
        </tbody>
      </table></div>
    </div>
    <div id="user-editor"></div>`;

  $("#new-user-btn").addEventListener("click", () => { state.editingUserId = null; renderUserEditor(); });
  $$("button[data-edit]", body).forEach((b) => b.addEventListener("click", () => {
    state.editingUserId = Number(b.dataset.edit);
    renderUserEditor();
  }));
  if (state.editingUserId !== undefined) renderUserEditor();
}

function renderUserEditor() {
  // 표 아래에 그려져 화면 밖에 있던 문제 — 다른 화면과 같이 팝업으로 띄운다
  revealPanel("#user-editor");
  const host = $("#user-editor");
  if (!host) return;
  const isNew = state.editingUserId === null;
  const u = isNew ? null : state.users.find((x) => x.id === state.editingUserId);
  if (!isNew && !u) { host.innerHTML = ""; return; }

  const menus = (state.registry && state.registry.menus) || [];
  const permChecked = (code) => (u ? u.perms.includes(code) : false);
  const menuChecked = (m) => permChecked(m.view) || m.perms.some((p) => permChecked(p.code));
  const catChecked = (id) => (u ? u.categoryIds.includes(id) : false);
  // 새 계정은 전체 분류 보기를 기본으로 둔다(꺼져 있으면 그 직원 화면에 자산이 하나도 안 보인다).
  // 담당을 나눠 쓸 때만 대표가 여기서 끄고 분류를 고른다.
  const allCats = u ? (u.isAdmin || u.allCategories) : true;
  const iAmAdmin = state.user.isAdmin;

  host.innerHTML = `
  <div class="card">
    <h3>${isNew ? "새 사용자 만들기" : `사용자 관리 — ${escapeHtml(u.displayName)} (${escapeHtml(u.username)})`}</h3>
    <div class="form-grid">
      ${isNew ? `<label>아이디 (3자 이상)<input type="text" id="ue-username"></label>` : ""}
      <label>표시 이름<input type="text" id="ue-displayname" value="${isNew ? "" : escapeHtml(u.displayName)}"></label>
      <label>${isNew ? "비밀번호 (8자 이상)" : "비밀번호 재설정 (변경 시에만 입력)"}<input type="password" id="ue-password" autocomplete="new-password"></label>
    </div>
    <div style="margin-top:12px;">
      ${iAmAdmin ? `<label class="check-line"><input type="checkbox" id="ue-admin" ${u && u.isAdmin ? "checked" : ""}> 관리자 (모든 권한 + 사용자 관리)</label>` : ""}
      ${!isNew ? `<label class="check-line"><input type="checkbox" id="ue-enabled" ${u.enabled ? "checked" : ""}> 계정 사용</label>` : ""}
    </div>
    <div id="ue-nonadmin" class="${u && u.isAdmin ? "hidden" : ""}">
      <hr style="border:none;border-top:1px solid var(--border);margin:16px 0;">
      <h3>메뉴 접근 권한</h3>
      <p class="muted">체크한 메뉴만 이 사용자의 화면에 나타납니다. 메뉴를 켜면 조회가 가능하고,
      그 안의 세부 항목으로 등록·수정 범위를 정합니다.</p>
      <div class="menu-perms">
        ${menus.map((m) => `
          <div class="menu-perm-card" data-menucard="${m.menu}">
            <label class="check-line menu-head">
              <input type="checkbox" data-menu="${m.menu}" data-view="${m.view}" ${menuChecked(m) ? "checked" : ""}>
              <b>${escapeHtml(m.label)}</b>
            </label>
            <div class="muted menu-desc">${escapeHtml(m.desc)}</div>
            <div class="menu-sub" style="${menuChecked(m) ? "" : "display:none;"}">
              ${m.perms.map((p) => `
                <label class="check-line"><input type="checkbox" data-perm="${p.code}" ${permChecked(p.code) ? "checked" : ""}> ${escapeHtml(p.label)}</label>`).join("")
                || '<span class="muted" style="font-size:12px;">조회 전용 메뉴입니다.</span>'}
            </div>
          </div>`).join("")}
      </div>
      <hr style="border:none;border-top:1px solid var(--border);margin:16px 0;">
      <details>
        <summary class="muted" style="cursor:pointer;">담당 제품 분류 제한 (선택)</summary>
        <p class="muted" style="margin:8px 0;">특정 제품 분류(PC·태블릿 등)의 자산·주문만 보게 하려면 지정하세요. 보통은 전체로 둡니다.</p>
        <label class="check-line"><input type="checkbox" id="ue-allcats" ${allCats ? "checked" : ""}> 전체 분류 접근</label>
        <div class="perm-grid" id="ue-cats" style="${allCats ? "display:none;" : ""}">
          ${(state.categories || []).map((c) => `
            <label class="check-line"><input type="checkbox" data-cat="${c.id}" ${catChecked(c.id) ? "checked" : ""}> ${escapeHtml(c.name)}${c.enabled ? "" : ' <span class="muted">(비활성)</span>'}</label>`).join("")}
        </div>
      </details>
    </div>
    <div class="editor-actions">
      <button class="btn btn-primary" id="ue-save">${isNew ? "사용자 생성" : "저장"}</button>
      <button class="btn" id="ue-cancel">닫기</button>
      ${!isNew && u && u.id !== state.user.id
        ? `<span style="flex:1"></span>
           <button class="btn btn-danger" id="ue-delete"
             title="로그인 자격만 지웁니다 — 작업 이력은 그대로 남습니다">계정 삭제</button>` : ""}
    </div>
  </div>`;

  const adminCb = $("#ue-admin");
  if (adminCb) adminCb.addEventListener("change", () => {
    $("#ue-nonadmin").classList.toggle("hidden", adminCb.checked);
  });
  // 메뉴 체크 → 하위 항목 표시/숨김. 메뉴를 끄면 하위 권한도 함께 해제된다.
  $$("input[data-menu]", host).forEach((cb) => cb.addEventListener("change", () => {
    const card = cb.closest(".menu-perm-card");
    const sub = card.querySelector(".menu-sub");
    sub.style.display = cb.checked ? "" : "none";
    if (!cb.checked) card.querySelectorAll("input[data-perm]").forEach((p) => { p.checked = false; });
  }));
  const allCatsCb = $("#ue-allcats");
  if (allCatsCb) allCatsCb.addEventListener("change", () => {
    $("#ue-cats").style.display = allCatsCb.checked ? "none" : "";
  });
  $("#ue-cancel").addEventListener("click", () => {
    state.editingUserId = undefined;
    host.innerHTML = "";
  });
  $("#ue-save").addEventListener("click", () => saveUserEditor(isNew, u));
  const delBtn = $("#ue-delete");
  if (delBtn) delBtn.addEventListener("click", () => deleteUser(u));
}

/* 계정 삭제 — 되돌릴 수 없으므로 무엇이 지워지고 무엇이 남는지 먼저 알려 준다.
   그냥 쓰지 않게만 하려면 [사용 중] 체크를 끄는 쪽(비활성)이 안전하다. */
async function deleteUser(u) {
  const ok = confirm(
    `'${u.displayName}(${u.username})' 계정을 삭제합니다.\n\n`
    + "· 지워지는 것 : 로그인 자격, 권한·분류 설정, 로그인 세션\n"
    + "· 그대로 남는 것 : 이 사람이 한 셋팅·출고·매입 기록(이름으로 남습니다)\n\n"
    + "되돌릴 수 없습니다. 잠시 못 쓰게만 하려면 [사용 중] 체크를 끄세요.\n계속할까요?");
  if (!ok) return;
  const btn = $("#ue-delete");
  if (btn) btn.disabled = true;
  try {
    await api(`/api/users/${u.id}`, { method: "DELETE" });
    toast(`${u.displayName} 계정을 삭제했습니다.`);
    state.editingUserId = undefined;
    closeModal();
    renderUsersTab($("#tab-body"));
  } catch (err) {
    toast(err.message, true);
    if (btn) btn.disabled = false;
  }
}

async function saveUserEditor(isNew, u) {
  const btn = $("#ue-save");
  btn.disabled = true;
  try {
    const isAdmin = $("#ue-admin") ? $("#ue-admin").checked : (u ? u.isAdmin : false);
    // 켜진 메뉴의 접근 권한(view) + 체크된 세부 권한
    const perms = [
      ...$$("input[data-menu]:checked").map((x) => x.dataset.view),
      ...$$("input[data-perm]:checked").map((x) => x.dataset.perm),
    ].filter((v, i, arr) => v && arr.indexOf(v) === i);
    const allCategories = $("#ue-allcats") ? $("#ue-allcats").checked : false;
    const categoryIds = $$("input[data-cat]:checked").map((x) => Number(x.dataset.cat));
    const displayName = $("#ue-displayname").value.trim();
    const password = $("#ue-password").value;

    if (isNew) {
      await api("/api/users", {
        method: "POST",
        body: {
          username: $("#ue-username").value.trim(),
          displayName, password, isAdmin,
          perms: isAdmin ? [] : perms,
          allCategories: isAdmin ? true : allCategories,
          categoryIds: isAdmin || allCategories ? [] : categoryIds,
        },
      });
      toast("사용자를 만들었습니다.");
    } else {
      // 권한/카테고리를 먼저 반영한 뒤 PATCH — 관리자 강등 시 권한 공백 방지
      if (!isAdmin) {
        await api(`/api/users/${u.id}/perms`, { method: "PUT", body: { perms } });
        await api(`/api/users/${u.id}/categories`, { method: "PUT", body: { allCategories, categoryIds } });
      }
      const patch = {};
      if (displayName && displayName !== u.displayName) patch.displayName = displayName;
      const enabledCb = $("#ue-enabled");
      if (enabledCb && enabledCb.checked !== u.enabled) patch.enabled = enabledCb.checked;
      if (state.user.isAdmin && isAdmin !== u.isAdmin) patch.isAdmin = isAdmin;
      if (password) patch.password = password;
      if (Object.keys(patch).length) await api(`/api/users/${u.id}`, { method: "PATCH", body: patch });
      toast("저장했습니다.");
    }
    state.editingUserId = undefined;
    // ★목록을 다시 그리기 전에 팝업을 닫는다. 안 닫으면 목록만 뒤에서 새로 그려지고
    //   입력 폼은 그대로 떠 있어, 사용자가 만들어졌는데도 "추가가 안 된다"로 보인다
    //   (그 상태에서 [저장]을 또 누르면 '이미 있는 아이디' 오류까지 난다). 2026-08-03 대표 지적.
    closeModal();
    renderUsersTab($("#tab-body"));
  } catch (err) {
    // 부분 실패 가능(여러 요청 중 일부만 성공) — 서버 상태를 다시 읽어 화면과 동기화
    toast(err.message + " (저장이 일부만 반영됐을 수 있어 최신 상태를 다시 불러옵니다)", true);
    closeModal();
    const body = $("#tab-body");
    if (body) renderUsersTab(body);
  }
}

/* ----- 카테고리 ----- */

async function renderCategoriesTab(body) {
  const seq = ++state.renderSeq;
  body.innerHTML = `<p class="muted">불러오는 중…</p>`;
  try {
    const cats = await api("/api/categories");
    if (seq !== state.renderSeq) return;
    state.categories = cats;
  } catch (err) {
    if (seq !== state.renderSeq) return;
    body.innerHTML = `<p class="muted">${escapeHtml(err.message)}</p>`; return;
  }

  body.innerHTML = `
    <div class="card">
      <h3>카테고리</h3>
      <p class="muted">카테고리별 담당자는 [사용자 관리]에서 사용자에게 카테고리를 지정해 구성합니다.</p>
      <div class="inline-row">
        <input type="text" id="cat-new-name" placeholder="새 카테고리 이름">
        <button class="btn btn-primary btn-sm" id="cat-add">추가</button>
      </div>
      <div class="table-wrap"><table>
        <thead><tr><th style="width:60px;">순서</th><th>이름</th><th>상태</th><th></th></tr></thead>
        <tbody>${state.categories.map((c, i) => `
          <tr>
            <td>
              <button class="btn btn-ghost btn-sm" data-move-up="${c.id}" ${i === 0 ? "disabled" : ""}>▲</button>
              <button class="btn btn-ghost btn-sm" data-move-down="${c.id}" ${i === state.categories.length - 1 ? "disabled" : ""}>▼</button>
            </td>
            <td>${escapeHtml(c.name)}</td>
            <td>${c.enabled ? '<span class="chip chip-green">사용 중</span>' : '<span class="chip chip-slate">비활성</span>'}</td>
            <td>
              <button class="btn btn-sm" data-rename="${c.id}">이름 변경</button>
              <button class="btn btn-sm" data-toggle="${c.id}">${c.enabled ? "비활성화" : "활성화"}</button>
            </td>
          </tr>`).join("")}
        </tbody>
      </table></div>
    </div>`;

  $("#cat-add").addEventListener("click", async () => {
    const name = $("#cat-new-name").value.trim();
    if (!name) return;
    try { await api("/api/categories", { method: "POST", body: { name } }); toast("추가했습니다."); renderCategoriesTab(body); }
    catch (err) { toast(err.message, true); }
  });
  $$("button[data-rename]", body).forEach((b) => b.addEventListener("click", async () => {
    const c = state.categories.find((x) => x.id === Number(b.dataset.rename));
    const name = prompt("새 이름", c.name);
    if (!name || name.trim() === c.name) return;
    try { await api(`/api/categories/${c.id}`, { method: "PATCH", body: { name: name.trim() } }); renderCategoriesTab(body); }
    catch (err) { toast(err.message, true); }
  }));
  $$("button[data-toggle]", body).forEach((b) => b.addEventListener("click", async () => {
    const c = state.categories.find((x) => x.id === Number(b.dataset.toggle));
    try { await api(`/api/categories/${c.id}`, { method: "PATCH", body: { enabled: !c.enabled } }); renderCategoriesTab(body); }
    catch (err) { toast(err.message, true); }
  }));
  const move = async (id, dir) => {
    const idx = state.categories.findIndex((x) => x.id === id);
    if (idx < 0 || !state.categories[idx + dir]) return;
    // 전체 순서를 한 번에 전송 — 서버가 단일 트랜잭션으로 반영(부분 실패 없음)
    const ids = state.categories.map((x) => x.id);
    [ids[idx], ids[idx + dir]] = [ids[idx + dir], ids[idx]];
    try {
      await api("/api/categories/reorder", { method: "POST", body: { ids } });
      renderCategoriesTab(body);
    } catch (err) { toast(err.message, true); }
  };
  $$("button[data-move-up]", body).forEach((b) => b.addEventListener("click", () => move(Number(b.dataset.moveUp), -1)));
  $$("button[data-move-down]", body).forEach((b) => b.addEventListener("click", () => move(Number(b.dataset.moveDown), +1)));
}

/* ----- 감사 로그 ----- */

async function renderAuditTab(body) {
  const seq = ++state.renderSeq;
  body.innerHTML = `
    <div class="card">
      <h3>감사 로그</h3>
      <div class="inline-row">
        <input type="text" id="audit-action" placeholder="액션 (예: login)">
        <input type="text" id="audit-user" placeholder="사용자 이름">
        <button class="btn btn-sm btn-primary" id="audit-refresh">조회</button>
      </div>
      <div class="table-wrap"><table>
        <thead><tr><th>시각</th><th>사용자</th><th>액션</th><th>대상</th><th>상세</th></tr></thead>
        <tbody id="audit-rows"><tr><td colspan="5" class="muted">불러오는 중…</td></tr></tbody>
      </table></div>
      <div id="audit-pager"></div>
    </div>`;
  const load = async () => {
    try {
      const params = new URLSearchParams();
      const a = $("#audit-action").value.trim(); if (a) params.set("action", a);
      const un = $("#audit-user").value.trim(); if (un) params.set("username", un);
      const rows = await api("/api/audit?" + params.toString());
      if (seq !== state.renderSeq || !$("#audit-rows")) return; // 화면 이동됨
      const pgAu = paged(rows, "audit");
      const abar = $("#audit-pager");
      if (abar) { abar.innerHTML = pgAu.bar; wirePager(abar, "audit", () => load()); }
      $("#audit-rows").innerHTML = rows.length ? pgAu.rows.map((r) => `
        <tr>
          <td style="white-space:nowrap;">${escapeHtml(r.ts.replace("T", " ").slice(0, 19))}</td>
          <td>${escapeHtml(r.username || "-")}</td>
          <td><span class="chip ${r.action.includes("failed") ? "chip-red" : "chip-slate"}">${escapeHtml(r.action)}</span></td>
          <td>${escapeHtml(r.target || "")}</td>
          <td class="muted" style="max-width:340px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${escapeHtml(r.detail ? JSON.stringify(r.detail) : "")}</td>
        </tr>`).join("") : `<tr><td colspan="5" class="muted">기록이 없습니다.</td></tr>`;
    } catch (err) {
      const el = $("#audit-rows");
      if (seq === state.renderSeq && el) el.innerHTML = `<tr><td colspan="5" class="muted">${escapeHtml(err.message)}</td></tr>`;
    }
  };
  $("#audit-refresh").addEventListener("click", load);
  load();
}

/* ----- 백업 ----- */

async function renderBackupTab(body) {
  const seq = ++state.renderSeq;
  body.innerHTML = `
    <div class="card">
      <div class="inline-row">
        <h3 style="margin:0; flex:1;">백업</h3>
        <button class="btn btn-primary btn-sm" id="backup-run">지금 백업</button>
      </div>
      <p class="muted" id="backup-desc">불러오는 중…</p>
      <div id="backup-warn"></div>
      <div class="table-wrap"><table>
        <thead><tr><th>파일</th><th>담긴 내용</th><th>크기</th><th>만든 시각</th><th></th></tr></thead>
        <tbody id="backup-rows"><tr><td colspan="5" class="muted">불러오는 중…</td></tr></tbody>
      </table></div>
    </div>
    <div class="card">
      <h3 style="margin-top:0;">데이터를 되살려야 할 때</h3>
      <ol class="muted" style="margin:0 0 0 18px; line-height:1.8;">
        <li>서버를 멈춥니다 (<code>stop_server.bat</code>).</li>
        <li><code>E:\\halfbook-system\\data\\hms.db</code> 를 다른 이름으로 옮겨 둡니다(지우지 마세요).</li>
        <li>되살릴 백업 파일을 <code>hms.db</code> 라는 이름으로 그 자리에 복사합니다.
            같은 폴더의 <code>hms.db-wal</code>, <code>hms.db-shm</code> 파일이 있으면 지웁니다.</li>
        <li>서버를 다시 켜고(<code>start_server.bat</code>) 주문·자산 건수가 맞는지 확인합니다.</li>
      </ol>
      <p class="muted" style="margin:8px 0 0;">되돌린 시점 이후의 작업은 사라집니다.
      헷갈리시면 파일을 지우지 마시고 그대로 두신 채 저를 부르세요.</p>
    </div>`;
  const load = async () => {
    try {
      const res = await api("/api/backups");
      const rows = res.backups || [];
      if (seq !== state.renderSeq || !$("#backup-rows")) return;
      const desc = $("#backup-desc");
      if (desc) {
        desc.innerHTML = `쓰기 작업이 있으면 <b>${res.intervalHours}시간</b>마다 자동 백업되고 ${res.retentionDays}일간 보관됩니다.`
          + (res.mirror
            ? ` 2차 사본 위치: <code>${escapeHtml(res.mirror)}</code>
               (${res.mirrorWritable === false ? '<b style="color:var(--danger)">쓸 수 없음 — 경로를 확인하세요</b>'
                 : `${res.mirrorCount}개 보관 중`})`
            : ` <b style="color:var(--danger)">2차 사본이 꺼져 있습니다</b> — 지금은 백업이 원본과 같은 디스크에만 있어, 디스크가 고장 나면 함께 사라집니다.`);
      }
      // 빈 백업만 있으면 백업이 없는 것과 같다 — 그 사실을 눈에 띄게 알린다
      const usable = rows.filter((r) => r.counts && (r.counts.orders > 0 || r.counts.assets > 0));
      const warn = $("#backup-warn");
      if (warn) {
        warn.innerHTML = rows.length && !usable.length
          ? `<div style="background:var(--danger-soft); border-radius:8px; padding:10px 12px; margin:8px 0;">
               <b>⚠ 쓸 수 있는 백업이 없습니다</b> — 목록의 백업 파일에 주문·자산이 하나도 들어 있지 않습니다.
               지금 바로 [지금 백업]을 눌러 주세요.</div>`
          : "";
      }
      $("#backup-rows").innerHTML = rows.length ? rows.map((r) => {
        const c = r.counts;
        const empty = c && !c.orders && !c.assets;
        return `<tr${empty ? ' style="opacity:.6;"' : ""}>
          <td>${escapeHtml(r.name)}</td>
          <td>${c ? `주문 ${c.orders} · 자산 ${c.assets}${empty ? ' <span class="chip chip-red">비어 있음</span>' : ""}`
                  : '<span class="muted">읽을 수 없음</span>'}</td>
          <td>${(r.sizeBytes / 1024).toFixed(1)} KB</td>
          <td>${escapeHtml(r.modifiedAt.replace("T", " ").slice(0, 19))}</td>
          <td><a class="btn btn-sm" href="/api/backups/${encodeURIComponent(r.name)}/download">내려받기</a></td>
        </tr>`;
      }).join("") : `<tr><td colspan="5" class="muted">백업이 아직 없습니다.</td></tr>`;
    } catch (err) {
      const el = $("#backup-rows");
      if (seq === state.renderSeq && el) el.innerHTML = `<tr><td colspan="5" class="muted">${escapeHtml(err.message)}</td></tr>`;
    }
  };
  $("#backup-run").addEventListener("click", async (e) => {
    const btn = e.currentTarget;
    btn.disabled = true; // 더블클릭 중복 백업 방지
    try { const r = await api("/api/backups/run", { method: "POST" }); toast(`백업 완료: ${r.name}`); load(); }
    catch (err) { toast(err.message, true); }
    finally { btn.disabled = false; }
  });
  load();
}

/* ----- 내 계정 ----- */

function renderAccountTab(body) {
  body.innerHTML = `
    <div class="card" style="max-width:480px;">
      <h3>비밀번호 변경</h3>
      <div class="form-col">
        <label>현재 비밀번호<input type="password" id="pw-current" autocomplete="current-password"></label>
        <label style="margin-top:10px;">새 비밀번호 (8자 이상)<input type="password" id="pw-new" autocomplete="new-password"></label>
        <label style="margin-top:10px;">새 비밀번호 확인<input type="password" id="pw-new2" autocomplete="new-password"></label>
      </div>
      <div class="editor-actions"><button class="btn btn-primary" id="pw-save">변경</button></div>
    </div>`;
  $("#pw-save").addEventListener("click", async () => {
    if ($("#pw-new").value !== $("#pw-new2").value) { toast("새 비밀번호 확인이 일치하지 않습니다.", true); return; }
    try {
      await api("/api/auth/password", {
        method: "PATCH",
        body: { currentPassword: $("#pw-current").value, newPassword: $("#pw-new").value },
      });
      toast("비밀번호를 변경했습니다.");
      $("#pw-current").value = $("#pw-new").value = $("#pw-new2").value = "";
    } catch (err) { toast(err.message, true); }
  });
}

/* ---------------- 부팅 ---------------- */

(async function boot() {
  try {
    const b = await api("/api/auth/bootstrap");
    if (b.needsSetup) { showAuth("setup"); return; }
  } catch (_e) { /* 서버 오류여도 로그인 시도는 가능하게 */ }
  try {
    state.user = await api("/api/auth/me");
    showApp();
  } catch (_e) {
    showAuth("login");
  }
})();
