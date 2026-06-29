"""
db.py - everything about the database lives here.

We use SQLite: a database that is just a single file (invoiceai.db) sitting
next to this code. No server to install. Good enough for a real working
project, and you can move to PostgreSQL later without changing your app logic
much.

Tables:
  organizations    - each client/company that uses the system
  telegram_links   - maps a Telegram chat to an organization
  extraction_fields- the configurable fields each org wants pulled from invoices
  invoices         - every invoice that comes in, plus its extracted data
  audit_log        - a record of every change (who did what, when)
"""

import os
import sqlite3
import json
import hashlib
import secrets
from datetime import datetime, timezone

DB_PATH = os.path.join(os.path.dirname(__file__), "invoiceai.db")


def now_iso() -> str:
    """Current time as a text string we can store and sort."""
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------
# Password hashing & sessions
# --------------------------------------------------------------------------
def hash_password(password: str) -> str:
    """
    Turn a plain password into a safe-to-store hash.
    We use PBKDF2 (built into Python) with a random salt and many iterations,
    so even if the database leaks, the actual passwords stay protected.
    Format stored: salt$hash (both hex).
    """
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 200_000)
    return f"{salt}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Check a login attempt against the stored salt$hash. Constant-time compare."""
    try:
        salt, expected = stored.split("$", 1)
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 200_000)
        return secrets.compare_digest(dk.hex(), expected)
    except Exception:
        return False


def new_session(user_id: int) -> str:
    """Create a login session, return its token (goes in the browser cookie)."""
    token = secrets.token_urlsafe(32)
    conn = get_db()
    conn.execute(
        "INSERT INTO sessions (token, user_id, created_at) VALUES (?,?,?)",
        (token, user_id, now_iso()),
    )
    conn.commit()
    conn.close()
    return token


def user_for_session(token: str):
    """Given a cookie token, return the logged-in user row, or None."""
    if not token:
        return None
    conn = get_db()
    row = conn.execute(
        "SELECT u.* FROM users u JOIN sessions s ON s.user_id = u.id WHERE s.token = ?",
        (token,),
    ).fetchone()
    conn.close()
    return row


def end_session(token: str):
    conn = get_db()
    conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
    conn.commit()
    conn.close()


def user_in_org(user_id: int, org_id: int) -> bool:
    """The core security check: is this user a member of this org?"""
    conn = get_db()
    row = conn.execute(
        "SELECT 1 FROM memberships WHERE user_id = ? AND org_id = ?",
        (user_id, org_id),
    ).fetchone()
    conn.close()
    return row is not None


