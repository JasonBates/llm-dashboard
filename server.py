#!/usr/bin/env python3
"""Tiny HTTP server that queries PostgreSQL and serves dashboard data as JSON.

Usage:
    uv run server.py                          # defaults
    uv run server.py --port 8765              # custom port
    uv run server.py --db 'postgresql://...'  # custom DB URL

Supports query params on all endpoints:
    ?exclude_accounts=user@example.com,other@example.com
    ?since=2025-01-01
"""
# /// script
# requires-python = ">=3.11"
# dependencies = ["psycopg2-binary", "redis"]
# ///

import argparse
import json
import os
from decimal import Decimal
from http.server import HTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

import psycopg2
import psycopg2.extras
import redis as redis_lib

DB_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://postgres:password@localhost:5433/inboxzero",
)

REDIS_HOST = os.environ.get("REDIS_HOST", "127.0.0.1")
REDIS_PORT = int(os.environ.get("REDIS_PORT", "6380"))

# Each query is a function that returns (sql, params) given filter args.
# Filters: exclude_accounts (list of emails), since (date string).

def _account_filter(table_prefix="ea", param_offset=0):
    """Returns (sql_fragment, param_count) for account exclusion."""
    return f'{table_prefix}.email != ALL(%s)', 1

def _date_filter(date_col, param_offset=0):
    return f'{date_col} >= %s::date', 1


def build_query(base_sql, filters, *, has_account_join=True, date_col='er."createdAt"'):
    """Append WHERE clauses for exclude_accounts and since filters."""
    clauses = []
    params = []

    if filters.get("exclude_accounts"):
        if has_account_join:
            clauses.append('ea.email != ALL(%s)')
            params.append(filters["exclude_accounts"])
        # If no account join, skip account filtering
    if filters.get("since"):
        clauses.append(f'{date_col} >= %s::date')
        params.append(filters["since"])

    if clauses:
        where = " AND ".join(clauses)
        # Insert WHERE before GROUP BY or ORDER BY
        for keyword in ("GROUP BY", "ORDER BY", "LIMIT"):
            idx = base_sql.upper().find(keyword)
            if idx != -1:
                base_sql = base_sql[:idx] + f"WHERE {where}\n        " + base_sql[idx:]
                break
        else:
            base_sql += f"\n        WHERE {where}"

    return base_sql, params


