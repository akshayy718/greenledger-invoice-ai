# InvoiceAI — Operations

A multi-organization invoice intake, review, and export system. Invoices arrive
by **web upload** or **Telegram**, get read by AI into configurable fields,
are reviewed and corrected by a human with an audit trail, then exported clean
for accounting.

This is the full version. The backend (FastAPI + SQLite) and the bot are code
you wrote and can explain — that's the point.

## What it does

- **Multi-organization** — each client has its own invoices and field setup.
- **Two intake channels** — upload in the dashboard, or send to a Telegram bot.
- **Configurable extraction fields** — per org, with optional AI hints, required
  flags, and active/off toggles. No fields are hardcoded.
- **AI extraction with confidence scores** (innovation #1) — every field shows
  how sure the AI is; low-confidence fields are visibly flagged for review.
- **Dashboard analytics** (innovation #2) — totals, approved vs. pending,
  spend by vendor, spend by month.
- **Duplicate detection** (innovation #3) — a fingerprint of vendor + invoice #
  + amount flags repeat invoices.
- **Line-item extraction** (innovation #4) — the individual billed items, not
  just the header fields.
- **Review workflow** — correct any field, re-extract with a typed hint,
  approve, and see a full edit history (who changed what, when).
- **Export** — approved invoices to Excel, or everything to CSV.

## The files

| File | Role |
|------|------|
| `main.py` | The web server and all API endpoints. |
| `db.py` | Database schema and helpers (SQLite). |
| `extraction.py` | Reading files + AI extraction + duplicate fingerprint. |
| `telegram_bot.py` | The Telegram bot (runs in the background). |
| `static/index.html` | The dashboard (the whole UI). |
| `requirements.txt` | Libraries to install. |

## How it fits together

```
            ┌─────────────┐         ┌──────────────┐
  Telegram ─►             │         │              │
            │  main.py    ├────────►│ extraction.py│──► AI (Groq)
  Web upload►  (FastAPI)  │         │              │
            │             │◄────────┤   reads PDF/  │
            └──────┬──────┘ fields  │   image text  │
                   │               └──────────────┘
                   ▼
              db.py (SQLite)  ──► invoices, fields, audit log, orgs
                   │
                   ▼
          static/index.html  (dashboard: review, approve, analytics, export)
```

Both intake channels call the SAME `process_invoice_file()` function, so
Telegram and web uploads behave identically.

## Running it (Windows / PowerShell)

You have two Python versions installed; use the one your commands actually run.
Earlier, `python` pointed to 3.14, so these use `python -m` to stay consistent.

```powershell
# 1. install libraries into the same Python you run with
python -m pip install -r requirements.txt

# 2. (only for image invoices) install the Tesseract OCR engine
#    download the Windows installer from the Tesseract OCR GitHub releases page.
#    PDFs work without it.

# 3. set your keys for this terminal session
$env:GROQ_API_KEY="your_groq_key"          # required, free at console.groq.com
$env:TELEGRAM_BOT_TOKEN="your_bot_token"    # optional, from @BotFather

# 4. run the whole product (web + bot together)
python -m uvicorn main:app --reload

# 5. open the dashboard
#    http://127.0.0.1:8000
```

If `TELEGRAM_BOT_TOKEN` is not set, the app runs web-only — everything works
except the bot. Add the token later and restart to turn the bot on.

## First steps in the dashboard

1. A **Demo Company** org is created automatically with sensible default fields.
2. Go to **Invoices → Upload invoice** and drop in a PDF to see extraction,
   confidence meters, and line items.
3. Go to **Extraction fields** to add a field (e.g. `vehicle_number`) — it'll
   be used on the next extraction.
4. For Telegram: send `/start` to your bot, copy the chat ID it replies, and
   paste it under **Telegram intake → Link a chat**. Then send an invoice photo.

## Honest limitations (say these in an interview)

- Authentication is not built yet — anyone who can reach the dashboard can use
  it. Real deployment needs login + per-user org permissions.
- Telegram routing is by linked chat ID, which is the access control: unlinked
  chats are refused. That's deliberate, but it's basic.
- SQLite is single-file and fine for this scale; a real multi-tenant production
  system would move to PostgreSQL with row-level isolation.
