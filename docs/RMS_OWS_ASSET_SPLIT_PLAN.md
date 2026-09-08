# 렌탈(RMS) ↔ 판매(OWS) 자산 분리 · 양방향 이관 설계안

작성 2026-08-04 / 실측 기준: `E:\rental-system\rental_system.db`, `E:\operations-system\data\ows.db`,
`E:\operations-system\tms-export\재고항목현황260804.xlsx` (모두 읽기 전용 조회)

---

## 1. RMS 자산 데이터 실측

### 1-1. 저장 구조 — OWS와 완전히 다르다

| | RMS | OWS |
|---|---|---|
| 테이블 | `assets(key TEXT PK, data TEXT, updated_at)` | `assets(id INTEGER PK, asset_no UNIQUE, …45개 컬럼)` |
| 형태 | **KV 1행 = JSON 자산 1건** | 정규화 컬럼 |
| 키 | `AST-0001` (내부 일련번호) | `id` / `asset_no` |
| TMS 관리번호 | JSON 안의 `mgmt_no` | `asset_no` (그 자체) |
| 행 수 | **1,338** (삭제 8 포함) | **15,066** |

RMS는 `load_assets()` → `db_load_kv('assets')`로 **전체를 통째로 읽고 통째로 저장**한다
(`app.py:807`, `app.py:828`). 컬럼 추가가 아니라 **JSON 키 추가**로 필드를 늘린다.
반대로 OWS는 `ALTER TABLE ADD COLUMN`이 필요하다. 이관 설계에서 이 비대칭이 핵심이다.

### 1-2. 관리번호 체계 — 두 종류가 섞여 있다

| 형식 | 건수 | 성격 |
|---|---|---|
| `YYMMDD-NNNN` (TMS형) | **1,280** | 매입팀이 TMS에서 채번한 진짜 공통키 |
| `YYYY-NNNN` / `YYYYMMDD-NNNN` / `1`, `2` 등 | **58** | RMS가 자체 채번(`next_mgmt_no()`, `app.py:838`) |

- 빈 관리번호 0건, 고유 관리번호 **1,336개**
- **중복 관리번호 2건**: `260408-0013`, `250509-0044` ← 이관 전 정리 필요
- 자체 채번 58건은 TMS에 없는 번호라 **공통키로 못 쓴다.** 시리얼 보조 매칭 대상.

### 1-3. 현재 보유 자산

| RMS status | 건수 | 의미 |
|---|---|---|
| `rented` | 1,070 | 고객이 쓰고 있음 |
| `available` | 168 | 창고 가용 |
| `holding` | 100 | 출고 전 예약 |
| `sold` | **0** | 스키마엔 있으나(`app.py:750`) 실제 사용 0 |

라인업: 게임용/기업용 512, 갤럭시탭S 200, 사무용 117, 휴대용 98, 전문가용 97, 갤럭시탭A 96, 워크스테이션 94 …

---

## 2. RMS ↔ OWS ↔ TMS 3자 대조 결과

### 2-1. 교집합

```
RMS 고유 관리번호 1,336  ∩  OWS asset_no 15,066  =  1,255 일치
  RMS에만 있음   81   (대부분 위 1-2의 자체 채번 58건 + 폐기/삭제분)
  OWS에만 있음  13,811 (순수 판매 자산)
```

### 2-2. 대표님이 말씀하신 592대 — 정확히 재현됨

TMS 재고항목현황 2,691건 기준:

| TMS 재고상태 | 건수 |
|---|---|
| 매입 | 1,075 |
| **렌탈** | **876** |
| **반납** | **545** |
| 반입 | 191 |
| 판매취소 | 4 |

렌탈+반납 **1,421건 → 전부 OWS에 존재**. 그 OWS 상태는:

| OWS status | 건수 |
|---|---|
| shipped | 829 |
| **in_stock** | **582** |
| **ready** | **10** |
| | **= 592 오염 확정** |

### 2-3. 오염 원인 — 추정이 아니라 코드에 있다

`app/purchase/migration.py:60~73` 의 `TMS_STATUS_MAP`:

```python
"매입": "ready",      # 판매 대기 재고
"렌탈": "shipped",    # 렌탈로 나가 있는 것 = 우리 손에 없다
"반납": "in_stock",   # ← ★렌탈에서 돌아온 것을 '입고'로 넣었다
"반입": "in_stock",
```

