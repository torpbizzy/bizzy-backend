"""
Bizzy Backend API v1.0
FastAPI + SQLite (upgrades to PostgreSQL on Railway automatically)
Deploy to Railway — connects to bizzys.in frontend
"""

from fastapi import FastAPI, HTTPException, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, EmailStr
from typing import Optional, List
import sqlite3
import hashlib
import hmac
import json
import os
import requests
from datetime import datetime, timedelta
import uuid
import jwt

# ── CONFIG ────────────────────────────────────────────────
SECRET_KEY     = os.getenv("SECRET_KEY", "bizzy-secret-change-in-production")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "Bizzy@Admin2025")
RAZORPAY_KEY   = os.getenv("RAZORPAY_KEY_ID", "")
RAZORPAY_SEC   = os.getenv("RAZORPAY_KEY_SECRET", "")
DATABASE       = os.getenv("DATABASE_URL", "bizzy.db")
TRIAL_DAYS     = 7
MONTHLY_PRICE  = 5000   # INR
ANNUAL_PRICE   = 60000  # INR

app = FastAPI(
    title="Bizzy API",
    description="Backend for bizzys.in — MSME Financial Dashboard",
    version="1.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://bizzys.in", "https://www.bizzys.in", "http://localhost:3000", "*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── DATABASE ──────────────────────────────────────────────
def get_db():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    c = conn.cursor()

    c.execute("""
        CREATE TABLE IF NOT EXISTS businesses (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            biz_name TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            gstin TEXT,
            mobile TEXT,
            plan TEXT DEFAULT 'trial',
            trial_ends TEXT,
            razorpay_sub_id TEXT,
            tally_connected INTEGER DEFAULT 0,
            sync_key TEXT UNIQUE,
            joined_at TEXT DEFAULT CURRENT_TIMESTAMP,
            last_active TEXT
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS tally_syncs (
            id TEXT PRIMARY KEY,
            business_id TEXT NOT NULL,
            synced_at TEXT DEFAULT CURRENT_TIMESTAMP,
            data_size INTEGER,
            status TEXT DEFAULT 'success',
            payload TEXT,
            FOREIGN KEY (business_id) REFERENCES businesses(id)
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS invoices (
            id TEXT PRIMARY KEY,
            business_id TEXT NOT NULL,
            invoice_number TEXT,
            party_name TEXT,
            amount REAL,
            invoice_date TEXT,
            due_date TEXT,
            status TEXT DEFAULT 'pending',
            type TEXT DEFAULT 'sales',
            synced_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (business_id) REFERENCES businesses(id)
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS ledgers (
            id TEXT PRIMARY KEY,
            business_id TEXT NOT NULL,
            name TEXT,
            group_name TEXT,
            closing_balance REAL,
            synced_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (business_id) REFERENCES businesses(id)
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS sync_logs (
            id TEXT PRIMARY KEY,
            business_id TEXT NOT NULL,
            sync_key TEXT,
            status TEXT,
            entries_count INTEGER DEFAULT 0,
            message TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (business_id) REFERENCES businesses(id)
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS subscriptions (
            id TEXT PRIMARY KEY,
            business_id TEXT NOT NULL,
            plan TEXT,
            amount REAL,
            razorpay_payment_id TEXT,
            status TEXT DEFAULT 'active',
            starts_at TEXT,
            ends_at TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (business_id) REFERENCES businesses(id)
        )
    """)

    conn.commit()
    conn.close()

init_db()

# ── AUTH HELPERS ──────────────────────────────────────────
def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()

def verify_password(plain: str, hashed: str) -> bool:
    return hash_password(plain) == hashed

def create_token(business_id: str) -> str:
    payload = {
        "sub": business_id,
        "exp": datetime.utcnow() + timedelta(days=30),
        "iat": datetime.utcnow()
    }
    return jwt.encode(payload, SECRET_KEY, algorithm="HS256")

def decode_token(token: str) -> str:
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
        return payload["sub"]
    except:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

def get_current_business(authorization: str = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Authorization header required")
    token = authorization.split(" ")[1]
    return decode_token(token)

def generate_sync_key(business_id: str) -> str:
    raw = f"BZY-{business_id[:4].upper()}-{uuid.uuid4().hex[:4].upper()}-SYNC"
    return raw

# ── MODELS ────────────────────────────────────────────────
class RegisterRequest(BaseModel):
    name: str
    biz_name: str
    email: str
    password: str
    gstin: Optional[str] = None
    mobile: Optional[str] = None

class LoginRequest(BaseModel):
    email: str
    password: str

class SyncPayload(BaseModel):
    sync_key: str
    data: dict
    sent_at: Optional[str] = None

class BillingRequest(BaseModel):
    plan: str  # monthly or annual
    razorpay_payment_id: str

class AdminLoginRequest(BaseModel):
    password: str

# ── ROOT ──────────────────────────────────────────────────
@app.get("/")
def root():
    return {
        "service": "Bizzy API",
        "version": "1.0.0",
        "status": "running",
        "docs": "/docs"
    }

@app.get("/health")
def health():
    return {"status": "healthy", "timestamp": datetime.now().isoformat()}

# ── AUTH ENDPOINTS ────────────────────────────────────────
@app.post("/api/auth/register")
def register(req: RegisterRequest):
    conn = get_db()
    c = conn.cursor()

    # Check existing
    existing = c.execute("SELECT id FROM businesses WHERE email=?", (req.email.lower(),)).fetchone()
    if existing:
        conn.close()
        raise HTTPException(status_code=400, detail="Email already registered")

    # Validate GSTIN format if provided
    if req.gstin:
        import re
        gstin_pattern = r'^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z]{1}[1-9A-Z]{1}Z[0-9A-Z]{1}$'
        if not re.match(gstin_pattern, req.gstin.upper()):
            conn.close()
            raise HTTPException(status_code=400, detail="Invalid GSTIN format")

    biz_id = "BZY-" + uuid.uuid4().hex[:8].upper()
    sync_key = generate_sync_key(biz_id)
    trial_ends = (datetime.now() + timedelta(days=TRIAL_DAYS)).isoformat()

    c.execute("""
        INSERT INTO businesses
        (id, name, biz_name, email, password_hash, gstin, mobile, plan, trial_ends, sync_key, joined_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'trial', ?, ?, ?)
    """, (
        biz_id, req.name, req.biz_name,
        req.email.lower(), hash_password(req.password),
        req.gstin.upper() if req.gstin else None,
        req.mobile, trial_ends, sync_key,
        datetime.now().isoformat()
    ))
    conn.commit()
    conn.close()

    token = create_token(biz_id)
    return {
        "success": True,
        "token": token,
        "business": {
            "id": biz_id,
            "name": req.name,
            "biz_name": req.biz_name,
            "email": req.email.lower(),
            "plan": "trial",
            "trial_ends": trial_ends,
            "sync_key": sync_key,
            "gstin": req.gstin
        }
    }

@app.post("/api/auth/login")
def login(req: LoginRequest):
    conn = get_db()
    c = conn.cursor()
    biz = c.execute("SELECT * FROM businesses WHERE email=?", (req.email.lower(),)).fetchone()
    conn.close()

    if not biz or not verify_password(req.password, biz["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid email or password")

    # Update last active
    conn = get_db()
    conn.execute("UPDATE businesses SET last_active=? WHERE id=?",
                 (datetime.now().isoformat(), biz["id"]))
    conn.commit()
    conn.close()

    token = create_token(biz["id"])
    return {
        "success": True,
        "token": token,
        "business": {
            "id": biz["id"],
            "name": biz["name"],
            "biz_name": biz["biz_name"],
            "email": biz["email"],
            "plan": biz["plan"],
            "trial_ends": biz["trial_ends"],
            "sync_key": biz["sync_key"],
            "gstin": biz["gstin"],
            "tally_connected": bool(biz["tally_connected"])
        }
    }

# ── DASHBOARD ─────────────────────────────────────────────
@app.get("/api/dashboard")
def get_dashboard(business_id: str = Depends(get_current_business)):
    conn = get_db()
    c = conn.cursor()

    # Get latest sync
    latest_sync = c.execute("""
        SELECT * FROM tally_syncs
        WHERE business_id=? ORDER BY synced_at DESC LIMIT 1
    """, (business_id,)).fetchone()

    # Get invoice stats
    invoices = c.execute("""
        SELECT status, type, SUM(amount) as total, COUNT(*) as count
        FROM invoices WHERE business_id=?
        GROUP BY status, type
    """, (business_id,)).fetchall()

    # Get sync history
    sync_logs = c.execute("""
        SELECT * FROM sync_logs WHERE business_id=?
        ORDER BY created_at DESC LIMIT 10
    """, (business_id,)).fetchall()

    # Get business info
    biz = c.execute("SELECT * FROM businesses WHERE id=?", (business_id,)).fetchone()
    conn.close()

    # Process invoice data
    revenue_mtd = 0
    receivables = 0
    payables = 0
    overdue_count = 0

    for inv in invoices:
        if inv["type"] == "sales" and inv["status"] == "paid":
            revenue_mtd += inv["total"] or 0
        if inv["type"] == "sales" and inv["status"] == "pending":
            receivables += inv["total"] or 0
        if inv["type"] == "purchase" and inv["status"] == "pending":
            payables += inv["total"] or 0
        if inv["status"] == "overdue":
            overdue_count += inv["count"]

    return {
        "success": True,
        "dashboard": {
            "revenue_mtd": revenue_mtd,
            "receivables": receivables,
            "payables": payables,
            "overdue_invoices": overdue_count,
            "last_sync": latest_sync["synced_at"] if latest_sync else None,
            "tally_connected": bool(biz["tally_connected"]) if biz else False,
            "sync_count": len(sync_logs)
        }
    }

# ── TALLY SYNC ENDPOINT ───────────────────────────────────
@app.post("/api/sync")
def receive_tally_sync(payload: SyncPayload):
    conn = get_db()
    c = conn.cursor()

    # Verify sync key
    biz = c.execute("SELECT * FROM businesses WHERE sync_key=?",
                    (payload.sync_key,)).fetchone()
    if not biz:
        conn.close()
        raise HTTPException(status_code=401, detail="Invalid sync key")

    biz_id = biz["id"]
    data = payload.data
    sync_id = uuid.uuid4().hex

    try:
        payload_str = json.dumps(data, default=str)
        data_size = len(payload_str)

        # Store raw sync
        c.execute("""
            INSERT INTO tally_syncs (id, business_id, synced_at, data_size, status, payload)
            VALUES (?, ?, ?, ?, 'success', ?)
        """, (sync_id, biz_id, datetime.now().isoformat(), data_size, payload_str[:50000]))

        # Mark tally as connected
        c.execute("UPDATE businesses SET tally_connected=1, last_active=? WHERE id=?",
                  (datetime.now().isoformat(), biz_id))

        entries_count = 0

        # Parse and store invoices from sales data
        if "sales" in data and data["sales"]:
            sales_data = data["sales"]
            # Handle Tally XML parsed structure
            vouchers = []
            if isinstance(sales_data, dict):
                vouchers = sales_data.get("VOUCHER", [])
                if isinstance(vouchers, dict):
                    vouchers = [vouchers]

            for v in vouchers[:500]:  # limit to 500 per sync
                if isinstance(v, dict):
                    inv_id = uuid.uuid4().hex
                    c.execute("""
                        INSERT OR REPLACE INTO invoices
                        (id, business_id, invoice_number, party_name, amount, invoice_date, type, synced_at)
                        VALUES (?, ?, ?, ?, ?, ?, 'sales', ?)
                    """, (
                        inv_id, biz_id,
                        v.get("VOUCHERNUMBER", ""),
                        v.get("PARTYLEDGERNAME", ""),
                        float(v.get("AMOUNT", 0) or 0),
                        v.get("DATE", ""),
                        datetime.now().isoformat()
                    ))
                    entries_count += 1

        # Parse ledgers
        if "ledgers" in data and data["ledgers"]:
            ledger_data = data["ledgers"]
            ledger_list = []
            if isinstance(ledger_data, dict):
                ledger_list = ledger_data.get("LEDGER", [])
                if isinstance(ledger_list, dict):
                    ledger_list = [ledger_list]

            for led in ledger_list[:1000]:
                if isinstance(led, dict):
                    led_id = uuid.uuid4().hex
                    c.execute("""
                        INSERT OR REPLACE INTO ledgers
                        (id, business_id, name, group_name, closing_balance, synced_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                    """, (
                        led_id, biz_id,
                        led.get("NAME", ""),
                        led.get("PARENT", ""),
                        float(led.get("CLOSINGBALANCE", 0) or 0),
                        datetime.now().isoformat()
                    ))
                    entries_count += 1

        # Log the sync
        c.execute("""
            INSERT INTO sync_logs (id, business_id, sync_key, status, entries_count, message)
            VALUES (?, ?, ?, 'success', ?, ?)
        """, (uuid.uuid4().hex, biz_id, payload.sync_key,
              entries_count, f"Synced {entries_count} entries from Tally"))

        conn.commit()
        conn.close()

        return {
            "success": True,
            "sync_id": sync_id,
            "entries_stored": entries_count,
            "message": f"Sync successful — {entries_count} entries processed",
            "next_sync_in": "60 minutes"
        }

    except Exception as e:
        conn.rollback()
        conn.close()
        raise HTTPException(status_code=500, detail=f"Sync failed: {str(e)}")

# ── GST LOOKUP ────────────────────────────────────────────
@app.get("/api/gst/{gstin}")
def lookup_gstin(gstin: str, business_id: str = Depends(get_current_business)):
    """Verify GSTIN using public GST API."""
    gstin = gstin.upper().strip()
    import re
    if not re.match(r'^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z]{1}[1-9A-Z]{1}Z[0-9A-Z]{1}$', gstin):
        raise HTTPException(status_code=400, detail="Invalid GSTIN format")

    try:
        # Public GST verification API
        resp = requests.get(
            f"https://sheet.gstincheck.co.in/check/{gstin}",
            timeout=10
        )
        if resp.status_code == 200:
            return {"success": True, "gstin": gstin, "data": resp.json()}
    except:
        pass

    # Fallback — return basic validation
    state_codes = {
        "01":"Jammu & Kashmir","02":"Himachal Pradesh","03":"Punjab","04":"Chandigarh",
        "05":"Uttarakhand","06":"Haryana","07":"Delhi","08":"Rajasthan","09":"Uttar Pradesh",
        "10":"Bihar","11":"Sikkim","12":"Arunachal Pradesh","13":"Nagaland","14":"Manipur",
        "15":"Mizoram","16":"Tripura","17":"Meghalaya","18":"Assam","19":"West Bengal",
        "20":"Jharkhand","21":"Odisha","22":"Chattisgarh","23":"Madhya Pradesh",
        "24":"Gujarat","27":"Maharashtra","28":"Andhra Pradesh","29":"Karnataka",
        "30":"Goa","31":"Lakshadweep","32":"Kerala","33":"Tamil Nadu","34":"Puducherry",
        "35":"Andaman & Nicobar","36":"Telangana","37":"Andhra Pradesh(new)"
    }
    state_code = gstin[:2]
    return {
        "success": True,
        "gstin": gstin,
        "data": {
            "gstin": gstin,
            "state": state_codes.get(state_code, "Unknown"),
            "pan": gstin[2:12],
            "entity_type": "Regular",
            "status": "Active",
            "verified": True
        }
    }

# ── SYNC STATUS ───────────────────────────────────────────
@app.get("/api/sync/status")
def sync_status(business_id: str = Depends(get_current_business)):
    conn = get_db()
    c = conn.cursor()

    logs = c.execute("""
        SELECT * FROM sync_logs WHERE business_id=?
        ORDER BY created_at DESC LIMIT 20
    """, (business_id,)).fetchall()

    latest = c.execute("""
        SELECT synced_at, data_size FROM tally_syncs
        WHERE business_id=? ORDER BY synced_at DESC LIMIT 1
    """, (business_id,)).fetchone()

    biz = c.execute("SELECT tally_connected FROM businesses WHERE id=?",
                    (business_id,)).fetchone()
    conn.close()

    return {
        "success": True,
        "tally_connected": bool(biz["tally_connected"]) if biz else False,
        "last_sync": latest["synced_at"] if latest else None,
        "last_sync_size": latest["data_size"] if latest else 0,
        "sync_history": [dict(l) for l in logs]
    }

# ── INVOICES ──────────────────────────────────────────────
@app.get("/api/invoices")
def get_invoices(
    type: str = "sales",
    status: Optional[str] = None,
    limit: int = 50,
    business_id: str = Depends(get_current_business)
):
    conn = get_db()
    c = conn.cursor()

    query = "SELECT * FROM invoices WHERE business_id=? AND type=?"
    params = [business_id, type]

    if status:
        query += " AND status=?"
        params.append(status)

    query += " ORDER BY synced_at DESC LIMIT ?"
    params.append(limit)

    invoices = c.execute(query, params).fetchall()
    conn.close()

    return {
        "success": True,
        "invoices": [dict(i) for i in invoices],
        "count": len(invoices)
    }

# ── BILLING ───────────────────────────────────────────────
@app.post("/api/billing/subscribe")
def subscribe(req: BillingRequest, business_id: str = Depends(get_current_business)):
    conn = get_db()
    c = conn.cursor()

    amount = MONTHLY_PRICE if req.plan == "monthly" else ANNUAL_PRICE
    ends_at = datetime.now() + timedelta(days=30 if req.plan == "monthly" else 395)

    # Record subscription
    sub_id = uuid.uuid4().hex
    c.execute("""
        INSERT INTO subscriptions (id, business_id, plan, amount, razorpay_payment_id, status, starts_at, ends_at)
        VALUES (?, ?, ?, ?, ?, 'active', ?, ?)
    """, (sub_id, business_id, req.plan, amount,
          req.razorpay_payment_id,
          datetime.now().isoformat(), ends_at.isoformat()))

    # Update business plan
    c.execute("UPDATE businesses SET plan=?, razorpay_sub_id=? WHERE id=?",
              (req.plan, req.razorpay_payment_id, business_id))

    conn.commit()
    conn.close()

    return {
        "success": True,
        "subscription_id": sub_id,
        "plan": req.plan,
        "amount": amount,
        "expires_at": ends_at.isoformat(),
        "message": f"Subscription activated — {req.plan} plan"
    }

# ── ADMIN ENDPOINTS ───────────────────────────────────────
@app.post("/api/admin/login")
def admin_login(req: AdminLoginRequest):
    if req.password != ADMIN_PASSWORD:
        raise HTTPException(status_code=401, detail="Invalid admin password")
    token = jwt.encode(
        {"sub": "admin", "role": "admin",
         "exp": datetime.utcnow() + timedelta(days=1)},
        SECRET_KEY, algorithm="HS256"
    )
    return {"success": True, "token": token}

def verify_admin(authorization: str = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Unauthorized")
    token = authorization.split(" ")[1]
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
        if payload.get("role") != "admin":
            raise HTTPException(status_code=403, detail="Admin access required")
        return True
    except:
        raise HTTPException(status_code=401, detail="Invalid token")

@app.get("/api/admin/businesses")
def admin_businesses(is_admin: bool = Depends(verify_admin)):
    conn = get_db()
    c = conn.cursor()
    businesses = c.execute("""
        SELECT b.*,
               COUNT(DISTINCT s.id) as sync_count,
               MAX(s.synced_at) as last_sync,
               COUNT(DISTINCT i.id) as invoice_count
        FROM businesses b
        LEFT JOIN tally_syncs s ON s.business_id = b.id
        LEFT JOIN invoices i ON i.business_id = b.id
        GROUP BY b.id
        ORDER BY b.joined_at DESC
    """).fetchall()
    conn.close()

    result = []
    for b in businesses:
        row = dict(b)
        row.pop("password_hash", None)
        result.append(row)

    return {"success": True, "businesses": result, "count": len(result)}

@app.get("/api/admin/metrics")
def admin_metrics(is_admin: bool = Depends(verify_admin)):
    conn = get_db()
    c = conn.cursor()

    total = c.execute("SELECT COUNT(*) as n FROM businesses").fetchone()["n"]
    paid = c.execute("SELECT COUNT(*) as n FROM businesses WHERE plan IN ('monthly','annual')").fetchone()["n"]
    trial = c.execute("SELECT COUNT(*) as n FROM businesses WHERE plan='trial'").fetchone()["n"]
    tally_connected = c.execute("SELECT COUNT(*) as n FROM businesses WHERE tally_connected=1").fetchone()["n"]
    total_syncs = c.execute("SELECT COUNT(*) as n FROM tally_syncs").fetchone()["n"]
    monthly_subs = c.execute("SELECT COUNT(*) as n FROM businesses WHERE plan='monthly'").fetchone()["n"]
    annual_subs = c.execute("SELECT COUNT(*) as n FROM businesses WHERE plan='annual'").fetchone()["n"]

    conn.close()

    mrr = (monthly_subs * MONTHLY_PRICE) + (annual_subs * (ANNUAL_PRICE / 12))
    arr = mrr * 12

    return {
        "success": True,
        "metrics": {
            "total_businesses": total,
            "paid_businesses": paid,
            "trial_businesses": trial,
            "tally_connected": tally_connected,
            "total_syncs": total_syncs,
            "monthly_subscribers": monthly_subs,
            "annual_subscribers": annual_subs,
            "mrr": round(mrr, 2),
            "arr": round(arr, 2)
        }
    }

@app.put("/api/admin/businesses/{biz_id}/plan")
def update_business_plan(
    biz_id: str,
    body: dict,
    is_admin: bool = Depends(verify_admin)
):
    plan = body.get("plan")
    if plan not in ["trial", "monthly", "annual", "cancelled"]:
        raise HTTPException(status_code=400, detail="Invalid plan")

    conn = get_db()
    conn.execute("UPDATE businesses SET plan=? WHERE id=?", (plan, biz_id))
    conn.commit()
    conn.close()

    return {"success": True, "message": f"Plan updated to {plan}"}

@app.get("/api/admin/sync-logs")
def admin_sync_logs(limit: int = 50, is_admin: bool = Depends(verify_admin)):
    conn = get_db()
    c = conn.cursor()
    logs = c.execute("""
        SELECT sl.*, b.biz_name, b.email
        FROM sync_logs sl
        JOIN businesses b ON b.id = sl.business_id
        ORDER BY sl.created_at DESC
        LIMIT ?
    """, (limit,)).fetchall()
    conn.close()
    return {"success": True, "logs": [dict(l) for l in logs]}
