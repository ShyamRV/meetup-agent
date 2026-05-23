# meetup-networking-agent

An ASI:One chat agent that helps people at a tech meetup find the right
person to talk to next — co-founders, collaborators, mentors, or
interesting humans — by reading each attendee's LinkedIn + GitHub
profile and matching them semantically against everyone else who has
checked into the same event.

Built on the standard `innovation-lab-agents` stack:

- **uAgents** framework + ASI:One chat protocol
- **agents_shared** for Sentry, health probes, SSRF-safe HTTP,
  pgvector I/O, rate limiting and structured logs
- **Postgres + pgvector** for state (stateless pods; nothing is held
  in memory across restarts)
- Idempotent writes via `ON CONFLICT DO UPDATE`
- Dockerised, non-root, `tini`-supervised, health-checked

---

## What it does

```
QR scan ──► chat with the agent
              │
              ├── LinkedIn OAuth ─► fetch profile (name, headline,
              │                                roles, skills, education)
              ├── GitHub OAuth   ─► fetch repos, languages,
              │                     pinned projects, activity
              ├── merge → embed → upsert into meetup_attendees + pgvector
              │
              └── chat: "what kind of connection are you looking for?"
                        │
                        ├── co-founder
                        ├── contributor / collaborator
                        ├── mentor
                        └── friend / general
                        │
                        ▼
                pgvector top-20 ─► intent re-rank ─► top 5 + explanation
                        │
                        └── natural-language refinements:
                            "only Rust devs", "anyone in fintech",
                            "designers in the room", "10+ years of experience"
```

---

## How attendees use it

1. Scan the QR code at the venue (encodes
   `https://asi1.ai/chat?agent=meetup&event_id=<EVENT_ID>`).
2. Tap the **LinkedIn** authorisation link the agent sends.
3. Tap the **GitHub** authorisation link (optional — improves match
   accuracy, especially for builders).
4. Say what kind of connection you're looking for.
5. Get 3–5 matches with concrete shared/complementary signals and a
   one-sentence reason to chat.
6. Refine in natural language as you go.

Nothing is shared with other attendees beyond their own LinkedIn URL.
The agent never collects passwords — it uses standard OAuth 2.0.

---

## How an organiser registers an event

Insert a row into `meetup_events`. The `qr_seed` is the value the QR
generator binds to:

```sql
INSERT INTO meetup_events (event_name, organizer_email, qr_seed, event_date, location)
VALUES ('Fetch.ai London Meetup', 'hello@example.com', 'fetch-ldn-2026-05', NOW(), 'King''s Cross');
```

Take the resulting `id` and put it in the QR-code URL:

```
https://asi1.ai/chat?agent=meetup&event_id=<id>
```

---

## How matching works (non-technical)

The agent reads each attendee's LinkedIn + GitHub, summarises it into a
rich paragraph (name, current role, recent roles, skills, bio, top open
source projects), and turns that paragraph into a *semantic embedding*
— a list of 1536 numbers that captures meaning. Two people whose
embeddings are close are likely to find each other interesting.

When you ask for matches, the agent finds the closest people in the
room and then **re-ranks** them based on the kind of connection you
want:

- **Co-founder** — boost complementary backgrounds (tech ↔ business)
  with some shared core skills.
- **Contributor / collaborator** — boost shared languages and active
  open source contributors.
- **Mentor** — boost people with materially more years in your domain.
- **Friend / general** — pure semantic similarity, no re-ranking.

The matcher then asks the LLM to write one warm, specific sentence
explaining the strongest signal between the two of you.

---

## Privacy

- We store: your LinkedIn name, headline, current and past roles,
  skills, education, summary; your GitHub username, bio, languages,
  pinned repos and recent activity counts.
- We do **not** store passwords, OAuth refresh tokens persistently,
  emails, phone numbers, or any payload not needed for matching.
- All access tokens are held in-memory only for the duration of the
  check-in session and discarded; only their SHA-256 fingerprint is
  ever logged.
