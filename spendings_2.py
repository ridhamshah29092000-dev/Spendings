"""
SpendLens Backend - Flask API
Handles: CSV/XLSX parsing, SMS parsing, categorization, email sending
Database: PostgreSQL (persistent on Render)
"""
import pandas as pd
from flask import Flask, request, jsonify, Response, session
from flask_cors import CORS
import csv, io, re, json, os, smtplib, hashlib
from datetime import datetime, date, timedelta
from collections import defaultdict
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from functools import wraps
import hashlib
import psycopg2
import psycopg2.extras

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from io import BytesIO

# ── Config ────────────────────────────────────────────────────────────────────
FAMILY_CODE = os.getenv("FAMILY_CODE", "SHAH2026")
DATABASE_URL = os.getenv("DATABASE_URL")  # Set this in Render environment variables

def hash_password(p):
    return hashlib.sha256(p.encode()).hexdigest()

app = Flask(__name__)
CORS(app, supports_credentials=True)

from werkzeug.middleware.proxy_fix import ProxyFix
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

app.secret_key = os.getenv("APP_SECRET", "change-this-secret")
app.config.update(
    SESSION_COOKIE_SAMESITE="None",
    SESSION_COOKIE_SECURE=True
)
app.config["SESSION_COOKIE_DOMAIN"] = None

# ── PostgreSQL helpers ────────────────────────────────────────────────────────
def db():
    """Return a new psycopg2 connection with RealDictCursor as default."""
    conn = psycopg2.connect(DATABASE_URL)
    return conn

def cursor(conn):
    return conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

def init_db():
    conn = db()
    cur = cursor(conn)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            id TEXT PRIMARY KEY,
            user_id INTEGER REFERENCES users(id),
            date TEXT,
            description TEXT,
            amount REAL,
            type TEXT,
            balance REAL,
            category TEXT,
            source TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            id INTEGER PRIMARY KEY,
            email TEXT,
            sender_email TEXT,
            gmail_app_password TEXT
        )
    """)
    conn.commit()
    cur.close()
    conn.close()

# ── Auth routes ───────────────────────────────────────────────────────────────
@app.route("/api/verify-family", methods=["POST"])
def verify_family():
    data = request.json or {}
    if data.get("code") == FAMILY_CODE:
        session["family_ok"] = True
        return jsonify({"ok": True})
    return jsonify({"error": "Invalid family code"}), 401

@app.route("/api/login", methods=["POST"])
def login():
    if not session.get("family_ok"):
        return jsonify({"error": "Family access required"}), 403
    data = request.json or {}
    username = data.get("username")
    password = data.get("password")
    conn = db()
    cur = cursor(conn)
    cur.execute("SELECT * FROM users WHERE username=%s", (username,))
    user = cur.fetchone()
    cur.close()
    conn.close()
    if user and user["password_hash"] == hash_password(password):
        session["user_id"] = user["id"]
        session["username"] = user["username"]
        return jsonify({"ok": True})
    return jsonify({"error": "Invalid login"}), 401

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
    return jsonify({"ok": True})

@app.route("/api/create-user", methods=["POST"])
def create_user():
    if not session.get("family_ok"):
        return jsonify({"error": "Family code required"}), 403
    data = request.json or {}
    username = data.get("username")
    password = hash_password(data.get("password", ""))
    conn = db()
    cur = cursor(conn)
    try:
        cur.execute("INSERT INTO users(username,password_hash) VALUES(%s,%s)", (username, password))
        conn.commit()
    except psycopg2.errors.UniqueViolation:
        conn.rollback()
        cur.close()
        conn.close()
        return jsonify({"error": "User exists"}), 400
    cur.close()
    conn.close()
    return jsonify({"ok": True})

@app.route("/api/reset-password", methods=["POST"])
def reset_password():
    data = request.json or {}
    username = data.get("username")
    new_password = data.get("password")
    code = data.get("family_code")
    if code != FAMILY_CODE:
        return jsonify({"error": "Invalid family code"}), 401
    conn = db()
    cur = cursor(conn)
    cur.execute("SELECT id FROM users WHERE username=%s", (username,))
    user = cur.fetchone()
    if not user:
        cur.close()
        conn.close()
        return jsonify({"error": "User not found"}), 404
    cur.execute("UPDATE users SET password_hash=%s WHERE username=%s", (hash_password(new_password), username))
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({"ok": True})

def auth_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("user_id"):
            return jsonify({"error": "Unauthorized"}), 401
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
    "Food & Dining":"#f97316","Transport":"#3b82f6","Shopping":"#a855f7",
    "Utilities":"#10b981","Health":"#ef4444","Entertainment":"#f59e0b",
    "Education":"#06b6d4","Investments":"#22c55e","Transfers":"#6b7280",
    "ATM/Cash":"#84cc16","Rent & Housing":"#f43f5e","Others":"#8b5cf6",
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

# ── Description Cleaning ──────────────────────────────────────────────────────
def clean_description(desc: str) -> str:
    if not desc:
        return "Transaction"
    d = desc.upper()
    for prefix in ["UPI-", "UPI/", "IMPS-", "NEFT-"]:
        d = d.replace(prefix, "")
    d = re.sub(r'@\w+', '', d)
    d = re.sub(r'\b\d{7,}\b', '', d)
    d = re.sub(r'\b[A-Z0-9]{8,}\b', '', d)
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
        if not any(clean): continue
        if any("statement" in c for c in clean): continue
        if any("account" in c for c in clean): continue
        if any("*****" in c for c in clean): continue
        if (any("date" in c for c in clean) and
                any(x in clean for x in ["narration","description","particulars","remarks"])):
            header = clean
            for i, h in enumerate(clean):
                if "date" in h: date_idx = i
                if any(x in h for x in ["narration","description","particulars","remarks"]): desc_idx = i
                if any(x in h for x in ["withdrawal","debit","dr"]): debit_idx = i
                if any(x in h for x in ["deposit","credit","cr"]): credit_idx = i
                if "balance" in h: bal_idx = i
            continue
        if header is None: continue
        try:
            def amt(i):
                if i is None or i >= len(row): return 0
                return float(row[i].replace(",", "") or 0)
            debit = amt(debit_idx)
            credit = amt(credit_idx)
            bal = amt(bal_idx)
            if debit == 0 and credit == 0: continue
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
    return transactions

# ── XLSX Parser ───────────────────────────────────────────────────────────────
def parse_xlsx(file_bytes):
    transactions = []
    df = pd.read_excel(io.BytesIO(file_bytes), engine="openpyxl")
    df.columns = [c.lower().strip() for c in df.columns]
    date_col = desc_col = debit_col = credit_col = bal_col = None
    for c in df.columns:
        if "date" in c and not date_col: date_col = c
        if any(k in c for k in ["description","narration","remarks","particulars"]) and not desc_col: desc_col = c
        if any(k in c for k in ["debit","withdrawal","dr"]) and not debit_col: debit_col = c
        if any(k in c for k in ["credit","deposit","cr"]) and not credit_col: credit_col = c
        if "balance" in c and not bal_col: bal_col = c
    for _, row in df.iterrows():
        debit = float(row.get(debit_col, 0) or 0)
        credit = float(row.get(credit_col, 0) or 0)
        if debit == 0 and credit == 0: continue
        desc = str(row.get(desc_col, ""))
        t = {
            "date": str(row.get(date_col)),
            "description": desc,
            "amount": debit if debit > 0 else -credit,
            "type": "debit" if debit > 0 else "credit",
            "balance": float(row.get(bal_col, 0) or 0),
            "category": categorize(desc),
            "source": "xlsx"
        }
        t["id"] = make_id(t)
        transactions.append(t)
    return transactions

# ── SMS Parser ────────────────────────────────────────────────────────────────
SMS_PATTERNS = [
    (r"(?:debited|debit|dr)\s+(?:with\s+)?(?:rs\.?|inr|₹)\s*([\d,]+\.?\d*)", "debit"),
    (r"(?:rs\.?|inr|₹)\s*([\d,]+\.?\d*)\s+(?:debited|debit|dr|spent|paid)", "debit"),
    (r"spent\s+(?:rs\.?|inr|₹)\s*([\d,]+\.?\d*)", "debit"),
    (r"payment\s+of\s+(?:rs\.?|inr|₹)\s*([\d,]+\.?\d*)", "debit"),
    (r"withdrawn\s+(?:rs\.?|inr|₹)\s*([\d,]+\.?\d*)", "debit"),
    (r"(?:credited|credit|cr)\s+(?:with\s+)?(?:rs\.?|inr|₹)\s*([\d,]+\.?\d*)", "credit"),
    (r"(?:rs\.?|inr|₹)\s*([\d,]+\.?\d*)\s+(?:credited|credit|cr|received)", "credit"),
    (r"received\s+(?:rs\.?|inr|₹)\s*([\d,]+\.?\d*)", "credit"),
]

def parse_sms(sms_text: str):
    low = sms_text.lower()
    bank_keywords = ["sbi","hdfc","icici","axis","kotak","bank","account","a/c","ac no","upi","neft","imps","atm"]
    if not any(k in low for k in bank_keywords):
        return None
    txn_type = "debit"
    amount = None
    for pattern, t in SMS_PATTERNS:
        m = re.search(pattern, low)
        if m:
            txn_type = t
            amount = float(m.group(1).replace(",", ""))
            break
    if not amount:
        return None
    desc = "SMS Transaction"
    for marker in ["at ", "to ", "for ", "towards ", "from ", "via "]:
        idx = low.find(marker)
        if idx != -1:
            snippet = sms_text[idx+len(marker):idx+len(marker)+40].split(".")[0].split(",")[0].strip()
            if snippet:
                desc = snippet
                break
    balance = 0.0
    bal_match = re.search(r"(?:avl|avail|available|bal|balance)\.?\s*(?:rs\.?|inr|₹)?\s*([\d,]+\.?\d*)", low)
    if bal_match:
        balance = float(bal_match.group(1).replace(",", ""))
    t = {
        "date": date.today().isoformat(),
        "description": desc,
        "amount": amount if txn_type == "debit" else -amount,
        "type": txn_type,
        "balance": balance,
        "category": categorize(desc),
        "source": "sms"
    }
    t["id"] = make_id(t)
    return t

# ── Analytics ─────────────────────────────────────────────────────────────────
def compute_analytics(txns: list) -> dict:
    debits  = [t for t in txns if t["amount"] > 0]
    credits = [t for t in txns if t["amount"] < 0]
    total_debit  = sum(t["amount"] for t in debits)
    total_credit = sum(-t["amount"] for t in credits)
    cat_groups = defaultdict(lambda: {"total": 0, "count": 0})
    for t in debits:
        g = cat_groups[t["category"]]
        g["total"] += t["amount"]
        g["count"] += 1
    daily = defaultdict(float)
    for t in debits:
        daily[t["date"]] += t["amount"]
    merchant_totals = defaultdict(float)
    for t in debits:
        merchant_totals[t["description"][:30]] += t["amount"]
    top_merchants = sorted(merchant_totals.items(), key=lambda x: x[1], reverse=True)[:8]
    return {
        "total_debit":   round(total_debit, 2),
        "total_credit":  round(total_credit, 2),
        "net":           round(total_credit - total_debit, 2),
        "count":         len(txns),
        "debit_count":   len(debits),
        "credit_count":  len(credits),
        "categories":    {k: {"total": round(v["total"], 2), "count": v["count"], "color": CAT_COLORS.get(k, "#888"), "icon": CAT_ICONS.get(k, "📦")}
                          for k, v in sorted(cat_groups.items(), key=lambda x: x[1]["total"], reverse=True)},
        "daily":         dict(sorted(daily.items())),
        "top_merchants": [{"name": m[0], "amount": round(m[1], 2)} for m in top_merchants],
        "avg_daily":     round(total_debit / max(len(daily), 1), 2),
    }


import matplotlib.pyplot as plt
import base64

def generate_daily_chart(daily_data):

    dates = list(daily_data.keys())
    values = list(daily_data.values())

    if not dates:
        return ""

    plt.figure(figsize=(10,3))
    plt.plot(dates, values, color="#6ee7b7", linewidth=2)
    plt.fill_between(dates, values, alpha=0.25)

    plt.xticks(rotation=45)
    plt.tight_layout()

    buf = BytesIO()
    plt.savefig(buf, format="png")
    plt.close()

    buf.seek(0)
    img_base64 = base64.b64encode(buf.read()).decode("utf-8")

    return img_base64


def weekly_summary(txns):

    weeks = defaultdict(float)

    for t in txns:
        if t["amount"] > 0:
            d = datetime.fromisoformat(t["date"])
            week = f"{d.year}-W{d.isocalendar()[1]}"
            weeks[week] += t["amount"]

    return sorted(weeks.items())

# ── Email HTML builder ────────────────────────────────────────────────────────
def build_email_html(analytics: dict, txns: list, period: str = "") -> str:
    cats  = analytics["categories"]
    total = analytics["total_debit"]
    today = datetime.now().strftime("%A, %d %B %Y")
    cat_rows = ""
    for cat, data in cats.items():
        pct = (data["total"] / total * 100) if total else 0
        color = data["color"]
        cat_rows += f"""
        <tr>
          <td style="padding:10px 8px;font-size:13px;color:#374151">{data['icon']} {cat}</td>
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
    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<title>SpendLens Report</title></head>
