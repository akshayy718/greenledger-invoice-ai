"""
extraction.py - turning a file into structured invoice data.

Two paths:
  - PDFs with a text layer   -> read text directly, send text to a text model
  - Images (and scanned PDFs)-> send the IMAGE ITSELF to a VISION model that
                                "looks" at the picture and reads it directly.

The vision path is what makes crumpled, angled, real-world photos work. OCR
reads literal pixels and fails on messy photos; a vision model reasons about
what it's seeing (e.g. reads a blurry "ALOOLA" as "Aloola Petrol Station").

Models are named at the top so you can swap them when Groq deprecates one.
"""

import os
import io
import json
import base64
import hashlib

import pdfplumber
from PIL import Image, ImageOps

from groq import Groq

# --- Models. Update these when Groq deprecates one (console.groq.com/docs/models)
TEXT_MODEL = "llama-3.3-70b-versatile"                       # for PDF text
VISION_MODEL = "meta-llama/llama-4-scout-17b-16e-instruct"   # for images


def _client():
    key = os.environ.get("GROQ_API_KEY", "").strip()
    return Groq(api_key=key) if key else None


# --------------------------------------------------------------------------
# Reading helpers
# --------------------------------------------------------------------------
def read_text(path: str) -> str:
    """Pull a text layer out of a PDF. Returns '' if there's no text layer."""
    if path.lower().endswith(".pdf"):
        parts = []
        with pdfplumber.open(path) as pdf:
            for page in pdf.pages:
                parts.append(page.extract_text() or "")
        return "\n".join(parts).strip()
    return ""  # images have no text layer; they go through the vision path


def _is_image(path: str) -> bool:
    return path.lower().endswith((".png", ".jpg", ".jpeg", ".webp"))


def _prepare_image_b64(path: str) -> str:
    """
    Load an image, fix common phone-photo problems, and return base64.
      - Respect EXIF orientation so sideways phone photos are turned upright.
      - Convert to RGB (vision API wants a standard JPEG).
      - Downscale if huge, to stay under Groq's request size limit.
    """
    img = Image.open(path)
    img = ImageOps.exif_transpose(img)   # auto-rotate to upright
    img = img.convert("RGB")
    longest = max(img.size)
    if longest > 1600:
        scale = 1600 / longest
        img = img.resize((int(img.width * scale), int(img.height * scale)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=88)
    return base64.b64encode(buf.getvalue()).decode("utf-8")


# --------------------------------------------------------------------------
# Prompt + result shaping
# --------------------------------------------------------------------------
def _field_instructions(fields: list[dict], instruction: str = "") -> str:
    lines = []
    for f in fields:
        hint = f" (hint: {f['hint']})" if f.get("hint") else ""
        lines.append(f'  - {f["field_key"]}: {f["label"]}{hint}')
    field_block = "\n".join(lines)
    extra = f"\nExtra instruction from a reviewer: {instruction}\n" if instruction else ""
    return (
        "You are an invoice/receipt data extraction engine. "
        "Decide if this is an invoice or receipt, then extract these fields:\n"
        f"{field_block}\n\n"
        "Also extract line items (the individual products/services billed).\n"
        f"{extra}\n"
        "Return EXACTLY this JSON shape and nothing else:\n"
        "{\n"
        '  "is_invoice_document": true/false,\n'
        '  "document_type": "invoice" | "receipt" | "other",\n'
        '  "fields": { "<field_key>": "<value or null>", ... },\n'
        '  "confidence": { "<field_key>": 0.0-1.0, ... },\n'
        '  "line_items": [ {"description":"", "qty":"", "unit_price":"", "amount":""} ]\n'
        "}\n"
        "Use null for missing values and 0.0 confidence for them. "
        "Dates as YYYY-MM-DD. Amounts as numbers only."
    )


def _blank(fields, diag=None):
    return {
        "is_invoice_document": False,
        "document_type": "unknown",
        "fields": {f["field_key"]: None for f in fields},
        "confidence": {f["field_key"]: 0.0 for f in fields},
        "line_items": [],
        "diagnostic": diag,
    }


def _normalize(data: dict, fields: list[dict]) -> dict:
    """Ensure every configured field exists, even if the model skipped one."""
    out_fields = {f["field_key"]: None for f in fields}
    out_conf = {f["field_key"]: 0.0 for f in fields}
    for f in fields:
        k = f["field_key"]
        if isinstance(data.get("fields"), dict) and k in data["fields"]:
            out_fields[k] = data["fields"][k]
        if isinstance(data.get("confidence"), dict) and k in data["confidence"]:
            try:
                out_conf[k] = float(data["confidence"][k])
            except (TypeError, ValueError):
                out_conf[k] = 0.0
    return {
        "is_invoice_document": bool(data.get("is_invoice_document", True)),
        "document_type": data.get("document_type", "unknown"),
        "fields": out_fields,
        "confidence": out_conf,
        "line_items": data.get("line_items", []) or [],
        "diagnostic": None,
    }


def _call_vision(client, b64: str, prompt: str, fields):
    resp = client.chat.completions.create(
        model=VISION_MODEL,
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url",
                 "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
            ],
        }],
        temperature=0,
        response_format={"type": "json_object"},
    )
    raw = resp.choices[0].message.content.strip().replace("```json", "").replace("```", "").strip()
    return _normalize(json.loads(raw), fields)