QUERY_DEFS = {
    "llm-efficiency": {
        "sql": """
            SELECT
              date_trunc('day', er."createdAt")::date AS day,
              count(*) FILTER (WHERE er."matchMetadata"::text LIKE '%%AI%%'
                               AND er."matchMetadata"::text NOT LIKE '%%LEARNED_PATTERN%%'
                               AND er."matchMetadata"::text NOT LIKE '%%PRESET%%') AS ai_calls,
              count(*) FILTER (WHERE er."matchMetadata"::text LIKE '%%LEARNED_PATTERN%%') AS pattern_matches,
              count(*) FILTER (WHERE er."matchMetadata"::text LIKE '%%PRESET%%'
                               AND er."matchMetadata"::text NOT LIKE '%%AI%%') AS preset_matches,
              count(*) AS total
            FROM "ExecutedRule" er
            JOIN "EmailAccount" ea ON er."emailAccountId" = ea.id
            GROUP BY 1
            ORDER BY 1
        """,
        "has_account_join": True,
        "date_col": 'er."createdAt"',
    },
    "ai-calls-by-type": {
        "sql": """
            SELECT
              date_trunc('day', er."createdAt")::date AS day,
              COALESCE(r."systemType"::text, 'OTHER') AS system_type,
              CASE
                WHEN er."matchMetadata"::text LIKE '%%LEARNED_PATTERN%%' THEN 'PATTERN'
                WHEN er."matchMetadata"::text LIKE '%%AI%%' THEN 'AI'
                ELSE 'OTHER'
              END AS match_type,
              count(*) AS cnt
            FROM "ExecutedRule" er
            LEFT JOIN "Rule" r ON er."ruleId" = r.id
            JOIN "EmailAccount" ea ON er."emailAccountId" = ea.id
            GROUP BY 1, 2, 3
            ORDER BY 1, 2, 3
        """,
        "has_account_join": True,
        "date_col": 'er."createdAt"',
    },
    "pattern-growth": {
        "sql": """
            SELECT day, source, new_patterns,
                   sum(new_patterns) OVER (PARTITION BY source ORDER BY day) AS cumulative
            FROM (
                SELECT date_trunc('day', gi."createdAt")::date AS day,
                       gi.source::text AS source,
                       count(*) AS new_patterns
                FROM "GroupItem" gi
                GROUP BY 1, 2
            ) sub
            ORDER BY day, source
        """,
        "has_account_join": False,
        "date_col": 'gi."createdAt"',
    },
    "rules-by-account": {
        "sql": """
            SELECT date_trunc('day', er."createdAt")::date AS day,
                   ea.email,
                   count(*) AS cnt
            FROM "ExecutedRule" er
            JOIN "EmailAccount" ea ON er."emailAccountId" = ea.id
            GROUP BY 1, 2
            ORDER BY 1, 2
        """,
        "has_account_join": True,
        "date_col": 'er."createdAt"',
    },
    "action-distribution": {
        "sql": """
            SELECT
              ea2.type::text AS action_type,
              count(*) AS cnt
            FROM "ExecutedAction" ea2
            JOIN "ExecutedRule" er ON ea2."executedRuleId" = er.id
            JOIN "EmailAccount" ea ON er."emailAccountId" = ea.id
            GROUP BY 1
            ORDER BY 2 DESC
        """,
        "has_account_join": True,
        "date_col": 'er."createdAt"',
    },
    "ai-calls-per-rule-type": {
        "sql": """
            SELECT
              COALESCE(r."systemType"::text, 'OTHER') AS system_type,
              count(*) FILTER (WHERE er."matchMetadata"::text LIKE '%%AI%%'
                               AND er."matchMetadata"::text NOT LIKE '%%LEARNED_PATTERN%%') AS ai_calls,
              count(*) FILTER (WHERE er."matchMetadata"::text LIKE '%%LEARNED_PATTERN%%') AS pattern_matches,
              count(*) AS total
            FROM "ExecutedRule" er
            LEFT JOIN "Rule" r ON er."ruleId" = r.id
            JOIN "EmailAccount" ea ON er."emailAccountId" = ea.id
            GROUP BY 1
            ORDER BY 4 DESC
        """,
        "has_account_join": True,
        "date_col": 'er."createdAt"',
    },
    "accounts": {
        "sql": """
            SELECT id, email FROM "EmailAccount" ORDER BY email
        """,
        "has_account_join": False,
        "date_col": None,
    },
    "signal-noise": {
        "sql": """
            SELECT
              date_trunc('day', er."createdAt")::date AS day,
              count(*) FILTER (WHERE COALESCE(r."systemType"::text, 'CUSTOM')
                  IN ('MARKETING', 'COLD_EMAIL')) AS noise,
              count(*) FILTER (WHERE COALESCE(r."systemType"::text, 'CUSTOM')
                  NOT IN ('MARKETING', 'COLD_EMAIL')) AS signal,
              count(*) AS total
            FROM "ExecutedRule" er
            LEFT JOIN "Rule" r ON er."ruleId" = r.id
            JOIN "EmailAccount" ea ON er."emailAccountId" = ea.id
            GROUP BY 1
            ORDER BY 1
        """,
        "has_account_join": True,
        "date_col": 'er."createdAt"',
    },
    "signal-noise-detail": {
        "sql": """
            SELECT
              COALESCE(r."systemType"::text, 'CUSTOM') AS system_type,
              CASE
                WHEN COALESCE(r."systemType"::text, 'CUSTOM')
                  IN ('MARKETING', 'COLD_EMAIL') THEN 'NOISE'
                ELSE 'SIGNAL'
              END AS category,
              count(*) AS cnt
            FROM "ExecutedRule" er
            LEFT JOIN "Rule" r ON er."ruleId" = r.id
            JOIN "EmailAccount" ea ON er."emailAccountId" = ea.id
            GROUP BY 1, 2
            ORDER BY 3 DESC
        """,
        "has_account_join": True,
        "date_col": 'er."createdAt"',
    },
    "estimated-cost": {
        "sql": """
            SELECT
              date_trunc('day', er."createdAt")::date AS day,
              count(*) FILTER (WHERE er."matchMetadata"::text LIKE '%%AI%%'
                               AND er."matchMetadata"::text NOT LIKE '%%LEARNED_PATTERN%%'
                               AND er."matchMetadata"::text NOT LIKE '%%PRESET%%') AS ai_calls,
              count(*) AS total
            FROM "ExecutedRule" er
            JOIN "EmailAccount" ea ON er."emailAccountId" = ea.id
            GROUP BY 1
            ORDER BY 1
        """,
        "has_account_join": True,
        "date_col": 'er."createdAt"',
    },
    "top-ai-senders": {
        "sql": """
            SELECT domain, sum(ai_calls)::int AS ai_calls,
                   sum(pattern_matches)::int AS pattern_matches, sum(total)::int AS total
            FROM (
                SELECT DISTINCT ON (er.id)
                  COALESCE(em."fromDomain", 'unknown') AS domain,
                  CASE WHEN er."matchMetadata"::text LIKE '%%AI%%'
                       AND er."matchMetadata"::text NOT LIKE '%%LEARNED_PATTERN%%'
                       THEN 1 ELSE 0 END AS ai_calls,
                  CASE WHEN er."matchMetadata"::text LIKE '%%LEARNED_PATTERN%%'
                       THEN 1 ELSE 0 END AS pattern_matches,
                  1 AS total,
                  ea.email AS account_email
                FROM "ExecutedRule" er
                JOIN "EmailAccount" ea ON er."emailAccountId" = ea.id
                LEFT JOIN "EmailMessage" em ON er."emailAccountId" = em."emailAccountId"
                    AND (er."messageId" = em."messageId" OR er."threadId" = em."threadId")
                ORDER BY er.id, (em."messageId" = er."messageId") DESC NULLS LAST
            ) sub
            GROUP BY 1
            ORDER BY 2 DESC
            LIMIT 15
        """,
        "has_account_join": True,
        "date_col": 'er."createdAt"',
    },
}


