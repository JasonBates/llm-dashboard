#!/usr/bin/env python3
"""Tiny HTTP server that queries PostgreSQL and serves dashboard data as JSON.

Usage:
    uv run server.py                          # defaults
    uv run server.py --port 8765              # custom port
    uv run server.py --db 'postgresql://...'  # custom DB URL

Serves:
    GET /api/rules-by-day     — executed rules by day and systemType
    GET /api/cold-email-trend — cold email pattern growth over time
    GET /api/accounts         — email accounts
    GET /api/group-items      — learned patterns by source and day
    GET /                     — redirects to index.html
    GET /index.html           — the dashboard
"""
# /// script
# requires-python = ">=3.11"
# dependencies = ["psycopg2-binary"]
# ///

import argparse
import json
import os
from http.server import HTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

import psycopg2
import psycopg2.extras

DB_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://postgres:password@localhost:5433/inboxzero",
)

QUERIES = {
    "rules-by-day": """
        SELECT date_trunc('day', er."createdAt")::date AS day,
               COALESCE(r."systemType"::text, 'OTHER') AS system_type,
               count(*) AS cnt
        FROM "ExecutedRule" er
        LEFT JOIN "Rule" r ON er."ruleId" = r.id
        GROUP BY 1, 2
        ORDER BY 1, 2
    """,
    "cold-email-trend": """
        SELECT date_trunc('day', "createdAt")::date AS day,
               source::text AS source,
               count(*) AS cnt
        FROM "GroupItem"
        GROUP BY 1, 2
        ORDER BY 1, 2
    """,
    "accounts": """
        SELECT id, email FROM "EmailAccount" ORDER BY email
    """,
    "group-items": """
        SELECT day, source, new_patterns,
               sum(new_patterns) OVER (PARTITION BY source ORDER BY day) AS cumulative
        FROM (
            SELECT date_trunc('day', "createdAt")::date AS day,
                   source::text AS source,
                   count(*) AS new_patterns
            FROM "GroupItem"
            GROUP BY 1, 2
        ) sub
        ORDER BY day, source
    """,
    "rules-by-account": """
        SELECT date_trunc('day', er."createdAt")::date AS day,
               ea.email,
               COALESCE(r."systemType"::text, 'OTHER') AS system_type,
               count(*) AS cnt
        FROM "ExecutedRule" er
        LEFT JOIN "Rule" r ON er."ruleId" = r.id
        JOIN "EmailAccount" ea ON er."emailAccountId" = ea.id
        GROUP BY 1, 2, 3
        ORDER BY 1, 2, 3
    """,
    "cold-email-checks-by-day": """
        SELECT date_trunc('day', er."createdAt")::date AS day,
               count(*) AS total_rules,
               count(*) FILTER (WHERE r."systemType"::text = 'COLD_EMAIL') AS cold_email_rules,
               count(*) FILTER (WHERE r."systemType"::text != 'COLD_EMAIL' OR r."systemType" IS NULL) AS other_rules
        FROM "ExecutedRule" er
        LEFT JOIN "Rule" r ON er."ruleId" = r.id
        GROUP BY 1
        ORDER BY 1
    """,
}


class DashboardHandler(SimpleHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        if path.startswith("/api/"):
            endpoint = path[5:]  # strip /api/
            if endpoint in QUERIES:
                self._serve_query(endpoint)
            else:
                self._json_response(404, {"error": f"Unknown endpoint: {endpoint}"})
        elif path in ("", "/index.html"):
            # Serve the HTML file
            super().do_GET()
        else:
            super().do_GET()

    def _serve_query(self, endpoint):
        try:
            conn = psycopg2.connect(DB_URL)
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(QUERIES[endpoint])
            rows = cur.fetchall()
            cur.close()
            conn.close()

            # Convert non-serializable types
            from decimal import Decimal
            for row in rows:
                for k, v in row.items():
                    if hasattr(v, "isoformat"):
                        row[k] = v.isoformat()
                    elif isinstance(v, Decimal):
                        row[k] = float(v)

            self._json_response(200, {"data": rows, "endpoint": endpoint})
        except Exception as e:
            self._json_response(500, {"error": str(e)})

    def _json_response(self, status, data):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        # Quieter logging
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
