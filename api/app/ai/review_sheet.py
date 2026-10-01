"""The claim-by-claim review sheet for an eval output, so reviewers can start the moment it lands.

python -m app.ai.review_sheet [docs/phase4-samples.md] [-o docs/phase4-review-sheet.csv]
python -m app.ai.review_sheet --tally filled-sheet.csv       # errors by cause, once reviewers have filled it in

One row per claim (a sentence of a recap, a preview or a team summary, an "edge" bullet, a headline) with the fact
lines it most likely rests on, or the article it cites. Reviewers fill three columns:
  verdict  ok | wrong | unsupported   (wrong: FACTS say otherwise; unsupported: FACTS don't say it)
  cause    wrong comparison | wrong team | invented cause | temporal claim | other   (for wrong/unsupported rows)
  note     what is wrong, in a few words (for "other", the kind: player/team mix-up, wrong number, banned word...)
The causes are the breakdown the SWE uses to rank fixes (Oct 1: 24 errors tagged by hand, see known_errors.json).
Failed texts are skipped (they show the fallback line). Opens in Excel or Sheets (UTF-8 with a BOM).
"""
from __future__ import annotations

import csv
import re
import sys
from collections import Counter
from pathlib import Path

from . import writer

ROOT = Path(__file__).resolve().parents[3]
CAUSES = ["wrong comparison", "wrong team", "invented cause", "temporal claim", "other"]
VERDICTS = ["ok", "wrong", "unsupported"]
COLUMNS = ["id", "section", "text", "part", "claim", "facts it rests on / source", "verdict", "cause", "note"]
NUM = re.compile(r"\d[\d,.:]*\d|\d")
STOP = {"the", "and", "for", "with", "that", "this", "from", "were", "was", "had", "have", "their", "while", "into",
        "after", "before", "game", "team", "points", "point", "quarter"}


def sections(md: str) -> list[dict]:
    """Every text in an eval output: section (Previews / Recaps / Headlines), title, status, its fact lines."""
    out = []
    for part in re.split(r"(?m)^## ", md)[1:]:
        name, _, rest = part.partition("\n")
        for block in re.split(r"(?m)^### ", rest)[1:]:
            title, _, body = block.partition("\n")
            status = re.search(r"^\*(\w+) ·", body, re.M)
            main, _, sheet = body.partition("<details>")
            out.append({"section": name.strip(), "title": title.strip(), "status": status[1] if status else "?",
                        "body": main, "facts": [m[1].strip() for m in re.finditer(r"(?m)^- (.+)$", sheet)]})
    return out


def paragraphs(body: str) -> list[str]:
    return [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]


def split_claims(text: str) -> list[str]:
    return [s.strip() for s in writer.SENTENCE.split(writer._norm(text).replace("\n", " ")) if s.strip()]


def link(item: str) -> tuple[str, str]:
    """('text', 'url') from 'text. ([ESPN](https://...))'; url '' when there is none."""
    m = re.search(r"\s*\(\[[^\]]*\]\((https?://[^)]+)\)\)\s*$", item)
    return (item[:m.start()].strip(), m[1]) if m else (item.strip(), "")


def related_facts(claim: str, facts: list[str], n: int = 6) -> str:
    """The fact lines sharing the most numbers and names with the claim: a starting point, not the whole sheet.
    A name (a capitalized word) counts 3, a number 2, another word 1; ties keep the sheet's order."""
    nums = set(NUM.findall(claim))
    words = {w.lower(): w[0].isupper() for w in re.findall(r"[A-Za-z][A-Za-z.'-]{2,}", claim) if w.lower() not in STOP}
    scored = []
    for i, f in enumerate(facts):
        fw = {w.lower() for w in re.findall(r"[A-Za-z][A-Za-z.'-]{2,}", f)}
        s = 2 * len(nums & set(NUM.findall(f))) + sum(3 if cap else 1 for w, cap in words.items() if w in fw)
        if s:
            scored.append((-s, i, f))
    return " | ".join(f for _, _, f in sorted(scored)[:n]) or "(none matched: see the fact sheet under the text)"


