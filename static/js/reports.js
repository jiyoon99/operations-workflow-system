/* 설정 ▸ 매출/실적의 [판매 분석]·[매출 대장] 보기 — 옛 '리포트' 탭에서 이사.
   ★2026-08-31 대표: "리포트 기능은 설정 내 매출/실적으로 통합하여 필요한 기능만".
   가져온 것: 무엇이 남는 장사인가 · 채널별 판매 · 월별 추이 · 오래 묵은 재고 ·
   데이터 갭 경고(→판매 분석), 매출 대장+엑셀(→매출 대장).
   버린 것: KPI·손익 구성([📊 기간 실적]이 부가세·TMS 합산까지 상위호환),
   담당자 실적([기간별 작업량]이 대체 — 출고 확인 단계도 그쪽에 추가). */
"use strict";

/* 공용 기간 필터 — 두 보기가 같은 state.repFilter 를 쓴다(보기를 오가도 기간 유지) */
function repFilter() {
  const today = new Date();
  return state.repFilter || (state.repFilter = {
    from: ymd(new Date(today.getFullYear(), today.getMonth(), 1)),
    to: ymd(today),
  });
}

function repFilterHtml(f) {
  return `<div class="card" style="padding:14px 16px;">
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
    </div>`;
}

function wireRepFilter(host, f, reload) {
  $("#rp-go", host).addEventListener("click", () => {
    const from = $("#rp-from", host).value, to = $("#rp-to", host).value;
    // ★날짜를 거꾸로 넣으면 조용히 전부 0원으로 나왔다 — 매출이 없는 건지
    //   날짜를 잘못 넣은 건지 구분할 수 없었다(2026-07-29 전수조사).
    if (from && to && from > to) {
      toast(`시작일(${from})이 종료일(${to})보다 늦습니다. 날짜를 바꿔 주세요.`, true);
      return;
    }
    f.from = from; f.to = to;
    reload();
  });
  $$("button[data-quick]", host).forEach((b) => b.addEventListener("click", () => {
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
    $("#rp-from", host).value = f.from; $("#rp-to", host).value = f.to;
    reload();
  }));
}

/* ── 판매 분석 — 무엇이 남는 장사인가 · 채널별 · 월별 추이 · 오래 묵은 재고 ── */
function renderAnalysisView(host) {
  const f = repFilter();
  host.innerHTML = repFilterHtml(f) + `<div id="rp-body"><p class="muted">불러오는 중…</p></div>`;
  wireRepFilter(host, f, () => loadAnalysis(host));
  loadAnalysis(host);
}

