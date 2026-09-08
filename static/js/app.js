/* OWS 프론트엔드 — Phase 0: 인증 / 쉘 / 설정(사용자·권한·카테고리·감사로그·백업) */
"use strict";

const $ = (sel, el) => (el || document).querySelector(sel);
const $$ = (sel, el) => Array.from((el || document).querySelectorAll(sel));

const state = {
  user: null,
  serverHealthTimer: null,
  view: "dashboard",
  settingsTab: "users",
  registry: null, // 권한 레지스트리 {menus:[...], flat:[...]}
  categories: [],
  users: [],
  editingUserId: undefined, // undefined=닫힘, null=신규, number=수정
  renderSeq: 0,   // 늦게 도착한 비동기 응답이 다른 화면에 쓰는 것을 차단
  pages: {},      // 목록별 현재 페이지 (공용 페이지 넘김)
};

/* ---------------- 카카오(다음) 우편번호 주소검색 ----------------

   RMS에서 그대로 이식(대표 2026-08-10: "모든 주소시스템까지").
   주소 오타는 곧 CJ 접수 실패·오배송이라, 주소를 치는 모든 자리에 🔍 버튼을 단다.

   쓰는 법:
     attachAddrSearch(btnEl, { zip: "#no-zip", addr: "#no-addr", detail: "#no-addr2" })
   detail 은 없어도 된다(고르고 나면 상세주소 칸으로 커서 이동). */
let _daumPostcodeReady = null;
function loadDaumPostcode() {
  if (window.daum && window.daum.Postcode) return Promise.resolve();
  if (_daumPostcodeReady) return _daumPostcodeReady;
  _daumPostcodeReady = new Promise((resolve, reject) => {
    const s = document.createElement("script");
    s.src = "https://t1.daumcdn.net/mapjsapi/bundle/postcode/prod/postcode.v2.js";
    s.onload = resolve;
    s.onerror = () => { _daumPostcodeReady = null; reject(new Error("주소검색 스크립트를 불러오지 못했습니다(인터넷 확인)")); };
    document.head.appendChild(s);
  });
  return _daumPostcodeReady;
}

function openAddressSearch(onPick) {
  loadDaumPostcode().then(() => {
    new daum.Postcode({
      oncomplete: (d) => onPick({
        zip: d.zonecode || "",
        addr: d.roadAddress || d.jibunAddress || "",
      }),
    }).open();
  }).catch((e) => toast(e.message, true));
}

function attachAddrSearch(btn, sel) {
  if (!btn) return;
  btn.addEventListener("click", (e) => {
    e.preventDefault();
    openAddressSearch((got) => {
      const zipEl = sel.zip && $(sel.zip);
      const addrEl = sel.addr && $(sel.addr);
      const detailEl = sel.detail && $(sel.detail);
      if (zipEl) zipEl.value = got.zip;
      if (addrEl) addrEl.value = got.addr;
      if (detailEl) detailEl.focus();          // 상세주소만 마저 치면 된다
      else if (addrEl) addrEl.focus();
      // input 이벤트를 쏴서 자동저장/검증 로직이 있으면 따라오게 한다
      [zipEl, addrEl].forEach((el) => el && el.dispatchEvent(new Event("input", { bubbles: true })));
    });
  });
}

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

/* opts.mall = true 를 주면 몰(고도몰)에 그 코드가 실제로 있는지도 함께 확인한다.
   대표 요청 2026-08-07: "제품 제목이 필요한 게 아니라, 그 제품코드가 확실히 있는지
   확인하고 매칭하려는 것." 그래서 상품명은 작게, 있고 없고를 크게 보여 준다.
   ★몰 조회는 따로 띄운다 — 몰이 느리거나 죽어 있어도 우리 자산 기준 목록은 즉시 뜬다. */
function attachCodeLookup(id, onPick, opts = {}) {
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
  let mall = null;                // {ok, codes[], exact, reason} — 아직 안 왔으면 null
  const close = () => { box.style.display = "none"; items = []; mall = null; cursor = -1; };

  /* 몰에만 있는 코드(우리가 아직 한 대도 안 산 것)도 고를 수 있게 목록에 합친다. */
  const merged = () => {
    const rows = items.map((c) => ({ ...c, inMall: undefined }));
    const have = new Set(rows.map((c) => (c.code || "").toLowerCase()));
    if (mall && mall.ok) {
      const set = new Set((mall.codes || []).map((c) => (c.code || "").toLowerCase()));
      rows.forEach((c) => { c.inMall = set.has((c.code || "").toLowerCase()); });
      (mall.codes || []).forEach((c) => {
        if (have.has((c.code || "").toLowerCase())) return;
        rows.push({ code: c.code, total: 0, shippable: 0, model: "", options: [],
                    mallOnly: true, inMall: true, mallName: c.name,
                    mallStock: c.stock, soldOut: c.soldOut });
      });
    }
    return rows;
  };

  const mallChip = (c) => {
    if (c.inMall === undefined) return "";                      // 아직 확인 전
    return c.inMall
      ? `<span class="chip chip-green" title="몰에 이 코드가 있습니다">몰 ✓</span>`
      : `<span class="chip chip-slate" title="몰에서 이 코드를 못 찾았습니다">몰 ✗</span>`;
  };

  /* 맨 윗줄 — 지금 친 글자가 몰에 그대로 있는지. 매칭해도 되는지를 이 한 줄로 판단한다. */
  const headline = () => {
    if (!opts.mall) return "";
    const q = input.value.trim();
    if (q.length < 2) return "";
    if (mall === null) return `<div class="code-head muted">몰 확인 중…</div>`;
    if (!mall.ok) {
      return `<div class="code-head muted">몰 확인 불가 — ${escapeHtml(mall.reason || "몰이 꺼져 있습니다")}</div>`;
    }
    return mall.exact
      ? `<div class="code-head ok">몰에 <b>${escapeHtml(q)}</b> 있음 — 매칭해도 됩니다</div>`
      : `<div class="code-head warn">몰에 <b>${escapeHtml(q)}</b> 없음 — 아래에서 고르거나 코드를 확인하세요</div>`;
  };

  /* ★오타 경고(2026-08-09 대표 승인) — 띄어쓰기·대소문자만 다른 기존 코드가 있으면
     새 코드를 만들지 말라고 먼저 알린다. 오타 하나가 같은 상품 재고를 둘로 쪼갠다. */
  const normWarn = () => {
    const q = input.value.trim();
    if (q.length < 2) return "";
    const norm = (s) => (s || "").toUpperCase().replace(/\s+/g, "");
    const twin = items.find((c) => c.code && c.code !== q && norm(c.code) === norm(q));
    return twin ? `<div class="code-head warn">띄어쓰기·대소문자만 다른 기존 코드
      <b>${escapeHtml(twin.code)}</b>가 이미 있습니다 — 같은 상품이면 그 코드를 그대로 쓰세요</div>` : "";
  };

  const paint = () => {
    const rows = merged();
    box.innerHTML = normWarn() + headline() + (rows.length ? rows.map((c, i) => `
      <div class="code-item${i === cursor ? " on" : ""}">
        <b>${escapeHtml(c.code)}</b>
        ${mallChip(c)}
        ${c.mallOnly
          ? `<span class="muted">우리 재고 없음</span>`
          : c.fromOrder
            /* 주문에서 쓰던 코드 — 자산엔 아직 안 들어간 값이다(대표 2026-08-17).
               이걸 안 보여 주면 자산에 코드가 없는 동안 자동완성이 통째로 빈다. */
            ? `<span class="chip chip-blue">주문 ${c.orderCount}건</span>
               <span class="muted">자산에 아직 없음</span>`
            : `<span class="chip ${c.shippable ? "chip-green" : "chip-slate"}">출고가능 ${c.shippable}</span>
               <span class="muted">보유 ${c.total}</span>`}
        ${c.model ? `<div class="muted" style="font-size:12px;">${escapeHtml(c.model)}</div>` : ""}
        ${(c.mallOnly && c.mallName) || (c.fromOrder && c.productName)
          ? `<div class="muted" style="font-size:11.5px;">${escapeHtml(c.mallName || c.productName)}</div>` : ""}
        ${!c.mallOnly && c.options && c.options.length
          ? `<div class="muted" style="font-size:11.5px;">옵션: ${
              escapeHtml(c.options.slice(0, 2).join(" · "))}</div>` : ""}
      </div>`).join("")
      : `<div class="muted" style="padding:8px 10px;">쓰고 있는 코드가 없습니다 — 새로 적으셔도 됩니다.</div>`);
    box.style.display = "block";
    $$(".code-item", box).forEach((el, i) =>
      el.addEventListener("mousedown", (e) => { e.preventDefault(); pick(rows[i]); }));
  };
  const pick = (c) => {
    if (!c) return;
    input.value = c.code;
    close();
    if (onPick) onPick(c);
  };
  const search = async () => {
    const my = ++seq;
    const q = input.value.trim();
    mall = null;
    if (opts.mall && q.length >= 2) {
      // 우리 목록을 기다리게 하지 않는다 — 몰은 늦게 와도 그때 다시 그린다.
      api("/api/mall-product-codes?q=" + encodeURIComponent(q))
        .then((r) => { if (my === seq) { mall = r; paint(); } })
        .catch(() => { if (my === seq) { mall = { ok: false, reason: "확인 실패" }; paint(); } });
    }
    try {
      const r = await api("/api/product-codes?q=" + encodeURIComponent(q));
      if (my !== seq) return;              // 늦게 온 응답이 최신 목록을 덮지 않게
      items = r.codes || []; cursor = -1;
      paint();
    } catch { close(); }
  };
  input.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(search, 200); });
  input.addEventListener("focus", () => { clearTimeout(timer); timer = setTimeout(search, 120); });
  input.addEventListener("blur", () => setTimeout(close, 150));
  input.addEventListener("keydown", (e) => {
    const rows = merged();                 // 몰에만 있는 코드도 ↑↓로 고를 수 있어야 한다
    if (box.style.display !== "block" || !rows.length) return;
    if (e.key === "ArrowDown") { e.preventDefault(); cursor = Math.min(rows.length - 1, cursor + 1); paint(); }
    else if (e.key === "ArrowUp") { e.preventDefault(); cursor = Math.max(0, cursor - 1); paint(); }
    else if (e.key === "Enter" && cursor >= 0) { e.preventDefault(); pick(rows[cursor]); }
    else if (e.key === "Escape") close();
  });

  /* ── 제품코드 모델 속성(2026-08-13 대표): DDR3/4/5·NVMe/M.2 SATA/2.5 SATA.
     "16GB 추가" 옵션이 D4/D5 어느 부품인지 고르는 판별 근거라 코드 칸 밑에 늘 보여 준다.
     ★자산 실물 스펙과 다른 층위(모델 고유 속성) — 실물 스펙 칸에는 절대 안 복사한다. */
  if (opts.mall) {
    const line = document.createElement("div");
    line.className = "muted";
    line.style.cssText = "font-size:12px; margin-top:3px;";
    (input.closest("label") || input.parentElement).appendChild(line);
    let sseq = 0;
    const chips = (s) => {
      const bits = [];
      if (s.ramGen) bits.push(s.ramGen + (s.ramGb ? ` ${s.ramGb}GB` : "")
        + (s.ramOnboard ? " (온보드+슬롯)" : ""));
      else if (s.ramOnboard) bits.push("온보드램(슬롯 업글 불가)");
      if (s.storageType) bits.push(s.storageType + (s.storageCap ? ` ${s.storageCap}` : ""));
      return bits.join(" · ");
    };
    const paintSpec = (s, code) => {
      const label = chips(s);
      const srcLabel = s.source === "manual" ? "직접 지정"
        : ({ godo: "고도몰", godomall: "고도몰", smartstore: "스마트스토어" }[s.source] || "몰 스펙");
      line.innerHTML = `🧬 ${label
        ? `<b>${escapeHtml(label)}</b> <span class="muted">(${srcLabel})</span>`
        : `모델 스펙 미확인 — 구분별 옵션 기입(D4/D5)이 보류됩니다`}
        <button type="button" class="link-btn" data-specsync style="font-size:12px;">몰에서 불러오기</button>
        <button type="button" class="link-btn" data-specedit style="font-size:12px;">직접 지정</button>`;
      $("[data-specsync]", line).addEventListener("click", async () => {
        try {
          const r = await api("/api/code-specs/sync", { method: "POST", body: { code } });
          toast(chips(r) ? `모델 스펙 확인 — ${chips(r)}`
                         : "몰 스펙 문구에서 세대·방식을 못 읽었습니다 — [직접 지정]으로 채워 주세요.");
          paintSpec(r, code);
        } catch (err) { toast(err.message, true); }
      });
      $("[data-specedit]", line).addEventListener("click", () => {
        line.innerHTML = `🧬
          <select data-sg>${["", "DDR3", "DDR4", "DDR5", "LPDDR3", "LPDDR4", "LPDDR5"].map((g) =>
            `<option value="${g}" ${s.ramGen === g ? "selected" : ""}>${g || "램 세대?"}</option>`).join("")}</select>
          <select data-sgb>${["", 4, 8, 12, 16, 24, 32, 64].map((v) =>
            `<option value="${v}" ${String(s.ramGb || "") === String(v) ? "selected" : ""}>${v ? v + "GB" : "기준 램?"}</option>`).join("")}</select>
          <select data-ss>${["", "M.2 NVMe", "M.2 SATA", "2.5 SSD", "2.5 HDD"].map((t) =>
            `<option value="${t}" ${s.storageType === t ? "selected" : ""}>${t || "저장 방식?"}</option>`).join("")}</select>
          <select data-ssc>${["", "128G", "256G", "512G", "500G", "1TB", "2TB"].map((v) =>
            `<option value="${v}" ${(s.storageCap || "") === v ? "selected" : ""}>${v || "기준 용량?"}</option>`).join("")}</select>
          <label style="display:inline;"><input type="checkbox" data-sb ${s.ramOnboard ? "checked" : ""}> 온보드</label>
          <button type="button" class="link-btn" data-sok>저장</button>`;
        $("[data-sok]", line).addEventListener("click", async () => {
          try {
            const r = await api("/api/code-specs", { method: "PATCH", body: {
              code, ramGen: $("[data-sg]", line).value,
              ramGb: Number($("[data-sgb]", line).value) || 0,
              storageType: $("[data-ss]", line).value,
              storageCap: $("[data-ssc]", line).value,
              ramOnboard: $("[data-sb]", line).checked } });
            toast("모델 스펙을 확정했습니다 — 이후 몰 동기화가 이 값을 덮지 않습니다.");
            paintSpec(r, code);
          } catch (err) { toast(err.message, true); }
        });
      });
    };
    const refreshSpec = async () => {
      const code = input.value.trim();
      const my = ++sseq;
      if (code.length < 2) { line.innerHTML = ""; return; }
      try {
        const s = await api("/api/code-specs?code=" + encodeURIComponent(code));
        if (my === sseq) paintSpec(s, code);
      } catch (_e) { if (my === sseq) line.innerHTML = ""; }
    };
    let stimer = null;
    input.addEventListener("input", () => { clearTimeout(stimer); stimer = setTimeout(refreshSpec, 400); });
    input.addEventListener("change", refreshSpec);
    if (input.value.trim()) refreshSpec();
  }
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

function stopServerHealth() {
  if (state.serverHealthTimer) clearInterval(state.serverHealthTimer);
  state.serverHealthTimer = null;
}

function startServerHealth() {
  stopServerHealth();
  const host = $("#server-health");
  const label = $("#server-health-text");
  if (!host || !label) return;

  const show = (status, message, title) => {
    host.className = `server-health ${status}`;
    label.textContent = message;
    host.title = title;
  };

  const check = async () => {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 4000);
    const started = performance.now();
    try {
      const res = await fetch(`/api/health?_=${Date.now()}`, {
        cache: "no-store",
        signal: controller.signal,
      });
      const data = await res.json();
      // 재시작 전 구버전 응답에는 database 필드가 없다. 명시적으로 false일 때만 장애 처리한다.
      if (!res.ok || !data.ok || data.database === false) throw new Error("health check failed");
      const elapsed = Math.round(performance.now() - started);
      const detail = data.database === true ? "서버·DB 정상" : "서버 정상";
      show("ok", "OWS 정상", `${detail} · 응답 ${elapsed}ms · ${data.time || ""}`);
    } catch (_err) {
      show("down", "OWS 연결 끊김", "서버 또는 데이터베이스가 응답하지 않습니다.");
    } finally {
      clearTimeout(timeout);
    }
  };

  show("checking", "OWS 확인 중", "OWS 서버와 데이터베이스 상태를 확인 중입니다.");
  check();
  state.serverHealthTimer = setInterval(check, 10000);
}

function showAuth(mode) {
  stopServerHealth();
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
  startServerHealth();
  $("#chip-role").textContent = state.user.isAdmin
    ? "관리자"
    : `메뉴 ${(state.user.menus || []).length}개 · 권한 ${state.user.perms.length}개`;
  applyMenuPermissions();
  startGlobalNotifications();
  initWhatsNew();
  renderView();
}

/* ---------------- 업데이트 내역(📢) ----------------
   대표 요청(2026-08-14): "그날 바뀐 것을 날짜별로 1. 2. 3. 으로 정리해 두면
   작업자들이 뭐가 바뀌었는지 알 수 있다."
   ★안 본 날짜가 있으면 버튼에 빨간 점을 찍는다(브라우저에 마지막으로 본 날짜를 기억).
   ★읽기는 모두, 편집은 설정 권한자만(서버가 canEdit로 알려 준다). */
const WN_SEEN_KEY = "ows.whatsnew.seen";

