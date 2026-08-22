#!/usr/bin/env python3
"""Produce the backlog adjudication sheet for pattern-guard candidates.

WHY
---
Two cheap rules (role-address-is-not-cold-email; splits-on-List-Unsubscribe) narrow
~1,000 archiving patterns to a couple of dozen candidates. Neither the rules nor an
LLM judge can be trusted yet: on a blind run the model called `membership@examine.com`
"fine", eleven days after Jason had personally excluded it. Precision is unmeasured
in both directions.

The only fix is ground truth, and organic corrections arrive about four times a year.
So this generates a review sheet: every candidate, what we know about it, and the
model's verdict, with a keep/drop line for Jason. His answers become the labelled set
that the rules and the model can finally be scored against.

The model is polled three times per run because a single pass is not stable — two
earlier runs disagreed on a third of the senders. The vote split is shown so a 2-1
can be read as "uncertain" rather than a verdict.

USAGE
  ./pattern-guard-review.py                 # print the sheet
  ./pattern-guard-review.py --out FILE.md   # write it
"""

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.request
from collections import Counter
from pathlib import Path

os.environ["PATH"] = "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin"

CONTAINER = "inbox-zero-services-db-1"
ALIX_ENV = Path.home() / "Code/repos/alix/.env"
MODEL = "gpt-5.6-luna"
VOTES = 3

ROLE_LOCALPART = re.compile(
    r"(no-?reply|do-?not-?reply|donotreply|notification|alert|billing|invoice"
    r"|receipt|statement|support|service|shipment|shipping|tracking|delivery"
    r"|order|account|security|verify|auto|system|mailer|postmaster|admin)",
    re.I,
)
NEVER_SETS_HEADER = 0.8


def psql(sql: str) -> list[dict]:
    wrapped = f"SELECT COALESCE(json_agg(t), '[]'::json) FROM ({sql}) t;"
    p = subprocess.run(
        ["docker", "exec", CONTAINER, "psql", "-U", "postgres", "-d", "inboxzero", "-tAc", wrapped],
        capture_output=True, text=True,
    )
    if p.returncode != 0:
        raise SystemExit(f"psql failed: {p.stderr.strip()}")
    return json.loads(p.stdout.strip() or "[]")


# exclude=true means Jason has already corrected this sender; it is settled, not a
# candidate. Those become the calibration set instead.
SQL = """
  WITH archiving AS (
    SELECT gi.value AS sender, bool_or(gi.exclude) AS corrected,
           ea.id AS acct, ea.email AS account, g.name AS grp
    FROM "GroupItem" gi
    JOIN "Group" g         ON gi."groupId" = g.id
    JOIN "EmailAccount" ea ON g."emailAccountId" = ea.id
    LEFT JOIN "Rule" r     ON r."groupId" = g.id
    LEFT JOIN "Action" a   ON a."ruleId" = r.id
    WHERE gi.type = 'FROM'
    GROUP BY gi.value, ea.id, ea.email, g.name
    HAVING string_agg(DISTINCT a.type::text, ',') ~ '(ARCHIVE|MARK_SPAM)'
  )
  SELECT ar.sender, ar.account, ar.grp, ar.corrected,
         count(em.*)                                   AS msgs,
         count(em.*) - count(em."unsubscribeLink")     AS no_unsub,
         sum(CASE WHEN NOT em.inbox THEN 1 ELSE 0 END) AS archived,
         min(em.date)::date::text                      AS first_seen,
         max(em.date)::date::text                      AS last_seen,
         string_agg(DISTINCT coalesce(em."fromName",''), ' | ') AS names
  FROM archiving ar
  LEFT JOIN "EmailMessage" em ON em."from" = ar.sender AND em."emailAccountId" = ar.acct
  GROUP BY 1,2,3,4
"""


def rule_for(r: dict) -> str | None:
    local = r["sender"].split("@")[0]
    if r["grp"] == "Cold Email" and ROLE_LOCALPART.search(local):
        return "A"
    msgs, no_unsub = r["msgs"] or 0, r["no_unsub"] or 0
    if msgs >= 3 and no_unsub > 0 and (no_unsub / msgs) <= NEVER_SETS_HEADER:
        return "B"
    return None


def api_key() -> str:
    for line in ALIX_ENV.read_text().splitlines():
        if line.startswith("OPENAI_API_KEY="):
            return line.split("=", 1)[1].strip().strip("\"'")
    raise SystemExit("no OPENAI_API_KEY in alix/.env")


PROMPT = """Jason runs an automatic email filter across five accounts. When it decides a sender is bulk mail, it archives every future email from that address and marks it read, so he never sees any of it.

He is UK-based, works at 11FS (fintech), runs jasonbates.com and b8s.cc, plays pickleball, and writes a book. He does not read archived mail.

For each sender: if EVERY future email from this address were archived and marked read automatically, would Jason miss something he actually needs?

"miss" — the address carries anything consequential: deliveries, orders, invoices, billing, renewals, security or login codes, account or service notices, appointments, travel, legal or government mail, documents shared by a real person, or club/community mail he has joined.
"fine" — only bulk marketing, newsletters, promotional mail, platform noise, or unsolicited sales outreach from strangers.
"unsure" — better than a guess.

Note that many organisations send BOTH marketing and transactional mail from one address. Judge whether the address ever carries the consequential kind, not what most of its volume is.

Return ONLY a JSON array, no prose:
[{"n": <number>, "verdict": "miss"|"fine"|"unsure", "why": "<max 12 words>"}]

SENDERS:
"""


