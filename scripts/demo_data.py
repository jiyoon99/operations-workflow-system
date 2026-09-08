"""Synthetic interview fixtures created through the application's APIs."""

DEMO_USER = "admin"
DEMO_PASSWORD = "12345678"


def seed_demo(app):
    from app import config
    from app.db import tx

    client = app.test_client()

    def call(method, url, body):
        response = client.open(url, method=method, json=body)
        if response.status_code not in (200, 201):
            raise RuntimeError(f"Demo seed {url}: {response.status_code} {response.get_json()}")
        return response.get_json()

    call("POST", "/api/auth/setup", {
        "username": DEMO_USER, "displayName": "데모 관리자", "password": DEMO_PASSWORD,
    })
    call("POST", "/api/users", {
        "username": "viewer", "displayName": "조회 담당자", "password": DEMO_PASSWORD,
        "perms": ["orders.view", "purchase.view"], "allCategories": True,
    })
    today = config.now().strftime("%Y-%m-%d")
    category = client.get("/api/categories").get_json()[0]["id"]
    supplier = call("POST", "/api/suppliers", {"name": "데모 공급사", "phone": "010-0000-0000"})
    batch = call("POST", "/api/purchase-batches", {
        "supplierId": supplier["id"], "purchaseDate": today,
        "stage": "purchased", "totalAmount": 2400000,
    })
    models = [("Lenovo", "ThinkPad T14"), ("Dell", "Latitude 5420"), ("HP", "EliteBook 840")]
    assets = []
    for i in range(8):
        maker, model = models[i % len(models)]
        asset = call("POST", "/api/assets", {
            "categoryId": category, "batchId": batch["id"], "maker": maker, "model": model,
            "grade": "SA", "purchasePrice": 300000, "qty": 1,
            "cpu": "i5", "ram": "16G", "ssd": "512G", "serial": f"DEMO-SERIAL-{i + 1:03}",
        })[0]
        # Establish the initial fixture state only in the newly created demo DB.
        with app.app_context(), tx(write=True) as conn:
            conn.execute("UPDATE assets SET status='ready', tier='실재고', received=1, "
                         "stock_listed=1, product_code=? WHERE id=?",
                         (f"DEMO{i + 1}_i5_내장", asset["id"]))
        assets.append(asset)
    orders = []
    steps = [[], ["preparing"], ["production"],
             ["production", "softwareInspection"], ["production", "softwareInspection", "shipping"]]
    for i, actions in enumerate(steps):
        order = call("POST", "/api/orders", {
            "channel": "수기", "orderNo": f"DEMO-ORDER-{i + 1:03}",
            "recipient": f"데모 고객 {i + 1}", "phone": f"010-0000-{i + 1:04}",
            "address": "서울특별시 예시구 예시로 1", "postalCode": "00000",
            "productName": models[i % len(models)][1], "productCode": f"DEMO{i + 1}_i5_내장",
            "optionName": "16GB / 512GB", "quantity": 1, "amount": 450000,
        })
        call("PATCH", f"/api/orders/{order['id']}", {"action": "assets", "assetIds": [assets[i]["id"]]})
        for action in actions:
            call("PATCH", f"/api/orders/{order['id']}", {"action": action, "value": True})
        orders.append(order)
    call("POST", "/api/sale-slips", {
        "channel": "방문구매", "customer": "데모 구매처", "saleDate": today,
        "lines": [{"assetNo": assets[6]["assetNo"], "salePrice": 480000}],
    })
    tickets = []
    for i, symptom in enumerate(["화면 점검", "충전 상태 점검", "키보드 점검"]):
        ticket = call("POST", "/api/as-tickets", {
            "customer": f"데모 고객 {i + 1}", "phone": f"010-0000-{i + 1:04}",
            "symptom": symptom, "chargeTo": "customer" if i == 0 else "company",
            "intakeItems": "본체, 충전기", "intake": "visit" if i < 2 else "parcel",
            "model": models[i][1],
        })
        if i < 2:
            call("PATCH", f"/api/as-tickets/{ticket['id']}", {"status": "done" if i == 0 else "repairing"})
        tickets.append(ticket)
    return {"assets": assets, "orders": orders, "tickets": tickets}