async function initWhatsNew() {
  const btn = $("#whatsnew-btn");
  if (!btn) return;
  if (!btn._wired) {                      // 리스너는 한 번만 — 다시 로그인해도 안 쌓인다
    btn._wired = true;
    btn.addEventListener("click", () => openWhatsNew());
  }
  try {
    const d = await api("/api/changelog?days=1");
    state.whatsNewLatest = d.latest || "";
    const seen = localStorage.getItem(WN_SEEN_KEY) || "";
    $("#whatsnew-dot").classList.toggle("hidden", !d.latest || seen >= d.latest);
  } catch (_e) { /* 못 불러와도 화면은 그대로 — 업무를 막지 않는다 */ }
}

async function openWhatsNew() {
  let d;
  try { d = await api("/api/changelog"); }
  catch (err) { toast(err.message, true); return; }
  let host = $("#whatsnew-panel");
  if (!host) {
    host = document.createElement("div");
    host.id = "whatsnew-panel";
    document.body.appendChild(host);
  }
  // ★화면 구성은 RMS의 '📋 업데이트 내역'과 똑같이 맞춘다(대표 2026-08-17) —
  //   두 시스템을 오가는 사람이 같은 자리에서 같은 모양을 보게.
  const kdate = (s) => `${s.slice(0, 4)}년 ${Number(s.slice(5, 7))}월 ${Number(s.slice(8, 10))}일`;
  const dayCard = (x) => `
    <div class="wn-day">
      <div class="wn-day-head">
        <b>${escapeHtml(kdate(x.day))}</b>
        ${x.day === d.today ? `<span class="wn-today">오늘</span>` : ""}
        <span class="wn-meta">${x.items.length}건${x.author ? " · " + escapeHtml(x.author) : ""}</span>
        ${d.canEdit ? `<button class="link-btn wn-edit" data-day="${escapeHtml(x.day)}">수정</button>` : ""}
      </div>
      <ol>${x.items.map((t) => `<li>${escapeHtml(t)}</li>`).join("")}</ol>
    </div>`;
  host.innerHTML = `
    <div class="card">
      <div class="inline-row">
        <h3 style="margin:0; flex:1;">📋 업데이트 내역</h3>
        <button class="btn btn-sm" id="wn-close">✕</button>
      </div>
      <div class="inline-row" style="align-items:flex-start;">
        <p class="muted" style="margin:0; flex:1; font-size:12.5px;">OWS에 적용된 변경 사항을
          <b>날짜별</b>로 정리합니다. 작업한 날의 변경 내용이 요약돼 올라옵니다.</p>
        ${d.canEdit ? `<button class="btn btn-sm" id="wn-new">✏️ 오늘 항목 작성</button>` : ""}
      </div>
      <div id="wn-editor"></div>
      <div id="wn-list" style="max-height:60vh; overflow-y:auto; margin-top:10px;">
        ${d.days.length ? d.days.map(dayCard).join("")
          : `<p class="muted" style="text-align:center; padding:24px 0;
               border:1px dashed var(--border); border-radius:12px;">아직 등록된 업데이트 내역이 없습니다.</p>`}
      </div>
    </div>`;
  openModalWith(host);

  // 편집 상자 — 날짜를 바꿔 지난 날짜도 고칠 수 있다(RMS와 같은 동작)
  const openEditor = (day) => {
    const items = (d.days.find((x) => x.day === day) || {}).items || [];
    $("#wn-editor", host).innerHTML = `
      <div class="wn-editor">
        <div class="inline-row">
          <input type="date" id="wn-date" value="${escapeHtml(day)}">
          <span class="muted" style="font-size:11.5px;">한 줄에 한 항목 — 저장하면 1. 2. 3. 으로 표시됩니다</span>
        </div>
        <textarea id="wn-text" rows="6" placeholder="셋팅 화면 재고를 제품코드 기준으로만 세도록 변경&#10;부품 단가표에 삭제 기능 추가">${escapeHtml(items.join("\n"))}</textarea>
        <div class="inline-row" style="justify-content:flex-end;">
          <span class="muted" style="font-size:11px; margin-right:auto;">항목을 모두 비우고 저장하면 그 날짜는 목록에서 삭제됩니다.</span>
          <button class="btn btn-sm" id="wn-cancel">취소</button>
          <button class="btn btn-sm btn-primary" id="wn-save">💾 저장</button>
        </div>
      </div>`;
    $("#wn-cancel", host).addEventListener("click", () => { $("#wn-editor", host).innerHTML = ""; });
    $("#wn-save", host).addEventListener("click", async () => {
      try {
        await api("/api/changelog", { method: "POST", body: {
          day: $("#wn-date", host).value, items: $("#wn-text", host).value } });
        toast("업데이트 내역을 저장했습니다.");
        closeModal();
        openWhatsNew();
      } catch (err) { toast(err.message, true); }
    });
  };
  const newBtn = $("#wn-new", host);
  if (newBtn) newBtn.addEventListener("click", () => openEditor(d.today));
  $$(".wn-edit", host).forEach((b) => b.addEventListener("click", () => openEditor(b.dataset.day)));
  // 열어 봤으면 '안 본 표시'를 끈다
  if (d.latest) {
    localStorage.setItem(WN_SEEN_KEY, d.latest);
    $("#whatsnew-dot").classList.add("hidden");
  }
  $("#wn-close", host).addEventListener("click", closeModal);
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
  localStorage.setItem("ows-theme", t);
}
$("#theme-toggle").addEventListener("click", () => {
  applyTheme(localStorage.getItem("ows-theme") === "dark" ? "light" : "dark");
});
applyTheme(localStorage.getItem("ows-theme") || "light");

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
    case "reports": {                     // 옛 링크 — 리포트는 설정 ▸ 매출/실적으로 통합(8/31 대표)
      state.settingsTab = "sales";
      state.salesView = "period";
      return go("settings");
    }
    case "workload": {                    // 옛 링크 — 이제 [📅 셋팅·작업 실적] 하나로 합쳐졌다
      state.settingsTab = "sales";
      state.salesView = "stats";
      return go("settings");
    }
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
      ${hasPerm("reports.view") ? `
      <div class="kpi" data-kpi="reports" style="cursor:pointer;" title="설정 ▸ 매출/실적으로 이동">
        <div class="kpi-label">이번 달 매출(몰)</div><div class="kpi-value" id="kpi-sales">-</div>
        <div class="kpi-sub muted" id="kpi-margin" style="font-size:12px;"></div></div>` : ""}
      ${hasPerm("purchase.view") && (hasPerm("reports.view") || hasPerm("settings.manage")) ? `
      <div class="kpi" data-kpi="recv" style="cursor:pointer;"
        title="설정 ▸ 매출/실적 ▸ 판매 전표(미수금)로 이동">
        <div class="kpi-label">미수금 잔액</div><div class="kpi-value" id="kpi-recv">-</div>
        <div class="kpi-sub muted" id="kpi-recv-sub" style="font-size:12px;"></div></div>` : ""}
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
    if (target === "recv") {                    // 미수금 → 설정 ▸ 매출/실적 ▸ 판매 전표
      if (!canSeeMenu("settings")) { toast("이 메뉴를 볼 권한이 없습니다.", true); return; }
      state.settingsTab = "sales";
      state.salesView = "slips";
      go("settings");
      return;
    }
    if (target === "reports") {                 // 매출 → 설정 ▸ 매출/실적(리포트 통합, 8/31)
      if (!canSeeMenu("settings")) { toast("이 메뉴를 볼 권한이 없습니다.", true); return; }
      state.settingsTab = "sales";
      state.salesView = "period";
      go("settings");
      return;
    }
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
  // ★자동화 건강 경보(2026-08-09 대표 승인) — 파이프라인이 조용히 멈추면
  //   이후 모든 숫자를 못 믿게 된다. 첫 화면에서 바로 보이게 한다.
  if (hasPerm("purchase.view")) {
    try {
      const ts = await api("/api/tms-sync/status");
      const ex = ts.exporter || {};
      if (ex.loginNeeded) {
        todo.push({ text: `⚠ <b>TMS 로그인이 풀렸습니다</b> — 185 서버에서 TMS-EXPORT.bat [2] 실행 필요`,
                    view: "purchase", tab: "base", baseView: "migrate" });
      } else if (ex.stale) {
        todo.push({ text: `⚠ TMS 받아오기가 <b>${ex.staleHours}시간</b>째 멈춰 있습니다(2시간마다가 정상)`,
                    view: "purchase", tab: "base", baseView: "migrate" });
      }
      if ((ts.failures || []).length) {
        todo.push({ text: `⚠ TMS 반영 실패 파일 <b>${ts.failures.length}개</b> — ${
          escapeHtml(ts.failures.map((x) => x.file).slice(0, 3).join(", "))}`,
                    view: "purchase", tab: "base", baseView: "migrate" });
      }
    } catch (_e) { /* 권한/오류 시 생략 */ }
    try {
      const rv = await api("/api/assets/revert-candidates");
      if (rv.count) todo.push({ text: `↩ 재고 복귀 후보 <b>${rv.count}대</b> — 실물 확인 후 원클릭 복귀`,
                                view: "purchase", tab: "assets", assetView: "revert" });
    } catch (_e) { /* 생략 */ }
    // ★이중입력 경보(2026-09-03, 창구 동시 마감 계획서 §5) — 마감일 뒤 TMS에 새 전표·자산이 들어오면
    //   첫 화면에서 바로 잡는다(같은 물건이 두 원장에 생기기 전에). 판정은 매입 ▸ 기준정보 ▸ TMS 자동 반영 카드에서.
    //   번호대 침범(TMS에 자산 5000~·전표 500~)은 마감과 무관하게 0이 아니면 띄운다.
    try {
      const co = await api("/api/cutover/alerts");
      if (co.active && co.open && co.open.total) {
        const parts = [];
        if (co.open["판매"]) parts.push(`판매 전표 ${co.open["판매"]}건`);
        if (co.open["매입"]) parts.push(`매입 전표 ${co.open["매입"]}건`);
        if (co.open["재고"]) parts.push(`자산 ${co.open["재고"]}대`);
        todo.push({ text: `⚠ 마감(${escapeHtml(co.date)}) 뒤 TMS에 새 입력 <b>${co.open.total}건</b> — ${parts.join(" · ")} → OWS 재입력 여부 확인`,
                    view: "purchase", tab: "base", baseView: "migrate" });
      }
      const inv = co.invariants || {};
      if (inv.assetNo || inv.slipNo) {
        todo.push({ text: `⚠ TMS에 OWS 번호대 침범 — 자산 <b>${inv.assetNo}</b>건 · 전표 <b>${inv.slipNo}</b>건 (TMS 손입력 번호를 예약 번호대 밖으로 바로잡기)`,
                    view: "purchase", tab: "base", baseView: "migrate" });
      }
    } catch (_e) { /* 생략 */ }
  }
  // ★A/S 처리 기한(2026-09-02 대표) — 접수만 해 놓고 잊히는 건이 없게 첫 화면에 띄운다.
  //   기준 날수는 설정(as_due_days)에서 바꿀 수 있고, 기본 7일이다.
  if (["as.view", "as.manage"].some(hasPerm)) {
    try {
      // ★ymd()를 써야 한다 — toISOString()은 UTC라 한국 시간 오전 9시 이전에
      //   하루 전 날짜가 나오고, 조회 기간이 통째로 하루씩 밀린다.
      const today = new Date();
      const to = ymd(today);
      const from = ymd(new Date(today.getTime() - 30 * 864e5));
      const st = await api(`/api/as-stats?from=${from}&to=${to}`);
      const late = (st.late || []).length;
      if (late) {
        const worst = st.late[0];
        todo.push({ text: `🛠 A/S 처리 지연 <b>${late}건</b>(${st.dueDays}일 초과` +
                          `${worst ? ` · 최장 ${worst.days}일` : ""})`, view: "as" });
      }
    } catch (_e) { /* 권한/오류 시 생략 */ }
  }
  if (hasPerm("settings.manage")) {
    try {
      const b = await api("/api/backups");
      const newest = (b.backups || []).map((x) => x.modifiedAt || "").sort().pop() || "";
      if (newest) {
        const ageH = (Date.now() - new Date(newest).getTime()) / 3600000;
        if (ageH > 6) todo.push({ text: `⚠ 백업이 <b>${Math.round(ageH)}시간</b>째 없습니다(4시간마다가 정상)`,
                                  view: "settings" });
      }
      if (b.mirrorWritable === false) {
        todo.push({ text: `⚠ 백업 미러(D:\\ows-backups)에 쓸 수 없습니다 — 디스크 확인 필요`,
                    view: "settings" });
      }
    } catch (_e) { /* 생략 */ }
  }
  // 돈 KPI(2026-08-09) — 이번 달 매출·마진(몰)과 미수금 잔액
  if (hasPerm("reports.view")) {
    try {
      const s = await api("/api/reports/summary");
      const el = $("#kpi-sales"); if (el) el.textContent = fmtWon(s.sales.revenue);
      const m = $("#kpi-margin");
      if (m) m.textContent = `마진 ${fmtWon(s.sales.margin)} (${s.sales.marginRate}%)`
        + (s.slipSales && s.slipSales.amount ? ` · 전표매출 ${fmtWon(s.slipSales.amount)}` : "");
    } catch (_e) { /* 생략 */ }
  }
  if (hasPerm("purchase.view")) {
    try {
      const rc = await api("/api/sale-slips/receivables");
      const el = $("#kpi-recv"); if (el) el.textContent = fmtWon(rc.total);
      const sub = $("#kpi-recv-sub");
      if (sub) sub.textContent = rc.count ? `거래처 ${rc.count}곳` : "미수금 없음";
    } catch (_e) { /* 생략 */ }
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
      if (t.baseView) state.baseView = t.baseView;   // 매입 ▸ 기준정보 안쪽 보기
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
              ["operations", "운영 설정"], ["label", "🏷 라벨"], ["migrate", "데이터 이관"]);
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

/* ---------------- 🏷 자산 라벨 (대표 2026-08-27, XP-DT427B 50×80) ----------------
   자산 1대 = 라벨 1장: QR(자산번호)·고객명·자산번호·모델명·옵션명.
   ★발행 버튼은 아직 화면에 안 붙였다(대표: "구현만 해두고 안 보이게") —
     openAssetLabels(주문id) 를 부르면 바로 인쇄된다. 어느 탭에 붙일지는 추후 지시. */
// ★가로로 눕힌 80×50 이 기본(대표 2026-08-27) — 왼쪽 로고·글, 오른쪽 QR.
//   w(너비mm)를 주면 그 폭 안에서 줄바꿈, 0이면 라벨 오른쪽 끝까지.
const LABEL_DEFAULT = {
  w: 80, h: 50,
  els: {
    logo:    { show: 1, x: 2,  y: 2,  size: 20 },                       // size=너비mm(높이 비율 자동)
    qr:      { show: 1, x: 47, y: 9,  size: 30 },                       // size=mm
    name:    { show: 1, x: 2,  y: 12, size: 13, align: "left", bold: 1, w: 44 },
    assetNo: { show: 1, x: 2,  y: 20, size: 12, align: "left", bold: 1, w: 44 },
    model:   { show: 1, x: 2,  y: 27, size: 9,  align: "left", bold: 0, w: 44 },
    spec:    { show: 0, x: 2,  y: 31, size: 7,  align: "left", bold: 0, lines: 1, w: 44 },
    option:  { show: 1, x: 2,  y: 34.5, size: 7, align: "left", bold: 0, lines: 4, w: 44 },
    orderNo: { show: 0, x: 2,  y: 44, size: 7,  align: "left", bold: 0, w: 44 },
    channel: { show: 0, x: 47, y: 41, size: 7,  align: "left", bold: 0, w: 30 },
    date:    { show: 0, x: 47, y: 45, size: 7,  align: "left", bold: 0, w: 30 },
    text1:   { show: 0, x: 2,  y: 2,  size: 9,  align: "left", bold: 1, w: 44, text: "" },
    text2:   { show: 0, x: 2,  y: 46, size: 7,  align: "left", bold: 0, w: 76, text: "" },
  },
};
const LABEL_EL_NAMES = { logo: "회사 로고", qr: "QR(자산번호)", assetNo: "자산번호",
                         name: "고객명", model: "모델명", spec: "상세스펙(제품코드 소제목)",
                         option: "옵션명",
                         orderNo: "주문번호", channel: "판매채널", date: "출고일(인쇄일)",
                         text1: "자유 문구 1", text2: "자유 문구 2" };

/* 옵션 라벨(대표 2026-08-31) — 셋팅·QC 작업대용 옵션표. 주문 1건 = 라벨 1장,
   QR 내용은 주문번호. 인쇄 진입은 주문관리 일괄 바 [🏷 옵션라벨](orders.js). */
const OPTLABEL_DEFAULT = {
  w: 80, h: 50,
  els: {
    logo:     { show: 0, x: 2,  y: 2,  size: 18 },
    qr:       { show: 0, x: 60, y: 2,  size: 18 },
    prodCode: { show: 1, x: 2,  y: 2,  size: 13, align: "left", bold: 1, w: 56 },
    name:     { show: 1, x: 2,  y: 9.5, size: 11, align: "left", bold: 1, w: 44 },
    model:    { show: 1, x: 2,  y: 16, size: 9,  align: "left", bold: 0, w: 76 },
    option:   { show: 1, x: 2,  y: 22, size: 7,  align: "left", bold: 0, lines: 4, w: 76 },
    prepOpts: { show: 1, x: 2,  y: 36.5, size: 7, align: "left", bold: 1, lines: 2, w: 76 },
    orderNo:  { show: 0, x: 2,  y: 45.5, size: 7, align: "left", bold: 0, w: 44 },
    channel:  { show: 0, x: 48, y: 45.5, size: 7, align: "left", bold: 0, w: 30 },
    date:     { show: 0, x: 66, y: 21, size: 7,  align: "left", bold: 0, w: 12 },
    text1:    { show: 0, x: 2,  y: 2,  size: 9,  align: "left", bold: 1, w: 44, text: "" },
    text2:    { show: 0, x: 2,  y: 45.5, size: 7, align: "left", bold: 0, w: 76, text: "" },
    // 여러 대 주문의 '몇 번째 라벨인가'(1/4). 한 대짜리 주문에서는 자동으로 안 찍힌다.
    seq:      { show: 1, x: 64, y: 9.5, size: 12, align: "right", bold: 1, w: 14 },
  },
};
const OPTLABEL_EL_NAMES = { logo: "회사 로고", qr: "QR(주문번호)", prodCode: "제품코드",
                            name: "주문자", model: "모델명", option: "옵션표(주문 옵션 원문)",
                            prepOpts: "제공옵션", orderNo: "주문번호", channel: "판매채널",
                            date: "인쇄일", text1: "자유 문구 1", text2: "자유 문구 2",
                            seq: "장수 표시(1/4 — 여러 대 주문일 때만)" };

function labelLayout(saved, defaults) {
  const base = JSON.parse(JSON.stringify(defaults || LABEL_DEFAULT));
  if (saved && saved.els) {
    base.w = Number(saved.w) || base.w;
    base.h = Number(saved.h) || base.h;
    for (const k of Object.keys(base.els)) {
      if (saved.els[k]) Object.assign(base.els[k], saved.els[k]);
    }
  }
  return base;
}

/* 라벨 1장 HTML — 인쇄 창과 설정 견본이 같은 함수를 쓴다(다르면 견본이 거짓말한다).
   요소 사전(lay.els)을 그대로 돌므로 자산/옵션 라벨이 렌더러 하나를 공유한다:
   logo=이미지, qr=QR(내용은 lb.qr, 자산 라벨은 자산번호 폴백), text 칸이 있는
   요소(자유 문구)는 그 고정 문구, 나머지는 lb[키] 텍스트 — lines 가 있으면
   그 줄수 높이에서 자른다(옵션표처럼 길어질 수 있는 칸). */
function labelHtml(lb, lay) {
  const el = (k, inner, extra) => {
    const e = lay.els[k];
    if (!e || !e.show) return "";
    // 너비: w(mm)가 있으면 그 폭(가로형에서 QR 침범 방지), 없으면 오른쪽 끝까지
    const span = e.w ? `width:${e.w}mm;`
      : `right:${e.align === "left" ? "2mm" : e.x + "mm"};`;
    return `<div class="el" data-elkey="${k}" style="left:${e.x}mm; top:${e.y}mm; ${span}
      font-size:${e.size}pt; text-align:${e.align || "left"};
      ${e.bold ? "font-weight:700;" : ""} ${extra || ""}">${inner}</div>`;
  };
  const q = lay.els.qr;
  const lg = lay.els.logo;
  const parts = [];
  if (lg && lg.show && lb.logo) {
    parts.push(`<div data-elkey="logo" style="position:absolute;
      left:${lg.x}mm; top:${lg.y}mm; width:${lg.size}mm;"><img src="${lb.logo}"
      style="width:100%; height:auto; display:block;"></div>`);
  }
  if (q && q.show) {
    parts.push(`<div data-elkey="qr" style="position:absolute; left:${q.x}mm; top:${q.y}mm;">${QR.svg(lb.qr || lb.assetNo || "-", q.size)}</div>`);
  }
  for (const k of Object.keys(lay.els)) {
    if (k === "qr" || k === "logo") continue;
    const e = lay.els[k];
    const txt = typeof e.text === "string" ? e.text : (lb[k] || "");
    const clamp = e.lines
      ? `line-height:1.3; max-height:${(e.lines || 1) * (e.size || 7) * 0.47}mm; overflow:hidden;`
      : "";
    parts.push(el(k, escapeHtml(txt || ""), clamp));
  }
  return `<div class="lbl" style="width:${lay.w}mm; height:${lay.h}mm;">${parts.join("\n    ")}</div>`;
}

