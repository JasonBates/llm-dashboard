# LLM Dashboard

A single-page dashboard for monitoring a self-hosted [Inbox Zero](https://github.com/elie222/inbox-zero) instance: AI call efficiency, pattern learning, cost, and email signal-vs-noise metrics. No build step: `server.py` (stdlib `http.server`, JSON API over Inbox Zero's PostgreSQL and Redis), `usage_metrics.py` (bounded Docker usage collection, local SQLite model counters), `index.html` (Tailwind + Chart.js from CDN), `LLM Dashboard.command` (double-click launcher).

## Commands

- Run: `uv run server.py --port 8765` (dependencies from the inline `# /// script` block; expects Inbox Zero's Postgres on localhost:5433 and Redis on 6380, overridable with `DATABASE_URL`, `REDIS_HOST`, `REDIS_PORT`). Check with `curl http://127.0.0.1:8765/api/accounts`.
- Tests: `uv run pytest -q` (offline suite).
- Shadow and reverse-audit commands and configuration: `README.md`.

## Key concepts

- **Tiers**: Inbox Zero processes emails in tiers. Tier 1 (patterns/presets) is free. Tier 3 (AI/LLM calls) costs money. The goal is to maximize pattern matches and minimize AI calls.
- **matchMetadata**: The `ExecutedRule.matchMetadata` JSON field indicates how a rule was matched — `AI`, `LEARNED_PATTERN`, or `PRESET`.
- **Costs**: Use logged `estimatedCost` for Jev, fallback and other models. Never multiply rule matches by a fixed Sonnet call price or present model estimates as provider invoices.
- **Signal vs Noise**: Marketing and cold email are noise. Count distinct account/message pairs per day, so multi-rule results do not inflate email counts.
- Relevant Inbox Zero tables: `ExecutedRule`, `Rule` (`systemType`), `EmailAccount`, `EmailMessage` (partial sync: not all emails are stored), `GroupItem` (learned patterns), `ExecutedAction`.

## Development notes

- Before adding a chart, read `docs/adding-a-chart.md` (`QUERY_DEFS` entry, canvas, render function, `refreshAll()`, palette and helper conventions). Write each query without WHERE: the server injects the account, since-date and setup-hours filters.
- All chart rendering is client-side in `index.html`; data auto-refreshes every 5 minutes (`REFRESH_MS`).
- When joining `ExecutedRule` to `EmailMessage`, use `DISTINCT ON (er.id)` to avoid row duplication from the OR join on `messageId`/`threadId`.

## Shadow evaluation (paused)

`shadow/` shadow-classifies every Inbox Zero rule execution with TypeSafe Jev and
records both models' decisions and costs in `data/shadow-truth.sqlite`
(captures, label checks, human verdicts; never regenerated) and
`data/shadow-derived.sqlite` (Jev judgments, incumbent per-call costs parsed from
the web container's logs, watermarks). `server.py` serves it under
`/api/shadow/*` and `index.html` renders the "Shadow: Jev vs incumbent" section
with the adjudication queue. The unattended loop runs from
`scripts/shadow-loop.sh` under the launchd agent
`com.jasonbates.inbox-zero-shadow` (template in `scripts/`), logging to
`/tmp/inbox-zero-shadow.log`. Commands and configuration are in `README.md`.
That launch agent is paused: its plist remains in `~/Library/LaunchAgents` but
it is unloaded (absent from `launchctl list`), so the shadow data is historical.

Constraints that must hold: Gmail access is GET-only and Postgres is opened
read-only; tokens are decrypted in memory and never written; `docker logs` is
read with `--tail`, never `--since` or a full read, because this container's
json-file log stops at a restart boundary for forward reads. The historical incumbent
classifier was Sonnet 4.6 (the `default` tier), not the economy model. Design
plan: `~/.claude/plans/inbox-zero-jev-shadow.md`.

## Native Jev usage

Native Jev is enabled in Inbox Zero, with the existing AI model as fallback. The
older shadow launch agent is paused; its data remains historical. The dashboard's
own server collects bounded Docker logs every minute while running, persisting
only model counters and IDs in `data/model-usage.sqlite`. There is no new launch
agent. `/api/model-usage` separates production, tests and unknown attribution,
and shows fallback only with explicit warning evidence. Never send new Jev
requests from this collector or write to Inbox Zero/Postgres to obtain metrics.
Keep missing costs, ambiguous attribution and log-coverage gaps visible. Run
`uv run pytest -q` for the offline suite; the double-click launcher starts the
dashboard with its usage collector.

## Native Jev reverse audit (complete)

`reverse_audit.py` ran the user-authorized temporary Sonnet review of native
Jev Marketing/Cold Email decisions; `/api/reverse-audit` and the top **Jev safety
review** panel show results. The audit is complete: `data/reverse-audit.log`
ends on 25 September 2026 with `running: false` (about $0.54 committed against
the $5 cap), and the launch agent `com.jasonbates.inbox-zero-reverse-audit` is
idle. It must not be re-initialised or extended, nor the historical shadow
classifier restarted, without authorization. Commands and
limitations are in README. Durable evidence and paid attempts live in
`data/reverse-audit-state.sqlite`; rebuildable comparisons in
`data/reverse-audit-derived.sqlite`. Never delete reservations to retry billed
requests. Only saved APPLIED native reasons with exclusively AI metadata qualify;
logs enrich confidence, and native deferrals count as fallback. Keep Jev's answer
hidden from the LLM reviewer; treat email content as untrusted input. Reviews
are advisory, not truth or authorization to relabel mail. The static HTTP handler
must not expose local files beyond index.html.
