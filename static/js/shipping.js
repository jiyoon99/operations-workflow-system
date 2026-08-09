/* HMS 배송/송장 — QC 완료 주문의 송장 선출력(자산번호 인쇄) + 출고 확인 + 송장 목록 */
"use strict";

/* 회수한 물건을 되돌릴 상태 — 화면에서 골라 쓰도록 한글 라벨을 붙여 둔다 */
const RECEIVE_STATUSES = [
  ["in_stock", "입고 (그대로 재고로)"],
  ["refurbishing", "정비중 (손봐서 다시 판매)"],
  ["repair", "수리"],
  ["defective", "불량"],
  ["scrapped", "폐기"],
];

/* 송장 칸 — 발급 조건·발급 중·재발급을 한자리에서 다룬다(2026-07-29 전수조사).
   ★예전엔 버튼이 항상 활성이라, 주소 없는 주문(실데이터 28건 중 23건)에서 누를 때마다
     '수취인과 주소를 먼저 입력하세요' 토스트만 뜨고 끝났다. 무엇이 빠졌는지 행에서 바로 보이게 한다. */
function waybillBlockers(o) {
  const out = [];
  if (!o.assets.length && !o.isReview) out.push("자산 미매칭");
  if (!(o.recipient || "").trim()) out.push("수취인 없음");
  if (!(o.address || "").trim()) out.push("주소 없음");
  return out;
}

function waybillCellHtml(o, activeWb, canShip) {
  if (activeWb && activeWb.status === "pending") {
    // 발급이 도중에 멈춘 상태 — PDF를 누르면 영문 오류만 떴다
    return `<span class="chip chip-violet">발급 중</span>
      <div class="muted" style="font-size:12px;">택배사 응답을 기다리는 중입니다.
        몇 분 지나도 그대로면 [송장 목록]에서 취소 후 다시 발급하세요.</div>`;
  }
  if (activeWb) {
    return `<b>${escapeHtml(activeWb.invoiceNo)}</b>${
        activeWb.status === "test" ? ' <span class="chip chip-slate">테스트</span>' : ""}
      <button class="btn btn-sm" data-pdf="${activeWb.wid}">PDF</button>
      ${canShip && activeWb.status !== "delivered"
        ? `<button class="btn btn-ghost btn-sm" data-reissue="${o.id}" data-wid="${activeWb.wid}"
             title="기존 송장을 취소하고 새로 발급합니다">재발급</button>` : ""}`;
  }
  if (!canShip) return '<span class="muted">-</span>';
  const blockers = waybillBlockers(o);
  if (blockers.length) {
    return `<button class="btn btn-sm" disabled>송장 발급·출력</button>
      <div class="chip chip-red" style="margin-top:4px; white-space:normal;">${escapeHtml(blockers.join(" · "))}</div>
      <div class="muted" style="font-size:11px;">주문관리 ▸ 해당 주문 ▸ [상세]에서 채울 수 있습니다</div>`;
  }
  // 📦 한 송장으로 여러 상자가 나가는 경우가 있다(대표 2026-07-30) — 발급 전에 지정한다
  return `<div class="inline-row" style="gap:4px;">
    <input type="number" class="wb-box" data-oid="${o.id}" min="1" max="10" value="1"
           title="상자 수 — 2개 이상이면 송장에 「박스 N개」로 찍힙니다"
           style="width:52px; padding:4px 6px; border:1px solid var(--border);
                  border-radius:8px; background:var(--bg); font-size:12px;">
    <button class="btn btn-sm btn-primary" data-issue="${o.id}">송장 발급·출력</button>
  </div>`;
}

