# Inbox Zero LLM Dashboard

A lightweight dashboard for tracking AI and automation metrics from a self-hosted [Inbox Zero](https://github.com/elie222/inbox-zero) instance. It queries your PostgreSQL database and Redis directly to visualize how effectively pattern learning is reducing LLM calls over time.

![Dashboard screenshot](screenshot.png)

## What it tracks

- **LLM Efficiency** — percentage of rule matches handled by learned patterns vs. AI calls
- **Signal vs. Noise** — how much of your email is marketing, newsletters, cold email, and notifications vs. real mail
- **AI Cost** — estimated and actual (via Redis) cost per account, with cumulative savings from pattern matching
- **Pattern Growth** — cumulative learned patterns over time by source (AI-generated, user-created, label-based)
- **Tier Migration** — how rule categories move from Tier 3 (AI, paid) to Tier 1 (pattern, free)
- **Activity by Account** — rule executions per email account per day
- **Action Distribution** — breakdown of automated actions (archive, label, draft, etc.)
- **Token Usage** — input/output/cached token counts and cache hit rates per account

## Prerequisites

- A running Inbox Zero instance with PostgreSQL and Redis
- Python 3.11+
- [uv](https://github.com/astral-sh/uv) (recommended) or pip

## Quick start

```bash
# Clone and run (uv handles dependencies automatically)
git clone https://github.com/JasonBates/llm-dashboard.git
cd llm-dashboard
uv run server.py
```

Open [http://127.0.0.1:8765](http://127.0.0.1:8765).

## Configuration

The server reads connection details from environment variables with sensible defaults for local development:

| Variable | Default | Description |
|----------|---------|-------------|
| `DATABASE_URL` | `postgresql://postgres:password@localhost:5433/inboxzero` | PostgreSQL connection string |
| `REDIS_HOST` | `127.0.0.1` | Redis host |
| `REDIS_PORT` | `6380` | Redis port |

You can also pass flags directly:

```bash
uv run server.py --port 8080 --db 'postgresql://user:pass@host:5432/dbname'
```

## Filters

The dashboard UI provides interactive filters:

- **Exclude accounts** — click account chips to hide specific email accounts
- **Skip setup period** — ignore the first N hours of each account (to filter out initial bulk processing)
- **Since date** — only show data from a specific date onward

These are passed as query params to the API and work across all endpoints:

```
?exclude_accounts=user@example.com,other@example.com
?since=2025-01-01
?skip_setup_hours=24
```

## Architecture

Three files, no build step:

- `server.py` — Python HTTP server with JSON API endpoints that query PostgreSQL and Redis
- `index.html` — Single-page dashboard using Tailwind CSS and Chart.js (loaded from CDN)
- `LLM Dashboard.command` — macOS double-click launcher

The server auto-refreshes data every 5 minutes. All charts are interactive with tooltips.

## API endpoints

| Endpoint | Description |
|----------|-------------|
| `/api/llm-efficiency` | Daily AI calls vs. pattern/preset matches |
| `/api/ai-calls-by-type` | AI calls broken down by rule system type |
| `/api/pattern-growth` | Cumulative learned patterns by source |
| `/api/rules-by-account` | Daily rule executions per email account |
| `/api/action-distribution` | Counts of each automated action type |
| `/api/ai-calls-per-rule-type` | AI vs. pattern matches per rule category |
| `/api/signal-noise` | Daily signal vs. noise email counts |
| `/api/signal-noise-detail` | Noise breakdown by category |
| `/api/estimated-cost` | Daily AI call counts for cost estimation |
| `/api/accounts` | List of email accounts |
| `/api/redis-usage` | Per-account token usage and cost from Redis |