<body style="margin:0;padding:24px;background:#f1f5f9;font-family:'Segoe UI',Arial,sans-serif">
<div style="max-width:680px;margin:0 auto">
  <div style="background:linear-gradient(135deg,#0f172a,#1e293b);border-radius:16px 16px 0 0;padding:32px;color:white">
    <div style="font-size:28px;font-weight:800;margin-bottom:4px">💳 Your Spend Report</div>
    <div style="font-size:13px;color:#94a3b8">{today}{' · ' + period if period else ''}</div>
  </div>
  <div style="background:#0f172a;padding:0 32px">
    <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:0;border-top:1px solid #1e293b">
      <div style="padding:24px 16px;border-right:1px solid #1e293b">
        <div style="font-size:10px;letter-spacing:2px;color:#475569;margin-bottom:6px">TOTAL SPENT</div>
        <div style="font-size:26px;font-weight:800;color:#f8fafc">₹{analytics['total_debit']:,.0f}</div>
        <div style="font-size:11px;color:#dc2626">↓ {analytics['debit_count']} transactions</div>
      </div>
      <div style="padding:24px 16px;border-right:1px solid #1e293b">
        <div style="font-size:10px;letter-spacing:2px;color:#475569;margin-bottom:6px">TOTAL RECEIVED</div>
        <div style="font-size:26px;font-weight:800;color:#f8fafc">₹{analytics['total_credit']:,.0f}</div>
        <div style="font-size:11px;color:#16a34a">↑ {analytics['credit_count']} transactions</div>
      </div>
      <div style="padding:24px 16px">
        <div style="font-size:10px;letter-spacing:2px;color:#475569;margin-bottom:6px">AVG DAILY SPEND</div>
        <div style="font-size:26px;font-weight:800;color:#f8fafc">₹{analytics['avg_daily']:,.0f}</div>
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
        <th style="padding:8px;text-align:left;font-size:10px;color:#9ca3af;font-weight:normal">DATE</th>
        <th style="padding:8px;text-align:left;font-size:10px;color:#9ca3af;font-weight:normal">DESCRIPTION</th>
        <th style="padding:8px;text-align:left;font-size:10px;color:#9ca3af;font-weight:normal">CATEGORY</th>
        <th style="padding:8px;text-align:right;font-size:10px;color:#9ca3af;font-weight:normal">AMOUNT</th>
      </tr></thead>
      <tbody>{txn_rows}</tbody>
    </table>
  </div>
  <div style="background:#f8fafc;border-radius:0 0 16px 16px;padding:20px 32px;text-align:center">
    <p style="font-size:11px;color:#9ca3af;margin:0;letter-spacing:2px">SPENDLENS · AUTO-GENERATED</p>
  </div>