const LABEL_CSS = (lay) => `
  @page { size: ${lay.w}mm ${lay.h}mm; margin: 0; }
  * { box-sizing: border-box; }
  body { margin: 0; font-family: 'Malgun Gothic', '맑은 고딕', sans-serif; }
  .lbl { position: relative; overflow: hidden; page-break-after: always; background: #fff; }
  .lbl:last-child { page-break-after: auto; }
  .el { position: absolute; white-space: pre-wrap; word-break: break-all; line-height: 1.25; color: #000; }`;

/* labels: [{assetNo, name, model, option, ...}] — 인쇄 대화상자까지 바로.
   반환: 인쇄를 열었는지(false 면 호출부가 '뽑음' 기록을 남기면 안 된다) */
function printAssetLabels(labels, lay) {
  if (!labels || !labels.length) { toast("인쇄할 라벨이 없습니다.", true); return false; }
  // ★팝업 창이 아니라 숨은 iframe 으로 인쇄한다(2026-08-31). 팝업은 클릭에서 한 박자
  //   늦은 호출(제작완료 자동인쇄: 체크 → 서버 확인 → 인쇄)이 브라우저에 차단돼
  //   라벨이 조용히 안 나갔다. iframe 인쇄는 팝업 차단과 무관하고 빈 탭도 안 남는다.
  const fr = document.createElement("iframe");
  fr.style.cssText = "position:fixed; right:0; bottom:0; width:0; height:0; border:0;";
  document.body.appendChild(fr);
  const doc = fr.contentDocument;
  doc.open();
  doc.write(`<!doctype html><html><head><meta charset="utf-8">
    <title>라벨 ${labels.length}장</title><style>${LABEL_CSS(lay)}</style></head>
    <body>${labels.map((lb) => labelHtml(lb, lay)).join("")}</body></html>`);
  doc.close();
  setTimeout(() => {
    try { fr.contentWindow.focus(); fr.contentWindow.print(); } catch (_e) {}
    // 인쇄 대화상자가 닫히기 전에 문서를 지우면 백지가 나간다 — 넉넉히 두고 치운다
    setTimeout(() => fr.remove(), 60000);
  }, 300);
  return true;
}

/* 주문 1건 → 매칭 자산 수만큼 라벨 인쇄. (숨김 기능 — 버튼은 추후 지시된 탭에 붙인다) */
async function openAssetLabels(oid) {
  let o = (state.setupOrders || []).find((x) => x.id === Number(oid));
  if (!o) {
    try { o = await api(`/api/orders/${oid}`); } catch (err) { toast(err.message, true); return; }
  }
  const assets = o.assets || [];
  if (!assets.length) { toast("매칭된 자산이 없습니다 — 자산 매칭 후 인쇄하세요.", true); return; }
  let saved = null, logo = "";
  try {
    const d = await api("/api/asset-label");
    saved = d.layout;
    logo = d.logo || "";
  } catch (_e) {}
  const lay = labelLayout(saved);
  // 상세스펙(제품코드 소제목) — 셋팅 보드가 이미 받아 왔으면 그대로, 아니면 그 코드만 묻는다.
  //   실패해도 라벨은 나가야 한다(스펙 칸만 빈다).
  let spec = "";
  const code = (o.productCode || "").trim();
  if (code && lay.els.spec && lay.els.spec.show) {
    const cached = (state.productInfo || {})[code];
    if (cached && cached.spec) spec = cached.spec;
    else {
      try {
        const r = await api("/api/orders/product-info", {
          method: "POST",
          body: { mall: "godomall", codes: [code], names: { [code]: o.productName || "" } },
        });
        const gi = (r.products || {})[code];
        if (gi && gi.spec) spec = gi.spec;
      } catch (_e) {}
    }
  }
  const today = ymd();
  printAssetLabels(assets.map((a) => ({
    assetNo: a.assetNo, name: o.recipient || "",
    model: [a.maker, a.model].filter(Boolean).join(" ") || o.productName || "",
    spec, option: o.optionName || "", orderNo: o.orderNumber || "",
    channel: o.channel || "", date: today, logo,
  })), lay);
}

/* 주문들 → 옵션라벨 인쇄(대표 2026-08-31) — 주문관리 일괄 바가 부른다. 주문 1건 = 라벨 1장.
   모델명은 자산이 매칭돼 있으면 그 기계, 아니면 상품명. 반환: 인쇄창이 실제로 열렸는지. */
/* 인쇄창을 띄운 뒤 '정말 뽑혔는지' 사람에게 확인받는다(2026-09-03 대표).
   브라우저는 인쇄 성공 여부를 알려 주지 않는다 — 물어보지 않으면 취소했는데도
   '인쇄됨 ✓'이 붙어, 나중에 '안 뽑은 것만' 골라 인쇄할 때 그 건이 빠진다. */
function confirmPrinted(n) {
  return new Promise((resolve) => {
    // 인쇄 대화상자가 뜨는 데 시간이 걸린다 — 그 위에 확인창이 겹치지 않게 잠시 기다린다
    setTimeout(() => resolve(confirm(
      `옵션라벨${n && n > 1 ? ` ${n}장` : ""}이 정상적으로 출력됐나요?\n\n`
      + "[확인] 인쇄됨으로 표시합니다.\n"
      + "[취소] 표시하지 않습니다 — 목록에서 다시 뽑을 수 있습니다.")), 1200);
  });
}

async function printOptionLabels(orderList) {
  if (!orderList || !orderList.length) return false;
  let saved = null, logo = "";
  try {
    const d = await api("/api/asset-label");
    saved = d.optionLayout;
    logo = d.logo || "";
  } catch (_e) {}
  const lay = labelLayout(saved, OPTLABEL_DEFAULT);
  const today = ymd();
  // ★한 사람이 4대를 시키면 라벨도 4장 나온다(2026-09-03 대표). 장마다 1/4·2/4…를 찍어
  //   포장 담당이 '몇 대 중 몇 번째 상자인지' 알 수 있게 한다.
  //   대수는 매칭된 자산 수를 먼저 보고(실물 기준), 없으면 주문 수량을 쓴다.
  const labels = [];
  for (const o of orderList) {
    const assets = o.assets || [];
    const total = Math.max(1, assets.length || Number(o.quantity) || 1);
    for (let i = 0; i < total; i++) {
      const a = assets[i] || assets[0];
      labels.push({
        prodCode: o.productCode || "", name: o.recipient || "",
        model: (a ? [a.maker, a.model].filter(Boolean).join(" ") : "") || o.productName || "",
        option: o.optionName || "",
        prepOpts: (o.prepOptions || []).map((x) => x.name).filter(Boolean).join(" / "),
        orderNo: o.orderNumber || o.orderNo || "", channel: o.channel || "",
        // 한 대짜리 주문에는 '1/1'을 찍지 않는다 — 쓸데없는 글자다
        seq: total > 1 ? `${i + 1}/${total}` : "",
        date: today, qr: o.orderNumber || o.orderNo || "", logo,
      });
    }
  }
  return printAssetLabels(labels, lay);
}

/* ── 라벨 편집기(자산/옵션 공용) ──────────────────────────────────────────
   견본·드래그·저장 기계는 하나고, cfg 가 요소 사전만 바꾼다:
   { saveKey: POST /asset-label 바디 키, pick: GET 응답에서 저장 레이아웃 고르기,
     defaults: 기본 배치, names: 요소 이름표, sampleData: 견본 데이터, head: 제목 HTML } */
async function labelEditor(body, cfg) {
  let saved = null, logo = "";
  try {
    const d0 = await api("/api/asset-label");
    saved = cfg.pick(d0);
    logo = d0.logo || "";
  } catch (_e) {}
  const lay = labelLayout(saved, cfg.defaults);
  const NAMES = cfg.names;
  // 견본 문구는 실제 주문·상품 데이터의 생김새 그대로(지어낸 표기는 헷갈린다 — 대표 8/28 지적).
  //   로고는 업로드 직후 견본에 바로 반영돼야 해서 getter 로 둔다.
  const SAMPLE = Object.assign({ date: ymd() }, cfg.sampleData);
  Object.defineProperty(SAMPLE, "logo", { get: () => logo });
  const canEdit = hasPerm("settings.manage");
  const numIn = (k, field, label, step) => `
    <label style="font-size:12px;">${label}
      <input type="number" data-lk="${k}" data-lf="${field}" value="${lay.els[k][field]}"
        step="${step || 1}" style="width:64px; padding:4px 6px;" ${canEdit ? "" : "disabled"}></label>`;
  const row = (k) => `
    <div class="inline-row" style="gap:8px; flex-wrap:wrap; padding:8px 0;
         border-bottom:1px solid rgba(100,116,139,.16); align-items:center;">
      <label class="check-line" style="flex:0 0 130px;">
        <input type="checkbox" data-lk="${k}" data-lf="show" ${lay.els[k].show ? "checked" : ""}
          ${canEdit ? "" : "disabled"}>
        <b style="font-size:13px;">${NAMES[k] || k}</b></label>
      ${numIn(k, "x", "왼쪽 mm")} ${numIn(k, "y", "위 mm")}
      ${numIn(k, "size", k === "qr" || k === "logo" ? "크기 mm" : "글자 pt")}
      ${k !== "qr" && k !== "logo" ? numIn(k, "w", "너비mm(0=끝)") : ""}
      ${k !== "qr" && k !== "logo" ? `<label style="font-size:12px;">정렬
        <select data-lk="${k}" data-lf="align" ${canEdit ? "" : "disabled"}>
          <option value="left" ${lay.els[k].align === "left" ? "selected" : ""}>왼쪽</option>
          <option value="center" ${lay.els[k].align === "center" ? "selected" : ""}>가운데</option>
        </select></label>
      <label class="check-line" style="font-size:12px;"><input type="checkbox"
        data-lk="${k}" data-lf="bold" ${lay.els[k].bold ? "checked" : ""} ${canEdit ? "" : "disabled"}> 굵게</label>` : ""}
      ${lay.els[k].lines !== undefined ? numIn(k, "lines", "최대 줄") : ""}
      ${typeof lay.els[k].text === "string" ? `<label style="font-size:12px; flex:1 1 200px;">문구
        <input type="text" data-lk="${k}" data-lf="text" value="${escapeHtml(lay.els[k].text || "")}"
          placeholder="예: 예시 운영사 정품 검수 완료" style="width:100%; padding:4px 6px;"
          ${canEdit ? "" : "disabled"}></label>` : ""}
    </div>`;
  body.innerHTML = `
    <div class="card">
      ${cfg.head}
      <div>
        <div>
          <div class="muted" style="font-size:12px; margin-bottom:6px;">실시간 견본 (실제 크기의 1.8배)
            — <b>끌어서 이동</b>, <b>초록 모서리를 끌면 크기</b>. 숫자칸은 미세조정용.</div>
          <!-- ★scale 은 자리 계산에 안 잡혀 아래와 겹친다(대표 지적) — 래퍼가 확대분만큼 자리를 잡는다 -->
          <div id="lb-prevwrap" style="overflow:hidden; margin-bottom:16px;">
            <div id="lb-preview" style="transform:scale(1.8); transform-origin:top left;
                 border:1px solid var(--border); display:inline-block; background:#fff; color:#000;"></div>
          </div>
        </div>
        <div style="max-width:720px; border-top:1px solid var(--border); padding-top:10px;">
          <div class="inline-row" style="gap:8px;">
            <label style="font-size:12px;">라벨 가로 mm
              <input type="number" id="lb-w" value="${lay.w}" style="width:64px; padding:4px 6px;" ${canEdit ? "" : "disabled"}></label>
            <label style="font-size:12px;">세로 mm
              <input type="number" id="lb-h" value="${lay.h}" style="width:64px; padding:4px 6px;" ${canEdit ? "" : "disabled"}></label>
          </div>
          ${Object.keys(lay.els).map(row).join("")}
          <div class="inline-row" style="gap:8px; padding:8px 0; align-items:center;
               border-bottom:1px solid rgba(100,116,139,.16);">
            <b style="font-size:13px; flex:0 0 130px;">로고 이미지</b>
            ${canEdit ? `<input type="file" id="lb-logo-file" accept="image/*" style="font-size:12px;">
            <button class="btn btn-ghost btn-sm" id="lb-logo-del" ${logo ? "" : "disabled"}>제거</button>` : ""}
            <span class="muted" style="font-size:12px;">${logo ? "등록됨" : "없음"} — 자산·옵션 라벨 공용.
              감열 인쇄라 흑백 로고를 권장합니다(회색은 점묘로 찍힘)</span>
          </div>
          <div class="inline-row" style="margin-top:10px;">
            ${canEdit ? `<button class="btn btn-sm btn-primary" id="lb-save">저장</button>` : ""}
            <button class="btn btn-sm" id="lb-test">🖨 시험 인쇄</button>
            ${canEdit ? `<button class="btn btn-ghost btn-sm" id="lb-reset"
              title="기본 배치로 되돌립니다(저장을 눌러야 확정)">기본값</button>` : ""}
          </div>
          <p class="muted" style="font-size:12px; margin-top:8px;">
            프린터 드라이버의 용지 크기를 라벨과 같게 맞춰 두세요 — 50×80 라벨지를 가로로 쓰므로 드라이버에서 80×50(또는 50×80+회전)으로 잡으면 됩니다.
            인쇄 창에서 대상 프린터로 XP-DT427B 를 고르면 됩니다.</p>
        </div>
      </div>
    </div>`;
  const readLay = () => {
    lay.w = Number($("#lb-w", body).value) || cfg.defaults.w;
    lay.h = Number($("#lb-h", body).value) || cfg.defaults.h;
    $$("[data-lk]", body).forEach((el) => {
      const t = lay.els[el.dataset.lk];
      if (!t) return;
      if (el.type === "checkbox") t[el.dataset.lf] = el.checked ? 1 : 0;
      else if (el.tagName === "SELECT" || el.dataset.lf === "text") t[el.dataset.lf] = el.value;
      else t[el.dataset.lf] = Number(el.value) || 0;
    });
  };
  const paint = () => {
    const host = $("#lb-preview", body);
    host.innerHTML = `<style>${LABEL_CSS(lay)} .lbl{page-break-after:auto;}
      [data-elkey]{cursor:move;} [data-elkey]:hover{outline:1px dashed #16a34a; outline-offset:1px;}
      .lb-handle{position:absolute; width:6px; height:6px; right:-3px; bottom:-3px;
        background:#16a34a; cursor:nwse-resize; z-index:5;}</style>`
      + labelHtml(SAMPLE, lay);
    const wrap = $("#lb-prevwrap", body);
    if (wrap) {
      wrap.style.width = Math.ceil(lay.w * 3.7795 * 1.8 + 6) + "px";
      wrap.style.height = Math.ceil(lay.h * 3.7795 * 1.8 + 6) + "px";
    }
    attachDrag();
  };
  // ── 미리보기에서 직접 조작(대표 2026-08-27 "GUI로 직접 이동·크기") — 끌면 이동,
  //    초록 모서리를 끌면 크기(QR·로고=mm, 글=글자 pt·너비). 숫자칸과 양방향 동기화.
  const pxPerMm = () => {
    const el = body.querySelector("#lb-preview .lbl");
    return el ? el.getBoundingClientRect().width / lay.w : 6.8;
  };
  const syncInputs = (k) => {
    for (const f of ["x", "y", "size", "w"]) {
      const inp = body.querySelector(`[data-lk="${k}"][data-lf="${f}"]`);
      if (inp && lay.els[k][f] !== undefined) inp.value = lay.els[k][f];
    }
  };
  function beginDrag(ev, k, mode) {
    ev.preventDefault();
    const e = lay.els[k];
    const px = pxPerMm();
    const sx = ev.clientX, sy = ev.clientY;
    const ox = e.x, oy = e.y, os = e.size, ow = e.w || 0;
    let raf = 0;
    const onMove = (mv) => {
      const dx = (mv.clientX - sx) / px, dy = (mv.clientY - sy) / px;
      if (mode === "move") {
        e.x = Math.max(0, Math.round((ox + dx) * 2) / 2);
        e.y = Math.max(0, Math.round((oy + dy) * 2) / 2);
      } else if (k === "qr" || k === "logo") {
        e.size = Math.max(5, Math.round((os + Math.max(dx, dy)) * 2) / 2);
      } else {
        e.size = Math.max(5, Math.round(os + dy * 2.83));      // 세로 끌기 = 글자 크기(pt)
        if (ow) e.w = Math.max(5, Math.round((ow + dx) * 2) / 2);  // 가로 끌기 = 너비(mm)
      }
      if (!raf) raf = requestAnimationFrame(() => { raf = 0; paint(); });
    };
    const onUp = () => {
      document.removeEventListener("mousemove", onMove);
      document.removeEventListener("mouseup", onUp);
      syncInputs(k);
      paint();
    };
    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseup", onUp);
  }
  function attachDrag() {
    body.querySelectorAll("#lb-preview [data-elkey]").forEach((node) => {
      const k = node.dataset.elkey;
      node.title = `${NAMES[k] || k} — 끌어서 이동, 초록 모서리로 크기`;
      node.addEventListener("mousedown", (ev) => beginDrag(ev, k, "move"));
      const h = document.createElement("div");
      h.className = "lb-handle";
      h.addEventListener("mousedown", (ev) => {
        ev.stopPropagation();
        beginDrag(ev, k, "size");
      });
      node.appendChild(h);
    });
  }
  paint();
  body.addEventListener("input", (e) => {
    if (e.target.closest("#lb-preview")) return;
    readLay();
    paint();
  });
  body.addEventListener("change", () => { readLay(); paint(); });
  $("#lb-save", body)?.addEventListener("click", async () => {
    readLay();
    try {
      await api("/api/asset-label", { method: "POST", body: { [cfg.saveKey]: lay } });
      toast("저장했습니다 — 다음 인쇄부터 이 배치로 나갑니다.");
    } catch (err) { toast(err.message, true); }
  });
  $("#lb-test", body).addEventListener("click", () => {
    readLay();
    printAssetLabels([Object.assign({}, SAMPLE, { logo })], lay);
  });
  $("#lb-logo-file", body)?.addEventListener("change", (e) => {
    const f = e.target.files && e.target.files[0];
    if (!f) return;
    if (f.size > 280_000) { toast("로고가 너무 큽니다 — 300KB 이내로 줄여 주세요.", true); return; }
    const rd = new FileReader();
    rd.onload = async () => {
      try {
        await api("/api/asset-label-logo", { method: "POST", body: { dataUrl: rd.result } });
        logo = rd.result;
        toast("로고를 등록했습니다.");
        paint();
        $("#lb-logo-del", body).disabled = false;
      } catch (err) { toast(err.message, true); }
    };
    rd.readAsDataURL(f);
  });
  $("#lb-logo-del", body)?.addEventListener("click", async () => {
    try {
      await api("/api/asset-label-logo", { method: "POST", body: { dataUrl: "" } });
      logo = "";
      toast("로고를 제거했습니다.");
      paint();
      $("#lb-logo-del", body).disabled = true;
    } catch (err) { toast(err.message, true); }
  });
  $("#lb-reset", body)?.addEventListener("click", () => {
    Object.assign(lay, JSON.parse(JSON.stringify(cfg.defaults)));
    labelEditor(body, cfg);
  });
}

