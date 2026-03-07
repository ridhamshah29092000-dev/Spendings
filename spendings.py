"""
SpendLens Backend - Flask API
Handles: CSV/PDF parsing, SMS parsing, categorization, email sending
"""
import pandas as pd
from flask import Flask, request, jsonify
from flask_cors import CORS
import csv, io, re, json, os, smtplib, hashlib
from datetime import datetime, date, timedelta
from collections import defaultdict
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from flask import Flask, request, jsonify, session
from functools import wraps

import hashlib

FAMILY_CODE = os.getenv("FAMILY_CODE", "SHAH2026")

def hash_password(p):
    return hashlib.sha256(p.encode()).hexdigest()

app = Flask(__name__)
CORS(app)

# In-memory database
DB = {
    "transactions": [],
    "settings": {
        "email": "",
        "sender_email": "",
        "gmail_app_password": ""
    }
}

from functools import wraps
from flask import session

app.secret_key = os.getenv("APP_SECRET", "change-this-secret")
APP_PASSWORD = os.getenv("SPENDLENS_PASSWORD", "family123")

# ── In-memory store (persists while server runs) ──────────────────────────────
import sqlite3

DB_FILE = "spendlens.db"

def db():
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():

    conn = db()

    conn.execute("""
    CREATE TABLE IF NOT EXISTS users(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE,
        password_hash TEXT
    )
    """)

    conn.execute("""
    CREATE TABLE IF NOT EXISTS transactions(
        id TEXT PRIMARY KEY,
        user_id INTEGER,
        date TEXT,
        description TEXT,
        amount REAL,
        type TEXT,
        balance REAL,
        category TEXT,
        source TEXT
    )
    """)

    conn.commit()
    conn.close()

@app.route("/api/verify-family", methods=["POST"])
def verify_family():

    data = request.json or {}
    code = data.get("code")

    if code == FAMILY_CODE:
        session["family_ok"] = True
        return jsonify({"ok": True})

    return jsonify({"error": "Invalid family code"}), 401   


@app.route("/api/login", methods=["POST"])
def login():

    if not session.get("family_ok"):
     return jsonify({"error":"Family access required"}),403

    data = request.json or {}
    username = data.get("username")
    password = data.get("password")

    conn = db()

    user = conn.execute(
        "SELECT * FROM users WHERE username=?",
        (username,)
    ).fetchone()

    conn.close()

    if user and user["password_hash"] == hash_password(password):

        session["user_id"] = user["id"]
        session["username"] = user["username"]

        return jsonify({"ok":True})

    return jsonify({"error":"Invalid login"}), 401

@app.route("/api/me")
def me():

    return jsonify({
        "logged_in": bool(session.get("user_id")),
        "family_ok": bool(session.get("family_ok")),
        "username": session.get("username")
    })

