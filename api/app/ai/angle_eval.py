"""Z5: does code's recap angle (facts.recap_outline, M3) match the lead story a person picks? Zero tokens: no model,
no network, no database.

    python -m app.ai.angle_eval                              # code's angle for each fixture final
    python -m app.ai.angle_eval --labels z5_labels.json      # ... and how many match the labels
    python -m app.ai.angle_eval --outline                    # ... with each outline's lines
    python -m app.ai.angle_eval --angles-out z5_angles.json  # the allowed ids and their meanings, for the labeller

Labels: {"angles": [the allowed ids], "<fixture name>": "<angle id>", ...}, a fixture named as in tests/fixtures/ai
("final_phi_chi" or "phi_chi"). The labeller picks each final's lead story blind, from its fact sheet and the
meanings in facts.ANGLES (never the rules, which --angles-out leaves out and lists alphabetically, not by
priority). Pass bar (prep plan, Z5): code matches 14 of the 16 finals.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import facts

FIX = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "ai"
PASS_BAR = 14


def finals() -> dict[str, dict]:
    """The fixture finals by name without the prefix ('phi_chi'), in name order."""
    return {p.stem.removeprefix("final_"): json.loads(p.read_text(encoding="utf-8"))
            for p in sorted(FIX.glob("final_*.json"))}


def angles_file() -> dict:
    """What the labeller gets: the ids and their meanings (alphabetical: the order of the rules is a rule), the
    finals to label and the reply format."""
    return {"angles": [{"id": k, "meaning": facts.ANGLES[k]} for k in sorted(facts.ANGLES)],
            "fixtures": list(finals()),
            "labels_format": {"angles": ["<every id above>"], "<fixture>": "<the one id that is its lead story>"}}


def read_labels(path: Path) -> dict[str, str]:
    """{fixture: angle id} from a labels file; ValueError for an id that isn't allowed or isn't code's, or a
    fixture that doesn't exist."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    allowed = raw.get("angles")
    if not isinstance(allowed, list) or not allowed:
        raise ValueError('labels need an "angles" list of the allowed ids')
    unknown = sorted(set(allowed) - set(facts.ANGLES))
    if unknown:
        raise ValueError(f"angle ids code doesn't know: {unknown}")
    labels = {k.removeprefix("final_"): v for k, v in raw.items() if k != "angles"}
    strays = sorted(set(labels) - set(finals()))
    if strays:
        raise ValueError(f"no such fixture final: {strays}")
    bad = {k: v for k, v in labels.items() if v not in allowed}
    if bad:
        raise ValueError(f"labels outside the allowed ids: {bad}")
    return labels


def evaluate(labels: dict[str, str] | None = None) -> list[dict]:
    """One row per fixture final: name, code's angle, frame and lines, the label (or None) and whether they match."""
    rows = []
    for name, game in finals().items():
        plan = facts.recap_outline(game) or {"angle": None, "frame": "", "lines": []}
        label = (labels or {}).get(name)
        rows.append({"game": name, "angle": plan["angle"], "frame": plan["frame"], "lines": plan["lines"],
                     "label": label, "match": label is not None and label == plan["angle"]})
    return rows


def report(rows: list[dict], show_lines: bool = False) -> str:
    out = [f"{'final':10} {'code':20} {'label':20} match"]
    for r in rows:
        mark = "" if r["label"] is None else ("yes" if r["match"] else "NO")
        out.append(f"{r['game']:10} {r['angle'] or '(no outline)':20} {r['label'] or '-':20} {mark}")
        if show_lines:
            out.append(f"{'':10} angle: {r['frame']}")
            out += [f"{'':10} {i}. {line}" for i, line in enumerate(r["lines"], 1)]
    labelled = [r for r in rows if r["label"] is not None]
    if labelled:
        hits = sum(r["match"] for r in labelled)
        verdict = ("incomplete: not every final is labelled" if len(labelled) < len(rows) else
                   "PASS" if hits >= PASS_BAR else "FAIL")
        out.append(f"\ncode matched {hits}/{len(labelled)} labelled finals (Z5 bar: {PASS_BAR} of {len(rows)}): "
                   f"{verdict}")
    return "\n".join(out)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--labels", type=Path, help="a labels file: {\"angles\": [...], \"<fixture>\": \"<angle id>\"}")
    ap.add_argument("--outline", action="store_true", help="print each outline's lead and lines too")
    ap.add_argument("--angles-out", type=Path, help="write the labeller's file (ids and meanings) here")
    args = ap.parse_args(argv)
    if args.angles_out:
        args.angles_out.write_text(json.dumps(angles_file(), indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"wrote {args.angles_out}: {len(facts.ANGLES)} angles, {len(finals())} finals")
    try:
        labels = read_labels(args.labels) if args.labels else None
    except (OSError, ValueError) as exc:
        print(f"labels: {exc}", file=sys.stderr)
        return 2
    print(report(evaluate(labels), args.outline))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