</div></body></html>"""

# ── API Routes ────────────────────────────────────────────────────────────────
@app.route("/api/health")
def health():
    conn = db()
    cur = cursor(conn)
    cur.execute("SELECT COUNT(*) as cnt FROM transactions")
    count = cur.fetchone()["cnt"]
    cur.close()
    conn.close()
    return jsonify({"status": "ok", "transactions": count})

@app.route("/api/reset-session")
def reset_session():
    session.clear()
    return jsonify({"ok": True})

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
    cur = cursor(conn)
    added = 0
    for t in new_txns:
        try:
            cur.execute("""
                INSERT INTO transactions (id,user_id,date,description,amount,type,balance,category,source)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (id) DO NOTHING
            """, (t["id"], session["user_id"], t["date"], t["description"],
                  t["amount"], t["type"], t["balance"], t["category"], t["source"]))
            if cur.rowcount > 0:
                added += 1
        except:
            pass
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({"added": added})

@app.route("/api/upload/sms", methods=["POST"])
@auth_required
def upload_sms():
    data = request.json or {}
    messages = data.get("messages", [])
    if isinstance(messages, str):
        messages = [messages]
    conn = db()
    cur = cursor(conn)
    cur.execute("SELECT id FROM transactions WHERE user_id=%s", (session["user_id"],))
    existing_ids = {r["id"] for r in cur.fetchall()}
    added = 0
    for sms in messages:
        t = parse_sms(sms)
        if t and t["id"] not in existing_ids:
            try:
                cur.execute("""
                    INSERT INTO transactions (id,user_id,date,description,amount,type,balance,category,source)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (id) DO NOTHING
                """, (t["id"], session["user_id"], t["date"], t["description"],
                      t["amount"], t["type"], t["balance"], t["category"], t["source"]))
                if cur.rowcount > 0:
                    existing_ids.add(t["id"])
                    added += 1
            except:
                pass
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({"added": added})

@app.route("/api/upload/manual", methods=["POST"])
@auth_required
def upload_manual():
    t = request.json or {}
    required = ["date", "description", "amount", "type"]
    if not all(k in t for k in required):
        return jsonify({"error": "Missing fields"}), 400
    t["category"] = categorize(t["description"])
    t["source"]   = "manual"
    t["balance"]  = 0
    t["id"]       = make_id(t)
    conn = db()
    cur = cursor(conn)
    cur.execute("""
        INSERT INTO transactions (id,user_id,date,description,amount,type,balance,category,source)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (id) DO NOTHING
    """, (t["id"], session["user_id"], t["date"], t["description"],
          t["amount"], t["type"], t["balance"], t["category"], t["source"]))
    if cur.rowcount == 0:
        cur.close()
        conn.close()
        return jsonify({"error": "Duplicate"}), 409
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({"added": 1})

@app.route("/api/transactions")
@auth_required
def get_transactions():
    conn = db()
    cur = cursor(conn)
    cur.execute("SELECT * FROM transactions WHERE user_id=%s ORDER BY date DESC", (session["user_id"],))
    txns = [dict(r) for r in cur.fetchall()]
    cur.close()
    conn.close()
    return jsonify(txns)

@app.route("/api/transactions/<tid>", methods=["PATCH"])
@auth_required
def update_transaction(tid):
    data = request.json or {}
    conn = db()
    cur = cursor(conn)
    if "category" in data:
        cur.execute("UPDATE transactions SET category=%s WHERE id=%s AND user_id=%s",
                    (data["category"], tid, session["user_id"]))
    if "description" in data:
        cur.execute("UPDATE transactions SET description=%s WHERE id=%s AND user_id=%s",
                    (data["description"], tid, session["user_id"]))
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({"updated": True})

@app.route("/api/transactions/<tid>", methods=["DELETE"])
@auth_required
def delete_transaction(tid):
    conn = db()
    cur = cursor(conn)
    cur.execute("DELETE FROM transactions WHERE id=%s AND user_id=%s", (tid, session["user_id"]))
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({"deleted": 1})

@app.route("/api/analytics")
@auth_required
def get_analytics():
    conn = db()
    cur = cursor(conn)
    cur.execute("SELECT * FROM transactions WHERE user_id=%s", (session["user_id"],))
    txns = [dict(r) for r in cur.fetchall()]
    cur.close()
    conn.close()
    return jsonify(compute_analytics(txns))

@app.route("/api/settings", methods=["GET", "POST"])
def settings():
    conn = db()
    cur = cursor(conn)
    if request.method == "POST":
        data = request.json or {}
        cur.execute("""
            INSERT INTO settings (id,email,sender_email,gmail_app_password)
            VALUES (1,%s,%s,%s)
            ON CONFLICT (id) DO UPDATE SET email=EXCLUDED.email,
            sender_email=EXCLUDED.sender_email, gmail_app_password=EXCLUDED.gmail_app_password
        """, (data.get("email"), data.get("sender_email"), data.get("gmail_app_password")))
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({"ok": True})
    cur.execute("SELECT email,sender_email FROM settings WHERE id=1")
    row = cur.fetchone()
    cur.close()
    conn.close()
    return jsonify(dict(row) if row else {})

@app.route("/api/send-report", methods=["POST"])
@auth_required
def send_report():
    conn = db()
    cur = cursor(conn)
    cur.execute("SELECT email,sender_email,gmail_app_password FROM settings WHERE id=1")
    row = cur.fetchone()
    if not row:
        cur.close()
        conn.close()
        return jsonify({"error": "Email settings not configured"}), 400
    to_email = row["email"]
    sender   = row["sender_email"]
    password = row["gmail_app_password"]
    if not all([to_email, sender, password]):
        cur.close()
        conn.close()
        return jsonify({"error": "Email credentials not configured"}), 400
    cur.execute("SELECT * FROM transactions WHERE user_id=%s", (session["user_id"],))
    txns = [dict(r) for r in cur.fetchall()]
    cur.close()
    conn.close()
    data   = request.json or {}
    period = data.get("period", "")
    analytics_data = compute_analytics(txns)
    html   = build_email_html(analytics_data, sorted(txns, key=lambda x: x["date"], reverse=True), period)
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

@app.route("/api/download-report")
@auth_required
def download_report():
    conn = db()
    cur = cursor(conn)
    cur.execute("SELECT * FROM transactions WHERE user_id=%s", (session["user_id"],))
    txns = [dict(r) for r in cur.fetchall()]
    cur.close()
    conn.close()
    analytics_data = compute_analytics(txns)
    html = build_email_html(analytics_data, sorted(txns, key=lambda x: x["date"], reverse=True), "SpendLens Report")
    return Response(html, headers={"Content-Type": "text/html", "Content-Disposition": "attachment; filename=spendlens_report.html"})

from weasyprint import HTML
from flask import send_file

@app.route("/api/download-report-pdf")
@auth_required
def download_report_pdf():

    from_date = request.args.get("from")
    to_date = request.args.get("to")

    conn = db()
    cur = cursor(conn)

    query = "SELECT * FROM transactions WHERE user_id=%s"
    params = [session["user_id"]]

    if from_date and to_date:
        query += " AND date BETWEEN %s AND %s"
        params.extend([from_date, to_date])

    cur.execute(query, params)

    txns = [dict(r) for r in cur.fetchall()]

    cur.close()
    conn.close()

    analytics_data = compute_analytics(txns)

    chart = generate_daily_chart(analytics_data["daily"])

    weekly = weekly_summary(txns)

    weekly_rows = "".join(
        f"<tr><td>{w}</td><td>₹{v:,.0f}</td></tr>"
        for w,v in weekly
    )

    base_html = build_email_html(
        analytics_data,
        sorted(txns, key=lambda x: x["date"], reverse=True),
        "SpendLens Report"
    )

    extra_pages = f"""
    <div style="page-break-before:always"></div>

    <h2 style="font-size:18px;margin-top:20px">Daily Spending Trend</h2>

    <img src="data:image/png;base64,{chart}" style="width:100%;border-radius:10px"/>

    <div style="page-break-before:always"></div>

    <h2 style="font-size:18px;margin-top:20px">Weekly Summary</h2>

    <table style="width:100%;border-collapse:collapse">
        <thead>
            <tr>
                <th style="text-align:left;padding:8px">Week</th>
                <th style="text-align:right;padding:8px">Total Spend</th>
            </tr>
        </thead>
        <tbody>
            {weekly_rows}
        </tbody>
    </table>
    """

    final_html = base_html.replace("</body>", extra_pages + "</body>")

    pdf_bytes = HTML(string=final_html).write_pdf()

    return Response(
        pdf_bytes,
        mimetype="application/pdf",
        headers={"Content-Disposition":"attachment; filename=spendlens_report.pdf"}
    )

@app.route("/api/preview-report")
@auth_required
def preview_report():
    conn = db()
    cur = cursor(conn)
    cur.execute("SELECT * FROM transactions WHERE user_id=%s", (session["user_id"],))
    txns = [dict(r) for r in cur.fetchall()]
    cur.close()
    conn.close()
    analytics_data = compute_analytics(txns)
    html = build_email_html(analytics_data, sorted(txns, key=lambda x: x["date"], reverse=True))
    return Response(html, mimetype="text/html")

@app.route("/api/clear", methods=["POST"])
@auth_required
def clear_data():
    conn = db()
    cur = cursor(conn)
    cur.execute("DELETE FROM transactions WHERE user_id=%s", (session["user_id"],))
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({"ok": True})

@app.route("/api/categories")
def get_categories():
    return jsonify([{"name": k, "color": CAT_COLORS.get(k, "#888"), "icon": CAT_ICONS.get(k, "📦")} for k in CATEGORIES])

# ── Frontend ──────────────────────────────────────────────────────────────────
FRONTEND_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<link rel="manifest" href="/manifest.json">
<meta name="theme-color" content="#070b12">
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<title>SpendLens · Bank Analytics</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<style>

body{
  padding-top: env(safe-area-inset-top);
  padding-bottom: env(safe-area-inset-bottom);
}

@import url('https://fonts.googleapis.com/css2?family=DM+Mono:wght@400;500;700&display=swap');
*{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent}
:root{
  --bg:#070b12;--surface:#0d1117;--card:#111827;--border:#1f2937;
  --accent:#6ee7b7;--red:#f87171;--green:#34d399;--muted:#4b5563;
  --text:#e5e7eb;--sub:#6b7280;
}
html,body{
  background:var(--bg);
  color:var(--text);
  font-family:'DM Mono',monospace;
  min-height:100vh;
  width:100%;
  max-width:100%;
  overflow-x:hidden;
}
::-webkit-scrollbar{width:4px;height:4px}
::-webkit-scrollbar-track{background:var(--surface)}
::-webkit-scrollbar-thumb{background:var(--border);border-radius:2px}

/* ── Header ── */
.header{background:var(--surface);border-bottom:1px solid var(--border);padding:0 20px;
  position:sticky;top:0;z-index:200}
.header-inner{max-width:1100px;margin:0 auto;display:flex;align-items:center;
  justify-content:space-between;padding:14px 0}
.logo{font-size:20px;font-weight:800;letter-spacing:3px;color:var(--accent)}
.logo-sub{font-size:10px;color:var(--sub);letter-spacing:2px;margin-left:10px}
.status-dot{width:7px;height:7px;border-radius:50%;display:inline-block;margin-right:5px}
.status-text{font-size:11px;color:var(--sub)}

/* ── Desktop Tabs ── */
.tabs{background:var(--surface);border-bottom:1px solid var(--border);padding:0 20px}
.tabs-inner{max-width:1100px;margin:0 auto;display:flex;gap:4px}
.tab-btn{padding:13px 18px;background:none;border:none;border-bottom:2px solid transparent;
  color:var(--sub);cursor:pointer;font-size:12px;font-family:'DM Mono',monospace;
  font-weight:400;transition:all .15s;white-space:nowrap}
.tab-btn.active{border-bottom-color:var(--accent);color:var(--accent);font-weight:700}
@media(max-width:700px){.tabs{display:none}}

/* ── Bottom Nav (Mobile) ── */
.bottom-nav{
  position:fixed;
  bottom:0;
  left:0;
  width:100%;
  background:var(--surface);
  border-top:1px solid var(--border);
  display:none;
  justify-content:space-around;
  align-items:center;
  height:78px;
  z-index:300;
  padding-top:8px;  
   padding-bottom: calc(env(safe-area-inset-bottom) + 4px);
}
.bottom-nav button{
  background:none;
  border:none;
  color:var(--sub);
  font-size:26px;
  display:flex;
  flex-direction:column;
  align-items:center;
  justify-content:center;
  gap:3px;
  width:25%;
  height:100%;
  cursor:pointer;
  transition:color .15s;
}
.bottom-nav button span{
  font-size:12px;
   margin-top:2px;
  font-family:'DM Mono',monospace;
}
.bottom-nav button.active{color:var(--accent)}
@media(max-width:700px){.bottom-nav{display:flex}}

/* ── Content ── */
.content{
  max-width:1100px;
  margin:0 auto;
  padding:16px;
}
@media(max-width:700px){
  .content{padding:16px 14px;padding-bottom:80px}
}

/* ── Cards ── */
.card{background:var(--card);border:1px solid var(--border);border-radius:14px;
  padding:20px;margin-bottom:16px}
.label{font-size:10px;letter-spacing:3px;color:var(--muted);text-transform:uppercase;margin-bottom:10px}

/* ── Stats Grid ── */
.stats-grid{display:grid;grid-template-columns:repeat(2,1fr);gap:12px;margin-bottom:16px}
@media(min-width:600px){.stats-grid{grid-template-columns:repeat(4,1fr)}}
.stat-card{background:var(--card);border:1px solid var(--border);border-radius:14px;padding:18px}
.stat-value{font-size:22px;font-weight:800;margin-top:4px}
@media(min-width:600px){.stat-value{font-size:28px}}
.stat-sub{font-size:11px;color:var(--sub);margin-top:3px}

/* ── Charts ── */
.charts-grid{display:grid;grid-template-columns:1fr;gap:14px;margin-bottom:14px}
@media(min-width:700px){.charts-grid{grid-template-columns:2fr 1fr}}
.charts-grid2{display:grid;grid-template-columns:1fr;gap:14px;margin-bottom:14px}
@media(min-width:700px){.charts-grid2{grid-template-columns:1fr 1fr}}

/* ── Buttons ── */
.btn{display:inline-flex;align-items:center;justify-content:center;
  padding:14px 20px;min-height:48px;border-radius:10px;border:none;
  cursor:pointer;font-family:'DM Mono',monospace;font-size:13px;
  font-weight:700;letter-spacing:1px;transition:all .15s;width:100%}
.btn-primary{background:var(--accent);color:#000}
.btn-primary:active{opacity:.85}
.btn-ghost{background:transparent;color:var(--text);border:1px solid var(--border)}
.btn-danger{background:#dc2626;color:#fff}
.btn:disabled{opacity:.5;cursor:not-allowed}
.btn-sm{padding:8px 14px;min-height:36px;font-size:12px;width:auto}

/* ── Inputs ── */
.input{background:var(--surface);border:1px solid var(--border);border-radius:10px;
  color:var(--text);padding:14px 16px;width:100%;
  font-family:'DM Mono',monospace;font-size:16px;outline:none;
  -webkit-appearance:none;appearance:none}
.input:focus{border-color:var(--accent)}
select.input option{background:var(--card)}

/* ── Dropzone ── */
.dropzone{border:2px dashed var(--border);border-radius:12px;padding:44px 20px;
  text-align:center;cursor:pointer;transition:all .2s}
.dropzone:hover,.dropzone.drag{border-color:var(--accent);background:#6ee7b711}
.dropzone-icon{font-size:36px;margin-bottom:10px}

/* ── Table ── */
.table-wrap{
  overflow-x:auto;
  border-radius:14px;
  border:1px solid var(--border);
  -webkit-overflow-scrolling:touch;
}
table{
  width:100%;
  border-collapse:collapse;
  font-size:12px;
  min-width:420px;
}
thead tr{background:var(--surface);border-bottom:1px solid var(--border)}
th{padding:11px 14px;text-align:left;color:var(--sub);font-weight:500;
   font-size:10px;letter-spacing:2px;white-space:nowrap}
tbody tr{border-bottom:1px solid var(--border);transition:background .1s}
tbody tr:hover{background:var(--surface)}
td{padding:11px 14px;color:var(--text)}
.table-footer{padding:10px 14px;font-size:12px;color:var(--sub);
  background:var(--surface);border-top:1px solid var(--border);border-radius:0 0 14px 14px}

/* ── Pill ── */
.pill{display:inline-block;padding:3px 10px;border-radius:20px;font-size:11px;font-weight:700}

/* ── Category bar ── */
.cat-bar-bg{height:4px;background:var(--border);border-radius:2px;margin-top:5px}
.cat-bar-fill{height:100%;border-radius:2px;transition:width .6s ease}

/* ── Method selector ── */
.method-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-bottom:16px}
@media(max-width:500px){.method-grid{grid-template-columns:1fr}}
.method-btn{padding:14px;border-radius:12px;border:2px solid var(--border);
  background:var(--card);color:var(--text);cursor:pointer;text-align:left;transition:all .15s}
.method-btn.active{border-color:var(--accent);background:#6ee7b711}
.method-title{font-size:14px;font-weight:700;margin-bottom:3px}
.method-desc{font-size:11px;color:var(--sub)}

/* ── Toast ── */
#toasts{position:fixed;bottom:80px;right:16px;z-index:9999;
  display:flex;flex-direction:column;gap:8px;pointer-events:none}
@media(min-width:700px){#toasts{bottom:24px;right:24px}}
.toast{padding:12px 16px;border-radius:10px;font-size:13px;
  font-family:'DM Mono',monospace;box-shadow:0 4px 20px rgba(0,0,0,.5);
  animation:slideIn .2s ease;color:#fff;pointer-events:all}
.toast.success{background:#14532d}
.toast.error{background:#7f1d1d}
.toast.info{background:#1e3a5f}
@keyframes slideIn{from{opacity:0;transform:translateX(20px)}to{opacity:1;transform:none}}

/* ── Offline banner ── */
.offline-banner{background:#7f1d1d;padding:10px 20px;text-align:center;font-size:12px}

/* ── Empty state ── */
.empty-state{padding:50px;text-align:center;color:var(--sub)}

/* ── Form grid ── */
.form-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:12px}
@media(max-width:500px){.form-grid{grid-template-columns:1fr}}

/* ── Misc ── */
.hint{background:var(--surface);border-radius:10px;padding:14px;margin-top:14px}
code{background:#1e293b;padding:2px 8px;border-radius:4px;
  color:var(--accent);font-family:'DM Mono',monospace;font-size:12px}
.merchant-row{display:flex;justify-content:space-between;padding:10px 0;
  border-bottom:1px solid var(--border);font-size:13px}

/* ── Auth screens ── */
.auth-overlay{position:fixed;top:0;left:0;width:100%;height:100%;
  display:flex;align-items:center;justify-content:center;z-index:9999;
  padding:20px}
.auth-card{background:#111827;padding:36px 32px;border-radius:16px;
  width:100%;max-width:340px;text-align:center;border:1px solid var(--border)}
.auth-card h2{margin-bottom:20px;color:var(--accent);letter-spacing:2px;font-size:16px}
.auth-stack{display:flex;flex-direction:column;gap:10px}

/* ── Splash ── */
#splashScreen{
  position:fixed;top:0;left:0;width:100%;height:100%;
  background:linear-gradient(135deg,#020617,#0f172a);
  display:flex;flex-direction:column;align-items:center;
  justify-content:center;z-index:100000;text-align:center;padding:20px
}
</style>
</head>
<body>

<!-- Toast container -->
<div id="toasts"></div>

<!-- Splash -->
<div id="splashScreen">
  <div style="font-size:48px;margin-bottom:14px">💳</div>
  <div style="font-size:26px;font-weight:800;letter-spacing:3px;color:var(--accent);margin-bottom:8px">SPENDLENS</div>
  <div style="font-size:12px;color:#64748b;letter-spacing:2px;margin-bottom:44px">PERSONAL FINANCE ANALYTICS</div>
  <button class="btn btn-primary" onclick="enterApp()" style="width:200px;font-size:15px">Enter App</button>
</div>

<!-- Family Gate -->
<div id="familyGate" class="auth-overlay" style="background:var(--bg);display:none">
  <div class="auth-card">
    <h2>FAMILY ACCESS</h2>
    <div class="auth-stack">
      <input id="familyCode" type="password" placeholder="Enter family code" class="input"
        onkeydown="if(event.key==='Enter')verifyFamily()">
      <button class="btn btn-primary" onclick="verifyFamily()">Enter →</button>
    </div>
  </div>
</div>

<!-- Login -->
<div id="loginScreen" class="auth-overlay" style="background:var(--bg);display:none">
  <div class="auth-card">
    <h2>LOGIN</h2>
    <div class="auth-stack">
      <input id="loginUser" type="text" placeholder="Username" class="input"
        autocapitalize="none" onkeydown="if(event.key==='Enter')login()">
      <input id="loginPassword" type="password" placeholder="Password" class="input"
        onkeydown="if(event.key==='Enter')login()">
      <button class="btn btn-primary" onclick="login()">Login</button>
      <button class="btn btn-ghost" onclick="openSignup()">Create Account</button>
      <button class="btn btn-ghost" onclick="forgotPassword()" style="font-size:11px;min-height:36px">Forgot Password?</button>
    </div>
  </div>
</div>

<!-- Signup -->
<div id="signupScreen" class="auth-overlay" style="background:var(--bg);display:none">
  <div class="auth-card">
    <h2>CREATE ACCOUNT</h2>
    <div class="auth-stack">
      <input id="signupUser" type="text" placeholder="Username" class="input" autocapitalize="none">
      <input id="signupPass" type="password" placeholder="Password" class="input">
      <button class="btn btn-primary" onclick="createAccount()">Create Account</button>
      <button class="btn btn-ghost" onclick="backToLogin()">← Back to Login</button>
    </div>
  </div>
</div>

<!-- Header -->
<div class="header">
  <div class="header-inner">
    <div style="display:flex;align-items:center">
      <div class="logo">SPENDLENS</div>
      <div class="logo-sub">· ANALYTICS</div>
    </div>
    <div style="display:flex;align-items:center;gap:12px">
      <div>
        <span class="status-dot" id="statusDot" style="background:var(--muted)"></span>
        <span class="status-text" id="statusText">connecting…</span>
      </div>
      <button class="btn btn-danger btn-sm" onclick="clearAll()" id="clearBtn" style="display:none">Clear All</button>
    </div>
  </div>
</div>

<!-- Offline banner -->
<div class="offline-banner" id="offlineBanner" style="display:none">
  ⚠️ Server not reachable. Check your deployment.
</div>

<!-- Desktop Tabs -->
<div class="tabs">
  <div class="tabs-inner">
    <button class="tab-btn active" id="tab-dashboard" onclick="showTab('dashboard')">📊 Dashboard</button>
    <button class="tab-btn" id="tab-import" onclick="showTab('import')">📥 Import Data</button>
    <button class="tab-btn" id="tab-transactions" onclick="showTab('transactions')">📋 Transactions</button>
    <button class="tab-btn" id="tab-email" onclick="showTab('email')">📧 Reports</button>
  </div>
</div>

<!-- ── DASHBOARD ── -->
<div class="content" id="page-dashboard">
  <div id="dashEmpty" class="empty-state" style="font-size:14px">
    <div style="font-size:40px;margin-bottom:12px">📂</div>
    No data yet. Import a bank statement or add a transaction.
  </div>
  <div id="dashContent" style="display:none">
    <div id="statsGrid" class="stats-grid"></div>
    <div class="charts-grid">
      <div class="card"><div class="label">Daily Spend (Last 30 Days)</div><canvas id="areaChart"></canvas></div>
      <div class="card"><div class="label">Categories</div><canvas id="donutChart"></canvas></div>
    </div>
    <div class="charts-grid2">
      <div class="card" id="catBreakdown"></div>
      <div class="card" id="merchantList"></div>
    </div>
  </div>
</div>

<!-- ── IMPORT ── -->
<div class="content" id="page-import" style="display:none">
  <div class="method-grid">
    <button class="method-btn active" onclick="setMethod('csv')" id="method-csv">
      <div class="method-title">📄 CSV / XLSX</div>
      <div class="method-desc">Bank statement file</div>
    </button>
    <button class="method-btn" onclick="setMethod('sms')" id="method-sms">
      <div class="method-title">💬 SMS</div>
      <div class="method-desc">Paste bank alerts</div>
    </button>
    <button class="method-btn" onclick="setMethod('manual')" id="method-manual">
      <div class="method-title">✏️ Manual</div>
      <div class="method-desc">Add one entry</div>
    </button>
  </div>

  <!-- CSV panel -->
  <div class="card" id="panel-csv">
    <div class="label">Upload Bank Statement</div>
    <div class="dropzone" id="dropzone"
      onclick="document.getElementById('csvFile').click()"
      ondragover="event.preventDefault();this.classList.add('drag')"
      ondragleave="this.classList.remove('drag')"
      ondrop="handleDrop(event)">
      <div class="dropzone-icon">📂</div>
      <div style="color:var(--text);margin-bottom:6px;font-size:14px">Drop your CSV or XLSX here</div>
      <div style="color:var(--sub);font-size:12px">or tap to browse · SBI, HDFC, ICICI, Axis</div>
      <div id="uploadStatus" style="margin-top:12px;color:var(--accent);font-size:13px"></div>
      <input type="file" id="csvFile" accept=".csv,.txt,.xlsx" style="display:none" onchange="uploadCSV(this.files[0])">
    </div>
    <div class="hint">
      <div class="label">How to get SBI CSV</div>
      <div style="font-size:12px;color:var(--sub);line-height:2">
        1. Login → <strong style="color:var(--text)">onlinesbi.sbi</strong><br>
        2. My Accounts → <strong style="color:var(--text)">Account Statement</strong><br>
        3. Set date range → <strong style="color:var(--text)">Download as CSV</strong>
      </div>
    </div>
  </div>

  <!-- SMS panel -->
  <div class="card" id="panel-sms" style="display:none">
    <div class="label">Paste Bank SMS Messages</div>
    <p style="color:var(--sub);font-size:12px;margin-bottom:12px">
      Paste one or multiple SMS alerts. Separate multiple with <code>---</code>
    </p>
    <textarea id="smsText" class="input" rows="6" style="resize:vertical"
      placeholder="Your SBI A/C XXXXX1234 debited INR 450.00 on 07-03-2026. Info: Swiggy. Avl Bal: INR 12,350.00&#10;---&#10;Rs.2000.00 withdrawn from A/C XX1234 at ATM. Bal:Rs.10,350.00"></textarea>
    <div style="margin-top:12px">
      <button class="btn btn-primary" onclick="uploadSMS()">Parse SMS →</button>
    </div>
  </div>

  <!-- Manual panel -->
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
    <div style="margin-bottom:12px">
      <div class="label">Description</div>
      <input type="text" id="m-desc" class="input" placeholder="e.g. Swiggy Order, Petrol bunk">
    </div>
    <div style="margin-bottom:16px">
      <div class="label">Amount (₹)</div>
      <input type="number" id="m-amt" class="input" placeholder="0.00" inputmode="decimal">
    </div>
    <button class="btn btn-primary" onclick="addManual()">➕ Add Transaction</button>
  </div>
</div>

<!-- ── TRANSACTIONS ── -->
<div class="content" id="page-transactions" style="display:none">
  <div style="display:flex;gap:10px;flex-wrap:wrap;margin-bottom:14px">
    <input type="text" class="input" id="txSearch" placeholder="🔍 Search…"
      style="flex:2;min-width:160px" oninput="renderTransactions()">
    <select class="input" id="txCatFilter" style="min-width:150px" onchange="renderTransactions()">
      <option value="">All Categories</option>
    </select>
    <select class="input" id="txTypeFilter" style="min-width:120px" onchange="renderTransactions()">
      <option value="">All Types</option>
      <option value="debit">Debits</option>
      <option value="credit">Credits</option>
    </select>
  </div>
  <div class="table-wrap">
    <table>
      <thead><tr>
        <th>DATE</th><th>DESCRIPTION</th><th>CATEGORY</th><th>SRC</th>
        <th style="text-align:right">AMOUNT</th><th></th>
      </tr></thead>
      <tbody id="txBody"></tbody>
    </table>
    <div class="table-footer" id="txFooter">0 transactions</div>
  </div>
</div>

<!-- ── REPORTS ── -->
<div class="content" id="page-email" style="display:none">
  <div style="max-width:480px;margin:auto">
    <div class="card">
      <div class="label">Generate Report</div>

      <div class="form-grid">
  <div>
    <div class="label">From Date</div>
    <input type="date" id="reportFrom" class="input">
  </div>
  <div>
    <div class="label">To Date</div>
    <input type="date" id="reportTo" class="input">
  </div>
</div>
      <p style="color:var(--sub);font-size:13px;margin-bottom:18px">
        Download or email your full spending report with analytics, categories and transactions.
      </p>
      <div style="display:flex;flex-direction:column;gap:12px">
        <button class="btn btn-primary" onclick="downloadPDF()">📄 Download PDF Report</button>
        <button class="btn btn-primary" onclick="sendReport()">📧 Email Report to Me</button>
        <button class="btn btn-ghost" onclick="previewReport()">👁 Preview Report</button>
      </div>
    </div>
    <div class="card">
      <div class="label">Automatic Weekly Reports</div>
      <p style="color:var(--sub);font-size:13px">
        A weekly summary is automatically emailed every Sunday morning if email settings are configured.
      </p>
    </div>
    <div class="card">
      <div class="label">Email Settings</div>
      <div style="display:flex;flex-direction:column;gap:10px">
        <input type="email" id="set-email" class="input" placeholder="Recipient email">
        <input type="email" id="set-sender" class="input" placeholder="Your Gmail address">
        <input type="password" id="set-pass" class="input" placeholder="Gmail App Password">
        <button class="btn btn-ghost" onclick="saveSettings()">💾 Save Settings</button>
      </div>
    </div>
  </div>
</div>

<!-- Bottom Nav -->
<div class="bottom-nav">
  <button id="nav-dashboard" onclick="showTab('dashboard')" class="active">
    🏠<span>Home</span>
  </button>
  <button id="nav-import" onclick="showTab('import')">
    ➕<span>Import</span>
  </button>
  <button id="nav-transactions" onclick="showTab('transactions')">
    📋<span>History</span>
  </button>
  <button id="nav-email" onclick="showTab('email')">
    📧<span>Reports</span>
  </button>
</div>

<script>
const API = "/api";
let allTransactions = [];
let analytics = {};
let categories = [];
let areaChartInst = null;
let donutChartInst = null;

let lastActive = Date.now();

function updateActivity(){
  lastActive = Date.now();
}

document.addEventListener("click", updateActivity);
document.addEventListener("touchstart", updateActivity);
document.addEventListener("keydown", updateActivity);

// ── Toast ──────────────────────────────────────────────────────────────────
function toast(msg, type="info") {
  const el = document.createElement("div");
  el.className = `toast ${type}`;
  el.textContent = (type==="success"?"✅ ":type==="error"?"❌ ":"ℹ️ ") + msg;
  document.getElementById("toasts").appendChild(el);
  setTimeout(()=>el.remove(), 3500);
}

// ── Auth ───────────────────────────────────────────────────────────────────
async function verifyFamily() {
  const code = document.getElementById("familyCode").value;
  const r = await fetch("/api/verify-family",{method:"POST",credentials:"include",
    headers:{"Content-Type":"application/json"},body:JSON.stringify({code})});
  if(r.ok){
    document.getElementById("familyGate").style.display="none";
    document.getElementById("loginScreen").style.display="flex";
  } else { toast("Wrong family code","error"); }
}

async function login() {
  const username = document.getElementById("loginUser").value;
  const password = document.getElementById("loginPassword").value;
  const r = await fetch("/api/login",{method:"POST",credentials:"include",
    headers:{"Content-Type":"application/json"},body:JSON.stringify({username,password})});
  if(r.ok){
    document.getElementById("loginScreen").style.display="none";
    fetchAll();
  } else { toast("Wrong username or password","error"); }
}

function openSignup() {
  document.getElementById("loginScreen").style.display="none";
  document.getElementById("signupScreen").style.display="flex";
}
function backToLogin() {
  document.getElementById("signupScreen").style.display="none";
  document.getElementById("loginScreen").style.display="flex";
}

async function createAccount() {
  const username = document.getElementById("signupUser").value;
  const password = document.getElementById("signupPass").value;
  if(!username||!password){toast("Enter username and password","error");return;}
  const r = await fetch("/api/create-user",{method:"POST",credentials:"include",
    headers:{"Content-Type":"application/json"},body:JSON.stringify({username,password})});
  const d = await r.json();
  if(d.ok){toast("Account created! Now login.","success");backToLogin();}
  else toast(d.error||"Failed","error");
}

async function forgotPassword() {
  const username = prompt("Enter your username");
  if(!username) return;
  const newpass = prompt("Enter new password");
  if(!newpass) return;
  const code = prompt("Enter family code");
  const r = await fetch("/api/reset-password",{method:"POST",credentials:"include",
    headers:{"Content-Type":"application/json"},
    body:JSON.stringify({username,password:newpass,family_code:code})});
  const d = await r.json();
  if(d.ok) toast("Password reset. Please login.","success");
  else toast(d.error||"Failed","error");
}

// ── Health ─────────────────────────────────────────────────────────────────
async function checkHealth() {
  try {
    await fetch(`${API}/health`);
    document.getElementById("statusDot").style.background="var(--green)";
    document.getElementById("statusText").textContent="online";
    document.getElementById("offlineBanner").style.display="none";
    return true;
  } catch {
    document.getElementById("statusDot").style.background="var(--red)";
    document.getElementById("statusText").textContent="offline";
    document.getElementById("offlineBanner").style.display="block";
    return false;
  }
}

// ── Fetch all ──────────────────────────────────────────────────────────────
async function fetchAll() {
  try {
    const [txR, anR, catR] = await Promise.all([
      fetch(`${API}/transactions`,{credentials:"include"}),
      fetch(`${API}/analytics`,{credentials:"include"}),
      fetch(`${API}/categories`),
    ]);
    allTransactions = await txR.json();
    analytics       = await anR.json();
    categories      = await catR.json();
    renderDashboard();
    renderTransactions();
    populateCatFilter();
    document.getElementById("clearBtn").style.display =
      allTransactions.length > 0 ? "inline-flex" : "none";
  } catch(e) { console.error(e); }
}

// ── Tabs ───────────────────────────────────────────────────────────────────
function showTab(name) {
  ["dashboard","import","transactions","email"].forEach(t => {
    document.getElementById(`page-${t}`).style.display = t===name?"block":"none";
    const tabBtn = document.getElementById(`tab-${t}`);
    if(tabBtn) tabBtn.classList.toggle("active", t===name);
    const navBtn = document.getElementById(`nav-${t}`);
    if(navBtn) navBtn.classList.toggle("active", t===name);
  });
  if(name==="dashboard") renderDashboard();
  // Load settings on reports tab
  if(name==="email") loadSettings();
}

// ── Import methods ─────────────────────────────────────────────────────────
function setMethod(m) {
  ["csv","sms","manual"].forEach(x => {
    document.getElementById(`method-${x}`).classList.toggle("active", x===m);
    document.getElementById(`panel-${x}`).style.display = x===m?"block":"none";
  });
}

function handleDrop(e) {
  e.preventDefault();
  document.getElementById("dropzone").classList.remove("drag");
  const f = e.dataTransfer.files[0];
  if(f) uploadCSV(f);
}

async function uploadCSV(file) {
  if(!file) return;
  document.getElementById("uploadStatus").textContent="Parsing…";
  const fd = new FormData();
  fd.append("file", file);
  try {
    const r = await fetch(`${API}/upload/file`,{method:"POST",credentials:"include",body:fd});
    const d = await r.json();
    if(d.error){toast(d.error,"error");document.getElementById("uploadStatus").textContent="";}
    else{
      toast(`Added ${d.added} transactions!`,"success");
      document.getElementById("uploadStatus").textContent=`✅ ${d.added} transactions loaded`;
      fetchAll();
    }
  } catch{ toast("Upload failed","error"); }
}

async function uploadSMS() {
  const text = document.getElementById("smsText").value.trim();
  if(!text) return;
  const messages = text.split("---").map(s=>s.trim()).filter(Boolean);
  try {
    const r = await fetch(`${API}/upload/sms`,{method:"POST",credentials:"include",
      headers:{"Content-Type":"application/json"},body:JSON.stringify({messages})});
    const d = await r.json();
    toast(`Parsed ${d.added} SMS transactions!`,"success");
    document.getElementById("smsText").value="";
    fetchAll();
  } catch{ toast("SMS upload failed","error"); }
}

async function addManual() {
  const d = document.getElementById("m-date").value;
  const desc = document.getElementById("m-desc").value;
  const amt = parseFloat(document.getElementById("m-amt").value);
  const type = document.getElementById("m-type").value;
  if(!d||!desc||isNaN(amt)){toast("Fill all fields","error");return;}
  try {
    const r = await fetch(`${API}/upload/manual`,{method:"POST",credentials:"include",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({date:d,description:desc,amount:amt,type})});
    const res = await r.json();
    if(res.error) toast(res.error,"error");
    else{
      toast("Transaction added!","success");
      document.getElementById("m-date").value="";
      document.getElementById("m-desc").value="";
      document.getElementById("m-amt").value="";
      fetchAll();
    }
  } catch{ toast("Failed","error"); }
}

// ── Dashboard ──────────────────────────────────────────────────────────────
function fmt(n){ return "₹"+Math.abs(n).toLocaleString("en-IN",{maximumFractionDigits:0}); }

function renderDashboard() {
  if(!analytics||analytics.count===0){
    document.getElementById("dashEmpty").style.display="block";
    document.getElementById("dashContent").style.display="none";
    return;
  }
  document.getElementById("dashEmpty").style.display="none";
  document.getElementById("dashContent").style.display="block";

  // Stats
  const statsData = [
    {label:"Total Spent",    value:fmt(analytics.total_debit),  sub:`${analytics.debit_count} txns`, color:"var(--red)"},
    {label:"Total Received", value:fmt(analytics.total_credit), sub:`${analytics.credit_count} txns`,color:"var(--green)"},
    {label:"Avg Daily",      value:fmt(analytics.avg_daily),    sub:"per day",color:"var(--text)"},
    {label:"Net Flow",       value:fmt(analytics.net), sub:analytics.net>=0?"Surplus":"Deficit",
     color:analytics.net>=0?"var(--green)":"var(--red)"},
  ];
  document.getElementById("statsGrid").innerHTML = statsData.map(s=>`
    <div class="stat-card">
      <div class="label">${s.label}</div>
      <div class="stat-value" style="color:${s.color}">${s.value}</div>
      <div class="stat-sub">${s.sub}</div>
    </div>`).join("");

  // Area chart
  const dailyEntries = Object.entries(analytics.daily).slice(-30);
  const areaCtx = document.getElementById("areaChart").getContext("2d");
  if(areaChartInst) areaChartInst.destroy();
  areaChartInst = new Chart(areaCtx,{
    type:"line",
    data:{
      labels:dailyEntries.map(([d])=>d.slice(5)),
      datasets:[{data:dailyEntries.map(([,v])=>v),borderColor:"#6ee7b7",
        backgroundColor:"rgba(110,231,183,.12)",fill:true,tension:.4,pointRadius:2,borderWidth:2}]
    },
    options:{responsive:true,plugins:{legend:{display:false}},
      scales:{
        x:{ticks:{color:"#6b7280",font:{size:9}},grid:{color:"#1f2937"}},
        y:{ticks:{color:"#6b7280",font:{size:9},callback:v=>`₹${(v/1000).toFixed(0)}k`},grid:{color:"#1f2937"}},
      }}
  });

  // Donut chart
  const cats = Object.entries(analytics.categories);
  const donutCtx = document.getElementById("donutChart").getContext("2d");
  if(donutChartInst) donutChartInst.destroy();
  donutChartInst = new Chart(donutCtx,{
    type:"doughnut",
    data:{
      labels:cats.map(([k])=>k),
      datasets:[{data:cats.map(([,v])=>v.total),backgroundColor:cats.map(([,v])=>v.color),
        borderWidth:2,borderColor:"#111827"}]
    },
    options:{responsive:true,cutout:"62%",
      plugins:{legend:{position:"right",labels:{color:"#e5e7eb",font:{size:10},boxWidth:10}}}}
  });

  // Category breakdown
  const total = analytics.total_debit;
  document.getElementById("catBreakdown").innerHTML = `<div class="label">Category Breakdown</div>` +
    cats.map(([cat,data])=>{
      const pct = total?(data.total/total*100):0;
      return `<div style="margin-bottom:13px">
        <div style="display:flex;justify-content:space-between;margin-bottom:4px">
          <span style="font-size:12px">${data.icon} ${cat} <span style="color:var(--sub);font-size:10px">(${data.count})</span></span>
          <span style="font-size:12px;font-weight:700;font-family:monospace">${fmt(data.total)}</span>
        </div>
        <div class="cat-bar-bg"><div class="cat-bar-fill" style="width:${pct}%;background:${data.color}"></div></div>
      </div>`;
    }).join("");

  // Top merchants
  document.getElementById("merchantList").innerHTML = `<div class="label">Top Merchants</div>` +
    analytics.top_merchants.map(m=>`
      <div class="merchant-row">
        <span style="color:var(--text);font-size:12px">${m.name.substring(0,28)}</span>
        <span style="color:var(--red);font-weight:700;font-family:monospace;font-size:12px">${fmt(m.amount)}</span>
      </div>`).join("");
}

// ── Transactions ───────────────────────────────────────────────────────────
const CAT_COLORS_MAP = {
  "Food & Dining":"#f97316","Transport":"#3b82f6","Shopping":"#a855f7","Utilities":"#10b981",
  "Health":"#ef4444","Entertainment":"#f59e0b","Education":"#06b6d4","Investments":"#22c55e",
  "Transfers":"#6b7280","ATM/Cash":"#84cc16","Rent & Housing":"#f43f5e","Others":"#8b5cf6"
};

function populateCatFilter() {
  const sel = document.getElementById("txCatFilter");
  const cur = sel.value;
  sel.innerHTML = '<option value="">All Categories</option>' +
    categories.map(c=>`<option value="${c.name}">${c.icon} ${c.name}</option>`).join("");
  sel.value = cur;
}

function renderTransactions() {
  const search = document.getElementById("txSearch").value.toLowerCase();
  const cat    = document.getElementById("txCatFilter").value;
  const type   = document.getElementById("txTypeFilter").value;
  let filtered = allTransactions
    .filter(t=>!cat ||t.category===cat)
    .filter(t=>!type||t.type===type)
    .filter(t=>!search||t.description.toLowerCase().includes(search)||t.category.toLowerCase().includes(search));
  const tbody = document.getElementById("txBody");
  if(!filtered.length){
    tbody.innerHTML=`<tr><td colspan="6" class="empty-state">No transactions found</td></tr>`;
  } else {
    tbody.innerHTML = filtered.map(t=>{
      const color = t.amount>0?"#f87171":"#34d399";
      const sign  = t.amount>0?"−":"+";
      const cc    = CAT_COLORS_MAP[t.category]||"#888";
      return `<tr>
        <td style="color:var(--sub);font-family:monospace;white-space:nowrap;font-size:12px">${t.date}</td>
        <td style="max-width:200px"><div style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:12px">${t.description.substring(0,20)}</div></td>
        <td><span class="pill" style="background:${cc}22;color:${cc}">${t.category}</span></td>
        <td><span style="font-size:10px;color:var(--sub);background:var(--surface);padding:2px 7px;border-radius:4px">${t.source}</span></td>
        <td style="text-align:right;font-family:monospace;font-weight:700;color:${color};white-space:nowrap;font-size:12px">${sign}${fmt(t.amount)}</td>
        <td><button onclick="deleteTx('${t.id}')" style="background:none;border:none;color:var(--muted);cursor:pointer;font-size:15px;padding:2px 4px;border-radius:4px">🗑</button></td>
      </tr>`;
    }).join("");
  }
  document.getElementById("txFooter").textContent=`Showing ${filtered.length} of ${allTransactions.length} transactions`;
}

async function deleteTx(id) {
  if(!confirm("Delete this transaction?")) return;
  await fetch(`${API}/transactions/${id}`,{method:"DELETE",credentials:"include"});
  toast("Deleted","success");
  fetchAll();
}

async function clearAll() {
  if(!confirm("Clear ALL transactions? This cannot be undone.")) return;
  await fetch(`${API}/clear`,{method:"POST",credentials:"include"});
  toast("All data cleared","info");
  fetchAll();
}

// ── Reports ────────────────────────────────────────────────────────────────
async function sendReport() {
  const r = await fetch("/api/send-report",{method:"POST",credentials:"include"});
  const d = await r.json();
  if(d.ok) toast("Report sent to "+d.sent_to,"success");
  else toast(d.error||"Failed","error");
}

async function downloadPDF() {

  const from = document.getElementById("reportFrom").value;
  const to = document.getElementById("reportTo").value;

  const r = await fetch(`/api/download-report-pdf?from=${from}&to=${to}`,{
    credentials:"include"
  });

  const blob = await r.blob();

  const url = window.URL.createObjectURL(blob);

  const a = document.createElement("a");

  a.href = url;
  a.download = "spendlens_report.pdf";

  a.click();
}

async function previewReport() {
  const r = await fetch("/api/preview-report",{credentials:"include"});
  const html = await r.text();
  const w = window.open();
  w.document.write(html);
  w.document.close();
}

async function loadSettings() {
  try {
    const r = await fetch("/api/settings",{credentials:"include"});
    const d = await r.json();
    if(d.email) document.getElementById("set-email").value=d.email;
    if(d.sender_email) document.getElementById("set-sender").value=d.sender_email;
  } catch{}
}

async function saveSettings() {
  const email = document.getElementById("set-email").value;
  const sender_email = document.getElementById("set-sender").value;
  const gmail_app_password = document.getElementById("set-pass").value;
  const r = await fetch("/api/settings",{method:"POST",credentials:"include",
    headers:{"Content-Type":"application/json"},
    body:JSON.stringify({email,sender_email,gmail_app_password})});
  const d = await r.json();
  if(d.ok) toast("Settings saved!","success");
  else toast("Failed to save","error");
}

// ── Init ───────────────────────────────────────────────────────────────────
function enterApp() {
  document.getElementById("splashScreen").style.display="none";
  document.getElementById("familyGate").style.display="flex";
}

async function init() {
  await checkHealth();
  const r = await fetch("/api/me",{credentials:"include"});
  const me = await r.json();
  if(me.family_ok && me.logged_in){
    document.getElementById("loginScreen").style.display="none";
    document.getElementById("familyGate").style.display="none";
    fetchAll();
  } else if(me.family_ok){
    document.getElementById("familyGate").style.display="none";
    document.getElementById("loginScreen").style.display="flex";
  }
  setInterval(checkHealth, 15000);
}

setInterval(async ()=>{
  const inactive = Date.now() - lastActive;

  if(inactive > 10 * 60 * 1000){ // 10 minutes
    const r = await fetch("/api/logout",{method:"POST",credentials:"include"});
    document.getElementById("loginScreen").style.display="flex";
    toast("Session locked. Please login again.","info");
  }
},60000);


window.onload = init;
</script>
</body>
</html>"""

