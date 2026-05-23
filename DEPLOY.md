# Deploy Meetup Networking Agent — 30 min playbook

Target shape:

- **Postgres + pgvector** on **Neon** (free, instant pgvector).
- **Agent container** on **Render** (Docker web service, free tier OK).
- The agent runs in **mailbox** mode so ASI:One / DeltaV reach it via
  Agentverse without us exposing a public uAgents envelope port. The
  same container also serves `/healthz`, `/readyz`, `/status` and the
  OAuth callback routes on Render's public HTTPS port.

---

## Before you start — have these ready

- [render.com](https://render.com) account (free, no credit card)
- [neon.tech](https://neon.tech) account (free Postgres with pgvector)
- [agentverse.ai](https://agentverse.ai) account (free)
- LinkedIn app at [linkedin.com/developers](https://www.linkedin.com/developers/apps)
  with **Sign In with LinkedIn using OpenID Connect** product added
- GitHub OAuth app at [github.com/settings/developers](https://github.com/settings/developers)
- OpenAI API key (for `text-embedding-3-small`)

---

## Step 1 — Neon Postgres (3 min)

1. [neon.tech](https://neon.tech) → **New Project** → name: `meetupdb` → **Create**.
2. Copy the **connection string** (looks like
   `postgresql://...neon.tech/...?sslmode=require`).
3. In Neon's **SQL Editor** run:
   ```sql
   CREATE EXTENSION IF NOT EXISTS vector;
   ```
4. Set the env var locally and apply migrations:
   ```powershell
   $env:DATABASE_URL = "<paste your Neon URL>"
   cd meetup-networking-agent
   python scripts/bootstrap_db.py
   ```
   Expect: `applied 2 migration(s)`.
5. Verify the schema:
   ```powershell
   python scripts/verify_db.py
   ```
   Expect: `OK: schema looks healthy`.
6. Create the first event row:
   ```powershell
   python scripts/create_event.py --name "Your Meetup" --location "Your City"
   ```
   Copy the **Event ID** printed at the end — call it `EVENT_ID`.

---

## Step 2 — OAuth apps (5 min)

Skip this step if you already have working LinkedIn + GitHub apps.

### LinkedIn

1. [linkedin.com/developers/apps](https://www.linkedin.com/developers/apps) → **Create app**.
2. **Products** tab → request **Sign In with LinkedIn using OpenID Connect** (approved instantly).
3. **Auth** tab → add a placeholder redirect URL:
   `https://YOUR-RENDER-HOST/oauth/linkedin/callback`
   (We'll fix the host in Step 3.)
4. Copy **Client ID** + **Client Secret**.

### GitHub

1. [github.com/settings/developers](https://github.com/settings/developers) → **OAuth Apps** → **New OAuth App**.
2. **Homepage URL**: `https://YOUR-RENDER-HOST`
3. **Authorization callback URL**: `https://YOUR-RENDER-HOST/oauth/github/callback`
4. Generate client secret. Copy **Client ID** + **Client Secret**.

---

## Step 3 — Render deploy (5 min)

1. Push this repo to GitHub if you haven't already.
2. [dashboard.render.com](https://dashboard.render.com) → **New** → **Blueprint** → connect this repo.
3. Render reads `render.yaml` at the repo root and provisions a web
   service using `meetup-networking-agent/Dockerfile`.
4. Open the service → **Environment** tab → fill in every `sync: false`
   variable:

   | Variable | Value |
   |---|---|
   | `AGENT_SEED` | `python -c "import secrets; print(secrets.token_hex(32))"` |
   | `SESSION_SECRET` | `python -c "import secrets; print(secrets.token_urlsafe(32))"` |
   | `DATABASE_URL` | The Neon URL from Step 1 |
   | `LINKEDIN_CLIENT_ID` / `SECRET` | From Step 2 |
   | `LINKEDIN_REDIRECT_URI` | `https://<RENDER_HOST>/oauth/linkedin/callback` |
   | `GITHUB_CLIENT_ID` / `SECRET` | From Step 2 |
   | `GITHUB_REDIRECT_URI` | `https://<RENDER_HOST>/oauth/github/callback` |
   | `OPENAI_API_KEY` | Your OpenAI key |
   | `DEFAULT_EVENT_ID` | `EVENT_ID` from Step 1 |
   | `SENTRY_DSN` | Optional, leave blank to disable |

5. Click **Save Changes**. Render deploys automatically (3-5 min).
6. Note the URL Render assigns you, e.g.
   `https://meetup-networking-agent.onrender.com`. Call it `RENDER_HOST`.
7. Go back to **LinkedIn + GitHub** and replace the
   `YOUR-RENDER-HOST` placeholders in their redirect URLs with the real host.

---

## Step 4 — Get the agent address (1 min)

```bash
curl https://<RENDER_HOST>/status
```

Expected JSON includes `"address": "agent1q..."`. Copy that string — call it `AGENT_ADDRESS`. You'll paste it into Agentverse and the QR script.

While you're here, also confirm:

```bash
curl https://<RENDER_HOST>/healthz   # -> {"status":"ok"}
curl https://<RENDER_HOST>/readyz    # -> {"ready":true}  (200)
```

If `/readyz` returns 503, the agent can't reach Neon — re-check
`DATABASE_URL` in Render and that pgvector was created.

---

## Step 5 — Connect the agent to Agentverse (Mailbox) (3 min)

The agent registers itself with Agentverse on startup via the mailbox
transport — you just need to claim it under your account so DeltaV /
ASI:One can route messages to it.

1. [agentverse.ai](https://agentverse.ai) → **My Agents** → **+ New Agent** →
   **Connect Existing Agent** → **Mailbox**.
2. Paste `AGENT_ADDRESS`. Agentverse runs an identity challenge with
   your Render-hosted agent (watch Render logs — you'll see the mailbox
   challenge succeed).
3. When status flips to **Connected**, the agent appears in your
   dashboard. Two protocols should be visible:
   - `AgentChatProtocol` (ASI:One chat surface)
   - `MeetupMatchmaking v1.0.0` (typed surface for DeltaV)

---

## Step 6 — Publish as an AI Engine service (3 min)

1. Agentverse → **Services** → **+ New Service**.
2. **Name**: `Meetup Networking Agent`
3. **Description** (DeltaV reads this to decide when to route here):
   > Helps people at tech meetups find co-founders, contributors,
   > mentors and collaborators by matching LinkedIn and GitHub
   > profiles. Attendees scan a QR code at the event to start.
4. **Agent address**: paste `AGENT_ADDRESS`.
5. **Protocol**: `MeetupMatchmaking v1.0.0` (auto-detected from the
   published manifest).
6. **Type**: Public. Click **Publish**.

Sanity test on [deltav.agentverse.ai](https://deltav.agentverse.ai):
type *"I'm at a tech meetup, help me find a co-founder"* — AI Engine
should route to your agent.

---

## Step 7 — Print the QR (2 min)

```powershell
cd meetup-networking-agent
pip install "qrcode[pil]"

$env:AGENT_ADDRESS    = "agent1q..."
$env:DEFAULT_EVENT_ID = "<EVENT_ID from step 1>"

python scripts/print_qr.py
```

Or pass flags inline:

```powershell
python scripts/print_qr.py --agent agent1q... --event-id <EVENT_ID>
```

Output is `meetup_qr.png` at the project root. Print it at A5 size.

---

## Step 8 — Pre-meetup verification

```bash
# 1. Agent is alive on Render
curl https://<RENDER_HOST>/readyz

# 2. /status reports a non-zero address and the right event count
curl https://<RENDER_HOST>/status

# 3. Agent is connected on Agentverse → My Agents → status = Connected
#    (no curl — check the dashboard)

# 4. Scan the QR with your phone — ASI:One should load with the agent
#    pre-selected and the event_id in context.
```

For staging without real OAuth, you can also run the local dev client
against the deployed agent (it derives the target address from the same
seed you configured on Render):

```powershell
$env:AGENT_SEED = "<same seed you set in Render>"
python scripts/dev_chat.py `
  --target-seed $env:AGENT_SEED `
  --event-id    "<EVENT_ID>" `
  --message     "hi I scanned the QR" `
  --timeout     30
```

---

## During the meetup

- Bookmark `https://<RENDER_HOST>/status` and refresh to watch the
  attendee count climb.
- On Render's free tier the service sleeps after ~15 min of no traffic.
  Hitting `/status` from your phone wakes it; first scan after a sleep
  takes 30-45 s to respond.
- Enable **Auto-Deploy** in Render so the next `git push` to `main`
  ships the fix without manual clicks.

---

## After the meetup

The `meetup_purge_expired()` function (migration
`agents-db/migrations/20260522_02_meetup_retention.sql`) wipes anything
tied to events older than `RETENTION_DAYS` (default 30):

```sql
SELECT meetup_purge_expired();
```

---

## Variable cheatsheet

| Var | Required | Notes |
|---|---|---|
| `AGENT_SEED` | yes | Deterministic — changing it changes the address. |
| `AGENT_MAILBOX` | yes (set by `render.yaml`) | `true` enables Agentverse mailbox transport. |
| `AGENT_HANDLE` | optional | Pretty handle on Agentverse. |
| `DATABASE_URL` | yes | Postgres URL, must have pgvector. |
| `LINKEDIN_CLIENT_ID` / `SECRET` / `REDIRECT_URI` | yes | OIDC product. |
| `LINKEDIN_SCOPES` | optional | Override if you have a legacy app; default `openid profile email`. |
| `GITHUB_CLIENT_ID` / `SECRET` / `REDIRECT_URI` | yes | |
| `OPENAI_API_KEY` | yes | For embeddings. |
| `SESSION_SECRET` | yes | HMAC for OAuth state — keep stable across restarts. |
| `SENTRY_DSN` | optional | Recommended for live events. |
| `ENVIRONMENT` | yes | Set to `production`. |
| `LOG_LEVEL` | optional | Default `INFO`. |
| `RETENTION_DAYS` | optional | Default `30`. |
| `DEFAULT_EVENT_ID` | yes (for `/status` count + QR script) | UUID from `create_event.py`. |
| `DEV_LINKEDIN_ID` | dev only | Bypasses OAuth in non-production envs. |
| `PORT` | injected by Render | Health server binds here automatically. |

---

## Common deploy issues

| Symptom | Fix |
|---|---|
| Build fails: `No module named 'agents_shared'` | Render is building from the agent subdirectory instead of the repo root. Check `dockerContext: .` in `render.yaml` and that the service was created **as a Blueprint** (which respects `render.yaml`). |
| `/readyz` returns 503 forever | Agent can't reach Postgres — verify `DATABASE_URL`, check pgvector extension exists in Neon. |
| LinkedIn returns "scope not authorised" | Your app doesn't have OIDC product enabled. Add **Sign In with LinkedIn using OpenID Connect** under Products. |
| `oauth rejected: missing session_id` | Old build — the callback handler used to require `session_id` as a query param. Redeploy from current `main`. |
| Agentverse mailbox stuck on "pending" | Render container is asleep or its outbound poll to `agentverse.ai` is blocked. Hit `/status` to wake it, then re-trigger the connect. |
| LinkedIn profile data feels thin | OIDC returns name + email + locale only. GitHub gives the rich data; if you need LinkedIn positions/skills, ask the user in chat after auth. |
