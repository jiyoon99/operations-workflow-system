/* 반응형 보조 — 폰·태블릿에서 화면이 깨지지 않게 거드는 얇은 층.
 *
 * 원칙: 화면을 그리는 파일(orders.js·setup.js·purchase.js …)은 건드리지 않는다.
 *       그 파일들은 검증이 끝난 코드고, 반응형 때문에 8개를 다 고치면
 *       고장 날 자리만 늘어난다. 그래서 여기서 '그려진 뒤'에 손을 본다.
 *
 * 하는 일 두 가지
 *   1) 좁은 화면에서 왼쪽 메뉴를 서랍으로 여닫는다(상단 ☰).
 *   2) 표의 각 칸에 머리글 이름을 data-label로 붙인다.
 *      CSS가 그 이름으로 표를 카드처럼 세운다(app.css의 table.resp-card).
 */
(function () {
  "use strict";

  var NARROW = 1024;      // 이 아래로는 서랍 메뉴 (아이패드 세로 768 포함)
  var CARD_MIN_COLS = 5;  // 칸이 이만큼 많은 표만 카드로 — 3~4칸짜리는 그대로가 낫다

  // ---------------------------------------------------------------- 서랍 메뉴

  function closeNav() {
    document.body.classList.remove("nav-open");
    var t = document.getElementById("menu-toggle");
    if (t) t.setAttribute("aria-expanded", "false");
  }

  function toggleNav() {
    var open = document.body.classList.toggle("nav-open");
    var t = document.getElementById("menu-toggle");
    if (t) t.setAttribute("aria-expanded", open ? "true" : "false");
  }

  function initNav() {
    var toggle = document.getElementById("menu-toggle");
    var backdrop = document.querySelector(".nav-backdrop");
    if (toggle) toggle.addEventListener("click", toggleNav);
    if (backdrop) backdrop.addEventListener("click", closeNav);

    // 메뉴를 고르면 서랍은 닫힌다 — 고르고 나서 또 닫는 동작을 시키면 안 된다
    var nav = document.getElementById("nav");
    if (nav) nav.addEventListener("click", function (e) {
      if (e.target.closest("a")) closeNav();
    });

    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape") closeNav();
    });

    // 창을 넓히면(가로로 돌리면) 서랍 상태를 지워 PC 배치로 되돌린다
    window.addEventListener("resize", function () {
      if (window.innerWidth > NARROW) closeNav();
      syncTitle();
    });
  }

  // 상단 바에 지금 보고 있는 화면 이름을 띄운다
  function syncTitle() {
    var el = document.querySelector(".topbar .tb-title");
    if (!el) return;
    var active = document.querySelector("#nav a.active");
    el.textContent = active ? (active.textContent || "").trim() : "";
  }

  // ---------------------------------------------------------------- 표 → 카드

  function labelize(root) {
    var tables = (root || document).querySelectorAll("#main table");
    for (var i = 0; i < tables.length; i++) {
      var t = tables[i];
      var ths = t.querySelectorAll("thead th");
      if (!ths.length) continue;

      var heads = [];
      for (var h = 0; h < ths.length; h++) {
        // 머리글에 버튼·정렬표시가 섞여 있어도 글자만 남긴다
        heads.push((ths[h].textContent || "").replace(/\s+/g, " ").trim());
      }

      if (ths.length >= CARD_MIN_COLS) t.classList.add("resp-card");
      else t.classList.remove("resp-card");

      // tfoot(합계 줄)도 함께 — 여기를 빠뜨리면 합계만 표 폭으로 남아 화면이 밀린다
      var rows = t.querySelectorAll("tbody tr, tfoot tr");
      for (var r = 0; r < rows.length; r++) {
        var cells = rows[r].children;
        // '데이터가 없습니다' 같은 한 칸짜리 안내 줄은 건드리지 않는다
        if (cells.length === 1 && cells[0].hasAttribute("colspan")) continue;
        for (var c = 0; c < cells.length && c < heads.length; c++) {
          if (cells[c].dataset.label === undefined) cells[c].dataset.label = heads[c];
        }
      }
    }
  }

  // #main은 화면을 바꿀 때마다 통째로 다시 그려진다. 그때마다 이름표를 새로 붙인다.
  function watchMain() {
    var main = document.getElementById("main");
    if (!main || !window.MutationObserver) return;
    var pending = 0;
    var mo = new MutationObserver(function () {
      // 한 번 그릴 때 수십 번 불리므로 묶어서 처리한다.
      // ★requestAnimationFrame을 쓰면 안 된다 — 창이 뒤에 가려져 있거나 다른 탭이면
      //   브라우저가 이걸 아예 호출하지 않아서, 그 사이 그려진 표에 이름표가 안 붙는다
      //   (2026-07-29 실제로 그래서 카드 전환이 통째로 안 먹었다).
      if (pending) return;
      pending = setTimeout(function () {
        pending = 0;
        try { labelize(); } catch (e) { /* 이름표는 부가 기능 — 실패해도 화면은 살아야 한다 */ }
      }, 16);
    });
    // childList만 본다. attributes까지 보면 우리가 붙인 data-label에 스스로 반응한다.
    mo.observe(main, { childList: true, subtree: true });
  }

  function init() {
    initNav();
    watchMain();
    labelize();
    syncTitle();
    // 메뉴 활성 표시가 바뀌면 상단 제목도 따라간다
    var nav = document.getElementById("nav");
    if (nav && window.MutationObserver) {
      new MutationObserver(syncTitle).observe(nav, {
        attributes: true, subtree: true, attributeFilter: ["class"],
      });
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
