#!/usr/bin/env python3
"""Backtest two proposed pattern-creation guards against existing Inbox Zero data.

WHY
---
Inbox Zero learns a FROM pattern for a sender, after which every message from that
address is actioned with no content check. Two classes of sender get badly served by
that, and each has a cheap deterministic tell:

  Rule A — role addresses are never cold email.
    Cold email is a human sending unsolicited outreach because they want a reply.
    A no-reply/billing/shipment-tracking address cannot be cold outreach. Filing one
    under Cold Email is a category error, and Cold Email archives AND marks read.

  Rule B — senders whose mail splits on List-Unsubscribe are multiplexing.
    Bulk marketing carries List-Unsubscribe; transactional mail usually doesn't. A
    sender with both is using one address for two kinds of mail, so the address
    doesn't determine what any given message is. Senders that never set the header
    are excluded: that's a sender who doesn't use it, not one running two streams.

This script does not change anything. It asks, over data already on disk: which
existing patterns would each rule have prevented, how much mail would have stayed
visible, and what would it have cost in extra classification calls.

USAGE
  ./pattern-guard-backtest.py            # summary
  ./pattern-guard-backtest.py --detail   # list every sender each rule catches

CAVEAT
  EmailMessage is only populated for accounts whose stats page has been loaded in the
  web UI. On this instance it is current for one account and months stale for the
  others, so per-message figures are a floor, not a total. Pattern counts are complete.
"""

import argparse
import json
import os
import re
import subprocess
import sys

os.environ["PATH"] = "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin"

CONTAINER = "inbox-zero-services-db-1"
DB_USER = "postgres"
DB_NAME = "inboxzero"

# Local-parts that indicate an automated sending address. Substring match, not a
# prefix match: `drive-shares-dm-noreply@google.com` is the 34-message case on this
# instance and a prefix test misses it entirely.
ROLE_LOCALPART = re.compile(
    r"(no-?reply|do-?not-?reply|donotreply|notification|alert|billing|invoice"
    r"|receipt|statement|support|service|shipment|shipping|tracking|delivery"
    r"|order|account|security|verify|auto|system|mailer|postmaster|admin)",
    re.I,
)
# Deliberately NOT included: info@. Small businesses send genuine cold outreach from
# it, so it catches six real cold-email senders on this instance and buys nothing —
# unlike no-reply@, which cannot be outreach because it cannot receive a reply.

# Above this share of messages lacking List-Unsubscribe, the sender simply doesn't
# set the header rather than running two streams. One sender here is 50/52.
NEVER_SETS_HEADER = 0.8


