from __future__ import annotations

import base64
import csv
import hashlib
import hmac
import json
import mimetypes
import os
import secrets
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


ROOT = Path(__file__).parent
STATIC_DIR = ROOT / "static"
DATA_DIR = ROOT / "data"
DB_PATH = Path(os.environ.get("SUPRA_DMS_DB_PATH", ROOT / "supra_dms.sqlite3"))
BEAT_MASTER_PATH = DATA_DIR / "beat_master.csv"
PRODUCT_MASTER_PATH = DATA_DIR / "product_master.csv"
SECRET = os.environ.get("SUPRA_DMS_SECRET", "dev-secret-change-before-production").encode()
DB_LOCK = threading.RLock()


def load_json_file(name: str):
    with (DATA_DIR / name).open("r", encoding="utf-8") as handle:
        return json.load(handle)


SETTINGS = load_json_file("app_settings.json")
INVOICE_CAP = float(SETTINGS["invoice_cap"])
TOKEN_TTL_SECONDS = int(SETTINGS["token_ttl_seconds"])
DEFAULT_OPENING_STICKS = float(SETTINGS["default_opening_sticks"])
SALESMAN_PASSWORD_SUFFIX = str(SETTINGS["default_salesman_password_suffix"])


ROLE_PERMISSIONS = {
    "admin": {
        "open_session",
        "set_inventory",
        "enter_tally",
        "correct_entries",
        "generate_invoices",
        "download_csv",
        "end_session",
        "manage_masters",
        "view_audit",
        "view_reports",
    },
    "supervisor": {
        "open_session",
        "generate_invoices",
        "download_csv",
        "end_session",
        "view_reports",
    },
    "salesman": {"enter_tally", "generate_bill"},
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def today() -> str:
    return datetime.now().date().isoformat()


def next_date(date_text: str) -> str:
    return (datetime.fromisoformat(date_text).date() + timedelta(days=1)).isoformat()


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def rows_to_dicts(rows):
    return [dict(row) for row in rows]


def salesman_is_locked(conn: sqlite3.Connection, session_id: int, salesman_id: int) -> bool:
    return False


def visible_salesman_clause(user):
    if user["role"] == "salesman" and user["salesman_id"]:
        return " AND s.id = ?", (user["salesman_id"],)
    return "", ()


def hash_password(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 120_000)
    return f"{salt}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    salt, _ = stored.split("$", 1)
    return hmac.compare_digest(hash_password(password, salt), stored)


def sign(payload: dict) -> str:
    body = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).rstrip(b"=")
    sig = hmac.new(SECRET, body, hashlib.sha256).digest()
    return f"{body.decode()}.{base64.urlsafe_b64encode(sig).rstrip(b'=').decode()}"


def unsign(token: str) -> dict | None:
    try:
        body, sig = token.split(".", 1)
        expected = base64.urlsafe_b64encode(hmac.new(SECRET, body.encode(), hashlib.sha256).digest()).rstrip(b"=")
        if not hmac.compare_digest(sig.encode(), expected):
            return None
        padded = body + "=" * (-len(body) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode()))
        if payload.get("exp", 0) < int(time.time()):
            return None
        return payload
    except Exception:
        return None


def audit(conn, user_id, event_type, entity_type, entity_id, old_value=None, new_value=None, ip=""):
    conn.execute(
        """
        INSERT INTO audit_log(user_id, event_type, entity_type, entity_id, old_value, new_value, ip_address, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            user_id,
            event_type,
            entity_type,
            str(entity_id),
            json.dumps(old_value, sort_keys=True) if old_value is not None else None,
            json.dumps(new_value, sort_keys=True) if new_value is not None else None,
            ip,
            utc_now(),
        ),
    )


def init_db():
    with DB_LOCK, connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                pin_hash TEXT,
                salesman_id INTEGER REFERENCES salesmen(id),
                role TEXT NOT NULL,
                failed_attempts INTEGER NOT NULL DEFAULT 0,
                locked_until INTEGER NOT NULL DEFAULT 0,
                active INTEGER NOT NULL DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS salesmen (
                id INTEGER PRIMARY KEY,
                billing_code TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL,
                ds_code TEXT NOT NULL,
                beat_code TEXT NOT NULL,
                beat_name TEXT NOT NULL,
                customer_id TEXT NOT NULL,
                customer_name TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY,
                item_code TEXT UNIQUE NOT NULL,
                item_description TEXT NOT NULL,
                tally_sort_order INTEGER NOT NULL,
                pack_size INTEGER NOT NULL,
                ptr_per_pac NUMERIC NOT NULL,
                mrp_per_pac NUMERIC NOT NULL DEFAULT 0,
                uom2 NUMERIC NOT NULL,
                price_per_stick NUMERIC NOT NULL,
                active INTEGER NOT NULL DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS invoice_slots (
                id INTEGER PRIMARY KEY,
                salesman_id INTEGER NOT NULL REFERENCES salesmen(id),
                slot_number INTEGER NOT NULL,
                slot_code TEXT NOT NULL,
                ds_code TEXT NOT NULL DEFAULT '',
                ds_name TEXT NOT NULL DEFAULT '',
                beat_code TEXT NOT NULL DEFAULT '',
                beat_name TEXT NOT NULL DEFAULT '',
                customer_id TEXT NOT NULL DEFAULT '',
                customer_name TEXT NOT NULL DEFAULT '',
                UNIQUE(salesman_id, slot_number)
            );

            CREATE TABLE IF NOT EXISTS daily_sessions (
                id INTEGER PRIMARY KEY,
                session_date TEXT UNIQUE NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('open', 'locked', 'csv_generated', 'uploaded')),
                opened_by INTEGER REFERENCES users(id),
                locked_by INTEGER REFERENCES users(id),
                created_at TEXT NOT NULL,
                locked_at TEXT
            );

            CREATE TABLE IF NOT EXISTS inventory (
                session_id INTEGER NOT NULL REFERENCES daily_sessions(id),
                product_id INTEGER NOT NULL REFERENCES products(id),
                opening_sticks NUMERIC NOT NULL DEFAULT 0,
                sticks_in_stock NUMERIC NOT NULL DEFAULT 0,
                PRIMARY KEY(session_id, product_id)
            );

            CREATE TABLE IF NOT EXISTS session_salesman_locks (
                session_id INTEGER NOT NULL REFERENCES daily_sessions(id),
                salesman_id INTEGER NOT NULL REFERENCES salesmen(id),
                locked_by INTEGER NOT NULL REFERENCES users(id),
                locked_at TEXT NOT NULL,
                reason TEXT,
                PRIMARY KEY(session_id, salesman_id)
            );

            CREATE TABLE IF NOT EXISTS tally_entries (
                id INTEGER PRIMARY KEY,
                session_id INTEGER NOT NULL REFERENCES daily_sessions(id),
                salesman_id INTEGER NOT NULL REFERENCES salesmen(id),
                product_id INTEGER NOT NULL REFERENCES products(id),
                quantity_m NUMERIC NOT NULL,
                sticks NUMERIC NOT NULL,
                line_value NUMERIC NOT NULL,
                created_by INTEGER REFERENCES users(id),
                updated_by INTEGER REFERENCES users(id),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(session_id, salesman_id, product_id)
            );

            CREATE TABLE IF NOT EXISTS invoice_allocations (
                id INTEGER PRIMARY KEY,
                session_id INTEGER NOT NULL REFERENCES daily_sessions(id),
                salesman_id INTEGER NOT NULL REFERENCES salesmen(id),
                slot_id INTEGER NOT NULL REFERENCES invoice_slots(id),
                invoice_no TEXT NOT NULL,
                total_value NUMERIC NOT NULL,
                lines_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(session_id, slot_id)
            );

            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY,
                user_id INTEGER,
                event_type TEXT NOT NULL,
                entity_type TEXT NOT NULL,
                entity_id TEXT NOT NULL,
                old_value TEXT,
                new_value TEXT,
                ip_address TEXT,
                created_at TEXT NOT NULL
            );
            """
        )
        migrate(conn)
        seed(conn)


