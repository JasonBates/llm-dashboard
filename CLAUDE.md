# LLM Dashboard — Claude Code Instructions

## What this is

A single-page dashboard for monitoring a self-hosted [Inbox Zero](https://github.com/elie222/inbox-zero) instance. It tracks AI call efficiency, pattern learning, cost, and email signal-vs-noise metrics.

## Architecture

Three files, no build step:

- `server.py` — Python HTTP server (stdlib `http.server`) with JSON API endpoints querying PostgreSQL and Redis
- `index.html` — Single-page dashboard using Tailwind CSS and Chart.js (both loaded from CDN)
- `LLM Dashboard.command` — macOS double-click launcher (optional)

## Setup

### Prerequisites

- Python 3.11+
- [uv](https://github.com/astral-sh/uv) — handles dependencies automatically via inline script metadata
- A running Inbox Zero PostgreSQL database
- A running Inbox Zero Redis instance

### Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `DATABASE_URL` | `postgresql://postgres:password@localhost:5433/inboxzero` | PostgreSQL connection string |
| `REDIS_HOST` | `127.0.0.1` | Redis host |
| `REDIS_PORT` | `6380` | Redis port |

### Start the server

```bash
uv run server.py --port 8765
```

No `pip install` or `venv` needed — uv reads dependencies from the inline `# /// script` block in `server.py` and installs them automatically.

### Verify it's working

1. `curl http://127.0.0.1:8765/api/accounts` — should return JSON with email accounts from the database
2. Open `http://127.0.0.1:8765` in a browser — the dashboard auto-fetches all endpoints on load

## Key concepts

- **Tiers**: Inbox Zero processes emails in tiers. Tier 1 (patterns/presets) is free. Tier 3 (AI/LLM calls) costs money. The goal is to maximize pattern matches and minimize AI calls.
- **matchMetadata**: The `ExecutedRule.matchMetadata` JSON field indicates how a rule was matched — `AI`, `LEARNED_PATTERN`, or `PRESET`.
- **COST_PER_CALL**: Estimated at $0.014 per AI call (Sonnet pricing ~$3/1M input, ~$15/1M output).
- **Signal vs Noise**: Marketing, newsletters, cold email, and notifications are "noise". Everything else is "signal".

## Database schema (relevant tables)

- `ExecutedRule` — every rule execution, with `matchMetadata`, `emailAccountId`, `ruleId`, `messageId`, `threadId`, `createdAt`
- `Rule` — rule definitions with `systemType` (MARKETING, NEWSLETTER, COLD_EMAIL, etc.)
- `EmailAccount` — email accounts with `id`, `email`, `createdAt`
- `EmailMessage` — partial email sync with `fromDomain`, `messageId`, `threadId` (not all emails are stored)
- `GroupItem` — learned patterns with `source` and `createdAt`
- `ExecutedAction` — actions taken (archive, label, draft, etc.) linked to `ExecutedRule`

## Adding a new chart

### 1. Add the SQL endpoint in `server.py`

Add an entry to the `QUERY_DEFS` dict:

```python
"my-new-endpoint": {
    "sql": """
        SELECT date_trunc('day', er."createdAt")::date AS day,
               count(*) AS cnt
        FROM "ExecutedRule" er
        JOIN "EmailAccount" ea ON er."emailAccountId" = ea.id
        GROUP BY 1
        ORDER BY 1
    """,
    "has_account_join": True,   # True if query JOINs EmailAccount (enables account exclusion filter)
    "date_col": 'er."createdAt"',  # Column used for "since" date filter, or None
},
```

The server automatically handles filter injection (WHERE clauses for `exclude_accounts`, `since`, `skip_setup_hours`) — just write the base query without WHERE.

### 2. Add the chart container in `index.html`

Add a `<canvas>` inside the appropriate row section:

```html
<div class="bg-white border border-gray-200 rounded-xl p-5">
  <h2 class="text-sm font-semibold text-gray-900">Chart Title</h2>
  <p class="text-xs text-gray-500 mb-3">Brief description</p>
  <div class="h-64"><canvas id="chart-my-new"></canvas></div>
</div>
```

### 3. Add the render function in `index.html`

Follow the existing pattern — fetch data, destroy previous chart instance, create new Chart.js chart:

```javascript
async function renderMyNew() {
  const rows = await fetchData('my-new-endpoint');
  if (!rows.length) return;
  if (charts.myNew) charts.myNew.destroy();

  charts.myNew = new Chart(document.getElementById('chart-my-new'), {
    type: 'line',  // or 'bar', 'doughnut', 'bubble', etc.
    data: { labels: rows.map(r => r.day), datasets: [/* ... */] },
    options: {
      responsive: true, maintainAspectRatio: false,
      scales: { x: { grid: noGrid }, y: { grid: gridOpts, beginAtZero: true } },
    },
  });
}
```

### 4. Call it from `refreshAll()`

Add your render function to `refreshAll()` so it runs on load and every 5 minutes.

### Conventions

- Color palette: use the `C` object (e.g., `C.primary500`, `C.success500`, `C.warning500`, `C.error500`) for consistency
- Grid options: use `noGrid` for x-axis, `gridOpts` for y-axis
- Rolling averages: use the `rollingAvg(arr, window)` helper for smoothed overlays
- Chart instances: store in the `charts` object so they're properly destroyed on refresh
- `fetchData(endpoint)` handles filter params automatically — just pass the endpoint name

## Development notes

- All chart rendering is client-side in `index.html` — no server-side templating
- The server serves static files from the working directory for anything not under `/api/`
- Data auto-refreshes every 5 minutes (configurable via `REFRESH_MS` in `index.html`)
- Filters (exclude accounts, since date, skip setup hours) are passed as query params to all `/api/` endpoints
- When joining `ExecutedRule` to `EmailMessage`, use `DISTINCT ON (er.id)` to avoid row duplication from the OR join on `messageId`/`threadId`