function renderShippingView(main) {
  // 배송 전용 사용자(shipping.view)도 들어올 수 있어야 한다
  if (!canSeeMenu("shipping")) {
    main.innerHTML = `<h1 class="page-title">배송 / 송장</h1><div class="card placeholder"><p>배송 메뉴 접근 권한이 없습니다.</p></div>`;
    return;
  }
  if (!state.shipTab) state.shipTab = "ready";
  main.innerHTML = `
    <h1 class="page-title">배송 / 송장</h1>
    <p class="page-desc">QC 완료 → <b>송장 먼저 출력</b>(상품명에 쇼핑몰·제품코드·자산번호·옵션 인쇄) → 포장 → 출고 확인</p>
    <div class="tabs">
      <button data-stab="ready" class="${state.shipTab === "ready" ? "active" : ""}">송장 발급 대기 / 출고</button>
      <button data-stab="waybills" class="${state.shipTab === "waybills" ? "active" : ""}">송장 목록</button>
      <button data-stab="recall" class="${state.shipTab === "recall" ? "active" : ""}">회수 / 반품</button>
    </div>
    <div id="stab-body"></div>`;
  $$("button[data-stab]", main).forEach((b) => b.addEventListener("click", () => {
    state.shipTab = b.dataset.stab;
    clearPollers();          // 탭 전환 시 이전 폴러 정리 (누적 방지)
    ++state.renderSeq;
    renderShippingView(main);
  }));
  const body = $("#stab-body");
  if (state.shipTab === "ready") {
    renderShipReady(body);
    // 폴러는 화면당 1회만 등록한다. renderShipReady 안에서 등록하면
    // 폴러가 다시 renderShipReady를 부르며 인터벌이 기하급수로 늘어난다.
    addPoller(() => {
      if (!isEditingInput() && state.view === "shipping" && state.shipTab === "ready") {
        renderShipReady($("#stab-body"));
      }
    }, 15000);
  } else if (state.shipTab === "waybills") {
    renderWaybillList(body);
  } else {
    renderRecallList(body);
  }
}

/* ---------------- 회수 / 반품 ---------------- */

