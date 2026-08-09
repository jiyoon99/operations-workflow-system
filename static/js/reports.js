/* HMS 리포트 — 기간 요약(매입/판매/마진), 월별 추이, 채널별, 담당자 실적, 재고 체류 */
"use strict";

function renderReportsView(main) {
  if (!hasPerm("reports.view")) {
    main.innerHTML = `<h1 class="page-title">리포트</h1><div class="card placeholder"><p>통계 조회 권한이 없습니다.</p></div>`;
    return;
  }
  const today = new Date();
  const f = state.repFilter || (state.repFilter = {
    from: ymd(new Date(today.getFullYear(), today.getMonth(), 1)),
    to: ymd(today),
  });
  main.innerHTML = `
    <h1 class="page-title">리포트</h1>
    <p class="page-desc">매입·판매·마진과 재고 현황</p>
    <div class="card" style="padding:14px 16px;">
      <div class="inline-row" style="margin:0;">
        <label class="muted">기간</label>
        <input type="date" id="rp-from" value="${f.from}">
        <span class="muted">~</span>
        <input type="date" id="rp-to" value="${f.to}">
        <button class="btn btn-sm btn-primary" id="rp-go">조회</button>
        <button class="btn btn-sm" data-quick="month">이번 달</button>
        <button class="btn btn-sm" data-quick="last">지난 달</button>
        <button class="btn btn-sm" data-quick="year">올해</button>
      </div>
    </div>
    <div id="rp-body"><p class="muted">불러오는 중…</p></div>`;
  $("#rp-go").addEventListener("click", () => {
    const from = $("#rp-from").value, to = $("#rp-to").value;
    // ★날짜를 거꾸로 넣으면 조용히 전부 0원으로 나왔다 — 매출이 없는 건지
    //   날짜를 잘못 넣은 건지 구분할 수 없었다(2026-07-29 전수조사).
    if (from && to && from > to) {
      toast(`시작일(${from})이 종료일(${to})보다 늦습니다. 날짜를 바꿔 주세요.`, true);
      return;
    }
    f.from = from; f.to = to;
    loadReports();
  });
  $$("button[data-quick]", main).forEach((b) => b.addEventListener("click", () => {
    const now = new Date();
    if (b.dataset.quick === "month") {
      f.from = ymd(new Date(now.getFullYear(), now.getMonth(), 1));
      f.to = ymd(now);
    } else if (b.dataset.quick === "last") {
      f.from = ymd(new Date(now.getFullYear(), now.getMonth() - 1, 1));
      f.to = ymd(new Date(now.getFullYear(), now.getMonth(), 0));
    } else {
      f.from = ymd(new Date(now.getFullYear(), 0, 1));
      f.to = ymd(now);
    }
    $("#rp-from").value = f.from; $("#rp-to").value = f.to;
    loadReports();
  }));
  loadReports();
}