- `반납 → in_stock` 이 545대를 통째로 판매 재고로 밀어 넣었다 (실제 in_stock 527 + 기타).
- `렌탈 → shipped` 는 "우리 손에 없다"는 의도였지만, 결과적으로 **829대가 OWS에서 '판매 출고완료'로 보인다.**
- 근본 문제: **이관 시점에 사업부 구분 자체가 없었다.** 상태값만으로 소유를 표현하려 해서 실패.

교집합 1,255건 전부가 `purchase_batches.memo = '[TMS 이관]'` 배치 소속이다.
즉 OWS가 자기 매입으로 등록한 게 아니라 **7/30 벌크 유입분**이다.

### 2-4. 829대 shipped의 정체 — 판별됨

shipped 교집합 791건의 `asset_events`:

| action | 건수 |
|---|---|
| TMS이관 | 788 |
| TMS이관-보완 | 668 |
| **판매** | **18** |
| 이관등록 | 3 |

→ **판매 증거(판매전표 또는 OWS 주문 연결)가 있는 건 21건뿐.** 나머지 ~770대는
"렌탈 출고를 판매로 오인"한 게 맞다. RMS를 봐야 판별된다는 가설이 데이터로 확인됐다.

### 2-5. 판매가능 592대의 상태

- `stock_listed = 1` (몰 노출) : **0건** ← 다행히 아직 몰에 안 올라갔다
- tier: 실재고 260, 양품 204 / grade: 미정 260, A급 103, AS 33 …

몰 노출 전이라 **외부 판매 사고는 아직 없다.** 지금이 정리 적기.

---

## 3. 소유 판정 규칙 (제안)

### 3-1. 우선순위 규칙

| # | 규칙 | 판정 | 근거 |
|---|---|---|---|
| R1 | RMS `status ∈ (rented, holding)` | **rental** | 고객이 쓰고 있거나 나갈 예정 = 렌탈이 확실 |
| R2 | TMS 재고상태 ∈ (렌탈, 반납) | **rental** | 매입팀 원장이 렌탈로 찍음 |
| R3 | R1·R2 해당인데 OWS에 판매전표/주문/몰노출 있음 | **충돌 → 수동** | 양쪽이 다 소유 주장 |
| R4 | 위에 안 걸리고 OWS에 존재 | **sale** | 기본값 |
| R5 | RMS에만 있고 OWS에 없음 | **rental** | RMS 원장 그대로 유지 |
| R6 | RMS 보유 · 비대여 · TMS도 렌탈 아님 | **회색 → 대표 확인** | 22건, 전부 `available` |

**R1을 R2보다 위에 둔 이유**: TMS는 매입팀이 손으로 갱신해서 뒤처진다.
"고객이 지금 들고 있다"는 RMS 사실이 더 강하다.

### 3-2. 규칙 적용 시뮬레이션 (실측)

```
R1  RMS rented+holding            1,170
R2  TMS 렌탈/반납                 1,421
R1 ∪ R2 후보                      1,603
  − R3 판매증거 충돌 (수동판정)      13
  = rental 확정                    1,590
      OWS에도 있음                 1,513
      RMS에만 있음(R5)                77
R6  회색지대                          22   (전부 RMS available / TMS는 매입 17·없음 5)
```

**백필 효과:**

| | 현재 | 백필 후 |
|---|---|---|
| OWS 판매가능(in_stock+ready) | 1,929 | **1,241** |
| 빠지는 대수 | | **688** |

> 592가 아니라 **688**이다. 차이 96대는 TMS는 아직 '매입'으로 두고 있지만
> **RMS가 "지금 고객이 쓰고 있다"고 말하는 자산**이다(RMS rented × TMS 매입 90 + holding × 매입 26 등).
> TMS 원장만 봤으면 놓쳤을 96대다. → RMS 대조를 넣은 값어치가 여기서 나온다.

### 3-3. 수동 판정 필요 목록 (13건)

```
241216-0016  250102-0001  250102-0002  250102-0003  250102-0004
250102-0005  250107-0072  250116-0016  250121-0047  250225-0027
250314-0021  250509-0044  250925-0001
```

예: `241216-0016` — TMS 렌탈 / RMS 보유 / OWS엔 판매전표 `S250102-001` 박주환 390,000원 자사몰.
**실제로 팔린 건지, 렌탈 자산이 판매전표에 잘못 붙은 건지 대표님 확인이 필요합니다.**

### 3-4. 정리 선행 과제

