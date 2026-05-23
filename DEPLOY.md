# Deploying the meetup-networking-agent

Target deployment shape:

* **Agent container** runs on **Railway** (any other Docker host works
  identically — Render, Fly.io, AWS Fargate).
* **Postgres + pgvector** runs as a separate Railway service.
* The agent registers with **Agentverse as a Mailbox agent** so that
  ASI:One and DeltaV can reach it without us exposing a public uAgents
  envelope endpoint. The same container also serves health probes and
  OAuth callbacks on Railway's public HTTPS port.

The whole thing fits in ~25 minutes once you have the OAuth apps ready.

---

## Pre-flight checklist

| What | Where | Notes |
|---|---|---|
| Agentverse account | https://agentverse.ai | Free signup |
| Railway account | https://railway.app | Free trial credit |
| LinkedIn Developer app | https://www.linkedin.com/developers/apps | OIDC product is enough |
| GitHub OAuth app | https://github.com/settings/developers | |
| OpenAI API key | https://platform.openai.com/api-keys | For `text-embedding-3-small` |
| Repo pushed to GitHub | | Railway deploys from a git remote |

---

## Step 1 — Provision Postgres on Railway (3 min)

1. Railway dashboard → **New Project** → **Provision PostgreSQL**.
2. Open the new Postgres service → **Variables** tab → copy
   `DATABASE_URL` (the public one; starts with
   `postgresql://...@containers-us-west.railway.app:...`).
3. Enable pgvector — open the **Data** tab → **Query** → run:

   ```sql
   CREATE EXTENSION IF NOT EXISTS vector;
   CREATE EXTENSION IF NOT EXISTS pgcrypto;
   ```

4. From your laptop, apply the meetup migrations:

   ```powershell
   $env:DATABASE_URL = "postgresql://...railway.app:.../railway"
   cd meetup-networking-agent
   python scripts/bootstrap_db.py
   ```

   You should see `applied 2 migration(s)`.

---

## Step 2 — Register OAuth apps (5 min)

Skip this step if you already have working LinkedIn + GitHub apps.

### LinkedIn

1. https://www.linkedin.com/developers/apps → **Create app**.
2. Fill required fields (LinkedIn page can be your own profile page).
3. **Products** tab → request **"Sign In with LinkedIn using OpenID
   Connect"**. Approved instantly.