const ASSETLABEL_CFG = {
  saveKey: "layout",
  pick: (d) => d.layout,
  defaults: LABEL_DEFAULT,
  names: LABEL_EL_NAMES,
  sampleData: { assetNo: "260821-0009", name: "홍길동", model: "LENOVO Z16 GEN1",
    spec: "i7-1260P / RAM 16G / NVMe 512G / 16인치 WUXGA / Win11",
    option: "제품등급선택 (필수): A급 외관 / S급 배터리 / 메모리 8G→16G로 UP↑ / 윈도우 복구 프로그램",
    orderNo: "2026082712345678", channel: "자사몰(업무관리)" },
  head: `<h3>🏷 자산 라벨 <span class="muted" style="font-weight:400; font-size:13px;">
      — XP-DT427B 같은 라벨 프린터용(기본 80×50mm 가로). 자산 1대 = 라벨 1장,
      QR을 스캔하면 자산번호가 읽힙니다.</span></h3>
    <p class="muted" style="margin:2px 0 10px; font-size:13px;">
      <b>라벨에 들어가는 정보</b> — 아래 목록에서 켜고 끄고, 위치·크기를 정합니다:
      회사 로고 · QR(자산번호) · 자산번호 · 고객명 · 모델명 · 상세스펙(제품코드 소제목) ·
      옵션명(주문 옵션 원문) · 주문번호 · 판매채널 · 출고일 · 자유 문구 2칸(원하는 말을 직접).</p>`,
};

const OPTLABEL_CFG = {
  saveKey: "optionLayout",
  pick: (d) => d.optionLayout,
  defaults: OPTLABEL_DEFAULT,
  names: OPTLABEL_EL_NAMES,
  sampleData: { prodCode: "840 G3_i7-6_내장", name: "홍길동", model: "HP 840 G3",
    option: "제품등급선택 (필수): A급 외관 / S급 배터리 / 메모리 8G→16G로 UP↑ / 윈도우 복구 프로그램",
    prepOpts: "리브레오피스 설치 / 윈도우 복구 프로그램",
    orderNo: "2026083112345678", channel: "자사몰(업무관리)", qr: "2026083112345678",
    seq: "1/4" },
  head: `<h3>📋 옵션 라벨 <span class="muted" style="font-weight:400; font-size:13px;">
      — 셋팅·QC 작업대용 옵션표. <b>주문 1대 = 라벨 1장</b>이라, 한 사람이 4대를 시키면
      4장이 나옵니다. 인쇄는 주문관리에서 주문을 체크하고 [🏷 옵션라벨]을 누르면 되고,
      아직 안 뽑은 주문만 골라 나갑니다.</span></h3>
    <p class="muted" style="margin:2px 0 10px; font-size:13px;">
      <b>라벨에 들어가는 정보</b> — 아래 목록에서 켜고 끄고, 위치·크기를 정합니다:
      제품코드 · 주문자 · 모델명 · 옵션표(주문 옵션 원문) · 제공옵션 · 주문번호 · 판매채널 ·
      인쇄일 · QR(주문번호) · 회사 로고 · 자유 문구 2칸 ·
      <b>장수 표시(1/4)</b> — 여러 대 주문일 때만 찍히고, 한 대짜리에는 안 나옵니다.</p>`,
};

/* 설정 ▸ 🏷 라벨 — 자산 라벨/옵션 라벨 세부 탭(대표 2026-08-31 "라벨로 변경 후 세부탭").
   두 편집기는 labelEditor 하나를 cfg 만 바꿔 쓴다 — 기계가 갈라지면 견본이 거짓말한다. */
const LABEL_VIEWS = [["asset", "자산 라벨"], ["option", "옵션 라벨"]];

function renderLabelTab(body) {
  if (!LABEL_VIEWS.some(([k]) => k === state.labelView)) state.labelView = "asset";
  body.innerHTML = `
    <div class="subtabs">${LABEL_VIEWS.map(([k, l]) =>
      `<button data-lview="${k}" class="${k === state.labelView ? "active" : ""}">${l}</button>`).join("")}</div>
    <div id="lview-body"></div>`;
  $$("button[data-lview]", body).forEach((b) => b.addEventListener("click", () => {
    state.labelView = b.dataset.lview;
    renderLabelTab(body);
  }));
  const host = $("#lview-body", body);
  if (state.labelView === "option") {
    // ★제작완료 자동인쇄(2026-08-31 대표 "설정/해제는 설정 탭 내 옵션라벨 쪽에서") —
    //   셋팅/QC에서 [제작 완료]를 체크하면 옵션라벨이 자동 인쇄된다. 전 작업대 공통 설정.
    const canEdit = hasPerm("settings.manage");
    const wrap = document.createElement("div");
    wrap.className = "card";
    wrap.innerHTML = `
      <label style="display:inline-flex; align-items:center; gap:6px;">
        <input type="checkbox" id="optauto-toggle" ${canEdit ? "" : "disabled"}>
        <b>🏷 제작완료 시 자동 인쇄</b></label>
      <span class="muted" style="margin-left:8px; font-size:13px;">셋팅/QC에서 [제작 완료]를
        체크하면 옵션라벨이 자동으로 인쇄됩니다. 이미 뽑은 주문은 "다시 출력할까요?"를 물어보고,
        끄면 송장 칸의 [🏷 옵션라벨] 버튼으로 수동 인쇄합니다.</span>`;
    body.insertBefore(wrap, host);
    (async () => {
      try {
        const d = await api("/api/asset-label");
        const cb = $("#optauto-toggle");
        if (cb) cb.checked = d.optionAutoPrint !== false;
      } catch (_e) {}
    })();
    $("#optauto-toggle", wrap).addEventListener("change", async (e) => {
      try {
        await api("/api/asset-label", { method: "POST",
          body: { optionAutoPrint: e.target.checked } });
        toast(e.target.checked
          ? "이제 [제작 완료]를 체크하면 옵션라벨이 자동 인쇄됩니다."
          : "자동 인쇄를 껐습니다 — 셋팅/QC 송장 칸의 [🏷 옵션라벨]로 수동 인쇄하세요.");
      } catch (err) {
        toast(err.message, true);
        e.target.checked = !e.target.checked;
      }
    });
  }
  return labelEditor(host, state.labelView === "option" ? OPTLABEL_CFG : ASSETLABEL_CFG);
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
    case "operations": return renderOperationsTab(body);
    case "sales": return renderSalesTab(body);
    case "label": return renderLabelTab(body);
    case "migrate": return renderMigrateTab(body);
    case "audit": return renderAuditTab(body);
    case "backup": return renderBackupTab(body);
    case "account": return renderAccountTab(body);
  }
}

async function renderOperationsTab(body) {
  body.innerHTML = `<p class="muted">불러오는 중…</p>`;
  let settings;
  try { settings = await api("/api/settings"); }
  catch (err) { body.innerHTML = `<p class="muted">${escapeHtml(err.message)}</p>`; return; }
  const enabled = settings.order_asset_duplicate
    ? !!settings.order_asset_duplicate.enabled : true;
  const rentalPrebuild = settings.prebuild_rental_asset
    ? !!settings.prebuild_rental_asset.enabled : false;
  body.innerHTML = `
    <div class="card">
      <h3 style="margin-top:0;">자산번호 중복 입력</h3>
      <label class="check-line" style="align-items:flex-start;">
        <input type="checkbox" id="op-allow-duplicate" ${enabled ? "checked" : ""}>
        <span><b>중복 자산번호 입력 허용</b><br>
          <span class="muted">체크하면 반품 후 재출고 등으로 과거 주문에 연결된 자산번호도 새 주문에 입력할 수 있습니다. 체크를 풀면 중복 입력을 차단합니다.</span>
        </span>
      </label>
      <hr style="margin:18px 0; border:0; border-top:1px solid var(--border);">
      <h3>렌탈 자산 제작·셋팅</h3>
      <label class="check-line" style="align-items:flex-start;">
        <input type="checkbox" id="op-allow-rental-prebuild" ${rentalPrebuild ? "checked" : ""}>
        <span><b>렌탈 자산 제작·셋팅 허용</b><br>
          <span class="muted">체크하면 렌탈 사업부 자산도 선제작 탭에 등록하거나 주문 셋팅의 자산번호로 입력할 수 있습니다. 체크를 풀면 두 곳 모두 다시 차단합니다.</span>
        </span>
      </label>
      <div class="editor-actions"><button class="btn btn-primary" id="op-save">저장</button></div>
    </div>
    <div id="ops-asdoc"></div>`;
  $("#op-save", body).addEventListener("click", async () => {
    const button = $("#op-save", body);
    button.disabled = true;
    try {
      await api("/api/settings", { method: "PUT", body: {
        order_asset_duplicate: { enabled: $("#op-allow-duplicate", body).checked },
        prebuild_rental_asset: { enabled: $("#op-allow-rental-prebuild", body).checked }
      }});
      toast("운영 설정을 저장했습니다.");
    } catch (err) { toast(err.message, true); }
    finally { button.disabled = false; }
  });
  // A/S 수리내역서·청구내역서에 찍히는 회사 정보·도장(2026-08-31 대표) — as.js 가 그린다
  renderAsDocSettings($("#ops-asdoc", body));
}

/* 매출 / 실적 — 판매 전표(얼마 팔렸나)와 셋팅 실적(누가 몇 대 했나)을 한자리에.
   ★판매 전표는 원래 매입 탭에 있었는데, 매입 화면에 매출이 있는 게 어색하고
     탭만 늘어서 여기로 합쳤다(대표 지시 2026-08-04). */
const SALES_VIEWS = [["period", "📊 기간 실적"], ["analysis", "판매 분석"],
                     ["ledger", "매출 대장"], ["slips", "판매 전표"],
                     ["saleAssets", "💻 판매 자산"], ["stats", "📅 셋팅·작업 실적"],
                     ["asstats", "🔧 A/S 실적"]];
// analysis·ledger = 옛 '리포트' 탭에서 이사(8/31)
// ★2026-09-03 대표: "셋팅 실적 / 기간별 작업량이 거의 같은 내용이라 서로 흡수해서
//   필요한 기능만, 두 개로 나누지 마라" → [📅 셋팅·작업 실적] 하나로 합쳤다.
//   같은 사람의 같은 일을 두 화면이 각자 세면 숫자가 반드시 어긋난다.
// ★2026-09-03 대표: "A/S 내 현황 통계는 매출실적에서 보여주면 돼" → [🔧 A/S 실적] 신설
//   (그림은 as.js renderAsStatsBody 가 그대로 그린다 — 화면을 두 벌 만들지 않는다).

/* 기간 실적 — 일/주/월/분기/연(대표 2026-08-24: "매출/부가세/순이익 한눈에 편하게").
   ★출고(shipping_done) 기준이다. 판매가는 부가세 포함 총액이라 공급가·부가세로 나눈다.
   ★계산은 서버가 /reports/summary 와 같은 잣대로 한다 — 두 화면이 다른 숫자를 말하면 안 된다. */
const PERIOD_UNITS = [["day", "일"], ["week", "주"], ["month", "월"],
                      ["quarter", "분기"], ["year", "연"]];