def psql(sql: str) -> list[dict]:
    wrapped = f"SELECT COALESCE(json_agg(t), '[]'::json) FROM ({sql}) t;"
    proc = subprocess.run(
        ["docker", "exec", CONTAINER, "psql", "-U", DB_USER, "-d", DB_NAME, "-tAc", wrapped],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise SystemExit(f"psql failed: {proc.stderr.strip()}")
    return json.loads(proc.stdout.strip() or "[]")


PATTERNS_SQL = """
  SELECT gi.id,
         gi.value                AS sender,
         g.name                  AS group_name,
         ea.email                AS account,
         ea.id                   AS account_id,
         string_agg(DISTINCT a.type::text, ',' ORDER BY a.type::text) AS actions
  FROM "GroupItem" gi
  JOIN "Group" g         ON gi."groupId" = g.id
  JOIN "EmailAccount" ea ON g."emailAccountId" = ea.id
  LEFT JOIN "Rule" r     ON r."groupId" = g.id
  LEFT JOIN "Action" a   ON a."ruleId" = r.id
  WHERE gi.type = 'FROM'
  GROUP BY gi.id, gi.value, g.name, ea.email, ea.id
  HAVING string_agg(DISTINCT a.type::text, ',') ~ '(ARCHIVE|MARK_SPAM)'
"""

# Per sender+account: message counts, how many lacked List-Unsubscribe, how many
# were archived, and how many distinct display names each stream used. A sender
# whose streams use different display names ("Steam" vs "Steam Support") is
# corroborating evidence for Rule B.
MESSAGES_SQL = """
  SELECT em."from"           AS sender,
         em."emailAccountId" AS account_id,
         count(*)                                              AS msgs,
         count(*) - count(em."unsubscribeLink")                AS no_unsub,
         sum(CASE WHEN NOT em.inbox THEN 1 ELSE 0 END)         AS archived,
         sum(CASE WHEN em."unsubscribeLink" IS NULL AND NOT em.inbox THEN 1 ELSE 0 END) AS archived_no_unsub,
         count(DISTINCT em."fromName") FILTER (WHERE em."unsubscribeLink" IS NULL)     AS names_no_unsub,
         count(DISTINCT em."fromName") FILTER (WHERE em."unsubscribeLink" IS NOT NULL) AS names_unsub
  FROM "EmailMessage" em
  GROUP BY 1, 2
"""

# How many times each pattern actually fired. This is the cost side: every one of
# these would have been an LLM classification instead.
HITS_SQL = """
  SELECT em."from"           AS sender,
         er."emailAccountId" AS account_id,
         count(*)            AS hits
  FROM "ExecutedRule" er
  JOIN "EmailMessage" em
    ON em."messageId" = er."messageId" AND em."emailAccountId" = er."emailAccountId"
  WHERE er."matchMetadata"::text LIKE '%LEARNED_PATTERN%'
  GROUP BY 1, 2
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--detail", action="store_true", help="list every sender caught")
    args = ap.parse_args()

    patterns = psql(PATTERNS_SQL)
    msgs = {(r["sender"], r["account_id"]): r for r in psql(MESSAGES_SQL)}
    hits = {(r["sender"], r["account_id"]): r["hits"] for r in psql(HITS_SQL)}

    rule_a, rule_b = [], []

    for p in patterns:
        key = (p["sender"], p["account_id"])
        m = msgs.get(key)
        local = p["sender"].split("@")[0]

        # Rule A: a role address filed as cold email.
        if p["group_name"] == "Cold Email" and ROLE_LOCALPART.search(local + "@"):
            rule_a.append({**p, **(m or {}), "hits": hits.get(key, 0)})
            continue  # don't double-count under B

        # Rule B: needs message history to judge.
        if not m or m["msgs"] < 3:
            continue
        no_unsub, total = m["no_unsub"], m["msgs"]
        if 0 < no_unsub and (no_unsub / total) <= NEVER_SETS_HEADER:
            rule_b.append({**p, **m, "hits": hits.get(key, 0)})

    def report(title: str, rows: list[dict], rationale: str) -> None:
        archived = sum(r.get("archived_no_unsub") or r.get("archived") or 0 for r in rows)
        cost = sum(r.get("hits") or 0 for r in rows)
        print(f"\n{title}")
        print(f"  {rationale}")
        print(f"  patterns prevented ........ {len(rows)}")
        print(f"  messages kept visible ..... {archived}")
        print(f"  extra classifications ..... {cost}")
        if args.detail:
            for r in sorted(rows, key=lambda x: -(x.get("archived") or 0)):
                names = ""
                if r.get("names_no_unsub") and r.get("names_unsub"):
                    names = f"  names {r['names_no_unsub']}/{r['names_unsub']}"
                print(
                    f"    {r['sender']:<46} {r['group_name']:<11} "
                    f"msgs={r.get('msgs') or 0:<4} archived={r.get('archived') or 0:<4} "
                    f"hits={r.get('hits') or 0}{names}"
                )

    total_archiving = len(patterns)
    print(f"Archiving FROM patterns on this instance: {total_archiving}")

    report(
        "RULE A — role address filed as Cold Email",
        rule_a,
        "cold outreach comes from a human who wants a reply; no-reply@ cannot",
    )
    report(
        "RULE B — sender's mail splits on List-Unsubscribe",
        rule_b,
        "one address carrying both marketing and transactional mail",
    )

    caught = len(rule_a) + len(rule_b)
    cost = sum(r.get("hits") or 0 for r in rule_a + rule_b)
    print(
        f"\nCombined: {caught} of {total_archiving} patterns "
        f"({100 * caught / total_archiving:.1f}%) would not have been created, "
        f"at a cost of {cost} extra classification calls over the instance's lifetime."
    )
    print(
        "Messages kept visible is a floor: EmailMessage is only populated for accounts "
        "whose stats page has been loaded."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
