# Customer Support Agent with Voice

Fresh React + FastAPI customer support agent.

## Description

The support agent answers customer questions in text or voice, looks up account data for signed-in customers, and sends anything it cannot or should not settle on its own to a human through a ticket queue.

- **Support manual knowledge base**: the agent answers policy and troubleshooting questions from `backend/manual/support-manual.md`. The manual is split by heading topic and then by paragraph, embedded into Qdrant, and searched with the `search_manual` tool. Answers cite the manual section they came from. Without Qdrant or a Gemini key, the same manual is searched by keyword.
- **Public chat and voice access**: anyone can chat or talk to the agent without signing in and ask general questions. Chat replies stream over SSE, with live step indicators. Voice connects the browser directly to Gemini Live with a short-lived, constrained token, and its tool calls still run through FastAPI. Text and voice share the same conversation history.
- **On-demand sign-in and sign-out**: the agent asks for sign-in only when a request needs account data, by calling `request_login`, which opens the sign-in dialog. After sign-in, the conversation moves to the authenticated session and the pending request is re-queued, so the customer does not have to repeat it. "Sign me out" calls `logout_session`, which revokes the session token.
- **Customer and order database lookup**: for signed-in customers, `lookup_customer`, `list_customer_orders` and `list_customer_cases` read PostgreSQL. These tools are registered only for authenticated sessions and only ever return the signed-in customer's own data, so the model can't ask for another customer's email or ID. A return request chains them in order: verify identity, tier and contract status, find the order item, then look up the tier-specific return policy.
- **Escalation tickets and support tickets to message queues**: `create_escalation_ticket` writes to the `support:escalations` Redis Stream. It is used for returns from expired-contract customers, refunds at or above the tier limit (Standard $1,000, Plus $2,000), account closures, and low-confidence manual searches. `create_support_case` saves a case in PostgreSQL and publishes it to the `support:tickets` stream, but only after the customer explicitly confirms. Both return a reference number to the customer.
- **Three Gemini models, each matched to its job**:
  | Role | Setting (default) | Why |
  | --- | --- | --- |
  | Agent reasoning and tool calling | `GEMINI_AGENT_MODEL` (`gemini-3.5-flash-lite`; `.env.example` uses `gemini-3.5-flash`) | Fast, low-cost model with reliable function calling for the multi-step tool loop. |
  | RAG query rewriting | `GEMINI_RAG_MODEL` (`gemini-3.1-flash-lite`) | Cheapest model, used only to rewrite the question into a concise search query before retrieval. The main agent still writes the answer. |
  | Voice | `GEMINI_LIVE_MODEL` (`gemini-3.8-live`) | Native audio streaming with low latency, interruption handling and live transcripts. |

  Embeddings use `gemini-embedding-001`.
- **Guardrail to prevent hallucination**: every manual search reports a confidence score, the highest cosine similarity among the top 3 chunks. Chunks below `RAG_MIN_SIMILARITY` (default 0.52; keyword fallback uses `rag_min_keyword_overlap`) are discarded. If none remain, the agent does not answer from its own knowledge. It tells the customer it couldn't find a reliable answer and creates a `low_confidence` escalation ticket for a human. The system prompt also tells the agent never to invent customer or order data, and never to say a ticket or action succeeded unless the tool result confirms it.

## Architecture

- `frontend/`: React chat page and support operations dashboard.
- `backend/app/agent/`: provider-neutral agent loop, events, tool registry and support policy.
- `backend/app/providers/`: Gemini text/function-calling adapter. Gemini Live voice uses a separate browser WebSocket session, short-lived constrained tokens from FastAPI, and FastAPI-mediated support tools.
- PostgreSQL: conversations, messages, customers, orders, support cases and trace events. There are no migration files: the SQLAlchemy models in `backend/app/database.py` and the fixtures in `backend/app/demo_data.py` are the whole schema and seed. On startup the backend creates any missing tables and inserts any demo customers, cases and orders whose ids are not present yet; existing rows are never modified. Databases created by older versions with a different schema should be recreated (`docker compose down -v` locally).
- Qdrant: manual chunks and vector search. With a Gemini key and Qdrant running, startup embeds the sample manual (`backend/manual/support-manual.md`) into Qdrant; search optionally uses the configured lightweight Gemini model to rewrite the retrieval query. Offline/Fallback mode uses keyword search over the same sample manual.
- Docker Compose: local PostgreSQL, Qdrant, and Redis Streams for escalation tickets.

The chat API returns Server-Sent Events (SSE) so the UI can display progress and agent steps. Optional voice connects from the browser to Gemini Live over WebSocket using a short-lived, configuration-constrained token issued by FastAPI. Audio stays off the Vercel function path; support tools still execute through FastAPI. Voice transcripts are stored in the same conversation history as typed messages.

## Run locally

