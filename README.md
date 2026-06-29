<div align="center">

<img src="https://img.shields.io/badge/Status-Live-1F6F4F?style=for-the-badge" />
<img src="https://img.shields.io/badge/Python-3.12-1F6F4F?style=for-the-badge&logo=python&logoColor=white" />
<img src="https://img.shields.io/badge/FastAPI-Backend-1F6F4F?style=for-the-badge&logo=fastapi&logoColor=white" />
<img src="https://img.shields.io/badge/Groq-Vision%20AI-1F6F4F?style=for-the-badge" />
<img src="https://img.shields.io/badge/Telegram-Bot-1F6F4F?style=for-the-badge&logo=telegram&logoColor=white" />

<br/><br/>

<h1>🌿 Greenledger AI</h1>

<p><b>An AI-powered invoice intake, review, and approval system —<br/>built with configurable extraction, multi-tenant auth, and a live Telegram bot.</b></p>

<p>
  <a href="https://greenledger-invoice-ai.onrender.com"><b>🔗 Live Demo</b></a> ·
  <a href="#-features">Features</a> ·
  <a href="#-architecture">Architecture</a> ·
  <a href="#-running-locally">Run locally</a> ·
  <a href="#-honest-limitations">Limitations</a>
</p>

</div>

<br/>

> **Demo note:** hosted on a free tier, so the app may take 30–60 seconds to wake up on first load, and data resets periodically. Login: `demo@greenledger.ai` / `demo1234` — or sign up fresh, it's instant.

---

## 📌 The problem

Every business handles invoices differently — different fields, different formats, different review processes. Most "AI invoice extraction" demos hardcode a fixed set of fields and assume every document looks the same. **Greenledger doesn't.**

It's built around one core idea: **an admin defines what fields matter for their organization** — vendor, invoice number, tax, vehicle number, PO number, whatever — and the AI extracts exactly that, with confidence scores so a human knows what to double-check. Every correction is logged. Nothing gets approved silently.

## ✨ Features

| | |
|---|---|
| 🔐 **Real authentication** | Email/password signup, salted password hashing (PBKDF2), session cookies. Every organization's data is access-controlled — users can only ever see orgs they belong to. |
| 🏢 **Multi-tenant from the ground up** | One login can own multiple organizations. Each org has its own invoices, fields, and Telegram routing — fully isolated. |
| 🧩 **Configurable extraction fields** | No hardcoded schema. Add, edit, or deactivate fields per organization, with optional hints that steer the AI ("this is the long number labeled Invoice No, not Receipt No"). |
| 👁️ **Vision-model AI extraction** | Photos and PDFs are sent directly to a vision-capable LLM (Groq · Llama 4 Scout) instead of brittle OCR — it *reads* a blurry receipt the way a person would, auto-rotating sideways phone photos first. |
| 🎯 **Confidence scores** | Every extracted field shows how sure the AI is. Low-confidence fields are visibly flagged so a reviewer knows exactly what to check. |
| 🧾 **Line-item extraction** | Pulls the actual billed items — description, qty, price, amount — not just header totals. |
| 🔁 **Re-extraction with hints** | Got a field wrong? Type a correction hint and re-run extraction on the same document instead of fixing it by hand every time. |
| 🔍 **Duplicate detection** | Fingerprints vendor + invoice number + amount to flag likely-duplicate submissions automatically. |
| 🤖 **Live Telegram bot** | Send an invoice as a photo or document straight from your phone. The bot reads it, replies with a summary, and routes it to the right organization by chat ID — unlinked chats are refused. |
| 📝 **Full audit trail** | Every correction, re-extraction, and approval is logged with who changed what, old value → new value, and a timestamp. |
| 📊 **Analytics dashboard** | Totals, approval status, spend by vendor, spend by month — at a glance. |
| 📤 **Clean export** | One click to Excel or CSV, scoped to approved-only or everything, ready to hand to accounting. |

## 🏗️ Architecture

```
                    ┌──────────────┐
   Telegram photo ─►│              │
                     │   main.py    │──────► extraction.py ──► Groq Vision/Text AI
   Web upload ──────►│  (FastAPI)   │           ▲  reads PDF/image,
                     │              │           │  routes to the right model
                     └──────┬───────┘           │
                            │                    │
                            ▼                    │
                      db.py (SQLite) ◄────────────┘
                  users · orgs · memberships
              invoices · extraction_fields · audit_log
                            │
                            ▼
                 static/index.html (dashboard)
          review · approve · analytics · export · auth
```

Both intake channels — Telegram and web upload — call the **same** `process_invoice_file()` function, so a photo sent from a phone gets identical treatment to a file dropped in the browser.

## 🛠️ Tech stack

- **Backend:** FastAPI (Python), SQLite
- **AI:** Groq API — vision model for images, text model for typed PDFs
- **Bot:** Telegram Bot API (long polling), running in a background thread alongside the web server
- **Frontend:** Vanilla HTML/CSS/JS — no framework, no build step
- **Auth:** PBKDF2 password hashing, server-side sessions via httponly cookies
- **Deployment:** Render

## 🚀 Running locally

```bash
git clone https://github.com/akshayy718/greenledger-invoice-ai.git
cd greenledger-invoice-ai

pip install -r requirements.txt

# set your keys
export GROQ_API_KEY="your_groq_key"            # free at console.groq.com
export TELEGRAM_BOT_TOKEN="your_bot_token"      # optional — from @BotFather

uvicorn main:app --reload
```

Open `http://127.0.0.1:8000` — it redirects to a login page. Sign up, or use the auto-created demo login (`demo@greenledger.ai` / `demo1234`).

## 📁 Project structure

```
greenledger-invoice-ai/
├── main.py            # FastAPI app — all routes, auth, org access control
├── db.py               # SQLite schema, password hashing, session helpers
├── extraction.py        # File reading + AI extraction (vision & text paths)
├── telegram_bot.py      # Telegram long-polling bot, shares the extraction pipeline
├── static/
│   ├── index.html       # The dashboard (single-page, vanilla JS)
│   └── login.html        # Sign in / sign up
└── requirements.txt
```

## 🔍 What I'd build next

- Switch the Telegram bot from polling to **webhooks** — more efficient, and works on hosts that don't allow always-on background tasks.
- Self-service org linking (right now, a user must already be logged in to link a Telegram chat to *their own* org — there's no public invite flow for a stranger to join someone else's org).
- A proper relational database (Postgres) instead of SQLite, for real concurrent multi-user load.

## ⚠️ Honest limitations

- **Free-tier hosting** means the app sleeps after 15 minutes of inactivity (30–60s cold start) and the filesystem resets on restart — fine for a demo, not for production data.
- **Vision-model accuracy isn't perfect** on dense, cluttered, or extremely low-quality photos — the confidence scores and human-review step exist specifically because no AI extraction system gets every field right on every document. Built deliberately around verify-then-approve, not blind trust.
- **Telegram chat ↔ org linking is admin-only** — there's no public signup flow for a new chat to self-link to an org without already having dashboard access.

## 👤 Author

**Akshay Santhosh**
B.Tech Computer Science (AI), Jain University · AI/ML Engineer & SAP BTP Developer
[GitHub](https://github.com/akshayy718) · akshaysanthosh718@gmail.com

---

<div align="center"><sub>Built end-to-end — backend, AI pipeline, bot integration, auth, and deployment — as a real working system, not a tutorial demo.</sub></div>
