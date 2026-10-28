# Sahayak Partners

The repository contains two independent applications. The Next.js frontend lives in `frontend/`; the FastAPI backend lives in `backend/`. Each application has its own dependencies, runtime configuration and local environment file.

The approved backend database target is PostgreSQL. Existing Next.js API routes remain in place as temporary compatibility routes and still use MongoDB. This restructuring does not migrate or modify database records and does not enable dual writes.

## Run the backend

Windows PowerShell:

```powershell
cd backend
python -m venv .venv
Copy-Item .env.example .env
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m uvicorn app.main:app --reload
```

Linux/macOS:

```bash
cd backend
python3 -m venv .venv
cp .env.example .env
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m uvicorn app.main:app --reload
```

Backend liveness endpoints:

- `http://127.0.0.1:8000/health`
- `http://127.0.0.1:8000/api/v1/health`

The sample PostgreSQL URL is a placeholder. Set `DATABASE_URL` and explicit `FRONTEND_ORIGINS` in `backend/.env`. Health endpoints do not require a database server.

## Run the frontend

Windows PowerShell:

```powershell
cd frontend
Copy-Item .env.example .env.local
npm ci
npm run dev
```

Linux/macOS:

```bash
cd frontend
cp .env.example .env.local
npm ci
npm run dev
```

Set `BACKEND_API_BASE_URL` in `frontend/.env.local` to the FastAPI origin. The Next.js `/backend-api` handler currently proxies only `GET /api/v1/health`, with no cookies or identity headers; all other backend API routes fail closed until their authentication, tenant-host, cookie, and deployment contracts are approved. FastAPI separately uses the explicit `FRONTEND_ORIGINS` allowlist for direct cross-origin requests.

The frontend's temporary compatibility routes still require `MONGODB_URI` and optionally `MONGODB_DB`. Set them in `frontend/.env.local` when those routes need a database. Do not copy real credentials into `.env.example` or commit local environment files.

## Verification

Run frontend commands from `frontend/`:

```bash
npm run typecheck
npm run lint
npm run build
```

Run backend tests from `backend/` with its virtual environment active:

```bash
python -m pytest -q
```