1. Copy `backend/.env.example` to `backend/.env`, then set `GEMINI_API_KEY`.
2. Start data services from this directory: `docker compose up -d postgres qdrant redis`.
3. In a Python 3.11+ environment, install `backend/requirements.txt` and run `uvicorn app.main:app --app-dir backend --reload`.
4. In another terminal, run `cd frontend && npm install && npm run dev`.

Frontend defaults to `http://localhost:5173`; API defaults to `http://localhost:8000`. Voice requires `GEMINI_API_KEY`, Live API access for `GEMINI_LIVE_MODEL` (default `gemini-3.8-live`), and microphone permission. Browsers require localhost or HTTPS for microphone access. The Talk control is disabled in demo-provider mode.
Set `VITE_API_URL` if the backend is elsewhere. Without a Gemini key, the API uses a deterministic local demo provider so the UI and loop are usable without external credentials.

## Initial scope

Included tools: search the support manual, look up the signed-in customer from customer database, list their orders and cases, create a support case, create a human escalation ticket for expired-contract returns/refunds, tier-qualified refund amounts, or account closure, and end the chat. Account-specific tools require sign-in; support-case creation requires explicit confirmation.

The dashboard includes usage counters, conversation list, expandable event tree, and duration/latency breakdown and estimated tokens and model cost. This is a starter operations view, not an authenticated production admin system.

## Public chat and account sign-in

General policy and troubleshooting questions are available without signing in. Account-specific requests open the sign-in dialog. The local demo login uses mock email/password fixtures and issues an eight-hour signed bearer session; account tools are only registered for authenticated sessions. Set a private `SESSION_SECRET` in `backend/.env` before starting the backend.

Demo customer login credentials:

| Customer | Email | Password |
| --- | --- | --- |
| Alex Morgan | `alex@example.com` | `AlexDemo!2026` |
| Sam Lee | `sam@example.com` | `SamDemo!2026` |
| Taylor Reed | `taylor@example.com` | `TaylorDemo!2026` |

Seeded demo customer subscription data (the current database expiry date remains the source of truth):

| Customer | Tier | Contract expiry date | Expired |
| --- | --- | --- | --- |
| Alex Morgan | Plus | 2027-06-30 | No |
| Sam Lee | Standard | 2025-09-30 | Yes |
| Taylor Reed | Standard | 2027-12-31 | No |

The account lookup calculates the expired flag from the expiry date.

Passwords are stored as salted PBKDF2 hashes. The mock login is for local development only and must be replaced by a real identity provider before using real customer data.

To reset customers, support cases, and orders to the demo fixtures and clear the Redis escalation and support-ticket streams, set a strong `ADMIN_API_KEY` in `backend/.env`, restart the backend, then call `POST /api/admin/reset-db` with the `X-Admin-Key` header. Conversation history, messages, and traces are preserved. The previous `/api/admin/reset-customer-db` route remains as a compatibility alias.

The demo customer database contains five orders: two for Alex, two for Sam, and one for Taylor. Every order has at least one item priced above $1,000. `ord_3001` includes an `ABC Pro Laptop` so the return tool chain can be demonstrated. Taylor's $1,299 Cobalt Ultra Tablet order provides an active Standard-tier account for testing the $1,000 escalation threshold.

Signed-in customers can ask the agent to list their own orders. On-demand sign-in is available when the agent determines it is necessary.
Conversation is trasferred to the just signed-in session and the last request is re-queued for execution and tool use.

The sample return flow chains tools sequentially: `lookup_customer` verifies identity and obtains tier and contract status, `list_customer_orders` locates the item, and `search_manual` retrieves tier-specific return policy. It creates a Redis escalation ticket for every return/refund from an expired-contract customer, Standard refunds of $1,000 or more, and Plus refunds of $2,000 or more. Try “I want to return my ABC item” after starting Redis; sign in when prompted. An active Plus customer can return this $1,499 item without escalation; if that customer's contract is expired, the request escalates regardless of amount.

The sample mode uses fake customer records. Gemini's free tier may use submitted content to improve Google's products, so do not send real customer information until an appropriate paid-tier/privacy setup and customer authentication are in place.

## Deploy to Vercel

The repo deploys as **two Vercel projects** from the same Git repository: `backend/` (Vercel detects FastAPI and serves `app/main.py` as one Python function; do not add catch-all rewrites in `backend/vercel.json`, they change the path FastAPI sees and every route returns 404) and `frontend/` (static Vite build). Vercel cannot run the Docker Compose services, so use hosted ones (all have free tiers):

| Local service | Hosted option | Backend env var |
| --- | --- | --- |
| PostgreSQL | Neon / Vercel Postgres (Vercel Marketplace) | `DATABASE_URL` (paste as given; `?sslmode=require` is handled) |
| Redis | Upstash (Vercel Marketplace) | `REDIS_URL` (the `rediss://` URL) |
| Qdrant | Qdrant Cloud | `QDRANT_URL`, `QDRANT_API_KEY` |

No database setup step is needed: the backend creates tables and seeds the demo data on first start.

### Deploy with the CLI

