# DHIS2 Public Health Intelligence Assistant

Conversational public health analytics for DHIS2. The implementation follows the consolidated spec in `dhis2-ai-analyst-spec.md` and ships all major surfaces at once: streaming chat, deterministic local agent fallback, DHIS2 Analytics API adapter, metadata search/sync contract, evidence tags, report/dashboard/presentation/export outputs, standalone auth, and a no-build static frontend.

## Run Backend

```powershell
python -m venv .venv
.\.venv\Scripts\pip install -r backend\requirements.txt
.\.venv\Scripts\python -m uvicorn backend.main:app --reload --reload-dir backend --host 127.0.0.1 --port 8000
```

## Run Frontend

The project can be run in two modes:

### Production / Consolidated Mode (Default)
Start the backend using the command above and open:
```text
http://127.0.0.1:8000/
```
The backend serves the pre-built static assets from `frontend/dist/`.

### Development Mode
For a hot-reloading development experience:
1. Run backend (above).
2. Start the frontend development server:
   ```powershell
   cd frontend
   npm install
   npm run dev
   ```
3. Open `http://localhost:5173/` in your browser. It will proxy requests to the backend server.

## Run with a hosted model (Gemini) against the public DHIS2 demo

Copy `.env.example` to `.env` and set:

```env
DEPLOYMENT_MODE=standalone
DHIS2_BASE_URL=https://play.im.dhis2.org/stable-2-41-10   # check it is up: /api/system/info.json
DHIS2_SERVICE_ACCOUNT_USER=admin                          # public demo credentials only
DHIS2_SERVICE_ACCOUNT_PASS=district
LLM_PROVIDER=gemini
LLM_MODEL=gemini/gemini-flash-latest                      # or another current Gemini model
LLM_API_KEY=<your Google AI Studio key>                   # never commit this
LLM_TIMEOUT_SECONDS=90
JWT_SECRET=<long random string>
STANDALONE_ACCESS_CODE=<password for anyone you share a hosted URL with>
```

Then rebuild the frontend once (`cd frontend && npm install && npm run build`) and start the backend.
Behind a corporate/HTTP proxy, also set `DISABLE_AIOHTTP_TRANSPORT=True` (LiteLLM's default
aiohttp transport ignores `HTTPS_PROXY`). The app only issues GET requests to DHIS2.

## Privacy Defaults

- No LLM or Tavily request is made by default.
- `LLM_PROVIDER=mock` uses deterministic local logic.
- Web enrichment is opt-in per request and disabled unless `TAVILY_API_KEY` is configured.
- Direct SQL is disabled unless `ENABLE_DIRECT_SQL=true`.
- Logs use session/user context and do not log secrets.

## Verification

```powershell
python -m pytest backend/tests
python scripts/eval_metadata.py
```

The metadata evaluation is a small local smoke gate. Replace `GOLDEN` with 50 real analyst terms from the target DHIS2 instance before production rollout.