- RMS 중복 관리번호 2건 (`260408-0013`, `250509-0044`) 해소
- RMS 자체 채번 58건 → TMS 번호로 교체하거나 "공통키 없음"으로 명시 표시

---

## 4. 사업부 귀속 필드 설계

### 4-1. OWS (원장 보유측)

```sql
ALTER TABLE assets ADD COLUMN division       TEXT NOT NULL DEFAULT 'sale';  -- 'sale' | 'rental'
ALTER TABLE assets ADD COLUMN division_since TEXT NOT NULL DEFAULT '';
ALTER TABLE assets ADD COLUMN division_by    TEXT NOT NULL DEFAULT '';
ALTER TABLE assets ADD COLUMN division_ref   TEXT NOT NULL DEFAULT '';      -- 이관 커밋 ID
CREATE INDEX idx_assets_division ON assets(division, status);
```

`DEFAULT 'sale'` 로 두면 기존 13,811대 순수 판매자산은 손 안 대고 그대로 정상이다.

### 4-2. RMS (JSON 키 추가)

```json
{ "division": "rental", "division_since": "...", "division_by": "...", "division_ref": "TRF-0001" }
```

`load_assets()`가 통째로 읽으므로 **마이그레이션 없이 키만 추가**하면 된다.
읽는 쪽은 `a.get('division') or 'rental'` (RMS 기본값은 rental).

### 4-3. 기존 `sold` 상태와의 관계 — 재활용 제안

RMS→OWS 이관 시 **`division='sale'` + `status='sold'` 를 함께 설정**할 것을 권합니다.

RMS 코드에 `sold` 자산 제외 로직이 이미 6곳 깔려 있습니다
(`app.py:1408` 대여후보 제외, `app.py:2658` 충돌검사, `app.py:2700` 경고, `app.py:13333` 통계 등).
`sold`를 같이 세우면 **"RMS에서 판매 제품이 보이면 안 된다"가 기존 로직만으로 즉시 충족**되고,
`division`은 그 위에서 "누구 소유인가"라는 별도 의미를 담습니다.

| | `status='sold'` | `division='sale'` |
|---|---|---|
| 뜻 | RMS 재고에서 빠짐 | 판매 사업부 소유 |
| 렌탈사업부가 고객에게 직접 매각 | ✅ | ❌ (여전히 rental) |
| 판매사업부로 이관 | ✅ | ✅ |

→ 둘을 구분해 두면 나중에 "렌탈이 직접 판 것"과 "판매팀에 넘긴 것"을 재무에서 갈라 볼 수 있습니다.

### 4-4. 이관 원장 테이블 (OWS에 신설)

```sql
CREATE TABLE asset_transfers (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  commit_id     TEXT NOT NULL UNIQUE,        -- TRF-20260804-0001
  direction     TEXT NOT NULL,               -- 'rental->sale' | 'sale->rental'
  state         TEXT NOT NULL DEFAULT 'pending',
                                             -- pending → committed → done
                                             -- (rejected / rolled_back)
  asset_count   INTEGER NOT NULL DEFAULT 0,
  reason        TEXT NOT NULL DEFAULT '',
  requested_by  TEXT NOT NULL DEFAULT '',  requested_at TEXT NOT NULL DEFAULT '',
  committed_by  TEXT NOT NULL DEFAULT '',  committed_at TEXT NOT NULL DEFAULT '',
  ows_acked_at  TEXT NOT NULL DEFAULT '',  rms_acked_at TEXT NOT NULL DEFAULT '',
  snapshot      TEXT NOT NULL DEFAULT ''     -- 이관 직전 양쪽 상태 JSON (롤백용)
);
CREATE TABLE asset_transfer_items (
  commit_id  TEXT NOT NULL,
  asset_no   TEXT NOT NULL,
  result     TEXT NOT NULL DEFAULT 'pending',  -- ok | blocked | unmatched
  block_note TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (commit_id, asset_no)
);
```

원장을 OWS에 두는 이유: 매입 시스템이 OWS에 붙어 있고, 매입이 두 시스템의 **중간 성격**이므로
자산 소유 이력의 단일 진실 원천을 OWS에 두는 게 구조와 맞습니다.

---

## 5. 양방향 이관 커밋 설계

### 5-1. 2단계 커밋 (제안 → 커밋 → 양측 ack)

