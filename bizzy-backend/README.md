# Bizzy Backend API
## FastAPI + SQLite → Railway

---

## Deploy to Railway in 5 minutes

### Step 1 — Push to GitHub
1. Create a new repo on github.com — name it `bizzy-backend`
2. Upload all these files to the repo

### Step 2 — Deploy on Railway
1. Go to railway.app → New Project
2. Click "Deploy from GitHub repo"
3. Select your `bizzy-backend` repo
4. Railway auto-detects Python + Procfile
5. Click Deploy — live in 2 minutes

### Step 3 — Add Environment Variables
In Railway → Your Project → Variables → Add:
```
SECRET_KEY         = bizzy-super-secret-key-change-this-2025
ADMIN_PASSWORD     = Bizzy@Admin2025
RAZORPAY_KEY_ID    = rzp_live_XXXXXXXXXXXXXXX
RAZORPAY_KEY_SECRET= your_razorpay_secret
```

### Step 4 — Get your API URL
Railway gives you a URL like:
`https://bizzy-backend-production.up.railway.app`

### Step 5 — Connect to bizzys.in frontend
In your bizzys.in index.html find:
`https://bizzys.in/api/sync`
Replace with your Railway URL:
`https://bizzy-backend-production.up.railway.app/api/sync`

Also update in connector.py:
`BIZZY_API = "https://bizzy-backend-production.up.railway.app/api/sync"`

---

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | /api/auth/register | Register new business |
| POST | /api/auth/login | Login |
| GET | /api/dashboard | Business dashboard data |
| POST | /api/sync | Receive Tally sync data |
| GET | /api/sync/status | Sync history |
| GET | /api/gst/{gstin} | Verify GSTIN |
| GET | /api/invoices | List invoices |
| POST | /api/billing/subscribe | Activate subscription |
| POST | /api/admin/login | Admin login |
| GET | /api/admin/businesses | All businesses |
| GET | /api/admin/metrics | MRR, ARR, counts |
| PUT | /api/admin/businesses/{id}/plan | Update plan |
| GET | /api/admin/sync-logs | All sync logs |

Full interactive docs at: `your-railway-url.up.railway.app/docs`

---

## Testing the API locally

```bash
pip install -r requirements.txt
uvicorn main:app --reload
```

Open http://localhost:8000/docs — full Swagger UI

### Test registration:
```bash
curl -X POST http://localhost:8000/api/auth/register \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Rajesh Sharma",
    "biz_name": "Sharma Industries",
    "email": "rajesh@test.com",
    "password": "test1234",
    "gstin": "27AAPFU0939F1ZV"
  }'
```

### Test Tally sync:
```bash
curl -X POST http://localhost:8000/api/sync \
  -H "Content-Type: application/json" \
  -d '{
    "sync_key": "BZY-XXXX-XXXX-SYNC",
    "data": {"company": {}, "sales": {}, "ledgers": {}},
    "sent_at": "2025-03-25T10:00:00"
  }'
```

---

## Architecture

```
bizzys.in (Netlify)
    ↓ API calls
Railway Backend (FastAPI)
    ↓ stores data
SQLite DB (upgrades to PostgreSQL)
    ↑ syncs data
Tally Connector (Windows PC)
    ↑ reads from
Tally Prime / ERP 9
```

---

## Your first client test checklist

1. Deploy backend to Railway ✓
2. Get Railway URL
3. Update BIZZY_API in connector.py with Railway URL
4. Client installs connector on their Tally PC
5. Client registers on bizzys.in
6. Client pastes their sync key into connector
7. First sync runs — check /api/admin/sync-logs
8. Dashboard populates with real Tally data