async function renderPeriodReport(host) {
  if (!host) return;
  state.periodUnit = state.periodUnit || "day";
  state.periodAt = state.periodAt || ymd();
  let d;
  try {
    d = await api(`/api/reports/period?unit=${state.periodUnit}&at=${state.periodAt}`);
  } catch (err) { host.innerHTML = `<p class="muted">${escapeHtml(err.message)}</p>`; return; }
  const kpi = (label, value, sub, color) => `
    <div class="kpi"${color ? ` style="border-left-color:${color};"` : ""}>
      <div class="kpi-label">${label}</div>
      <div class="kpi-value">${fmtWon(value)}</div>
      ${sub ? `<div class="muted" style="font-size:12px;">${sub}</div>` : ""}</div>`;
  const profitColor = d.profit >= 0 ? "var(--primary)" : "var(--danger)";
  host.innerHTML = `
    <div class="card">
      <div class="inline-row" style="margin:0 0 10px;">
        <div class="tabs" style="border:0; margin:0;">
          ${PERIOD_UNITS.map(([k, l]) =>
            `<button data-pu="${k}" class="${k === state.periodUnit ? "active" : ""}">${l}</button>`).join("")}
        </div>
        <span style="flex:1"></span>
        <button class="btn btn-sm" id="pr-prev">◀ 이전</button>
        <b style="align-self:center; min-width:150px; text-align:center;">${escapeHtml(d.label)}</b>
        <button class="btn btn-sm" id="pr-next">다음 ▶</button>
        <button class="btn btn-sm" id="pr-today">오늘</button>
      </div>
      <div class="kpi-row">
        ${kpi("매출 (총액)", d.revenue, `${d.orders}건 · ${d.units}대`)}
        ${kpi("공급가", d.supply, "부가세 뺀 금액")}
        ${kpi("매출 부가세", d.vat, "총액 × 10/110")}
        ${kpi("순이익", d.profit, `순이익률 ${d.profitRate}%`, profitColor)}
        ${d.tmsUnits ? kpi("＋TMS 수기 판매", d.tmsRevenue,
            `${d.tmsUnits}대 · 마진 ${fmtNum(d.tmsProfit)}원 — OWS 주문 없는 판매만(자산번호로 중복 제거)`,
            "var(--slate)") : ""}
        ${d.tmsUnits ? kpi("합산 순이익", d.combinedProfit,
            `합산 매출 ${fmtNum(d.combinedRevenue)}원 (OWS 출고 + TMS 수기)`,
            d.combinedProfit >= 0 ? "var(--primary)" : "var(--danger)") : ""}
      </div>
      <div class="table-wrap" style="margin-top:10px;"><table>
        <tbody>
          <tr><td>매출 (총액)</td><td style="text-align:right;"><b>${fmtWon(d.revenue)}</b></td></tr>
          <tr><td class="muted" style="padding-left:16px;">− 몰·PG 수수료</td>
              <td style="text-align:right;" class="muted">${fmtWon(d.fee)}</td></tr>
          <tr><td class="muted" style="padding-left:16px;">− 환불</td>
              <td style="text-align:right;" class="muted">${fmtWon(d.refund)}</td></tr>
          <tr><td>= 실입금</td><td style="text-align:right;">${fmtWon(d.netRevenue)}</td></tr>
          <tr><td class="muted" style="padding-left:16px;">− 매입 원가</td>
              <td style="text-align:right;" class="muted">${fmtWon(d.buyCost)}</td></tr>
          <tr><td class="muted" style="padding-left:16px;">− 부품·수리비</td>
              <td style="text-align:right;" class="muted">${fmtWon(d.repairCost)}</td></tr>
          <tr><td class="muted" style="padding-left:16px;">− 택배비</td>
              <td style="text-align:right;" class="muted">${fmtWon(d.shippingCost)}</td></tr>
          <tr><td><b>= 순이익</b></td>
              <td style="text-align:right;"><b style="color:${profitColor};">${fmtWon(d.profit)}</b></td></tr>
        </tbody>
        <tfoot>
          <tr><th>부가세 — 매출세액</th><th style="text-align:right;">${fmtWon(d.vat)}</th></tr>
          <tr><td class="muted">− 매입세액 (수리비에 포함된 부가세)</td>
              <td style="text-align:right;" class="muted">${fmtWon(d.repairVat)}</td></tr>
          <tr><th>= 낼 세금 (추정)</th>
              <th style="text-align:right;">${fmtWon(d.vatPayable)}</th></tr>
        </tfoot>
      </table></div>
      <p class="muted" style="margin:8px 0 0; font-size:12px;">
        ★<b>출고 확인</b>된 주문 기준입니다(${escapeHtml(d.from)} ~ ${escapeHtml(d.to)}).
        회수 완료된 건은 빠집니다. 매입 원가가 비어 있는 자산이 있으면 순이익이 실제보다 높게 나옵니다.</p>
    </div>`;
  $$("button[data-pu]", host).forEach((b) => b.addEventListener("click", () => {
    state.periodUnit = b.dataset.pu;
    renderPeriodReport(host);
  }));
  $("#pr-prev").addEventListener("click", () => { state.periodAt = d.prev; renderPeriodReport(host); });
  $("#pr-next").addEventListener("click", () => { state.periodAt = d.next; renderPeriodReport(host); });
  $("#pr-today").addEventListener("click", () => { state.periodAt = ymd(); renderPeriodReport(host); });
}

function renderSalesTab(body) {
  // 실적만 볼 수 있는 작업자에게 판매 금액까지 보여줄 이유는 없다
  const canMoney = hasPerm("settings.manage") || hasPerm("reports.view")
    || hasPerm("purchase.money");
  // ★기간 실적도 매출 화면(2026-08-27 대표 "실제 매출을 보는 것") — 금액 권한 필요
  const views = SALES_VIEWS.filter(([k]) => {
    // A/S 실적은 A/S 권한이 있는 사람 몫이다(A/S 담당자는 매출 권한이 없을 수 있다)
    if (k === "asstats") return hasPerm("as.view") || hasPerm("as.manage");
    return canMoney || (k !== "slips" && k !== "saleAssets" && k !== "period"
                        && k !== "analysis" && k !== "ledger");
  });
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
  if (state.salesView === "period") renderPeriodReport(host);
  else if (state.salesView === "analysis") renderAnalysisView(host);   // reports.js
  else if (state.salesView === "ledger") renderLedgerView(host);   // reports.js
  else if (state.salesView === "slips") renderSaleSlips(host);   // purchase.js
  else if (state.salesView === "saleAssets") renderSaleAssets(host);   // purchase.js
  else if (state.salesView === "asstats") renderAsStatsBody(host);   // as.js
  else renderSetupStatsTab(host);
}