Vercel deploys from a Git repository (GitHub/GitLab/Bitbucket) or from local files with the CLI. `.gitignore` and the `.vercelignore` files keep `.env`, `.venv/` and `node_modules/` out of both.

```bash
npm i -g vercel
vercel login

# 1. Backend
cd backend
vercel link                       # create/link a project, e.g. "support-api"
for name in GEMINI_API_KEY ADMIN_API_KEY SESSION_SECRET DATABASE_URL REDIS_URL QDRANT_URL QDRANT_API_KEY COMPANY_NAME; do
  vercel env add "$name" production   # paste each value when prompted
done
vercel --prod                     # note the URL, e.g. https://support-api.vercel.app

# 2. Frontend
cd ../frontend
vercel link                       # e.g. "support-ui"
vercel env add VITE_API_URL production   # the backend URL from step 1, no trailing slash
vercel --prod                     # note the URL, e.g. https://support-ui.vercel.app

# 3. Allow the frontend origin on the backend, then redeploy it
cd ../backend
vercel env add FRONTEND_ORIGINS production   # the frontend URL from step 2
vercel --prod

```


Optional model and pricing settings from `backend/.env.example` (`GEMINI_AGENT_MODEL`, `GEMINI_LIVE_MODEL`, `RAG_MIN_SIMILARITY`, ...) can be added the same way; unset ones use the defaults in `backend/app/config.py`.

With a Git repository, the dashboard flow is equivalent: **Add New → Project**, import the repo twice, with **Root Directory** `backend` and `frontend`, and set the same environment variables.

### Verify

- `https://<backend>/api/health` should report `"database": "postgres"`, `"rag": "qdrant"` and `"escalation_queue": "redis"`. `memory-demo`, `keyword-demo` or `unavailable` means that service's URL or credentials are wrong (see the function logs).
- Open the frontend, ask a general question, then sign in as a demo customer.

### Notes

- Services connect on the first request of each cold start (`ensure_started` in `backend/app/main.py`); the manual is re-embedded only when it changes. Several instances may start at once on a fresh deployment, so table creation, demo seeding and Qdrant collection creation all tolerate a concurrent instance doing the same.
- Chat replies stream over SSE from the Vercel function, within the plan's function duration limit, which is enough for normal turns. Voice connects from the browser directly to Gemini Live with an ephemeral token, so it is not limited by Vercel. Vercel provides HTTPS, which browsers require for microphone access.
- `VITE_API_URL` is compiled into the frontend: redeploy the frontend after changing it.
- The Vercel Hobby plan is free for non-commercial use. Never commit `.env`; configure secrets as Vercel environment variables.

## Company branding

Set `COMPANY_NAME` in `backend/.env` to customize the company displayed in the chat and the company identity used by the Gemini agent. It defaults to `ErwinCo`. Restart the backend and refresh the browser after changing it.

## RAG confidence threshold

use RAG_MIN_SIMILARITY in .env to set confidence threshold, otherwise 0.52 is used as per `backend/app/confiug.py`

## Example tool uses

* Prompt "I want a refund":
  * customer db query
  * login dialog

* If the requested refund meets the customer's tier threshold (Standard $1,000; Plus $2,000)
  * sign in, then create an escalation ticket in Redis and return its reference

* If the user wants to close an account
  * trigger a ticket to redis

* "sign me out"
  * trigger a tool use in the backend to handle session logout. Frontend will also respond.
  There is a trick here: the Model may return both logout_session and end_chat. Here logout_session takes priority.


## Escalation

The escalation tool requires a signed-in customer. It writes tickets to the `support:escalations` Redis Stream and returns a reference such as `ESC-261120-ABC123`. The stream uses Redis AOF persistence with a Docker volume. Check readiness at `/api/health` (`escalation_queue: redis`). In the frontend, open **Escalation queue**, enter `ADMIN_API_KEY` from `backend/.env`, and use **Refresh** or **Clear queue**. Clearing requires a confirmation and removes the queued tickets. You can also inspect tickets with `docker compose exec redis redis-cli XRANGE support:escalations - +`.

**New conversation** clears the current chat transcript and signs the customer out. Escalation tickets stay in Redis until an administrator clears them from the queue page or calls the protected reset-db endpoint; starting a fresh chat does not delete support tickets.

## Live Prompts and Steps

See the right panel to check the steps under user prompts.
Expand them to see the details.
Tool Calls are in a different color

## Voice

* Follow [ephemeral token guidance from Google](https://aistudio.google.com/docs/live-api/ephemeral-tokens)
* 3 problems were encountered and mitigations:
  * while login is displaying, the voice path was stopped. It is mitigated by blocking the dialog box while the agent speaks.
  * after login, the first request was missed. It is mitigated by re-queue the request.
  * when the AI agent speaks, the same voice should be canceled in the microphone to avoid mistaken interruption. Migitgated by using WebRTC loopback
* Echo cancellation and interruption is done by the following setting:
  * getUserMedia is called with echoCancellation: true in transport.ts





