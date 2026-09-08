/* 기간별 작업량 — 2026-09-03 대표 지시로 [📅 셋팅·작업 실적] 하나에 흡수됐다.
   ("셋팅실적 / 기간별 작업량이 거의 같은 내용이라서 서로 흡수해서 필요한 기능만,
    두 개로 나누지 말고")

   그림은 app.js 의 renderSetupStatsTab · renderWorkDetail 이 그린다.
   창구(/api/workload-stats)와 표 모양(workload.css)은 그대로 쓴다 — 계산은 안 건드렸다.
   ★이 파일에 화면을 되살리지 마라. 같은 사람의 같은 일을 두 화면이 각자 세면
     숫자가 반드시 어긋나고, 대표가 어느 쪽을 믿어야 할지 모르게 된다. */
"use strict";
function renderWorkloadView(main) {          // 옛 링크가 남아 있어도 죽지 않게
  state.settingsTab = "sales";
  state.salesView = "stats";
  if (typeof renderSettings === "function") return renderSettings(main);
}