def migrate(conn: sqlite3.Connection):
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
    if "salesman_id" not in columns:
        conn.execute("ALTER TABLE users ADD COLUMN salesman_id INTEGER REFERENCES salesmen(id)")
    slot_columns = {row["name"] for row in conn.execute("PRAGMA table_info(invoice_slots)").fetchall()}
    for column in ["ds_code", "ds_name", "beat_code", "beat_name", "customer_id", "customer_name"]:
        if column not in slot_columns:
            conn.execute(f"ALTER TABLE invoice_slots ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")
    product_columns = {row["name"] for row in conn.execute("PRAGMA table_info(products)").fetchall()}
    if "mrp_per_pac" not in product_columns:
        conn.execute("ALTER TABLE products ADD COLUMN mrp_per_pac NUMERIC NOT NULL DEFAULT 0")


def seed(conn: sqlite3.Connection):
    sync_salesmen(conn, load_json_file("seed_salesmen.json"))
    sync_users(conn, load_json_file("seed_users.json"))
    sync_products(conn, load_json_file("seed_products.json"))
    if PRODUCT_MASTER_PATH.exists():
        sync_product_master(conn, PRODUCT_MASTER_PATH)
    if BEAT_MASTER_PATH.exists():
        sync_beat_master(conn, BEAT_MASTER_PATH)
    clear_operational_locks(conn)
    for session in conn.execute("SELECT id FROM daily_sessions").fetchall():
        ensure_inventory(conn, session["id"])


def clear_operational_locks(conn: sqlite3.Connection):
    conn.execute("DELETE FROM session_salesman_locks")
    conn.execute(
        """
        UPDATE daily_sessions
        SET status = 'open', locked_by = NULL, locked_at = NULL
        WHERE status IN ('locked', 'csv_generated')
        """
    )


def sync_users(conn: sqlite3.Connection, users: list[dict]):
    active_usernames = {user["username"] for user in users}
    for user in users:
        existing = conn.execute("SELECT id FROM users WHERE username = ?", (user["username"],)).fetchone()
        if not existing:
            conn.execute(
                "INSERT INTO users(username, password_hash, role, salesman_id, active) VALUES (?, ?, ?, NULL, 1)",
                (user["username"], hash_password(user["password"]), user["role"]),
            )
        else:
            conn.execute(
                "UPDATE users SET role = ?, salesman_id = NULL, active = 1 WHERE username = ?",
                (user["role"], user["username"]),
            )
    if active_usernames:
        placeholders = ",".join("?" for _ in active_usernames)
        conn.execute(
            f"UPDATE users SET active = 0 WHERE username NOT IN ({placeholders})",
            tuple(sorted(active_usernames)),
        )