def poll(lines: list[str], key: str) -> dict[int, dict]:
    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=json.dumps({
            "model": MODEL,
            "messages": [{"role": "user", "content": PROMPT + "\n".join(lines)}],
            "max_completion_tokens": 30000,
        }).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
    )
    with urllib.request.urlopen(req, timeout=600) as resp:
        body = json.loads(resp.read())
    text = body["choices"][0]["message"].get("content") or ""
    m = re.search(r"\[.*\]", text, re.S)
    if not m:
        return {}
    return {v["n"]: v for v in json.loads(m.group(0)) if isinstance(v.get("n"), int)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", help="write the sheet to this path")
    args = ap.parse_args()

    rows = psql(SQL)
    candidates = [r for r in rows if rule_for(r) and not r["corrected"]]
    corrected = [r for r in rows if r["corrected"]]
    candidates.sort(key=lambda r: -(r["archived"] or 0))

    lines = [
        f'{i}. {r["sender"]}  names=[{" | ".join(sorted({n for n in (r["names"] or "").split(" | ") if n})) or "none"}]  '
        f'messages={r["msgs"] or 0}  span={r["first_seen"] or "?"}..{r["last_seen"] or "?"}  filed_as={r["grp"]}'
        for i, r in enumerate(candidates)
    ]

    key = api_key()
    polls = [poll(lines, key) for _ in range(VOTES)]

    out = []
    out.append("# Inbox Zero — Filter Audit: backlog adjudication\n")
    out.append(
        f"{len(candidates)} senders where a cheap rule says the automatic filing decision "
        f"might be wrong, out of ~1,000 patterns that archive.\n"
    )
    out.append(
        "Neither the rules nor the model is trustworthy yet. On a blind run the model called "
        "`membership@examine.com` *fine*, eleven days after you had personally excluded it. "
        "Your answers below are the ground truth both get scored against.\n"
    )
    out.append(
        "**Mark each row `keep` (archiving is right) or `drop` (I should be seeing this).** "
        f"The model was polled {VOTES}× — a split vote means it was unsure.\n"
    )
    def row(i: int, r: dict) -> str:
        votes = Counter(p[i]["verdict"] for p in polls if i in p)
        top = votes.most_common(1)[0][0] if votes else "?"
        split = "/".join(f"{v[0]}{c}" for v, c in votes.most_common()) or "?"
        why = next((p[i].get("why", "") for p in polls if i in p), "")
        names = " / ".join(sorted({n for n in (r["names"] or "").split(" | ") if n})) or "—"
        return (
            # Label by domain, not local part: jason@11fs.com and jason@elevenfs.com
            # both render as "jason" otherwise, and the same sender can be patterned
            # separately on several accounts.
            f"| ☐ | `{r['sender']}` | {r['account'].split('@')[1].split('.')[0]} | {r['grp']} | "
            f"{r['msgs'] or 0} | {r['archived'] or 0} | {names[:44]} | {rule_for(r)} | "
            f"**{top}** ({split}) | {why} |"
        )

    header = (
        "| ? | Sender | Account | Filed as | Msgs | Archived | Display names | Rule | Model | Why |\n"
        "|---|---|---|---|---:|---:|---|---|---|---|"
    )

    # Senders with no stored message history can't be judged on evidence — the model
    # is reasoning from the address alone. Kept separate so they don't pollute scoring.
    with_data = [(i, r) for i, r in enumerate(candidates) if (r["msgs"] or 0) > 0]
    no_data = [(i, r) for i, r in enumerate(candidates) if not (r["msgs"] or 0)]

    out.append(header)
    for i, r in with_data:
        out.append(row(i, r))

    if no_data:
        out.append(f"\n## No message history ({len(no_data)}) — judge on the address or skip\n")
        out.append(
            "`EmailMessage` is only populated for accounts whose stats page has been opened in "
            "the web UI, so there is no stored mail for these. The model is reasoning from the "
            "address alone, which is why most come back `unsure`. Excluded from scoring.\n"
        )
        out.append(header)
        for i, r in no_data:
            out.append(row(i, r))

    out.append("\n## Already settled — calibration\n")
    out.append("Senders you have corrected yourself. Any judge worth trusting should call these `drop`.\n")
    for r in corrected:
        out.append(f"- `{r['sender']}` — was filed {r['grp']}, {r['archived'] or 0} archived, you excluded it")

    out.append("\n## Scoring, once this is filled in\n")
    out.append("- Rule precision = `drop` rows ÷ all rows")
    out.append("- Model precision = rows where the model's majority verdict matches your mark")
    out.append("- A rule that scores well alone needs no LLM; if only the pair scores well, the LLM earns its call")

    text = "\n".join(out) + "\n"
    if args.out:
        Path(args.out).write_text(text)
        print(f"wrote {args.out} ({len(candidates)} candidates, {len(corrected)} calibration)")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