# Maps raw Redis usage-hash field names to the normalized keys the frontend reads.
USAGE_FIELD_MAP = {
    "openaiCalls": "openaiCalls",
    "openaiTokensUsed": "totalTokens",
    "openaiCompletionTokensUsed": "outputTokens",
    "openaiPromptTokensUsed": "inputTokens",
    "cachedInputTokensUsed": "cachedInputTokens",
    "reasoningTokensUsed": "reasoningTokens",
    "cost": "cost",
}
USAGE_KEYS = tuple(USAGE_FIELD_MAP.values())


def _load_account_lookups():
    """Build id→email maps for EmailAccount and User from Postgres."""
    conn = psycopg2.connect(DB_URL)
    cur = conn.cursor()
    cur.execute('SELECT id, email FROM "EmailAccount"')
    email_accounts = {row[0]: row[1] for row in cur.fetchall()}
    cur.execute('SELECT id, email FROM "User"')
    users = {row[0]: row[1] for row in cur.fetchall()}
    cur.close()
    conn.close()
    return email_accounts, users


def _normalize_usage(data):
    """Convert a raw Redis usage hash to the dashboard's normalized fields."""
    data = data or {}
    return {
        key: (float(data.get(raw, 0) or 0) if key == "cost"
              else int(data.get(raw, 0) or 0))
        for raw, key in USAGE_FIELD_MAP.items()
    }


def _add_usage(into, other):
    for key in USAGE_KEYS:
        into[key] += other[key]


def _parse_migration_snapshot(raw):
    """Return the legacy usage snapshot from a migration done-state, or None."""
    if not raw:
        return None
    try:
        return json.loads(raw).get("usage")
    except (ValueError, TypeError):
        return None


def _accumulate(by_email, email, usage):
    entry = by_email.get(email)
    if entry is None:
        by_email[email] = dict(usage)
    else:
        _add_usage(entry, usage)


def fetch_redis_usage():
    """Fetch per-email-account AI usage from Redis, resolving stored IDs to emails.

    inbox-zero writes usage under three key shapes:
      usage:email-account:<id>  — current per-account usage
      usage:user:<id>           — per-user rollup (sum across the user's accounts)
      usage:<email>             — legacy per-account usage (pre-migration, frozen)

    Resolution + merge mirrors inbox-zero's own getUsage so the totals match:
      • email-account IDs resolve to their email via Postgres.
      • Legacy email-keyed usage is combined into the same account. inbox-zero
        lazily folds legacy usage into the email-account key on read and records a
        `usage-migration:...:done` snapshot. So: if a done-state exists the legacy
        total is already in the email-account key (add only any un-folded delta);
        otherwise legacy and email-account usage are disjoint and must be summed.
      • User rollup keys are skipped — they duplicate per-account totals and would
        inflate this "per email account" view.
    """
    email_accounts, _users = _load_account_lookups()

    r = redis_lib.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)
    ea_raw = {}      # email-account id -> raw usage hash
    legacy_raw = {}  # email -> raw usage hash
    cursor = "0"
    while True:
        cursor, keys = r.scan(cursor=cursor, match="usage:*", count=200)
        for key in keys:
            if key.startswith(("usage-weekly-cost:", "usage-migration:")):
                continue
            subject = key[len("usage:"):]
            if subject.startswith("user:"):
                continue  # per-user rollup; duplicates the per-account rows
            if subject.startswith("email-account:"):
                ea_raw[subject[len("email-account:"):]] = r.hgetall(key)
            else:
                legacy_raw[subject] = r.hgetall(key)  # legacy usage:<email>
        if cursor == 0:
            break

    by_email = {}
    for account_id, raw in ea_raw.items():
        email = email_accounts.get(account_id, f"email-account:{account_id}")
        total = _normalize_usage(raw)
        legacy = legacy_raw.pop(email, None)
        if legacy is not None:
            legacy_usage = _normalize_usage(legacy)
            migrated = _parse_migration_snapshot(
                r.get(f"usage-migration:usage-email-account:{account_id}:done")
            )
            if migrated is None:
                _add_usage(total, legacy_usage)  # unmigrated: legacy + email-account
            else:
                snapshot = _normalize_usage(migrated)
                _add_usage(total, {
                    key: max(0, legacy_usage[key] - snapshot[key])
                    for key in USAGE_KEYS
                })  # migrated: only legacy not yet folded into the key
        _accumulate(by_email, email, total)

    # Legacy keys with no surviving email-account (e.g. a deleted account).
    for email, raw in legacy_raw.items():
        _accumulate(by_email, email, _normalize_usage(raw))

    r.close()

    results = [{"email": email, **usage} for email, usage in by_email.items()]
    results.sort(key=lambda x: x["cost"], reverse=True)
    return results


