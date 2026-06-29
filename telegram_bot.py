"""
telegram_bot.py - the Telegram side of intake.

How it works (long polling - the simplest reliable method):
  - We repeatedly ask Telegram "any new messages?" (getUpdates).
  - When someone sends a PDF/photo, we download it, figure out which
    organization that chat belongs to, run it through the SAME processing
    pipeline the website uses, and reply with the extracted summary.
  - If the chat isn't linked to any org yet, we reply with the chat's ID so
    an admin can link it in the dashboard (Telegram tab). This closes the
    security gap of accepting invoices from unknown chats.

We talk to Telegram with plain HTTP calls (httpx) - no heavy library needed.
"""

import os
import json
import time
import tempfile

import httpx

import db

# Filled in once at startup by run_bot(), so other parts of the app (the
# dashboard) can tell users exactly which bot to message.
BOT_USERNAME = None


def fetch_bot_username(token: str):
    """Ask Telegram 'who am I' once, so we can show the bot's @username in the UI."""
    try:
        resp = httpx.get(_api(token, "getMe"), timeout=15)
        data = resp.json()
        return data.get("result", {}).get("username")
    except Exception as e:
        print("Could not fetch bot username:", repr(e))
        return None


def _api(token: str, method: str) -> str:
    return f"https://api.telegram.org/bot{token}/{method}"


def _file_url(token: str, file_path: str) -> str:
    return f"https://api.telegram.org/file/bot{token}/{file_path}"


def _org_for_chat(chat_id: str):
    """Return the org_id linked to this Telegram chat, or None."""
    conn = db.get_db()
    row = conn.execute(
        "SELECT org_id FROM telegram_links WHERE telegram_chat_id = ?", (str(chat_id),)
    ).fetchone()
    conn.close()
    return row["org_id"] if row else None


def _send(token: str, chat_id, text: str):
    try:
        httpx.post(_api(token, "sendMessage"),
                   json={"chat_id": chat_id, "text": text}, timeout=20)
    except Exception as e:
        print("Telegram send error:", e)


def run_bot(process_fn):
    """
    Main loop. `process_fn` is main.process_invoice_file, passed in so the bot
    reuses the exact same extraction + storage logic as the website.
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        print("Telegram: no token, not starting.")
        return

    global BOT_USERNAME
    BOT_USERNAME = fetch_bot_username(token)
    if BOT_USERNAME:
        print(f"Telegram: bot username is @{BOT_USERNAME}")

    offset = 0  # tracks which updates we've already seen
    print("Telegram: polling for messages...")

    while True:
        try:
            resp = httpx.get(
                _api(token, "getUpdates"),
                params={"offset": offset, "timeout": 25},
                timeout=30,
            )
            updates = resp.json().get("result", [])
        except Exception as e:
            print("Telegram poll error:", e)
            time.sleep(3)
            continue

        for upd in updates:
            offset = upd["update_id"] + 1
            msg = upd.get("message") or {}
            chat = msg.get("chat", {})
            chat_id = chat.get("id")
            if chat_id is None:
                continue

            # /start or any text -> greet and show how to link
            if "text" in msg and msg["text"].startswith("/start"):
                org = _org_for_chat(chat_id)
                if org:
                    _send(token, chat_id, "InvoiceAI ready. Send a PDF, JPG, or PNG invoice.")
                else:
                    _send(token, chat_id,
                          "Welcome to InvoiceAI.\n\n"
                          f"This chat's ID is: {chat_id}\n"
                          "Ask your admin to link this ID to your organization "
                          "in the dashboard (Telegram tab). Then send an invoice.")
                continue

            # Find a file in the message (document or the largest photo size)
            file_id, filename = None, None
            if "document" in msg:
                file_id = msg["document"]["file_id"]
                filename = msg["document"].get("file_name", "invoice.pdf")
            elif "photo" in msg:
                file_id = msg["photo"][-1]["file_id"]  # last = highest resolution
                filename = "invoice.jpg"

            if not file_id:
                continue

            org_id = _org_for_chat(chat_id)
            if not org_id:
                _send(token, chat_id,
                      f"This chat isn't linked yet. Your chat ID is {chat_id}. "
                      "Ask your admin to link it in the dashboard.")
                continue

            # Download the file from Telegram
            try:
                gf = httpx.get(_api(token, "getFile"),
                               params={"file_id": file_id}, timeout=30).json()
                tg_path = gf["result"]["file_path"]
                content = httpx.get(_file_url(token, tg_path), timeout=60).content
            except Exception as e:
                _send(token, chat_id, "Could not download that file. Try again.")
                print("Telegram download error:", e)
                continue

            # Save into the shared uploads folder
            uploads = os.path.join(os.path.dirname(__file__), "uploads")
            safe_name = f"tg_{chat_id}_{int(time.time())}_{filename}"
            save_path = os.path.join(uploads, safe_name)
            with open(save_path, "wb") as out:
                out.write(content)

            # Run the SAME pipeline the website uses
            try:
                inv = process_fn(org_id, save_path, filename, "telegram", str(chat_id))
            except Exception as e:
                _send(token, chat_id, "Something went wrong processing that invoice.")
                print("Telegram process error:", e)
                continue

            # Reply with a short summary
            f = inv["fields"]
            dup_note = "\n(Looks like a DUPLICATE of an earlier invoice.)" if inv["is_duplicate"] else ""
            diag = inv.get("diagnostic")
            if diag:
                _send(token, chat_id,
                      "Processed, but extraction came back empty.\n\n"
                      f"Reason: {diag}\n\n"
                      "Fix that and resend this same file to try again. "
                      "Open the dashboard to see the raw entry.")
            else:
                _send(token, chat_id,
                      "Processed.\n"
                      f"Vendor: {f.get('vendor', '-')}\n"
                      f"Invoice #: {f.get('invoice_number', '-')}\n"
                      f"Amount: {f.get('amount', '-')} {f.get('currency', '') or ''}\n"
                      f"Date: {f.get('date', '-')}{dup_note}\n\n"
                      "Open the dashboard to review and approve.")