def get_db():
    """
    Open a connection to the database file.
    row_factory = sqlite3.Row lets us read columns by name (row["vendor"])
    instead of by number (row[3]), which is far easier to read.
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")  # enforce relationships between tables
    return conn


def init_db():
    """Create all tables if they don't exist yet. Safe to run every startup."""
    conn = get_db()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS organizations (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT NOT NULL,
            slug        TEXT UNIQUE NOT NULL,
            created_at  TEXT
        );

        -- A person who can log in. Passwords are stored ONLY as a salted hash,
        -- never as plain text.
        CREATE TABLE IF NOT EXISTS users (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            email         TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            name          TEXT,
            created_at    TEXT
        );

        -- Which users belong to which organizations. A user only ever sees the
        -- orgs they are a member of. This is the heart of multi-tenant security.
        CREATE TABLE IF NOT EXISTS memberships (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL,
            org_id      INTEGER NOT NULL,
            role        TEXT DEFAULT 'member',   -- 'owner' or 'member'
            created_at  TEXT,
            UNIQUE(user_id, org_id),
            FOREIGN KEY (user_id) REFERENCES users(id),
            FOREIGN KEY (org_id) REFERENCES organizations(id)
        );

        -- Login sessions. A random token is stored in the user's browser cookie
        -- and matched here to know who is logged in.
        CREATE TABLE IF NOT EXISTS sessions (
            token       TEXT PRIMARY KEY,
            user_id     INTEGER NOT NULL,
            created_at  TEXT,
            FOREIGN KEY (user_id) REFERENCES users(id)
        );

        -- Which Telegram chat belongs to which org. This is how an invoice
        -- sent on Telegram gets routed to the right organization.
        CREATE TABLE IF NOT EXISTS telegram_links (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            org_id          INTEGER NOT NULL,
            telegram_chat_id TEXT NOT NULL,
            label           TEXT,
            created_at      TEXT,
            UNIQUE(telegram_chat_id),
            FOREIGN KEY (org_id) REFERENCES organizations(id)
        );

        -- The configurable extraction fields, per organization.
        -- This is the heart of the "different businesses care about different
        -- fields" idea. An admin edits these in the dashboard.
        CREATE TABLE IF NOT EXISTS extraction_fields (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            org_id      INTEGER NOT NULL,
            field_key   TEXT NOT NULL,        -- machine name e.g. "invoice_number"
            label       TEXT NOT NULL,        -- shown to humans e.g. "Invoice Number"
            hint        TEXT,                 -- optional guidance for the AI
            required    INTEGER DEFAULT 0,    -- 1 = must be present
            active      INTEGER DEFAULT 1,    -- 1 = currently used
            sort_order  INTEGER DEFAULT 0,
            FOREIGN KEY (org_id) REFERENCES organizations(id)
        );

        CREATE TABLE IF NOT EXISTS invoices (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            org_id          INTEGER NOT NULL,
            source          TEXT DEFAULT 'web',   -- 'web' or 'telegram'
            telegram_chat_id TEXT,                -- which chat sent this, if via telegram
            filename        TEXT,
            file_path       TEXT,
            raw_text        TEXT,
            fields_json     TEXT,    -- {field_key: value}
            confidence_json TEXT,    -- {field_key: 0.0-1.0}  (innovation #1)
            line_items_json TEXT,    -- [{description, qty, unit_price, amount}] (#4)
            doc_hash        TEXT,    -- fingerprint for duplicate detection (#3)
            is_duplicate    INTEGER DEFAULT 0,
            duplicate_of    INTEGER, -- id of the invoice this duplicates
            status          TEXT DEFAULT 'needs_review', -- needs_review|approved
            created_at      TEXT,
            FOREIGN KEY (org_id) REFERENCES organizations(id)
        );

        CREATE TABLE IF NOT EXISTS audit_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            invoice_id  INTEGER NOT NULL,
            action      TEXT,        -- 'corrected' | 'reextracted' | 'approved'
            field_name  TEXT,
            old_value   TEXT,
            new_value   TEXT,
            changed_by  TEXT,
            changed_at  TEXT,
            FOREIGN KEY (invoice_id) REFERENCES invoices(id)
        );
        """
    )
    conn.commit()

    # Migration: older databases won't have this column yet. Add it if missing.
    try:
        conn.execute("ALTER TABLE invoices ADD COLUMN telegram_chat_id TEXT")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # column already exists

    conn.close()


# Default field set we give a brand-new organization so it works immediately.
DEFAULT_FIELDS = [
    ("vendor",         "Vendor",         "The company that issued the invoice", 1),
    ("invoice_number", "Invoice Number", "The unique invoice/document number",  1),
    ("date",           "Invoice Date",   "Format as YYYY-MM-DD",                 0),
    ("due_date",       "Due Date",       "Format as YYYY-MM-DD",                 0),
    ("po_number",      "PO Number",      "Purchase order number if present",     0),
    ("amount",         "Total Amount",   "The grand total, numbers only",        1),
    ("currency",       "Currency",       "e.g. BHD, AED, USD",                   0),
    ("tax",            "Tax / VAT",      "Tax amount, numbers only",             0),
]


def seed_demo_data():
    """
    On first run, create a demo organization with default fields AND a demo
    login that owns it, so a fresh install is usable immediately.
    Demo login: demo@greenledger.ai / demo1234
    Also rescues any org with no members (e.g. data from before auth existed)
    by linking it to the demo user. Safe to call repeatedly.
    """
    conn = get_db()
    existing = conn.execute("SELECT COUNT(*) c FROM organizations").fetchone()["c"]
    has_users = conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]

    if existing == 0:
        cur = conn.execute(
            "INSERT INTO organizations (name, slug, created_at) VALUES (?, ?, ?)",
            ("Demo Company", "demo", now_iso()),
        )
        org_id = cur.lastrowid
        for i, (key, label, hint, required) in enumerate(DEFAULT_FIELDS):
            conn.execute(
                "INSERT INTO extraction_fields "
                "(org_id, field_key, label, hint, required, active, sort_order) "
                "VALUES (?, ?, ?, ?, ?, 1, ?)",
                (org_id, key, label, hint, required, i),
            )
        conn.commit()

    if has_users == 0:
        ucur = conn.execute(
            "INSERT INTO users (email, password_hash, name, created_at) VALUES (?,?,?,?)",
            ("demo@greenledger.ai", hash_password("demo1234"), "Demo User", now_iso()),
        )
        demo_user = ucur.lastrowid
        # Link the demo user to every org that has no members yet (rescues old data).
        orphan_orgs = conn.execute(
            "SELECT id FROM organizations WHERE id NOT IN (SELECT org_id FROM memberships)"
        ).fetchall()
        for o in orphan_orgs:
            conn.execute(
                "INSERT OR IGNORE INTO memberships (user_id, org_id, role, created_at) VALUES (?,?,?,?)",
                (demo_user, o["id"], "owner", now_iso()),
            )
        conn.commit()
    conn.close()


def get_active_fields(org_id: int):
    """Return the active extraction fields for an org, in display order."""
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM extraction_fields WHERE org_id = ? AND active = 1 "
        "ORDER BY sort_order, id",
        (org_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]