```
① 제안   RMS/OWS 어느 쪽에서든 자산번호 목록 붙여넣기
         → POST /api/transfers  (OWS)
         → 가드 검사 후 state=pending, commit_id 발급 + 차단목록 즉시 반환
         ★ 이 단계에서는 아무것도 안 바뀐다 (미리보기)

② 커밋   대표 확인 후
         → POST /api/transfers/<commit_id>/commit  (OWS)
         → OWS 측 적용 + snapshot 저장, state=committed

③ ack    RMS가 미처리 커밋을 받아 자기 쪽 적용
         → GET  /api/transfers/pending-for-rms
         → POST /api/transfers/<commit_id>/ack
         → 양측 ack 완료 시 state=done
```

- **commit_id 기준 멱등**: 같은 커밋 두 번 적용해도 결과 동일 (중복발송 방지 락과 같은 방식)
- 한쪽만 적용되고 끊긴 커밋은 `state=committed`로 남아 **홈 화면에 "미완료 이관 N건"** 으로 표면화
- `snapshot`으로 커밋 단위 롤백 가능

### 5-2. 가드 (이게 핵심)

**rental → sale 차단 조건**
- RMS `status ∈ (rented, holding)` → **차단**. 고객이 들고 있는 물건은 못 넘긴다.
- RMS에 미등록 관리번호 → `unmatched`로 반환(조용히 통과 금지)

**sale → rental 차단 조건**
- OWS `stock_listed = 1` (몰 노출 중) → **차단**. 먼저 몰에서 내려야 한다.
- OWS `order_assets` 연결됨 / `status='shipped'` + 판매전표 존재 → **차단**
- OWS `status ∈ (repair, as, defective, painting)` → 경고 후 선택 통과

**공통**
- 이미 목표 division이면 no-op (`result='ok'`, 무변경)
- 진행 중인 다른 pending 커밋에 든 자산이면 차단(이중 이관 방지)

### 5-3. 사업부별 반영 내용

| | rental → sale | sale → rental |
|---|---|---|
| OWS | `division='sale'`, `status`는 검수 필요하니 `in_stock`, `stock_listed=0` | `division='rental'`, 판매가능 집계에서 제외 |
| RMS | `division='sale'` + `status='sold'` → 자산관리/대여후보에서 사라짐 | `division='rental'`, `status='available'` 로 복원 (없으면 신규 생성) |
| 이력 | 양쪽 `history` / `asset_events` 에 `이관(commit_id)` 기록 | 동일 |

### 5-4. 매입 유입 라우팅 (최종 목표)

매입 전표에서 자산 등록 시 **`division` 을 그 자리에서 지정**한다.

- 전표 단위 기본값 (`purchase_batches.division`) + 자산별 개별 지정 가능
- `division='rental'` 로 등록되면 → 그 즉시 OWS 판매재고 집계에서 빠지고
  RMS `pending-for-rms` 큐에 올라가 RMS가 자동 수령
- 기존 매입 흐름은 그대로. **컬럼 하나 + 등록 화면 라디오 하나**만 늘어난다.

### 5-5. 시스템 간 연결

같은 PC(5000 ↔ 5100)이므로 **HTTP + 공유 시크릿** 방식.

- 시크릿: 환경변수 `ASSET_TRANSFER_TOKEN` (양쪽 동일값), `X-Transfer-Token` 헤더
- RMS→OWS 호출은 `127.0.0.1:5100` 고정 (고정경로 방식 — A/S 문자 차단 때와 같은 패턴)
- **OWS가 죽어 있어도 RMS는 정상 동작**해야 함 → 호출 실패 시 로컬 큐에 적재 후 재시도,
  화면엔 "이관 대기 N건"으로만 표시 (동기 의존 금지)

---

## 6. 화면 설계 — 탭 안 늘립니다

> "TMS가 쓸데없이 화면을 여러 개로 나눠놔서 못 쓰겠다. TMS 화면 1개당 OWS 탭 1개씩 만들지 마라."

**신설 탭 0개.** 전부 기존 화면에 흡수합니다.

### OWS (매입 화면 탭은 현행 3개 그대로: 매입 작업 / 재고 현황 / 자산 목록)