@app.route("/api/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"ok":True})


@app.route("/api/create-user", methods=["POST"])
def create_user():

    if not session.get("family_ok"):
        return jsonify({"error": "Family code required"}), 403

    data = request.json

    username = data["username"]
    password = hash_password(data["password"])

    conn = db()

    try:
        conn.execute(
            "INSERT INTO users(username,password_hash) VALUES(?,?)",
            (username,password)
        )
        conn.commit()
    except:
        return jsonify({"error":"User exists"}), 400

    conn.close()

    return jsonify({"ok":True})

@app.route("/api/reset-password", methods=["POST"])
def reset_password():

    data = request.json or {}

    username = data.get("username")
    new_password = data.get("password")
    code = data.get("family_code")

    if code != FAMILY_CODE:
        return jsonify({"error":"Invalid family code"}),401

    conn = db()

    user = conn.execute(
        "SELECT * FROM users WHERE username=?",
        (username,)
    ).fetchone()

    if not user:
        conn.close()
        return jsonify({"error":"User not found"}),404

    conn.execute(
        "UPDATE users SET password_hash=? WHERE username=?",
        (hash_password(new_password), username)
    )

    conn.commit()
    conn.close()

    return jsonify({"ok":True})

def auth_required(f):

    @wraps(f)
    def wrapper(*args, **kwargs):

        if not session.get("user_id"):
            return jsonify({"error":"Unauthorized"}),401

        return f(*args, **kwargs)

    return wrapper


# ── Categories ────────────────────────────────────────────────────────────────
CATEGORIES = {
    "Food & Dining":   ["swiggy","zomato","restaurant","cafe","food","dominos","mcdonald","kfc","pizza","blinkit","grofer","bigbasket","dunzo","zepto","dhaba","eat","tiffin","lunch","dinner"],
    "Transport":       ["uber","ola","auto","taxi","petrol","fuel","metro","bus","irctc","railway","train","rapido","yulu","bounce","vahan","fastag","toll"],
    "Shopping":        ["amazon","flipkart","myntra","meesho","ajio","mall","store","mart","retail","nykaa","purplle","tata cliq","snapdeal","shopsy"],
    "Utilities":       ["electricity","water","gas","broadband","wifi","bsnl","jio","airtel","vi ","vodafone","recharge","bill pay","bbps","mahanagar","torrent","adani elec"],
    "Health":          ["pharmacy","hospital","clinic","doctor","medicine","apollo","medplus","lab","diagnostic","1mg","netmeds","pharmeasy","practo","healthkart"],
    "Entertainment":   ["netflix","hotstar","prime video","spotify","youtube","cinema","pvr","inox","book my show","bookmyshow","zee5","sonyliv","mxplayer","gaana"],
    "Education":       ["udemy","coursera","school","college","fees","book","stationery","byju","unacademy","vedantu","khan","toppr"],
    "Investments":     ["mutual fund","sip","mf","zerodha","groww","upstox","smallcase","nps","lic","ppf","fd","rd","insurance","premium"],
    "Transfers":       ["neft","imps","upi","rtgs","transfer","sent to","paid to","p2p","wallet","paytm","phonepe","gpay","google pay","bhim"],
    "ATM/Cash":        ["atm","cash withdrawal","cdm","cash deposit"],
    "Rent & Housing":  ["rent","maintenance","society","housing","flat","pg","hostel","lease"],
    "Others":          []
}

CAT_COLORS = {
    "Food & Dining":   "#f97316",
    "Transport":       "#3b82f6",
    "Shopping":        "#a855f7",
    "Utilities":       "#10b981",
    "Health":          "#ef4444",
    "Entertainment":   "#f59e0b",
    "Education":       "#06b6d4",
    "Investments":     "#22c55e",
    "Transfers":       "#6b7280",
    "ATM/Cash":        "#84cc16",
    "Rent & Housing":  "#f43f5e",
    "Others":          "#8b5cf6",
}

CAT_ICONS = {
    "Food & Dining":"🍕","Transport":"🚗","Shopping":"🛍️","Utilities":"💡",
    "Health":"🏥","Entertainment":"🎬","Education":"📚","Investments":"📈",
    "Transfers":"💸","ATM/Cash":"🏧","Rent & Housing":"🏠","Others":"📦"
}

def categorize(description: str) -> str:
    desc = description.lower()
    for cat, keywords in CATEGORIES.items():
        if any(k in desc for k in keywords):
            return cat
    return "Others"

def make_id(t: dict) -> str:
    key = f"{t['date']}{t['description']}{t['amount']}"
    return hashlib.md5(key.encode()).hexdigest()[:10]


# ── Description Cleaning & Merchant Detection ────────────────────────────────
def clean_description(desc: str) -> str:
    if not desc:
        return "Transaction"

    d = desc.upper()

    # Remove common prefixes
    d = d.replace("UPI-", "")
    d = d.replace("UPI/", "")
    d = d.replace("IMPS-", "")
    d = d.replace("NEFT-", "")

    # Remove bank handles
    d = re.sub(r'@\w+', '', d)

    # Remove phone numbers
    d = re.sub(r'\b\d{7,}\b', '', d)

    # Remove long IDs
    d = re.sub(r'\b[A-Z0-9]{8,}\b', '', d)

    # Collapse spaces
    d = re.sub(r'\s+', ' ', d)

    return d.strip().title()


KNOWN_MERCHANTS = [
    "swiggy","zomato","amazon","flipkart","myntra","uber","ola",
    "zepto","blinkit","bigbasket","dominos","pizza","kfc",
    "mcdonald","netflix","spotify","airtel","jio"
]

def detect_merchant(desc: str) -> str:
    low = desc.lower()

    for m in KNOWN_MERCHANTS:
        if m in low:
            return m.title()

    return desc

# ── CSV Parser ────────────────────────────────────────────────────────────────
def parse_csv(text: str) -> list:

    transactions = []

    rows = list(csv.reader(io.StringIO(text)))

    header = None
    date_idx = desc_idx = debit_idx = credit_idx = bal_idx = None

    for row in rows:

        clean = [c.strip().lower() for c in row]

        # Skip garbage rows
        if not any(clean):
            continue

        if any("statement" in c for c in clean):
            continue

        if any("account" in c for c in clean):
            continue

        if any("*****" in c for c in clean):
            continue

        # Detect real header row
        if (
            any("date" in c for c in clean)
            and any(x in clean for x in ["narration","description","particulars","remarks"])
        ):

            header = clean

            for i, h in enumerate(clean):

                if "date" in h:
                    date_idx = i

                if any(x in h for x in ["narration","description","particulars","remarks"]):
                    desc_idx = i

                if any(x in h for x in ["withdrawal","debit","dr"]):
                    debit_idx = i

                if any(x in h for x in ["deposit","credit","cr"]):
                    credit_idx = i

                if "balance" in h:
                    bal_idx = i

            print("REAL HEADER DETECTED:", row)

            continue

        if header is None:
            continue

        try:

            def amt(i):
                if i is None or i >= len(row):
                    return 0
                return float(row[i].replace(",", "") or 0)

            debit  = amt(debit_idx)
            credit = amt(credit_idx)
            bal    = amt(bal_idx)

            if debit == 0 and credit == 0:
                continue

            desc_raw = row[desc_idx]
            desc = detect_merchant(clean_description(desc_raw))

            t = {
                "date": row[date_idx],
                "description": desc,
                "amount": debit if debit > 0 else -credit,
                "type": "debit" if debit > 0 else "credit",
                "balance": bal,
                "category": categorize(desc),
                "source": "csv"
            }

            t["id"] = make_id(t)

            transactions.append(t)

        except:
            pass

    print("Parsed transactions:", len(transactions))

    return transactions

# ── XLSX Parser ───────────────────────────────────────────────────────────────
def parse_xlsx(file_bytes):
    transactions = []

    df = pd.read_excel(io.BytesIO(file_bytes), engine="openpyxl")

    df.columns = [c.lower().strip() for c in df.columns]

    date_col = None
    desc_col = None
    debit_col = None
    credit_col = None
    bal_col = None

    for c in df.columns:
        if "date" in c and not date_col:
            date_col = c
        if any(k in c for k in ["description","narration","remarks","particulars"]) and not desc_col:
            desc_col = c
        if any(k in c for k in ["debit","withdrawal","dr"]) and not debit_col:
            debit_col = c
        if any(k in c for k in ["credit","deposit","cr"]) and not credit_col:
            credit_col = c
        if "balance" in c and not bal_col:
            bal_col = c

    for _, row in df.iterrows():

        debit = float(row.get(debit_col,0) or 0)
        credit = float(row.get(credit_col,0) or 0)

        if debit == 0 and credit == 0:
            continue

        desc = str(row.get(desc_col,""))

        t = {
            "date": str(row.get(date_col)),
            "description": desc,
            "amount": debit if debit > 0 else -credit,
            "type": "debit" if debit > 0 else "credit",
            "balance": float(row.get(bal_col,0) or 0),
            "category": categorize(desc),
            "source": "xlsx"
        }

        t["id"] = make_id(t)

        transactions.append(t)

    return transactions

# ── SMS Parser ────────────────────────────────────────────────────────────────
SMS_PATTERNS = [
    # Debit patterns
    (r"(?:debited|debit|dr)\s+(?:with\s+)?(?:rs\.?|inr|₹)\s*([\d,]+\.?\d*)", "debit"),
    (r"(?:rs\.?|inr|₹)\s*([\d,]+\.?\d*)\s+(?:debited|debit|dr|spent|paid)", "debit"),
    (r"spent\s+(?:rs\.?|inr|₹)\s*([\d,]+\.?\d*)", "debit"),
    (r"payment\s+of\s+(?:rs\.?|inr|₹)\s*([\d,]+\.?\d*)", "debit"),
    (r"withdrawn\s+(?:rs\.?|inr|₹)\s*([\d,]+\.?\d*)", "debit"),
    # Credit patterns
    (r"(?:credited|credit|cr)\s+(?:with\s+)?(?:rs\.?|inr|₹)\s*([\d,]+\.?\d*)", "credit"),
    (r"(?:rs\.?|inr|₹)\s*([\d,]+\.?\d*)\s+(?:credited|credit|cr|received)", "credit"),
    (r"received\s+(?:rs\.?|inr|₹)\s*([\d,]+\.?\d*)", "credit"),
]

def parse_sms(sms_text: str) -> dict | None:
    low = sms_text.lower()
    # Check it's a bank SMS
    bank_keywords = ["sbi","hdfc","icici","axis","kotak","bank","account","a/c","ac no","upi","neft","imps","atm"]
    if not any(k in low for k in bank_keywords):
        return None

    txn_type = "debit"
    amount = None

    for pattern, t in SMS_PATTERNS:
        m = re.search(pattern, low)
        if m:
            txn_type = t
            amount = float(m.group(1).replace(",",""))
            break

    if not amount:
        return None

    # Extract description / merchant
    desc = "SMS Transaction"
    for marker in ["at ", "to ", "for ", "towards ", "from ", "via "]:
        idx = low.find(marker)
        if idx != -1:
            snippet = sms_text[idx+len(marker):idx+len(marker)+40].split(".")[0].split(",")[0].strip()
            if snippet:
                desc = snippet
                break

    # Extract balance
    balance = 0.0
    bal_match = re.search(r"(?:avl|avail|available|bal|balance)\.?\s*(?:rs\.?|inr|₹)?\s*([\d,]+\.?\d*)", low)
    if bal_match:
        balance = float(bal_match.group(1).replace(",",""))

    t = {
        "date":        date.today().isoformat(),
        "description": desc,
        "amount":      amount if txn_type == "debit" else -amount,
        "type":        txn_type,
        "balance":     balance,
        "category":    categorize(desc),
        "source":      "sms"
    }
    t["id"] = make_id(t)
    return t


# ── Analytics helpers ─────────────────────────────────────────────────────────
def compute_analytics(txns: list) -> dict:
    debits  = [t for t in txns if t["amount"] > 0]
    credits = [t for t in txns if t["amount"] < 0]

    total_debit  = sum(t["amount"] for t in debits)
    total_credit = sum(-t["amount"] for t in credits)

    # Category breakdown
    cat_groups = defaultdict(lambda: {"total":0,"count":0,"items":[]})
    for t in debits:
        g = cat_groups[t["category"]]
        g["total"]  += t["amount"]
        g["count"]  += 1
        g["items"].append(t)

    # Daily spend (last 30 days)
    daily = defaultdict(float)
    for t in debits:
        daily[t["date"]] += t["amount"]

    # Top merchants
    merchant_totals = defaultdict(float)
    for t in debits:
        key = t["description"][:30]
        merchant_totals[key] += t["amount"]
    top_merchants = sorted(merchant_totals.items(), key=lambda x: x[1], reverse=True)[:8]

    return {
        "total_debit":   round(total_debit, 2),
        "total_credit":  round(total_credit, 2),
        "net":           round(total_credit - total_debit, 2),
        "count":         len(txns),
        "debit_count":   len(debits),
        "credit_count":  len(credits),
        "categories":    {k: {"total": round(v["total"],2), "count": v["count"], "color": CAT_COLORS.get(k,"#888"), "icon": CAT_ICONS.get(k,"📦")} for k,v in sorted(cat_groups.items(), key=lambda x: x[1]["total"], reverse=True)},
        "daily":         dict(sorted(daily.items())),
        "top_merchants": [{"name": m[0], "amount": round(m[1],2)} for m in top_merchants],
        "avg_daily":     round(total_debit / max(len(daily),1), 2),
    }


# ── Email HTML builder ────────────────────────────────────────────────────────
def build_email_html(analytics: dict, txns: list, period: str = "") -> str:
    cats    = analytics["categories"]
    total   = analytics["total_debit"]
    today   = datetime.now().strftime("%A, %d %B %Y")

    cat_rows = ""
    for cat, data in cats.items():
        pct = (data["total"]/total*100) if total else 0
        color = data["color"]
        cat_rows += f"""
        <tr>
          <td style="padding:10px 8px;font-size:13px;color:#374151">
            {data['icon']} {cat}
          </td>
          <td style="padding:10px 8px">
            <div style="background:#e5e7eb;border-radius:3px;height:7px;width:180px">
              <div style="background:{color};width:{min(pct,100):.1f}%;height:100%;border-radius:3px"></div>
            </div>
          </td>
          <td style="padding:10px 8px;font-size:12px;color:#9ca3af">{pct:.1f}%</td>
          <td style="padding:10px 8px;font-size:14px;font-weight:bold;color:#111;text-align:right">₹{data['total']:,.0f}</td>
        </tr>"""

    txn_rows = ""
    for t in txns[:25]:
        color  = "#dc2626" if t["amount"] > 0 else "#16a34a"
        sign   = "-" if t["amount"] > 0 else "+"
        amount = abs(t["amount"])
        txn_rows += f"""
        <tr style="border-bottom:1px solid #f3f4f6">
          <td style="padding:9px 8px;font-size:12px;color:#9ca3af">{t['date']}</td>
          <td style="padding:9px 8px;font-size:13px;color:#374151">{t['description'][:42]}</td>
          <td style="padding:9px 8px;font-size:12px">
            <span style="background:{CAT_COLORS.get(t['category'],'#888')}22;color:{CAT_COLORS.get(t['category'],'#888')};padding:2px 8px;border-radius:20px;font-size:11px">{t['category']}</span>
          </td>
          <td style="padding:9px 8px;font-size:13px;font-weight:bold;color:{color};text-align:right">{sign}₹{amount:,.0f}</td>
        </tr>"""

    top_merchants = "".join(
        f'<div style="display:flex;justify-content:space-between;padding:8px 0;border-bottom:1px solid #f3f4f6"><span style="font-size:13px;color:#374151">{m["name"][:30]}</span><span style="font-size:13px;font-weight:bold;color:#dc2626">₹{m["amount"]:,.0f}</span></div>'
        for m in analytics["top_merchants"]
    )

    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>SpendLens Report</title></head>
<body style="margin:0;padding:24px;background:#f1f5f9;font-family:'Segoe UI',Arial,sans-serif">
<div style="max-width:680px;margin:0 auto">

  <div style="background:linear-gradient(135deg,#0f172a 0%,#1e293b 100%);border-radius:16px 16px 0 0;padding:32px;color:white">
    <div style="font-size:11px;letter-spacing:4px;color:#64748b;margin-bottom:8px">SPENDLENS · BANK ANALYTICS</div>
    <div style="font-size:28px;font-weight:800;margin-bottom:4px">💳 Your Spend Report</div>
    <div style="font-size:13px;color:#94a3b8">{today}{' · ' + period if period else ''}</div>
  </div>

  <div style="background:#0f172a;padding:0 32px">
    <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:0;border-top:1px solid #1e293b">
      <div style="padding:24px 16px;border-right:1px solid #1e293b">
        <div style="font-size:10px;letter-spacing:2px;color:#475569;margin-bottom:6px">TOTAL SPENT</div>
        <div style="font-size:26px;font-weight:800;color:#f8fafc">₹{analytics['total_debit']:,.0f}</div>
        <div style="font-size:11px;color:#dc2626;margin-top:2px">↓ {analytics['debit_count']} transactions</div>
      </div>
      <div style="padding:24px 16px;border-right:1px solid #1e293b">
        <div style="font-size:10px;letter-spacing:2px;color:#475569;margin-bottom:6px">TOTAL RECEIVED</div>
        <div style="font-size:26px;font-weight:800;color:#f8fafc">₹{analytics['total_credit']:,.0f}</div>
        <div style="font-size:11px;color:#16a34a;margin-top:2px">↑ {analytics['credit_count']} transactions</div>
      </div>
      <div style="padding:24px 16px">
        <div style="font-size:10px;letter-spacing:2px;color:#475569;margin-bottom:6px">AVG DAILY SPEND</div>
        <div style="font-size:26px;font-weight:800;color:#f8fafc">₹{analytics['avg_daily']:,.0f}</div>
        <div style="font-size:11px;color:#475569;margin-top:2px">per day</div>
      </div>
    </div>
  </div>

  <div style="background:white;padding:28px 32px">
    <h2 style="font-size:11px;letter-spacing:3px;color:#9ca3af;margin:0 0 16px;text-transform:uppercase">Category Breakdown</h2>
    <table style="width:100%;border-collapse:collapse"><tbody>{cat_rows}</tbody></table>

    <h2 style="font-size:11px;letter-spacing:3px;color:#9ca3af;margin:28px 0 12px;text-transform:uppercase">Top Merchants</h2>
    {top_merchants}

    <h2 style="font-size:11px;letter-spacing:3px;color:#9ca3af;margin:28px 0 16px;text-transform:uppercase">Transactions</h2>
    <table style="width:100%;border-collapse:collapse">
      <thead><tr style="border-bottom:2px solid #f3f4f6">
        <th style="padding:8px;text-align:left;font-size:10px;color:#9ca3af;font-weight:normal;letter-spacing:1px">DATE</th>
        <th style="padding:8px;text-align:left;font-size:10px;color:#9ca3af;font-weight:normal;letter-spacing:1px">DESCRIPTION</th>
        <th style="padding:8px;text-align:left;font-size:10px;color:#9ca3af;font-weight:normal;letter-spacing:1px">CATEGORY</th>
        <th style="padding:8px;text-align:right;font-size:10px;color:#9ca3af;font-weight:normal;letter-spacing:1px">AMOUNT</th>
      </tr></thead>
      <tbody>{txn_rows}</tbody>
    </table>
  </div>

  <div style="background:#f8fafc;border-radius:0 0 16px 16px;padding:20px 32px;text-align:center">
    <p style="font-size:11px;color:#9ca3af;margin:0;letter-spacing:2px">SPENDLENS · AUTO-GENERATED · YOUR DATA STAYS LOCAL</p>
  </div>
</div></body></html>"""


# ── API Routes ────────────────────────────────────────────────────────────────

@app.route("/api/health")
def health():
    conn = db()
    count = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    conn.close()
    return jsonify({"status": "ok", "transactions": count})

@app.route("/api/reset-session")
def reset_session():
    session.clear()
    return jsonify({"ok":True})

@app.route("/api/upload/file", methods=["POST"])
@auth_required
def upload_file():

    if "file" not in request.files:
        return jsonify({"error": "No file"}), 400

    file = request.files["file"]
    filename = file.filename.lower()

    if filename.endswith(".csv"):
        text = file.read().decode("utf-8-sig", errors="ignore")
        new_txns = parse_csv(text)

    elif filename.endswith(".xlsx"):
        new_txns = parse_xlsx(file.read())

    else:
        return jsonify({"error": "Unsupported file format"}), 400

    if not new_txns:
        return jsonify({"error": "No transactions parsed"}), 400

    conn = db()

    added = 0

    for t in new_txns:
        try:
            conn.execute("""
            INSERT INTO transactions
            (id,user_id,date,description,amount,type,balance,category,source)
            VALUES(?,?,?,?,?,?,?,?,?)
            """,(
                t["id"],
                session["user_id"],
                t["date"],
                t["description"],
                t["amount"],
                t["type"],
                t["balance"],
                t["category"],
                t["source"]
            ))
            added += 1
        except:
            pass

    conn.commit()
    conn.close()

    return jsonify({"added": added})



@app.route("/api/upload/sms", methods=["POST"])
@auth_required
def upload_sms():
    data = request.json or {}
    messages = data.get("messages", [])
    if isinstance(messages, str): messages = [messages]
    added = 0
    conn = db()
    rows = conn.execute("SELECT id FROM transactions WHERE user_id=?", (session["user_id"],)).fetchall()
    existing_ids = {r["id"] for r in rows}
    conn.close()
    for sms in messages:
        t = parse_sms(sms)
        if t and t["id"] not in existing_ids:
            conn = db()

            conn.execute("""
            INSERT INTO transactions
            (id,user_id,date,description,amount,type,balance,category,source)
            VALUES(?,?,?,?,?,?,?,?,?)
            """,(
            t["id"],
            session["user_id"],
            t["date"],
            t["description"],
            t["amount"],
            t["type"],
            t["balance"],
            t["category"],
            t["source"]
             ))

            conn.commit()
            conn.close()
            existing_ids.add(t["id"])
            added += 1
    return jsonify({"added": added})


@app.route("/api/upload/manual", methods=["POST"])
@auth_required
def upload_manual():
    t = request.json or {}
    required = ["date","description","amount","type"]
    if not all(k in t for k in required):
        return jsonify({"error": "Missing fields"}), 400
    t["category"] = categorize(t["description"])
    t["source"]   = "manual"
    t["balance"]  = 0
    t["id"]       = make_id(t)
    conn = db()

    exists = conn.execute(
    "SELECT 1 FROM transactions WHERE id=?",
    (t["id"],)
    ).fetchone()

    if exists:
     return jsonify({"error":"Duplicate"}),409
  
    conn = db()

    conn.execute("""
        INSERT INTO transactions
        (id,user_id,date,description,amount,type,balance,category,source)
        VALUES(?,?,?,?,?,?,?,?,?)
        """,(
        t["id"],
        session["user_id"],
        t["date"],
        t["description"],
        t["amount"],
        t["type"],
        t["balance"],
        t["category"],
        t["source"]
        ))

    conn.commit()
    conn.close()
    return jsonify({"added": 1})


@app.route("/api/transactions")
@auth_required
def get_transactions():

    conn = db()

    rows = conn.execute(
        "SELECT * FROM transactions WHERE user_id=? ORDER BY date DESC",
        (session["user_id"],)
    ).fetchall()

    conn.close()

    txns = [dict(r) for r in rows]

    return jsonify(txns)


@app.route("/api/transactions/<tid>", methods=["PATCH"])
@auth_required
def update_transaction(tid):

    data = request.json or {}

    conn = db()

    if "category" in data:
        conn.execute(
            "UPDATE transactions SET category=? WHERE id=? AND user_id=?",
            (data["category"], tid, session["user_id"])
        )

    if "description" in data:
        conn.execute(
            "UPDATE transactions SET description=? WHERE id=? AND user_id=?",
            (data["description"], tid, session["user_id"])
        )

    conn.commit()
    conn.close()

    return jsonify({"updated":True})


@app.route("/api/transactions/<tid>", methods=["DELETE"])
@auth_required
def delete_transaction(tid):

    conn = db()

    conn.execute(
        "DELETE FROM transactions WHERE id=? AND user_id=?",
        (tid, session["user_id"])
    )

    conn.commit()
    conn.close()

    return jsonify({"deleted":1})


@app.route("/api/analytics")
@auth_required
def get_analytics():

    conn = db()

    rows = conn.execute(
        "SELECT * FROM transactions WHERE user_id=?",
        (session["user_id"],)
    ).fetchall()

    conn.close()

    txns = [dict(r) for r in rows]

    return jsonify(compute_analytics(txns))


@app.route("/api/settings", methods=["GET", "POST"])
def settings():
    if request.method == "POST":
        data = request.json or {}
        DB["settings"].update(data)
        return jsonify({"ok": True})
    # Don't expose password
    safe = {k: ("****" if "password" in k else v) for k, v in DB["settings"].items()}
    return jsonify(safe)


@app.route("/api/send-report", methods=["POST"])
@auth_required
def send_report():
    data     = request.json or {}
    to_email = data.get("email") or DB["settings"]["email"]
    sender   = data.get("sender_email") or DB["settings"]["sender_email"]
    password = data.get("gmail_app_password") or DB["settings"]["gmail_app_password"]
    period   = data.get("period", "")

    if not all([to_email, sender, password]):
        return jsonify({"error": "Email credentials not configured"}), 400

    txns     = DB["transactions"]
    analytics = compute_analytics(txns)
    html     = build_email_html(analytics, sorted(txns, key=lambda x: x["date"], reverse=True), period)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"💳 SpendLens Report — {date.today().strftime('%d %b %Y')}"
    msg["From"]    = sender
    msg["To"]      = to_email
    msg.attach(MIMEText(html, "html"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(sender, password)
            s.sendmail(sender, to_email, msg.as_string())
        return jsonify({"ok": True, "sent_to": to_email})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/preview-report", methods=["GET"])
@auth_required
def preview_report():
    conn = db()
    rows = conn.execute("SELECT * FROM transactions WHERE user_id=?", (session["user_id"],)).fetchall()
    conn.close()

    txns = [dict(r) for r in rows]
    analytics = compute_analytics(txns)
    html      = build_email_html(analytics, sorted(txns, key=lambda x: x["date"], reverse=True))
    from flask import Response
    return Response(html, mimetype="text/html")


@app.route("/api/clear", methods=["POST"])
@auth_required
def clear_data():
    conn = db()
    conn.execute("DELETE FROM transactions WHERE user_id=?", (session["user_id"],))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/categories", methods=["GET"])
def get_categories():
    return jsonify([{"name": k, "color": CAT_COLORS.get(k,"#888"), "icon": CAT_ICONS.get(k,"📦")} for k in CATEGORIES])





# ─── EMBEDDED FRONTEND ────────────────────────────────────────────────────────
FRONTEND_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, viewport-fit=cover">
<title>SpendLens · Bank Analytics</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<style>
  @import url('https://fonts.googleapis.com/css2?family=DM+Mono:wght@400;500&display=swap');
  *{box-sizing:border-box;margin:0;padding:0}
  :root{
    --bg:#070b12;--surface:#0d1117;--card:#111827;--border:#1f2937;
    --accent:#6ee7b7;--red:#f87171;--green:#34d399;--muted:#4b5563;
    --text:#e5e7eb;--sub:#6b7280;
  }
  body{background:var(--bg);color:var(--text);font-family:'DM Mono',monospace;min-height:100vh}
  ::-webkit-scrollbar{width:6px;height:6px}
  ::-webkit-scrollbar-track{background:var(--surface)}
  ::-webkit-scrollbar-thumb{background:var(--border);border-radius:3px}

  /* Layout */
  .header{background:var(--surface);border-bottom:1px solid var(--border);padding:0 24px}
  .header-inner{max-width:1100px;margin:0 auto;display:flex;align-items:center;justify-content:space-between;padding:16px 0}
  .logo{font-size:22px;font-weight:800;letter-spacing:3px;color:var(--accent)}
  .divider{width:1px;height:20px;background:var(--border);margin:0 16px}
  .logo-sub{font-size:11px;color:var(--sub);letter-spacing:2px}
  .status-dot{width:7px;height:7px;border-radius:50%;display:inline-block;margin-right:6px}
  .status-text{font-size:11px;color:var(--sub)}

  .tabs{background:var(--surface);border-bottom:1px solid var(--border);padding:0 24px}
  .tabs-inner{max-width:1100px;margin:0 auto;display:flex;gap:4px}
  .tab-btn{padding:14px 20px;background:none;border:none;border-bottom:2px solid transparent;
    color:var(--sub);cursor:pointer;font-size:13px;font-family:'DM Mono',monospace;
    font-weight:400;transition:all .15s}
  .tab-btn.active{border-bottom-color:var(--accent);color:var(--accent);font-weight:700}

  .content{max-width:1100px;margin:0 auto;padding:28px 24px}

  /* Cards */
  .card{background:var(--card);border:1px solid var(--border);border-radius:14px;padding:24px;margin-bottom:20px}
  .label{font-size:10px;letter-spacing:3px;color:var(--muted);text-transform:uppercase;margin-bottom:8px}

  /* Stats grid */
  .stats-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:16px;margin-bottom:20px}
  .stat-card{background:var(--card);border:1px solid var(--border);border-radius:14px;padding:24px}
  .stat-value{font-size:26px;font-weight:800;margin-top:4px}
  .stat-sub{font-size:12px;color:var(--sub);margin-top:4px}

  /* Charts grid */
  .charts-grid{display:grid;grid-template-columns:2fr 1fr;gap:20px;margin-bottom:20px}
  .charts-grid2{display:grid;grid-template-columns:1fr 1fr;gap:20px;margin-bottom:20px}
  @media(max-width:700px){.charts-grid,.charts-grid2{grid-template-columns:1fr}}

  /* Buttons */
  .btn{padding:10px 22px;border-radius:8px;border:none;cursor:pointer;font-family:'DM Mono',monospace;
    font-size:13px;font-weight:700;letter-spacing:1px;transition:all .15s}
  .btn-primary{background:var(--accent);color:#000}
  .btn-ghost{background:transparent;color:var(--text);border:1px solid var(--border)}
  .btn-danger{background:#dc2626;color:#fff}
  .btn:disabled{opacity:.5;cursor:not-allowed}

  /* Input */
  .input{background:var(--surface);border:1px solid var(--border);border-radius:8px;color:var(--text);
    padding:10px 14px;width:100%;font-family:'DM Mono',monospace;font-size:13px;outline:none}
  .input:focus{border-color:var(--accent)}
  select.input option{background:var(--card)}

  /* Dropzone */
  .dropzone{border:2px dashed var(--border);border-radius:12px;padding:52px 20px;text-align:center;
    cursor:pointer;transition:all .2s}
  .dropzone:hover,.dropzone.drag{border-color:var(--accent);background:#6ee7b711}
  .dropzone-icon{font-size:40px;margin-bottom:12px}

  /* Table */
  .table-wrap{overflow-x:auto;border-radius:14px;border:1px solid var(--border)}
  table{width:100%;border-collapse:collapse;font-size:13px}
  thead tr{background:var(--surface);border-bottom:1px solid var(--border)}
  th{padding:12px 16px;text-align:left;color:var(--sub);font-weight:500;font-size:11px;letter-spacing:2px}
  tbody tr{border-bottom:1px solid var(--border);transition:background .1s}
  tbody tr:hover{background:var(--surface)}
  td{padding:12px 16px;color:var(--text)}
  .table-footer{padding:10px 16px;font-size:12px;color:var(--sub);background:var(--surface);
    border-top:1px solid var(--border);border-radius:0 0 14px 14px}

  /* Pill */
  .pill{display:inline-block;padding:3px 10px;border-radius:20px;font-size:11px;font-weight:700}

  /* Category bar */
  .cat-bar-bg{height:4px;background:var(--border);border-radius:2px;margin-top:5px}
  .cat-bar-fill{height:100%;border-radius:2px;transition:width .6s ease}

  /* Method selector */
  .method-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin-bottom:20px}
  .method-btn{padding:16px;border-radius:12px;border:2px solid var(--border);background:var(--card);
    color:var(--text);cursor:pointer;text-align:left;transition:all .15s}
  .method-btn.active{border-color:var(--accent);background:#6ee7b711}
  .method-title{font-size:15px;font-weight:700;margin-bottom:4px}
  .method-desc{font-size:12px;color:var(--sub)}

  /* Toast */
  .toasts{position:fixed;bottom:24px;right:24px;z-index:9999;display:flex;flex-direction:column;gap:8px}
  .toast{padding:12px 18px;border-radius:10px;font-size:13px;font-family:'DM Mono',monospace;
    box-shadow:0 4px 20px rgba(0,0,0,.4);animation:slideIn .2s ease;color:#fff}
  .toast.success{background:#14532d}
  .toast.error{background:#7f1d1d}
  .toast.info{background:#1e3a5f}
  @keyframes slideIn{from{opacity:0;transform:translateX(20px)}to{opacity:1;transform:none}}

  /* Misc */
  .offline-banner{background:#7f1d1d;padding:12px 24px;text-align:center;font-size:13px}
  .empty-state{padding:60px;text-align:center;color:var(--sub)}
  .form-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:12px}
  @media(max-width:600px){.form-grid{grid-template-columns:1fr}}
  .hint{background:var(--surface);border-radius:10px;padding:16px;margin-top:16px}
  code{background:#1e293b;padding:2px 8px;border-radius:4px;color:var(--accent);font-family:'DM Mono',monospace}
  pre{background:var(--surface);border-radius:8px;padding:16px;color:var(--accent);font-size:12px;
    overflow-x:auto;font-family:'DM Mono',monospace;line-height:1.8}
  .merchant-row{display:flex;justify-content:space-between;padding:10px 0;border-bottom:1px solid var(--border);font-size:13px}
  .success-box{background:#14532d;border-radius:12px;padding:20px;display:flex;align-items:center;gap:12px;margin-bottom:16px}

/* Mobile scaling fix */
body{
  max-width:100%;
  overflow-x:hidden;
}

.content{
  max-width:1000px;
  margin:auto;
}

@media (max-width:600px){
  .content{
    padding:18px 14px;
  }

  .header-inner{
    flex-direction:column;
    gap:10px;
  }

  .logo{
    font-size:18px;
  }
}

</style>
</head>
<body>


<div id="familyGate" style="
position:fixed;
top:0;
left:0;
width:100%;
height:100%;
background:#070b12;
display:flex;
align-items:center;
justify-content:center;
z-index:99999;
">

<div style="background:#111827;padding:40px;border-radius:14px;width:320px;text-align:center">

<h2 style="margin-bottom:20px;color:#6ee7b7">FAMILY ACCESS</h2>

<input id="familyCode" type="password" placeholder="Enter family code"
class="input" style="margin-bottom:14px">

<button class="btn btn-primary" onclick="verifyFamily()" style="width:100%">
Enter
</button>

</div>
</div>

<div id="loginScreen" style="
position:fixed;
top:0;
left:0;
width:100%;
height:100%;
background:#070b12;
display:flex;
align-items:center;
justify-content:center;
z-index:9999;
">

<div style="background:#111827;padding:40px;border-radius:14px;width:320px;text-align:center">

<h2 style="margin-bottom:20px;color:#6ee7b7">SPENDLENS LOGIN</h2>

<input id="loginUser" type="text" placeholder="Username"
class="input" style="margin-bottom:10px">

<input id="loginPassword" type="password" placeholder="Password"
class="input" style="margin-bottom:14px">

<button class="btn btn-primary" onclick="login()" style="width:100%;margin-bottom:10px">
Login
</button>

<button class="btn btn-ghost" onclick="forgotPassword()" style="width:100%;margin-top:6px">
Forgot Password
</button>

<button class="btn btn-ghost" onclick="openSignup()" style="width:100%">
Create Account
</button>

</div>

</div>


<div id="signupScreen" style="
position:fixed;
top:0;
left:0;
width:100%;
height:100%;
background:#070b12;
display:none;
align-items:center;
justify-content:center;
z-index:9999;
">

<div style="background:#111827;padding:40px;border-radius:14px;width:320px;text-align:center">

<h2 style="margin-bottom:20px;color:#6ee7b7">CREATE ACCOUNT</h2>

<input id="signupUser" type="text" placeholder="Username"
class="input" style="margin-bottom:10px">

<input id="signupPass" type="password" placeholder="Password"
class="input" style="margin-bottom:14px">

<button class="btn btn-primary" onclick="createAccount()" style="width:100%;margin-bottom:10px">
Create Account
</button>

<button class="btn btn-ghost" onclick="backToLogin()" style="width:100%">
Back to Login
</button>

</div>

</div>

<!-- Header -->
<div class="header">
  <div class="header-inner">
    <div style="display:flex;align-items:center">
      <div class="logo">SPENDLENS</div>
      <div class="divider"></div>
      <div class="logo-sub">INDIAN BANK ANALYTICS</div>
    </div>
    <div style="display:flex;align-items:center;gap:16px">
      <div>
        <span class="status-dot" id="statusDot" style="background:var(--muted)"></span>
        <span class="status-text" id="statusText">connecting…</span>
      </div>
      <button class="btn btn-danger" style="padding:6px 14px;font-size:11px" onclick="clearAll()" id="clearBtn" style="display:none">Clear All</button>
    </div>
  </div>
</div>

<!-- Offline banner -->
<div class="offline-banner" id="offlineBanner" style="display:none">
  ⚠️ Backend not reachable. Make sure <code>python app.py</code> is running in a separate PowerShell window.
</div>

<!-- Tabs -->
<div class="tabs">
  <div class="tabs-inner">
    <button class="tab-btn active" onclick="showTab('dashboard')" id="tab-dashboard">📊 Dashboard</button>
    <button class="tab-btn" onclick="showTab('import')" id="tab-import">⬆️ Import Data</button>
    <button class="tab-btn" onclick="showTab('transactions')" id="tab-transactions">📋 Transactions</button>
    <button class="tab-btn" onclick="showTab('email')" id="tab-email">📧 Email Report</button>
  </div>
</div>

<!-- ── DASHBOARD TAB ───────────────────────────────────────────────── -->
<div class="content" id="page-dashboard">
  <div id="dashEmpty" class="empty-state">No data yet — go to <strong>Import Data</strong> to upload your bank statement.</div>
  <div id="dashContent" style="display:none">
    <div class="stats-grid" id="statsGrid"></div>
    <div class="charts-grid">
      <div class="card"><div class="label">Daily Spend — Last 30 Days</div><canvas id="areaChart" height="200"></canvas></div>
      <div class="card"><div class="label">By Category</div><canvas id="donutChart" height="200"></canvas></div>
    </div>
    <div class="charts-grid2">
      <div class="card" id="catBreakdown"><div class="label">Category Breakdown</div></div>
      <div class="card" id="merchantList"><div class="label">Top Merchants</div></div>
    </div>
  </div>
</div>

<!-- ── IMPORT TAB ─────────────────────────────────────────────────── -->
<div class="content" id="page-import" style="display:none">
  <div class="method-grid">
    <button class="method-btn active" onclick="setMethod('csv')" id="method-csv">
      <div class="method-title">📄 CSV Upload</div>
      <div class="method-desc">SBI / HDFC / ICICI export</div>
    </button>
    <button class="method-btn" onclick="setMethod('sms')" id="method-sms">
      <div class="method-title">💬 SMS Paste</div>
      <div class="method-desc">Paste bank SMS alerts</div>
    </button>
    <button class="method-btn" onclick="setMethod('manual')" id="method-manual">
      <div class="method-title">✏️ Manual Entry</div>
      <div class="method-desc">Add one transaction</div>
    </button>
  </div>

  <!-- CSV -->
  <div class="card" id="panel-csv">
    <div class="label">Bank Statement CSV</div>
    <div class="dropzone" id="dropzone" onclick="document.getElementById('csvFile').click()"
      ondragover="event.preventDefault();this.classList.add('drag')"
      ondragleave="this.classList.remove('drag')"
      ondrop="handleDrop(event)">
      <div class="dropzone-icon">📂</div>
      <div style="color:var(--text);margin-bottom:6px">Drop your CSV file here</div>
      <div style="color:var(--sub);font-size:12px">or click to browse · SBI, HDFC, ICICI, Axis formats</div>
      <div id="uploadStatus" style="margin-top:12px;color:var(--accent);font-size:13px"></div>
      <input type="file" id="csvFile" accept=".csv,.txt" style="display:none" onchange="uploadCSV(this.files[0])">
    </div>
    <div class="hint">
      <div class="label">How to download SBI CSV</div>
      <div style="font-size:13px;color:var(--sub);line-height:2">
        1. Login → <strong style="color:var(--text)">onlinesbi.sbi</strong><br>
        2. My Accounts → <strong style="color:var(--text)">Account Statement</strong><br>
        3. Set date range → <strong style="color:var(--text)">Download</strong><br>
        4. Choose <strong style="color:var(--text)">CSV format</strong> → Download
      </div>
    </div>
  </div>

  <!-- SMS -->
  <div class="card" id="panel-sms" style="display:none">
    <div class="label">Paste Bank SMS Messages</div>
    <p style="color:var(--sub);font-size:13px;margin-bottom:12px">Paste one or multiple SMS alerts. Separate multiple messages with a line containing <code>---</code></p>
    <textarea id="smsText" class="input" rows="7" style="resize:vertical"
      placeholder="Your SBI A/C XXXXX1234 debited INR 450.00 on 07-03-2026. Info: Swiggy Order. Avl Bal: INR 12,350.00&#10;---&#10;Dear Customer, Rs.2000.00 withdrawn from A/C XX1234 at ATM on 06-03-26. Bal:Rs.10,350.00"></textarea>
    <div style="margin-top:12px"><button class="btn btn-primary" onclick="uploadSMS()">Parse SMS →</button></div>
  </div>

  <!-- Manual -->
  <div class="card" id="panel-manual" style="display:none">
    <div class="label">Manual Transaction Entry</div>
    <div class="form-grid">
      <div><div class="label">Date</div><input type="date" id="m-date" class="input"></div>
      <div><div class="label">Type</div>
        <select id="m-type" class="input">
          <option value="debit">Debit (Expense)</option>
          <option value="credit">Credit (Income)</option>
        </select>
      </div>
    </div>
    <div style="margin-bottom:12px"><div class="label">Description / Merchant</div><input type="text" id="m-desc" class="input" placeholder="e.g. Swiggy Order, Petrol bunk"></div>
    <div style="margin-bottom:16px"><div class="label">Amount (₹)</div><input type="number" id="m-amt" class="input" placeholder="0.00"></div>
    <button class="btn btn-primary" onclick="addManual()">Add Transaction</button>
  </div>
</div>

<!-- ── TRANSACTIONS TAB ────────────────────────────────────────────── -->
<div class="content" id="page-transactions" style="display:none">
  <div style="display:flex;gap:12px;flex-wrap:wrap;margin-bottom:16px">
    <input type="text" class="input" id="txSearch" placeholder="🔍 Search…" style="flex:2;min-width:180px" oninput="renderTransactions()">
    <select class="input" id="txCatFilter" style="min-width:160px" onchange="renderTransactions()"><option value="">All Categories</option></select>
    <select class="input" id="txTypeFilter" style="min-width:130px" onchange="renderTransactions()">
      <option value="">All Types</option>
      <option value="debit">Debits</option>
      <option value="credit">Credits</option>
    </select>
  </div>
  <div class="table-wrap">
    <table><thead><tr>
      <th>DATE</th><th>DESCRIPTION</th><th>CATEGORY</th><th>SOURCE</th><th style="text-align:right">AMOUNT</th><th></th>
    </tr></thead>
    <tbody id="txBody"></tbody>
    </table>
    <div class="table-footer" id="txFooter">0 transactions</div>
  </div>
</div>

<!-- ── EMAIL TAB ──────────────────────────────────────────────────── -->
<div class="content" id="page-email" style="display:none">
  <div style="max-width:600px">
    <div class="card">
      <div class="label">Gmail Configuration</div>
      <p style="color:var(--sub);font-size:13px;margin-bottom:16px;line-height:1.7">
        Use a <strong style="color:var(--text)">Gmail App Password</strong> — not your login password.<br>
        Get it: Google Account → Security → 2-Step Verification → <strong style="color:var(--text)">App Passwords</strong>
      </p>
      <div style="margin-bottom:12px"><div class="label">Send Report To (Your Email)</div><input type="email" id="e-to" class="input" placeholder="you@gmail.com"></div>
      <div style="margin-bottom:12px"><div class="label">From Gmail (Sender)</div><input type="email" id="e-from" class="input" placeholder="sender@gmail.com"></div>
      <div style="margin-bottom:20px"><div class="label">Gmail App Password</div><input type="password" id="e-pass" class="input" placeholder="xxxx xxxx xxxx xxxx"></div>
      <div id="emailSuccess" class="success-box" style="display:none">
        <span style="font-size:28px">✅</span>
        <div><div style="color:#4ade80;font-weight:700">Report sent!</div><div style="color:#86efac;font-size:13px;margin-top:4px" id="emailSuccessMsg"></div></div>
      </div>
      <div style="display:flex;gap:12px;flex-wrap:wrap">
        <button class="btn btn-primary" id="sendBtn" onclick="sendReport()">📧 Send Report Now</button>
        <button class="btn btn-ghost" onclick="window.open('http://localhost:5000/api/preview-report','_blank')">👁 Preview HTML</button>
      </div>
    </div>

    <div class="card">
      <div class="label">Automate Daily Email (optional)</div>
      <p style="color:var(--sub);font-size:13px;margin-bottom:12px">Open a new PowerShell, edit <code>auto_report.py</code> with your email, then run:</p>
      <pre>cd C:/Users/Ridham J Shah/Downloads/spendlens/bankapp/backend
pip install schedule requests
python auto_report.py</pre>

      <p style="color:var(--sub);font-size:12px;margin-top:10px">This runs in background and sends a report every morning at 8:00 AM automatically.</p>
    </div>
  </div>
</div>

<!-- Toasts -->
<div class="toasts" id="toasts"></div>

<script>
const API = "/api";
let allTransactions = [];
let analytics = null;
let categories = [];
let areaChartInst = null;
let donutChartInst = null;

// ── Toast ─────────────────────────────────────────────────────────────────────
function toast(msg, type = "info") {
  const el = document.createElement("div");
  el.className = `toast ${type}`;
  el.textContent = (type === "success" ? "✅ " : type === "error" ? "❌ " : "ℹ️ ") + msg;
  document.getElementById("toasts").appendChild(el);
  setTimeout(() => el.remove(), 3500);
}


async function verifyFamily(){

  const code = document.getElementById("familyCode").value;

  const r = await fetch("/api/verify-family",{
    method:"POST",
    headers:{"Content-Type":"application/json"},
    body:JSON.stringify({code})
  });

  if(r.status===200){

      document.getElementById("familyGate").style.display="none";
      document.getElementById("loginScreen").style.display="flex";

  }else{

      alert("Wrong family code");

  }

}

// ── Server health ─────────────────────────────────────────────────────────────
async function checkHealth() {
  try {
    const r = await fetch(`${API}/health`);
    const d = await r.json();
    document.getElementById("statusDot").style.background = "var(--green)";
    document.getElementById("statusText").textContent = "server ok";
    document.getElementById("offlineBanner").style.display = "none";
    return true;
  } catch {
    document.getElementById("statusDot").style.background = "var(--red)";
    document.getElementById("statusText").textContent = "server offline";
    document.getElementById("offlineBanner").style.display = "block";
    return false;
  }
}

async function forgotPassword(){

  const username = prompt("Enter your username");
  if(!username) return;

  const newpass = prompt("Enter new password");
  if(!newpass) return;

  const code = prompt("Enter family code");

  const r = await fetch("/api/reset-password",{
    method:"POST",
    headers:{"Content-Type":"application/json"},
    body:JSON.stringify({
        username:username,
        password:newpass,
        family_code:code
    })
  });

  const d = await r.json();

  if(d.ok){
      alert("Password reset successful. Please login.");
  }else{
      alert(d.error || "Failed");
  }

}

// ── Fetch all data ────────────────────────────────────────────────────────────
async function fetchAll() {
  try {
    const [txR, anR, catR] = await Promise.all([
      fetch(`${API}/transactions`),
      fetch(`${API}/analytics`),
      fetch(`${API}/categories`),
    ]);
    allTransactions = await txR.json();
    analytics       = await anR.json();
    categories      = await catR.json();
    renderDashboard();
    renderTransactions();
    populateCatFilter();
    document.getElementById("clearBtn").style.display = allTransactions.length > 0 ? "inline-block" : "none";
  } catch { /* offline */ }
}

// ── Tabs ──────────────────────────────────────────────────────────────────────
function showTab(name) {
  ["dashboard","import","transactions","email"].forEach(t => {
    document.getElementById(`page-${t}`).style.display = t === name ? "block" : "none";
    document.getElementById(`tab-${t}`).classList.toggle("active", t === name);
  });
  if (name === "dashboard") renderDashboard();
}

// ── Import methods ────────────────────────────────────────────────────────────
function setMethod(m) {
  ["csv","sms","manual"].forEach(x => {
    document.getElementById(`method-${x}`).classList.toggle("active", x === m);
    document.getElementById(`panel-${x}`).style.display = x === m ? "block" : "none";
  });
}

// ── CSV Upload ────────────────────────────────────────────────────────────────
function handleDrop(e) {
  e.preventDefault();
  document.getElementById("dropzone").classList.remove("drag");
  const f = e.dataTransfer.files[0];
  if (f) uploadCSV(f);
}

async function uploadCSV(file) {
  if (!file) return;
  document.getElementById("uploadStatus").textContent = "Parsing…";
  const fd = new FormData();
  fd.append("file", file);
  try {
    const r = await fetch(`${API}/upload/file`, { method: "POST", body: fd });
    const d = await r.json();
    if (d.error) { toast(d.error, "error"); document.getElementById("uploadStatus").textContent = ""; }
    else { toast(`Added ${d.added} transactions!`, "success"); document.getElementById("uploadStatus").textContent = `✅ ${d.added} transactions loaded`; fetchAll(); }
  } catch { toast("Upload failed — is the server running?", "error"); }
}

async function uploadSMS() {
  const text = document.getElementById("smsText").value.trim();
  if (!text) return;
  const messages = text.split("---").map(s => s.trim()).filter(Boolean);
  try {
    const r = await fetch(`${API}/upload/sms`, { method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({messages}) });
    const d = await r.json();
    toast(`Parsed ${d.added} SMS transactions!`, "success");
    document.getElementById("smsText").value = "";
    fetchAll();
  } catch { toast("SMS upload failed", "error"); }
}

async function addManual() {
  const date = document.getElementById("m-date").value;
  const desc = document.getElementById("m-desc").value;
  const amt  = parseFloat(document.getElementById("m-amt").value);
  const type = document.getElementById("m-type").value;
  if (!date || !desc || isNaN(amt)) { toast("Fill all fields", "error"); return; }
  try {
    const r = await fetch(`${API}/upload/manual`, { method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({date,description:desc,amount:amt,type}) });
    const d = await r.json();
    if (d.error) toast(d.error, "error");
    else { toast("Transaction added!", "success"); document.getElementById("m-date").value=""; document.getElementById("m-desc").value=""; document.getElementById("m-amt").value=""; fetchAll(); }
  } catch { toast("Failed", "error"); }
}

// ── Dashboard ─────────────────────────────────────────────────────────────────
function fmt(n) { return "₹" + Math.abs(n).toLocaleString("en-IN",{maximumFractionDigits:0}); }

function renderDashboard() {
  if (!analytics || analytics.count === 0) {
    document.getElementById("dashEmpty").style.display = "block";
    document.getElementById("dashContent").style.display = "none";
    return;
  }
  document.getElementById("dashEmpty").style.display = "none";
  document.getElementById("dashContent").style.display = "block";

  // Stats
  const statsData = [
    { label:"Total Spent",     value: fmt(analytics.total_debit),  sub:`${analytics.debit_count} transactions`,  color:"var(--red)" },
    { label:"Total Received",  value: fmt(analytics.total_credit), sub:`${analytics.credit_count} transactions`, color:"var(--green)" },
    { label:"Avg Daily Spend", value: fmt(analytics.avg_daily),    sub:"per day",  color:"var(--text)" },
    { label:"Net Flow",        value: fmt(analytics.net),          sub: analytics.net >= 0 ? "Surplus" : "Deficit", color: analytics.net >= 0 ? "var(--green)" : "var(--red)" },
  ];
  document.getElementById("statsGrid").innerHTML = statsData.map(s => `
    <div class="stat-card">
      <div class="label">${s.label}</div>
      <div class="stat-value" style="color:${s.color}">${s.value}</div>
      <div class="stat-sub">${s.sub}</div>
    </div>`).join("");

  // Area chart
  const dailyEntries = Object.entries(analytics.daily).slice(-30);
  const areaCtx = document.getElementById("areaChart").getContext("2d");
  if (areaChartInst) areaChartInst.destroy();
  areaChartInst = new Chart(areaCtx, {
    type: "line",
    data: {
      labels: dailyEntries.map(([d]) => d.slice(5)),
      datasets: [{
        data: dailyEntries.map(([,v]) => v),
        borderColor: "#6ee7b7", backgroundColor: "rgba(110,231,183,.15)",
        fill: true, tension: 0.4, pointRadius: 2, borderWidth: 2,
      }]
    },
    options: {
      responsive:true, plugins:{legend:{display:false}},
      scales:{
        x:{ticks:{color:"#6b7280",font:{size:10}},grid:{color:"#1f2937"}},
        y:{ticks:{color:"#6b7280",font:{size:10},callback:v=>`₹${(v/1000).toFixed(0)}k`},grid:{color:"#1f2937"}},
      }
    }
  });

  // Donut chart
  const cats = Object.entries(analytics.categories);
  const donutCtx = document.getElementById("donutChart").getContext("2d");
  if (donutChartInst) donutChartInst.destroy();
  donutChartInst = new Chart(donutCtx, {
    type: "doughnut",
    data: {
      labels: cats.map(([k]) => k),
      datasets: [{ data: cats.map(([,v]) => v.total), backgroundColor: cats.map(([,v]) => v.color), borderWidth: 2, borderColor: "#111827" }]
    },
    options: {
      responsive:true, cutout:"65%",
      plugins:{ legend:{ position:"right", labels:{ color:"#e5e7eb", font:{size:11}, boxWidth:12 } } }
    }
  });

  // Category breakdown
  const total = analytics.total_debit;
  document.getElementById("catBreakdown").innerHTML = `<div class="label">Category Breakdown</div>` +
    cats.map(([cat, data]) => {
      const pct = total ? (data.total / total * 100) : 0;
      return `<div style="margin-bottom:14px">
        <div style="display:flex;justify-content:space-between;margin-bottom:5px">
          <span style="font-size:13px">${data.icon} ${cat} <span style="color:var(--sub);font-size:11px">(${data.count})</span></span>
          <span style="font-size:13px;font-weight:700;font-family:monospace">${fmt(data.total)}</span>
        </div>
        <div class="cat-bar-bg"><div class="cat-bar-fill" style="width:${pct}%;background:${data.color}"></div></div>
      </div>`;
    }).join("");

  // Merchants
  document.getElementById("merchantList").innerHTML = `<div class="label">Top Merchants</div>` +
    analytics.top_merchants.map(m => `
      <div class="merchant-row">
        <span style="color:var(--text)">${m.name.substring(0,32)}</span>
        <span style="color:var(--red);font-weight:700;font-family:monospace">${fmt(m.amount)}</span>
      </div>`).join("");
}

// ── Transactions ──────────────────────────────────────────────────────────────
const CAT_COLORS = {
  "Food & Dining":"#f97316","Transport":"#3b82f6","Shopping":"#a855f7","Utilities":"#10b981",
  "Health":"#ef4444","Entertainment":"#f59e0b","Education":"#06b6d4","Investments":"#22c55e",
  "Transfers":"#6b7280","ATM/Cash":"#84cc16","Rent & Housing":"#f43f5e","Others":"#8b5cf6"
};

function populateCatFilter() {
  const sel = document.getElementById("txCatFilter");
  const current = sel.value;
  sel.innerHTML = '<option value="">All Categories</option>' +
    categories.map(c => `<option value="${c.name}">${c.icon} ${c.name}</option>`).join("");
  sel.value = current;
}

function renderTransactions() {
  const search = document.getElementById("txSearch").value.toLowerCase();
  const cat    = document.getElementById("txCatFilter").value;
  const type   = document.getElementById("txTypeFilter").value;

  let filtered = allTransactions
    .filter(t => !cat  || t.category === cat)
    .filter(t => !type || t.type === type)
    .filter(t => !search || t.description.toLowerCase().includes(search) || t.category.toLowerCase().includes(search));

  const tbody = document.getElementById("txBody");
  if (filtered.length === 0) {
    tbody.innerHTML = `<tr><td colspan="6" class="empty-state">No transactions found</td></tr>`;
  } else {
    tbody.innerHTML = filtered.map(t => {
      const color = t.amount > 0 ? "#f87171" : "#34d399";
      const sign  = t.amount > 0 ? "−" : "+";
      const catColor = CAT_COLORS[t.category] || "#888";
      return `<tr>
        <td style="color:var(--sub);font-family:monospace;white-space:nowrap">${t.date}</td>
        <td style="max-width:220px"><div style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${t.description}</div></td>
        <td><span class="pill" style="background:${catColor}22;color:${catColor}">${t.category}</span></td>
        <td><span style="font-size:11px;color:var(--sub);background:var(--surface);padding:2px 8px;border-radius:4px">${t.source}</span></td>
        <td style="text-align:right;font-family:monospace;font-weight:700;color:${color};white-space:nowrap">${sign}${fmt(t.amount)}</td>
        <td><button onclick="deleteTx('${t.id}')" style="background:none;border:none;color:var(--muted);cursor:pointer;font-size:16px;padding:2px 6px;border-radius:4px" title="Delete">🗑</button></td>
      </tr>`;
    }).join("");
  }

  document.getElementById("txFooter").textContent = `Showing ${filtered.length} of ${allTransactions.length} transactions`;
}

async function deleteTx(id) {
  if (!confirm("Delete this transaction?")) return;
  await fetch(`${API}/transactions/${id}`, { method: "DELETE" });
  toast("Deleted", "success");
  fetchAll();
}

// ── Clear All ─────────────────────────────────────────────────────────────────
async function clearAll() {
  if (!confirm("Clear ALL transactions? This cannot be undone.")) return;
  await fetch(`${API}/clear`, { method: "POST" });
  toast("All data cleared", "info");
  fetchAll();
}

// ── Email ─────────────────────────────────────────────────────────────────────
async function sendReport() {
  const to   = document.getElementById("e-to").value;
  const from = document.getElementById("e-from").value;
  const pass = document.getElementById("e-pass").value;
  if (!to || !from || !pass) { toast("Fill all email fields", "error"); return; }
  const btn = document.getElementById("sendBtn");
  btn.disabled = true; btn.textContent = "Sending…";
  try {
    const r = await fetch(`${API}/send-report`, {
      method: "POST", headers: {"Content-Type":"application/json"},
      body: JSON.stringify({ email:to, sender_email:from, gmail_app_password:pass, period:"Full Statement" })
    });
    const d = await r.json();
    if (d.error) toast(d.error, "error");
    else {
      document.getElementById("emailSuccessMsg").textContent = `Check your inbox at ${to}`;
      document.getElementById("emailSuccess").style.display = "flex";
      toast(`Report sent to ${to}!`, "success");
    }
  } catch { toast("Failed — is the server running?", "error"); }
  btn.disabled = false; btn.textContent = "📧 Send Report Now";
}

async function login(){

  const username = document.getElementById("loginUser").value;
  const password = document.getElementById("loginPassword").value;

  const r = await fetch("/api/login",{
      method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({username,password})
  });

  if(r.status===200){
      document.getElementById("loginScreen").style.display="none";
      fetchAll();
  }else{
      alert("Wrong username or password");
  }
}

function openSignup(){
  document.getElementById("loginScreen").style.display="none";
  document.getElementById("signupScreen").style.display="flex";
}

function backToLogin(){
  document.getElementById("signupScreen").style.display="none";
  document.getElementById("loginScreen").style.display="flex";
}

async function createAccount(){

  const username = document.getElementById("signupUser").value;
  const password = document.getElementById("signupPass").value;

  if(!username || !password){
      alert("Enter username and password");
      return;
  }

  const r = await fetch("/api/create-user",{
      method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({username,password})
  });

  const d = await r.json();

  if(d.ok){
      alert("Account created! Now login.");
      backToLogin();
  }else{
      alert(d.error || "Failed");
  }

}

// ── Init ──────────────────────────────────────────────────────────────────────
(async function init() {

  await checkHealth();

  await fetch("/api/reset-session");

  document.getElementById("familyGate").style.display="flex";
  document.getElementById("loginScreen").style.display="none";

  setInterval(checkHealth, 10000);

})();
</script>
</body>
</html>
"""

@app.route("/")
def serve_frontend():
    return FRONTEND_HTML, 200, {"Content-Type": "text/html; charset=utf-8"}


if __name__ == "__main__":
    init_db()
    print("\n🚀 SpendLens running at http://localhost:5000")
    print("   Open this in your browser: http://localhost:5000\n")
    app.run(host="0.0.0.0", port=5000, debug=False)