async function renderRecallList(body) {
  const seq = state.renderSeq;
  const canShip = hasPerm("orders.ship");
  body.innerHTML = `<p class="muted">불러오는 중…</p>`;
  let rows, orders;
  try {
    [rows, orders] = await Promise.all([
      api("/api/waybills?type=recall"),
      // ★출고된 주문은 대개 '보관' 처리되므로 진행 중만 보면 선택칸이 비어 버린다.
      //   반품은 이미 나간 주문에 거는 것이라 전체에서 찾아야 한다.
      api("/api/orders?view=all&limit=1000").then((r) => r.orders || []),
    ]);
    if (seq !== state.renderSeq) return;
  } catch (err) {
    if (seq === state.renderSeq) body.innerHTML = `<p class="muted">${escapeHtml(err.message)}</p>`;
    return;
  }
  const recalls = rows.filter((w) => w.type === "recall");
  // 이미 회수를 걸어 둔 주문은 목록에서 뺀다(중복 예약은 서버도 막지만 고를 이유가 없다)
  const recalled = new Set(recalls.filter((w) => !["canceled"].includes(w.status)).map((w) => w.orderId));
  const shipped = orders.filter((o) => o.shippingDone && !o.cancelledAt && !recalled.has(o.id));
  body.innerHTML = `
    <div class="card">
      <h3>회수 예약</h3>
      <p class="muted">반품·교환으로 물건을 돌려받을 때 예약합니다.
      회수 송장번호는 기사가 집화할 때 CJ가 부여하므로 예약 직후에는 비어 있는 것이 정상입니다.</p>
      ${canShip ? `<div class="inline-row">
        <select id="rc-order" style="min-width:280px;">
          <option value="">- 출고 완료 주문 선택 -</option>
          ${shipped.map((o) => `<option value="${o.id}">${escapeHtml(`${o.orderNumber || "#" + o.id} ${o.recipient} · ${o.productName}`)}</option>`).join("")}
        </select>
        <input type="date" id="rc-date" title="수거 희망일">
        <input type="text" id="rc-reason" placeholder="회수 사유(예: 단순 변심 반품)" style="flex:1; min-width:160px;">
        <button class="btn btn-sm btn-primary" id="rc-add" ${shipped.length ? "" : "disabled"}>회수 예약</button>
      </div>` : ""}
      <div class="table-wrap"><table>
        <thead><tr><th>회수 ID</th><th>고객</th><th>내용</th><th>송장번호</th><th>상태</th><th>수거 희망일</th><th>예약</th><th></th></tr></thead>
        <tbody>${recalls.map((w) => `
          <tr>
            <td>${escapeHtml(w.wid)}</td>
            <td>${escapeHtml(w.recipient)}<div class="muted" style="font-size:12px;">${escapeHtml(w.phone)}</div></td>
            <td style="max-width:280px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${escapeHtml(w.items)}">${escapeHtml(w.items)}</td>
            <td>${w.invoiceNo ? escapeHtml(w.invoiceNo) : '<span class="muted">집화 시 부여</span>'}</td>
            <td>${w.status === "delivered" ? '<span class="chip chip-green">회수 완료</span>'
                 : w.status === "canceled" ? '<span class="chip chip-red">취소</span>'
                 : w.status === "test" ? '<span class="chip chip-slate">테스트</span>'
                 : '<span class="chip chip-blue">회수 중</span>'}</td>
            <td>${w.scheduledDate
              ? escapeHtml(w.scheduledDate)
              : '<span class="muted">미지정</span>'}</td>
            <td class="muted" style="font-size:12px;">${escapeHtml(w.createdBy)}<br>${escapeHtml((w.createdAt || "").slice(5, 16).replace("T", " "))}</td>
            <td>${canShip && !["delivered", "canceled"].includes(w.status)
              ? `<button class="btn btn-sm btn-primary" data-received="${w.wid}">입고 처리</button>
                 <button class="btn btn-ghost btn-sm" data-rcancel="${w.wid}">취소</button>` : ""}</td>
          </tr>
          ${canShip && !["delivered", "canceled"].includes(w.status) ? `
          <tr class="rc-recv" data-recvrow="${w.wid}" style="display:none;">
            <td colspan="8" style="background:var(--bg);">
              <div class="inline-row" style="margin:0;">
                ${(w.items || "").includes("A/S회수")
                  ? `<b>A/S 회수품입니다</b>
                     <span class="muted">— 고객 물건이라 판매 재고로 잡지 않고 'A/S' 상태로 입고됩니다.</span>`
                  : `<b>회수한 물건을 어떤 상태로 둘까요?</b>
                     <select data-recvsel="${w.wid}">
                       ${RECEIVE_STATUSES.map(([v, l], i) =>
                         `<option value="${v}" ${i === 1 ? "selected" : ""}>${l}</option>`).join("")}
                     </select>`}
                <button class="btn btn-sm btn-primary" data-recvok="${w.wid}">입고 확정</button>
                <button class="btn btn-ghost btn-sm" data-recvno="${w.wid}">닫기</button>
              </div>
            </td>
          </tr>` : ""}`).join("") || `<tr><td colspan="8" class="muted">회수 건이 없습니다.</td></tr>`}
        </tbody></table></div>
    </div>`;
  if (!canShip) return;
  const addBtn = $("#rc-add");
  if (addBtn) addBtn.addEventListener("click", async () => {
    const oid = $("#rc-order").value;
    if (!oid) { toast("주문을 선택하세요.", true); return; }
    addBtn.disabled = true;
    try {
      const res = await api(`/api/orders/${oid}/recall`, { method: "POST", body: {
        reason: $("#rc-reason").value.trim(), pickupDate: $("#rc-date").value } });
      toast(res.simulated ? `테스트 회수 예약: ${res.wid}` : `회수 예약 완료: ${res.wid}`);
      renderRecallList(body);
    } catch (err) { toast(err.message, true); addBtn.disabled = false; }
  });
  // 입고 처리 — 영문 코드를 외워 타이핑하지 않도록 그 자리에서 골라 확정한다
  $$("button[data-received]", body).forEach((b) => b.addEventListener("click", () => {
    const row = $(`tr[data-recvrow="${b.dataset.received}"]`, body);
    if (row) row.style.display = row.style.display === "none" ? "table-row" : "none";
  }));
  $$("button[data-recvno]", body).forEach((b) => b.addEventListener("click", () => {
    const row = $(`tr[data-recvrow="${b.dataset.recvno}"]`, body);
    if (row) row.style.display = "none";
  }));
  $$("button[data-recvok]", body).forEach((b) => b.addEventListener("click", async () => {
    const wid = b.dataset.recvok;
    const back = $(`select[data-recvsel="${wid}"]`, body)?.value || "refurbishing";
    b.disabled = true;
    try {
      const r = await api(`/api/waybills/${wid}/received`, { method: "POST", body: { status: back } });
      toast(`입고 처리 완료 — 자산 ${r.assets.length}대를 '${statusLabel(r.status || back)}' 상태로 넣었습니다.`);
      renderRecallList(body);
    } catch (err) { toast(err.message, true); b.disabled = false; }
  }));
  $$("button[data-rcancel]", body).forEach((b) => b.addEventListener("click", async () => {
    if (!confirm("이 회수 예약을 취소할까요?")) return;
    try { await api(`/api/waybills/${b.dataset.rcancel}/cancel`, { method: "POST" }); renderRecallList(body); }
    catch (err) { toast(err.message, true); }
  }));
}

