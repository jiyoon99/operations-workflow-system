-- OWS 스키마 (Phase 0: 계정/권한/카테고리/세션/감사로그/설정)
-- 모든 테이블은 IF NOT EXISTS — init_db가 기동 시마다 실행해도 안전하다.

CREATE TABLE IF NOT EXISTS users (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  username       TEXT NOT NULL UNIQUE COLLATE NOCASE,
  display_name   TEXT NOT NULL,
  pw_hash        TEXT NOT NULL,
  is_admin       INTEGER NOT NULL DEFAULT 0,
  all_categories INTEGER NOT NULL DEFAULT 0,
  enabled        INTEGER NOT NULL DEFAULT 1,
  created_at     TEXT NOT NULL,
  updated_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS user_perms (
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  perm    TEXT NOT NULL,
  PRIMARY KEY (user_id, perm)
);

CREATE TABLE IF NOT EXISTS categories (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  name       TEXT NOT NULL UNIQUE,
  sort       INTEGER NOT NULL DEFAULT 0,
  enabled    INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS user_categories (
  user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
  PRIMARY KEY (user_id, category_id)
);

CREATE TABLE IF NOT EXISTS sessions (
  token      TEXT PRIMARY KEY,
  user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  last_seen  TEXT NOT NULL,
  ip         TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);

CREATE TABLE IF NOT EXISTS audit_log (
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  ts       TEXT NOT NULL,
  user_id  INTEGER,
  username TEXT,
  action   TEXT NOT NULL,
  target   TEXT,
  detail   TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts);
CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_log(action);

-- 설정: 섹션 단위 JSON KV (예: general / cj / malls)
CREATE TABLE IF NOT EXISTS settings (
  key        TEXT PRIMARY KEY,
  value      TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  updated_by TEXT
);

-- ============================================================ Phase 1: 매입/자산

CREATE TABLE IF NOT EXISTS suppliers (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  name       TEXT NOT NULL UNIQUE,
  contact    TEXT NOT NULL DEFAULT '',
  phone      TEXT NOT NULL DEFAULT '',
  memo       TEXT NOT NULL DEFAULT '',
  enabled    INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL
);

-- 부품 단가표(대표 2026-08-10): RAM·SSD 없이 들어온 노트북에 부품을 꽂아 파는데
-- 단가가 매일 바뀐다 — 여기 '오늘 단가'를 한 번만 고쳐 두면 부품 추가 시 자동 적용된다.
-- ★그날 단가는 자산에 붙는 순간 asset_repairs.cost 로 동결되므로 과거 원가는 안 바뀐다.
--   단가 변경 자체는 audit_log(part_price_updated)에 남는다.
CREATE TABLE IF NOT EXISTS parts (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  name       TEXT NOT NULL UNIQUE,           -- 스펙 표기와 맞춘다: "D4 8G", "NVMe 512G"
  category   TEXT NOT NULL DEFAULT '',       -- 'ram' | 'ssd' | ''(배터리·어댑터 등 기타)
  grp        TEXT NOT NULL DEFAULT '',       -- 대제목(그룹) — 단가표 접고 펴기 묶음(2026-08-13)
  price      INTEGER NOT NULL DEFAULT 0,     -- 현재 단가 ★부가세 포함 총액
  enabled    INTEGER NOT NULL DEFAULT 1,
  sort       INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT '',
  created_by TEXT NOT NULL DEFAULT '',
  updated_at TEXT NOT NULL DEFAULT '',
  updated_by TEXT NOT NULL DEFAULT ''
);

-- 부품 재고 이동(2026-08-25 대표) — 램·SSD를 '수량'으로 관리한다. 단가표(parts)가 SKU 축,
-- 여기가 원장: 현재고 = SUM(qty). +입고(매입/회수/보정) −차감(장착).
-- repair_id 로 자산 원가 행과 짝을 이룬다 — 원가를 되돌리면 재고도 같이 돌아온다.
CREATE TABLE IF NOT EXISTS part_stock_moves (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  part_id     INTEGER NOT NULL REFERENCES parts(id),
  qty         INTEGER NOT NULL,               -- +입고 / −차감
  unit_cost   INTEGER NOT NULL DEFAULT 0,
  amount      INTEGER NOT NULL DEFAULT 0,     -- 매입 행 총액(입고 때만)
  supplier_id INTEGER REFERENCES suppliers(id),
  batch_id    INTEGER REFERENCES purchase_batches(id),
  repair_id   INTEGER,                        -- asset_repairs.id (자동 차감 연결고리)
  order_id    INTEGER,
  asset_id    INTEGER,
  reason      TEXT NOT NULL DEFAULT '',       -- purchase | use | return | adjust
  move_date   TEXT NOT NULL DEFAULT '',
  memo        TEXT NOT NULL DEFAULT '',
  created_by  TEXT NOT NULL DEFAULT '',
  created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_psm_part ON part_stock_moves(part_id);
CREATE INDEX IF NOT EXISTS idx_psm_repair ON part_stock_moves(repair_id);

-- 업데이트 내역(2026-08-14 대표) — "작업자들이 뭐가 바뀌었는지 알 수 있게".
-- 하루치를 1. 2. 3. 으로 묶어 보여 준다. 한 줄 = 한 항목, seq가 그날의 순번.
CREATE TABLE IF NOT EXISTS changelog (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  day        TEXT NOT NULL,                  -- '2026-08-14'
  seq        INTEGER NOT NULL DEFAULT 0,
  text       TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT '',
  created_by TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_changelog_day ON changelog(day DESC, seq);

-- 제품코드의 '모델 고유 속성'(2026-08-13 대표) — 램 세대·저장장치 방식은 모델에 고정이라
-- 코드 단위로 한 번만 알아내면 그 코드의 모든 주문·자산에 재사용된다.
-- ★자산 스펙(실물 상태)과 다른 층위다: 여기는 "이 모델은 DDR4다"이지 "이 기계에 8G가 꽂혀
--   있다"가 아니다. 옵션 자동 기입이 D4/D5 부품을 고를 때 이 표를 본다.
CREATE TABLE IF NOT EXISTS code_specs (
  code         TEXT PRIMARY KEY,              -- 제품코드(고도몰 자체상품코드와 동일 축)
  ram_gen      TEXT NOT NULL DEFAULT '',      -- DDR3/DDR4/DDR5/LPDDR3/LPDDR4/LPDDR5
  ram_onboard  INTEGER NOT NULL DEFAULT 0,    -- 온보드·LP 표기 있었음(슬롯 업글 불가 신호)
  ram_gb       INTEGER NOT NULL DEFAULT 0,    -- 출고 기준 램 용량(GB) — 매입 부족분 계산 축
  storage_type TEXT NOT NULL DEFAULT '',      -- NVMe / M.2 SATA / 2.5 SATA
  storage_cap  TEXT NOT NULL DEFAULT '',      -- 출고 기준 저장 용량("256G"/"1TB" — 부품명 규약)
  source       TEXT NOT NULL DEFAULT '',      -- godo(자동 파싱) | manual(사람이 확정)
  spec_text    TEXT NOT NULL DEFAULT '',      -- 파싱에 쓴 원문(상품명+짧은설명) 스냅샷
  updated_at   TEXT NOT NULL DEFAULT '',
  updated_by   TEXT NOT NULL DEFAULT ''
);

-- 매입 전표. TMS 구조를 따라 가입고(V) → 입고확인 → 매입(P) 2단계로 운영한다.
-- stage='provisional'(가입고) | 'purchased'(매입). 전표번호는 V/P + YYMMDD-NNN.
CREATE TABLE IF NOT EXISTS purchase_batches (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  slip_no       TEXT NOT NULL DEFAULT '',
  stage         TEXT NOT NULL DEFAULT 'purchased',
  supplier_id   INTEGER REFERENCES suppliers(id),
  purchase_date TEXT NOT NULL,          -- 매입일(가입고 단계에서는 가입고일)
  total_amount  INTEGER NOT NULL DEFAULT 0,
  memo          TEXT NOT NULL DEFAULT '',
  purchase_type TEXT NOT NULL DEFAULT '일반매입',
  channel       TEXT NOT NULL DEFAULT '',
  receive_method TEXT NOT NULL DEFAULT '택배',
  requester     TEXT NOT NULL DEFAULT '',
  address       TEXT NOT NULL DEFAULT '',
  vat           INTEGER NOT NULL DEFAULT 0,
  fee           INTEGER NOT NULL DEFAULT 0,
  shipping_fee  INTEGER NOT NULL DEFAULT 0,
  shipping_cod  INTEGER NOT NULL DEFAULT 0,   -- 착불 여부
  tracking_no   TEXT NOT NULL DEFAULT '',
  paid          INTEGER NOT NULL DEFAULT 1,   -- 납부확인: 1=완납 0=미지급
  paid_amount   INTEGER NOT NULL DEFAULT 0,   -- 실제 지급액(부분지급 가능)
  paid_at       TEXT NOT NULL DEFAULT '',     -- 지급일
  paid_memo     TEXT NOT NULL DEFAULT '',
  confirmed_at  TEXT NOT NULL DEFAULT '',     -- 매입 확정일
  confirmed_by  TEXT NOT NULL DEFAULT '',
  -- 취소: TMS도 '매입취소' 상태를 쓴다(465건 중 6건). 지우지 않고 표시만 한다 —
  -- 전표번호는 소비된 채 남아 결번이 생기는 것이 정상이다(TMS 실데이터와 동일).
  cancelled_at  TEXT NOT NULL DEFAULT '',
  cancelled_by  TEXT NOT NULL DEFAULT '',
  cancel_reason TEXT NOT NULL DEFAULT '',
  -- 매입 반품: 거래처로 되돌려 보낸 날. TMS에는 칸이 있으나 실사용 0건이라 기록용으로만 둔다.
  returned_at   TEXT NOT NULL DEFAULT '',
  return_reason TEXT NOT NULL DEFAULT '',
  created_by    TEXT NOT NULL,
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_batches_supplier ON purchase_batches(supplier_id);
-- 나중에 추가된 컬럼(stage/slip_no)을 쓰는 인덱스는 db.py의 _POST_MIGRATE_INDEXES에서 만든다.
-- 여기 두면 기존 DB에서 컬럼 추가(_migrate) 전에 실행되어 "no such column"으로 실패한다.

-- 자산: 입/출고의 기준 키. asset_no는 자동발번(YYYYMMDD+0001)이지만
-- 기존 TMS 자산번호 이관을 위해 수정 가능(UNIQUE 유지, 변경은 이력에 기록).
-- 자산. asset_no = 관리번호(YYMMDD-NNNN, TMS와 동일 형식. 수동 지정 가능)
-- 스펙 필드(cpu~charger)와 보관위치·판매가는 TMS 매입상세를 따른다.
CREATE TABLE IF NOT EXISTS assets (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  asset_no       TEXT NOT NULL UNIQUE,
  batch_id       INTEGER REFERENCES purchase_batches(id),
  category_id    INTEGER REFERENCES categories(id),
  maker          TEXT NOT NULL DEFAULT '',   -- 브랜드
  model          TEXT NOT NULL DEFAULT '',
  serial         TEXT NOT NULL DEFAULT '',
  grade          TEXT NOT NULL DEFAULT '미정',
  purchase_price INTEGER NOT NULL DEFAULT 0,
  sale_price     INTEGER NOT NULL DEFAULT 0,
  location       TEXT NOT NULL DEFAULT '',   -- 보관위치
  cpu            TEXT NOT NULL DEFAULT '',
  gpu            TEXT NOT NULL DEFAULT '',   -- 그래픽
  ram            TEXT NOT NULL DEFAULT '',
  ssd            TEXT NOT NULL DEFAULT '',
  inch           TEXT NOT NULL DEFAULT '',
  battery        TEXT NOT NULL DEFAULT '',   -- 배터리효율
  charger        TEXT NOT NULL DEFAULT '',   -- 충전기유무
  received       INTEGER NOT NULL DEFAULT 1, -- 입고확인(가입고 자산은 0으로 시작)
  received_at    TEXT NOT NULL DEFAULT '',
  received_by    TEXT NOT NULL DEFAULT '',
  status         TEXT NOT NULL DEFAULT 'in_stock',
  notes          TEXT NOT NULL DEFAULT '',   -- 특이사항(매입상세비고)
  created_by     TEXT NOT NULL DEFAULT '',
  created_at     TEXT NOT NULL,
  updated_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_assets_status ON assets(status);
CREATE INDEX IF NOT EXISTS idx_assets_category ON assets(category_id);
CREATE INDEX IF NOT EXISTS idx_assets_batch ON assets(batch_id);
-- 정규화 시리얼 색인 (2026-08-24)
--   '매입중복(같은 시리얼 두 번)' 검사가 자산끼리 전수 대조라 O(n²)였다 —
--   자산 15,500대에서 '정리 대기' 목록이 65초 걸렸다. 표현식 색인으로 조회로 바꾼다.
--   ★식이 쿼리와 글자 그대로 같아야 색인을 탄다(_hold / issue=hold 와 맞춰 둘 것).
CREATE INDEX IF NOT EXISTS idx_assets_serial_norm
  ON assets(REPLACE(REPLACE(UPPER(serial),' ',''),'-',''));

CREATE TABLE IF NOT EXISTS asset_repairs (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  asset_id    INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
  repair_date TEXT NOT NULL,
  description TEXT NOT NULL,
  cost        INTEGER NOT NULL DEFAULT 0,
  created_by  TEXT NOT NULL,
  created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_repairs_asset ON asset_repairs(asset_id);

-- 자산 이력 타임라인(등록/수정/번호변경/수리/상태전이/매칭/출고/회수)
CREATE TABLE IF NOT EXISTS asset_events (
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  asset_id INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
  ts       TEXT NOT NULL,
  action   TEXT NOT NULL,
  actor    TEXT NOT NULL DEFAULT '',
  detail   TEXT
);
CREATE INDEX IF NOT EXISTS idx_asset_events_asset ON asset_events(asset_id, id);

-- ============================================================ Phase 2: 주문

CREATE TABLE IF NOT EXISTS orders (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  channel          TEXT NOT NULL DEFAULT '',
  order_no         TEXT NOT NULL DEFAULT '',
  import_key       TEXT NOT NULL DEFAULT '',
  dedupe_key       TEXT NOT NULL DEFAULT '',
  content_key      TEXT NOT NULL DEFAULT '',
  cross_key        TEXT NOT NULL DEFAULT '',
  source_file      TEXT NOT NULL DEFAULT '',
  ordered_at       TEXT NOT NULL DEFAULT '',
  product_name     TEXT NOT NULL DEFAULT '',
  option_name      TEXT NOT NULL DEFAULT '',
  product_code     TEXT NOT NULL DEFAULT '',
  quantity         INTEGER NOT NULL DEFAULT 1,
  amount           INTEGER NOT NULL DEFAULT 0,
  recipient        TEXT NOT NULL DEFAULT '',
  phone            TEXT NOT NULL DEFAULT '',
  postal_code      TEXT NOT NULL DEFAULT '',
  address          TEXT NOT NULL DEFAULT '',
  delivery_message TEXT NOT NULL DEFAULT '',
  memo             TEXT NOT NULL DEFAULT '',
  category_id      INTEGER REFERENCES categories(id),
  courier          TEXT NOT NULL DEFAULT '',
  tracking_no      TEXT NOT NULL DEFAULT '',
  -- 쇼핑몰 관점 상태(내부 QC 단계와 별개)
  pay_status       TEXT NOT NULL DEFAULT 'paid',   -- paid | unpaid(입금대기)
  mall_status      TEXT NOT NULL DEFAULT '',       -- 몰이 준 원본 상태(참고)
  delivered_at     TEXT NOT NULL DEFAULT '',       -- 배송완료/구매확정 시각
  -- 정산: 판매가가 아니라 '실제로 남는 돈'으로 마진을 봐야 한다
  fee_amount       INTEGER NOT NULL DEFAULT 0,     -- 쇼핑몰·PG 판매수수료
  fee_rate         REAL NOT NULL DEFAULT 0,        -- 적용 요율(%) — 근거 보존
  shipping_cost    INTEGER NOT NULL DEFAULT 0,     -- 출고 택배비(원가)
  refund_amount    INTEGER NOT NULL DEFAULT 0,     -- 환불·부분환불 금액
  refund_at        TEXT NOT NULL DEFAULT '',
  refund_reason    TEXT NOT NULL DEFAULT '',
  fee_manual       INTEGER NOT NULL DEFAULT 0,     -- 사람이 직접 확정한 수수료(자동계산 제외)
  is_review        INTEGER NOT NULL DEFAULT 0,     -- 리뷰어 출고(체험단) — 셋팅·QC가 구분해 다룬다
  review_note      TEXT NOT NULL DEFAULT '',       -- 예: 제품X 빈박스출고
  mall_sent_at     TEXT NOT NULL DEFAULT '',       -- 쇼핑몰에 송장번호 전송 완료 시각
  mall_send_error  TEXT NOT NULL DEFAULT '',       -- 전송 실패 사유(재전송 목록에 뜬다)
  -- 단계: 「불리언+담당자+시각」 3필드 세트 (검증된 원본 설계 계승)
  preparing        INTEGER NOT NULL DEFAULT 0,
  preparing_by     TEXT NOT NULL DEFAULT '',
  preparing_at     TEXT NOT NULL DEFAULT '',
  production_done  INTEGER NOT NULL DEFAULT 0,
  production_by    TEXT NOT NULL DEFAULT '',
  production_at    TEXT NOT NULL DEFAULT '',
  inspection_done  INTEGER NOT NULL DEFAULT 0,   -- SW 검수(QC)
  inspection_by    TEXT NOT NULL DEFAULT '',
  inspection_at    TEXT NOT NULL DEFAULT '',
  shipping_done    INTEGER NOT NULL DEFAULT 0,
  shipping_by      TEXT NOT NULL DEFAULT '',
  shipping_at      TEXT NOT NULL DEFAULT '',
  cancelled_at     TEXT NOT NULL DEFAULT '',
  cancelled_by     TEXT NOT NULL DEFAULT '',
  cancel_reason    TEXT NOT NULL DEFAULT '',
  restored_at      TEXT NOT NULL DEFAULT '',
  restored_by      TEXT NOT NULL DEFAULT '',
  archived_at      TEXT NOT NULL DEFAULT '',
  pending_update   TEXT,                          -- 배송지 변경 대기(JSON)
  raw              TEXT,                          -- 수집 원본(JSON)
  created_by       TEXT NOT NULL DEFAULT '',
  created_at       TEXT NOT NULL,
  updated_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_orders_updated ON orders(updated_at);
CREATE INDEX IF NOT EXISTS idx_orders_dedupe ON orders(dedupe_key);
CREATE INDEX IF NOT EXISTS idx_orders_content ON orders(content_key);
CREATE INDEX IF NOT EXISTS idx_orders_order_no ON orders(order_no);

-- 주문 ↔ 자산 매칭 (기존 '관리번호 다중 입력'을 자산 FK로 승격)
CREATE TABLE IF NOT EXISTS order_assets (
  order_id    INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
  asset_id    INTEGER NOT NULL REFERENCES assets(id),
  prev_status TEXT NOT NULL DEFAULT 'ready',  -- 매칭 전 자산 상태(해제 시 복원용)
  matched_by  TEXT NOT NULL DEFAULT '',
  matched_at  TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (order_id, asset_id)
);
CREATE INDEX IF NOT EXISTS idx_order_assets_asset ON order_assets(asset_id);

-- 주문 전에 자산 한 대를 제작·SW검수·출고준비까지 끝내 두는 선제작 작업판.
-- 실적은 완료 시점이 아니라 실제 주문에 사용된 시점(used_at)에 확정한다.
CREATE TABLE IF NOT EXISTS asset_prebuilds (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  asset_id        INTEGER NOT NULL UNIQUE REFERENCES assets(id),
  production_done INTEGER NOT NULL DEFAULT 0,
  production_by   TEXT NOT NULL DEFAULT '',
  production_at   TEXT NOT NULL DEFAULT '',
  inspection_done INTEGER NOT NULL DEFAULT 0,
  inspection_by   TEXT NOT NULL DEFAULT '',
  inspection_at   TEXT NOT NULL DEFAULT '',
  ready_done      INTEGER NOT NULL DEFAULT 0,
  ready_by        TEXT NOT NULL DEFAULT '',
  ready_at        TEXT NOT NULL DEFAULT '',
  used_order_id   INTEGER REFERENCES orders(id),
  used_by         TEXT NOT NULL DEFAULT '',
  used_at         TEXT NOT NULL DEFAULT '',
  note            TEXT NOT NULL DEFAULT '',
  created_by      TEXT NOT NULL DEFAULT '',
  created_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL DEFAULT ''
  ,built_ram_type  TEXT NOT NULL DEFAULT ''
  ,built_ram_primary TEXT NOT NULL DEFAULT ''
  ,built_ram2_type TEXT NOT NULL DEFAULT ''
  ,built_ram2      TEXT NOT NULL DEFAULT ''
  ,built_ram       TEXT NOT NULL DEFAULT ''
  ,built_ssd_type  TEXT NOT NULL DEFAULT ''
  ,built_ssd       TEXT NOT NULL DEFAULT ''
  ,built_hdd       TEXT NOT NULL DEFAULT ''
  ,credit_voided   INTEGER NOT NULL DEFAULT 0
  ,credit_void_reason TEXT NOT NULL DEFAULT ''
  ,spec_change_required INTEGER NOT NULL DEFAULT 0
  ,spec_change_reason TEXT NOT NULL DEFAULT ''
  ,cancelled_at TEXT NOT NULL DEFAULT ''
  ,cancelled_by TEXT NOT NULL DEFAULT ''
  ,cancel_reason TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_prebuild_ready ON asset_prebuilds(ready_done, used_order_id);

-- TMS 기본 사양은 assets.ram/ssd에 그대로 두고, 선제작에서 실제 장착한 부품만 별도 보관한다.
-- 선제작 기록을 취소해도 실물에 장착된 부품은 남으므로 asset_id에 귀속한다.
CREATE TABLE IF NOT EXISTS asset_prebuild_mounts (
  asset_id     INTEGER NOT NULL REFERENCES assets(id),
  category     TEXT NOT NULL CHECK(category IN ('ram','ssd')),
  part_id      INTEGER NOT NULL REFERENCES parts(id),
  qty          INTEGER NOT NULL DEFAULT 1,
  repair_id    INTEGER,
  installed_by TEXT NOT NULL DEFAULT '',
  installed_at TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (asset_id, category)
);
CREATE INDEX IF NOT EXISTS idx_prebuild_mount_part ON asset_prebuild_mounts(part_id);

-- ============================================================ Phase 3: 송장

CREATE TABLE IF NOT EXISTS waybills (
  wid           TEXT PRIMARY KEY,               -- WB-YYYYMMDD-NN
  order_id      INTEGER REFERENCES orders(id),
  type          TEXT NOT NULL DEFAULT 'forward',
  cj_kind       TEXT NOT NULL DEFAULT 'ship',
  invoice_no    TEXT NOT NULL DEFAULT '',
  status        TEXT NOT NULL DEFAULT 'issued', -- issued/failed/canceled/delivered/test
  recipient     TEXT NOT NULL DEFAULT '',
  phone         TEXT NOT NULL DEFAULT '',
  postal_code   TEXT NOT NULL DEFAULT '',
  address       TEXT NOT NULL DEFAULT '',
  items         TEXT NOT NULL DEFAULT '',       -- 송장 상품명 구성 문자열
  label         TEXT,                            -- 출력 스냅샷(JSON)
  cj_response   TEXT,                            -- CJ 원본 응답(JSON)
  cust_use_no   TEXT NOT NULL DEFAULT '',
  ori_invc_no   TEXT NOT NULL DEFAULT '',
  cj_rcpt_ymd   TEXT NOT NULL DEFAULT '',
  box_qty       INTEGER NOT NULL DEFAULT 1,
  scheduled_date TEXT NOT NULL DEFAULT '',
  cj_stage_cd   TEXT NOT NULL DEFAULT '',
  cj_stage_nm   TEXT NOT NULL DEFAULT '',
  cj_stage_at   TEXT NOT NULL DEFAULT '',
  asset_ids     TEXT NOT NULL DEFAULT '',   -- 회수 대상 자산 id(JSON 배열)
  asset_prev    TEXT NOT NULL DEFAULT '',   -- 회수 직전 자산 상태(JSON {id: status})
  as_ticket_id  INTEGER,
  created_by    TEXT NOT NULL DEFAULT '',
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_waybills_order ON waybills(order_id);
CREATE INDEX IF NOT EXISTS idx_waybills_invoice ON waybills(invoice_no);

-- ============================================================ A/S

CREATE TABLE IF NOT EXISTS as_tickets (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  ticket_no     TEXT NOT NULL UNIQUE,          -- AS-YYMMDD-NN
  order_id      INTEGER REFERENCES orders(id),
  asset_id      INTEGER REFERENCES assets(id),
  customer      TEXT NOT NULL DEFAULT '',
  phone         TEXT NOT NULL DEFAULT '',
  address       TEXT NOT NULL DEFAULT '',
  channel       TEXT NOT NULL DEFAULT '',
  symptom       TEXT NOT NULL DEFAULT '',      -- 증상
  as_type       TEXT NOT NULL DEFAULT 'repair',-- repair(수리)/exchange(교환)/refund(환불)/inspect(점검)
  status        TEXT NOT NULL DEFAULT 'received', -- received/collecting/repairing/done/returned/cancelled
  cost          INTEGER NOT NULL DEFAULT 0,    -- 수리비(회사 부담/청구)
  charge_to     TEXT NOT NULL DEFAULT 'company', -- company(무상)/customer(유상)
  result        TEXT NOT NULL DEFAULT '',      -- 처리 내용
  received_at   TEXT NOT NULL,
  closed_at     TEXT NOT NULL DEFAULT '',
  assignee      TEXT NOT NULL DEFAULT '',
  created_by    TEXT NOT NULL DEFAULT '',
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_as_status ON as_tickets(status);
CREATE INDEX IF NOT EXISTS idx_as_asset ON as_tickets(asset_id);

CREATE TABLE IF NOT EXISTS as_events (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  ticket_id INTEGER NOT NULL REFERENCES as_tickets(id) ON DELETE CASCADE,
  ts        TEXT NOT NULL,
  action    TEXT NOT NULL,
  actor     TEXT NOT NULL DEFAULT '',
  detail    TEXT
);
CREATE INDEX IF NOT EXISTS idx_as_events_ticket ON as_events(ticket_id, id);

-- 고객 안내 문자 발송 이력.
-- ★(ticket_id, event)에 UNIQUE를 걸어 같은 단계 문자가 두 번 나가지 않게 한다.
--   파일 로그로 막으면 동시 발송에서 새므로 DB 제약으로 보장한다.
CREATE TABLE IF NOT EXISTS sms_log (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  ticket_id  INTEGER REFERENCES as_tickets(id) ON DELETE CASCADE,
  event      TEXT NOT NULL,              -- received / collecting / done / returned / manual-N
  phone      TEXT NOT NULL DEFAULT '',
  text       TEXT NOT NULL DEFAULT '',
  msg_type   TEXT NOT NULL DEFAULT '',   -- SMS / LMS
  status     TEXT NOT NULL DEFAULT '',   -- sent / simulated / failed / skipped
  reason     TEXT NOT NULL DEFAULT '',   -- 실패·건너뜀 사유
  sent_by    TEXT NOT NULL DEFAULT '',
  sent_at    TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_sms_once ON sms_log(ticket_id, event)
  WHERE ticket_id IS NOT NULL AND status IN ('sent','simulated');
CREATE INDEX IF NOT EXISTS idx_sms_ticket ON sms_log(ticket_id, id);
CREATE INDEX IF NOT EXISTS idx_sms_sent_at ON sms_log(sent_at);

-- A/S 수리내역 라인(2026-08-31 대표 — 수리내역서·청구내역서의 항목별 금액).
-- amount 는 부가세 포함 총액(OWS 금액 규약), vat/net 은 저장 시 split_vat 결과를 동결.
CREATE TABLE IF NOT EXISTS as_ticket_items (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  ticket_id  INTEGER NOT NULL REFERENCES as_tickets(id) ON DELETE CASCADE,
  part_id    INTEGER REFERENCES parts(id),   -- 수리 단가표 프리필 출처(없어도 됨)
  name       TEXT NOT NULL,                  -- 항목명(부품·공임·출장비…)
  qty        INTEGER NOT NULL DEFAULT 1,
  amount     INTEGER NOT NULL DEFAULT 0,     -- 이 줄 총액(수량 반영, 부가세 포함)
  vat        INTEGER NOT NULL DEFAULT 0,
  net        INTEGER NOT NULL DEFAULT 0,
  sort       INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_as_items_ticket ON as_ticket_items(ticket_id);

-- A/S 문서 발행 이력 — 발행본은 스냅샷(JSON) 불변. 나중에 항목을 고쳐도 발행본은 그대로다.
CREATE TABLE IF NOT EXISTS as_documents (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  ticket_id  INTEGER NOT NULL REFERENCES as_tickets(id) ON DELETE CASCADE,
  doc_type   TEXT NOT NULL,                  -- repair(수리내역서) | invoice(청구내역서)
  doc_no     TEXT NOT NULL UNIQUE,           -- ASR-/ASB-YYMMDD-NN
  snapshot   TEXT NOT NULL,                  -- 회사정보+고객+항목+합계 JSON(도장은 인쇄 시점 현재값)
  issued_by  TEXT NOT NULL DEFAULT '',
  issued_at  TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_as_docs_ticket ON as_documents(ticket_id);

-- ============================================================ 제공 옵션
-- 쇼핑몰이 옵션을 실어 주지 않는 경우(카카오쇼핑 등) 우리가 직접 「이 주문에는
-- 이것도 챙긴다」를 지정한다. 셋팅·QC 화면에 체크 목록으로 뜨고, 다 체크해야
-- 제작 완료로 넘어간다. 송장에도 함께 찍혀 포장 담당이 한 번 더 대조한다.

CREATE TABLE IF NOT EXISTS prep_options (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  name       TEXT NOT NULL,                    -- 리브레오피스 설치
  note       TEXT NOT NULL DEFAULT '',         -- 작업자용 상세 안내
  enabled    INTEGER NOT NULL DEFAULT 1,
  sort       INTEGER NOT NULL DEFAULT 0,
  created_by TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_prep_option_name ON prep_options(name);

-- 어떤 주문에 그 옵션이 붙는지 — 「쇼핑몰 + 조건」
CREATE TABLE IF NOT EXISTS prep_option_rules (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  option_id   INTEGER NOT NULL REFERENCES prep_options(id) ON DELETE CASCADE,
  channel     TEXT NOT NULL DEFAULT '',        -- '' = 모든 쇼핑몰
  match_type  TEXT NOT NULL DEFAULT 'all',     -- all | code | product | option
  match_value TEXT NOT NULL DEFAULT '',
  enabled     INTEGER NOT NULL DEFAULT 1,
  created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_prep_rules_option ON prep_option_rules(option_id);

-- 셋팅·QC가 '챙겼다'고 체크한 기록. 옵션이 규칙에서 빠져도 지우지 않는다
-- (규칙을 껐다 켤 때 이미 챙긴 것을 다시 체크하게 되면 안 되므로).
CREATE TABLE IF NOT EXISTS order_option_checks (
  order_id   INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
  option_id  INTEGER NOT NULL REFERENCES prep_options(id) ON DELETE CASCADE,
  checked_by TEXT NOT NULL DEFAULT '',
  checked_at TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (order_id, option_id)
);
CREATE INDEX IF NOT EXISTS idx_option_checks_order ON order_option_checks(order_id);

-- 주문자 정보 변경 이력(2026-08-24 대표) — 셋팅 중 성함·연락처·주소를 고치기 전 스냅샷.
-- 롤백은 이 스냅샷으로 복원한다(복원 직전 현재 값도 다시 스냅샷 — 롤백의 롤백 가능).
CREATE TABLE IF NOT EXISTS order_contact_log (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  order_id    INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
  recipient   TEXT NOT NULL DEFAULT '',
  phone       TEXT NOT NULL DEFAULT '',
  postal_code TEXT NOT NULL DEFAULT '',
  address     TEXT NOT NULL DEFAULT '',
  delivery_message TEXT NOT NULL DEFAULT '', -- 배송메모(2026-08-26 대표: 같이 수정·복원)
  reason      TEXT NOT NULL DEFAULT '',      -- '수정 전' | '롤백 전' + 메모
  created_by  TEXT NOT NULL DEFAULT '',
  created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_contact_log_order ON order_contact_log(order_id);

-- 매입 지급 이력(2026-08-25 대표) — 전표에 언제 얼마를 냈는지. purchase_batches.paid_amount
-- 는 '누계'만 갖는다. 부분지급을 여러 번 하면 각 건을 여기 남겨 되짚을 수 있어야 한다.
CREATE TABLE IF NOT EXISTS purchase_payments (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  batch_id   INTEGER NOT NULL REFERENCES purchase_batches(id) ON DELETE CASCADE,
  amount     INTEGER NOT NULL,
  pay_date   TEXT NOT NULL DEFAULT '',
  method     TEXT NOT NULL DEFAULT '',       -- 계좌이체 / 현금 / 카드 등(자유)
  memo       TEXT NOT NULL DEFAULT '',
  created_by TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_purchase_payments_batch ON purchase_payments(batch_id);

-- 추가 결제(2026-08-24 대표) — 셋팅 중 부품 업그레이드 비용을 기존 주문에 합친 내역.
-- ★amount/fee 는 이미 orders.amount/fee_amount 에 '합쳐져' 있다(매출·마진 집계가 자동으로 맞게).
--   이 표는 내역·되돌리기·자동 수수료 재계산의 근거다. method: bank(입금·수수료 없음) /
--   mall(몰 결제·채널 요율) / custom(요율 직접). 금액은 부가세 포함 총액.
CREATE TABLE IF NOT EXISTS order_extras (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  order_id   INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
  amount     INTEGER NOT NULL,
  fee        INTEGER NOT NULL DEFAULT 0,
  method     TEXT NOT NULL DEFAULT 'bank',
  rate       REAL NOT NULL DEFAULT 0,
  note       TEXT NOT NULL DEFAULT '',
  created_by TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_order_extras_order ON order_extras(order_id);

-- TMS 엑셀 자동 반영 이력. 같은 파일을 두 번 넣지 않기 위해 내용 해시를 기록한다.
CREATE TABLE IF NOT EXISTS tms_sync_log (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  filename   TEXT NOT NULL,
  file_hash  TEXT NOT NULL UNIQUE,
  size       INTEGER NOT NULL DEFAULT 0,
  rows       INTEGER NOT NULL DEFAULT 0,
  created    INTEGER NOT NULL DEFAULT 0,   -- 새로 만든 자산 수
  updated    INTEGER NOT NULL DEFAULT 0,   -- 빈 칸을 채운 자산 수
  errors     INTEGER NOT NULL DEFAULT 0,
  synced_at  TEXT NOT NULL,
  note       TEXT NOT NULL DEFAULT ''
);

-- 자산번호 정정 기록(2026-08-18 대표 지시).
-- TMS에서 번호를 잘못 적어 판매로 찍힌 탓에, 실물이 멀쩡한데 OWS가 '이미 출고'라며
-- 막는 일이 생긴다. 매입에서 번호를 맞바꾸거나 넘겨받아 바로잡는데 —
-- ★TMS는 2시간마다 같은 엑셀을 다시 보낸다. 아무 장치가 없으면
--   ① 비워진 옛 번호를 '처음 보는 자산'으로 여겨 유령 자산을 새로 만들고
--   ② 바로잡은 자산을 다시 '판매'로 되돌린다(정방향 상태 자동갱신).
--   그래서 정정한 번호를 여기 남기고, 자산에는 assets.tms_lock 을 세운다.
CREATE TABLE IF NOT EXISTS tms_number_fixes (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  asset_no   TEXT NOT NULL,              -- TMS가 계속 보내는(=잘못 적힌) 번호
  asset_id   INTEGER REFERENCES assets(id) ON DELETE SET NULL,  -- 지금 그 번호의 주인
  kind       TEXT NOT NULL DEFAULT 'take',   -- swap(맞바꿈) / take(넘겨받음) / force(셋팅 강제매칭)
  reason     TEXT NOT NULL DEFAULT '',
  created_by TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tms_fix_no ON tms_number_fixes(asset_no);

-- TMS 판매 전표 이관(2026-08-04) — 개발사 연락두절로 TMS 데이터를 OWS로 옮기는 중이다.
-- ★OWS의 orders(고객 주문 1건)와 층위가 다르다. TMS 판매전표는 '하루치 채널별 묶음'이라
--   S260804-001 하나에 방문구매 26대가 들어 있다. 자산별 내역은 asset_events의 '판매'가 갖고,
--   이 표는 그 전표의 금액·정산(부가세·수수료·배송비·입금)을 갖는다.
-- 판매내역(전표 마스터)과 판매미수금관리(입금 정보)를 판매전표 번호로 합쳐 넣는다.
CREATE TABLE IF NOT EXISTS sale_slips (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  slip_no         TEXT NOT NULL UNIQUE,          -- 판매전표 (S260804-001)
  channel         TEXT NOT NULL DEFAULT '',      -- 판매채널
  customer        TEXT NOT NULL DEFAULT '',      -- 판매처명 / 거래처명
  memo            TEXT NOT NULL DEFAULT '',
  sale_date       TEXT NOT NULL DEFAULT '',
  ship_date       TEXT NOT NULL DEFAULT '',
  return_date     TEXT NOT NULL DEFAULT '',      -- 반입일
  waybill         TEXT NOT NULL DEFAULT '',
  -- ★TMS는 같은 금액을 두 벌로 들고 있다. 이관에서 하나만 골라 넣으면 데이터가 없어진다.
  --   전표 헤더(사람이 적은 총액) : 판매금액 / 순이익액 / 수량
  --   자산 명세 합계(붙은 자산들)  : 판매가   / 순이익   / 판매수량
  --   판매차이금액 = 판매금액 - 판매가 (421/421 정확히 일치 확인). 자산에 안 붙은 금액이다.
  --   S241126-001처럼 자산이 하나도 안 붙었는데 금액만 있는 전표가 있어 둘을 합칠 수 없다.
  qty             INTEGER NOT NULL DEFAULT 0,    -- 판매수량(명세)
  head_qty        INTEGER NOT NULL DEFAULT 0,    -- 수량(헤더)
  purchase_amount INTEGER NOT NULL DEFAULT 0,    -- 매입금액
  sale_amount     INTEGER NOT NULL DEFAULT 0,    -- 판매금액(헤더 총액)
  item_sale_sum   INTEGER NOT NULL DEFAULT 0,    -- 판매가(명세 합계)
  diff_amount     INTEGER NOT NULL DEFAULT 0,    -- 판매차이금액
  vat             INTEGER NOT NULL DEFAULT 0,
  fee             INTEGER NOT NULL DEFAULT 0,    -- 수수료
  cod             INTEGER NOT NULL DEFAULT 0,    -- 착불
  shipping        INTEGER NOT NULL DEFAULT 0,    -- 배송비
  profit          INTEGER NOT NULL DEFAULT 0,    -- 순이익액(헤더)
  item_profit     INTEGER NOT NULL DEFAULT 0,    -- 순이익(명세 합계)
  stage           TEXT NOT NULL DEFAULT '',      -- 진행상태 (판매완료/판매취소 등)
  paid_status     TEXT NOT NULL DEFAULT '',      -- 납부확인 (완납/선수금/중도금)
  paid_at         TEXT NOT NULL DEFAULT '',      -- 입금일시
  paid_amount     INTEGER NOT NULL DEFAULT 0,
  paid_confirmed  INTEGER NOT NULL DEFAULT 0,
  source          TEXT NOT NULL DEFAULT 'TMS이관',
  created_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL
);
-- TMS 자산 단위 판매(2026-08-25 대표) — 판매현황.xlsx(관리번호별) 원장.
-- sale_slips 가 '전표(하루치 묶음)'라면 여기는 '자산 한 대'다. 매칭 축은 자산번호.
-- ★주문은 만들지 않는다 — OWS 주문과 매칭된 자산은 order_assets 로 읽어서 표시만.
-- 업그레이드1/2·부가세 칸은 TMS 판매등록 탭에만 있어 크롤러 확장 후 채운다(지금은 0).
CREATE TABLE IF NOT EXISTS tms_sales (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  slip_no        TEXT NOT NULL DEFAULT '',
  asset_no       TEXT NOT NULL DEFAULT '',
  asset_id       INTEGER,                       -- 매칭된 OWS 자산(없을 수 있음)
  customer       TEXT NOT NULL DEFAULT '',      -- 수령자성함
  channel        TEXT NOT NULL DEFAULT '',
  seller         TEXT NOT NULL DEFAULT '',      -- 판매처명
  model          TEXT NOT NULL DEFAULT '',
  memo           TEXT NOT NULL DEFAULT '',      -- 판매상세비고
  sale_date      TEXT NOT NULL DEFAULT '',
  ship_date      TEXT NOT NULL DEFAULT '',
  return_date    TEXT NOT NULL DEFAULT '',
  purchase_price INTEGER NOT NULL DEFAULT 0,    -- TMS 매입가
  sale_price     INTEGER NOT NULL DEFAULT 0,    -- TMS 판매가
  tms_profit     INTEGER NOT NULL DEFAULT 0,    -- TMS 순이익(참고 — OWS 마진은 따로 계산)
  up1            INTEGER NOT NULL DEFAULT 0,    -- 업그레이드1 (판매등록 추출 전 0)
  up2            INTEGER NOT NULL DEFAULT 0,
  buy_vat        INTEGER NOT NULL DEFAULT 0,    -- 매입부가세(별도 표기분)
  sale_vat       INTEGER NOT NULL DEFAULT 0,
  grade          TEXT NOT NULL DEFAULT '',
  stage          TEXT NOT NULL DEFAULT '',      -- 진행상태(판매/판매취소)
  purchase_slip  TEXT NOT NULL DEFAULT '',
  supplier_name  TEXT NOT NULL DEFAULT '',
  created_at     TEXT NOT NULL,
  updated_at     TEXT NOT NULL DEFAULT '',
  UNIQUE(slip_no, asset_no)
);
CREATE INDEX IF NOT EXISTS idx_tms_sales_asset ON tms_sales(asset_no);
CREATE INDEX IF NOT EXISTS idx_tms_sales_date ON tms_sales(sale_date);

CREATE INDEX IF NOT EXISTS idx_sale_slips_date ON sale_slips(sale_date);
CREATE INDEX IF NOT EXISTS idx_sale_slips_channel ON sale_slips(channel);


-- ─────────────────────────────────────────────────────────────────────────
-- 사업부 자산 이관 원장 (2026-08-04 설계 / 2026-08-12 규칙 확정)
--
-- 2단계 커밋: 제안(pending) → 커밋(committed) → RMS 수신(done)
--   ★제안 단계에서는 아무것도 안 바뀐다. 차단 사유를 먼저 보여주기 위한 미리보기다.
--   ★commit_id 기준 멱등 — 같은 커밋을 두 번 적용해도 결과가 같아야 한다.
CREATE TABLE IF NOT EXISTS asset_transfers (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  commit_id      TEXT NOT NULL UNIQUE,             -- TRF-YYMMDD-NNNN
  direction      TEXT NOT NULL,                    -- rental->sale | sale->rental
  state          TEXT NOT NULL DEFAULT 'pending',  -- pending|committed|done|rejected|rolled_back|expired
  asset_count    INTEGER NOT NULL DEFAULT 0,
  reason         TEXT NOT NULL DEFAULT '',
  source         TEXT NOT NULL DEFAULT 'ows',      -- ows | rms (어느 화면에서 시작했나)
  requested_by   TEXT NOT NULL DEFAULT '',
  requested_at   TEXT NOT NULL DEFAULT '',
  committed_by   TEXT NOT NULL DEFAULT '',
  committed_at   TEXT NOT NULL DEFAULT '',
  ows_acked_at   TEXT NOT NULL DEFAULT '',
  rms_acked_at   TEXT NOT NULL DEFAULT '',
  rolled_back_at TEXT NOT NULL DEFAULT '',
  rolled_back_by TEXT NOT NULL DEFAULT '',
  snapshot       TEXT NOT NULL DEFAULT '',         -- 되돌리기 근거
  created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_transfers_state ON asset_transfers(state, direction);

CREATE TABLE IF NOT EXISTS asset_transfer_items (
  commit_id     TEXT NOT NULL,
  asset_no      TEXT NOT NULL,
  asset_id      INTEGER,
  result        TEXT NOT NULL DEFAULT 'pending',   -- ok | blocked | unmatched | noop
  block_note    TEXT NOT NULL DEFAULT '',
  prev_division TEXT NOT NULL DEFAULT '',
  prev_status   TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (commit_id, asset_no)
);
CREATE INDEX IF NOT EXISTS idx_transfer_items_no ON asset_transfer_items(asset_no);


-- RMS가 지금 들고 있는 관리번호 사본 (2026-08-24)
--
-- 왜 필요한가: OWS는 185에서 돌고 RMS DB는 240에 있어 파일로 읽을 수 없다.
--   그런데 "OWS는 렌탈이라는데 RMS엔 없는 자산"(실측 229대)을 매입 화면에서 찾으려면
--   RMS가 뭘 들고 있는지 알아야 한다. 그래서 RMS가 주기적으로 자기 목록을 밀어 넣는다.
-- ★사본일 뿐이다 — 판단 근거로만 쓰고, 이 표를 보고 자산을 바꾸지 않는다.
--   RMS가 한 번도 안 밀었으면 비어 있고, 그때는 'RMS에 없음' 판정을 하지 않는다.
CREATE TABLE IF NOT EXISTS rms_inventory (
  asset_no  TEXT PRIMARY KEY,
  status    TEXT NOT NULL DEFAULT '',      -- RMS 상태(available/rented/holding…)
  -- 임차인(2026-09-01) — 판매 건에는 '방문구매 · 참다슬'이 붙는데 렌탈 건에는
  -- 아무것도 없어 "출고완료인데 왜 기록이 없지"가 된다. 같은 모양으로 적어 준다.
  renter    TEXT NOT NULL DEFAULT '',
  -- 시리얼·모델(2026-09-03) — ★OWS는 185에서 돌아 240의 RMS DB 파일을 못 읽는다.
  --   그래서 OWS 화면에서 시작한 이관은 시리얼 대조가 통째로 빠진 채 진행됐다
  --   ("RMS 자산 데이터를 읽을 수 없습니다"라고 띄우면서도 그냥 넘어갔다).
  --   RMS가 사본을 밀 때 시리얼·모델을 같이 보내면 그 대조를 되살릴 수 있다.
  serial    TEXT NOT NULL DEFAULT '',
  model     TEXT NOT NULL DEFAULT '',
  synced_at TEXT NOT NULL
);

-- ─────────────────────────────────────────────────────────────────────────
-- 공용 마스터 (2026-09-02 대표 방침 "모든 데이터는 OWS·RMS에서 직접 등록·관리")
--
-- 모델 마스터: TMS HB_MST모델(784행)과 같은 축. OWS 화면에서 직접 등록하고, TMS 행은
--   연동 창구(data-bridge)에서 실시간 사본으로 들어온다(app/purchase/masters.py).
--   ★이름은 두 겹으로 유일하다 — 원문(name)과 정규화 키(norm_name: 공백 제거·대문자).
--     'NT 850XAC'와 'nt850xac'가 두 줄로 갈리면 세 시스템 모델명 불일치가 다시 생긴다.
--   source='tms'(연동 행) | 'ows'(OWS 등록 행). ows_edited_at 이 찍힌 행은 연동이 덮지 않는다.
--   TMS 삭제는 지우지 않고 tms_deleted_at 표시만 한다(사본에 원본 보존).
CREATE TABLE IF NOT EXISTS models (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  name           TEXT NOT NULL UNIQUE,
  norm_name      TEXT NOT NULL UNIQUE,
  brand          TEXT NOT NULL DEFAULT '',
  category       TEXT NOT NULL DEFAULT '',        -- 대분류(PC/모니터/태블릿/웨어러블)
  subcategory    TEXT NOT NULL DEFAULT '',        -- 중분류(노트북/데스크탑/…)
  pet_name       TEXT NOT NULL DEFAULT '',
  spec           TEXT NOT NULL DEFAULT '',        -- 모델사양(요약 문자열)
  memo           TEXT NOT NULL DEFAULT '',
  enabled        INTEGER NOT NULL DEFAULT 1,
  source         TEXT NOT NULL DEFAULT 'ows',     -- tms | ows
  tms_key_id     INTEGER,                         -- TMS 키ID(연동 행) — NULL 이면 OWS 전용
  tms_deleted_at TEXT NOT NULL DEFAULT '',
  ows_edited_at  TEXT NOT NULL DEFAULT '',        -- 사람이 OWS에서 고친 시각(연동 보호 표시)
  ows_edited_by  TEXT NOT NULL DEFAULT '',
  created_at     TEXT NOT NULL,
  updated_at     TEXT NOT NULL DEFAULT '',
  updated_by     TEXT NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_models_tms_key ON models(tms_key_id) WHERE tms_key_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_models_category ON models(category, subcategory);
-- 모델 분류(대분류·중분류) — TMS HB_MST모델분류(8행)와 같은 축. 모델 등록 화면의 선택지.
CREATE TABLE IF NOT EXISTS model_categories (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  category       TEXT NOT NULL,
  subcategory    TEXT NOT NULL DEFAULT '',
  sort           INTEGER NOT NULL DEFAULT 0,
  enabled        INTEGER NOT NULL DEFAULT 1,
  source         TEXT NOT NULL DEFAULT 'ows',
  tms_key_id     INTEGER,
  tms_deleted_at TEXT NOT NULL DEFAULT '',
  created_at     TEXT NOT NULL DEFAULT '',
  updated_at     TEXT NOT NULL DEFAULT '',
  UNIQUE(category, subcategory)
);
-- 거래처 ↔ TMS 신원(키ID) 연결 원장.
--   suppliers 는 name UNIQUE 라 한 이름에 한 행뿐인데, TMS MST거래처에는 같은 구분·같은 이름이
--   둘 이상인 곳이 있다(실측 2026-09-02: 업무 거래처 105곳 중 5그룹, 원문까지 동일).
--   ★자동으로 합치지 않는다 — 첫 신원이 대표(is_primary=1, suppliers.tms_key_id 와 같음)가 되고
--     나머지는 부 신원으로 여기 남아 화면에서 사람이 대표를 바꾸거나 별도 거래처로 분리한다.
--   payload 는 TMS 행의 원본 칸(JSON). deleted_at 은 TMS 업무 거래처 목록에서 빠졌다는 표시.
CREATE TABLE IF NOT EXISTS supplier_tms_links (
  tms_key_id  INTEGER PRIMARY KEY,
  supplier_id INTEGER NOT NULL REFERENCES suppliers(id),
  is_primary  INTEGER NOT NULL DEFAULT 0,
  kind        TEXT NOT NULL DEFAULT '',
  code        TEXT NOT NULL DEFAULT '',
  name        TEXT NOT NULL DEFAULT '',
  payload     TEXT NOT NULL DEFAULT '',
  deleted_at  TEXT NOT NULL DEFAULT '',
  seen_at     TEXT NOT NULL DEFAULT '',
  created_at  TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_supplier_links_supplier ON supplier_tms_links(supplier_id);

-- OWS 채번 순번(2026-09-03, A4 — app/purchase/numbering.py). 관리번호 자동 채번은 그날 OWS 대역(5000~)의
--   마지막 순번을 여기 남긴다 — 번호를 바꾸거나 넘겨줘 자산에서 비워진 번호도 다시 나가지 않는다
--   (A/S 문서번호 채번과 같은 '번호 재사용 금지' 원칙). 손으로 넣은 5000번대 번호도 반영된다(numbering._reserve).
CREATE TABLE IF NOT EXISTS asset_no_counters (
  day        TEXT PRIMARY KEY,               -- YYMMDD
  last_seq   INTEGER NOT NULL,
  updated_at TEXT NOT NULL
);

-- 창구 동시 마감 뒤 TMS 에 새로 들어온 전표·자산 — 이중입력 경보(2026-09-03, app/purchase/cutover.py, docs/CUTOVER_PLAN.md §5)
-- 연동 사본 HIS변경요약의 '추가' 중 마감일 이후 것만. his_key UNIQUE 라 재스캔해도 한 번만 남는다. 판정(status)은 사람.
CREATE TABLE IF NOT EXISTS tms_dual_entries (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  his_key     INTEGER NOT NULL UNIQUE,          -- HIS변경요약 키ID
  table_name  TEXT NOT NULL,                    -- HB_TBL판매H / HB_TBL매입H / HB_TBL재고
  kind        TEXT NOT NULL,                    -- 판매 / 매입 / 재고
  tms_key     INTEGER NOT NULL DEFAULT 0,       -- 원본 행 키ID
  slip_no     TEXT NOT NULL DEFAULT '',
  asset_no    TEXT NOT NULL DEFAULT '',         -- 재고 행이면 관리번호
  party       TEXT NOT NULL DEFAULT '',         -- 거래처명
  qty         INTEGER NOT NULL DEFAULT 0,
  amount      REAL NOT NULL DEFAULT 0,
  actor       TEXT NOT NULL DEFAULT '',         -- TMS 입력자(판매자/매입자)
  pattern     TEXT NOT NULL DEFAULT '',         -- '임시매입 의심' / '옛 전표에 추가'
  changed_at  TEXT NOT NULL,                    -- TMS 변경일시
  detected_at TEXT NOT NULL,
  status      TEXT NOT NULL DEFAULT 'open',     -- open / ok(정상·되돌리기·정리) / reentered(OWS 재입력 완료)
  note        TEXT NOT NULL DEFAULT '',
  resolved_at TEXT NOT NULL DEFAULT '',
  resolved_by TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_dual_entries_status ON tms_dual_entries(status, changed_at);

-- ★OWS → TMS 되돌려 쓰기 큐(2026-09-08 대표 "앞으로는 tms와 ows 그대로 연동해서 쓸거야").
--   OWS에서 값이 바뀌면 여기 한 줄이 쌓이고, 연동 틱이 창구(data-bridge)로 밀어 넣는다.
--   ★큐로 두는 이유: 창구가 죽어 있거나 아직 무장 전이어도 '바뀐 사실'이 사라지지 않는다.
--   ★applied 줄의 payload 는 'OWS가 TMS에 넣어 둔 값'이라 되돌아오는 고리를 끊는 기준이 된다
--     (tms_push.tms_own_repair — 이게 없으면 자산 원가가 두 배로 잡힌다).
CREATE TABLE IF NOT EXISTS tms_outbox (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  kind        TEXT NOT NULL,                    -- asset_cost (창구 tms_writer.WRITABLE 와 같은 이름)
  asset_id    INTEGER REFERENCES assets(id),
  asset_no    TEXT NOT NULL DEFAULT '',
  tms_key_id  INTEGER NOT NULL,                 -- TMS 원본 행 키ID — 이것으로만 찾아 고친다
  payload     TEXT NOT NULL DEFAULT '{}',       -- 보낼 값 {"수리비": 75000}
  reason      TEXT NOT NULL DEFAULT '',         -- 왜 바뀌었는지(A/S 수리비 반영 등)
  status      TEXT NOT NULL DEFAULT 'queued',   -- queued / applied / conflict / failed / cancelled
  attempts    INTEGER NOT NULL DEFAULT 0,
  last_error  TEXT NOT NULL DEFAULT '',
  action_id   TEXT NOT NULL DEFAULT '',         -- TMS 액션집합ID 도장(변경 이력에서 찾을 수 있게)
  before_json TEXT NOT NULL DEFAULT '',         -- 바꾸기 전 TMS 값 — 되돌릴 때 근거
  after_json  TEXT NOT NULL DEFAULT '',
  applied_at  TEXT NOT NULL DEFAULT '',
  created_by  TEXT NOT NULL DEFAULT '',
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tms_outbox_status ON tms_outbox(status, id);
CREATE INDEX IF NOT EXISTS idx_tms_outbox_asset ON tms_outbox(asset_id, status);
