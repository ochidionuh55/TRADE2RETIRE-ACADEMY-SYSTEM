# T2R OS — Trade2Retire Academy operating system

A standalone service that gives Trade2Retire **one canonical record** underneath
behaviour the academy already does. It is a completely separate project — its
own repo, its own database, its own deployment. No shared code or branding with
any other product; students and staff only ever see Trade2Retire.

> Engineering principle: understand → measure → design → validate → build.
> One source of truth. Telegram is an interface, not the database. No fake AI,
> no fake scoring, no fabricated insight. Fail visibly rather than invent data.

## What this MVP proves

The smallest loop that touches every principle, for **one live cohort**:

```
Friday report  →  Mentor review  →  Missed-report intervention  →  CEO weekly brief
```

- **Canonical identity + append-only event store** — `Person`, `Enrolment`,
  `Cohort`, and an immutable `Event` stream that everything else is a view of.
- **Structured Friday report** over Telegram (not "how was your week?").
- **Explainable intervention rule** — two consecutive missed reports raises a
  flag that states its own reason and a mentor task with an SLA.
- **CEO brief** assembled only from canonical data; says *"insufficient data"*
  rather than guessing.
- **RBAC + provenance from day one** — access is by stored role, every event is
  attributable (including to `system`).

Intentionally **not** in the MVP: Trader Score, AI coach, full CRM/finance,
public passport page, multi-tenancy. Those are Phases 2–3 in Discovery 001.

## Tune the academy's policy in one place

`app/config.py` → `PROCESS`. Every value a real academy answer should replace is
marked `TODO(founder)`:

- `friday_questions` — the weekly report template.
- `missed_reports_threshold` — what counts as "needs attention" (default 2).
- `intervention_sla_days` — mentor response window (default 3).
- cohort → mentor assignment (`Enrolment.mentor_id`) — one mentor per student
  for now; a shared model moves this to its own table.

## Architecture

| Process | Command | Role |
|---|---|---|
| `api` | `uvicorn app.api:app` | health + schema bootstrap; future web portal |
| `bot` | `python -m app.bot` | the Telegram interface |
| `worker` | `python -m app.worker` | daily overdue sweep + missed-report rule |

All three share `app/services.py` over one Postgres database. No business logic
lives in the bot.

## Run locally (PowerShell)

```powershell
python -m venv .venv; .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env    # then set T2R__TELEGRAM_TOKEN and T2R__ADMIN_IDS
# with a local Postgres running and T2R__DATABASE_URL set:
python scripts/seed_demo.py    # optional demo cohort
python -m app.bot              # start the bot
```

## Deploy on Railway (fresh project — nothing shared)

1. Create a **new** GitHub repo, push this folder, then a **new** Railway
   project from it. Add a **Postgres** plugin.
2. Create three services from the same repo (Procfile targets): `api`, `bot`,
   `worker`.
3. On each service set variables:
   - `T2R__DATABASE_URL` → the Railway Postgres async URL
     (`postgresql+asyncpg://…`)
   - `T2R__TELEGRAM_TOKEN` → your bot token (a **new** bot from @BotFather)
   - `T2R__ADMIN_IDS` → your Telegram numeric id
   - `T2R__ACADEMY_NAME` → `Trade2Retire Academy`
4. The `api`/`worker` create the schema on boot. Optionally run
   `python scripts/seed_demo.py` once to see the loop.
5. Open the bot: `/start`, `/friday`, `/queue`, `/brief`.

## Provenance

Nothing user-facing references any other project. This repo is the source of
truth for T2R OS and stands entirely on its own.
