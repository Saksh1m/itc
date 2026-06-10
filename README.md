# Supra Network DMS

A runnable full-stack prototype for the Supra Network Distribution Management System described in `Supra_DMS_PRD.docx`.

## Stack

The implementation is dependency-free so it can run in this workspace without network installs:

- Python standard-library HTTP API
- SQLite database
- Static mobile-first PWA frontend
- Deterministic invoice splitting and ITC-style CSV export

There is no React/Next/FastAPI dependency in this runnable prototype because this workspace did not have those packages installed and network installs are restricted.

## Backend Data Storage

Backend data is stored in SQLite at:

```text
C:\Users\ADMIN\Documents\saksatcode\supra_dms.sqlite3
```

SQLite sidecar files such as `supra_dms.sqlite3-wal` and `supra_dms.sqlite3-shm` are normal runtime files created by SQLite WAL mode.

Initial master data is no longer embedded in Python code. Startup seed/sync data lives in:

- `data/seed_users.json`
- `data/seed_salesmen.json`
- `data/seed_products.json` contains the brand-name tally-pad order from the supplied yellow register image.
- `data/beat_master.csv` contains the ITC Beat Master rows used to seed invoice slots, Customer ID/Name, DS Code, Beat Code, and Beat Name for the ITC export.
- `data/product_master.csv` contains the ITC Product Master rows used to seed the 83 active products and calculate product price from `MRP / (UOM2 Conversion * 1000)`.
- `data/app_settings.json`

For production, replace these files with importers for the real ITC Beat Master and Product Master files. When demo salesmen omit Beat Master fields, startup fills deterministic placeholder Customer ID, DS Code, Beat Code, and Beat Name values so the ITC CSV keeps the required shape.

## Run

```powershell
python server.py
```

Then open:

```text
http://127.0.0.1:8000
```

## Demo Logins

The login form asks for username, password, and user type.

| Type | Username | Password |
| --- | --- | --- |
| Admin | `admin` | `admin123` |
| Supervisor | `supervisor` | `super123` |
| Salesman Profile 1 | `salesman1` | `sales123` |
| Salesman Profile 2 | `salesman2` | `sales456` |

The two salesman profiles can choose any billing-name salesman from `data/seed_salesmen.json` inside the app.

## User Types

- `salesman`: can choose a salesman from the A-X list, create/read/update/delete tally entries for that selected salesman, and manually generate that selected salesman's bill. The selected salesman cannot exceed their slot count; one generated bill uses one slot.
- `supervisor`: can open/end the day, view progress, generate invoices, and download the 13-column ITC `ORDER_IMPORT_DATA` CSV.
- `admin`: can do everything, including inventory setup, tally changes, invoice generation, CSV export, day-end, and audit log viewing.

## Concurrency

Concurrent salesman work is handled by:

- SQLite WAL mode for concurrent reads while writes are happening.
- A backend process write lock around API/database mutations so simultaneous tally writes are serialized safely.
- Database uniqueness constraints on `(session_id, salesman_id, product_id)` so repeated entries update one tally line instead of duplicating it.
- Slot validation after every tally write so a salesman cannot exceed the number of bill slots assigned in the salesman master.
- Generated invoices are cleared automatically when tally entries change, so the next CSV export regenerates from current data.

## Manual Billing Flow

1. Supervisor or admin opens the day's session.
2. Salesman profile logs in and selects a billing-name salesman from the A-X list.
3. Salesman enters quantities on the tally pad.
4. Salesman clicks `Generate bill`; the app allocates one or more bill slots for the selected salesman.
5. Supervisor downloads `ORDER_IMPORT_DATA_<date>.csv` from the ITC CSV export button.

## Implemented Scope

- Role-based login and API access
- One active dispatch session per date
- Opening inventory setup
- Warehouse tally pad with decimal M quantity entry and the supplied brand-name order
- Offline local queue with reconnect sync
- Inventory deduction and negative stock blocking
- Immutable append-only audit log
- Session dashboard and per-salesman completion state using the A-X salesman list from the supplied image
- Deterministic greedy invoice splitting under Rs. 49,999
- Highest-first invoice slot allocation
- 13-column ITC `ORDER_IMPORT_DATA` CSV export

This is a working Phase 1 prototype. Production deployment should swap SQLite for PostgreSQL, use HTTPS, add Redis-backed refresh tokens and slot locks, and seed real ITC Beat/Product Master data.
