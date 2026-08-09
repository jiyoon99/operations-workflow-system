# Halfbook Management System

중고 노트북/PC 유통 업무를 위한 사내 운영 관리 시스템입니다. 매입, 자산 관리, 주문 수집, QC, 송장 발급, A/S, 정산 리포트를 하나의 Flask 기반 웹 애플리케이션으로 통합했습니다.

## Portfolio Summary

- **Role**: Full-stack development, workflow design, data migration, API integration
- **Stack**: Python, Flask, SQLite WAL, Vanilla JavaScript, HTML/CSS, Waitress
- **Domain**: inventory, order operations, logistics, marketplace integration
- **Focus**: small-team operations, repeatable workflows, auditability, local-first deployment

## Key Features

- **Purchase and asset management**: purchase slips, asset numbers, categories, grade/tier, repair history, stock listing status
- **Order workflow**: manual and Excel import, marketplace API collection, deduplication, preparing/QC/shipping stages
- **Shipping and labels**: CJ Logistics integration, invoice issuing, waybill PDF generation, shipment tracking
- **Marketplace connectors**: Coupang, SmartStore, 11st, ESM, LotteON, Kakao Shopping, Toss Shopping, Temu, Godomall adapters
- **A/S operations**: return booking, repair progress, customer notification templates
- **Reporting**: sales, margin, purchase, stock, staff activity, setup/QC statistics
- **Admin controls**: account management, role permissions, category access, API key masking, audit log
- **Reliability**: SQLite WAL, transactional writes, scheduled backups, watchdog server process

## Architecture

```text
app/              Flask application and API blueprints
  auth/           Login, sessions, permissions
  purchase/       Purchase slips, assets, stock, migration helpers
  orders/         Order workflow, matching, waybill logic
  malls/          Marketplace API adapters and scheduler
  cj/             CJ Logistics client and label/PDF helpers
  settings/       Admin settings, QC import/watch tools
  reports/        Operational reporting APIs
static/           Single-page frontend using vanilla JS
tests/            Unit and workflow regression tests
scripts/          Maintenance and migration utilities
```

## Security And Data Handling

This repository is prepared as a **source-only portfolio copy**. Runtime data is intentionally excluded:

- SQLite databases
- customer/order exports
- backup files
- logs and PID files
- virtual environments
- `.env` files and local secrets

See [docs/GITHUB_UPLOAD_CHECKLIST.md](docs/GITHUB_UPLOAD_CHECKLIST.md) before publishing or deploying.

## Local Setup

```bat
python -m venv venv
venv\Scripts\pip install -r requirements.txt
venv\Scripts\python.exe run.py
```

Open:

```text
http://localhost:5100
```

Run tests:

```bat
venv\Scripts\python.exe -m unittest discover -s tests -v
```

## Notes

- The app is designed for Windows office environments and local/LAN deployment.
- Real marketplace keys, customer data, operational DB files, and exports are not included.
- Some background collectors are enabled by default in normal runtime and can be disabled with environment variables for testing.