async function loadReports() {
  const seq = ++state.renderSeq;
  const f = state.repFilter;
  const host = $("#rp-body");
  const qs = `?from=${f.from}&to=${f.to}`;
  let sum, months, chans, staff, aging;
  try {
    [sum, months, chans, staff, aging] = await Promise.all([
      api("/api/reports/summary" + qs),
      api("/api/reports/monthly"),
      api("/api/reports/channels" + qs),
      api("/api/reports/staff" + qs),
      api("/api/reports/aging"),
    ]);
    if (seq !== state.renderSeq) return;
  } catch (err) {
    if (host) host.innerHTML = `<p class="muted">${escapeHtml(err.message)}</p>`;
    return;
  }
  const maxRevenue = Math.max(1, ...months.map((m) => m.revenue));
  const maxPurchase = Math.max(1, ...months.map((m) => m.purchaseAmount));
  const marginColor = sum.sales.margin >= 0 ? "var(--primary)" : "var(--danger)";

  const g = sum.dataGaps || {};
  const gapMsgs = [];
  if (g.noBuyPrice) gapMsgs.push(`매입가가 비어 있는 자산 <b>${g.noBuyPrice}대</b>(출고 ${g.units}대 중)`);
  if (g.ordersWithoutAsset) gapMsgs.push(`자산이 매칭되지 않은 출고 주문 <b>${g.ordersWithoutAsset}건</b>`);
  if (g.ordersWithoutAmount) gapMsgs.push(`판매금액이 0인 출고 주문 <b>${g.ordersWithoutAmount}건</b>`);

  host.innerHTML = `
    ${gapMsgs.length ? `<div class="card" style="border-left:4px solid var(--danger);">
      <b>⚠ 아래 마진은 실제보다 높게 나옵니다</b>
      <ul style="margin:6px 0 0 18px;">${gapMsgs.map((m) => `<li class="muted">${m}</li>`).join("")}</ul>
      <p class="muted" style="margin:6px 0 0;">원가가 비어 있으면 그만큼 마진이 부풀려집니다.
      매입 &gt; 자산 목록에서 매입가를 채우면 숫자가 제자리를 찾습니다.</p>
    </div>` : ""}
    <div class="kpi-row">
      <div class="kpi"><div class="kpi-label">매입 (${sum.purchase.slips}건)</div>
        <div class="kpi-value">${fmtWon(sum.purchase.amount)}</div>
        <div class="muted" style="font-size:12px;">자산 ${sum.purchase.assets}대</div></div>
      <div class="kpi"><div class="kpi-label">판매 (${sum.sales.orders}건 출고)</div>
        <div class="kpi-value">${fmtWon(sum.sales.revenue)}</div>
        ${sum.sales.fee || sum.sales.refund
          ? `<div class="muted" style="font-size:12px;">실입금 ${fmtWon(sum.sales.netRevenue)}</div>` : ""}</div>
      <div class="kpi" style="border-left-color:${marginColor};"><div class="kpi-label">마진</div>
        <div class="kpi-value" style="color:${marginColor};">${fmtWon(sum.sales.margin)}</div>
        <div class="muted" style="font-size:12px;">마진율 ${sum.sales.marginRate}%</div></div>
      <div class="kpi"><div class="kpi-label">보유 재고 ${sum.stock.assets}대</div>
        <div class="kpi-value">${fmtWon(sum.stock.amount)}</div>
        <div class="muted" style="font-size:12px;">매입가 기준</div></div>
    </div>

    <div class="card">
      <h3>손익 구성 <span class="muted" style="font-size:13px;">${escapeHtml(sum.from)} ~ ${escapeHtml(sum.to)}</span></h3>
      <div class="table-wrap"><table>
        <tbody>
          <tr><td>판매 금액</td><td style="text-align:right;"><b>${fmtWon(sum.sales.revenue)}</b></td></tr>
          <tr><td class="muted">− 쇼핑몰·PG 판매수수료</td><td style="text-align:right;" class="muted">${fmtWon(sum.sales.fee)}</td></tr>
          <tr><td class="muted">− 환불</td><td style="text-align:right;" class="muted">${fmtWon(sum.sales.refund)}</td></tr>
          <tr><td><b>= 실입금</b></td><td style="text-align:right;"><b>${fmtWon(sum.sales.netRevenue)}</b></td></tr>
          <tr><td class="muted">− 자산 매입원가</td><td style="text-align:right;" class="muted">${fmtWon(sum.sales.buyCost)}</td></tr>
          <tr><td class="muted">− 수리비(A/S 포함)</td><td style="text-align:right;" class="muted">${fmtWon(sum.sales.repairCost)}</td></tr>
          ${sum.sales.repairVat ? `<tr>
            <td class="muted" style="padding-left:16px;">그중 부가세 <span class="muted">(매입세액)</span></td>
            <td style="text-align:right;" class="muted"
              title="수리비 총액에 포함된 부가세입니다. 매출 부가세에서 뺄 수 있습니다.&#10;공급가 ${fmtWon(sum.sales.repairNet)} + 부가세 ${fmtWon(sum.sales.repairVat)} = ${fmtWon(sum.sales.repairCost)}"
              >${fmtWon(sum.sales.repairVat)}</td></tr>` : ""}
          <tr><td class="muted">− 출고 택배비</td><td style="text-align:right;" class="muted">${fmtWon(sum.sales.shippingCost)}</td></tr>
          <tr><td><b>= 마진</b></td><td style="text-align:right;"><b style="color:${marginColor};">${fmtWon(sum.sales.margin)}</b></td></tr>
        </tbody>
      </table></div>
      <p class="muted" style="margin-top:8px;">출고 확인된 주문과, 그 주문에 매칭된 자산의 원가로 계산합니다.
      A/S 무상 처리 비용 ${fmtWon(sum.as.companyCost)}(${sum.as.tickets}건)은 해당 자산에 반영된 경우만 포함됩니다.
      ${!sum.sales.fee && !sum.sales.shippingCost
        ? `<br><b>수수료·택배비가 0입니다</b> — 설정 &gt; 정산에서 몰별 요율과 택배비를 넣으면 실제로 남는 돈 기준으로 바뀝니다.` : ""}</p>
    </div>

    <div class="card">
      <div class="inline-row">
        <h3 style="margin:0; flex:1;">무엇이 남는 장사인가</h3>
        ${[["model", "모델별"], ["grade", "등급별"], ["supplier", "매입처별"]].map(([k, l]) =>
          `<button class="btn btn-sm ${(state.profitBy || "model") === k ? "btn-primary" : ""}" data-profit="${k}">${l}</button>`).join("")}
      </div>
      <div id="rp-profit"><p class="muted">불러오는 중…</p></div>
    </div>

    <div class="card">
      <div class="inline-row">
        <h3 style="margin:0; flex:1;">매출 대장</h3>
        <button class="btn btn-sm" id="rp-ledger-export">📤 엑셀 내보내기</button>
      </div>
      <p class="muted" style="margin:4px 0 8px;">주문 한 건이 한 줄 — 판매가부터 마진까지. 세무·정산에 그대로 씁니다.</p>
      <div id="rp-ledger"><p class="muted">불러오는 중…</p></div>
    </div>

    <div class="card">
      <h3>월별 추이</h3>
      <div class="table-wrap"><table>
        <thead><tr><th>월</th><th>매입</th><th style="width:28%;"></th><th>판매</th><th style="width:28%;"></th></tr></thead>
        <tbody>${months.map((m) => `
          <tr>
            <td><b>${escapeHtml(m.month)}</b></td>
            <td>${fmtWon(m.purchaseAmount)}<div class="muted" style="font-size:12px;">${m.purchaseSlips}건</div></td>
            <td><div style="background:var(--slate-soft); border-radius:4px; height:8px;">
              <div style="width:${Math.round(m.purchaseAmount / maxPurchase * 100)}%; background:var(--text-dim); height:8px; border-radius:4px;"></div></div></td>
            <td>${fmtWon(m.revenue)}<div class="muted" style="font-size:12px;">${m.orders}건</div></td>
            <td><div style="background:var(--primary-soft); border-radius:4px; height:8px;">
              <div style="width:${Math.round(m.revenue / maxRevenue * 100)}%; background:var(--primary); height:8px; border-radius:4px;"></div></div></td>
          </tr>`).join("") || `<tr><td colspan="5" class="muted">데이터가 없습니다.</td></tr>`}
        </tbody></table></div>
    </div>

    <div class="card">
      <h3>채널별 판매</h3>
      <div class="table-wrap"><table>
        <thead><tr><th>채널</th><th>출고 건수</th><th>판매 금액</th></tr></thead>
        <tbody>${chans.map((c) => `
          <tr><td>${chBadge(c.channel)}</td><td>${c.orders}건</td><td>${fmtWon(c.revenue)}</td></tr>`).join("")
          || `<tr><td colspan="3" class="muted">출고 내역이 없습니다.</td></tr>`}
        </tbody></table></div>
    </div>

    <div class="card">
      <h3>담당자 실적</h3>
      <div class="table-wrap"><table>
        <thead><tr><th>담당자</th><th>제작 완료</th><th>출고 확인</th><th>출고 마감</th><th>합계</th></tr></thead>
        <tbody>${staff.map((s) => `
          <tr><td><b>${escapeHtml(s.name)}</b></td><td>${s.production}</td><td>${s.inspection}</td>
          <td>${s.shipping}</td><td><b>${s.production + s.inspection + s.shipping}</b></td></tr>`).join("")
          || `<tr><td colspan="5" class="muted">기록이 없습니다.</td></tr>`}
        </tbody></table></div>
    </div>

    <div class="card">
      <h3>오래 묵은 재고 <span class="muted" style="font-size:13px;">입고 후 경과일 순</span></h3>
      <div class="table-wrap"><table>
        <thead><tr><th>관리번호</th><th>모델</th><th>등급</th><th>상태</th><th>매입가</th><th>경과</th></tr></thead>
        <tbody>${aging.slice(0, 20).map((a) => `
          <tr>
            <td><b>${escapeHtml(a.assetNo)}</b></td>
            <td>${escapeHtml(a.model || "-")}</td>
            <td>${escapeHtml(a.grade)}</td>
            <td>${statusChip(a.status)}</td>
            <td>${fmtWon(a.purchasePrice)}</td>
            <td>${a.days >= 90 ? `<span class="chip chip-red">${a.days}일</span>`
                 : a.days >= 30 ? `<span class="chip chip-blue">${a.days}일</span>` : a.days + "일"}</td>
          </tr>`).join("") || `<tr><td colspan="6" class="muted">보유 재고가 없습니다.</td></tr>`}
        </tbody></table></div>
    </div>`;

  $("#rp-ledger-export").addEventListener("click", () =>
    window.open(`/api/reports/ledger/export${qs}`, "_blank"));
  $$("button[data-profit]", host).forEach((b) => b.addEventListener("click", () => {
    state.profitBy = b.dataset.profit;
    loadReports();
  }));
  loadLedger(qs);
  loadProfitability(qs);
}