| 위치 | 추가 | 방식 |
|---|---|---|
| 자산 목록 (`purchase.js:449`) | 사업부 필터 **셀렉트 1개** + 행에 `렌탈` 배지 | 기존 필터 줄에 끼움 |
| 자산 목록 | 여러 대 선택 → 기존 **일괄 변경 메뉴에 "사업부 이관" 항목 추가** | `purchase.js:371` 재사용 |
| 재고 현황 (`purchase.js:summary`) | KPI에 "렌탈 사업부 보유 N대(집계 제외)" **한 줄** | 카드 신설 없음 |
| 자산 상세 팝업 | 사업부 + 이관 이력 **2줄** | 기존 팝업 내부 |
| 홈 | 미완료 이관 있을 때만 **배너 1줄** | 조건부, 평소엔 안 보임 |

### RMS (자산관리 탭 `index.html:16423` 내부)

| 추가 | 방식 |
|---|---|
| `division='sale'` 자산 **목록에서 완전 제거** (기본) | 기존 `sold` 제외 로직 재사용 |
| "판매 이관분 보기" 토글 | 체크박스 1개, 기본 꺼짐 |
| 자산번호 붙여넣기 → 판매 이관 | 기존 `/api/assets/sell-by-number` (`app.py:14052`) **확장**. 새 화면 없음 |
| 이관 대기/실패 | 자산관리 상단 배너 1줄 |

### 이관 작업 자체

**전용 화면 안 만듭니다.** 양쪽 다 "자산번호 여러 개 붙여넣기 → 미리보기(차단 사유 포함) → 커밋"
**모달 하나**로 끝냅니다. RMS `sell-by-number`가 이미 붙여넣기·차단·미매칭 반환을 하고 있어서
그 UX를 그대로 씁니다.

---

## 7. 실행 계획

| 단계 | 내용 | 라이브 영향 |
|---|---|---|
| 0 | **백업** — OWS/RMS DB 각각, 건수로 확인 | 없음 |
| 1 | 사본 검증 환경: `OWS_DB` 로 사본 지정, **별도 포트**, 사본 전용 임시비번 로그인으로 사본임을 증명 | 없음 |
| 2 | 스키마 추가 (OWS 4컬럼 + 2테이블 / RMS JSON 키) — 사본에서 | 없음 |
| 3 | **백필 dry-run** — 판정 규칙 적용 결과표 출력 (1,590 rental / 688 판매재고 제외) | 없음 |
| 4 | **대표 확인**: 충돌 13건 + 회색지대 22건 | — |
| 5 | 이관 커밋 API + 가드 (사본에서 양방향 왕복 테스트) | 없음 |
| 6 | 화면 흡수 (위 6장) | 없음 |
| 7 | 라이브 백필 — 백업 직후, commit_id 부여해서 **통째로 롤백 가능**하게 | ★ |
| 8 | 매입 유입 라우팅 | ★ |

### 검증 항목 (사본에서 전부 통과 후 라이브)

- 백필 후 OWS 판매가능 1,929 → 1,241, `stock_listed=1` 인 rental 자산 **0건**
- 몰 재고 동기화(`stock_sync.py:35`)가 rental 자산을 **안 올림**
- RMS 자산관리·대여후보에 `division='sale'` **0건 노출**
- 왕복 테스트: rental → sale → rental 후 원상복구(스냅샷 일치)
- 같은 commit_id 2회 적용 시 무변경(멱등)
- 대여중 자산 이관 시도 → 전건 차단, DB 무변경

---

## 8. 대표 결정 사항 (2026-08-04)

### ✅ 결정 1 — 충돌 13건은 TMS 오배정. 예외 등록한다

근거: 판매전표 하나에 여러 사람이 서로 다른 자산번호로 붙어 있다.

```
250102-0001~0005  RMS 대여중(전부 서지아)  →  OWS 판매전표 S250102-001 하나에
                  윤동수·정민규·정수진·이재홍·이재만 5명이 각각 붙음
250121-0047  판매가 0원, 수령자 "이태연(교환)"
250314-0021  판매가 0원
```

정상 판매에서는 나올 수 없는 형태 → **TMS에서 관리번호가 잘못 배정된 것.**

처리:
- 13건 전부 `division='rental'` (R1 우선)
- `division_note` 에 무시한 판매전표를 **원문 그대로 보존**
  (예: `TMS 관리번호 오배정 — OWS 판매전표 S250102-001(윤동수 160,000) 무시. 2026-08-04 대표 확인`)
- `division_locked = 1` — **이후 자동 판정이 건드리지 않는다.** TMS 재이관해도 안 뒤집힘
- 판매전표 레코드 자체는 삭제하지 않음 (매출 대조 시 추적 가능해야 함)

