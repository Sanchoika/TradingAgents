"""Item 13: compare model/effort settings on FIXED saved cases (no live news, no trading).

The cases are synthetic test fixtures written for this evaluation, each with the expected answer
fixed BEFORE any model ran. They cover: duplicate story, genuine new fact, unrelated opposing
events, a verified denial, old news, and thin evidence (snippet only). Scoring is mechanical
(exact match on the fields that have a defensible answer); abstention quality is scored by
whether an ambiguous case is answered "unclear". It says nothing about trading profitability.

    python -m tradingagents.brent.research.model_eval OUT.json [--repeats 2]
Calls are counted and small (configs x repeats). Uses the existing ChatGPT sign-in; no paid API.
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime
from pathlib import Path

from tradingagents.brent import news

NOW = "2026-11-05T12:00:00+00:00"


def _item(key, title, summary, publisher, published, first_seen=NOW, repeat_of=None):
    return {"key": key, "title": title, "summary": summary, "publisher": publisher,
            "published": published, "first_seen": first_seen, "repeat_of": repeat_of}


# (item, expected) ; expected only lists fields with a defensible answer.
CASES = [
    (_item("c1", "OPEC+ agrees surprise 500,000 bpd output cut from December",
           "The group said in an official statement that eight members will cut output.",
           "OPEC (official)", "2026-11-05T11:30:00+00:00"),
     {"relevant": True, "status": "official", "bias": "bullish", "scope": "brent",
      "update_of_older_story": False}),
    (_item("c2", "OPEC+ to cut 500,000 bpd from December, statement says",
           "Reuters: the producer group announced the cut after its meeting.",
           "Reuters", "2026-11-05T11:40:00+00:00", repeat_of="c1"),
     {"relevant": True, "update_of_older_story": True}),
    (_item("c3", "Pipeline outage cuts North Sea exports; operator says repairs take two weeks",
           "Operator confirms the outage in a notice to shippers.",
           "Operator notice via Bloomberg", "2026-11-05T10:50:00+00:00"),
     {"relevant": True, "bias": "bullish", "update_of_older_story": False}),
    (_item("c4", "US shale producers expect record output next year, survey finds",
           "A survey of producers points to rising supply.",
           "Reuters", "2026-11-05T09:30:00+00:00"),
     {"relevant": True, "bias": "bearish", "scope": "wti_us"}),
    (_item("c5", "Ministry denies report it will cut oil exports",
           "The ministry said in a statement the earlier report is incorrect.",
           "Ministry statement via AP", "2026-11-05T11:00:00+00:00"),
     {"relevant": True, "status": "official", "update_of_older_story": True}),
    (_item("c6", "Oil prices: what traders should watch this week",
           "Commentary on possible scenarios for crude.",
           "Market blog", "2026-11-05T08:00:00+00:00"),
     {"status": "opinion", "bias": "unclear"}),
    (_item("c7", "Refinery strike in a distant region may affect product supply, sources say",
           "Unnamed sources describe possible disruption.",
           "Trade site", "2026-11-05T11:20:00+00:00"),
     {"status": "rumour"}),
    (_item("c8", "Tech giant unveils new smartphone",
           "Consumer electronics launch.", "Tech News", "2026-11-05T11:10:00+00:00"),
     {"relevant": False, "scope": "not_oil"}),
    (_item("c9", "Oil settles after OPEC meeting a week ago, analysts review the outcome",
           "Retrospective piece on last week's meeting.", "Financial paper",
           "2026-10-29T10:00:00+00:00", first_seen=NOW),
     {"update_of_older_story": True}),
    (_item("c10", "Weekly crude inventories fall by 3.2 million barrels",
           "Official weekly data; no consensus figure is quoted.", "EIA (official)",
           "2026-11-04T15:30:00+00:00"),
     {"category": "inventory", "bias": "unclear", "status": "official"}),
]
CONFIGS = [("gpt-6-luna", None), ("gpt-6-luna", "high"), ("gpt-6-sol", "medium"), ("gpt-6-sol", "high")]


def score(parsed: list[dict]) -> dict:
    by = {c["key"]: c for c in parsed}
    hits = total = 0
    wrong = []
    for item, exp in CASES:
        got = by.get(item["key"])
        for k, v in exp.items():
            total += 1
            if got and got.get(k) == v:
                hits += 1
            else:
                wrong.append({"key": item["key"], "field": k, "expected": v,
                              "got": (got or {}).get(k)})
    ambiguous = [by[i["key"]] for i, e in CASES if e.get("bias") == "unclear" and i["key"] in by]
    return {"fields_correct": hits, "fields_total": total, "accuracy": round(hits / total, 3),
            "abstained_where_ambiguous": sum(c["bias"] == "unclear" for c in ambiguous),
            "ambiguous_cases": len(ambiguous), "wrong": wrong}


def run_config(model_name: str, effort: str | None, repeats: int, make=news.default_model) -> dict:
    model = make("chatgpt", model_name, effort)
    rows = []
    for _ in range(repeats):
        t0 = time.monotonic()
        try:
            raw, usage = model(news.SYSTEM, news.build_user_message([i for i, _ in CASES]))
            parsed = news.validate(raw, {i["key"] for i, _ in CASES})
            res = score(parsed)
            res["valid_json"] = True
        except Exception as exc:
            res = {"valid_json": False, "error": f"{type(exc).__name__}: {str(exc)[:160]}"}
            usage = {}
        res["latency_s"] = round(time.monotonic() - t0, 1)
        res["tokens"] = (usage or {}).get("total_tokens")
        rows.append(res)
    return {"model": model_name, "effort": effort or "provider default", "runs": rows}


def main(argv=None) -> int:
    argv = list(argv or sys.argv[1:])
    repeats = int(argv[argv.index("--repeats") + 1]) if "--repeats" in argv else 2
    out = {"version": "model-eval-1", "prompt_version": news.PROMPT_VERSION,
           "ran_at": datetime.now().isoformat(), "cases": len(CASES),
           "calls": len(CONFIGS) * repeats,
           "configs": [run_config(m, e, repeats) for m, e in CONFIGS]}
    Path(argv[0]).write_text(json.dumps(out, indent=2))
    for c in out["configs"]:
        print(c["model"], c["effort"], [r.get("accuracy", "ERR") for r in c["runs"]])
    return 0


if __name__ == "__main__":
    sys.exit(main())