# --------------------------------------------------------------------------
# Main entry point
# --------------------------------------------------------------------------
def extract_from_file(path: str, fields: list[dict], instruction: str = "") -> dict:
    """
    Decide path based on file type, call the right Groq model, return structured
    data. Always returns the same shape, with a 'diagnostic' explaining failures.
    """
    client = _client()
    if client is None:
        return _blank(fields, "GROQ_API_KEY is not set in this server process.")

    prompt = _field_instructions(fields, instruction)

    # ---- IMAGE PATH: send the picture to the vision model ----
    if _is_image(path):
        try:
            b64 = _prepare_image_b64(path)
        except Exception as e:
            return _blank(fields, f"Could not read the image: {e}")
        try:
            return _call_vision(client, b64, prompt, fields)
        except Exception as e:
            print("Vision extraction error:", repr(e))
            return _blank(fields, f"Vision AI call failed: {e}")

    # ---- PDF PATH ----
    text = read_text(path)

    # Scanned PDF with no text layer -> render page 1 and use vision.
    if not text:
        try:
            with pdfplumber.open(path) as pdf:
                img = pdf.pages[0].to_image(resolution=200).original
            buf = io.BytesIO()
            img.convert("RGB").save(buf, format="JPEG", quality=88)
            b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
            return _call_vision(client, b64, prompt, fields)
        except Exception as e:
            print("Scanned PDF error:", repr(e))
            return _blank(fields, f"Scanned PDF could not be read: {e}")

    # Normal text PDF -> text model.
    try:
        resp = client.chat.completions.create(
            model=TEXT_MODEL,
            messages=[{"role": "user", "content": prompt + "\n\nDOCUMENT TEXT:\n" + text}],
            temperature=0,
            response_format={"type": "json_object"},
        )
        raw = resp.choices[0].message.content.strip().replace("```json", "").replace("```", "").strip()
        return _normalize(json.loads(raw), fields)
    except Exception as e:
        print("Text extraction error:", repr(e))
        return _blank(fields, f"AI call failed: {e}")


def make_doc_hash(fields: dict) -> str:
    """Fingerprint = vendor + invoice number + amount, for duplicate detection."""
    parts = [
        str(fields.get("vendor", "") or "").strip().lower(),
        str(fields.get("invoice_number", "") or "").strip().lower(),
        str(fields.get("amount", "") or "").strip().lower(),
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