def sync_salesmen(conn: sqlite3.Connection, salesmen: list[dict]):
    active_codes = {item["billing_code"] for item in salesmen}
    for item in salesmen:
        code = item["billing_code"]
        code_index = ord(code.upper()) - ord("A") + 1 if len(code) == 1 and code.isalpha() else 0
        demo_customer_id = f"118EXP2020CU{code_index:04d}" if code_index else f"118EXP2020CU{code}"
        demo_ds_code = f"118{code_index:02d}" if code_index else f"118{code}"
        demo_beat_code = str(code_index) if code_index else code
        demo_beat_name = f"SUPRA NETWORK BEAT {code}"
        values = (
            item["name"],
            item.get("ds_code") or demo_ds_code,
            item.get("beat_code") or demo_beat_code,
            item.get("beat_name") or demo_beat_name,
            item.get("customer_id") or demo_customer_id,
            item.get("customer_name", item["name"]),
            code,
        )
        existing = conn.execute("SELECT id FROM salesmen WHERE billing_code = ?", (code,)).fetchone()
        if existing:
            salesman_id = existing["id"]
            conn.execute(
                """
                UPDATE salesmen
                SET name = ?, ds_code = ?, beat_code = ?, beat_name = ?, customer_id = ?, customer_name = ?, active = 1
                WHERE billing_code = ?
                """,
                values,
            )
        else:
            cur = conn.execute(
                """
                INSERT INTO salesmen(billing_code, name, ds_code, beat_code, beat_name, customer_id, customer_name)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    code,
                    item["name"],
                    item.get("ds_code") or demo_ds_code,
                    item.get("beat_code") or demo_beat_code,
                    item.get("beat_name") or demo_beat_name,
                    item.get("customer_id") or demo_customer_id,
                    item.get("customer_name", item["name"]),
                ),
            )
            salesman_id = cur.lastrowid
        sync_invoice_slots(conn, salesman_id, code, int(item["slot_count"]))
    if active_codes:
        placeholders = ",".join("?" for _ in active_codes)
        conn.execute(f"UPDATE salesmen SET active = 0 WHERE billing_code NOT IN ({placeholders})", tuple(sorted(active_codes)))


def sync_invoice_slots(conn: sqlite3.Connection, salesman_id: int, billing_code: str, slot_count: int):
    for slot in range(1, slot_count + 1):
        conn.execute(
            """
            INSERT INTO invoice_slots(salesman_id, slot_number, slot_code)
            VALUES (?, ?, ?)
            ON CONFLICT(salesman_id, slot_number) DO UPDATE SET slot_code = excluded.slot_code
            """,
            (salesman_id, slot, f"{billing_code}-{slot}"),
        )


def load_beat_master(path: Path) -> list[dict]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.reader(handle))
    header_index = next((idx for idx, row in enumerate(rows) if row and row[0].strip() == "DS Code"), None)
    if header_index is None:
        return []
    header = [cell.strip() for cell in rows[header_index]]
    records = []
    for row in rows[header_index + 1 :]:
        if not row or not row[0].strip():
            continue
        padded = row + [""] * (len(header) - len(row))
        records.append({key: value.strip() for key, value in zip(header, padded)})
    return records


def parse_beat_slot_number(billing_code: str, customer_name: str) -> int | None:
    name = customer_name.strip()
    prefix = billing_code.upper()
    if name.upper().startswith(prefix):
        rest = name[len(prefix) :].lstrip(" -")
        digits = []
        for char in rest:
            if char.isdigit():
                digits.append(char)
            else:
                break
        if digits:
            return int("".join(digits))
    digits = []
    for char in name:
        if char.isdigit():
            digits.append(char)
        else:
            break
    return int("".join(digits)) if digits else None


def sync_beat_master(conn: sqlite3.Connection, path: Path):
    records = load_beat_master(path)
    if not records:
        return
    active_salesmen = rows_to_dicts(conn.execute("SELECT id, billing_code FROM salesmen WHERE active = 1").fetchall())
    rows_by_code = {}
    for row in records:
        if row.get("DS Status", "").lower() != "active" or row.get("Customer Status", "").lower() != "active":
            continue
        rows_by_code.setdefault(row.get("DS Name", "").strip(), []).append(row)

    for salesman in active_salesmen:
        code = salesman["billing_code"]
        beat_rows = rows_by_code.get(code, [])
        if not beat_rows:
            continue

        assigned = set()
        slot_rows = []
        for idx, row in enumerate(beat_rows, start=1):
            slot_number = parse_beat_slot_number(code, row.get("Customer Name", ""))
            if slot_number in assigned:
                slot_number = None
            if slot_number:
                assigned.add(slot_number)
            slot_rows.append([slot_number, idx, row])

        next_slot = 1
        for item in slot_rows:
            if item[0]:
                continue
            while next_slot in assigned:
                next_slot += 1
            item[0] = next_slot
            assigned.add(next_slot)

        for slot_number, _, row in sorted(slot_rows, key=lambda item: item[0]):
            conn.execute(
                """
                INSERT INTO invoice_slots(
                    salesman_id, slot_number, slot_code, ds_code, ds_name, beat_code, beat_name, customer_id, customer_name
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(salesman_id, slot_number) DO UPDATE SET
                    slot_code = excluded.slot_code,
                    ds_code = excluded.ds_code,
                    ds_name = excluded.ds_name,
                    beat_code = excluded.beat_code,
                    beat_name = excluded.beat_name,
                    customer_id = excluded.customer_id,
                    customer_name = excluded.customer_name
                """,
                (
                    salesman["id"],
                    slot_number,
                    f"{code}-{slot_number}",
                    row.get("DS Code", ""),
                    row.get("DS Name", ""),
                    row.get("Beat Code", ""),
                    row.get("Beat Name", ""),
                    row.get("Customer Code", ""),
                    row.get("Customer Name", ""),
                ),
            )

        conn.execute(
            """
            DELETE FROM invoice_slots
            WHERE salesman_id = ?
              AND slot_number NOT IN (%s)
              AND NOT EXISTS (
                  SELECT 1 FROM invoice_allocations ia WHERE ia.slot_id = invoice_slots.id
              )
            """
            % ",".join("?" for _ in assigned),
            (salesman["id"], *sorted(assigned)),
        )


def sync_products(conn: sqlite3.Connection, products: list[dict]):
    active_codes = {item["item_code"] for item in products}
    for idx, item in enumerate(products, start=1):
        price_per_stick = float(item["ptr_per_pac"]) / (float(item["uom2"]) * 1000)
        conn.execute(
            """
            INSERT INTO products(item_code, item_description, tally_sort_order, pack_size, ptr_per_pac, mrp_per_pac, uom2, price_per_stick, active)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(item_code) DO UPDATE SET
                item_description = excluded.item_description,
                tally_sort_order = excluded.tally_sort_order,
                pack_size = excluded.pack_size,
                ptr_per_pac = excluded.ptr_per_pac,
                mrp_per_pac = excluded.mrp_per_pac,
                uom2 = excluded.uom2,
                price_per_stick = excluded.price_per_stick,
                active = 1
            """,
            (
                item["item_code"],
                item["item_description"],
                item.get("tally_sort_order", idx),
                item["pack_size"],
                item["ptr_per_pac"],
                item.get("mrp_per_pac", item["ptr_per_pac"]),
                item["uom2"],
                price_per_stick,
            ),
        )
    if active_codes:
        placeholders = ",".join("?" for _ in active_codes)
        conn.execute(f"UPDATE products SET active = 0 WHERE item_code NOT IN ({placeholders})", tuple(sorted(active_codes)))


def parse_master_number(value, default=0.0) -> float:
    text = str(value or "").strip().replace(",", "")
    if not text:
        return default
    return float(text)


def load_product_master(path: Path) -> list[dict]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.reader(handle))
    header_index = next((idx for idx, row in enumerate(rows) if row and row[0].strip() == "SBU"), None)
    if header_index is None:
        return []
    header = [cell.strip() for cell in rows[header_index]]
    records = []
    for row in rows[header_index + 1 :]:
        if not row or not row[0].strip():
            continue
        padded = row + [""] * (len(header) - len(row))
        records.append({key: value.strip() for key, value in zip(header, padded)})
    return records


def sync_product_master(conn: sqlite3.Connection, path: Path):
    records = [
        row
        for row in load_product_master(path)
        if row.get("Item Status", "").strip().lower() == "active"
    ]
    active_codes = set()
    for idx, row in enumerate(records, start=1):
        item_code = row.get("Item Code", "").strip()
        if not item_code:
            continue
        mrp = parse_master_number(row.get("MRP"))
        ptr = parse_master_number(row.get("PTR per PAC"))
        uom2 = parse_master_number(row.get("UOM2 Conversion"))
        if not mrp or not uom2:
            continue
        pack_size = int(round(uom2 * 1000))
        price_per_stick = mrp / (uom2 * 1000)
        active_codes.add(item_code)
        conn.execute(
            """
            INSERT INTO products(item_code, item_description, tally_sort_order, pack_size, ptr_per_pac, mrp_per_pac, uom2, price_per_stick, active)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(item_code) DO UPDATE SET
                item_description = excluded.item_description,
                tally_sort_order = excluded.tally_sort_order,
                pack_size = excluded.pack_size,
                ptr_per_pac = excluded.ptr_per_pac,
                mrp_per_pac = excluded.mrp_per_pac,
                uom2 = excluded.uom2,
                price_per_stick = excluded.price_per_stick,
                active = 1
            """,
            (
                item_code,
                row.get("Item Description", "").strip(),
                idx,
                pack_size,
                ptr,
                mrp,
                uom2,
                price_per_stick,
            ),
        )
    if active_codes:
        placeholders = ",".join("?" for _ in active_codes)
        conn.execute(f"UPDATE products SET active = 0 WHERE item_code NOT IN ({placeholders})", tuple(sorted(active_codes)))


def require_permission(user, permission):
    if permission not in ROLE_PERMISSIONS.get(user["role"], set()):
        raise ApiError(HTTPStatus.FORBIDDEN, "Forbidden", "Your role cannot perform this action.")


class ApiError(Exception):
    def __init__(self, status, code, message):
        self.status = status
        self.code = code
        self.message = message


def parse_quantity(value) -> float:
    try:
        qty = float(str(value).strip())
    except Exception:
        raise ApiError(HTTPStatus.BAD_REQUEST, "InvalidQuantity", "Enter a number like 0.2 or 1.5")
    if qty <= 0 or qty > 999:
        raise ApiError(HTTPStatus.BAD_REQUEST, "InvalidQuantity", "Enter a number like 0.2 or 1.5")
    return round(qty, 3)


def get_session(conn, session_id):
    row = conn.execute("SELECT * FROM daily_sessions WHERE id = ?", (session_id,)).fetchone()
    if not row:
        raise ApiError(HTTPStatus.NOT_FOUND, "NotFound", "Session not found.")
    return row


def get_active_session(conn: sqlite3.Connection):
    return conn.execute(
        """
        SELECT *
        FROM daily_sessions
        WHERE status <> 'uploaded'
        ORDER BY session_date DESC, id DESC
        LIMIT 1
        """
    ).fetchone()


def create_or_get_session(conn: sqlite3.Connection, session_date: str, user_id: int, ip: str, advance_closed: bool = False):
    date_text = session_date
    while True:
        existing = conn.execute("SELECT * FROM daily_sessions WHERE session_date = ?", (date_text,)).fetchone()
        if not existing:
            cur = conn.execute(
                "INSERT INTO daily_sessions(session_date, status, opened_by, created_at) VALUES (?, 'open', ?, ?)",
                (date_text, user_id, utc_now()),
            )
            ensure_inventory(conn, cur.lastrowid)
            audit(conn, user_id, "SESSION_OPENED", "daily_sessions", cur.lastrowid, None, {"session_date": date_text}, ip)
            return get_session(conn, cur.lastrowid), True
        if existing["status"] != "uploaded":
            ensure_inventory(conn, existing["id"])
            return existing, False
        if not advance_closed:
            raise ApiError(
                HTTPStatus.CONFLICT,
                "SessionAlreadyClosed",
                f"The {date_text} session is closed. Open the next day session instead.",
            )
        date_text = next_date(date_text)


def get_product(conn, product_id):
    row = conn.execute("SELECT * FROM products WHERE id = ? AND active = 1", (product_id,)).fetchone()
    if not row:
        raise ApiError(HTTPStatus.NOT_FOUND, "NotFound", "Product not found.")
    return row


def ensure_inventory(conn, session_id):
    products = conn.execute("SELECT id FROM products WHERE active = 1").fetchall()
    for product in products:
        conn.execute(
            """
            INSERT OR IGNORE INTO inventory(session_id, product_id, opening_sticks, sticks_in_stock)
            VALUES (?, ?, ?, ?)
            """,
            (session_id, product["id"], DEFAULT_OPENING_STICKS, DEFAULT_OPENING_STICKS),
        )


def session_summary(conn, session_id, user=None):
    products = rows_to_dicts(conn.execute("SELECT * FROM products WHERE active = 1 ORDER BY tally_sort_order").fetchall())
    salesman_filter = ""
    params = [session_id]
    salesmen_params = []
    if user and user["role"] == "salesman" and user["salesman_id"]:
        salesman_filter = " AND s.id = ?"
        params.append(user["salesman_id"])
        salesmen_params.append(user["salesman_id"])
    salesmen = rows_to_dicts(
        conn.execute(
            f"SELECT s.* FROM salesmen s WHERE s.active = 1{salesman_filter} ORDER BY s.billing_code",
            salesmen_params,
        ).fetchall()
    )
    entries = rows_to_dicts(
        conn.execute(
            f"""
            SELECT te.*, p.item_description, p.item_code, s.billing_code, s.name AS salesman_name
            FROM tally_entries te
            JOIN products p ON p.id = te.product_id
            JOIN salesmen s ON s.id = te.salesman_id
            WHERE te.session_id = ?
              AND (
                  (SELECT MAX(ia.created_at) FROM invoice_allocations ia WHERE ia.session_id = te.session_id AND ia.salesman_id = te.salesman_id) IS NULL
                  OR te.updated_at > (SELECT MAX(ia.created_at) FROM invoice_allocations ia WHERE ia.session_id = te.session_id AND ia.salesman_id = te.salesman_id)
              )
            {salesman_filter}
            ORDER BY s.billing_code, p.tally_sort_order
            """,
            params,
        ).fetchall()
    )
    allocation_params = [session_id]
    allocation_filter = ""
    if user and user["role"] == "salesman" and user["salesman_id"]:
        allocation_filter = " AND ia.salesman_id = ?"
        allocation_params.append(user["salesman_id"])
    allocations = rows_to_dicts(
        conn.execute(
            f"""
            SELECT ia.*, s.billing_code, s.name AS salesman_name
            FROM invoice_allocations ia JOIN salesmen s ON s.id = ia.salesman_id
            WHERE ia.session_id = ?
            {allocation_filter}
            ORDER BY s.billing_code, ia.invoice_no
            """,
            allocation_params,
        ).fetchall()
    )
    allocated_by_salesman = allocation_totals(allocations)
    entry_by_salesman = {}
    for entry in entries:
        entry_by_salesman.setdefault(entry["salesman_id"], []).append(entry)
    salesman_status = []
    for salesman in salesmen:
        salesman_entries = entry_by_salesman.get(salesman["id"], [])
        slots_total = conn.execute("SELECT COUNT(*) FROM invoice_slots WHERE salesman_id = ?", (salesman["id"],)).fetchone()[0]
        slots_used = conn.execute(
            """
            SELECT COUNT(*) FROM invoice_allocations
            WHERE session_id = ? AND salesman_id = ?
            """,
            (session_id, salesman["id"]),
        ).fetchone()[0]
        locked = salesman_is_locked(conn, session_id, salesman["id"])
        value = sum(float(e["line_value"]) for e in salesman_entries)
        sticks = sum(float(e["sticks"]) for e in salesman_entries)
        allocated = allocated_by_salesman.get(salesman["id"], {"sticks": 0.0, "value": 0.0, "lines": 0})
        salesman_status.append(
            {
                **salesman,
                "complete": len(salesman_entries) > 0 or allocated["lines"] > 0,
                "locked": locked,
                "entry_count": len(salesman_entries),
                "sticks": sticks + allocated["sticks"],
                "value": round(value + allocated["value"], 2),
                "slots_total": slots_total,
                "slots_used": slots_used,
            }
        )
    session = dict(get_session(conn, session_id))
    allocated_sticks = sum(item["sticks"] for item in allocated_by_salesman.values())
    allocated_value = sum(item["value"] for item in allocated_by_salesman.values())
    allocated_lines = sum(item["lines"] for item in allocated_by_salesman.values())
    return {
        "session": session,
        "products": products,
        "salesmen": salesman_status,
        "entries": entries,
        "allocations": allocations,
        "totals": {
            "sticks": sum(float(e["sticks"]) for e in entries) + allocated_sticks,
            "value": round(sum(float(e["line_value"]) for e in entries) + allocated_value, 2),
            "entries": len(entries) + allocated_lines,
        },
    }


def split_salesman(conn, session_id, salesman_id):
    lines = rows_to_dicts(
        conn.execute(
            """
            SELECT te.product_id,
                   SUM(te.quantity_m) AS quantity_m,
                   SUM(te.sticks) AS sticks,
                   p.item_code,
                   p.item_description,
                   p.price_per_stick,
                   p.tally_sort_order
            FROM tally_entries te
            JOIN products p ON p.id = te.product_id
            WHERE te.session_id = ? AND te.salesman_id = ?
              AND (
                  (SELECT MAX(ia.created_at) FROM invoice_allocations ia WHERE ia.session_id = te.session_id AND ia.salesman_id = te.salesman_id) IS NULL
                  OR te.updated_at > (SELECT MAX(ia.created_at) FROM invoice_allocations ia WHERE ia.session_id = te.session_id AND ia.salesman_id = te.salesman_id)
              )
            GROUP BY te.product_id, p.item_code, p.item_description, p.price_per_stick, p.tally_sort_order
            ORDER BY p.tally_sort_order
            """,
            (session_id, salesman_id),
        ).fetchall()
    )
    groups = []
    remaining = []
    for line in lines:
        sticks = int(round(float(line["sticks"])))
        price = float(line["price_per_stick"])
        if sticks > 0 and price > 0:
            remaining.append({**line, "remaining_sticks": sticks, "price": price})

    def remaining_value(item):
        return item["remaining_sticks"] * item["price"]

    def make_part(item, sticks):
        value = round(sticks * item["price"], 2)
        return {
            "product_id": item["product_id"],
            "quantity_m": round(sticks / 1000, 3),
            "sticks": sticks,
            "item_code": item["item_code"],
            "item_description": item["item_description"],
            "price_per_stick": item["price_per_stick"],
            "tally_sort_order": item["tally_sort_order"],
            "line_value": value,
        }

    while any(item["remaining_sticks"] > 0 for item in remaining):
        current = {"total_value": 0.0, "lines": []}
        while True:
            budget = INVOICE_CAP - current["total_value"]
            candidates = []
            for idx, item in enumerate(remaining):
                if item["remaining_sticks"] <= 0:
                    continue
                max_sticks = int(budget // item["price"])
                if max_sticks <= 0:
                    continue
                sticks = min(item["remaining_sticks"], max_sticks)
                value = round(sticks * item["price"], 2)
                candidates.append(
                    {
                        "idx": idx,
                        "sticks": sticks,
                        "value": value,
                        "leftover": round(budget - value, 6),
                        "remaining_value": remaining_value(item),
                    }
                )
            if not candidates:
                break
            if current["lines"]:
                candidates.sort(key=lambda c: (c["leftover"], c["remaining_value"], -c["value"]))
            else:
                candidates.sort(key=lambda c: (c["leftover"], -c["remaining_value"]))
            choice = candidates[0]
            item = remaining[choice["idx"]]
            current["lines"].append(make_part(item, choice["sticks"]))
            current["total_value"] = round(current["total_value"] + choice["value"], 2)
            item["remaining_sticks"] -= choice["sticks"]
        if not current["lines"]:
            break
        current["lines"].sort(key=lambda line: line.get("tally_sort_order", 0))
        groups.append(current)
    available = conn.execute("SELECT COUNT(*) FROM invoice_slots WHERE salesman_id = ?", (salesman_id,)).fetchone()[0]
    if len(groups) > available:
        salesman = conn.execute("SELECT billing_code FROM salesmen WHERE id = ?", (salesman_id,)).fetchone()
        raise ApiError(
            HTTPStatus.CONFLICT,
            "InsufficientSlots",
            f"Cannot create invoice: salesman {salesman['billing_code']} has only {available} slots but needs {len(groups)}.",
        )
    return groups


def clear_materialized_tally(conn, session_id, salesman_id):
    latest = conn.execute(
        "SELECT MAX(created_at) FROM invoice_allocations WHERE session_id = ? AND salesman_id = ?",
        (session_id, salesman_id),
    ).fetchone()[0]
    if latest:
        conn.execute(
            """
            DELETE FROM tally_entries
            WHERE session_id = ? AND salesman_id = ? AND updated_at <= ?
            """,
            (session_id, salesman_id, latest),
        )


def allocation_totals(allocations):
    totals = {}
    for alloc in allocations:
        salesman_id = alloc["salesman_id"]
        item = totals.setdefault(salesman_id, {"sticks": 0.0, "value": 0.0, "lines": 0})
        item["value"] += float(alloc["total_value"])
        item["lines"] += 1
        for line in json.loads(alloc["lines_json"]):
            item["sticks"] += float(line.get("sticks", 0))
    return totals


def allocate_salesman_groups(conn, session_id, salesman, groups):
    slots = conn.execute(
        """
        SELECT *
        FROM invoice_slots isl
        WHERE isl.salesman_id = ?
          AND NOT EXISTS (
              SELECT 1 FROM invoice_allocations ia
              WHERE ia.session_id = ? AND ia.slot_id = isl.id
          )
        ORDER BY isl.slot_number DESC
        LIMIT ?
        """,
        (salesman["id"], session_id, len(groups)),
    ).fetchall()
    if len(slots) < len(groups):
        raise ApiError(
            HTTPStatus.CONFLICT,
            "InsufficientSlots",
            f"Cannot create invoice: salesman {salesman['billing_code']} has only {len(slots)} free slots but needs {len(groups)}.",
        )
    for group, slot in zip(groups, slots):
        conn.execute(
            """
            INSERT INTO invoice_allocations(session_id, salesman_id, slot_id, invoice_no, total_value, lines_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (session_id, salesman["id"], slot["id"], slot["slot_code"], group["total_value"], json.dumps(group["lines"]), utc_now()),
        )
    conn.execute("DELETE FROM tally_entries WHERE session_id = ? AND salesman_id = ?", (session_id, salesman["id"]))


def invoice_preview(conn, session_id):
    result = []
    for salesman in conn.execute("SELECT * FROM salesmen WHERE active = 1 ORDER BY billing_code").fetchall():
        groups = split_salesman(conn, session_id, salesman["id"])
        if groups:
            result.append({"salesman": dict(salesman), "groups": groups})
    return result


def generate_invoices(conn, session_id, user_id, ip, event_type="INVOICE_GENERATED", old_value=None):
    session = get_session(conn, session_id)
    preview = invoice_preview(conn, session_id)
    for item in preview:
        allocate_salesman_groups(conn, session_id, item["salesman"], item["groups"])
    audit(
        conn,
        user_id,
        event_type,
        "daily_sessions",
        session_id,
        old_value,
        {"groups": sum(len(i["groups"]) for i in preview)},
        ip,
    )
    return rows_to_dicts(conn.execute("SELECT * FROM invoice_allocations WHERE session_id = ?", (session_id,)).fetchall())


def generate_salesman_bill(conn, session_id, salesman_id, user_id, ip):
    session = get_session(conn, session_id)
    clear_materialized_tally(conn, session_id, salesman_id)
    salesman = conn.execute("SELECT * FROM salesmen WHERE id = ? AND active = 1", (salesman_id,)).fetchone()
    if not salesman:
        raise ApiError(HTTPStatus.NOT_FOUND, "NotFound", "Salesman not found.")
    groups = split_salesman(conn, session_id, salesman_id)
    if not groups:
        raise ApiError(HTTPStatus.CONFLICT, "NoEntries", "Enter bill quantities before generating a bill.")
    allocate_salesman_groups(conn, session_id, salesman, groups)
    audit(
        conn,
        user_id,
        "SALESMAN_BILL_GENERATED",
        "salesmen",
        salesman_id,
        None,
        {"session_id": session_id, "groups": len(groups)},
        ip,
    )
    return rows_to_dicts(
        conn.execute(
            """
            SELECT ia.*, s.billing_code
            FROM invoice_allocations ia JOIN salesmen s ON s.id = ia.salesman_id
            WHERE ia.session_id = ?
            ORDER BY s.billing_code, ia.invoice_no
            """,
            (session_id,),
        ).fetchall()
    )


def entered_data_csv(conn, session_id, user_id, ip):
    rows = conn.execute(
        """
        SELECT ds.session_date, s.billing_code, s.name AS salesman_name, p.item_code, p.item_description,
               te.quantity_m, te.sticks, te.line_value, te.created_at, te.updated_at
        FROM tally_entries te
        JOIN daily_sessions ds ON ds.id = te.session_id
        JOIN salesmen s ON s.id = te.salesman_id
        JOIN products p ON p.id = te.product_id
        WHERE te.session_id = ?
        ORDER BY s.billing_code, p.tally_sort_order
        """,
        (session_id,),
    ).fetchall()
    header = [
        "Session Date",
        "Billing Code",
        "Salesman Name",
        "Product Code",
        "Brand Name",
        "Quantity M",
        "Sticks",
        "Line Value",
        "Created At",
        "Updated At",
    ]
    output = [header]
    for row in rows:
        output.append(
            [
                row["session_date"],
                row["billing_code"],
                row["salesman_name"],
                row["item_code"],
                row["item_description"],
                f"{float(row['quantity_m']):g}",
                f"{float(row['sticks']):g}",
                f"{float(row['line_value']):.2f}",
                row["created_at"],
                row["updated_at"],
            ]
        )
    content = "\r\n".join(",".join(csv_quote(cell) for cell in row) for row in output) + "\r\n"
    file_hash = hashlib.sha256(content.encode()).hexdigest()
    audit(conn, user_id, "ENTERED_DATA_EXPORTED", "daily_sessions", session_id, None, {"sha256": file_hash}, ip)
    return content


def csv_export(conn, session_id, user_id, ip, salesman_id=None, allocation_id=None):
    conditions = ["ia.session_id = ?"]
    params = [session_id]
    if salesman_id is not None:
        conditions.append("ia.salesman_id = ?")
        params.append(salesman_id)
    if allocation_id is not None:
        conditions.append("ia.id = ?")
        params.append(allocation_id)
    allocations = conn.execute(
        f"""
        SELECT ia.*, s.customer_id AS salesman_customer_id, s.customer_name AS salesman_customer_name,
               s.ds_code AS salesman_ds_code, s.billing_code, s.beat_code AS salesman_beat_code,
               s.beat_name AS salesman_beat_name, isl.ds_code AS slot_ds_code, isl.beat_code AS slot_beat_code,
               isl.beat_name AS slot_beat_name, isl.customer_id AS slot_customer_id,
               isl.customer_name AS slot_customer_name
        FROM invoice_allocations ia
        JOIN salesmen s ON s.id = ia.salesman_id
        JOIN invoice_slots isl ON isl.id = ia.slot_id
        WHERE {" AND ".join(conditions)}
        ORDER BY s.billing_code, ia.invoice_no
        """,
        params,
    ).fetchall()
    if not allocations:
        raise ApiError(HTTPStatus.NOT_FOUND, "NoInvoices", "No generated invoices found for this export.")
    output = []
    header = [
        "Customer ID",
        "Customer Name",
        "Salesman ID",
        "Salesman Name",
        "Beat Code",
        "Beat Name",
        "Category",
        "Product Code",
        "Product Desc",
        "UOM (CFC/PAC/BaseUOM)",
        "Product Order QTY",
        "Order Reference No",
        "Cust Discount %",
    ]
    output.append(header)
    for alloc in allocations:
        for line in json.loads(alloc["lines_json"]):
            qty = "" if float(line["quantity_m"]) == 0 else f"{float(line['quantity_m']):g}"
            output.append(
                [
                    alloc["slot_customer_id"] or alloc["salesman_customer_id"],
                    alloc["slot_customer_name"] or alloc["salesman_customer_name"],
                    alloc["slot_ds_code"] or alloc["salesman_ds_code"],
                    alloc["billing_code"],
                    alloc["slot_beat_code"] or alloc["salesman_beat_code"],
                    alloc["slot_beat_name"] or alloc["salesman_beat_name"],
                    "CG-01 CIGARETTE",
                    line["item_code"],
                    line["item_description"],
                    "BaseUOM",
                    qty,
                    "",
                    "0.00",
                ]
            )
    text_lines = []
    for row in output:
        text_lines.append(",".join(csv_quote(cell) for cell in row))
    content = "\r\n".join(text_lines) + "\r\n"
    file_hash = hashlib.sha256(content.encode()).hexdigest()
    audit(
        conn,
        user_id,
        "CSV_EXPORTED",
        "daily_sessions",
        session_id,
        None,
        {
            "sha256": file_hash,
            "salesman_id": salesman_id,
            "allocation_id": allocation_id,
            "rows": max(0, len(output) - 1),
        },
        ip,
    )
    return content


def safe_filename_part(value):
    cleaned = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in str(value))
    return cleaned.strip("_") or "export"


def optional_int_query(query, name):
    raw = query.get(name, [""])[0]
    if raw in ("", None):
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise ApiError(HTTPStatus.BAD_REQUEST, "InvalidQuery", f"{name} must be a number.")


def csv_quote(value):
    value = "" if value is None else str(value)
    if any(ch in value for ch in [",", '"', "\n", "\r"]):
        return '"' + value.replace('"', '""') + '"'
    return value


class Handler(BaseHTTPRequestHandler):
    server_version = "SupraDMS/1.0"

    def log_message(self, fmt, *args):
        print(f"{self.address_string()} - {fmt % args}")

    def send_json(self, data, status=HTTPStatus.OK):
        payload = json.dumps(data, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def send_error_json(self, err: ApiError):
        self.send_json({"error": err.code, "message": err.message}, err.status)

    def read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length == 0:
            return {}
        return json.loads(self.rfile.read(length).decode())

    def current_user(self, conn):
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            raise ApiError(HTTPStatus.UNAUTHORIZED, "Unauthenticated", "Please log in.")
        payload = unsign(auth.split(" ", 1)[1])
        if not payload:
            raise ApiError(HTTPStatus.UNAUTHORIZED, "Unauthenticated", "Please log in again.")
        user = conn.execute("SELECT id, username, role, salesman_id FROM users WHERE id = ? AND active = 1", (payload["sub"],)).fetchone()
        if not user:
            raise ApiError(HTTPStatus.UNAUTHORIZED, "Unauthenticated", "Please log in again.")
        return user

    def do_GET(self):
        try:
            parsed = urlparse(self.path)
            if parsed.path.startswith("/api/"):
                self.handle_api("GET", parsed.path, parse_qs(parsed.query))
            else:
                self.serve_static(parsed.path)
        except ApiError as err:
            self.send_error_json(err)
        except Exception as exc:
            self.send_json({"error": "ServerError", "message": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_POST(self):
        try:
            parsed = urlparse(self.path)
            self.handle_api("POST", parsed.path, parse_qs(parsed.query))
        except ApiError as err:
            self.send_error_json(err)
        except Exception as exc:
            self.send_json({"error": "ServerError", "message": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_DELETE(self):
        try:
            parsed = urlparse(self.path)
            self.handle_api("DELETE", parsed.path, parse_qs(parsed.query))
        except ApiError as err:
            self.send_error_json(err)
        except Exception as exc:
            self.send_json({"error": "ServerError", "message": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def serve_static(self, path):
        target = STATIC_DIR / ("index.html" if path in ("", "/") else path.lstrip("/"))
        if not target.exists() or not target.is_file():
            raise ApiError(HTTPStatus.NOT_FOUND, "NotFound", "File not found.")
        content = target.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mimetypes.guess_type(str(target))[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def handle_api(self, method, path, query):
        with DB_LOCK, connect() as conn:
            ip = self.client_address[0]
            if path == "/api/auth/login" and method == "POST":
                data = self.read_json()
                username = str(data.get("username", "")).strip()
                password = str(data.get("password", ""))
                requested_role = str(data.get("role", "")).strip()
                user = conn.execute("SELECT * FROM users WHERE username = ? AND active = 1", (username,)).fetchone()
                if not user:
                    raise ApiError(HTTPStatus.UNAUTHORIZED, "LoginFailed", "Invalid username, password, or user type.")
                if requested_role and requested_role != user["role"]:
                    raise ApiError(HTTPStatus.UNAUTHORIZED, "LoginFailed", "Invalid username, password, or user type.")
                if user["locked_until"] > int(time.time()):
                    raise ApiError(HTTPStatus.LOCKED, "AccountLocked", "Account locked for 15 minutes after failed login attempts.")
                ok = verify_password(password, user["password_hash"]) if password else False
                if not ok:
                    attempts = user["failed_attempts"] + 1
                    locked_until = int(time.time()) + 15 * 60 if attempts >= 5 else 0
                    conn.execute("UPDATE users SET failed_attempts = ?, locked_until = ? WHERE id = ?", (attempts, locked_until, user["id"]))
                    raise ApiError(HTTPStatus.UNAUTHORIZED, "LoginFailed", "Invalid username, password, or user type.")
                conn.execute("UPDATE users SET failed_attempts = 0, locked_until = 0 WHERE id = ?", (user["id"],))
                payload = {"sub": user["id"], "role": user["role"], "exp": int(time.time()) + TOKEN_TTL_SECONDS}
                self.send_json({"token": sign(payload), "user": {"id": user["id"], "username": user["username"], "role": user["role"], "salesman_id": user["salesman_id"]}})
                return

            user = self.current_user(conn)

            if path == "/api/me" and method == "GET":
                self.send_json({"user": dict(user), "permissions": sorted(ROLE_PERMISSIONS.get(user["role"], []))})
                return

            if path == "/api/bootstrap" and method == "GET":
                active = get_active_session(conn)
                salesman_filter = ""
                salesman_params = []
                if user["role"] == "salesman" and user["salesman_id"]:
                    salesman_filter = " AND id = ?"
                    salesman_params.append(user["salesman_id"])
                self.send_json(
                    {
                        "products": rows_to_dicts(conn.execute("SELECT * FROM products WHERE active = 1 ORDER BY tally_sort_order").fetchall()),
                        "salesmen": rows_to_dicts(
                            conn.execute(
                                f"SELECT * FROM salesmen WHERE active = 1{salesman_filter} ORDER BY billing_code",
                                salesman_params,
                            ).fetchall()
                        ),
                        "session": dict(active) if active else None,
                        "settings": {
                            "invoice_cap": INVOICE_CAP,
                            "default_opening_sticks": DEFAULT_OPENING_STICKS,
                        },
                    }
                )
                return

            if path == "/api/sessions/open" and method == "POST":
                require_permission(user, "open_session")
                data = self.read_json()
                date = data.get("session_date") or today()
                advance_closed = "session_date" not in data or bool(data.get("advance_closed"))
                session_row, created = create_or_get_session(conn, date, user["id"], ip, advance_closed=advance_closed)
                self.send_json({"session": dict(session_row)}, HTTPStatus.CREATED if created else HTTPStatus.OK)
                return

            parts = path.strip("/").split("/")
            if len(parts) >= 3 and parts[0] == "api" and parts[1] == "sessions":
                session_id = int(parts[2])
                session = get_session(conn, session_id)
                if len(parts) == 4 and parts[3] == "summary" and method == "GET":
                    if "view_reports" not in ROLE_PERMISSIONS.get(user["role"], set()) and "enter_tally" not in ROLE_PERMISSIONS.get(user["role"], set()):
                        raise ApiError(HTTPStatus.FORBIDDEN, "Forbidden", "Your role cannot view this session.")
                    self.send_json(session_summary(conn, session_id, user))
                    return
                if len(parts) == 4 and parts[3] == "inventory" and method == "POST":
                    require_permission(user, "set_inventory")
                    data = self.read_json()
                    balances = data.get("balances", [])
                    for item in balances:
                        product_id = int(item["product_id"])
                        opening = float(item["opening_sticks"])
                        conn.execute(
                            """
                            INSERT INTO inventory(session_id, product_id, opening_sticks, sticks_in_stock)
                            VALUES (?, ?, ?, ?)
                            ON CONFLICT(session_id, product_id)
                            DO UPDATE SET opening_sticks = excluded.opening_sticks, sticks_in_stock = excluded.sticks_in_stock
                            """,
                            (session_id, product_id, opening, opening),
                        )
                    audit(conn, user["id"], "INVENTORY_ADJUSTED", "daily_sessions", session_id, None, {"count": len(balances)}, ip)
                    self.send_json({"ok": True})
                    return
                if len(parts) == 4 and parts[3] == "end" and method == "POST":
                    require_permission(user, "end_session")
                    if session["status"] == "uploaded":
                        raise ApiError(HTTPStatus.CONFLICT, "SessionEnded", "This session has already ended.")
                    data = self.read_json()
                    conn.execute(
                        "UPDATE daily_sessions SET status = 'uploaded' WHERE id = ?",
                        (session_id,),
                    )
                    audit(
                        conn,
                        user["id"],
                        "SESSION_ENDED",
                        "daily_sessions",
                        session_id,
                        {"status": session["status"]},
                        {"status": "uploaded"},
                        ip,
                    )
                    if data.get("open_next"):
                        next_session, _ = create_or_get_session(conn, next_date(session["session_date"]), user["id"], ip, advance_closed=True)
                        self.send_json(session_summary(conn, next_session["id"], user))
                        return
                    self.send_json(session_summary(conn, session_id, user))
                    return
                if len(parts) == 4 and parts[3] == "lock" and method == "POST":
                    raise ApiError(HTTPStatus.GONE, "LockingDisabled", "Session locking has been removed.")
                if len(parts) == 4 and parts[3] == "unlock" and method == "POST":
                    raise ApiError(HTTPStatus.GONE, "LockingDisabled", "Session locking has been removed.")
                if len(parts) == 5 and parts[3] == "salesmen-locks" and method == "POST":
                    raise ApiError(HTTPStatus.GONE, "LockingDisabled", "Salesman locking has been removed.")
                if len(parts) == 5 and parts[3] == "salesmen-locks" and method == "DELETE":
                    raise ApiError(HTTPStatus.GONE, "LockingDisabled", "Salesman locking has been removed.")
                if len(parts) == 5 and parts[3] == "salesmen-bills" and method == "POST":
                    if "generate_bill" not in ROLE_PERMISSIONS.get(user["role"], set()) and user["role"] != "admin":
                        raise ApiError(HTTPStatus.FORBIDDEN, "Forbidden", "Your role cannot generate salesman bills.")
                    salesman_id = int(parts[4])
                    generate_salesman_bill(conn, session_id, salesman_id, user["id"], ip)
                    self.send_json(session_summary(conn, session_id, user))
                    return
                if len(parts) == 4 and parts[3] == "invoice-preview" and method == "GET":
                    require_permission(user, "generate_invoices")
                    self.send_json({"preview": invoice_preview(conn, session_id)})
                    return
                if len(parts) == 4 and parts[3] == "generate-invoices" and method == "POST":
                    require_permission(user, "generate_invoices")
                    allocations = generate_invoices(conn, session_id, user["id"], ip)
                    self.send_json({"allocations": allocations})
                    return
                if len(parts) == 4 and parts[3] == "csv" and method == "GET":
                    require_permission(user, "download_csv")
                    salesman_id = optional_int_query(query, "salesman_id")
                    allocation_id = optional_int_query(query, "allocation_id")
                    if allocation_id is None:
                        generate_invoices(conn, session_id, user["id"], ip)
                    filename_suffix = ""
                    if salesman_id is not None:
                        salesman = conn.execute(
                            "SELECT billing_code FROM salesmen WHERE id = ? AND active = 1",
                            (salesman_id,),
                        ).fetchone()
                        if not salesman:
                            raise ApiError(HTTPStatus.NOT_FOUND, "NotFound", "Salesman not found.")
                        filename_suffix = f"_{safe_filename_part(salesman['billing_code'])}"
                    if allocation_id is not None:
                        allocation = conn.execute(
                            """
                            SELECT ia.invoice_no, s.billing_code
                            FROM invoice_allocations ia
                            JOIN salesmen s ON s.id = ia.salesman_id
                            WHERE ia.id = ? AND ia.session_id = ?
                            """,
                            (allocation_id, session_id),
                        ).fetchone()
                        if not allocation:
                            raise ApiError(HTTPStatus.NOT_FOUND, "NotFound", "Invoice not found.")
                        filename_suffix = f"_{safe_filename_part(allocation['billing_code'])}_{safe_filename_part(allocation['invoice_no'])}"
                    content = csv_export(conn, session_id, user["id"], ip, salesman_id=salesman_id, allocation_id=allocation_id)
                    payload = content.encode()
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", "text/csv")
                    self.send_header("Content-Disposition", f"attachment; filename=ORDER_IMPORT_DATA_{session['session_date']}{filename_suffix}.csv")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                if len(parts) == 4 and parts[3] == "entries-csv" and method == "GET":
                    require_permission(user, "download_csv")
                    content = entered_data_csv(conn, session_id, user["id"], ip)
                    payload = content.encode()
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", "text/csv")
                    self.send_header("Content-Disposition", f"attachment; filename=ENTERED_DATA_{session['session_date']}.csv")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return

            if path == "/api/tally" and method == "POST":
                require_permission(user, "enter_tally")
                data = self.read_json()
                session_id = int(data["session_id"])
                salesman_id = int(data["salesman_id"])
                product_id = int(data["product_id"])
                qty = parse_quantity(data["quantity_m"])
                if user["role"] == "salesman" and user["salesman_id"] and user["salesman_id"] != salesman_id:
                    raise ApiError(HTTPStatus.FORBIDDEN, "Forbidden", "Salesmen can only update their own billing code.")
                session = get_session(conn, session_id)
                if session["status"] == "uploaded":
                    raise ApiError(HTTPStatus.CONFLICT, "SessionUploaded", "Uploaded sessions cannot be changed.")
                clear_materialized_tally(conn, session_id, salesman_id)
                product = get_product(conn, product_id)
                sticks = round(qty * 1000, 3)
                line_value = round(sticks * float(product["price_per_stick"]), 2)
                existing = conn.execute(
                    "SELECT * FROM tally_entries WHERE session_id = ? AND salesman_id = ? AND product_id = ?",
                    (session_id, salesman_id, product_id),
                ).fetchone()
                previous_sticks = float(existing["sticks"]) if existing else 0
                delta = sticks - previous_sticks
                inv = conn.execute("SELECT * FROM inventory WHERE session_id = ? AND product_id = ?", (session_id, product_id)).fetchone()
                if not inv:
                    ensure_inventory(conn, session_id)
                    inv = conn.execute("SELECT * FROM inventory WHERE session_id = ? AND product_id = ?", (session_id, product_id)).fetchone()
                new_stock = float(inv["sticks_in_stock"]) - delta
                if new_stock < 0:
                    available_boxes = round(float(inv["sticks_in_stock"]) / product["pack_size"], 2)
                    requested_boxes = round(delta / product["pack_size"], 2)
                    raise ApiError(
                        HTTPStatus.CONFLICT,
                        "InsufficientStock",
                        f"Not enough stock for {product['item_description']}. Available: {available_boxes} boxes. Requested: {requested_boxes} boxes.",
                    )
                conn.execute("UPDATE inventory SET sticks_in_stock = ? WHERE session_id = ? AND product_id = ?", (new_stock, session_id, product_id))
                if existing:
                    old = dict(existing)
                    conn.execute(
                        """
                        UPDATE tally_entries
                        SET quantity_m = ?, sticks = ?, line_value = ?, updated_by = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (qty, sticks, line_value, user["id"], utc_now(), existing["id"]),
                    )
                    event = "ENTRY_MODIFIED"
                    entry_id = existing["id"]
                else:
                    old = None
                    cur = conn.execute(
                        """
                        INSERT INTO tally_entries(session_id, salesman_id, product_id, quantity_m, sticks, line_value, created_by, updated_by, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (session_id, salesman_id, product_id, qty, sticks, line_value, user["id"], user["id"], utc_now(), utc_now()),
                    )
                    event = "ENTRY_CREATED"
                    entry_id = cur.lastrowid
                split_salesman(conn, session_id, salesman_id)
                new = dict(conn.execute("SELECT * FROM tally_entries WHERE id = ?", (entry_id,)).fetchone())
                audit(conn, user["id"], event, "tally_entries", entry_id, old, new, ip)
                self.send_json({"entry": new, "summary": session_summary(conn, session_id, user)})
                return

            if path == "/api/tally" and method == "DELETE":
                require_permission(user, "enter_tally")
                data = self.read_json()
                session_id = int(data["session_id"])
                salesman_id = int(data["salesman_id"])
                product_id = int(data["product_id"])
                if user["role"] == "salesman" and user["salesman_id"] and user["salesman_id"] != salesman_id:
                    raise ApiError(HTTPStatus.FORBIDDEN, "Forbidden", "Salesmen can only delete their own entries.")
                session = get_session(conn, session_id)
                if session["status"] == "uploaded":
                    raise ApiError(HTTPStatus.CONFLICT, "SessionUploaded", "Uploaded sessions cannot be changed.")
                clear_materialized_tally(conn, session_id, salesman_id)
                existing = conn.execute(
                    "SELECT * FROM tally_entries WHERE session_id = ? AND salesman_id = ? AND product_id = ?",
                    (session_id, salesman_id, product_id),
                ).fetchone()
                if not existing:
                    raise ApiError(HTTPStatus.NOT_FOUND, "NotFound", "Tally entry not found.")
                inv = conn.execute("SELECT * FROM inventory WHERE session_id = ? AND product_id = ?", (session_id, product_id)).fetchone()
                if inv:
                    conn.execute(
                        "UPDATE inventory SET sticks_in_stock = ? WHERE session_id = ? AND product_id = ?",
                        (float(inv["sticks_in_stock"]) + float(existing["sticks"]), session_id, product_id),
                    )
                old = dict(existing)
                conn.execute("DELETE FROM tally_entries WHERE id = ?", (existing["id"],))
                audit(conn, user["id"], "ENTRY_DELETED", "tally_entries", existing["id"], old, None, ip)
                self.send_json({"ok": True, "summary": session_summary(conn, session_id, user)})
                return

            if path == "/api/audit" and method == "GET":
                require_permission(user, "view_audit")
                logs = rows_to_dicts(
                    conn.execute(
                        """
                        SELECT al.*, u.username
                        FROM audit_log al LEFT JOIN users u ON u.id = al.user_id
                        ORDER BY al.id DESC LIMIT 200
                        """
                    ).fetchall()
                )
                self.send_json({"logs": logs})
                return

            raise ApiError(HTTPStatus.NOT_FOUND, "NotFound", "Endpoint not found.")


def main():
    init_db()
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8000"))
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"Supra DMS running at http://{host}:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