- Profile data is **auto-deleted 30 days after the event** by the
  `meetup_purge_expired()` SQL function (see
  `agents-db/migrations/20260522_02_meetup_retention.sql`). An
  organiser can trigger an immediate purge for a given attendee on
  request.

---

## Local development

PowerShell:

```powershell
# 1. Start Postgres with the pgvector extension. We map host port 55432
# because 5432 is often already used by local Postgres or Docker Desktop.
docker rm -f meetup-pg 2>$null
docker run -d --name meetup-pg `
    -p 55432:5432 `
    -e POSTGRES_PASSWORD=meetup `
    -e POSTGRES_USER=meetup `
    -e POSTGRES_DB=meetup `
    pgvector/pgvector:pg16

# 2. Apply migrations. PowerShell does not support bash-style "< file"
# redirection, so pipe file contents into docker exec.
Get-Content ..\agents-db\migrations\20260522_01_meetup_networking.sql |
    docker exec -i meetup-pg psql -U meetup -d meetup -v ON_ERROR_STOP=1

Get-Content ..\agents-db\migrations\20260522_02_meetup_retention.sql |
    docker exec -i meetup-pg psql -U meetup -d meetup -v ON_ERROR_STOP=1

# 3. Install dependencies.
uv venv .venv
.\.venv\Scripts\Activate.ps1
uv pip install -e ..\agents_shared
uv pip install -e .[dev]

# 4. Copy and edit your env.
Copy-Item .env.example .env
# Set DATABASE_URL=postgresql://meetup:meetup@localhost:55432/meetup
# Fill in LINKEDIN_*, GITHUB_*, OPENAI_API_KEY, ASI1_API_KEY.

# 5. Run.
$env:PYTHONPATH = "$PWD;$(Resolve-Path ..)"
python agent.py
```

Bash:

```bash
docker rm -f meetup-pg 2>/dev/null || true
docker run -d --name meetup-pg -p 55432:5432 \
    -e POSTGRES_PASSWORD=meetup -e POSTGRES_USER=meetup -e POSTGRES_DB=meetup \
    pgvector/pgvector:pg16

psql postgresql://meetup:meetup@localhost:55432/meetup \
    -f ../agents-db/migrations/20260522_01_meetup_networking.sql
psql postgresql://meetup:meetup@localhost:55432/meetup \
    -f ../agents-db/migrations/20260522_02_meetup_retention.sql

uv venv .venv
source .venv/bin/activate
uv pip install -e ../agents_shared
uv pip install -e .[dev]