### ✅ 결정 2 — 회색지대 22건 중 20건은 렌탈. 2건은 자산 아님

열어보니 물어볼 게 없었다. 전부 7월에 렌탈 고객에게서 돌아온 물건이고 TMS만 안 고친 것:

```
250513-0034~0120  NT371B5M A급 10대   2026-07-27 계약종료(방문 반납)
250519-0005~0051  NT371B5M B급  5대   2026-07-27 계약종료(방문 반납)
250321-0032, 250714-0045  갤럭시탭S 2대  장비 제거-가용 반환
250103-0005/0009, 250121-0033  ThinkPad 3대  계약종료(택배 회수)
```

→ **20건 `division='rental'`**

이관 대상 제외 2건 (자산이 아님, "정리 필요" 표시만):
- `테스트` — 모델명 "테스트장비입출고용", 시험용 더미
- `2026-0031` — RMS 자체채번, TMS·OWS 양쪽 다 없음

### ✅ 결정 3 — 중복 관리번호: 데이터 그대로 둔다

대표 지시: "중복등록 데이터로 남겨주면 자산번호를 바꾸면 되니 그대로 남겨줘."

실측 결과 **이미 한쪽씩 휴지통에 들어가 있다**:

| 관리번호 | 살아있음 | 휴지통 | 시리얼 |
|---|---|---|---|
| `260408-0013` | `AST-0414` rented·김동준 | `AST-0990` (7/28 삭제) | `R54T3005WWT` |
| `250509-0044` | `AST-1273` rented·유나영 | `AST-1274` (7/30 삭제) | `411NDBP35989` |

살아있는 자산 기준 **중복 0건 / 시리얼 중복 0종** → 이관에 영향 없음.
(`AST-0990` 이력에 `250408-0013`이 찍혀 있다 — `26`↔`25` 오타 흔적. 번호 정정 시 참고)

**이관 로직 규칙 (합치기·삭제 금지):**
- 휴지통(`deleted=true`) 자산은 이관·백필 대상에서 제외
- 살아있는 자산끼리 관리번호가 겹치면 → **이관 제외 + "중복" 표시만.**
  자동 병합·삭제 절대 안 함. 번호를 바꾸면 다음 이관부터 자동 편입

### ✅ 결정 4 — RMS `sold` 상태 재활용 (권장안 채택)

이관 시 `division='sale'` + `status='sold'` 동시 설정.
기존 sold 제외 로직 6곳(`app.py:1408/2658/2700/13333` 등)을 그대로 써서
"RMS에서 판매 제품 안 보임"을 새 코드 없이 충족한다.

---

### ✅ 결정 5 — 자체 채번분은 반납 시 채번 게이트로 해소

대표 지시: "자산번호 관리 이전에 나간 출고제품이라, **반납 시 필수로 자산번호 채번 후
반납처리** 될 수 있게 진행 예정."

실측 (휴지통 제외 **56건**):

| 상태 | 건수 | 처리 |
|---|---|---|
| `rented` | **54** | **반납 처리 시 채번 게이트** — TMS 관리번호 없이는 반납 완료 불가 |
| `available` | 2 | 이미 반납됨 → 지금 채번 필요 (별도 목록으로 표시) |

번호 형식: `YYYY-NNNN` 34 / `YYYYMMDD-NNNN` 15 / 기타 7
(기타에는 `1`~`5`, `20250101-000120250101-0001` 같은 손입력 사고도 있다)

**구현 (RMS):**
- 반납/회수 처리 시 `mgmt_no`가 TMS형(`^\d{6}-\d{4}$`)이 아니면 → **차단 + 채번 입력칸 노출**
- 입력받은 번호는 TMS형 검증 + RMS 내 중복 검사 후 저장, 기존 번호는 이력에 원문 보존
- 채번 완료 자산은 그 시점부터 자동으로 이관 대상에 편입
- 새 화면 없음. 기존 반납 모달 안에 입력칸 1줄

**이 게이트가 있으면 자체 채번 문제는 시간이 지나며 저절로 0이 된다.**
따라서 지금 일괄 소급 채번은 하지 않는다(대여 중인 물건은 실물 확인이 안 되므로).

### ✅ 결정 6 — 829대는 "상태를 되돌리는" 게 아니라 "라벨만 붙인다"

★"되돌리기"라는 표현이 오해를 불렀다. **OWS `status`는 건드리지 않는다.**

