"""Does the fact-checker catch the errors we found by hand on 2026-09-29, without flagging correct text?

python -m app.ai.check_eval   (laptop only; uses the check model's quota, not the writer's)
Each case is a sentence the writer really produced, the game it was about, and whether it's wrong.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from . import client, facts, prompts, samples

FIX = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "ai"

# (fixture, text, is_wrong, what's wrong)
CASES = [
    ("final_bal_dal", "Baltimore scored 34 points, matching Dallas quarter by quarter with 7, 10, 7 and 10.",
     True, "Dallas scored 10, 3, 8, 10"),
    ("final_bal_dal", "The Ravens forced no turnovers, were 3-9 on third down, and possessed the ball for 29:22.",
     True, "the Ravens forced 1; they committed none"),
    ("final_bal_dal", "Both teams dominated time of possession, but the Ravens' error-free night carried them.",
     True, "nonsense claim"),
    ("final_phi_chi", "The third quarter saw the Bears extend the lead to 20-7, and a final seventh-quarter-long drive "
                      "sealed a 27-7 victory.", True, "no seventh quarter"),
    ("final_sea_wsh", "Seattle surged ahead with 14 points in Q4, but Washington answered with a late 9-point burst.",
     True, "Seattle never led"),
    ("final_sea_wsh", "The game featured 64 combined points and a total of 23 turnovers and penalties combined.",
     True, "made-up total"),
    ("final_ne_jax", "Jacksonville added another 14 in the third and a seventh-quarter touchdown.", True,
     "no seventh quarter"),
    ("final_ne_jax", "Jacksonville held the Patriots scoreless in the first half.", True, "NE scored 3 in Q2"),
    # Correct text: should come back clean.
    ("final_bal_dal", "Dallas put up 31 points, scoring 10, 3, 8 and 10 by quarter. The Cowboys recorded one turnover "
                      "and were 5-13 on third down, holding the ball for 30:38.", False, ""),
    ("final_phi_chi", "Chicago rolled to a 27-7 win, improving to 2-1 on the season. The Bears racked up 375 total "
                      "yards while forcing three turnovers and committing none.", False, ""),
    ("final_sea_wsh", "Washington never trailed at a quarter break and held on 33-31.", False, ""),
    ("final_ne_jax", "After a scoreless first quarter, the Jaguars pulled away with 14 points in each of the next "
                     "two quarters.", False, ""),
]


def main() -> None:
    samples._load_keys()
    hits = misses = false_alarms = clean = 0
    for fixture, text, wrong, why in CASES:
        game = json.loads((FIX / f"{fixture}.json").read_text(encoding="utf-8"))
        sheet = json.dumps(facts.recap_facts(game)["facts"], ensure_ascii=False)
        t = time.monotonic()
        try:
            out = json.loads(client.check(prompts.FACT_CHECK.format(facts=sheet, text=text)))
            probs = out.get("problems") or []
        except (client.AIError, ValueError) as exc:
            print(f"ERROR {type(exc).__name__}: {exc}")
            continue
        flagged = bool(probs)
        if wrong and flagged:
            hits += 1
        elif wrong:
            misses += 1
        elif flagged:
            false_alarms += 1
        else:
            clean += 1
        mark = "OK  " if flagged == wrong else "BAD "
        print(f"{mark}{'wrong' if wrong else 'right'} -> {'flagged' if flagged else 'clean'} "
              f"({time.monotonic() - t:.1f}s) {text[:70]}")
        for p in probs:
            print(f"       - {p.get('quote', '')[:60]!r}: {p.get('why', '')[:90]}")
    print(f"\ncaught {hits}/{hits + misses} errors; false alarms {false_alarms}/{false_alarms + clean} correct texts; "
          f"model {client.check_model()}")


if __name__ == "__main__":
    main()
