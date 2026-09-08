"""관리자 전용 기간별 작업량 — 기존 order-workflow 화면을 OWS 데이터로 제공."""
from datetime import date
from flask import abort, g, jsonify, request
from ..db import get_db
from . import bp

STAGES = (("production", "제작 완료", "production_by", "production_at"),
          ("inspection", "SW 검수 완료", "inspection_by", "inspection_at"))

PREBUILD_STAGES = (("prebuildProduction", "선제작 완료", "production_by", "production_at"),
                   ("prebuildInspection", "선SW 검수 완료", "inspection_by", "inspection_at"))
EXTRA_STAGES = (("specChange", "사양변경"),)

def _date_arg(name):
    try:
        return date.fromisoformat((request.args.get(name) or "").strip())
    except ValueError:
        abort(400, description="조회 기간을 올바르게 입력하세요.")

@bp.get("/workload-stats")
def workload_stats():
    if not g.user["is_admin"]:
        abort(403, description="관리자만 기간별 작업량을 조회할 수 있습니다.")
    start, end = _date_arg("from"), _date_arg("to")
    if end < start:
        abort(400, description="종료일은 시작일보다 빠를 수 없습니다.")
    if (end - start).days > 366:
        abort(400, description="조회 기간은 최대 1년입니다.")
    rows = get_db().execute("""
        SELECT o.id,o.channel,o.order_no,o.product_code,o.product_name,o.option_name,
               o.quantity,o.recipient,o.production_by,o.production_at,o.inspection_by,o.inspection_at,
               (SELECT GROUP_CONCAT(a.asset_no, ', ') FROM order_assets oa
                JOIN assets a ON a.id=oa.asset_id WHERE oa.order_id=o.id) asset_nos,
               (SELECT COUNT(*) FROM order_assets oa WHERE oa.order_id=o.id) asset_cnt
               ,(SELECT COUNT(*) FROM asset_prebuilds p
                 WHERE p.used_order_id=o.id) prebuild_cnt
               ,(SELECT COUNT(*) FROM asset_prebuilds p
                 WHERE p.used_order_id=o.id AND p.spec_change_required=1) spec_change_cnt
        FROM orders o WHERE o.cancelled_at=''
          AND (SUBSTR(o.production_at,1,10) BETWEEN ? AND ?
               OR SUBSTR(o.inspection_at,1,10) BETWEEN ? AND ?)""",
        (start.isoformat(),end.isoformat(),start.isoformat(),end.isoformat())).fetchall()
    all_stages = STAGES + PREBUILD_STAGES + EXTRA_STAGES
    workers, details = {}, []
    for row in rows:
        units = int(row["asset_cnt"] or 0) or max(1, int(row["quantity"] or 1))
        for key,label,worker_col,time_col in STAGES:
            worker, completed = (row[worker_col] or "").strip(), (row[time_col] or "").strip()
            if not worker or not (start.isoformat() <= completed[:10] <= end.isoformat()):
                continue
            out_key, out_label = key, label
            # 동일 사양 선제작을 실제 주문에 사용하면 선제작/선SW 담당자에게
            # 일반 제작완료와 일반 SW검수 실적도 각각 +1 한다.
            # 사양이 바뀐 주문의 제작완료는 일반 제작이 아니라 변경 작업자의 사양변경 실적이다.
            if row["spec_change_cnt"] and key == "production":
                out_key, out_label = "specChange", "사양변경"
            item = workers.setdefault(worker,{"worker":worker,"total":0,"units":0,
                "stages":{stage[0]:0 for stage in all_stages}})
            item["total"] += 1; item["units"] += units; item["stages"][out_key] += 1
            details.append({"id":row["id"],"stage":out_key,"stageLabel":out_label,"worker":worker,
                "completedAt":completed,"orderNumber":row["order_no"] or "","channel":row["channel"] or "",
                "productCode":row["product_code"] or "","productName":row["product_name"] or "",
                "optionName":row["option_name"] or "","quantity":units,"recipient":row["recipient"] or "",
                "managementNumber":row["asset_nos"] or ""})

    # 선제작은 주문 출고 여부와 무관하게 실제 선제작 작업을 완료한 시점에 별도 작업량으로 센다.
    prebuild_rows = get_db().execute("""
        SELECT p.id,p.production_by,p.production_at,p.inspection_by,p.inspection_at,
               p.built_ram_type,p.built_ram_primary,p.built_ram2_type,p.built_ram2,
               p.built_ram,p.built_ssd_type,p.built_ssd,p.built_hdd,
               a.asset_no,a.product_code,a.maker,a.model,a.ram,a.ssd
        FROM asset_prebuilds p JOIN assets a ON a.id=p.asset_id
        WHERE p.cancelled_at='' AND p.credit_voided=0
          AND (SUBSTR(p.production_at,1,10) BETWEEN ? AND ?
           OR SUBSTR(p.inspection_at,1,10) BETWEEN ? AND ?)""",
        (start.isoformat(),end.isoformat(),start.isoformat(),end.isoformat())).fetchall()
    for row in prebuild_rows:
        product_name = " ".join(x for x in (row["maker"], row["model"]) if x) or "선제작 제품"
        ram1 = " ".join(x for x in (row["built_ram_type"], row["built_ram_primary"]) if x)
        ram2 = " ".join(x for x in (row["built_ram2_type"], row["built_ram2"]) if x and x != "없음")
        ram_parts = " + ".join(x for x in (ram1, ram2) if x)
        ram_spec = (f"{ram_parts} (총 {row['built_ram']})" if ram2 else ram1) or row["built_ram"] or row["ram"]
        ssd_spec = " ".join(x for x in (row["built_ssd_type"], row["built_ssd"] or row["ssd"]) if x)
        hdd_spec = f"HDD {row['built_hdd']}" if row["built_hdd"] and row["built_hdd"] != "없음" else ""
        option_name = " / ".join(x for x in (ram_spec, ssd_spec, hdd_spec) if x)
        for key,label,worker_col,time_col in PREBUILD_STAGES:
            worker, completed = (row[worker_col] or "").strip(), (row[time_col] or "").strip()
            if not worker or not (start.isoformat() <= completed[:10] <= end.isoformat()):
                continue
            item = workers.setdefault(worker,{"worker":worker,"total":0,"units":0,
                "stages":{stage[0]:0 for stage in all_stages}})
            item["total"] += 1; item["units"] += 1; item["stages"][key] += 1
            details.append({"id":row["id"],"stage":key,"stageLabel":label,"worker":worker,
                "completedAt":completed,"orderNumber":"선제작","channel":"",
                "productCode":row["product_code"] or "","productName":product_name,
                "optionName":option_name,"quantity":1,"recipient":"",
                "managementNumber":row["asset_no"] or ""})

    worker_rows = sorted(workers.values(),key=lambda x:(-x["total"],x["worker"]))
    details.sort(key=lambda x:(x["completedAt"],x["orderNumber"]),reverse=True)
    return jsonify({"dateFrom":start.isoformat(),"dateTo":end.isoformat(),"days":(end-start).days+1,
        "stageLabels":{key:label for key,label,*_ in all_stages},"workers":worker_rows,"workOrders":details,
        "total":sum(x["total"] for x in worker_rows),"units":sum(x["units"] for x in worker_rows)})
