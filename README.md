# Halfbook Management System

> 중고 노트북/PC 유통 업무를 위한 Flask 기반 사내 운영 관리 시스템입니다.

![실제 대시보드 화면](docs/assets/portfolio-real-dashboard.png)

## 프로젝트 개요

| 항목 | 내용 |
|---|---|
| 프로젝트 | Halfbook Management System |
| 역할 | 기획, 업무 흐름 설계, 백엔드/프론트엔드 구현, 데이터 이관, API 연동 |
| 기술 | Python, Flask, SQLite WAL, Vanilla JavaScript, HTML/CSS, Waitress |
| 목적 | 매입, 자산, 주문, QC, 송장, A/S, 정산 업무를 한 시스템에서 처리 |
| 배포 환경 | Windows 기반 로컬/LAN 사내 운영 환경 |

이 저장소는 실제 운영 데이터를 제외하고 정리한 **포트폴리오용 소스 공개 버전**입니다. 화면 이미지는 포트폴리오용 테스트 DB와 더미 계정으로 실행한 실제 애플리케이션 캡처입니다.

## 실제 화면

### 주문관리

![실제 주문관리 화면](docs/assets/portfolio-real-orders.png)

쇼핑몰별 주문을 입금대기, 준비중, 배송중, 배송완료, 취소 상태로 나누어 관리합니다. 엑셀 가져오기와 쇼핑몰 새로고침을 통해 주문을 수집하고, 수취인/상품/금액/작업 상태를 한 화면에서 확인할 수 있습니다.

### 매입

![실제 매입 화면](docs/assets/portfolio-real-purchase.png)

매입 전표, 자산, 기준정보를 분리해 관리합니다. 관리번호 규칙과 입고/재고 이력을 기준으로 중고 PC 재고 흐름을 추적할 수 있도록 구성했습니다.

### 설정

![실제 설정 화면](docs/assets/portfolio-real-settings.png)

사용자, 권한, API 관리, 재고연동, 정산, 데이터 이관, 감사 로그, 백업을 관리자 화면에서 관리합니다. 메뉴 접근 권한을 사용자별로 제한할 수 있습니다.

## 핵심 기능

- **매입/자산 관리**: 전표, 자산번호, 분류, 등급, 재고 구분, 수리 이력 관리
- **주문 관리**: 엑셀 주문 가져오기, 쇼핑몰 API 주문 수집, 중복 주문 제거
- **QC/출고 흐름**: 준비, 검수, 자산 매칭, 송장 발급, 출고 확정 단계 추적
- **배송 연동**: CJ대한통운 송장 발급, 운송장 PDF, 배송 추적
- **A/S 관리**: 접수, 회수 예약, 수리 진행, 고객 안내 템플릿
- **리포트**: 매출, 원가, 수수료, 마진, 직원별 작업량, 재고 통계
- **관리자 기능**: 계정, 권한, 담당 분류, API 키 마스킹, 감사 로그

## 외부 연동

쇼핑몰마다 인증 방식과 응답 구조가 달라서 어댑터 방식으로 분리했습니다.

- 쿠팡
- 스마트스토어
- 11번가
- ESM
- 롯데온
- 카카오쇼핑
- 토스쇼핑
- 테무
- 고도몰
- CJ대한통운

## 구조

```text
app/              Flask API와 업무 모듈
  auth/           로그인, 세션, 권한
  purchase/       매입, 자산, 재고, 데이터 이관
  orders/         주문, QC, 자산 매칭, 송장 처리
  malls/          쇼핑몰 API 어댑터와 자동 수집
  cj/             CJ대한통운 연동과 운송장 PDF
  settings/       관리자 설정, QC 이관/감시 도구
  reports/        운영 리포트
static/           Vanilla JS 기반 단일 페이지 화면
tests/            업무 흐름 회귀 테스트
scripts/          유지보수/마이그레이션 스크립트
```

## 실행 방법

```bat
python -m venv venv
venv\Scripts\pip install -r requirements.txt
venv\Scripts\python.exe run.py
```

접속 주소:

```text
http://localhost:5100
```

테스트:

```bat
venv\Scripts\python.exe -m unittest discover -s tests -v
```

## 보안 및 공개 범위

포트폴리오 공개를 위해 다음 항목은 저장소에 포함하지 않았습니다.

- 실제 운영 DB
- 고객/주문 엑셀 파일
- 백업 파일
- 로그 파일
- 가상환경
- `.env` 및 API 키

업로드 전 점검 기준은 [GitHub upload checklist](docs/GITHUB_UPLOAD_CHECKLIST.md)에 정리했습니다.

## 검증

- Python 3.12 가상환경에서 의존성 설치 확인
- `/api/health` 스모크 테스트 통과
- 실제 앱을 테스트 DB로 실행해 화면 캡처 생성
- 전체 테스트는 3분 제한까지 다수 통과했으나 완료 전 타임아웃
