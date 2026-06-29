"""
main.py - the web server and all its endpoints (the "doorbells").

It serves the dashboard page and exposes a JSON API the page talks to:
organizations, configurable fields, invoice intake, review/correct/approve,
audit history, analytics, and Excel/CSV export.

It also starts the Telegram bot in the background (see telegram_bot.py) so a
single `python -m uvicorn main:app` command runs the whole product.
"""

import os
import io
import csv
import json
import shutil
import threading

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request, Response, Depends, Cookie
from fastapi.responses import HTMLResponse, StreamingResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

import db
import extraction

UPLOAD_DIR = os.path.join(os.path.dirname(__file__), "uploads")
HERE = os.path.dirname(__file__)

app = FastAPI(title="Greenledger AI")

# Render (and most hosts) set this automatically. We use it to decide whether
# the login cookie requires HTTPS - required in production, but turned off
# for local testing on plain http://127.0.0.1.
IS_PRODUCTION = bool(os.environ.get("RENDER") or os.environ.get("PRODUCTION"))


# --------------------------------------------------------------------------
# Auth: figure out who's logged in, and guard org access
# --------------------------------------------------------------------------
def current_user(session: str = Cookie(default=None)):
    """
    FastAPI dependency. Reads the 'session' cookie, returns the logged-in user
    row, or raises 401 if nobody is logged in. Attach this to any endpoint that
    requires login.
    """
    user = db.user_for_session(session)
    if user is None:
        raise HTTPException(status_code=401, detail="Not logged in")
    return user


def require_org_access(user, org_id: int):
    """
    The core multi-tenant security check. Call this inside every org-scoped
    endpoint: it raises 403 if the logged-in user is not a member of that org,
    so users can never read or touch another organization's data.
    """
    if not db.user_in_org(user["id"], org_id):
        raise HTTPException(status_code=403, detail="You don't have access to this organization")


def org_id_for_invoice(invoice_id: int):
    """Look up which org an invoice belongs to (for permission checks)."""
    conn = db.get_db()
    row = conn.execute("SELECT org_id FROM invoices WHERE id = ?", (invoice_id,)).fetchone()
    conn.close()
    return row["org_id"] if row else None


def org_id_for_field(field_id: int):
    conn = db.get_db()
    row = conn.execute("SELECT org_id FROM extraction_fields WHERE id = ?", (field_id,)).fetchone()
    conn.close()
    return row["org_id"] if row else None


# --------------------------------------------------------------------------
# Shared invoice processing - used by BOTH web upload and Telegram
# --------------------------------------------------------------------------
def process_invoice_file(org_id: int, file_path: str, filename: str, source: str,
                          telegram_chat_id: str = None):
    """
    The core pipeline: read text -> AI extract -> detect duplicates -> save.
    Returns the saved invoice row as a dict, plus a "diagnostic" string that
    explains exactly why extraction came back empty, if it did.
    """
    fields = db.get_active_fields(org_id)

    try:
        result = extraction.extract_from_file(file_path, fields)
        text = extraction.read_text(file_path)  # store text layer if any (PDFs)
    except Exception as e:
        result = extraction._blank(fields, f"Unexpected error: {e}")
        text = ""
        print("Process error:", repr(e))

    diagnostic = result.get("diagnostic")

    doc_hash = extraction.make_doc_hash(result["fields"])

    conn = db.get_db()
    # Duplicate check: same fingerprint, same org, already stored before.
    dup = conn.execute(
        "SELECT id FROM invoices WHERE org_id = ? AND doc_hash = ? LIMIT 1",
        (org_id, doc_hash),
    ).fetchone()
    is_dup = 1 if dup else 0
    dup_of = dup["id"] if dup else None

    cur = conn.execute(
        "INSERT INTO invoices (org_id, source, telegram_chat_id, filename, file_path, raw_text, "
        "fields_json, confidence_json, line_items_json, doc_hash, is_duplicate, "
        "duplicate_of, status, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            org_id, source, telegram_chat_id, filename, file_path, text,
            json.dumps(result["fields"]),
            json.dumps(result["confidence"]),
            json.dumps(result["line_items"]),
            doc_hash, is_dup, dup_of, "needs_review", db.now_iso(),
        ),
    )
    conn.commit()
    inv_id = cur.lastrowid
    row = conn.execute("SELECT * FROM invoices WHERE id = ?", (inv_id,)).fetchone()
    conn.close()
    out = _invoice_to_dict(row)
    out["diagnostic"] = diagnostic
    return out