4. **Auth** tab:
   - Add redirect URL (we'll fill in the real Railway host in step 4,
     for now use a placeholder you'll edit later):
     `https://YOUR-RAILWAY-HOST/oauth/linkedin/callback`
   - Copy **Client ID** + **Client Secret**.

### GitHub

1. https://github.com/settings/developers → **OAuth Apps → New OAuth App**.
2. **Homepage URL**: `https://YOUR-RAILWAY-HOST`
3. **Authorization callback URL**:
   `https://YOUR-RAILWAY-HOST/oauth/github/callback`
4. Generate client secret. Copy **Client ID** + **Client Secret**.

(You'll update both redirect URLs after Railway gives you a host in
step 4.)

---

## Step 3 — Deploy the agent to Railway (5 min)

1. Push this repository to GitHub if you haven't already.
2. In the Railway project from step 1 → **+ New** → **GitHub Repo** →
   pick this repo. Railway auto-detects the `railway.json` at the root
   and uses `meetup-networking-agent/Dockerfile`.
3. Open the new service → **Settings** → **Networking** → **Generate
   Domain**. Note the host, e.g. `meetup-agent-production.up.railway.app`
   — this is your `RAILWAY_HOST`.
4. Go back to LinkedIn + GitHub OAuth apps and replace
   `YOUR-RAILWAY-HOST` with this hostname in both redirect URLs.

### Environment variables

In the agent service → **Variables** → add:

```
AGENT_SEED                = <generate: python -c "import secrets; print(secrets.token_hex(32))">
AGENT_MAILBOX             = true
AGENT_HANDLE              = meetup-networking-agent
ENVIRONMENT               = production
LOG_LEVEL                 = INFO
SESSION_SECRET            = <generate: python -c "import secrets; print(secrets.token_urlsafe(32))">

DATABASE_URL              = <from step 1, the Postgres service URL>

LINKEDIN_CLIENT_ID        = <from step 2>
LINKEDIN_CLIENT_SECRET    = <from step 2>
LINKEDIN_REDIRECT_URI     = https://<RAILWAY_HOST>/oauth/linkedin/callback

GITHUB_CLIENT_ID          = <from step 2>
GITHUB_CLIENT_SECRET      = <from step 2>
GITHUB_REDIRECT_URI       = https://<RAILWAY_HOST>/oauth/github/callback

OPENAI_API_KEY            = <your OpenAI key>
EMBEDDING_MODEL           = text-embedding-3-small

# Optional
SENTRY_DSN                = <leave empty or paste a real DSN>
```

Tip: Railway injects `DATABASE_URL` for free if you click **Connect**
on the Postgres service and pick the agent service — that wires it
through a project variable so you don't paste it manually.

5. Trigger a deploy → watch the logs. You should see, in order:

   ```
   INFO: Starting agent with address: agent1q...
   INFO: Agent inspector available at https://agentverse.ai/inspect/...
   INFO: agents_shared.health: health.server_started host=0.0.0.0 port=<railway $PORT>
   INFO: agent.ready ... transport=mailbox
   ```

6. Verify the public HTTP surface from your laptop:

   ```powershell
   curl https://<RAILWAY_HOST>/healthz
   curl https://<RAILWAY_HOST>/readyz
   curl https://<RAILWAY_HOST>/status
   ```

   All should return 200. `/status` reports the deployed agent
   address and current attendee count.

Copy the `agent1q...` address from the logs. This is your
permanent public address — call it `AGENT_ADDRESS` below.

---

## Step 4 — Connect the agent to your Agentverse mailbox (3 min)

The agent is now publishing itself to Agentverse via the mailbox
transport, but it needs to be claimed by your Agentverse account so
DeltaV / ASI:One can route messages to it.

1. https://agentverse.ai → **My Agents** → **+ New Agent** →
   **Connect Existing Agent** → **Mailbox**.
2. Paste the `AGENT_ADDRESS` from step 3.
3. Agentverse will perform an identity challenge round-trip with the
   running agent. Watch the Railway logs — you should see a
   `mailbox` access challenge succeed.
4. Once status flips to **Connected**, the agent appears in your
   Agentverse dashboard. Two protocols should be visible:
   - `AgentChatProtocol` (ASI:One chat surface)
   - `MeetupMatchmaking v1.0.0` (typed surface for DeltaV)

---

## Step 5 — Publish as an AI Engine service (3 min)

1. Agentverse → **Services** → **+ New Service**.
2. **Name**: `Meetup Networking Agent`
3. **Description** — this is what DeltaV reads to decide when to route
   prompts to your agent. Use something close to:

   > Helps attendees at tech meetups find relevant connections. Given a
   > meetup event ID from a QR code and an authenticated LinkedIn
   > profile (optionally GitHub), returns the top 5 attendees worth
   > talking to ranked by intent: co-founder, contributor, mentor,
   > friend, or general networking. Supports natural-language
   > refinement filters like "only Rust devs" or "anyone in fintech".

4. **Agent address**: paste `AGENT_ADDRESS`.
5. **Protocol**: `MeetupMatchmaking v1.0.0` (auto-detected from the
   published manifest).
6. **Type**: Public.
7. Click **Publish**. Status should flip to Active within ~30 seconds.

Sanity test from https://deltav.agentverse.ai → start a chat → type
*"I'm at a tech meetup, help me find a co-founder"*. AI Engine should
route to your agent and ask for the event id.

---

## Step 6 — Create an event row and print the QR (2 min)

```powershell
$env:DATABASE_URL = "postgresql://...railway.app:.../railway"

$eventSql = @"
INSERT INTO meetup_events (event_name, qr_seed, organizer_email, location, event_date)
VALUES ('Innovation Lab Meetup', 'EVT_2026_001', 'you@example.com', 'Your City', NOW())
ON CONFLICT (qr_seed) DO UPDATE SET event_name = EXCLUDED.event_name
RETURNING id;
"@

# returns the event UUID
psql "$env:DATABASE_URL" -t -A -c $eventSql
```

(If you don't have `psql` locally, paste the SQL into Railway's
Postgres query editor — same result.)

Take the UUID and render the QR:

```powershell
cd meetup-networking-agent
pip install "qrcode[pil]"

python scripts/print_qr.py `
  --agent-address "agent1q..." `
  --event-id      "<EVENT_UUID>" `
  --event-name    "Innovation Lab Meetup" `
  --out           meetup_qr.png `
  --print-url
```

Print `meetup_qr.png` at A5 size. Done.

---

## Step 7 — Pre-meetup verification (3 min)

```powershell
# 1. Agent is alive on Railway
curl https://<RAILWAY_HOST>/readyz
curl https://<RAILWAY_HOST>/status

# 2. Agent is reachable via Agentverse mailbox
# → log into Agentverse → My Agents → status = Connected

# 3. Send a real chat message via the local dev client
$env:AGENT_SEED = "<same seed as Railway env>"
python meetup-networking-agent/scripts/dev_chat.py `
  --target-seed $env:AGENT_SEED `
  --event-id    "<EVENT_UUID>" `
  --message     "hi I scanned the QR" `
  --timeout     30

# Expected: greeting + a clickable LinkedIn OAuth URL
```

If you scan the QR with your phone, ASI:One should open with the
agent pre-loaded and the event id in context.

---

## What stays on disk after a meetup

- `meetup_events` rows — kept until manually deleted.
- `meetup_attendees`, `meetup_embeddings`, `meetup_connections` — the
  `meetup_purge_expired()` migration (file
  `agents-db/migrations/20260522_02_meetup_retention.sql`) wipes
  anything tied to events older than `RETENTION_DAYS` (default 30).

Run it manually after the event if you want immediate cleanup:

```sql
SELECT meetup_purge_expired();
```

---

## Variable cheatsheet

| Var | Required | Notes |
|---|---|---|
| `AGENT_SEED` | yes | Deterministic — changing it changes the address |
| `AGENT_MAILBOX` | yes for Railway | `true` enables Agentverse mailbox transport |
| `AGENT_HANDLE` | optional | Pretty handle on Agentverse |
| `DATABASE_URL` | yes | Postgres URL, must have pgvector |
| `LINKEDIN_CLIENT_ID` / `SECRET` / `REDIRECT_URI` | yes | OIDC product |
| `LINKEDIN_SCOPES` | optional | Override if you have a legacy app; default `openid profile email` |
| `GITHUB_CLIENT_ID` / `SECRET` / `REDIRECT_URI` | yes | |
| `OPENAI_API_KEY` | yes | For embeddings |
| `SESSION_SECRET` | yes | HMAC for OAuth state — keep stable across restarts |
| `SENTRY_DSN` | optional | Recommended for live events |
| `ENVIRONMENT` | yes | Set to `production` |
| `LOG_LEVEL` | optional | Default `INFO` |
| `RETENTION_DAYS` | optional | Default `30` |
| `DEV_LINKEDIN_ID` | dev only | Bypasses OAuth in non-production envs |
| `PORT` | injected by Railway | Health server binds here automatically |

---

## Common deploy issues

| Symptom | Fix |
|---|---|
| Build fails: `No module named 'agents_shared'` | Check that you deployed from the **repo root**, not the agent subdirectory — `railway.json` references `meetup-networking-agent/Dockerfile` and needs the parent context. |
| `/readyz` returns 503 forever | Agent can't reach Postgres — verify `DATABASE_URL`, then check the pgvector extension exists. |
| LinkedIn returns "scope not authorised" | Your app doesn't have OIDC product enabled. Add **Sign In with LinkedIn using OpenID Connect** under Products. |
| `oauth rejected: missing session_id` | You're on an old build — the callback handler used to require `session_id` as a query param. Redeploy from the current main. |
| Agentverse mailbox stuck on "pending" | The agent isn't running, or its outbound mailbox poll is being firewalled. Check Railway logs for `mailbox` errors and confirm outbound HTTPS to `agentverse.ai` is allowed. |
| LinkedIn profile data feels thin | OIDC returns name + email + locale only. The matching pool is strong because GitHub data is rich; if you need LinkedIn positions/skills, ask the user in chat after auth. |
