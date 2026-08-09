-- HMS 스키마 (Phase 0: 계정/권한/카테고리/세션/감사로그/설정)
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

-- TMS 판매 전표 이관(2026-08-04) — 개발사 연락두절로 TMS 데이터를 HMS로 옮기는 중이다.
-- ★HMS의 orders(고객 주문 1건)와 층위가 다르다. TMS 판매전표는 '하루치 채널별 묶음'이라
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
CREATE INDEX IF NOT EXISTS idx_sale_slips_date ON sale_slips(sale_date);
CREATE INDEX IF NOT EXISTS idx_sale_slips_channel ON sale_slips(channel);