/* 무엇이 남는 장사인가 — 모델·등급·매입처별 */
async function loadProfitability(qs) {
  const host = $("#rp-profit");
  if (!host) return;
  const by = state.profitBy || "model";
  try {
    const r = await api(`/api/reports/profitability${qs}&by=${by}`);
    if (!$("#rp-profit")) return;
    host.innerHTML = r.rows.length ? `<div class="table-wrap"><table>
      <thead><tr><th>${by === "model" ? "제품" : by === "grade" ? "등급" : "매입처"}</th>
        <th style="text-align:right;">판매</th><th style="text-align:right;">실입금</th>
        <th style="text-align:right;">원가</th><th style="text-align:right;">마진</th>
        <th style="text-align:right;">대당 마진</th><th style="text-align:right;">마진율</th></tr></thead>
      <tbody>${r.rows.map((x) => `<tr>
        <td><b>${escapeHtml(x.key)}</b> <span class="muted">${x.units}대</span></td>
        <td style="text-align:right;">${fmtWon(x.revenue)}</td>
        <td style="text-align:right;">${fmtWon(x.netRevenue)}</td>
        <td style="text-align:right;" class="muted">${fmtWon(x.cost)}</td>
        <td style="text-align:right;"><b style="color:${x.margin >= 0 ? "var(--primary)" : "var(--danger)"};">${fmtWon(x.margin)}</b></td>
        <td style="text-align:right;">${fmtWon(x.marginPerUnit)}</td>
        <td style="text-align:right;">${x.marginRate}%</td>
      </tr>`).join("")}</tbody></table></div>`
      : `<p class="muted">이 기간에 출고된 자산이 없습니다.</p>`;
  } catch (err) { host.innerHTML = `<p class="muted">${escapeHtml(err.message)}</p>`; }
}