def _invoice_to_dict(row) -> dict:
    """Turn a database row into clean JSON for the front end."""
    return {
        "id": row["id"],
        "org_id": row["org_id"],
        "source": row["source"],
        "filename": row["filename"],
        "fields": json.loads(row["fields_json"] or "{}"),
        "confidence": json.loads(row["confidence_json"] or "{}"),
        "line_items": json.loads(row["line_items_json"] or "[]"),
        "is_duplicate": bool(row["is_duplicate"]),
        "duplicate_of": row["duplicate_of"],
        "status": row["status"],
        "created_at": row["created_at"],
    }


# --------------------------------------------------------------------------
# Startup
# --------------------------------------------------------------------------
@app.on_event("startup")
def startup():
    db.init_db()
    db.seed_demo_data()
    # Start the Telegram bot in a background thread, only if a token is set.
    if os.environ.get("TELEGRAM_BOT_TOKEN", "").strip():
        import telegram_bot
        t = threading.Thread(
            target=telegram_bot.run_bot,
            args=(process_invoice_file,),
            daemon=True,
        )
        t.start()
        print("Telegram bot started.")
    else:
        print("No TELEGRAM_BOT_TOKEN set - running web only.")


@app.get("/", response_class=HTMLResponse)
def home(session: str = Cookie(default=None)):
    # If not logged in, send them to the login page.
    if db.user_for_session(session) is None:
        return RedirectResponse(url="/login")
    with open(os.path.join(HERE, "static", "index.html"), encoding="utf-8") as f:
        return f.read()


@app.get("/login", response_class=HTMLResponse)
def login_page():
    with open(os.path.join(HERE, "static", "login.html"), encoding="utf-8") as f:
        return f.read()


