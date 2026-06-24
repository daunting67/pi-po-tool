import os
import re
import json
import tempfile
import functools

import pdfplumber
import requests as req
from flask import (
    Flask, request, jsonify, render_template,
    session, redirect, url_for, render_template_string,
)
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "change-me-in-production")
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024  # 20 MB upload limit

GROQ_API_KEY        = os.getenv("GROQ_API_KEY", "")
FASTFIELD_API_KEY   = os.getenv("FASTFIELD_API_KEY", "")
FASTFIELD_USERNAME  = os.getenv("FASTFIELD_USERNAME", "")
FASTFIELD_PASSWORD  = os.getenv("FASTFIELD_PASSWORD", "")
FASTFIELD_SESSION_TOKEN = os.getenv("FASTFIELD_SESSION_TOKEN", "")
FASTFIELD_FORM_ID   = os.getenv("FASTFIELD_FORM_ID", "668283")
FASTFIELD_RECIPIENT_EMAIL = os.getenv("FASTFIELD_RECIPIENT_EMAIL", "")
APP_PASSWORD        = os.getenv("APP_PASSWORD", "")
FASTFIELD_BASE      = "https://api.fastfieldforms.com/services/v3"

# ── Auth ──────────────────────────────────────────────────────────────────────

LOGIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>P&I — Login</title>
  <style>
    *{box-sizing:border-box;margin:0;padding:0}
    body{font-family:Arial,sans-serif;background:#f0f2f5;display:flex;align-items:center;justify-content:center;min-height:100vh}
    .card{background:#fff;border-radius:10px;padding:40px 36px;width:340px;box-shadow:0 2px 12px rgba(0,0,0,.1)}
    h1{font-size:1.1rem;color:#1a3a5c;margin-bottom:6px}
    p{font-size:.85rem;color:#666;margin-bottom:24px}
    label{font-size:.75rem;font-weight:700;color:#555;text-transform:uppercase;letter-spacing:.5px;display:block;margin-bottom:4px}
    input{width:100%;border:1px solid #d0dae5;border-radius:6px;padding:9px 11px;font-size:.95rem;margin-bottom:16px}
    input:focus{outline:none;border-color:#1a3a5c}
    button{width:100%;background:#1a3a5c;color:#fff;border:none;border-radius:7px;padding:11px;font-size:.95rem;font-weight:700;cursor:pointer}
    button:hover{opacity:.88}
    .err{color:#c0392b;font-size:.85rem;margin-bottom:12px}
  </style>
</head>
<body>
  <div class="card">
    <h1>P&amp;I Purchase Order</h1>
    <p>Enter the team password to continue</p>
    {% if error %}<div class="err">{{ error }}</div>{% endif %}
    <form method="post">
      <label>Password</label>
      <input type="password" name="password" autofocus required/>
      <button type="submit">Sign in</button>
    </form>
  </div>
</body>
</html>"""


def login_required(f):
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        if APP_PASSWORD and not session.get("authenticated"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        if request.form.get("password") == APP_PASSWORD:
            session["authenticated"] = True
            return redirect(url_for("index"))
        error = "Incorrect password — try again"
    return render_template_string(LOGIN_HTML, error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ── PDF text extraction ───────────────────────────────────────────────────────

def extract_pdf_text(path):
    pages = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            pages.append(text)
    return "\n\n--- PAGE BREAK ---\n\n".join(pages)


# ── AI extraction — handles ANY supplier format ───────────────────────────────

EXTRACTION_PROMPT = """You are extracting data from a vendor quote or invoice PDF to fill a purchase order form.

Return ONLY valid JSON — no markdown, no explanation:

{{
  "vendor_company": "name of the company that issued this quote",
  "vendor_contact": "name of the person who signed or sent the quote",
  "vendor_address": "street address of the vendor (not the customer)",
  "vendor_city": "city of the vendor",
  "vendor_phone": "vendor phone number",
  "quote_reference": "quote number or reference",
  "quote_date": "date of the quote",
  "line_items": [
    {{
      "description": "full item/service description",
      "unit": "unit of measure (e.g. EA, hr, m, LENGTH)",
      "quantity": "numeric quantity only",
      "rate": "unit price as a number only",
      "total": "line total as a number only"
    }}
  ],
  "subtotal": "subtotal before GST as a number only",
  "other_charges": "freight/delivery/other charges as a number only",
  "gst_amount": "GST amount as a number only",
  "total": "grand total including GST as a number only",
  "purpose": "1-2 sentence summary of what is being purchased and what project or job it is for"
}}

Rules:
- The CUSTOMER is Pipeline & Infrastructure (P&I) — do NOT put P&I details in vendor fields
- Use empty string "" for any field not found in the quote
- Strip all currency symbols — numbers only
- Extract ALL line items listed
- For purpose: describe what the items are and mention the project/job name if visible

Quote text:
{text}"""


def _parse_ai_response(raw):
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.rsplit("```", 1)[0]
    return json.loads(raw.strip())


def parse_with_groq(text):
    text = text[:15000]
    resp = req.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {GROQ_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "model": "llama-3.3-70b-versatile",
            "temperature": 0,
            "max_tokens": 4096,
            "messages": [
                {
                    "role": "system",
                    "content": "Extract structured data from vendor quotes. Return only valid JSON, no markdown.",
                },
                {"role": "user", "content": EXTRACTION_PROMPT.format(text=text)},
            ],
        },
        timeout=30,
    )
    resp.raise_for_status()
    return _parse_ai_response(resp.json()["choices"][0]["message"]["content"])


# ── FastField helpers ─────────────────────────────────────────────────────────

def fastfield_authenticate():
    resp = req.post(
        f"{FASTFIELD_BASE}/authenticate",
        headers={
            "FastField-API-Key": FASTFIELD_API_KEY,
            "Content-Length": "0",
        },
        auth=(FASTFIELD_USERNAME, FASTFIELD_PASSWORD),
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json().get("sessionToken", "")


def build_merge_fields(data):
    fields = {
        "alpha_9":   data.get("vendor_company", ""),   # Other: Name of Company Not Listed
        "contact":   data.get("vendor_contact", ""),   # Contact Person
        "address":   data.get("vendor_address", ""),   # Vendor Address
        "city":      data.get("vendor_city", ""),      # Vendor City
        "phone":     data.get("vendor_phone", ""),     # Vendor Phone
        "company1":  data.get("deliver_to_company", "P&I (North) Ltd"),
        "address1":  data.get("deliver_to_address", ""),
        "city1":     data.get("deliver_to_city", ""),
        "phone1":    data.get("deliver_to_phone", ""),
        "Att":       data.get("deliver_to_attention", ""),
        "why":       data.get("purpose", ""),          # Purpose / explanation
        "alpha_13":  data.get("client", ""),           # Client
    }
    line_items = data.get("line_items", [])
    if line_items:
        lines = []
        for item in line_items:
            desc = item.get("description", "")
            qty = item.get("quantity", "")
            unit = item.get("unit", "")
            rate = item.get("rate", "")
            total = item.get("total", "")
            lines.append(f"{desc} | Qty: {qty} {unit} | Rate: {rate} | Total: {total}")
        fields["des"] = "\n".join(lines)
    return [{"fieldKey": k, "value": str(v) if not isinstance(v, str) else v} for k, v in fields.items() if v]


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
@login_required
def index():
    return render_template("index.html")


@app.route("/extract", methods=["POST"])
@login_required
def extract():
    if "pdf" not in request.files:
        return jsonify({"error": "No PDF file provided"}), 400

    pdf_file = request.files["pdf"]
    if not pdf_file.filename.lower().endswith(".pdf"):
        return jsonify({"error": "File must be a PDF"}), 400

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        pdf_file.save(tmp.name)
        tmp_path = tmp.name

    try:
        text = extract_pdf_text(tmp_path)
        if not text.strip():
            return jsonify({"error": "Could not extract text — the PDF may be a scanned image"}), 400

        if not GROQ_API_KEY:
            return jsonify({"error": "GROQ_API_KEY not set — add it to your .env or Render environment variables"}), 500

        data = parse_with_groq(text)
        return jsonify({"success": True, "data": data})

    except json.JSONDecodeError:
        return jsonify({"error": "Could not parse the quote — try again"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        os.unlink(tmp_path)


@app.route("/submit", methods=["POST"])
@login_required
def submit():
    if not FASTFIELD_API_KEY:
        return jsonify({"error": "FastField API key not configured"}), 400

    data = request.json or {}

    try:
        token = FASTFIELD_SESSION_TOKEN or fastfield_authenticate()
    except Exception as e:
        return jsonify({"error": f"FastField authentication failed: {e}"}), 401

    recipient = data.get("assigned_to", "").strip()
    if not recipient:
        return jsonify({"error": "Please enter the FastField email address to assign this PO to"}), 400

    vendor = data.get("vendor_company", "Unknown Vendor")
    date = data.get("quote_date", "")
    dispatch_name = f"PO – {vendor}" + (f" – {date}" if date else "")

    purpose = data.get("purpose", "")
    ref = data.get("quote_reference", "")
    description = purpose or (f"Quote ref: {ref}" if ref else dispatch_name)

    payload = {
        "formId": int(FASTFIELD_FORM_ID),
        "name": dispatch_name,
        "description": description,
        "recipients": [{"email": recipient}],
        "mergeFields": build_merge_fields(data),
    }
    headers = {
        "Content-Type": "application/json",
        "X-Gatekeeper-SessionToken": token,
        "FastField-API-Key": FASTFIELD_API_KEY,
    }

    print(f"FastField dispatch payload: {json.dumps(payload)}", flush=True)
    resp = req.post(f"{FASTFIELD_BASE}/dispatch", headers=headers, json=payload, timeout=15)

    if resp.ok:
        try:
            rj = resp.json()
        except Exception:
            rj = {}
        print(f"FastField dispatch response: {json.dumps(rj)}", flush=True)
        dispatch_id = rj.get("id") or rj.get("dispatchId") or rj.get("dispatch_id") or "n/a"
        if isinstance(dispatch_id, dict):
            dispatch_id = str(dispatch_id)
        return jsonify({"success": True, "dispatch_id": dispatch_id, "raw": rj})
    print(f"FastField dispatch error {resp.status_code}: {resp.text}", flush=True)
    return jsonify({"error": f"FastField error {resp.status_code}: {resp.text}"}), resp.status_code


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5050))
    app.run(host="0.0.0.0", port=port, debug=os.getenv("FLASK_DEBUG", "false").lower() == "true")