async function loadAnalysis(root) {
  const seq = ++state.renderSeq;
  const f = state.repFilter;
  const host = $("#rp-body", root);
  const qs = `?from=${f.from}&to=${f.to}`;
  let sum, months, chans, aging, slipChans;
  try {
    [sum, months, chans, aging, slipChans] = await Promise.all([
      // 데이터 갭 경고용 — 마진 숫자를 보여주는 화면이니 원가 구멍을 같이 알려야 한다
      api("/api/reports/summary" + qs).catch(() => null),
      api("/api/reports/monthly"),
      api("/api/reports/channels" + qs),
      api("/api/reports/aging"),
      api("/api/reports/slip-channels" + qs).catch(() => []),
    ]);
    if (seq !== state.renderSeq || !$("#rp-body", root)) return;
  } catch (err) {
    if (host) host.innerHTML = `<p class="muted">${escapeHtml(err.message)}</p>`;
    return;
  }
  const maxRevenue = Math.max(1, ...months.map((m) => m.revenue));
  const maxPurchase = Math.max(1, ...months.map((m) => m.purchaseAmount));

  const g = (sum && sum.dataGaps) || {};
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
    <div class="card">
      <div class="inline-row">
        <h3 style="margin:0; flex:1;">무엇이 남는 장사인가</h3>
        ${[["model", "모델별"], ["grade", "등급별"], ["supplier", "매입처별"]].map(([k, l]) =>
          `<button class="btn btn-sm ${(state.profitBy || "model") === k ? "btn-primary" : ""}" data-profit="${k}">${l}</button>`).join("")}
      </div>
      <div id="rp-profit"><p class="muted">불러오는 중…</p></div>
    </div>

    <div class="card">
      <h3>채널별 판매</h3>
      <div class="table-wrap"><table>
        <thead><tr><th>채널</th><th>출고 건수</th><th>판매 금액</th></tr></thead>
        <tbody>${chans.map((c) => `
          <tr><td>${chBadge(c.channel)}</td><td>${c.orders}건</td><td>${fmtWon(c.revenue)}</td></tr>`).join("")
          || `<tr><td colspan="3" class="muted">출고 내역이 없습니다.</td></tr>`}
        </tbody></table></div>
      ${(slipChans || []).length ? `
      <h3 style="margin-top:14px;">TMS 전표 채널별
        <span class="muted" style="font-size:13px;">— 방문·B2B 등, 몰 주문과 합산하지 않음</span></h3>
      <div class="table-wrap"><table>
        <thead><tr><th>채널</th><th>전표</th><th>수량</th><th>판매 금액</th></tr></thead>
        <tbody>${slipChans.map((c) => `
          <tr><td>${chBadge(c.channel)}</td><td>${c.slips}건</td><td>${c.qty}대</td>
          <td>${fmtWon(c.revenue)}</td></tr>`).join("")}
        </tbody></table></div>` : ""}
    </div>

    <div class="card">
      <h3>월별 추이 <span class="muted" style="font-size:13px;">TMS 전표는 몰 주문과 별도 집계(합산 아님)</span></h3>
      <div class="table-wrap"><table>
        <thead><tr><th>월</th><th>매입</th><th style="width:22%;"></th><th>판매(몰)</th><th style="width:22%;"></th><th>TMS 전표</th></tr></thead>
        <tbody>${months.map((m) => `
          <tr>
            <td><b>${escapeHtml(m.month)}</b></td>
            <td>${fmtWon(m.purchaseAmount)}<div class="muted" style="font-size:12px;">${m.purchaseSlips}건</div></td>
            <td><div style="background:var(--slate-soft); border-radius:4px; height:8px;">
              <div style="width:${Math.round(m.purchaseAmount / maxPurchase * 100)}%; background:var(--text-dim); height:8px; border-radius:4px;"></div></div></td>
            <td>${fmtWon(m.revenue)}<div class="muted" style="font-size:12px;">${m.orders}건</div></td>
            <td><div style="background:var(--primary-soft); border-radius:4px; height:8px;">
              <div style="width:${Math.round(m.revenue / maxRevenue * 100)}%; background:var(--primary); height:8px; border-radius:4px;"></div></div></td>
            <td>${m.slipRevenue ? fmtWon(m.slipRevenue) : "-"}<div class="muted" style="font-size:12px;">${
              m.slipCount ? m.slipCount + "건" : ""}</div></td>
          </tr>`).join("") || `<tr><td colspan="6" class="muted">데이터가 없습니다.</td></tr>`}
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

  $$("button[data-profit]", host).forEach((b) => b.addEventListener("click", () => {
    state.profitBy = b.dataset.profit;
    loadAnalysis(root);
  }));
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

/* ── 매출 대장 — 주문 1건 1줄, 세무·정산용. 엑셀로 그대로 나간다 ── */
function renderLedgerView(host) {
  const f = repFilter();
  host.innerHTML = repFilterHtml(f) + `
    <div class="card">
      <div class="inline-row">
        <h3 style="margin:0; flex:1;">매출 대장</h3>
        <button class="btn btn-sm" id="rp-ledger-export">📤 엑셀 내보내기</button>
      </div>
      <p class="muted" style="margin:4px 0 8px;">주문 한 건이 한 줄 — 판매가부터 마진까지. 세무·정산에 그대로 씁니다.</p>
      <div id="rp-ledger"><p class="muted">불러오는 중…</p></div>
    </div>`;
  const reload = () => loadLedger(`?from=${f.from}&to=${f.to}`);
  wireRepFilter(host, f, reload);
  $("#rp-ledger-export", host).addEventListener("click", () =>
    window.open(`/api/reports/ledger/export?from=${f.from}&to=${f.to}`, "_blank"));
  reload();
}

/* 매출 대장 표 */
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