# --------------------------------------------------------------------------
# Auth endpoints
# --------------------------------------------------------------------------
@app.post("/api/auth/signup")
def signup(response: Response, email: str = Form(...), password: str = Form(...), name: str = Form("")):
    email = email.strip().lower()
    if len(password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters.")
    conn = db.get_db()
    existing = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
    if existing:
        conn.close()
        raise HTTPException(status_code=400, detail="An account with that email already exists.")
    cur = conn.execute(
        "INSERT INTO users (email, password_hash, name, created_at) VALUES (?,?,?,?)",
        (email, db.hash_password(password), name.strip(), db.now_iso()),
    )
    user_id = cur.lastrowid
    # New signup gets their own starter organization automatically.
    org_cur = conn.execute(
        "INSERT INTO organizations (name, slug, created_at) VALUES (?,?,?)",
        (f"{name or email.split('@')[0]}'s Company", f"org{user_id}", db.now_iso()),
    )
    org_id = org_cur.lastrowid
    for i, (key, label, hint, required) in enumerate(db.DEFAULT_FIELDS):
        conn.execute(
            "INSERT INTO extraction_fields (org_id, field_key, label, hint, required, active, sort_order) "
            "VALUES (?,?,?,?,?,1,?)", (org_id, key, label, hint, required, i),
        )
    conn.execute(
        "INSERT INTO memberships (user_id, org_id, role, created_at) VALUES (?,?,?,?)",
        (user_id, org_id, "owner", db.now_iso()),
    )
    conn.commit()
    conn.close()
    token = db.new_session(user_id)
    response.set_cookie("session", token, httponly=True, samesite="lax", secure=IS_PRODUCTION, max_age=60*60*24*30)
    return {"ok": True}


@app.post("/api/auth/login")
def login(response: Response, email: str = Form(...), password: str = Form(...)):
    email = email.strip().lower()
    conn = db.get_db()
    user = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    conn.close()
    if user is None or not db.verify_password(password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Wrong email or password.")
    token = db.new_session(user["id"])
    response.set_cookie("session", token, httponly=True, samesite="lax", secure=IS_PRODUCTION, max_age=60*60*24*30)
    return {"ok": True}


@app.post("/api/auth/logout")
def logout(response: Response, session: str = Cookie(default=None)):
    if session:
        db.end_session(session)
    response.delete_cookie("session")
    return {"ok": True}


@app.get("/api/auth/me")
def me(user=Depends(current_user)):
    return {"id": user["id"], "email": user["email"], "name": user["name"]}


# --------------------------------------------------------------------------
# Organizations (scoped to the logged-in user)
# --------------------------------------------------------------------------
@app.get("/api/orgs")
def list_orgs(user=Depends(current_user)):
    """Only the organizations this user is a member of."""
    conn = db.get_db()
    rows = conn.execute(
        "SELECT o.* FROM organizations o "
        "JOIN memberships m ON m.org_id = o.id "
        "WHERE m.user_id = ? ORDER BY o.id",
        (user["id"],),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.post("/api/orgs")
def create_org(name: str = Form(...), slug: str = Form(...), user=Depends(current_user)):
    conn = db.get_db()
    try:
        cur = conn.execute(
            "INSERT INTO organizations (name, slug, created_at) VALUES (?,?,?)",
            (name, slug.strip().lower(), db.now_iso()),
        )
        org_id = cur.lastrowid
        # The creator becomes the owner/member of their new org.
        conn.execute(
            "INSERT INTO memberships (user_id, org_id, role, created_at) VALUES (?,?,?,?)",
            (user["id"], org_id, "owner", db.now_iso()),
        )
        for i, (key, label, hint, required) in enumerate(db.DEFAULT_FIELDS):
            conn.execute(
                "INSERT INTO extraction_fields "
                "(org_id, field_key, label, hint, required, active, sort_order) "
                "VALUES (?,?,?,?,?,1,?)",
                (org_id, key, label, hint, required, i),
            )
        conn.commit()
    except Exception as e:
        conn.close()
        raise HTTPException(status_code=400, detail=str(e))
    conn.close()
    return {"id": org_id, "name": name, "slug": slug}


# --------------------------------------------------------------------------
# Configurable extraction fields
# --------------------------------------------------------------------------
@app.get("/api/orgs/{org_id}/fields")
def get_fields(org_id: int, user=Depends(current_user)):
    require_org_access(user, org_id)
    conn = db.get_db()
    rows = conn.execute(
        "SELECT * FROM extraction_fields WHERE org_id = ? ORDER BY sort_order, id",
        (org_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.post("/api/orgs/{org_id}/fields")
def add_field(
    org_id: int,
    field_key: str = Form(...),
    label: str = Form(...),
    hint: str = Form(""),
    required: int = Form(0),
    user=Depends(current_user),
):
    require_org_access(user, org_id)
    conn = db.get_db()
    maxorder = conn.execute(
        "SELECT COALESCE(MAX(sort_order), 0) m FROM extraction_fields WHERE org_id = ?",
        (org_id,),
    ).fetchone()["m"]
    cur = conn.execute(
        "INSERT INTO extraction_fields "
        "(org_id, field_key, label, hint, required, active, sort_order) "
        "VALUES (?,?,?,?,?,1,?)",
        (org_id, field_key.strip(), label.strip(), hint.strip(), int(required), maxorder + 1),
    )
    conn.commit()
    fid = cur.lastrowid
    conn.close()
    return {"id": fid}


@app.put("/api/fields/{field_id}")
def update_field(
    field_id: int,
    label: str = Form(...),
    hint: str = Form(""),
    required: int = Form(0),
    active: int = Form(1),
    user=Depends(current_user),
):
    oid = org_id_for_field(field_id)
    if oid is None: raise HTTPException(status_code=404, detail="Field not found")
    require_org_access(user, oid)
    conn = db.get_db()
    conn.execute(
        "UPDATE extraction_fields SET label=?, hint=?, required=?, active=? WHERE id=?",
        (label.strip(), hint.strip(), int(required), int(active), field_id),
    )
    conn.commit()
    conn.close()
    return {"ok": True}


@app.delete("/api/fields/{field_id}")
def delete_field(field_id: int, user=Depends(current_user)):
    oid = org_id_for_field(field_id)
    if oid is None: raise HTTPException(status_code=404, detail="Field not found")
    require_org_access(user, oid)
    # Soft delete: mark inactive so old invoices keep their data.
    conn = db.get_db()
    conn.execute("UPDATE extraction_fields SET active = 0 WHERE id = ?", (field_id,))
    conn.commit()
    conn.close()
    return {"ok": True}


# --------------------------------------------------------------------------
# Invoices
# --------------------------------------------------------------------------
@app.get("/api/orgs/{org_id}/invoices")
def list_invoices(org_id: int, user=Depends(current_user)):
    require_org_access(user, org_id)
    conn = db.get_db()
    rows = conn.execute(
        "SELECT * FROM invoices WHERE org_id = ? ORDER BY id DESC", (org_id,)
    ).fetchall()
    conn.close()
    return [_invoice_to_dict(r) for r in rows]


@app.post("/api/orgs/{org_id}/upload")
async def upload(org_id: int, file: UploadFile = File(...), user=Depends(current_user)):
    require_org_access(user, org_id)
    save_path = os.path.join(UPLOAD_DIR, f"{org_id}_{file.filename}")
    with open(save_path, "wb") as out:
        out.write(await file.read())
    invoice = process_invoice_file(org_id, save_path, file.filename, "web")
    return invoice


@app.delete("/api/invoices/{invoice_id}")
def delete_invoice(invoice_id: int, user=Depends(current_user)):
    """Permanently remove an invoice and its audit history."""
    oid = org_id_for_invoice(invoice_id)
    if oid is None: raise HTTPException(status_code=404, detail="Invoice not found")
    require_org_access(user, oid)
    conn = db.get_db()
    row = conn.execute("SELECT id FROM invoices WHERE id = ?", (invoice_id,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Invoice not found")
    conn.execute("DELETE FROM audit_log WHERE invoice_id = ?", (invoice_id,))
    conn.execute("DELETE FROM invoices WHERE id = ?", (invoice_id,))
    conn.commit()
    conn.close()
    return {"ok": True}


@app.post("/api/orgs/{org_id}/invoices/bulk_delete")
async def bulk_delete_invoices(org_id: int, ids: str = Form(...), user=Depends(current_user)):
    require_org_access(user, org_id)
    """Delete several invoices at once. ids = comma-separated invoice IDs."""
    id_list = [int(x) for x in ids.split(",") if x.strip().isdigit()]
    if not id_list:
        return {"ok": True, "deleted": 0}
    conn = db.get_db()
    placeholders = ",".join("?" * len(id_list))
    conn.execute(
        f"DELETE FROM audit_log WHERE invoice_id IN ({placeholders})", id_list
    )
    conn.execute(
        f"DELETE FROM invoices WHERE id IN ({placeholders}) AND org_id = ?",
        id_list + [org_id],
    )
    conn.commit()
    conn.close()
    return {"ok": True, "deleted": len(id_list)}


@app.get("/api/invoices/{invoice_id}")
def get_invoice(invoice_id: int, user=Depends(current_user)):
    oid = org_id_for_invoice(invoice_id)
    if oid is None: raise HTTPException(status_code=404, detail="Invoice not found")
    require_org_access(user, oid)
    conn = db.get_db()
    row = conn.execute("SELECT * FROM invoices WHERE id = ?", (invoice_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="Invoice not found")
    return _invoice_to_dict(row)


@app.post("/api/invoices/{invoice_id}/correct")
async def correct(
    invoice_id: int,
    field_name: str = Form(...),
    new_value: str = Form(...),
    changed_by: str = Form("admin"),
    user=Depends(current_user),
):
    oid = org_id_for_invoice(invoice_id)
    if oid is None: raise HTTPException(status_code=404, detail="Invoice not found")
    require_org_access(user, oid)
    conn = db.get_db()
    row = conn.execute("SELECT fields_json FROM invoices WHERE id=?", (invoice_id,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Invoice not found")
    fields = json.loads(row["fields_json"] or "{}")
    old = fields.get(field_name)
    fields[field_name] = new_value
    conn.execute(
        "UPDATE invoices SET fields_json=?, status='corrected' WHERE id=?",
        (json.dumps(fields), invoice_id),
    )
    conn.execute(
        "INSERT INTO audit_log (invoice_id, action, field_name, old_value, new_value, "
        "changed_by, changed_at) VALUES (?,?,?,?,?,?,?)",
        (invoice_id, "corrected", field_name, str(old), str(new_value), changed_by, db.now_iso()),
    )
    conn.commit()
    conn.close()
    return {"ok": True}


@app.post("/api/invoices/{invoice_id}/reextract")
def reextract(invoice_id: int, instruction: str = Form(""), changed_by: str = Form("admin"), user=Depends(current_user)):
    oid0 = org_id_for_invoice(invoice_id)
    if oid0 is None: raise HTTPException(status_code=404, detail="Invoice not found")
    require_org_access(user, oid0)
    conn = db.get_db()
    row = conn.execute("SELECT * FROM invoices WHERE id=?", (invoice_id,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Invoice not found")
    org_id = row["org_id"]
    old_fields = json.loads(row["fields_json"] or "{}")
    fields_cfg = db.get_active_fields(org_id)
    file_path = row["file_path"]
    conn.close()

    # Re-run extraction from the ORIGINAL file so re-extract also uses vision
    # for images, applying the reviewer's instruction.
    result = extraction.extract_from_file(file_path, fields_cfg, instruction)

    conn = db.get_db()
    conn.execute(
        "UPDATE invoices SET fields_json=?, confidence_json=?, line_items_json=?, "
        "status='corrected' WHERE id=?",
        (
            json.dumps(result["fields"]),
            json.dumps(result["confidence"]),
            json.dumps(result["line_items"]),
            invoice_id,
        ),
    )
    conn.execute(
        "INSERT INTO audit_log (invoice_id, action, field_name, old_value, new_value, "
        "changed_by, changed_at) VALUES (?,?,?,?,?,?,?)",
        (invoice_id, "reextracted", "(all fields)", json.dumps(old_fields),
         json.dumps(result["fields"]), changed_by, db.now_iso()),
    )
    conn.commit()
    conn.close()
    return {
        "ok": True,
        "fields": result["fields"],
        "confidence": result["confidence"],
        "diagnostic": result.get("diagnostic"),
    }


@app.post("/api/invoices/{invoice_id}/approve")
def approve(invoice_id: int, changed_by: str = Form("admin"), user=Depends(current_user)):
    oid = org_id_for_invoice(invoice_id)
    if oid is None: raise HTTPException(status_code=404, detail="Invoice not found")
    require_org_access(user, oid)
    conn = db.get_db()
    row = conn.execute("SELECT * FROM invoices WHERE id=?", (invoice_id,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Invoice not found")
    conn.execute("UPDATE invoices SET status='approved' WHERE id=?", (invoice_id,))
    conn.execute(
        "INSERT INTO audit_log (invoice_id, action, field_name, old_value, new_value, "
        "changed_by, changed_at) VALUES (?,?,?,?,?,?,?)",
        (invoice_id, "approved", "status", row["status"], "approved", changed_by, db.now_iso()),
    )
    conn.commit()
    conn.close()

    # If this invoice came in via Telegram, send a confirmation back to that chat.
    if row["source"] == "telegram" and row["telegram_chat_id"]:
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        if token:
            try:
                import telegram_bot
                f = json.loads(row["fields_json"] or "{}")
                telegram_bot._send(
                    token, row["telegram_chat_id"],
                    "Approved ✓\n"
                    f"Vendor: {f.get('vendor', '-')}\n"
                    f"Amount: {f.get('amount', '-')} {f.get('currency', '') or ''}\n"
                    f"Approved by: {changed_by}",
                )
            except Exception as e:
                print("Could not send Telegram approval notice:", repr(e))

    return {"ok": True}


@app.get("/api/invoices/{invoice_id}/history")
def history(invoice_id: int, user=Depends(current_user)):
    oid = org_id_for_invoice(invoice_id)
    if oid is None: raise HTTPException(status_code=404, detail="Invoice not found")
    require_org_access(user, oid)
    conn = db.get_db()
    rows = conn.execute(
        "SELECT action, field_name, old_value, new_value, changed_by, changed_at "
        "FROM audit_log WHERE invoice_id=? ORDER BY id DESC",
        (invoice_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------
# Analytics (innovation #2)
# --------------------------------------------------------------------------
@app.get("/api/orgs/{org_id}/analytics")
def analytics(org_id: int, user=Depends(current_user)):
    require_org_access(user, org_id)
    conn = db.get_db()
    rows = conn.execute("SELECT * FROM invoices WHERE org_id=?", (org_id,)).fetchall()
    conn.close()

    total = len(rows)
    approved = sum(1 for r in rows if r["status"] == "approved")
    needs_review = sum(1 for r in rows if r["status"] in ("needs_review", "corrected"))
    duplicates = sum(1 for r in rows if r["is_duplicate"])

    by_vendor = {}
    by_month = {}
    total_value = 0.0
    for r in rows:
        f = json.loads(r["fields_json"] or "{}")
        vendor = (f.get("vendor") or "Unknown").strip() or "Unknown"
        amount = _to_float(f.get("amount"))
        total_value += amount
        by_vendor[vendor] = by_vendor.get(vendor, 0.0) + amount
        month = (f.get("date") or "")[:7] or "unknown"
        by_month[month] = by_month.get(month, 0.0) + amount

    top_vendors = sorted(by_vendor.items(), key=lambda x: x[1], reverse=True)[:6]
    months = sorted([m for m in by_month if m != "unknown"])

    return {
        "total": total,
        "approved": approved,
        "needs_review": needs_review,
        "duplicates": duplicates,
        "total_value": round(total_value, 3),
        "top_vendors": [{"vendor": v, "amount": round(a, 3)} for v, a in top_vendors],
        "by_month": [{"month": m, "amount": round(by_month[m], 3)} for m in months],
    }


def _to_float(v) -> float:
    """Best-effort convert a messy amount string to a number."""
    if v is None:
        return 0.0
    s = str(v).replace(",", "").strip()
    cleaned = "".join(c for c in s if (c.isdigit() or c == "."))
    try:
        return float(cleaned) if cleaned else 0.0
    except ValueError:
        return 0.0


# --------------------------------------------------------------------------
# Export
# --------------------------------------------------------------------------
@app.get("/api/orgs/{org_id}/export")
def export(org_id: int, format: str = "xlsx", scope: str = "approved", user=Depends(current_user)):
    require_org_access(user, org_id)
    conn = db.get_db()
    if scope == "approved":
        rows = conn.execute(
            "SELECT * FROM invoices WHERE org_id=? AND status='approved' ORDER BY id",
            (org_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM invoices WHERE org_id=? ORDER BY id", (org_id,)
        ).fetchall()
    field_defs = db.get_active_fields(org_id)
    conn.close()

    headers = ["id", "status", "source"] + [f["field_key"] for f in field_defs]
    data_rows = []
    for r in rows:
        f = json.loads(r["fields_json"] or "{}")
        data_rows.append(
            [r["id"], r["status"], r["source"]] + [f.get(fd["field_key"], "") for fd in field_defs]
        )

    if format == "csv":
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(headers)
        w.writerows(data_rows)
        buf.seek(0)
        return StreamingResponse(
            iter([buf.getvalue()]),
            media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename=invoices_{scope}.csv"},
        )
    else:
        from openpyxl import Workbook
        wb = Workbook()
        ws = wb.active
        ws.title = "Invoices"
        ws.append(headers)
        for dr in data_rows:
            ws.append(dr)
        bio = io.BytesIO()
        wb.save(bio)
        bio.seek(0)
        return StreamingResponse(
            bio,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f"attachment; filename=invoices_{scope}.xlsx"},
        )


# --------------------------------------------------------------------------
# Telegram link management (map a chat to an org)
# --------------------------------------------------------------------------
@app.get("/api/telegram/bot-info")
def telegram_bot_info(user=Depends(current_user)):
    """
    Tell the dashboard which Telegram bot to message. Bots aren't publicly
    searchable - the only way anyone finds it is by being told its exact
    username, so we surface it here instead of making people ask the admin.
    """
    username = None
    try:
        import telegram_bot
        username = telegram_bot.BOT_USERNAME
    except Exception:
        pass
    return {"username": username, "configured": bool(os.environ.get("TELEGRAM_BOT_TOKEN", "").strip())}


@app.get("/api/orgs/{org_id}/telegram")
def list_links(org_id: int, user=Depends(current_user)):
    require_org_access(user, org_id)
    conn = db.get_db()
    rows = conn.execute("SELECT * FROM telegram_links WHERE org_id=?", (org_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.post("/api/orgs/{org_id}/telegram")
def add_link(org_id: int, telegram_chat_id: str = Form(...), label: str = Form(""), user=Depends(current_user)):
    require_org_access(user, org_id)
    conn = db.get_db()
    try:
        conn.execute(
            "INSERT INTO telegram_links (org_id, telegram_chat_id, label, created_at) "
            "VALUES (?,?,?,?)",
            (org_id, telegram_chat_id.strip(), label.strip(), db.now_iso()),
        )
        conn.commit()
    except Exception as e:
        conn.close()
        raise HTTPException(status_code=400, detail="That chat ID is already linked to an organization.")
    conn.close()
    return {"ok": True}


@app.delete("/api/orgs/{org_id}/telegram/{link_id}")
def remove_link(org_id: int, link_id: int, user=Depends(current_user)):
    """
    Unlink a Telegram chat from this org. Lets you move a chat to a different
    organization, since a chat can only be linked to one org at a time.
    """
    require_org_access(user, org_id)
    conn = db.get_db()
    row = conn.execute(
        "SELECT id FROM telegram_links WHERE id = ? AND org_id = ?", (link_id, org_id)
    ).fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Link not found in this organization.")
    conn.execute("DELETE FROM telegram_links WHERE id = ?", (link_id,))
    conn.commit()
    conn.close()
    return {"ok": True}


# Serve uploaded files so the dashboard can preview them.
app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")