async function renderPrebuildStatsTab(body) {
  state.prebuildStats = state.prebuildStats || { period: "month", anchor: ymd() };
  const s = state.prebuildStats;
  body.innerHTML = `<p class="muted">불러오는 중…</p>`;
  let d;
  try { d = await api(`/api/reports/prebuild-stats?period=${s.period}&date=${s.anchor}`); }
  catch (err) { body.innerHTML = `<p class="muted">${escapeHtml(err.message)}</p>`; return; }
  const card = (label, n, hint, color) => `<div class="kpi"${color ? ` style="border-left-color:${color};"` : ""}>
    <div class="kpi-label">${label}</div><div class="kpi-value">${n}대</div>
    <div class="muted" style="font-size:12px;">${hint}</div></div>`;
  const mark = (on, by, at) => on
    ? `<b>${escapeHtml(by || "-")}</b><div class="muted">${escapeHtml(String(at || "").replace("T", " ").slice(0, 16))}</div>`
    : '<span class="muted">-</span>';
  body.innerHTML = `
    <div class="card"><div class="inline-row" style="margin:0;">
      <div class="tabs" style="border:0; margin:0;">${SETUP_PERIODS.map(([k, l]) =>
        `<button data-pbps="${k}" class="${k === s.period ? "active" : ""}">${l}</button>`).join("")}</div>
      <span style="flex:1"></span><button class="btn btn-sm" id="pbps-prev">◀ 이전</button>
      <b style="min-width:160px;text-align:center;">${escapeHtml(d.period.label)}</b>
      <button class="btn btn-sm" id="pbps-next">다음 ▶</button><button class="btn btn-sm" id="pbps-today">오늘</button>
    </div></div>
    <div class="kpi-row">
      ${card("선제작완료", d.totals.production, "이 기간에 완료", "var(--primary)")}
      ${card("선SW검수 완료", d.totals.inspection, "이 기간에 검수", "#7c3aed")}
      ${card("출고 준비완료", d.totals.ready, "이 기간에 준비", "#0f766e")}
      ${card("주문 사용완료", d.totals.used, "이 기간에 셋팅에서 사용", "#2563eb")}
      ${card("현재 사용 대기", d.currentReady, "기간과 무관한 현재 재고", "var(--slate)")}
    </div>
    <div class="card"><h3>작업자별 선제작</h3><div class="table-wrap"><table>
      <thead><tr><th>작업자</th><th>선제작완료</th><th>선SW검수</th><th>출고 준비</th><th>주문 사용</th></tr></thead>
      <tbody>${d.staff.map((x) => `<tr><td><b>${escapeHtml(x.name)}</b></td><td>${x.production}대</td><td>${x.inspection}대</td><td>${x.ready}대</td><td>${x.used}대</td></tr>`).join("")
        || '<tr><td colspan="5" class="muted">이 기간의 선제작 작업이 없습니다.</td></tr>'}</tbody>
    </table></div></div>
    <div class="card"><h3>선제작 자산 상세</h3><div class="table-wrap"><table>
      <thead><tr><th>자산번호</th><th>제품 / 사양</th><th>선제작완료</th><th>선SW검수</th><th>출고 준비</th><th>주문 사용</th></tr></thead>
      <tbody>${d.rows.map((r) => `<tr><td><b>${escapeHtml(r.assetNo)}</b><div class="muted">${escapeHtml(r.productCode || "")}</div></td>
        <td>${escapeHtml(r.product || "-")}<div class="muted">RAM ${escapeHtml(r.ram || "-")} · SSD ${escapeHtml(r.ssd || "-")}</div></td>
        <td>${mark(r.included.production, r.productionBy, r.productionAt)}</td>
        <td>${mark(r.included.inspection, r.inspectionBy, r.inspectionAt)}</td>
        <td>${mark(r.included.ready, r.readyBy, r.readyAt)}</td>
        <td>${r.included.used ? `${mark(true, r.usedBy, r.usedAt)}<div class="muted">주문 ${escapeHtml(r.usedOrderNo || "#" + r.usedOrderId)} ${escapeHtml(r.usedRecipient || "")}</div>` : '<span class="muted">-</span>'}</td></tr>`).join("")
        || '<tr><td colspan="6" class="muted">이 기간의 선제작 내역이 없습니다.</td></tr>'}</tbody>
    </table></div></div>`;
  $$("button[data-pbps]", body).forEach((b) => b.addEventListener("click", () => {
    s.period = b.dataset.pbps; renderPrebuildStatsTab(body);
  }));
  $("#pbps-prev", body).addEventListener("click", () => { s.anchor = shiftAnchor(s.period, s.anchor, -1); renderPrebuildStatsTab(body); });
  $("#pbps-next", body).addEventListener("click", () => { s.anchor = shiftAnchor(s.period, s.anchor, 1); renderPrebuildStatsTab(body); });
  $("#pbps-today", body).addEventListener("click", () => { s.anchor = ymd(); renderPrebuildStatsTab(body); });
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

/* 단계 이름 — 기간별 작업량이 세던 것 그대로. 색은 workload.css 의 work-stage-tag--*. */
const WORK_STAGES = [["production", "제작 완료"], ["inspection", "SW 검수 완료"],
                     ["specChange", "사양변경"], ["prebuildProduction", "선제작 완료"],
                     ["prebuildInspection", "선SW 검수 완료"]];

async function renderSetupStatsTab(body) {
  const seq = ++state.renderSeq;
  if (!state.setupStats) {
    // ★UTC 변환 금지 — 오전 9시 이전에 하루 밀린다
    state.setupStats = { period: "month", anchor: ymd(), from: "", to: "",
      worker: "", stage: "all" };
  }
  const s = state.setupStats;
  const { period, anchor } = s;
  body.innerHTML = `<p class="muted">불러오는 중…</p>`;
  let d;
  try {
    const statsUrl = period === "custom"
      ? `/api/reports/setup-stats?from=${encodeURIComponent(s.from)}&to=${encodeURIComponent(s.to)}`
      : `/api/reports/setup-stats?period=${period}&date=${anchor}`;
    d = await api(statsUrl);
    s.from = d.period.from;
    s.to = d.period.to;
    if (seq !== state.renderSeq) return;
  } catch (err) {
    if (seq === state.renderSeq) body.innerHTML = `<p class="muted">${escapeHtml(err.message)}</p>`;
    return;
  }
  // 단계별 작업량은 관리자만 볼 수 있다(원래 [기간별 작업량]이 관리자 전용이었다).
  // 권한이 없으면 그 칸만 빠지고 셋팅 실적은 그대로 보인다 — 화면이 통째로 죽지 않게.
  let w = null;
  try { w = await api(`/api/workload-stats?from=${d.period.from}&to=${d.period.to}`); }
  catch (_e) { w = null; }
  if (seq !== state.renderSeq) return;

  const diff = d.previous.diff;
  const diffHtml = diff === 0 ? '<span class="muted">지난 기간과 같음</span>'
    : `<span style="color:${diff > 0 ? "var(--primary)" : "var(--danger)"};">
         ${diff > 0 ? "▲" : "▼"} ${Math.abs(diff)}대</span>
       <span class="muted">(${escapeHtml(d.previous.label)} ${d.previous.units}대)</span>`;

  // 두 창구의 사람 이름을 하나로 모은다 — 셋팅 실적 순서(대수 많은 순)를 먼저 지킨다
  const wByName = {};
  ((w && w.workers) || []).forEach((x) => { wByName[x.worker] = x; });
  const names = d.staff.map((x) => x.name);
  Object.keys(wByName).forEach((n) => { if (!names.includes(n)) names.push(n); });
  const setupOf = Object.fromEntries(d.staff.map((x) => [x.name, x]));
  if (s.worker && !names.includes(s.worker)) s.worker = "";

  body.innerHTML = `
    <div class="card">
      <div class="inline-row" style="margin:0;">
        <h3 style="margin:0; flex:1;">📅 셋팅 · 작업 실적</h3>
        <div class="tabs" style="border:0; margin:0;">
          ${SETUP_PERIODS.map(([k, label]) =>
            `<button data-sp="${k}" class="${k === period ? "active" : ""}">${label}</button>`).join("")}
        </div>
      </div>
      <p class="muted" style="margin:6px 0 0;">
        <b>셋팅 대수</b>는 제품을 준비해 <b>SW 검수 완료까지 끝낸 것</b>을 한 대로 셉니다(매칭한 자산 수 기준).
        ${w ? "옆의 <b>단계별 건수</b>는 제작·검수·사양변경·선제작을 각각 센 것입니다 — 대수가 아니라 건수입니다."
            : ""}</p>
      <form id="ss-range" class="workload-filter" style="margin-top:12px;">
        <label>시작일<input id="ss-from" type="date" value="${escapeHtml(s.from)}" required></label>
        <label>종료일<input id="ss-to" type="date" value="${escapeHtml(s.to)}" required></label>
        <button class="btn btn-primary btn-sm">선택 날짜 조회</button>
      </form>
      <div class="inline-row" style="margin-top:10px;">
        <button class="btn btn-sm" id="ss-prev">◀ 이전</button>
        <b style="font-size:15px;">${escapeHtml(d.period.label)}</b>
        <button class="btn btn-sm" id="ss-next">다음 ▶</button>
        <button class="btn btn-ghost btn-sm" id="ss-today">오늘</button>
        <span class="muted" style="font-size:12px;">${escapeHtml(d.period.from)} ~ ${escapeHtml(d.period.to)}</span>
      </div>
      <div class="kpi-row" style="margin-top:12px;">
        <div class="kpi"><div class="kpi-label">셋팅 대수</div>
          <div class="kpi-value">${d.total.units}대</div>
          <div class="kpi-sub">${diffHtml}</div></div>
        <div class="kpi"><div class="kpi-label">주문 건수</div>
          <div class="kpi-value">${d.total.orders}건</div></div>
        <div class="kpi"><div class="kpi-label">참여 담당자</div>
          <div class="kpi-value">${d.total.staffCount}명</div></div>
        ${w ? `<div class="kpi"><div class="kpi-label">단계별 작업</div>
          <div class="kpi-value">${w.total}건</div>
          <div class="kpi-sub">처리 수량 ${w.units}대</div></div>` : ""}
      </div>
    </div>

    <div class="card">
      <h3>작업자별 카드 ${w ? `<span class="muted" style="font-weight:400; font-size:13px;">
        — 카드를 누르면 그 사람이 끝낸 건이 아래에 열립니다</span>` : ""}</h3>
      ${names.length ? `<div class="workload-cards">${names.map((name) => {
          const x = setupOf[name] || { name, units: 0, orders: 0, inspected: 0 };
          const wk = wByName[name];
          const pct = d.total.units ? Math.round(x.units / d.total.units * 100) : 0;
          const stages = (wk && wk.stages) || {};
          const setupTotal = (stages.production || 0) + (stages.inspection || 0)
            + (stages.specChange || 0);
          const prebuildTotal = (stages.prebuildProduction || 0)
            + (stages.prebuildInspection || 0);
          const stat = (key, label) => `<div><dt>${label}</dt><dd>${stages[key] || 0}건</dd></div>`;
          return `<button type="button" class="workload-card ${s.worker === name ? "is-selected" : ""}"
              data-ssworker="${escapeHtml(name)}" ${w ? "" : "disabled"}>
            <div class="workload-card-head"><h3>${escapeHtml(name)}</h3>
              <strong>셋팅 ${x.units}대</strong></div>
            <p>주문 ${x.orders}건 · 검수 ${x.inspected || 0}대 · 셋팅 비중 ${pct}%</p>
            ${w ? `<div class="workload-card-groups">
              <section class="workload-card-group workload-card-group--setup">
                <h4><span>셋팅 작업</span><b>${setupTotal}건</b></h4><dl>
                  ${stat("production", "제작 완료")}${stat("inspection", "SW 검수 완료")}${stat("specChange", "사양변경")}
                </dl>
              </section>
              <section class="workload-card-group workload-card-group--prebuild">
                <h4><span>선제작</span><b>${prebuildTotal}건</b></h4><dl>
                  ${stat("prebuildProduction", "선제작 완료")}${stat("prebuildInspection", "선SW 검수 완료")}
                </dl>
              </section>
            </div>` : ""}
          </button>`;
        }).join("")}</div>`
        : `<p class="muted">이 기간에 SW 검수 완료된 셋팅이 없습니다.</p>`}
      <p class="muted" style="margin-top:8px; font-size:12px;">
        '셋팅 대수'는 제작을 완료한 사람 기준입니다. 검수만 맡은 경우는 [검수] 칸에 따로 표시되며
        합계에는 더하지 않습니다(같은 제품을 두 번 세지 않기 위해서입니다).</p>
      <div id="ss-work"></div>
    </div>

    <div class="card">
      <h3>날짜별 <span class="muted" style="font-size:13px;">— 날짜를 누르면 그날 목록이 열립니다</span></h3>
      <div id="ss-cal"></div>
      <div id="ss-day"></div>
    </div>`;

  $$("button[data-sp]", body).forEach((b) => b.addEventListener("click", () => {
    s.period = b.dataset.sp;
    renderSetupStatsTab(body);
  }));
  $("#ss-range", body).addEventListener("submit", (e) => {
    e.preventDefault();
    const from = $("#ss-from", body).value, to = $("#ss-to", body).value;
    if (!from || !to) return;
    if (from > to) { toast("종료일은 시작일보다 빠를 수 없습니다.", true); return; }
    s.period = "custom";
    s.from = from;
    s.to = to;
    renderSetupStatsTab(body);
  });
  $("#ss-prev").addEventListener("click", () => {
    if (period === "custom") {
      const start = new Date(s.from + "T12:00:00"), end = new Date(s.to + "T12:00:00");
      const span = Math.round((end - start) / 86400000) + 1;
      start.setDate(start.getDate() - span); end.setDate(end.getDate() - span);
      s.from = ymd(start); s.to = ymd(end);
    } else s.anchor = shiftAnchor(period, anchor, -1);
    renderSetupStatsTab(body);
  });
  $("#ss-next").addEventListener("click", () => {
    if (period === "custom") {
      const start = new Date(s.from + "T12:00:00"), end = new Date(s.to + "T12:00:00");
      const span = Math.round((end - start) / 86400000) + 1;
      start.setDate(start.getDate() + span); end.setDate(end.getDate() + span);
      s.from = ymd(start); s.to = ymd(end);
    } else s.anchor = shiftAnchor(period, anchor, 1);
    renderSetupStatsTab(body);
  });
  $("#ss-today").addEventListener("click", () => {
    s.period = "day";
    s.anchor = ymd();
    renderSetupStatsTab(body);
  });
  if (w) {
    $$("[data-ssworker]", body).forEach((card) => card.addEventListener("click", () => {
      s.worker = s.worker === card.dataset.ssworker ? "" : card.dataset.ssworker;
      $$("[data-ssworker]", body).forEach((x) =>
        x.classList.toggle("is-selected", x.dataset.ssworker === s.worker));
      renderWorkDetail(w);
    }));
    renderWorkDetail(w);
  }
  renderSetupCalendar(d);
}

/* 담당자를 고르면 그 사람이 그 기간에 끝낸 건을 그대로 보여 준다
   (옛 [기간별 작업량]의 '작업자별 완료 건' 표 — 화면만 합치고 내용은 그대로다). */
function renderWorkDetail(w) {
  const host = $("#ss-work");
  if (!host) return;
  const s = state.setupStats;
  if (!s.worker) {
    host.innerHTML = `<p class="muted" style="margin:10px 0 0; font-size:12.5px;">
      작업자 카드를 누르면 그 사람이 끝낸 건이 여기에 열립니다.</p>`;
    return;
  }
  const rows = (w.workOrders || []).filter((x) =>
    x.worker === s.worker && (s.stage === "all" || x.stage === s.stage));
  host.innerHTML = `
    <div style="border-top:1px solid var(--border); margin-top:12px; padding-top:10px;">
      <div class="inline-row" style="margin:0 0 8px; flex-wrap:wrap;">
        <b style="flex:0 0 auto;">${escapeHtml(s.worker)} — 끝낸 건 ${rows.length}건</b>
        <span style="flex:1"></span>
        <div class="workload-stage-tags" style="padding:0;">
          <button data-wstage="all" class="${s.stage === "all" ? "is-active" : ""}">전체</button>
          ${WORK_STAGES.map(([k, l]) =>
            `<button data-wstage="${k}" class="${s.stage === k ? "is-active" : ""}">${escapeHtml(l)}</button>`).join("")}
        </div>
        <button class="btn btn-ghost btn-sm" id="ss-workclose">닫기</button>
      </div>
      ${rows.length ? `<div class="table-wrap"><table>
        <thead><tr><th>구분</th><th>완료일시</th><th>채널 / 주문번호</th><th>상품 정보</th>
          <th>수량</th><th>수취인</th><th>관리번호</th></tr></thead>
        <tbody>${rows.map((x) => `<tr>
          <td><span class="work-stage-tag work-stage-tag--${escapeHtml(x.stage)}">${escapeHtml(x.stageLabel)}</span></td>
          <td class="muted">${escapeHtml(String(x.completedAt || "").replace("T", " ").slice(0, 16))}</td>
          <td>${chBadge(x.channel)}<br><b>${escapeHtml(x.orderNumber || "-")}</b></td>
          <td>${x.productCode ? `<b class="order-product-code">${escapeHtml(x.productCode)}</b><br>` : ""}${escapeHtml(x.productName)}
            ${x.optionName ? `<div class="muted workload-meta">${escapeHtml(x.optionName)}</div>` : ""}</td>
          <td>${x.quantity}대</td><td>${escapeHtml(x.recipient)}</td>
          <td class="muted">${escapeHtml(x.managementNumber || "미등록")}</td>
        </tr>`).join("")}</tbody></table></div>`
        : `<p class="muted">이 조건으로 끝낸 작업이 없습니다.</p>`}
    </div>`;
  $$("button[data-wstage]", host).forEach((b) => b.addEventListener("click", () => {
    s.stage = b.dataset.wstage;
    renderWorkDetail(w);
  }));
  $("#ss-workclose", host).addEventListener("click", () => {
    s.worker = "";
    $$("[data-ssworker]").forEach((x) => x.classList.remove("is-selected"));
    renderWorkDetail(w);
  });
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

// 구분별 부품 연결의 구분 축 — 서버 purchase.PART_GEN_AXES와 같은 목록(바꿀 때 같이)
const PART_GEN_AXES = ["DDR3", "DDR4", "DDR5", "LPDDR3", "LPDDR4", "LPDDR5",
                       "M.2 NVMe", "M.2 SATA", "2.5 SSD", "2.5 HDD"];

async function renderPrepTab(body) {
  const seq = ++state.renderSeq;
  body.innerHTML = `<p class="muted">불러오는 중…</p>`;
  let data, channels, partsBook;
  try {
    [data, channels, partsBook] = await Promise.all([
      api("/api/prep-options"), api("/api/order-channels").catch(() => []),
      api("/api/parts").catch(() => [])]);
    if (seq !== state.renderSeq) return;
  } catch (err) {
    if (seq === state.renderSeq) body.innerHTML = `<p class="muted">${escapeHtml(err.message)}</p>`;
    return;
  }
  const chNames = (channels || []).map((c) => c.channel).filter(Boolean);
  const matchTypes = data.matchTypes || [];
  const liveParts = (partsBook || []).filter((p) => p.enabled);

  let notice = { text: "" };
  try { notice = await api("/api/setup-notice"); } catch (_e) {}
  body.innerHTML = `
    <div class="card">
      <h3>📌 셋팅 필수 참고 사항</h3>
      <p class="muted">셋팅/QC 보드 상단(부품 수량 아래)에 노란 카드로 뜹니다.
        줄바꿈 그대로 보이고, <b>비워서 저장하면 카드가 사라집니다</b>.</p>
      <textarea id="pn-text" rows="4" style="width:100%; max-width:720px; padding:10px 12px;
        border:1px solid var(--border); border-radius:8px; background:var(--bg); resize:vertical;"
        placeholder="예: 8월 셋팅분부터 정품 스티커 위치는 좌측 하단 통일">${escapeHtml(notice.text || "")}</textarea>
      <div class="inline-row" style="margin-top:8px;">
        <button class="btn btn-sm btn-primary" id="pn-save">저장</button>
        ${notice.updatedAt ? `<span class="muted" style="font-size:12px;">마지막 수정
          ${escapeHtml((notice.updatedAt || "").slice(0, 16).replace("T", " "))} ${escapeHtml(notice.updatedBy || "")}</span>` : ""}
      </div>
    </div>
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
            ${o.kind ? ' <span class="chip chip-blue" title="시스템이 관리하는 옵션 — 조건·부품 연결 없이 주문 제목 파싱으로 자동으로 붙습니다">⚙ 시스템(판매 구성)</span>' : ""}
            ${o.enabled ? "" : ' <span class="chip chip-slate">사용 안 함</span>'}
            ${o.note ? `<div class="muted" style="font-size:13px;">${escapeHtml(o.note)}</div>` : ""}
          </div>
          <div class="inline-row" style="margin:0;">
            <button class="btn btn-sm" data-toggle="${o.id}">${o.enabled ? "사용 안 함" : "다시 사용"}</button>
            ${o.kind ? "" : `<button class="btn btn-sm btn-danger" data-del="${o.id}">삭제</button>`}
          </div>
        </div>
        ${o.kind ? "" : `
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
        <div class="inline-row" style="border-top:1px dashed var(--border); padding-top:10px; flex-wrap:wrap;">
          <span style="font-size:13px;" title="이 옵션을 셋팅에서 체크하는 순간, 매칭된 자산에 부품 단가표의 '우리 매입 단가'가 원가로 자동 기입됩니다. 고객에게 받는 옵션가와는 무관합니다.">
            🔩 부품 원가 자동 기입</span>
          <select data-part="${o.id}">
            <option value="">연결 안 함</option>
            ${liveParts.map((p) => `<option value="${p.id}" ${o.partId === p.id ? "selected" : ""}>
              ${escapeHtml(p.name)} — ${(p.price || 0).toLocaleString("ko-KR")}원</option>`).join("")}
          </select>
          <input type="number" data-partqty="${o.id}" min="1" max="10" value="${o.partQty || 1}"
                 title="수량 (예: 8G 두 장이면 2)" style="width:56px;">
          <button class="btn btn-sm" data-part-save="${o.id}">부품 연결 저장</button>
          ${o.part ? `<span class="chip ${o.part.price ? "chip-green" : "chip-red"}">
              체크하면 ${escapeHtml(o.part.name)}${(o.partQty || 1) > 1 ? ` ×${o.partQty}` : ""} =
              ${((o.part.price || 0) * (o.partQty || 1)).toLocaleString("ko-KR")}원이 원가로 들어감${
              o.part.price ? "" : " — ★단가 0원, 단가표부터 채우세요"}</span>` : ""}
        </div>
        <div class="inline-row" style="padding-left:18px; flex-wrap:wrap;">
          <span class="muted" style="font-size:12.5px;"
            title="같은 옵션이라도 제품코드 스펙에 따라 다른 부품이 들어갈 때 씁니다 — 예: 16GB 추가가 DDR4 모델이면 D4 16G, DDR5 모델이면 D5 16G.&#10;구분별 연결이 하나라도 있으면 위의 단일 연결 대신 이것이 쓰이고, 주문 제품코드의 스펙을 몰라 판별이 안 되면 기입을 보류하고 셋팅 화면에 알립니다.">
            └ 구분별 연결(DDR4/DDR5·NVMe/SATA)</span>
          ${(o.partMap || []).map((m, i) => `
            <span class="chip ${m.part ? "chip-blue" : "chip-red"}">${escapeHtml(m.gen || "")} → ${
              m.part ? escapeHtml(m.part.name) : "부품 없음"}
              <button class="link-btn" data-pm-del="${o.id}|${i}" title="이 구분 연결을 지웁니다">✕</button></span>`).join("")}
          <select data-pm-gen="${o.id}">
            <option value="">구분…</option>
            ${PART_GEN_AXES.map((g2) => `<option>${g2}</option>`).join("")}
          </select>
          <select data-pm-part="${o.id}">
            <option value="">부품…</option>
            ${liveParts.map((p) => `<option value="${p.id}">${escapeHtml(p.name)} — ${(p.price || 0).toLocaleString("ko-KR")}원</option>`).join("")}
          </select>
          <button class="btn btn-sm" data-pm-add="${o.id}">구분 연결 추가</button>
        </div>`}
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
    $$("[data-part-save]").forEach((b) => b.addEventListener("click", async () => {
      const id = Number(b.dataset.partSave);
      const pid = $(`[data-part="${id}"]`).value;
      const qty = Number($(`[data-partqty="${id}"]`).value) || 1;
      try {
        await api(`/api/prep-options/${id}`, { method: "PATCH",
          body: { partId: pid ? Number(pid) : null, partQty: qty } });
        toast(pid ? "부품을 연결했습니다 — 이제 체크하면 원가가 자동 기입됩니다."
                  : "부품 연결을 해제했습니다.");
        renderPrepTab(body);
      } catch (err) { toast(err.message, true); }
    }));
    // 구분별 연결 추가/삭제 — 저장은 목록 통째 PATCH(부분 수정보다 단순·안전)
    const pmOf = (o) => (o.partMap || []).map((m) => ({ gen: m.gen, partId: m.partId }));
    $$("[data-pm-add]").forEach((b) => b.addEventListener("click", async () => {
      const id = Number(b.dataset.pmAdd);
      const o = options.find((x) => x.id === id);
      const gen = $(`[data-pm-gen="${id}"]`).value;
      const pid = Number($(`[data-pm-part="${id}"]`).value);
      if (!gen || !pid) { toast("구분과 부품을 모두 고르세요.", true); return; }
      try {
        await api(`/api/prep-options/${id}`, { method: "PATCH",
          body: { partMap: pmOf(o).filter((m) => m.gen !== gen).concat({ gen, partId: pid }) } });
        toast(`구분별 연결 추가 — 제품코드가 ${gen}인 주문은 이 부품으로 기입됩니다.`);
        renderPrepTab(body);
      } catch (err) { toast(err.message, true); }
    }));
    $$("[data-pm-del]").forEach((b) => b.addEventListener("click", async () => {
      const [id, idx] = b.dataset.pmDel.split("|").map(Number);
      const o = options.find((x) => x.id === id);
      try {
        await api(`/api/prep-options/${id}`, { method: "PATCH",
          body: { partMap: pmOf(o).filter((_m, i) => i !== idx) } });
        renderPrepTab(body);
      } catch (err) { toast(err.message, true); }
    }));
  };

  $("#pn-save", body)?.addEventListener("click", async () => {
    const btn = $("#pn-save", body);
    btn.disabled = true;
    try {
      await api("/api/setup-notice", { method: "POST",
        body: { text: $("#pn-text", body).value } });
      toast("저장했습니다 — 셋팅 보드에 바로 반영됩니다.");
    } catch (err) { toast(err.message, true); }
    btn.disabled = false;
  });
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
  // ★서버 기본 요율(app/orders/__init__.py DEFAULT_FEE_RATES)과 같은 표 — 바꾸면 같이 바꿀 것.
  //   대표 지시(2026-08-11) "채널별 수수료는 일단 기본 값으로" — 통상 요율 추정치이고,
  //   여기서 값을 저장하면 그 채널은 저장값이 이긴다(0도 존중).
  const DEFAULT_RATES = { "고도몰": 3.4, "쿠팡": 5.0, "스마트스토어": 5.6, "카카오": 4.5,
                          "토스": 2.0, "11번가": 5.0, "롯데온": 5.5, "G마켓": 5.5,
                          "옥션": 5.5, "테무": 0 };
  const counts = {};
  (channels || []).forEach((m) => { counts[m.channel] = m.count; });
  const names = [...new Set([...(channels || []).map((m) => m.channel).filter(Boolean),
                             ...Object.keys(DEFAULT_RATES),
                             ...Object.keys(rates).filter((k) => k !== "_default")])];
  body.innerHTML = `
    <div class="card">
      <h3 style="margin-top:0;">판매수수료율</h3>
      <p class="muted">쇼핑몰·PG가 떼어 가는 비율입니다. 넣어 두면 <b>출고 확인할 때 자동으로</b> 주문에 붙고,
      리포트의 마진이 판매가가 아니라 <b>실제로 남는 돈</b> 기준이 됩니다.<br>
      <b>비워 두면 회색으로 보이는 기본 요율(통상 요율 추정치)이 적용됩니다</b> — 계약 요율과 다르면
      여기서 실제 값을 저장하세요. 0을 저장하면 '수수료 없음'으로 확정됩니다.
      전화·방문·b2b 같은 직거래 채널은 기본 0%입니다.</p>
      <div class="form-grid">
        ${names.map((n) => `<label>${escapeHtml(n)} (%)${counts[n] ? ` <span class="muted" style="font-weight:400;">주문 ${counts[n]}건</span>` : ""}
          <input type="text" data-rate="${escapeHtml(n)}" value="${rates[n] != null ? rates[n] : ""}"
                 placeholder="${DEFAULT_RATES[n] != null ? `기본 ${DEFAULT_RATES[n]}` : "0"}"></label>`).join("")}
        <label>그 외 채널 기본 (%)
          <input type="text" data-rate="_default" value="${rates._default != null ? rates._default : ""}" placeholder="기본 0"></label>
        <label>출고 택배비 (원)
          <input type="text" id="st-ship" value="${s.shippingCost || ""}" placeholder="예: 3000"></label>
      </div>
      <div class="editor-actions"><button class="btn btn-primary" id="st-save">저장</button></div>
    </div>
    <div class="card">
      <h3 style="margin-top:0;">과거 주문에 소급 적용</h3>
      <p class="muted">요율을 넣거나 고치면, 이미 출고된 주문의 자동 산정 수수료를 지금 요율로
      다시 계산합니다(기본 요율로 붙어 있던 것 포함). 먼저 [계산해 보기]로 금액을 확인한 뒤
      적용하세요. 손으로 확정한 값은 건드리지 않습니다.</p>
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
    if (back._temp) {
      host.remove();                   // 허공에서 온 패널 — 되돌릴 자리가 없으니 지운다
    } else {
      // 패널을 원래 자리로 돌려놓고 비운다 — 다음에 다시 그릴 때 그대로 쓰인다
      if (back._home && back._home.parentNode) back._home.parentNode.insertBefore(host, back._home);
      host.innerHTML = "";
      host.classList.remove("in-modal");
    }
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
  // ★허공에 만든(document.createElement 직후, 아직 DOM 밖) 패널도 받는다(2026-08-24).
  //   원래는 화면에 있는 패널만 받아서, 떼어 온 div를 넘기면 parentNode가 null이라
  //   insertBefore에서 죽었다 — 주문자 수정·추가 결제·번호 바로잡기 모달이 전부
  //   "버튼을 눌러도 아무 일도 없는" 상태였다(브라우저 검수에서 발견).
  if (!host.parentNode) {
    back._temp = true;                 // 닫을 때 되돌릴 자리가 없다 — 통째로 지운다
  } else {
    // 원래 위치를 기억해 둔다(닫을 때 되돌리기 위해)
    const home = document.createComment("panel-home");
    host.parentNode.insertBefore(home, host);
    back._home = home;
  }
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

/* ── 2층 팝업 — 이미 떠 있는 팝업 위에 작은 창을 '겹쳐' 띄운다(2026-08-31 대표).
   openModalWith 는 층이 하나뿐이라 새 팝업을 열면 먼저 떠 있던 팝업을 닫아 버린다 —
   A/S 상세(팝업)에서 문자 매크로를 누르면 상세가 통째로 사라지던 문제.
   이 층은 닫아도 아래(1층) 팝업이 그대로 남는다. 아래층이 없으면 보통 팝업으로 연다. */
function openModalOver(host) {
  if (!document.getElementById("modal-back")) { openModalWith(host); return; }
  if (host.classList.contains("in-modal")) return;
  closeModalOver();
  const back = document.createElement("div");
  back.id = "modal-back2";
  back.className = "modal-back";
  back.style.zIndex = "320";               // .modal-back(300)보다 위
  if (!host.parentNode) {
    back._temp = true;                     // 허공에서 온 패널 — 닫을 때 통째로 지운다
  } else {
    const home = document.createComment("panel-home2");
    host.parentNode.insertBefore(home, host);
    back._home = home;
  }
  back._host = host;
  host.classList.add("in-modal");
  back.appendChild(host);
  document.body.appendChild(back);
  back.addEventListener("mousedown", (e) => { if (e.target === back) closeModalOver(); });
  const focusTarget = host.querySelector("[data-autofocus], input:not([type=checkbox]):not([disabled]), textarea");
  if (focusTarget) focusTarget.focus();
  const obs = new MutationObserver(() => {
    if (!host.firstElementChild) closeModalOver();
  });
  obs.observe(host, { childList: true });
  back._obs = obs;
}

function closeModalOver() {
  const back = document.getElementById("modal-back2");
  if (!back) return;
  const host = back._host;
  if (host) {
    if (back._temp) {
      host.remove();
    } else {
      if (back._home && back._home.parentNode) back._home.parentNode.insertBefore(host, back._home);
      host.innerHTML = "";
      host.classList.remove("in-modal");
    }
  }
  if (back._obs) back._obs.disconnect();
  back.remove();
  // body.modal-open 은 1층(closeModal)이 관리한다 — 여기서 지우면 아래층 스크롤 잠금이 풀린다
}

document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  if (document.getElementById("modal-back2")) { closeModalOver(); return; }   // 위층부터 닫는다
  if (document.getElementById("modal-back")) closeModal();
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

/* 몰 설정을 저장·삭제한 뒤 목록만 새로 그린다.
   ★반드시 '몰 버튼줄이 사는 자리'(#apiview-body)에만 그려야 한다. 위 자리(#tab-body)에
     그리면 세부탭 줄이 한 겹씩 쌓인다 — 대표가 본 그 증상이다(2026-08-10). */
function redrawApiSettings() {
  const host = $("#apiview-body");
  if (host) renderApiSettings(host);
  else renderApiTab($("#tab-body"));            // 화면 구조가 바뀐 경우의 폴백
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
  // ★자기 자리(body)에 다시 그린다. 예전엔 renderApiTab(body)를 불러 한 단계 위 화면을
  //   이 안에 통째로 덮어썼고, 그래서 몰을 누를 때마다 세부탭 줄('쇼핑몰·택배 API / 제공 옵션')이
  //   하나씩 쌓였다(대표 보고 2026-08-10).
  $$("button[data-api]", body).forEach((b) => b.addEventListener("click", () => {
    state.apiSection = b.dataset.api;
    renderApiSettings(body);
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
          <input type="text" id="sms-key" data-saved="${cfg.hasApiKey ? "1" : ""}"
            value="${cfg.hasApiKey ? SECRET_DOTS : ""}"
            placeholder="${cfg.hasApiKey ? "" : "비즈고에서 발급받은 통합 API 키"}">
          ${cfg.hasApiKey ? `<span class="muted" style="font-size:12px;">키가 저장돼 있습니다 — 바꿀 때만 새로 입력하세요(보안상 실제 값은 화면에 안 보여 줍니다)</span>` : ""}</label>
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
    // ●점만 그대로면 '바꾸지 않음'(MASK) — 몰·CJ 키와 같은 규칙(secretVal).
    //   서버(_merge_secrets)가 MASK 를 받으면 기존 키를 그대로 둔다.
    const key = secretVal($("#sms-key"));
    if (key && key !== MASK) payload.apiKey = key;
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
      // ★저장된 적 없는 칸은 정의의 기본값(f.default)을 따른다 — 새 스위치를 켠 채로 내보낼 수 있게.
      //   (송장 발급 즉시 전송, 2026-09-08). 한 번 저장하면 그때부터는 저장값이 이긴다.
      const on = (val === undefined || val === null) ? !!f.default : !!val;
      return `<label class="check-line" title="${escapeHtml(f.help || "")}"><input type="checkbox" id="${id}" ${on ? "checked" : ""}> ${escapeHtml(f.label)}</label>`;
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
  // 고도몰 택배사 코드는 가맹점마다 달라 몰에 물어봐야 한다(RMS 이식 2026-09-08).
  //   [🔍 택배사 조회]가 CJ대한통운의 번호를 찾아 칸에 채운다 — 비워 둬도 전송 때 자동으로 찾지만,
  //   여기서 한 번 확인해 두면 어떤 택배사로 올라가는지 눈으로 볼 수 있다.
  if (m.code === "godomall") {
    const cc = document.getElementById("mf-cj_courier_code");
    if (cc) {
      cc.insertAdjacentHTML("afterend",
        `<button type="button" class="btn btn-sm" id="mf-godo-couriers" style="margin-top:4px;"
           title="고도몰에 등록된 택배사 목록을 읽어 CJ대한통운 번호를 채웁니다(읽기만 — 몰에 아무 변경 없음)">🔍 택배사 조회</button>`);
      document.getElementById("mf-godo-couriers").addEventListener("click", async () => {
        const btn = document.getElementById("mf-godo-couriers");
        btn.disabled = true;
        try {
          const r = await api("/api/malls/godomall/couriers");
          if (r.cjSno) cc.value = r.cjSno;
          toast(r.cjSno
            ? `CJ대한통운 = ${r.cjSno} (등록 택배사 ${r.couriers.length}개) — [저장]을 눌러 확정하세요.`
            : `CJ대한통운이 등록돼 있지 않습니다: ${r.couriers.map((c) => c.name).join(", ") || "없음"}`,
            !r.cjSno);
        } catch (err) { toast(err.message, true); }
        finally { btn.disabled = false; }
      });
    }
  }
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
      // ★#tab-body(설정 탭 전체)가 아니라 API 화면 자리에만 다시 그린다 —
      //   위 자리에 그리면 세부탭 줄이 겹쳐 쌓인다(2026-08-10).
      redrawApiSettings();
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
      redrawApiSettings();
    } catch (err) { toast(err.message, true); }
  });
}

/* 운송장 문구 편집 — 왼쪽은 종이에 찍히는 모습, 오른쪽은 문구. 고치면 바로 왼쪽에 반영된다.
   (대표 2026-08-24: "실제 어떤 부분에 어떤 내용이 들어가는지 미리보기로 좌측에 있고
    우측에서 문구 넣으면 바로 반영되서 보이는 것처럼")
   ★칸이 120/60자로 고정이라 순서가 곧 우선순위다 — 넘치면 뒤가 빠진다. */
async function bindLabelTmpl(cj) {
  const itemIn = $("#cj-tmpl-item"), rmIn = $("#cj-tmpl-remark");
  if (!itemIn || !rmIn) return;
  let meta;
  try { meta = await api("/api/cj/label-tokens"); }
  catch (err) {
    $("#cj-tmpl").innerHTML =
      `<p class="muted">문구 설정을 불러오지 못했습니다 — ${escapeHtml(err.message)}</p>`;
    return;
  }
  const saved = (cj && cj.label_tmpl) || {};
  itemIn.value = (saved.item || "").trim() || meta.item;
  rmIn.value = (saved.remark || "").trim() || meta.remark;

  const insert = (input, name) => {
    const at = input.selectionStart ?? input.value.length;
    const tok = `[${name}]`;
    input.value = input.value.slice(0, at) + tok + input.value.slice(input.selectionEnd ?? at);
    input.focus();
    input.selectionStart = input.selectionEnd = at + tok.length;
    draw();
  };
  const chips = (hostId, input, scope) => {
    const host = $(hostId);
    if (!host) return;
    host.innerHTML = meta.tokens
      .filter((t) => t.scope === "both" || t.scope === scope)
      .map((t) => `<button type="button" class="btn btn-sm btn-ghost" data-tok="${escapeHtml(t.name)}"
             title="${escapeHtml(t.help)}">${escapeHtml(t.name)}</button>`).join("");
    $$("button[data-tok]", host).forEach((b) =>
      b.addEventListener("click", () => insert(input, b.dataset.tok)));
  };
  chips("#cj-tmpl-item-chips", itemIn, "item");
  chips("#cj-tmpl-remark-chips", rmIn, "remark");

  const lenTag = (id, n, max) => {
    const el = $(id);
    if (!el) return;
    el.textContent = `${n} / ${max}자`;
    el.style.color = n > max ? "var(--danger)" : n > max * 0.9 ? "var(--amber, #b45309)" : "";
  };

  let timer = null;
  const draw = () => { clearTimeout(timer); timer = setTimeout(paint, 200); };
  const paint = async () => {
    const box = $("#cj-tmpl-preview");
    if (!box) return;
    let p;
    try {
      p = await api("/api/cj/label-preview", { method: "POST",
        body: { item: itemIn.value, remark: rmIn.value } });
    } catch (err) { box.innerHTML = `<p class="muted">${escapeHtml(err.message)}</p>`; return; }
    lenTag("#cj-len-item", p.itemLen, 120);
    lenTag("#cj-len-remark", p.remarkLen, 60);
    const bad = [...(p.unknown.item || []), ...(p.unknown.remark || [])];
    // ★실제 견본 운송장 PDF를 그대로 띄운다 — 모형 그림으로는 잘림·넘침을 알 수 없다.
    //   문구를 쿼리로 실어 보내 '지금 고치는 중인 값'이 종이에 그대로 나온다.
    // ★rot=0 = 화면용 바로선 견본(대표 2026-08-24: "옆으로 누워있어서 보기가 어렵네").
    //   프린터로 나가는 실제 출력은 세로급지용으로 눕힌 그대로다 — 보는 각도만 다르다.
    const q = "item=" + encodeURIComponent(itemIn.value)
            + "&remark=" + encodeURIComponent(rmIn.value)
            + "&_=" + Date.now();            // 브라우저가 PDF를 캐시하지 않게
    const src = "/api/cj/sample-pdf?rot=0&" + q
              + "#toolbar=0&navpanes=0&scrollbar=0&view=Fit";   // 스크롤 없이 한 장 맞춤
    const printSrc = "/api/cj/sample-pdf?" + q;                 // 프린터 방향(눕힘) 그대로
    box.innerHTML = `
      ${bad.length ? `<div class="wb-bad">⚠ 모르는 키워드:
        ${bad.map((b) => `<code>[${escapeHtml(b)}]</code>`).join(", ")} — 이대로는 저장되지 않습니다.</div>` : ""}
      <div class="wb-pdf"><iframe title="견본 운송장" src="${escapeHtml(src)}"></iframe></div>
      <div class="inline-row" style="margin-top:6px;">
        <span class="muted" style="font-size:12px; flex:1;">
          실제 견본 운송장입니다 — 문구를 고치면 바로 다시 그려집니다.</span>
        <button class="btn btn-sm" type="button" id="cj-tmpl-open"
          title="프린터로 나가는 방향(세로급지용으로 눕힌 상태) 그대로 엽니다">🖨 인쇄 방향으로 열기</button>
      </div>
      <div style="border:1px solid var(--border); border-radius:8px; padding:8px 10px;
                  margin-top:8px; background:var(--bg); font-size:12.5px;">
        <div class="muted" style="font-size:11.5px;">⑯ 상품명 칸</div>
        <div style="overflow-wrap:anywhere; font-weight:600; white-space:pre-line;">${escapeHtml(p.itemSummary) || '<span class="muted">(비어 있음)</span>'}</div>
        <div class="muted" style="font-size:11.5px; margin-top:6px;">⑰ 배송메세지 칸</div>
        <div style="overflow-wrap:anywhere;">${escapeHtml(p.remark) || '<span class="muted">(없음)</span>'}</div>
      </div>`;
    $("#cj-tmpl-open")?.addEventListener("click", () => window.open(printSrc, "_blank"));
  };
  itemIn.addEventListener("input", draw);
  rmIn.addEventListener("input", draw);
  $("#cj-tmpl-reset")?.addEventListener("click", () => {
    itemIn.value = meta.defaults.item;
    rmIn.value = meta.defaults.remark;
    draw();
  });
  paint();
}

function renderCJSection(host, cj) {
  const sender = cj.sender || {};
  const pickup = cj.pickup || {};
  const label = cj.label || {};
  host.innerHTML = `
    <div class="card" style="max-width:860px;">
      <h3>CJ대한통운 연동</h3>
      <p class="muted">고객코드는 예시 운영사 명의(00000000)를 사용합니다.
      <b>운영(prod) + 실발행 무장이 모두 켜져야 실제 송장이 접수</b>되며, 그 전에는 테스트 발행(999 번호)으로 출력 흐름만 검증됩니다.</p>
      <div class="form-grid">
        <label>환경<select id="cj-env">
          <option value="dev" ${cj.env !== "prod" ? "selected" : ""}>개발(dev) — 실배송 없음</option>
          <option value="prod" ${cj.env === "prod" ? "selected" : ""}>운영(prod) — 실배송</option>
        </select></label>
        <label class="check-line" style="align-self:end;"><input type="checkbox" id="cj-armed" ${cj.armed ? "checked" : ""}> 실발행 무장(armed)</label>
        <label>고객코드 (CUST_ID)<input type="text" id="cj-custid" value="${escapeHtml(cj.cust_id || "00000000")}"></label>
        <label>사업자등록번호<input type="text" id="cj-bizreg"
          value="${cj.biz_reg_num === MASK ? SECRET_DOTS : escapeHtml(cj.biz_reg_num || "")}"
          placeholder="숫자만" data-secret="1" data-saved="${cj.biz_reg_num === MASK ? 1 : 0}"></label>
      </div>
      <h3 style="margin-top:16px;">보내는 분 (출고지)</h3>
      <div class="form-grid">
        <label>이름<input type="text" id="cj-sname" value="${escapeHtml(sender.name || "")}" placeholder="예: 업무관리"></label>
        <label>전화<input type="text" id="cj-stel" value="${escapeHtml(sender.tel || "")}"></label>
        <label>우편번호<div class="inline-row" style="gap:4px; margin:0;">
          <input type="text" id="cj-szip" value="${escapeHtml(sender.zip || "")}" style="flex:1;">
          <button class="btn btn-sm" id="cj-szip-find" type="button">🔍 주소검색</button></div></label>
        <label>주소<input type="text" id="cj-saddr" value="${escapeHtml(sender.addr || "")}"></label>
        <label>상세주소<input type="text" id="cj-saddr2" value="${escapeHtml(sender.addr_detail || "")}"></label>
      </div>
      <h3 style="margin-top:16px;">회수지 (반품·회수 받는 곳)
        <span class="muted" style="font-size:12px; font-weight:400;">— 비워 두면 보내는 분 주소로 회수합니다</span></h3>
      <div class="form-grid">
        <label>이름<input type="text" id="cj-pname" value="${escapeHtml(pickup.name || "")}"></label>
        <label>전화<input type="text" id="cj-ptel" value="${escapeHtml(pickup.tel || "")}"></label>
        <label>우편번호<div class="inline-row" style="gap:4px; margin:0;">
          <input type="text" id="cj-pzip" value="${escapeHtml(pickup.zip || "")}" style="flex:1;">
          <button class="btn btn-sm" id="cj-pzip-find" type="button">🔍 주소검색</button></div></label>
        <label>주소<input type="text" id="cj-paddr" value="${escapeHtml(pickup.addr || "")}"></label>
        <label>상세주소<input type="text" id="cj-paddr2" value="${escapeHtml(pickup.addr_detail || "")}"></label>
      </div>
      <h3 style="margin-top:16px;">접수 기본값 <span class="muted" style="font-size:12px; font-weight:400;">— RMS 대표 지정값(신용·극소)과 동일 기준</span></h3>
      <div class="form-grid">
        <label>운임구분<select id="cj-frt">
          ${[["01", "선불"], ["02", "착불"], ["03", "신용(계약운임)"]].map(([v, l]) =>
            `<option value="${v}" ${(cj.frt_dv || "03") === v ? "selected" : ""}>${l}</option>`).join("")}
        </select></label>
        <label>박스타입<select id="cj-box">
          ${[["01", "극소"], ["02", "소"], ["03", "중"], ["04", "대"], ["05", "특大"], ["06", "이형"], ["07", "대2"]].map(([v, l]) =>
            `<option value="${v}" ${(cj.box_type || "01") === v ? "selected" : ""}>${l}</option>`).join("")}
        </select></label>
      </div>
      <h3 style="margin-top:16px;">집화 휴무일
        <span class="muted" style="font-size:12px; font-weight:400;">— 기사가 안 오는 날은 회수(택배) 예약을 받지 않습니다</span></h3>
      <div id="cj-pickup"><p class="muted">불러오는 중…</p></div>
      <h3 style="margin-top:16px;">운송장 문구
        <span class="muted" style="font-size:12px; font-weight:400;">— 왼쪽이 종이에 찍히는 모습입니다</span></h3>
      <div id="cj-tmpl" class="wb-tmpl">
        <div class="wb-tmpl-pv" id="cj-tmpl-preview"></div>
        <div class="wb-tmpl-ed">
          <label>⑯ 상품명 칸 <span class="muted" id="cj-len-item"></span>
            <input type="text" id="cj-tmpl-item" autocomplete="off"></label>
          <div id="cj-tmpl-item-chips" class="wb-chips"></div>
          <label style="margin-top:10px;">⑰ 배송메세지 칸 <span class="muted" id="cj-len-remark"></span>
            <input type="text" id="cj-tmpl-remark" autocomplete="off"></label>
          <div id="cj-tmpl-remark-chips" class="wb-chips"></div>
          <p class="muted" style="font-size:12px; margin:10px 0 0;">
            키워드를 누르면 커서 자리에 들어갑니다. <code>/</code> 로 칸을 나누고,
            <b>앞에 적은 것이 먼저 자리를 가져갑니다</b>(칸이 모자라면 뒤가 빠집니다).</p>
          <div class="inline-row" style="margin-top:8px;">
            <button class="btn btn-sm" id="cj-tmpl-reset" type="button">기본 문구로</button>
          </div>
        </div>
      </div>
      <h3 style="margin-top:16px;">운송장 인쇄 보정 <span class="muted">(레이아웃은 동결 — 위치 이슈는 보정값으로만)</span></h3>
      <div class="form-grid">
        <label>가로 오프셋 ox(mm)<input type="text" id="cj-ox" value="${label.ox ?? 0}"></label>
        <label>세로 오프셋 oy(mm)<input type="text" id="cj-oy" value="${label.oy ?? 0}"></label>
        <label>배율 sc(%)<input type="text" id="cj-sc" value="${label.sc ?? 100}"></label>
        <label>상품명 칸 줄수<select id="cj-lines"
            title="자산번호 줄바꿈으로 최대 4줄까지 씁니다. 인쇄가 어긋나면 2로 되돌리세요 — 예전과 똑같아집니다.">
          ${[2, 3, 4].map((n) => `<option value="${n}" ${(label.lines ?? 4) === n ? "selected" : ""}>${n}줄${n === 2 ? " (예전 방식)" : n === 4 ? " (기본)" : ""}</option>`).join("")}
        </select></label>
        <label class="check-line" style="align-self:end;"
               title="끄면 자산번호도 한 줄에 ' / '로 이어집니다 — 2026-08-24 이전과 동일">
          <input type="checkbox" id="cj-linebreak" ${label.lineBreak === false ? "" : "checked"}>
          자산번호 줄바꿈</label>
      </div>
      <div class="editor-actions" style="flex-wrap:wrap;">
        <button class="btn btn-primary" id="cj-save">저장</button>
        <select id="cj-identity" title="계약 정보(고객코드·사업자번호·주소)는 RMS와 같습니다. 발송인 명의·전화만 고르세요."
                style="padding:6px 8px; border:1px solid var(--border); border-radius:8px; background:var(--bg);">
          <option value="operations">발송인 표기: 업무관리 / 02-0000-0000</option>
          <option value="rms">발송인 표기: RMS 그대로(예시 운영사·예시 렌탈사 / 0000-0000)</option>
        </select>
        <button class="btn" id="cj-import" title="RMS(렌탈 시스템)의 CJ 설정을 그대로 가져옵니다. 실발행 무장은 가져오지 않습니다.">📥 RMS 정보 가져오기</button>
        <button class="btn" id="cj-test">🔌 연결 테스트</button>
        <button class="btn" id="cj-sample" title="CJ 호출 없이 견본 운송장 PDF를 출력해 프린터 정렬을 확인합니다">🏷 견본 운송장</button>
        <button class="btn" id="cj-addrtest" title="보내는 분 주소로 주소정제 API를 왕복 확인합니다 — 배송 예약이 생기지 않습니다">📍 주소정제 시험</button>
        <button class="btn" id="cj-roundtrip" style="border-color:var(--danger);"
          title="우리 주소로 진짜 접수한 뒤 곧바로 취소합니다. 운영+무장 상태에서만 동작.">🚚 실발행 왕복 시험</button>
      </div>
      <div id="cj-result" style="margin-top:10px;"></div>
      <p class="muted" style="margin-top:8px;">연결 테스트는 <b>토큰 발급만</b> 확인합니다 — 배송 예약이 생기지 않습니다.
      실발행 왕복 시험만 실제 접수를 만들며, 접수 직후 자동으로 취소합니다.</p>
    </div>`;
  const bindCJ = () => {
    bindLabelTmpl(cj);
    const resBox = () => $("#cj-result");
    const showOk = (html) => { resBox().innerHTML =
      `<div style="background:var(--primary-soft); border-radius:8px; padding:10px 12px;">${html}</div>`; };
    const showErr = (html) => { resBox().innerHTML =
      `<div style="background:var(--danger-soft); border-radius:8px; padding:10px 12px;">${html}</div>`; };

    $("#cj-save").addEventListener("click", async () => {
      try {
        await api("/api/settings", { method: "PUT", body: { cj: {
          env: $("#cj-env").value,
          armed: $("#cj-armed").checked,
          cust_id: $("#cj-custid").value.trim(),
          biz_reg_num: secretVal($("#cj-bizreg")),
          label_tmpl: { item: $("#cj-tmpl-item").value.trim(),
                        remark: $("#cj-tmpl-remark").value.trim() },
          sender: {
            name: $("#cj-sname").value.trim(), tel: $("#cj-stel").value.trim(),
            zip: $("#cj-szip").value.trim(), addr: $("#cj-saddr").value.trim(),
            addr_detail: $("#cj-saddr2").value.trim(),
          },
          pickup: {
            name: $("#cj-pname").value.trim(), tel: $("#cj-ptel").value.trim(),
            zip: $("#cj-pzip").value.trim(), addr: $("#cj-paddr").value.trim(),
            addr_detail: $("#cj-paddr2").value.trim(),
          },
          frt_dv: $("#cj-frt").value,
          box_type: $("#cj-box").value,
          // 집화 휴무일 — 카드가 화면 상태를 그대로 돌려준다(as.js renderCjPickupOff)
          ...($("#cj-pickup") && $("#cj-pickup")._value ? { pickup: $("#cj-pickup")._value() } : {}),
          label: { ox: Number($("#cj-ox").value) || 0, oy: Number($("#cj-oy").value) || 0,
                   sc: Number($("#cj-sc").value) || 100,
                   // ★인쇄가 어긋나면 여기서 되돌린다 — 2줄 + 줄바꿈 해제 = 예전과 동일
                   lines: Number($("#cj-lines")?.value) || 4,
                   lineBreak: !!$("#cj-linebreak")?.checked },
        } } });
        toast("CJ 설정을 저장했습니다.");
      } catch (err) { toast(err.message, true); }
    });

    renderCjPickupOff($("#cj-pickup"), cj);
    attachAddrSearch($("#cj-szip-find"), { zip: "#cj-szip", addr: "#cj-saddr", detail: "#cj-saddr2" });
    attachAddrSearch($("#cj-pzip-find"), { zip: "#cj-pzip", addr: "#cj-paddr", detail: "#cj-paddr2" });

    // RMS 값 그대로 가져오기 — 저장까지 서버가 한다. 화면은 다시 그려서 보여만 준다.
    $("#cj-import").addEventListener("click", async () => {
      if (!confirm("RMS(렌탈 시스템)의 CJ 설정을 가져와 저장합니다.\n"
          + "고객코드·사업자번호·보내는분·회수지가 채워집니다.\n"
          + "실발행 무장은 가져오지 않습니다 — 확인 후 직접 켜세요.\n\n계속할까요?")) return;
      const btn = $("#cj-import");
      btn.disabled = true;
      try {
        const r = await api("/api/cj/import-rms", { method: "POST",
          body: { identity: $("#cj-identity")?.value || "operations" } });
        toast(r.message);
        const s = await api("/api/settings");
        renderCJSection(host, s.cj || {});
        $("#cj-result").innerHTML =
          `<div style="background:var(--primary-soft); border-radius:8px; padding:10px 12px;">
          ✅ <b>RMS 값 이식 완료</b> — 고객코드 ${escapeHtml(r.custId)} ·
          <span class="chip ${r.env === "prod" ? "chip-green" : "chip-slate"}">${r.env === "prod" ? "운영" : "개발"}</span>
          ${r.armed ? '<span class="chip chip-red">실발행 무장</span>' : '<span class="chip chip-slate">무장 꺼짐 — 직접 켜세요</span>'}<br>
          <span class="muted">보내는분 ${escapeHtml(r.senderName)} · 회수지 ${escapeHtml(r.pickupName)}<br>
          다음: [연결 테스트] → [견본 운송장] → 무장 켜고 [실발행 왕복 시험]</span></div>`;
      } catch (err) { toast(err.message, true); btn.disabled = false; }
    });

    $("#cj-test").addEventListener("click", async () => {
      const btn = $("#cj-test");
      btn.disabled = true;
      resBox().innerHTML = `<span class="muted">CJ대한통운에 연결하는 중…</span>`;
      try {
        const r = await api("/api/cj/test", { method: "POST" });
        showOk(`✅ <b>CJ 연결 성공</b> <span class="chip ${r.env === "prod" ? "chip-green" : "chip-slate"}">${r.env === "prod" ? "운영" : "개발"}</span>
          ${r.armed ? '<span class="chip chip-red">실발행 무장</span>' : '<span class="chip chip-slate">테스트 발행</span>'}<br>
          <span class="muted">고객코드 ${escapeHtml(r.custId)} · ${escapeHtml(r.host)}<br>${escapeHtml(r.message)}</span>`);
        toast("CJ 연결 성공");
      } catch (err) {
        showErr(`❌ <b>연결 실패</b><br><span style="color:var(--danger)">${escapeHtml(err.message)}</span><br>
          <span class="muted">고객코드·사업자등록번호를 저장한 뒤 다시 시도하세요.</span>`);
      } finally { btn.disabled = false; }
    });

    $("#cj-sample").addEventListener("click", () => {
      window.open("/api/cj/sample-pdf", "_blank");
    });

    $("#cj-addrtest").addEventListener("click", async () => {
      const btn = $("#cj-addrtest");
      btn.disabled = true;
      resBox().innerHTML = `<span class="muted">주소정제 호출 중…</span>`;
      try {
        const r = await api("/api/cj/addr-test", { method: "POST", body: {} });
        showOk(`✅ <b>주소정제 성공</b><br><span class="muted">${escapeHtml(r.address)}<br>
          분류코드 <b>${escapeHtml(r.clsfcd || "-")}</b> · ${escapeHtml(r.clsfaddr || "")} · ${escapeHtml(r.bran || "")}</span>`);
      } catch (err) {
        showErr(`❌ <b>주소정제 실패</b><br><span style="color:var(--danger)">${escapeHtml(err.message)}</span>`);
      } finally { btn.disabled = false; }
    });

    // 실발행 왕복 — 진짜 접수를 만드니 두 번 묻는다
    $("#cj-roundtrip").addEventListener("click", async () => {
      if (!confirm("실발행 왕복 시험을 시작합니다.\n\n"
          + "우리 주소 → 우리 주소로 CJ에 진짜 접수한 뒤 곧바로 취소합니다.\n"
          + "운영(prod) + 실발행 무장 상태여야 합니다.\n\n계속할까요?")) return;
      const btn = $("#cj-roundtrip");
      btn.disabled = true;
      resBox().innerHTML = `<span class="muted">접수 → 취소 왕복 중… (몇 초 걸립니다)</span>`;
      try {
        const r = await api("/api/cj/live-roundtrip", { method: "POST" });
        if (r.ok) {
          showOk(`✅ <b>실발행 왕복 성공</b> — 송장 ${escapeHtml(r.invoice)} 접수 후 즉시 취소<br>
            <span class="muted">CJ 연동이 끝까지 검증됐습니다. 이제 셋팅/배송 화면의 [송장 발급]이 실제 접수됩니다.</span>`);
          toast("실발행 왕복 성공");
        } else {
          showErr(`⚠ <b>접수는 됐는데 취소 실패</b> — <b style="font-size:16px;">송장번호 ${escapeHtml(r.invoice)}</b><br>
            <span style="color:var(--danger)">${escapeHtml(r.message)}</span>`);
        }
      } catch (err) {
        showErr(`❌ <b>실발행 시험 실패</b><br><span style="color:var(--danger)">${escapeHtml(err.message)}</span>`);
      } finally { btn.disabled = false; }
    });
  };
  bindCJ();
}

/* ----- 데이터 이관 (기존 QC 프로그램) ----- */

function renderMigrateTab(body) {
  // ★TMS 이관·자동반영이 이 탭의 주인공(대표 2026-08-26 — 매입 ▸ 기준정보에서 이사,
  //   "스케줄러가 도니 버튼은 필요없고 마지막 반영이 언제인지만"). QC 이관은 접이식으로.
  const tmsHost = document.createElement("div");
  body.innerHTML = "";
  body.appendChild(tmsHost);
  renderMigrate(tmsHost);                     // purchase.js — 자동 반영 현황 + 수동 이관(접이식)
  const qc = document.createElement("div");
  body.appendChild(qc);
  renderQcMigrate(qc);
}

function renderQcMigrate(body) {
  body.innerHTML = `
    <details class="card" style="max-width:860px;">
      <summary style="cursor:pointer;"><b>기존 QC 프로그램 데이터 가져오기 · 단계 감시</b>
        <span class="muted">— 전환기용(평소엔 쓸 일 없음)</span></summary>
    <div class="card" style="max-width:860px; margin-top:10px;">
      <h3>기존 QC 프로그램 데이터 가져오기</h3>
      <p class="muted">NAS에 백업된 주문 워크플로 프로그램의 <b>주문·사용자·관리번호</b>를 OWS로 옮깁니다.
      비밀번호 저장 방식이 같아서 <b>직원들은 쓰던 비밀번호 그대로</b> 로그인할 수 있습니다.
      이미 들어온 주문·계정은 건너뛰므로 여러 번 실행해도 중복되지 않습니다.</p>

      <div class="form-grid" style="margin-top:10px;">
        <label style="grid-column:1/-1;">프로그램 폴더 경로 (data 폴더가 있는 위치)
          <input type="text" id="qc-path" placeholder="예: \\\\ExampleShare\\Operations\\...\\order-workflow-sample">
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
               placeholder="\\127.0.0.1\order-data">
        <button class="btn btn-sm" id="qcw-save">저장</button>
        <button class="btn btn-sm btn-primary" id="qcw-run">지금 반영</button>
      </div>
      <div id="qcw-status" class="muted" style="margin-top:8px; font-size:13px;">확인 중…</div>
    </div>
    </details>`;

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
    if (!confirm("기존 프로그램의 주문·사용자·관리번호를 OWS로 가져옵니다.\n\n"
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
      <h3>OWS 기준 재고 <span class="muted" style="font-weight:400;">제품코드 ${d.totalCodes}종 · ${d.totalUnits}대</span></h3>
      <p class="muted">매입에서 <b>제품코드</b>를 기입하고 <b>[몰 재고 전송]</b>을 체크한 자산만 여기에 세어집니다.
        체크가 없으면 0으로 시작하는 게 정상입니다.
        <br><b>순서</b>: 아래에서 몰을 먼저 켜야 매입 화면에 [몰 재고 전송] 칸이 나타납니다.
        이 체크는 <u>몰에 보낼지 여부</u>일 뿐, 사내 재고(가용·실재고)와는 무관합니다.</p>
      <div class="table-wrap" style="max-height:300px; overflow-y:auto;"><table>
        <thead><tr><th>제품코드</th><th>재고</th></tr></thead>
        <tbody>${(d.codes || []).map((c) => `
          <tr><td>${escapeHtml(c.productCode)}</td><td><b>${c.count}대</b></td></tr>`).join("")
          || `<tr><td colspan="2" class="muted">몰에 보낼 자산이 아직 없습니다 — 아래에서 몰을 켠 뒤 매입 화면에서 제품코드를 넣고 [몰 재고 전송]을 체크하세요.</td></tr>`}
        </tbody></table></div>
    </div>
    <div class="card">
      <h3>쇼핑몰별 전송 스위치 <span class="chip chip-red">기본 전부 꺼짐</span></h3>
      <p class="muted">꺼진 몰은 OWS가 아무것도 보내지 않습니다 — 몰에 등록된 기존 재고값이 그대로 유지됩니다.</p>
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
    if (on && !confirm("이 쇼핑몰에 OWS 기준 재고를 보내기 시작합니다.\n"
        + "몰에 등록된 재고 숫자가 OWS 값으로 바뀝니다. 켤까요?")) return;
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
        <li><code>E:\\operations-system\\data\\ows.db</code> 를 다른 이름으로 옮겨 둡니다(지우지 마세요).</li>
        <li>되살릴 백업 파일을 <code>ows.db</code> 라는 이름으로 그 자리에 복사합니다.
            같은 폴더의 <code>ows.db-wal</code>, <code>ows.db-shm</code> 파일이 있으면 지웁니다.</li>
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