/* 쇼핑몰에 아직 안 올라간 송장 — 여기서 다시 보낸다(전에는 대표가 몰 관리자에서 손으로 입력) */
async function renderInvoicePush(body) {
  const host = $("#wf-push", body);
  if (!host) return;
  let rows;
  try {
    rows = await api("/api/orders/invoice-push/pending");
  } catch (_e) { return; }
  if (!rows.length) { host.innerHTML = ""; return; }
  host.innerHTML = `
    <div style="border:1px solid var(--border); border-left:4px solid var(--blue);
                border-radius:8px; padding:10px 12px; margin:8px 0;">
      <div class="inline-row" style="margin:0;">
        <b style="flex:1;">쇼핑몰에 송장번호가 아직 안 올라간 주문 ${rows.length}건</b>
        <button class="btn btn-sm btn-primary" id="wf-push-all">모두 보내기</button>
      </div>
      <div class="table-wrap" style="margin-top:6px;"><table>
        <thead><tr><th>쇼핑몰</th><th>주문번호</th><th>수취인</th><th>송장번호</th><th>사유</th><th></th></tr></thead>
        <tbody>${rows.map((r) => `<tr>
          <td>${chBadge(r.channel)}</td>
          <td>${escapeHtml(r.orderNumber || "-")}</td>
          <td>${escapeHtml(r.recipient)}</td>
          <td>${escapeHtml(r.invoiceNo)}</td>
          <td class="muted" style="max-width:280px;">${escapeHtml(r.error || "아직 보내지 않음")}</td>
          <td><button class="btn btn-sm" data-push="${r.id}">보내기</button></td>
        </tr>`).join("")}</tbody>
      </table></div>
    </div>`;
  const send = async (ids) => {
    try {
      const r = await api("/api/orders/invoice-push", { method: "POST", body: { ids } });
      toast(r.failed.length ? `${r.ok}건 전송 · ${r.failed.length}건 실패` : `${r.ok}건 전송 완료`);
      if (r.failed.length) toast(r.failed[0].message, true);
      renderInvoicePush(body);
    } catch (err) { toast(err.message, true); }
  };
  $("#wf-push-all", host).addEventListener("click", () => {
    if (confirm(`${rows.length}건의 송장번호를 쇼핑몰에 보냅니다.\n고객에게 배송 안내가 나갈 수 있습니다. 계속할까요?`)) {
      send(rows.map((r) => r.id));
    }
  });
  $$("button[data-push]", host).forEach((b) => b.addEventListener("click", () =>
    send([Number(b.dataset.push)])));
}

/* ---------------- 발급 대기 / 출고 ---------------- */

