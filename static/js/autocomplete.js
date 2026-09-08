/* 스펙 입력 자동완성 — input에 붙여 쓰는 경량 컴포넌트.

   attachAutocomplete(input, "cpu")
   - 타이핑하면 서버(/api/spec-options)에서 부분일치 후보를 받아 목록 표시
   - 이미 등록된 자산에서 쓰던 값이 위, 카탈로그가 아래(태그로 구분)
   - 목록에 없는 값도 그냥 입력하면 저장됨(자유 입력). 한 번 쓰면 다음부터 후보에 나온다
   - 키보드: ↑↓ 이동, Enter 선택, Esc 닫기
*/
"use strict";

const AC_CACHE = new Map();   // `${field}|${q}` → options

async function acFetch(field, q) {
  const key = `${field}|${q}`;
  if (AC_CACHE.has(key)) return AC_CACHE.get(key);
  const params = new URLSearchParams({ field, q, limit: "30" });
  const res = await api("/api/spec-options?" + params.toString());
  AC_CACHE.set(key, res.options);
  if (AC_CACHE.size > 300) AC_CACHE.clear();   // 캐시 무한 증식 방지
  return res.options;
}

function acHighlight(text, q) {
  if (!q) return escapeHtml(text);
  const i = text.toLowerCase().indexOf(q.toLowerCase());
  if (i < 0) return escapeHtml(text);
  return escapeHtml(text.slice(0, i)) + "<mark>" + escapeHtml(text.slice(i, i + q.length)) +
         "</mark>" + escapeHtml(text.slice(i + q.length));
}

function attachAutocomplete(input, field) {
  if (!input || input.dataset.acBound === "1") return;
  input.dataset.acBound = "1";
  input.setAttribute("autocomplete", "off");

  // input을 감싸는 래퍼(위치 기준). 이미 감싸져 있으면 재사용
  let wrap = input.parentElement;
  if (!wrap.classList.contains("ac-wrap")) {
    wrap = document.createElement("div");
    wrap.className = "ac-wrap";
    input.parentNode.insertBefore(wrap, input);
    wrap.appendChild(input);
  }
  let list = null;
  let items = [];
  let active = -1;
  let seq = 0;

  const close = () => { if (list) { list.remove(); list = null; } active = -1; };

  const pick = (v) => {
    input.value = v;
    close();
    input.dispatchEvent(new Event("change", { bubbles: true }));
  };

  const draw = (options, q) => {
    close();
    if (!options.length) return;
    items = options;
    list = document.createElement("div");
    list.className = "ac-list";
    list.innerHTML = options.map((o, i) => `
      <div class="ac-item${i === active ? " active" : ""}" data-i="${i}">
        <span>${acHighlight(o.value, q)}</span>
        ${o.source === "used" ? '<span class="ac-tag">사용 중</span>'
          : o.source === "master" ? `<span class="ac-tag">${o.hint ? escapeHtml(o.hint) + " · " : ""}마스터</span>` : ""}
      </div>`).join("");
    wrap.appendChild(list);
    list.querySelectorAll(".ac-item").forEach((el) => {
      el.addEventListener("mousedown", (e) => { e.preventDefault(); pick(options[Number(el.dataset.i)].value); });
    });
  };

  const load = async () => {
    const q = input.value.trim();
    const my = ++seq;
    try {
      const options = await acFetch(field, q);
      if (my !== seq || document.activeElement !== input) return;  // 늦게 온 응답 무시
      draw(options, q);
    } catch (_e) { /* 자동완성 실패는 입력을 막지 않는다 */ }
  };

  let timer = null;
  input.addEventListener("input", () => {
    clearTimeout(timer);
    timer = setTimeout(load, 150);
  });
  input.addEventListener("focus", load);
  input.addEventListener("blur", () => setTimeout(close, 120));
  input.addEventListener("keydown", (e) => {
    if (!list) {
      if (e.key === "ArrowDown") load();
      return;
    }
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      active = e.key === "ArrowDown"
        ? Math.min(active + 1, items.length - 1)
        : Math.max(active - 1, 0);
      list.querySelectorAll(".ac-item").forEach((el, i) => el.classList.toggle("active", i === active));
      const el = list.querySelector(".ac-item.active");
      if (el) el.scrollIntoView({ block: "nearest" });
    } else if (e.key === "Enter") {
      if (active >= 0) { e.preventDefault(); pick(items[active].value); }
    } else if (e.key === "Escape") {
      close();
    }
  });
}

/* 폼 안의 스펙 입력칸에 한 번에 붙이기.
   prefix: id 접두사(예: "ar-" → #ar-cpu, #ar-ram …) */
function attachSpecAutocomplete(prefix) {
  ["cpu", "gpu", "ram", "ssd", "inch", "battery", "charger", "location", "maker", "model"]
    .forEach((f) => {
      const el = document.getElementById(prefix + f);
      if (el) attachAutocomplete(el, f);
    });
}
