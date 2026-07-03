# kita-cuan-wa-bot (laris.AI)

Bot WhatsApp pencatatan transaksi UMKM — laris.AI.

**Repository ini berdiri sendiri** (bukan monorepo). Berisi kode
bot FastAPI + dependency yang dibutuhkan. Service yang sebelumnya
berada di monorepo `157vis/bukuwarung-ai` sudah di-split ke sini
agar tidak terjadi benturan konfigurasi Railway.

---

## Struktur

```
.
├── main.py              # FastAPI entry point (webhook + endpoint)
├── agents.py            # AI extractor (Groq) — transaksi, intent
├── bot_helpers.py       # Helper untuk format pesan WA
├── orchestrator.py      # Orchestrator transaksi
├── config.py            # Constants (STOCK_THRESHOLD, dll)
├── paths.py             # Path helper (self-contained)
├── brand.py             # Branding constants
├── laris_core.py        # Logika bisnis bersama
├── log_config.py        # Logging setup
├── requirements.txt     # Python deps (FastAPI, Groq, Supabase)
├── Procfile             # Railway start command
├── railway.toml         # Railway config
└── .env.example         # Environment variables template
```

## Environment Variables

| Name | Required | Keterangan |
|---|---|---|
| `SUPABASE_URL` | Ya | URL Supabase project |
| `SUPABASE_KEY` | Ya | Service role key |
| `GROQ_API_KEY` | Ya | https://console.groq.com/keys |
| `WA_API_KEY` | Ya | Token Fonnte (https://md.fonnte.com) |
| `WA_PROVIDER` | Tidak | Default `fonnte` |
| `STOCK_THRESHOLD` | Tidak | Default `10` |
| `REORDER_QTY` | Tidak | Default `20` |
| `PORT` | Auto | Di-inject Railway |

## Local Development

```bash
# Install deps
pip install -r requirements.txt

# Setup env
cp .env.example .env
# Edit .env dengan kredensial Anda

# Run
python -m uvicorn main:app --reload --port 8000
```

## Railway Deployment

1. Login ke https://railway.app
2. **+ New Service** → GitHub Repo → `157vis/kita-cuan-wa-bot`
3. **Settings**:
   - **Root Directory**: kosongkan (default `/`)
   - **Custom Start Command**: ON, isi `python -m uvicorn main:app --host 0.0.0.0 --port $PORT`
4. **Variables**: tambahkan env vars di atas
5. **Deploy**

## API Endpoints

- `GET /` — health check
- `GET /health` — health check
- `POST /webhook` — webhook Fonnte WhatsApp

## License

Private — laris.AI UMKM Indonesia
