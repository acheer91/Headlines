"""Replay the code checks (writer.claims_ok and the number check) on saved sample outputs, offline.

python -m app.ai.replay [--only-new]

Reads the recap and preview sections of the two saved eval outputs (docs/phase4-samples.md at 3dacc26, the Sep 30
10:21 PM run, and docs/phase4-samples-recaps.md at bffeb78, the rerun), rebuilds each text's fact sheet from
tests/fixtures/ai, and runs the same code checks the writer runs. tests/fixtures/ai/known_errors.json lists the
errors two reviewers found by hand (anchor = words from the text). Reports, per run:
  * catch rate: known errors whose sentence the checks flag (the model fact-checker is not replayed);
  * texts the reviewers passed clean that the checks flag (false positives);
  * other flagged sentences nobody called an error (to read by hand: a false positive or a miss in the review).
No network, no tokens, no database.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

from . import facts, writer

ROOT = Path(__file__).resolve().parents[3]
FIX = ROOT / "api" / "tests" / "fixtures" / "ai"
RUNS = {"3dacc26": "docs/phase4-samples.md", "bffeb78": "docs/phase4-samples-recaps.md"}
DASH = re.compile("[‐-―]")


def norm(s: str) -> str:
    return DASH.sub("-", s.replace("’", "'")).lower()


def saved_output(ref: str) -> str:
    """The saved eval output exactly as committed at `ref`."""
    return subprocess.run(["git", "-C", str(ROOT), "show", f"{ref}:{RUNS[ref]}"], capture_output=True, text=True,
                          encoding="utf-8", check=True).stdout


def fixtures() -> dict[str, dict]:
    return {p.stem: json.loads(p.read_text(encoding="utf-8")) for p in sorted(FIX.glob("*.json"))
            if p.stem != "known_errors"}


def parse(md: str) -> list[dict]:
    """Every ready recap in an eval output: title, status, and its recap / home / away paragraphs by label."""
    out = []
    for block in re.split(r"(?m)^### ", md)[1:]:
        title, _, rest = block.partition("\n")
        if not title.startswith("Final: "):
            body = rest.split("<details>")[0]
            paras = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]
            status = re.search(r"^\*(\w+) ·", rest, re.M)
            texts = {}
            if status and status[1] == "ready" and len(paras) > 1:
                texts["preview"] = paras[1]
                for i, m in enumerate(re.finditer(r"(?m)^- (.+?) \(\[", body)):
                    texts[f"edge{i}"] = m[1]
            line = re.search(r"Point spread: (.+?) favored by ([\d.]+) points", rest)
            out.append({"title": title.strip(), "status": status[1] if status else "?", "texts": texts,
                        "spread": (line[1], float(line[2])) if line else None})
            continue
        status = re.search(r"^\*(\w+) ·", rest, re.M)
        body = rest.split("<details>")[0]
        paras = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]
        # The status line can hold a checker's multi-paragraph reasoning, so find the recap by the bets line the
        # code adds right after it.
        at = next((i for i, p in enumerate(paras) if "(added by code from the graded results)" in p), None)
        texts = {}
        if at:
            texts["recap"] = paras[at - 1]
            for p in paras[at + 1:]:
                m = re.match(r"\*\*(.+?):\*\*\s*(.*)", p, re.S)
                if m:
                    texts[m[1]] = m[2].strip()
        out.append({"title": title.strip(), "status": status[1] if status else "?", "texts": texts})
    return out


def game_for(title: str, games: dict[str, dict]) -> tuple[str, dict] | None:
    m = re.match(r"Final: (.+?) (\d+), (.+?) (\d+)$", title)
    for key, g in games.items():
        if m and (g["away"]["name"], str(g["away"]["score"]), g["home"]["name"], str(g["home"]["score"])) == m.groups():
            return key.removeprefix("final_"), g
        if not m and g["state"] == "pre" and g["home"]["name"] in title and g["away"]["name"] in title:
            return key.removeprefix("pre_"), g
    return None


def sentences(text: str) -> list[str]:
    return [s for s in writer.SENTENCE.split(writer._norm(text)) if s.strip()]


def flagged(texts: list[str], game: dict) -> list[tuple[str, str]]:
    """(sentence, why) for everything the code checks flag in these texts."""
    if game["state"] == "pre":
        out = []
        for t in texts:
            try:
                writer.line_ok(t, game)
            except writer.CheckFailed as exc:
                out.append((t, str(exc)))
        return out
    sheet = facts.recap_facts(game)
    fj = json.dumps(sheet["facts"], ensure_ascii=False)
    allowed = set(writer.NUM.findall(fj))
    out = []
    for t in texts:
        for s in sentences(t):
            bad = [n for n in writer.NUM.findall(s) if n not in allowed]
            if bad:
                out.append((s, f"numbers not in the facts: {bad}"))
        if writer.BAD_PERIOD.search(t):
            out.append((writer.BAD_PERIOD.search(t)[0], "no such quarter"))
        if writer.ADVICE.search(t):
            out.append((writer.ADVICE.search(t)[0], "advice wording"))
        bet = writer.bet_talk([t])
        if bet:
            out.append((bet, "bet talk"))
    out += writer.claim_problems(texts, game, sheet)
    return out


def overlaps(anchor: str, span: str) -> bool:
    """The flagged words and the reviewer's words are the same stretch of text (one inside the other)."""
    a, s = norm(anchor), norm(span)
    if s.isdigit():
        return re.search(rf"(?<!\d){s}(?!\d)", a) is not None
    return a in s or s in a