실측 (TMS 렌탈/반납 ∩ OWS shipped = **829대**):

| 항목 | 값 |
|---|---|
| TMS 내역 | 렌탈 811 / 반납 18 |
| 판매전표 붙음 | **4대** (`250116-0016`, `250121-0047`, `250225-0027`, `250925-0001`) — **전부 결정 1의 예외 13건 안에 이미 포함** |
| OWS 주문 연결 | **0** |
| 판매증거 전혀 없음 | **825대** |
| 몰 노출(`stock_listed=1`) | **0** |
| RMS에 존재 | 777대 (그중 `rented` **708** = 지금 고객이 쓰는 중) |

**핵심: 이 829대는 지금도 판매 재고를 부풀리고 있지 않다.**

- 판매가능 집계 = `status IN (in_stock, refurbishing, ready)` → **shipped 미포함**
- 몰 재고동기화(`stock_sync.py:35`) = `status IN (in_stock, ready)` → **shipped 미포함**

592대(in_stock/ready) 문제와 **성격이 다르다.** 829대의 문제는 수치가 아니라 **라벨**이다 —
렌탈로 나가 있는 물건이 화면에 "판매 출고완료"로 보인다.

**처리: `division='rental'`만 찍고 `status='shipped'`는 그대로 둔다.**

`shipped`의 뜻은 "OWS 손에 없다"이고, 렌탈로 나가 있는 것도 OWS 손에 없으니 **상태값 자체는 맞다.**
여기서 `in_stock`으로 "되돌리면" 오히려 "창고에 있다"가 되어
판매가능 재고가 1,970 → 2,799로 **늘어난다. 그게 진짜 사고다.**

| 백필 후 변화 | |
|---|---|
| 판매가능 재고(1,970) | **변화 없음** — 829대는 이미 집계 밖 |
| 몰 재고동기화 대상 | **변화 없음** |
| 판매 매출(421건 / 37억) | **변화 없음** — 판매전표 붙은 4대는 이미 예외 처리분 |
| 화면 | 자산 목록/상세에 `렌탈` 배지, 판매 리포트에서 제외 |

→ **재무 수치는 1원도 안 움직인다.** 라벨과 리포트 구분만 정확해진다.

나중에 이 자산이 실제로 반납되어 판매로 넘어올 때, **이관 커밋을 통해서만**
`division='sale'` + `status='in_stock'`으로 전환된다(검수 후 판매가능).

---

## 9. 구현 현황 (2026-08-04 · OWS 측 완료, 사본에서만)

### 작업 위치 — ★라이브와 분리했다

`E:\operations-system`은 **라이브 서버가 돌고 있는 바로 그 소스 트리**다. git도 없고
작업스케줄러에 `OperationsSystemAutoStart`(워치독)가 걸려 있어, 여기서 고치면
**라이브가 재시작되는 순간 작업 중인 코드가 그대로 운영에 올라간다.** 그래서 분리했다.

| | 경로 / 포트 |
|---|---|
| 라이브 (건드리지 않음) | `E:\operations-system` · `data\ows.db` · **5100** |
| 개발 트리 | `E:\operations-system-dev` |
| 사본 DB | `E:\operations-system-dev\data\ows-verify.db` |
| 검증 서버 | **127.0.0.1:5307** (`OWS_BACKUP_MIRROR=''`, `OWS_SMS_BLOCK=1`) |
| 사본 전용 계정 | `copycheck` — 라이브 users에는 0건(확인 완료) |

★사본 DB 파일명을 `ows.db`로 두면 dev 트리의 `LIVE_DB_PATH`와 같아져
`mirror_backup`의 '라이브인가' 판정을 통과해 `D:\ows-backups`를 오염시킨다.
그래서 `ows-verify.db`로 두고 미러도 껐다(이중 안전장치).

백업: `ows-20260804-203820-before-division.db` / `rms-20260804-203820-before-division.db`
— 둘 다 테이블별 건수 대조로 검증.

### OWS 구현 내용

**스키마** — `assets`에 `division`(기본 `sale`) `division_since` `division_by`
`division_ref` `division_note` `division_locked`, 인덱스 `(division, status)`.
원장 `asset_transfers` + `asset_transfer_items`.

**판매재고 격리** — `db.py`에 `sale_only()` 한 곳을 두고 다음 전부에 적용:

| 지점 | 파일 |
|---|---|
| ★몰 재고동기화(바깥으로 나가는 유일 경로) | `malls/stock_sync.py` |
| 재고 현황 집계 / 모델별 재고 / 모델 브리핑 / 최근매입 단가 | `purchase/__init__.py` |
| 제품코드 미입력 / 양품 전환 대상 | `purchase/__init__.py` |
| 셋팅 화면 재고·등급·tier 브리핑 4곳 | `orders/product_info.py` |
| 보유 재고 자산가치 / 장기 재고 | `reports/__init__.py` |
| ★주문 자산 매칭 차단 + 스캔 시 사유 표시 | `orders/__init__.py` |

스펙 자동완성(`spec_options.py`)과 A/S 자산 검색은 **일부러 제외했다** — 값 사전이고,
A/S는 렌탈 자산도 받는다.

**이관 API** (`purchase/transfers.py`, 매입 블루프린트에 부착 — 신설 없음)
`POST /api/transfers`(제안) · `/commit` · `/reject` · `/rollback` · `/ack` ·
`GET /transfers` · `/transfers/<cid>` · `/transfers/pending-for-rms` ·
`POST /api/assets/division-exception`(예외 등록·해제)

**화면 — 탭 신설 0개**
자산 목록 필터 줄에 사업부 셀렉트 1개 + 행에 `렌탈` 배지 / 일괄 변경 줄에 `↔ 사업부 이관`
버튼 하나(미리보기→커밋→되돌리기를 그 자리에서) / 재고 현황에 "렌탈 보유 N대 제외" 1줄 /
자산 상세에 사업부·이관 이력 / 엑셀 내보내기도 같은 조건.

### 백필 결과 (사본)

```
rental 확정 1,566건
  R1 RMS 대여중/예약   1,100      R2 TMS 렌탈/반납  433
  R3 예외(잠금)           13      R6 회색지대        20
제외: 자산 아님 2건(테스트, 2026-0031) · 중복 관리번호 0건
판매가능(in_stock+ready)  1,970 → 1,245   (725대 제외)
몰 노출된 렌탈 자산 0건
```

예측과 실측이 정확히 일치. 상태값은 결정 6대로 **건드리지 않았다**.

### 검증 — 46개 항목 전부 통과 (`tests/test_division_workflow.py`)

사본 증명 · 목록 필터 3종 + 400 · 예외 13건 잠금/사유 보존 · **제안만으로는 자산 무변경** ·
가드 6종(진행중 커밋/예외잠금/없는번호/방향오타/빈목록/취소한 제안) · 커밋(도착상태 `in_stock`,
몰노출 OFF, 커밋ID 기록) · **멱등**(커밋·롤백·ack·취소 각각 2회) · 주문 매칭 409 차단 ·
몰 재고 렌탈 0건 · 롤백 원상복구 · RMS 큐/ack/미완료 집계 · 예외 등록·해제

### 검증 중 잡은 실제 버그 2건

1. **커밋이 자기 제안을 이중 이관으로 보고 전부 차단** — `_inspect`의 중복 가드가
   자기 `commit_id`를 걸러내지 않았다. 이대로면 **어떤 이관도 커밋되지 않는다.**
   → `exclude_cid` 추가 + `result='ok'`인 항목만 중복으로 세도록 수정.
2. **미리보기만 하고 닫은 제안이 그 자산을 영구히 붙잡음** — 취소 경로가 없었다.
   → `POST /transfers/<cid>/reject` 추가.

### 남은 것

- **OWS 화면 육안 클릭 확인** — 세션 쿠키가 HttpOnly라 로그인 화면을 거쳐야 한다.
  비밀번호 입력은 대표가 직접(아래 10장).
- **RMS 측 구현** — RMS도 라이브(5000)와 같은 트리이므로 dev 트리 + 사본 DB 분리부터.
  JSON `division` 키, 판매 귀속분 숨김(`sold` 재활용), `sell-by-number` 확장,
  반납 채번 게이트(54대), OWS 큐 수신·ack.

## 10. 대표가 직접 해야 하는 것

검증 서버 **http://127.0.0.1:5307** 로그인 (사본 전용 계정 · 라이브에는 없는 계정)

```
아이디   copycheck
비밀번호 copy-only-20260804
```

로그인만 해 주시면 이후 화면 클릭 확인은 이어서 진행합니다.
(비밀번호 대리 입력은 하지 않습니다 — [[rental-system-e2e-login]]과 같은 방식)
