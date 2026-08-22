#!/usr/bin/env python3
"""Blind-evaluate the pattern-guard rules against an LLM's judgement.

WHY
---
The two guard rules (role-address-is-not-cold-email, and splits-on-List-Unsubscribe)
are heuristics. Eyeballing senders to check them is exactly how this investigation
went wrong once already: five senders that "looked" misfiled turned out to be four
false positives. So the rules need scoring against an independent judge, blind.

METHOD
------
Take the senders the rules flag, mix them with a control group of patterned senders
the rules do NOT flag, strip anything that identifies which is which, and ask the
model one question per sender:

    if mail from this sender were archived and marked read automatically,
    would Jason miss something he needs?

Deliberately withheld from the model: which rule fired, and the List-Unsubscribe
split. Handing over the unsubscribe counts would let it re-derive rule B and agree
with itself. It sees the address, the display names the sender uses, the volume, the
date span and the label currently applied — roughly what a person would have.

Then compare:
  precision — of the senders the rules flag, how many does the judge agree on?
  recall    — of the controls, how many does the judge say should be reaching him?
              (those are misses the rules don't catch)

USAGE
  ./pattern-guard-eval.py             # summary + disagreements
  ./pattern-guard-eval.py --all       # every verdict
"""

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

os.environ["PATH"] = "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin"

CONTAINER = "inbox-zero-services-db-1"
ALIX_ENV = Path.home() / "Code/repos/alix/.env"
MODEL = "gpt-5.6-luna"
CONTROLS = 30

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


SQL = """
  WITH archiving AS (
    SELECT gi.value AS sender, ea.id AS acct, ea.email AS account, g.name AS grp
    FROM "GroupItem" gi
    JOIN "Group" g         ON gi."groupId" = g.id
    JOIN "EmailAccount" ea ON g."emailAccountId" = ea.id
    LEFT JOIN "Rule" r     ON r."groupId" = g.id
    LEFT JOIN "Action" a   ON a."ruleId" = r.id
    WHERE gi.type = 'FROM'
    GROUP BY 1,2,3,4
    HAVING string_agg(DISTINCT a.type::text, ',') ~ '(ARCHIVE|MARK_SPAM)'
  )
  SELECT ar.sender, ar.account, ar.grp,
         count(em.*)                            AS msgs,
         count(em."unsubscribeLink")            AS with_unsub,
         count(em.*) - count(em."unsubscribeLink") AS no_unsub,
         sum(CASE WHEN NOT em.inbox THEN 1 ELSE 0 END) AS archived,
         min(em.date)::date::text               AS first_seen,
         max(em.date)::date::text               AS last_seen,
         string_agg(DISTINCT coalesce(em."fromName",''), ' | ') AS names
  FROM archiving ar
  JOIN "EmailMessage" em ON em."from" = ar.sender AND em."emailAccountId" = ar.acct
  GROUP BY 1,2,3
  HAVING count(em.*) >= 3
"""


def flagged_by(row: dict) -> str | None:
    local = row["sender"].split("@")[0]
    if row["grp"] == "Cold Email" and ROLE_LOCALPART.search(local):
        return "A"
    total, no_unsub = row["msgs"], row["no_unsub"]
    if no_unsub > 0 and (no_unsub / total) <= NEVER_SETS_HEADER:
        return "B"
    return None


def api_key() -> str:
    for line in ALIX_ENV.read_text().splitlines():
        if line.startswith("OPENAI_API_KEY="):
            return line.split("=", 1)[1].strip().strip("\"'")
    raise SystemExit("no OPENAI_API_KEY in alix/.env")


PROMPT_HEAD = """Jason runs an automatic email filter across five accounts. When it decides a sender is bulk mail, it archives every future email from that address and marks it read, so he never sees any of it.

He is UK-based, works at 11FS (fintech), runs the personal domains jasonbates.com and b8s.cc, plays pickleball, and writes a book. He does not read archived mail.

For each sender below, judge ONE thing: if EVERY future email from this address were archived and marked read automatically, would Jason miss something he actually needs?

Say "miss" when the address carries anything consequential: deliveries, orders, invoices, billing, security or login codes, account or service notices, appointments, travel, legal or government mail, documents shared by a real person, or messages from someone he knows.

Say "fine" when the address carries only bulk marketing, newsletters he signed up for, promotional mail, platform noise, or unsolicited sales outreach from strangers. Unsolicited sales pitches are "fine" — he does not want those, even from a real human.

"unsure" is allowed and is better than a guess.

You are given the address, the display names it has used, how many messages have arrived, the date span, and the label the filter currently applies. Judge the ADDRESS, not any one message.

Return ONLY a JSON array, one object per sender, no prose:
[{"n": <number>, "verdict": "miss"|"fine"|"unsure", "why": "<max 12 words>"}]

SENDERS:
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="print every verdict")
    args = ap.parse_args()

    rows = psql(SQL)
    flagged = [r for r in rows if flagged_by(r)]
    controls = [r for r in rows if not flagged_by(r)]

    # Deterministic, content-derived ordering so reruns are comparable and the
    # flagged senders aren't clustered where the model could notice a block.
    controls.sort(key=lambda r: hash(r["sender"]) % 100000)
    sample = flagged + controls[:CONTROLS]
    sample.sort(key=lambda r: hash(r["sender"] + "salt") % 100000)

    lines = []
    for i, r in enumerate(sample):
        names = " | ".join(sorted({n for n in (r["names"] or "").split(" | ") if n})) or "(none)"
        lines.append(
            f'{i}. {r["sender"]}  names=[{names}]  messages={r["msgs"]}  '
            f'span={r["first_seen"]}..{r["last_seen"]}  filed_as={r["grp"]}'
        )

    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=json.dumps({
            "model": MODEL,
            "messages": [{"role": "user", "content": PROMPT_HEAD + "\n".join(lines)}],
            "max_completion_tokens": 30000,
        }).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key()}"},
    )
    with urllib.request.urlopen(req, timeout=600) as resp:
        body = json.loads(resp.read())

    text = body["choices"][0]["message"].get("content") or ""
    m = re.search(r"\[.*\]", text, re.S)
    if not m:
        raise SystemExit(f"no JSON array in reply: {text[:400]}")
    verdicts = {v["n"]: v for v in json.loads(m.group(0)) if isinstance(v.get("n"), int)}

    tp = fp = 0
    misses = []
    print(f"model={body.get('model')}  flagged={len(flagged)}  controls={len(controls[:CONTROLS])}\n")

    for i, r in enumerate(sample):
        v = verdicts.get(i)
        if not v:
            continue
        rule = flagged_by(r)
        verdict = v["verdict"]
        if rule and verdict == "miss":
            tp += 1
        elif rule and verdict == "fine":
            fp += 1
        elif not rule and verdict == "miss":
            misses.append((r, v))
        if args.all:
            print(f"  [{'RULE '+rule if rule else 'control'}] {verdict:<6} {r['sender']:<46} {v.get('why','')}")

    judged_flagged = tp + fp
    print(f"\nOf {judged_flagged} flagged senders the judge ruled on:")
    print(f"  agrees he would MISS something ... {tp}")
    print(f"  says archiving is FINE ........... {fp}")
    if judged_flagged:
        print(f"  precision ....................... {100*tp/judged_flagged:.0f}%")

    print(f"\nControls the judge says he'd MISS (rules did not catch these): {len(misses)}")
    for r, v in misses:
        print(f"  {r['sender']:<46} filed={r['grp']:<11} msgs={r['msgs']:<4} {v.get('why','')}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