/* 매출 대장 */
async function loadLedger(qs) {
  const host = $("#rp-ledger");
  if (!host) return;
  try {
    const r = await api("/api/reports/ledger" + qs);
    if (!$("#rp-ledger")) return;
    const t = r.totals;
    host.innerHTML = r.rows.length ? `
      <div class="table-wrap"><table>
        <thead><tr><th>출고일</th><th>쇼핑몰</th><th>주문번호</th><th>상품</th><th>자산번호</th>
          <th style="text-align:right;">판매가</th><th style="text-align:right;">수수료</th>
          <th style="text-align:right;">환불</th><th style="text-align:right;">실입금</th>
          <th style="text-align:right;">원가</th><th style="text-align:right;">마진</th></tr></thead>
        <tbody>${r.rows.slice(0, 200).map((x) => `<tr>
          <td class="muted">${escapeHtml(x.shippedAt)}</td>
          <td>${escapeHtml(x.channel || "-")}</td>
          <td>${escapeHtml(x.orderNumber || "-")}</td>
          <td style="max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${escapeHtml(x.productName)}">${escapeHtml(x.productName)}</td>
          <td class="muted">${escapeHtml(x.assetNos || "-")}</td>
          <td style="text-align:right;">${fmtWon(x.amount)}</td>
          <td style="text-align:right;" class="muted">${x.fee ? fmtWon(x.fee) : "-"}</td>
          <td style="text-align:right;" class="muted">${x.refund ? fmtWon(x.refund) : "-"}</td>
          <td style="text-align:right;">${fmtWon(x.netAmount)}</td>
          <td style="text-align:right;" class="muted">${fmtWon(x.cost)}</td>
          <td style="text-align:right;"><b style="color:${x.margin >= 0 ? "var(--primary)" : "var(--danger)"};">${fmtWon(x.margin)}</b></td>
        </tr>`).join("")}</tbody>
        <tfoot><tr style="border-top:2px solid var(--border);">
          <td colspan="5"><b>합계 ${t.orders}건</b></td>
          <td style="text-align:right;"><b>${fmtWon(t.amount)}</b></td>
          <td style="text-align:right;">${fmtWon(t.fee)}</td>
          <td style="text-align:right;">${fmtWon(t.refund)}</td>
          <td style="text-align:right;"><b>${fmtWon(t.netAmount)}</b></td>
          <td style="text-align:right;">${fmtWon(t.cost)}</td>
          <td style="text-align:right;"><b>${fmtWon(t.margin)}</b></td>
        </tr></tfoot>
      </table></div>
      ${r.rows.length > 200 ? `<p class="muted">화면에는 200건까지 보입니다 — 전체는 엑셀로 받으세요(총 ${r.rows.length}건).</p>` : ""}`
      : `<p class="muted">이 기간에 출고된 주문이 없습니다.</p>`;
  } catch (err) { host.innerHTML = `<p class="muted">${escapeHtml(err.message)}</p>`; }
}