def rows(md: str) -> tuple[list[list[str]], list[str]]:
    out, skipped = [], []
    for sec in sections(md):
        if sec["status"] != "ready":
            skipped.append(f"{sec['title']} ({sec['status']})")
            continue
        paras = paragraphs(sec["body"])
        claims: list[tuple[str, str, str]] = []          # (part, claim, source url)
        if sec["section"] == "Recaps":
            at = next((i for i, p in enumerate(paras) if "(added by code from the graded results)" in p), None)
            if at is None:
                skipped.append(f"{sec['title']} (no recap found)")
                continue
            claims += [("recap", c, "") for c in split_claims(paras[at - 1])]
            for p in paras[at + 1:]:
                m = re.match(r"\*\*(.+?):\*\*\s*(.*)", p, re.S)
                if m:
                    claims += [(m[1], c, "") for c in split_claims(m[2])]
        elif sec["section"] == "Previews":
            first = next((i for i, p in enumerate(paras) if re.match(r"\*\*.* edges\*\*$", p)), None)
            if first:
                claims += [("preview", c, "") for c in split_claims(paras[first - 1])]
            part = ""
            for line in sec["body"].splitlines():
                h = re.match(r"\*\*(.+?)\*\*$", line.strip())
                if h:
                    part = h[1]
                elif line.startswith("- ") and part and not part.startswith("Sources") and "None in these" not in line:
                    text, url = link(line[2:])
                    claims.append((part, text, url))
        else:                                              # Headlines: one claim per line, no fact sheet
            for line in sec["body"].splitlines():
                if line.startswith("- "):
                    text, url = link(line[2:])
                    claims.append(("headline", text, url))
        # A preview also draws on its articles (storylines have no fact line): list them after the matching facts.
        srcs = re.findall(r"\]\((https?://[^)]+)\)\s*$", sec["body"].partition("**Sources used**")[2], re.M)
        for part, claim, url in claims:
            basis = url or (related_facts(claim, sec["facts"]) if sec["facts"] else "(no fact sheet: check the source)")
            if sec["section"] == "Previews" and not url and srcs:
                basis += " | articles: " + " ".join(srcs)
            out.append([sec["section"], sec["title"], part, claim, basis])
    return [[str(i + 1)] + r + ["", "", ""] for i, r in enumerate(out)], skipped


def write_sheet(md: str, path: Path) -> tuple[int, list[str]]:
    data, skipped = rows(md)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["id", "section", "text", "part", "claim", "facts it rests on / source",
                    "verdict (" + " / ".join(VERDICTS) + ")", "cause (" + " / ".join(CAUSES) + ")", "note"])
        w.writerows(data)
    return len(data), skipped


def tally(path: Path) -> str:
    """Errors by cause from a filled sheet, plus how many texts had at least one."""
    with path.open(encoding="utf-8-sig", newline="") as f:
        rd = list(csv.reader(f))
    head = rd[0]
    vi = next(i for i, h in enumerate(head) if h.startswith("verdict"))
    ci = next(i for i, h in enumerate(head) if h.startswith("cause"))
    errors = [r for r in rd[1:] if r[vi].strip().lower() in ("wrong", "unsupported")]
    by = Counter((r[ci].strip().lower() or "(no cause given)") for r in errors)
    texts = {(r[1], r[2]) for r in errors}
    reviewed = sum(1 for r in rd[1:] if r[vi].strip())
    lines = [f"{len(errors)} errors in {len(texts)} text(s); {reviewed} of {len(rd) - 1} claims reviewed"]
    lines += [f"  {c:18} {by[c]}" for c in CAUSES] + [f"  {c:18} {n}" for c, n in by.items() if c not in CAUSES]
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    if argv and argv[0] == "--tally":
        print(tally(Path(argv[1])))
        return 0
    out = ROOT / "docs" / "phase4-review-sheet.csv"
    if "-o" in argv:
        out = Path(argv[argv.index("-o") + 1])
        argv = [a for i, a in enumerate(argv) if i not in (argv.index("-o"), argv.index("-o") + 1)]
    src = Path(argv[0]) if argv else ROOT / "docs" / "phase4-samples.md"
    n, skipped = write_sheet(src.read_text(encoding="utf-8"), out)
    print(f"wrote {out}: {n} claims" + (f"; skipped {len(skipped)} failed or empty: {', '.join(skipped)}" if skipped else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