class DashboardHandler(SimpleHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        if path.startswith("/api/"):
            endpoint = path[5:]
            if endpoint == "redis-usage":
                try:
                    data = fetch_redis_usage()
                    self._json_response(200, {"data": data, "endpoint": endpoint})
                except Exception as e:
                    self._json_response(500, {"error": str(e)})
                return
            if endpoint in QUERY_DEFS:
                qs = parse_qs(parsed.query)
                filters = {}
                if "exclude_accounts" in qs:
                    filters["exclude_accounts"] = qs["exclude_accounts"][0].split(",")
                if "since" in qs:
                    filters["since"] = qs["since"][0]
                if "skip_setup_hours" in qs:
                    filters["skip_setup_hours"] = int(qs["skip_setup_hours"][0])
                self._serve_query(endpoint, filters)
            else:
                self._json_response(404, {"error": f"Unknown endpoint: {endpoint}"})
        else:
            super().do_GET()

    def _serve_query(self, endpoint, filters):
        try:
            qdef = QUERY_DEFS[endpoint]
            sql = qdef["sql"]
            params = []

            # Apply filters
            clauses = []
            if filters.get("exclude_accounts") and qdef["has_account_join"]:
                clauses.append('ea.email != ALL(%s)')
                params.append(filters["exclude_accounts"])
            if filters.get("since") and qdef.get("date_col"):
                clauses.append(f'{qdef["date_col"]} >= %s::date')
                params.append(filters["since"])
            if filters.get("skip_setup_hours") and qdef["has_account_join"]:
                clauses.append('er."createdAt" >= ea."createdAt" + make_interval(hours => %s)')
                params.append(filters["skip_setup_hours"])

            if clauses:
                where_str = " AND ".join(clauses)
                # Find insertion point
                upper = sql.upper()
                for kw in ("GROUP BY", "ORDER BY", "LIMIT"):
                    idx = upper.find(kw)
                    if idx != -1:
                        sql = sql[:idx] + f"WHERE {where_str}\n            " + sql[idx:]
                        break
                else:
                    sql += f"\n            WHERE {where_str}"

            conn = psycopg2.connect(DB_URL)
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(sql, params)
            rows = cur.fetchall()
            cur.close()
            conn.close()

            for row in rows:
                for k, v in row.items():
                    if hasattr(v, "isoformat"):
                        row[k] = v.isoformat()
                    elif isinstance(v, Decimal):
                        row[k] = float(v)

            self._json_response(200, {"data": rows, "endpoint": endpoint})
        except Exception as e:
            self._json_response(500, {"error": str(e), "sql": sql if 'sql' in dir() else ""})

    def _json_response(self, status, data):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        if "/api/" in str(args[0]):
            return
        super().log_message(format, *args)


def main():
    global DB_URL

    parser = argparse.ArgumentParser(description="LLM Dashboard Server")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--db", type=str, default=DB_URL)
    args = parser.parse_args()

    DB_URL = args.db

    os.chdir(os.path.dirname(os.path.abspath(__file__)))

    server = HTTPServer(("127.0.0.1", args.port), DashboardHandler)
    print(f"Dashboard: http://127.0.0.1:{args.port}")
    print(f"Database:  {DB_URL.split('@')[1] if '@' in DB_URL else DB_URL}")
    print("Press Ctrl+C to stop")
    server.serve_forever()


if __name__ == "__main__":
    main()