cp .env.example .env
# Set DATABASE_URL=postgresql://meetup:meetup@localhost:55432/meetup
python agent.py
```

The agent listens on `AGENT_PORT` (default `8000`) for uAgent traffic
and on `HEALTH_PORT` (default `8080`) for `/healthz`, `/readyz`, and
the OAuth callbacks.

### Running tests

```bash
pytest
```

The test suite uses an in-memory DB and a deterministic vector store —
no Postgres or network calls required.

---

## Environment variables

| Variable                    | Required | Purpose                                               |
| --------------------------- | -------- | ----------------------------------------------------- |
| `AGENT_NAME`                | -        | Identity broadcast on the uAgent network.             |
| `AGENT_SEED`                | yes      | Deterministic key seed. **Keep secret.**              |
| `AGENT_PORT`                | -        | uAgent port (default `8000`).                         |
| `AGENT_ENDPOINT`            | -        | Public uAgent endpoint URL.                           |
| `DATABASE_URL`              | yes      | Postgres DSN. Must have the `vector` extension.       |
| `DB_POOL_MIN` / `DB_POOL_MAX` | -      | asyncpg pool sizing.                                  |
| `LINKEDIN_CLIENT_ID`        | yes      | LinkedIn OAuth app.                                   |
| `LINKEDIN_CLIENT_SECRET`    | yes      | LinkedIn OAuth app.                                   |
| `LINKEDIN_REDIRECT_URI`     | yes      | Must match `https://<host>/oauth/linkedin/callback`.  |
| `GITHUB_CLIENT_ID`          | yes      | GitHub OAuth app.                                     |
| `GITHUB_CLIENT_SECRET`      | yes      | GitHub OAuth app.                                     |
| `GITHUB_REDIRECT_URI`       | yes      | Must match `https://<host>/oauth/github/callback`.    |
| `OPENAI_API_KEY`            | yes      | Embedding generation.                                 |
| `EMBEDDING_MODEL`           | -        | Default `text-embedding-3-small`.                     |
| `ASI1_API_KEY`              | -        | Optional — used for the match explanation sentence and refinement parsing. Falls back to a deterministic regex/template if absent. |
| `ASI1_MODEL`                | -        | Default `asi1-mini`.                                  |
| `ASI1_API_BASE`             | -        | Default `https://api.asi1.ai/v1`.                     |
| `SENTRY_DSN`                | -        | Sentry project DSN. Disabled if empty.                |
| `ENVIRONMENT`               | -        | `development` / `staging` / `production`.             |
| `LOG_LEVEL`                 | -        | Default `INFO`.                                       |
| `HEALTH_PORT`               | -        | Default `8080`.                                       |
| `RETENTION_DAYS`            | -        | Default `30`.                                         |
| `SESSION_SECRET`            | yes      | 32+ byte secret used to sign OAuth `state` HMACs.     |

---

## File layout

```
meetup-networking-agent/
├── agent.py                  # uAgent entry point + chat protocol wiring
├── callback_server.py        # /oauth/<platform>/callback routes
├── config.py                 # env-backed Settings (Pydantic)
├── session.py                # in-memory per-sender onboarding state
├── handlers/
│   ├── onboarding.py         # LinkedIn + GitHub OAuth (signed state, rate-limited)
│   ├── profile_fetch.py      # LinkedIn + GitHub API + merge_profiles()
│   ├── embedding.py          # profile_to_text + embed_profile + idempotent upsert
│   ├── matching.py           # pgvector search + intent re-rank + explain_match
│   └── conversation.py       # parse_refinement + apply_refinement
├── models/                   # Pydantic v2, strict=True
│   ├── profile.py            # LinkedInProfile / GitHubProfile / ProfileData
│   ├── attendee.py           # DB row model
│   ├── match.py              # MatchResult / MatchExplanation / MatchSignal
│   └── refinement.py         # RefinementQuery / Intent
├── prompts/
│   └── system_prompt.txt     # Agent persona + flow guidance
├── db/
│   └── queries.py            # Typed SQL helpers — only place we touch SQL
├── tests/                    # pytest-asyncio suite (no external services)
├── Dockerfile                # multi-stage, non-root, tini, healthcheck
├── pyproject.toml            # uv-friendly project metadata
└── .env.example
```

---

## Operational notes

- **Stateless pods.** Anything that must survive a restart lives in
  Postgres. The session store (`session.py`) is a soft cache of
  in-flight onboarding state; loss of the cache only forces the user
  to re-authorise once.
- **Idempotency.** Every write is `ON CONFLICT DO UPDATE`. Retrying a
  check-in (network blip, browser refresh) is safe.
- **Rate limiting.** OAuth callbacks are rate-limited per
  `(platform, session_id)` and per remote IP (see
  `agents_shared.rate_limit`).
- **SSRF safety.** All external HTTP calls go through
  `agents_shared.security_fetch`, which refuses private / link-local /
  metadata IPs and enforces HTTPS in production.
- **Observability.** Structured JSON logs everywhere. Every `except`
  block hands the exception to `agents_shared.sentry.capture_exception`
  with operation context.
- **Health.** `/readyz` flips to 200 only after the DB connection is
  confirmed. Kubernetes / Render / Fly will keep the pod out of
  rotation until then.

---

## License

Apache 2.0. See `LICENSE` at the monorepo root.