async function renderShipReady(body) {
  const seq = state.renderSeq;
  const canShip = hasPerm("orders.ship");
  // 출고 확인은 배송 담당의 일 — 셋팅 작업 권한이 없어도 체크할 수 있어야 한다
  const canWork = hasPerm("orders.work") || hasPerm("orders.ship");
  body.innerHTML = `<p class="muted">불러오는 중…</p>`;
  let orders;
  try {
    orders = (await fetchOrders({ view: "active" }))
      .filter((o) => o.productionDone && o.softwareInspectionDone);
    if (seq !== state.renderSeq) return;
  } catch (err) {
    if (seq === state.renderSeq) body.innerHTML = `<p class="muted">${escapeHtml(err.message)}</p>`;
    return;
  }
  // ★방문수령·퀵은 택배가 아니라 송장이 필요 없다(대표 2026-08-05).
  //   '송장 발급 대기'에 섞어 두면 매번 왜 송장이 없냐고 확인하게 된다 — 따로 뺀다.
  const notShipped = orders.filter((o) => !o.shippingDone);
  const waiting = notShipped.filter(needsWaybill);
  const pickup = notShipped.filter((o) => !needsWaybill(o));
  const shipped = orders.filter((o) => o.shippingDone);
  // ★출고완료는 예전에 앞 50건만 잘라 보여 나머지는 볼 방법이 없었다 — 이제 넘길 수 있다
  const pgW = paged(waiting, "shipWait");
  const pgK = paged(pickup, "shipPickup");
  const pgD = paged(shipped, "shipDone");
  const rowHtml = (o) => {
    // 회수 송장은 배송/송장 > 회수·반품 탭에서 따로 본다 — 출고 송장만 여기서 다룬다
    const fwd = (o.waybills || []).filter((w) => w.type !== "recall" && w.status !== "canceled");
    const hasWb = fwd.length > 0;
    const activeWb = hasWb ? fwd[0] : null;
    return `<tr>
      <td>${chBadge(o.channel)}${receiveChip(o)}<div class="muted" style="font-size:12px;">${escapeHtml(o.orderNumber || `#${o.id}`)}</div></td>
      <td style="max-width:240px;"><b>${escapeHtml(o.productName)}</b>
        ${o.optionName ? `<div class="muted" style="font-size:12px;">${escapeHtml(o.optionName)}</div>` : ""}
        ${o.memo ? `<div class="chip chip-red" style="margin-top:4px; white-space:normal;">📌 ${escapeHtml(o.memo)}</div>` : ""}</td>
      <td>${escapeHtml(o.recipient)}<div class="muted" style="font-size:12px;">${escapeHtml(o.phone)}</div></td>
      <td>${o.assets.map((a) => `<span class="chip chip-green">${escapeHtml(a.assetNo)}</span>`).join(" ") || '<span class="chip chip-red">자산 미매칭</span>'}</td>
      <td>${waybillCellHtml(o, activeWb, canShip)}</td>
      <td class="stage-cell">
        <label class="check-line" style="justify-content:center;">
          <input type="checkbox" data-ship="${o.id}" ${o.shippingDone ? "checked" : ""} ${canWork ? "" : "disabled"}>
        </label>
        ${o.shippingBy ? `<div class="stage-by">${escapeHtml(o.shippingBy)}</div>` : ""}
      </td>
    </tr>`;
  };
  body.innerHTML = `
    <div class="kpi-row">
      <div class="kpi"><div class="kpi-label">송장 발급 대기 (QC 완료)</div><div class="kpi-value">${waiting.filter((o) => !o.waybills.some((w) => w.type !== "recall" && w.status !== "canceled")).length}</div></div>
      <div class="kpi"><div class="kpi-label">포장 대기 (송장 출력됨)</div><div class="kpi-value">${waiting.filter((o) => o.waybills.some((w) => w.type !== "recall" && w.status !== "canceled")).length}</div></div>
      <div class="kpi"><div class="kpi-label">오늘 출고 확인</div><div class="kpi-value">${shipped.filter((o) => (o.shippingAt || "").slice(0, 10) === ymd()).length}</div></div>
    </div>
    <div class="card">
      <h3>진행 중 (QC 완료 주문)</h3>
      <div class="table-wrap"><table>
        <thead><tr><th>채널 / 주문</th><th>상품</th><th>수취인</th><th>자산번호</th><th>송장</th><th class="stage-th">출고 확인</th></tr></thead>
        <tbody>${pgW.rows.map(rowHtml).join("") || `<tr><td colspan="6" class="muted">QC 완료된 대기 주문이 없습니다. (셋팅 탭에서 제작·검수를 완료하면 여기에 나타납니다)</td></tr>`}</tbody>
      </table></div>
      ${pgW.bar}
    </div>
    ${pickup.length ? `<div class="card" style="border-left:4px solid var(--violet, #7c3aed);">
      <h3>🚶 택배 아님 — 방문수령 · 퀵 <span class="muted" style="font-size:13px;">${pickup.length}건</span></h3>
      <p class="muted" style="margin:0 0 8px;">송장을 뽑지 않습니다. 물건을 건네고 [출고 확인]만 눌러 주세요.</p>
      <div class="table-wrap"><table>
        <thead><tr><th>채널 / 주문</th><th>상품</th><th>수취인</th><th>자산번호</th><th>송장</th><th class="stage-th">출고 확인</th></tr></thead>
        <tbody>${pgK.rows.map(rowHtml).join("")}</tbody>
      </table></div>
      ${pgK.bar}
    </div>` : ""}
    ${shipped.length ? `<div class="card"><h3>출고 확인됨</h3>
      <div class="table-wrap"><table>
        <thead><tr><th>채널 / 주문</th><th>상품</th><th>수취인</th><th>자산번호</th><th>송장</th><th class="stage-th">출고 확인</th></tr></thead>
        <tbody>${pgD.rows.map(rowHtml).join("")}</tbody>
      </table></div>
      ${pgD.bar}</div>` : ""}`;
  wirePager(body, "shipWait", () => renderShipReady(body));
  wirePager(body, "shipPickup", () => renderShipReady(body));
  wirePager(body, "shipDone", () => renderShipReady(body));

  const boxOf = (oid) => {
    const el = $(`.wb-box[data-oid="${oid}"]`, body);
    return Math.max(1, Math.min(Number(el ? el.value : 1) || 1, 10));
  };
  $$("button[data-issue]", body).forEach((b) => b.addEventListener("click", async () => {
    b.disabled = true;
    try {
      const res = await api(`/api/orders/${b.dataset.issue}/waybill`,
                            { method: "POST", body: { boxQty: boxOf(b.dataset.issue) } });
      toast(res.simulated
        ? `테스트 송장 발행: ${res.invoiceNo} (CJ 미설정 — 설정>배송/CJ에서 실발행 전환)`
        : `송장 발행: ${res.invoiceNo}`);
      window.open(`/api/waybills/${res.wid}/pdf`, "_blank");
      renderShipReady(body);
    } catch (err) { toast(err.message, true); b.disabled = false; }
  }));
  // 재발급 = [기존 송장 취소] + [새로 발급]을 한 번에. 예전엔 다른 탭에 가서
  // 손으로 취소한 뒤 돌아와야 했고, 그 경로 안내조차 없었다(2026-07-29 전수조사).
  $$("button[data-reissue]", body).forEach((b) => b.addEventListener("click", async () => {
    if (!confirm("기존 송장을 취소하고 새 송장을 발급합니다.\n"
                 + "이미 붙여서 내보낸 송장이면 새 번호로 다시 붙여야 합니다.\n\n계속할까요?")) return;
    b.disabled = true;
    try {
      await api(`/api/waybills/${b.dataset.wid}/cancel`, { method: "POST", body: {} });
      const res = await api(`/api/orders/${b.dataset.reissue}/waybill`,
                            { method: "POST", body: { boxQty: boxOf(b.dataset.reissue) } });
      toast(`재발급 완료: ${res.invoiceNo || res.wid}`);
      window.open(`/api/waybills/${res.wid}/pdf`, "_blank");
      renderShipReady(body);
    } catch (err) { toast(err.message, true); b.disabled = false; }
  }));
  $$("button[data-pdf]", body).forEach((b) => b.addEventListener("click", () =>
    window.open(`/api/waybills/${b.dataset.pdf}/pdf`, "_blank")));
  $$("input[data-ship]", body).forEach((cb) => cb.addEventListener("change", async () => {
    cb.disabled = true;
    try {
      await api(`/api/orders/${cb.dataset.ship}`, {
        method: "PATCH", body: { action: "shipping", value: cb.checked },
      });
    } catch (err) {
      toast(err.message, true);
    }
    renderShipReady(body); // 성공/실패 모두 서버 상태로 다시 그린다
  }));
}

/* ---------------- 송장 목록 ---------------- */

async function renderWaybillList(body) {
  const seq = state.renderSeq;
  const canShip = hasPerm("orders.ship");
  body.innerHTML = `<p class="muted">불러오는 중…</p>`;
  let rows;
  try {
    const f = state.wbFilter || (state.wbFilter = { q: "", status: "" });
    const p = new URLSearchParams();
    if (f.q) p.set("q", f.q);
    if (f.status) p.set("status", f.status);
    rows = await api("/api/waybills?" + p.toString());
    if (seq !== state.renderSeq) return;
  } catch (err) {
    if (seq === state.renderSeq) body.innerHTML = `<p class="muted">${escapeHtml(err.message)}</p>`;
    return;
  }
  const f = state.wbFilter;
  const stChip = { issued: "chip-green", test: "chip-slate", canceled: "chip-red",
                   delivered: "chip-blue", pending: "chip-violet" };
  const stLabel = { issued: "발행", test: "테스트", canceled: "취소",
                    delivered: "배송완료", pending: "발급 중" };
  body.innerHTML = `
    <div class="card">
      <div class="inline-row">
        <input type="text" id="wf-q" placeholder="송장번호/수취인/상품 검색" value="${escapeHtml(f.q)}" style="min-width:220px;">
        <select id="wf-status"><option value="">전체</option>
          ${Object.entries(stLabel).map(([k, l]) => `<option value="${k}" ${f.status === k ? "selected" : ""}>${l}</option>`).join("")}</select>
        <button class="btn btn-sm btn-primary" id="wf-search">조회</button>
        <span class="muted">${rows.length}건</span>
        <span style="flex:1"></span>
        ${canShip ? `<button class="btn btn-sm" id="wf-track" title="CJ에 지금 배송상태를 물어봅니다(3시간마다 자동 갱신)">📡 배송추적 새로고침</button>` : ""}
      </div>
      <div id="wf-push"></div>
      <div class="table-wrap"><table>
        <thead><tr><th>송장 ID</th><th>송장번호</th><th>수취인</th><th>상품명(라벨)</th><th>상태</th><th>발행</th><th></th></tr></thead>
        <tbody>${rows.map((w) => `
          <tr>
            <td>${escapeHtml(w.wid)}</td>
            <td><b>${escapeHtml(w.invoiceNo)}</b></td>
            <td>${escapeHtml(w.recipient)}</td>
            <td style="max-width:300px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${escapeHtml(w.items)}">${escapeHtml(w.items)}</td>
            <td><span class="chip ${stChip[w.status] || "chip-slate"}">${stLabel[w.status] || escapeHtml(w.status)}</span></td>
            <td class="muted">${escapeHtml(w.createdBy)}<br>${escapeHtml((w.createdAt || "").slice(5, 16).replace("T", " "))}</td>
            <td>
              ${w.type === "recall"
                ? '<span class="muted" title="회수는 기사가 집화할 때 CJ가 번호를 매깁니다">집화 시 번호 부여</span>'
                : `<button class="btn btn-sm" data-pdf="${w.wid}">PDF</button>`}
              ${canShip && !["canceled", "delivered"].includes(w.status)
                ? `<button class="btn btn-ghost btn-sm" data-wbcancel="${w.wid}">취소</button>`
                : (w.status === "delivered"
                    ? '<span class="muted" style="font-size:12px;" title="이미 고객에게 도착했습니다. 취소해도 택배사에는 반영되지 않습니다">배송 완료</span>'
                    : "")}
            </td>
          </tr>`).join("") || `<tr><td colspan="7" class="muted">송장이 없습니다.</td></tr>`}
        </tbody></table></div>
    </div>`;
  const doSearch = () => {
    f.q = $("#wf-q").value.trim(); f.status = $("#wf-status").value;
    renderWaybillList(body);
  };
  $("#wf-search").addEventListener("click", doSearch);
  autoSearch("#wf-q", doSearch);
  if (canShip) renderInvoicePush(body);
  const trackBtn = $("#wf-track");
  if (trackBtn) trackBtn.addEventListener("click", async () => {
    trackBtn.disabled = true;
    trackBtn.textContent = "조회 중…";
    try {
      const r = await api("/api/waybills/track-sync", { method: "POST" });
      toast(r.changed ? `${r.checked}건 확인 · ${r.changed}건 상태가 바뀌었습니다.`
                      : `${r.checked}건 확인 · 바뀐 건이 없습니다.`);
      renderWaybillList(body);
    } catch (err) {
      toast(err.message, true);
      trackBtn.disabled = false;
      trackBtn.textContent = "📡 배송추적 새로고침";
    }
  });
  $$("button[data-pdf]", body).forEach((b) => b.addEventListener("click", () =>
    window.open(`/api/waybills/${b.dataset.pdf}/pdf`, "_blank")));
  $$("button[data-wbcancel]", body).forEach((b) => b.addEventListener("click", async () => {
    if (!confirm(`송장 ${b.dataset.wbcancel}을(를) 취소할까요?`)) return;
    try {
      await api(`/api/waybills/${b.dataset.wbcancel}/cancel`, { method: "POST" });
      toast("송장을 취소했습니다.");
      renderWaybillList(body);
    } catch (err) { toast(err.message, true); }
  }));
}