# ── Static routes ─────────────────────────────────────────────────────────────
@app.route("/manifest.json")
def manifest():
    return jsonify({
        "name": "SpendLens",
        "short_name": "SpendLens",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#070b12",
        "theme_color": "#070b12",
        "icons": [{"src": "https://cdn-icons-png.flaticon.com/512/3135/3135706.png", "sizes": "512x512", "type": "image/png"}]
    })

@app.route("/service-worker.js")
def service_worker():
    return """self.addEventListener('install',e=>self.skipWaiting());
self.addEventListener('fetch',event=>event.respondWith(fetch(event.request)));""", 200, {"Content-Type": "application/javascript"}

@app.route("/")
def serve_frontend():
    return FRONTEND_HTML, 200, {"Content-Type": "text/html; charset=utf-8"}

# ── Weekly report job ─────────────────────────────────────────────────────────
import threading, time

def weekly_report_job():
    while True:
        now = datetime.now()
        if now.weekday() == 6 and now.hour == 8 and now.minute == 0:
            try:
                conn = db()
                cur = cursor(conn)
                cur.execute("SELECT * FROM users")
                users = cur.fetchall()
                cur.execute("SELECT email,sender_email,gmail_app_password FROM settings WHERE id=1")
                s = cur.fetchone()
                if s:
                    for user in users:
                        cur2 = cursor(conn)
                        cur2.execute("SELECT * FROM transactions WHERE user_id=%s", (user["id"],))
                        txns = [dict(r) for r in cur2.fetchall()]
                        cur2.close()
                        if not txns: continue
                        analytics_data = compute_analytics(txns)
                        html = build_email_html(analytics_data, sorted(txns, key=lambda x: x["date"], reverse=True), "Weekly Report")
                        msg = MIMEMultipart("alternative")
                        msg["Subject"] = "💳 Weekly SpendLens Report"
                        msg["From"]    = s["sender_email"]
                        msg["To"]      = s["email"]
                        msg.attach(MIMEText(html, "html"))
                        try:
                            with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
                                smtp.login(s["sender_email"], s["gmail_app_password"])
                                smtp.sendmail(s["sender_email"], s["email"], msg.as_string())
                        except Exception as e:
                            print(f"Email error for user {user['username']}: {e}")
                cur.close()
                conn.close()
            except Exception as e:
                print("Weekly report error:", e)
            time.sleep(60)
        time.sleep(20)

# ── Startup ───────────────────────────────────────────────────────────────────
init_db()

if __name__ == "__main__":
    print("\n🚀 SpendLens running at http://localhost:5000\n")
    threading.Thread(target=weekly_report_job, daemon=True).start()
    app.run(host="0.0.0.0", port=5000, debug=False)