def evaluate(ref: str) -> dict:
    """Replay one saved run: {caught, known, missed, by_tag, clean_flagged, extra_flagged, texts}."""
    games = fixtures()
    known = [e for e in json.loads((FIX / "known_errors.json").read_text(encoding="utf-8")) if e["run"] == ref]
    ready = {}
    for r in parse(saved_output(ref)):
        hit = game_for(r["title"], games)
        if r["status"] == "ready" and hit:
            g = hit[1]
            if r.get("spread"):       # the line the eval's own fact sheet had (the fixture was saved later)
                fav, pts = r["spread"]
                g = dict(g, line=dict(g["line"], home_spread=-pts if fav == g["home"]["name"] else pts))
            ready[hit[0]] = (r, g)
    flags = {key: flagged(list(r["texts"].values()), g) for key, (r, g) in ready.items()}
    missed, by_tag = [], Counter()
    for e in known:
        caught = any(overlaps(e["anchor"], s) for s, _ in flags[e["game"]])
        by_tag[(e["tag"], caught)] += 1
        if not caught:
            missed.append(e)
    with_errors = {e["game"] for e in known}
    unfound = [e for e in known if not any(norm(e["anchor"]) in norm(t) for t in ready[e["game"]][0]["texts"].values())]
    return {"ref": ref, "texts": len(ready), "known": len(known), "caught": len(known) - len(missed),
            "missed": missed, "by_tag": by_tag, "unfound": unfound,
            "clean": sorted(k for k in ready if k not in with_errors),
            "clean_flagged": {k: flags[k] for k in ready if k not in with_errors and flags[k]},
            "extra_flagged": [(k, s, why) for k in with_errors for s, why in flags[k]
                              if not any(e["game"] == k and overlaps(e["anchor"], s) for e in known)]}


def main() -> int:
    total_caught = total_known = 0
    for ref in RUNS:
        res = evaluate(ref)
        print(f"\n=== {ref}  ({RUNS[ref]}): {res['texts']} ready texts")
        print(f"known errors caught by code: {res['caught']}/{res['known']}")
        for tag in sorted({t for t, _ in res["by_tag"]}):
            n = res["by_tag"][(tag, True)]
            print(f"   {tag:18} {n}/{n + res['by_tag'][(tag, False)]}")
        for e in res["missed"]:
            print(f"   MISSED #{e['id']} [{e['game']}] {e['anchor']!r}  ({e['tag']})")
        print(f"texts the reviewers passed clean ({', '.join(res['clean'])}): "
              f"{len(res['clean_flagged'])} flagged")
        for key, fl in res["clean_flagged"].items():
            for s, why in fl:
                print(f"   FALSE POSITIVE [{key}] {s[:110]!r}: {why[:100]}")
        print("flagged words in texts with errors that are not a listed error (read these by hand):")
        for key, s, why in res["extra_flagged"]:
            print(f"   [{key}] {s[:120]!r}: {why[:100]}")
        total_caught += res["caught"]
        total_known += res["known"]
    print(f"\nTOTAL caught {total_caught}/{total_known}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
