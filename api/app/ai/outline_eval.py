"""T3: does code's recap outline (M3) keep recaps as accurate, and can the recap writer then run at low reasoning?
First drafts of the 16 fixture finals in three arms, scored claim by claim by people on review_sheet.py's sheet.

    python -m app.ai.outline_eval                           # dry run (the default): 120b tokens per arm, nothing sent
    python -m app.ai.outline_eval --run --arm outline+low --labels <labels.json> --z5-fixtures <dir>   # one arm (120b)
    python -m app.ai.outline_eval --sheet                   # rebuild the blind review file and sheet from the drafts
    python -m app.ai.outline_eval --tally filled.csv        # errors per claim by arm, once the sheet is filled in

Arms: medium (today's recap prompt), outline+medium and outline+low (writer.recap_prompt with facts.recap_outline,
the writer's reasoning effort as named). One call per final and arm: the first draft only, no rewrite and no fact
check (no Qwen tokens). The code checks' verdict on each draft is kept in the key, never in the review file. Drafts
are saved to docs/phase4-t3-drafts.json (merged an arm at a time; a final already drafted in an arm is skipped unless
--redo), then shuffled into one blind file, docs/phase4-t3-review.md (texts T3-01, T3-02...; which arm wrote which is
only in docs/phase4-t3-key.json), and its sheet docs/phase4-t3-review-sheet.csv. Review once all three arms are in.
The sheet's causes (T3_CAUSES) add "wrong quarter" and "order in a quarter" in place of "temporal claim"; the tally
counts the cause column exactly, and a "temporal claim" there as a possible wrong quarter.

PARKED until Z5 passes on a fresh set (prep plan §3: M3 is gated on Z5 and T3, so a T3 run before Z5 passes spends
~200K of 120b on an outline that can't ship). Z5 failed 11/16 on Oct 2; the angle rules were then changed on those
labels, which now score 15/16 in-sample and can't open this gate. A real run needs --run and refuses unless (prep
plan §6: evals that spend 120b run on weekdays, at most 100K a day):
  * --labels names Z5 labels that pass angle_eval on --z5-fixtures <dir>, a fresh set angle_fixtures built
    (angle_eval.fresh_set_problem: not under tests/, its manifest's facts.py sha256 still facts.py's, none of the 16
    finals the rules were tuned on); the bar is 85% of its finals, rounded up; the labels' sha256 goes into the
    ledger and the key;
  * the eval ledger (checks/phase4_tokens.json, phase4_eval.py's format) shows under 100K tokens in the last 24 h, and
    that plus this run's high estimate fits in 100K, so one arm a day (an arm is 48-82K on the dry run);
  * it is a weekday in Pacific time;
  * the recap writer is gpt-oss-120b (no AI_WRITERS override) and GROQ_API_KEY is set (the environment or ../.env).
The run adds the tokens it spent to the ledger. If the laptop stack shares the server's Groq organization, pause its
AI schedules first (prep plan I14). Pass bar (prep plan, T3): neither outline arm has a higher per-claim error rate
than medium (95% interval) and neither has a wrong-team or wrong-quarter claim; otherwise drop M3b. Every arm uses
the prompt as M2 ordered it (writer.recap_prompt), so T3 can't show whether that order cost accuracy: the usage
report's M2 line watches it.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import random
import re
import subprocess
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from . import angle_eval, client, facts, review_sheet, samples, writer
from .angle_eval import finals

ROOT = Path(__file__).resolve().parents[3]
DOCS = ROOT / "docs"
LEDGER = ROOT.parent / "checks" / "phase4_tokens.json"
PT = ZoneInfo("America/Los_Angeles")
MODEL = "openai/gpt-oss-120b"
DAY_CAP = 100_000
SEED = 20261002              # the blind order of the review file
ARMS = {"medium": (False, "medium"), "outline+medium": (True, "medium"), "outline+low": (True, "low")}
# A recap draft's reply (reasoning + JSON) at medium, measured on the laptop: ai_calls #13 and #11 (Oct 2), 4,734 and
# 4,825 tokens in all less their counted prompts, 2,491 and 2,372. Prompts are counted here per final (o200k_harmony).
MEDIUM_REPLY = (2_243, 2_453)
# Low reasoning on a recap is unmeasured: the prep anatomy's estimate (M3b) saves 1,408-1,908 a write.
LOW_SAVING = (1_408, 1_908)
REPLY = {"medium": MEDIUM_REPLY, "low": (MEDIUM_REPLY[0] - LOW_SAVING[1], MEDIUM_REPLY[1] - LOW_SAVING[0])}
CODE = re.compile(r"\((T3-\d+)\)\s*$")
# review_sheet's causes with "temporal claim" split in two, so the pass bar's wrong quarter is a value, not a note:
# "wrong quarter" puts a score, a lead or a stat in a quarter FACTS doesn't; "order in a quarter" says what came first
# inside one ("before", "responded").
T3_CAUSES = ["wrong comparison", "wrong team", "wrong quarter", "order in a quarter", "invented cause", "other"]
REVIEW_NOTE = ("Fill the sheet's verdict and cause for every claim. Causes: wrong team (a stat, score or play given to "
               "the other team); wrong quarter (a score, lead or play put in a quarter FACTS doesn't); order in a "
               "quarter (what came first inside one quarter); wrong comparison; invented cause; other (say what in the "
               "note). The tally reads the cause column, not the note.")


def estimate(games: dict[str, dict]) -> dict[str, tuple[int, int, int]]:
    """arm -> (prompt tokens, low total, high total) for one first draft of every final."""
    out = {}
    for arm, (outline, effort) in ARMS.items():
        prompt = sum(client.prompt_tokens(MODEL, writer.recap_prompt(g, outline=outline)) for g in games.values())
        lo, hi = REPLY[effort]
        out[arm] = (prompt, prompt + lo * len(games), prompt + hi * len(games))
    return out


def ledger_spent(path: Path, now: datetime) -> int:
    """Tokens the eval ledger shows in the 24 h before `now`. Raises when it can't be read."""
    entries = json.loads(path.read_text(encoding="utf-8"))
    return sum(int(e["tokens"]) for e in entries
               if now - timedelta(hours=24) < datetime.fromisoformat(e["at"]) <= now)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def z5_problem(labels: Path | None, fixtures: Path | None) -> str | None:
    """Why Z5 doesn't clear T3 to run (prep plan §3: M3 is gated on both), or None when `labels` pass on `fixtures`,
    a fresh set (angle_eval.fresh_set_problem): never the 16 finals the angle rules were tuned on."""
    if labels is None:
        return ("Z5 hasn't passed: name a fresh set's labels that do with --labels and its folder with --z5-fixtures "
                "(python -m app.ai.angle_fixtures, then angle_eval --fixtures; 85% of its finals)")
    if fixtures is None:
        return ("Z5 needs the fresh set the labels were made from: --z5-fixtures <dir> (python -m "
                "app.ai.angle_fixtures); the 16 test fixtures are the finals the angle rules were tuned on")
    fresh = angle_eval.fresh_set_problem(fixtures)
    if fresh:
        return f"Z5 fixtures: {fresh}"
    try:
        rows = angle_eval.evaluate(angle_eval.read_labels(labels, fixtures), fixtures)
    except (OSError, ValueError) as exc:
        return f"Z5 labels {labels}: {exc}"
    if not angle_eval.passed(rows):
        hits, labelled = sum(r["match"] for r in rows), sum(r["label"] is not None for r in rows)
        return (f"Z5 fails on {labels.name}: code matched {hits}/{labelled} labelled finals (needs "
                f"{angle_eval.bar(len(rows))} of {len(rows)}); after a change to the angle rules, label a fresh set")
    return None


def refusals(arms: list[str], est: dict, ledger: Path, now: datetime, labels: Path | None = None,
             fixtures: Path | None = None) -> list[str]:
    """Every reason a real run may not start now; empty = it may."""
    why = []
    z5 = z5_problem(labels, fixtures)
    if z5:
        why.append(z5)
    try:
        spent = ledger_spent(ledger, now)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        why.append(f"the eval ledger {ledger} can't be read ({type(exc).__name__}); run checks/phase4_eval.py once")
        spent = None
    need = sum(est[a][2] for a in arms)
    if spent is not None and spent >= DAY_CAP:
        why.append(f"the eval ledger shows {spent:,} tokens in the last 24 h; a run needs under {DAY_CAP:,}")
    elif spent is not None and spent + need > DAY_CAP:
        why.append(f"{spent:,} in the last 24 h + up to {need:,} for {', '.join(arms)} is over {DAY_CAP:,}: "
                   "one arm a day")
    day = now.astimezone(PT)
    if day.weekday() >= 5:
        why.append(f"it is {day:%A} in Pacific time: weekdays only")
    if client.route("recap")[0] != [MODEL]:
        why.append(f"the recap writer is {client.route('recap')[0]}, not {MODEL} (unset AI_WRITERS)")
    if not os.environ.get("GROQ_API_KEY"):
        why.append("GROQ_API_KEY is not set")
    return why


def load_drafts(path: Path) -> dict[str, dict]:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")


def draft(arm: str, game: dict) -> dict:
    """One first draft, never rewritten or fact-checked: its status, body, the code checks' verdict and tokens."""
    outline, effort = ARMS[arm]
    sheet = facts.recap_facts(game)
    plan = facts.recap_outline(game, sheet) or {}
    prompt = writer.recap_prompt(game, sheet, outline=outline)
    entry = {"arm": arm, "angle": plan.get("angle"), "frame": plan.get("frame"), "status": "failed",
             "prompt_tokens": client.prompt_tokens(MODEL, prompt),
             "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    client.begin("recap")
    before = client.tokens_used
    try:
        x = json.loads(client.write(prompt, json_out=True, reasoning=effort))
        parts = ("recap", "home", "away")
        if not (isinstance(x, dict) and all(isinstance(x.get(k), str) and x[k].strip() for k in parts)):
            raise writer.CheckFailed("reply had the wrong shape")
        entry |= {"status": "ready", "body": {k: x[k] for k in parts}}
        try:
            writer.recap_code_check(x, game, sheet)
            entry["code_check"] = "pass"
        except writer.CheckFailed as exc:
            entry["code_check"] = str(exc)
    except (ValueError, client.BadReply):
        entry["reason"] = "reply was not JSON"
    except writer.CheckFailed as exc:
        entry["reason"] = str(exc)
    except client.RateLimited:
        raise                           # 120b is cooling down or out of budget: stop the run here
    except client.AIError as exc:
        entry["reason"] = str(exc)
    finally:
        entry["tokens"] = client.tokens_used - before
        entry["writer"] = client.last_writer()
    return entry


def run(arms: list[str], games: dict[str, dict], out: Path, ledger: Path, z5: str | None = None,
        redo: bool = False) -> int:
    """Write the arms' first drafts (saved after each one), add the tokens to the ledger, rebuild the review file. A
    final already drafted ('ready') in an arm is skipped, so a run stopped by a rate limit picks up where it stopped;
    redo writes it again. z5: the sha256 of the Z5 labels that cleared the run, kept with every draft and the ledger."""
    drafts_path = out / "phase4-t3-drafts.json"
    drafts = load_drafts(drafts_path)
    spent, stopped = 0, None
    commit = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"], capture_output=True,
                            text=True).stdout.strip() or "?"
    try:
        for arm in arms:
            for name, game in games.items():
                if not redo and drafts.get(f"{arm}|{name}", {}).get("status") == "ready":
                    continue
                entry = draft(arm, game) | {"game": name, "commit": commit, "z5": z5}
                drafts[f"{arm}|{name}"] = entry
                save_json(drafts_path, drafts)
                spent += entry["tokens"]
                print(f"{arm:15} {name:8} {entry['status']:6} {entry['tokens']:6,} tokens"
                      f"  code checks: {entry.get('code_check', entry.get('reason'))}"[:160], flush=True)
    except client.RateLimited as exc:
        stopped = f"stopped: {exc}"
    finally:
        entries = json.loads(ledger.read_text(encoding="utf-8"))
        entries.append({"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "tokens": spent,
                        "run": f"T3 outline_eval {', '.join(arms)} at {commit}", "z5_labels_sha256": z5})
        ledger.write_text(json.dumps(entries, indent=1), encoding="utf-8")
    print(f"{spent:,} tokens of {MODEL}, added to {ledger}" + (f"; {stopped}" if stopped else ""))
    print(build_review(drafts, games, out))
    return 1 if stopped else 0


def _one_paragraph(text: str) -> str:
    """review_sheet reads the paragraph above the bets line as the recap: fold a recap's paragraphs into one."""
    return re.sub(r"\s*\n\s*", " ", text.strip())


def build_review(drafts: dict[str, dict], games: dict[str, dict], out: Path) -> str:
    """The blind review file (shuffled, arms hidden), its key and its review_sheet CSV, from the saved drafts."""
    order = sorted(drafts)
    random.Random(SEED).shuffle(order)
    md = ["# T3 outline eval: recap first drafts, blind\n",
          f"{len(order)} of {len(ARMS) * len(games)} drafts. Not fact-checked or rewritten; which arm wrote each "
          "text is in phase4-t3-key.json. Paragraph breaks are folded so every sentence lands on the sheet.\n",
          REVIEW_NOTE + "\n", "## Recaps\n"]
    key = {}
    for i, k in enumerate(order, 1):
        e, code = drafts[k], f"T3-{i:02d}"
        game = games[e["game"]]
        h, a = game["home"], game["away"]
        key[code] = {f: e.get(f) for f in ("arm", "game", "angle", "status", "code_check", "reason", "tokens",
                                            "prompt_tokens", "writer", "commit", "z5")}
        md.append(f"### Final: {a['name']} {a.get('score')}, {h['name']} {h.get('score')} ({code})\n")
        if e["status"] != "ready":
            md.append("*failed · first draft only*\n")
            continue
        b = e["body"]
        md += ["*ready · first draft only, not fact-checked*\n", _one_paragraph(b["recap"]) + "\n",
               f"*{writer.bets_line(game)}* (added by code from the graded results)\n",
               f"**{a['short']}:** {_one_paragraph(b['away'])}\n", f"**{h['short']}:** {_one_paragraph(b['home'])}\n"]
        md += samples._sheet(facts.recap_facts(game))
    text = "\n".join(md)
    (out / "phase4-t3-review.md").write_text(text, encoding="utf-8", newline="\n")
    save_json(out / "phase4-t3-key.json", key)
    n, skipped = review_sheet.write_sheet(text, out / "phase4-t3-review-sheet.csv", causes=T3_CAUSES)
    return (f"wrote {out / 'phase4-t3-review.md'} and its sheet: {n} claims from {len(order) - len(skipped)} drafts"
            + (f"; {len(skipped)} failed" if skipped else ""))


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """The 95% Wilson interval of k in n."""
    if n == 0:
        return 0.0, 1.0
    p, d = k / n, 1 + z * z / n
    c, h = (p + z * z / (2 * n)) / d, z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0.0, c - h), min(1.0, c + h)


def tally(sheet: Path, key: dict) -> str:
    """Errors per reviewed claim by arm, and each outline arm against medium (Newcombe's interval for the
    difference; claims within one text aren't independent, so read it as a guide). An arm gets a verdict only once
    it and medium are fully reviewed: every ready draft has a reviewed claim and every error a cause from T3_CAUSES.
    A wrong team or wrong quarter fails an arm at once. Causes are read exactly; "temporal claim" (review_sheet's
    general cause) counts as a possible wrong quarter."""
    with sheet.open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f))
    head = rows[0]
    col = lambda name: next(i for i, h in enumerate(head) if h.startswith(name))
    ti, vi, ci = col("text"), col("verdict"), col("cause")
    by = {arm: Counter() for arm in ARMS}
    reviewed = Counter()
    for r in rows[1:]:
        m = CODE.search(r[ti])
        verdict = r[vi].strip().lower()
        if not m or m[1] not in key or not verdict:
            continue
        reviewed[m[1]] += 1
        c = by[key[m[1]]["arm"]]
        c["claims"] += 1
        if verdict in ("wrong", "unsupported"):
            c["errors"] += 1
            cause = r[ci].strip().lower()
            c["wrong team"] += cause == "wrong team"
            c["wrong quarter"] += cause in ("wrong quarter", "temporal claim")
            c["no cause"] += cause not in T3_CAUSES and cause != "temporal claim"
    unread = Counter(e["arm"] for code, e in key.items() if e.get("status") == "ready" and not reviewed[code])
    out = [f"{'arm':15} {'claims':>6} {'errors':>6}  rate (95%)            wrong team  wrong quarter  unreviewed drafts"]
    rate = {}
    for arm, c in by.items():
        lo, hi = wilson(c["errors"], c["claims"])
        rate[arm] = (c["errors"] / c["claims"] if c["claims"] else 0.0, lo, hi)
        out.append(f"{arm:15} {c['claims']:6} {c['errors']:6}  {rate[arm][0]:6.1%} ({lo:.1%}-{hi:.1%})"
                   f"   {c['wrong team']:10}  {c['wrong quarter']:13}  {unread[arm]}")
    p0, l0, u0 = rate["medium"]
    verdicts = []
    for arm in ("outline+medium", "outline+low"):
        p1, l1, u1 = rate[arm]
        low = (p1 - p0) - math.sqrt((p1 - l1) ** 2 + (u0 - p0) ** 2)
        high = (p1 - p0) + math.sqrt((u1 - p1) ** 2 + (p0 - l0) ** 2)
        higher = low > 0
        clean = not by[arm]["wrong team"] and not by[arm]["wrong quarter"]
        done = all(by[a]["claims"] and not unread[a] and not by[a]["no cause"] for a in (arm, "medium"))
        verdict = "FAIL" if not clean else "not reviewed yet" if not done else "FAIL" if higher else "pass"
        verdicts.append(verdict)
        left = "; ".join(f"{a}: {by[a]['claims']} reviewed claims, {unread[a]} ready draft(s) with none, "
                         f"{by[a]['no cause']} error(s) without a T3 cause"
                         for a in (arm, "medium") if not by[a]["claims"] or unread[a] or by[a]["no cause"])
        out.append(f"{arm} - medium: {p1 - p0:+.1%} ({low:+.1%} to {high:+.1%}): "
                   f"{'HIGHER than medium' if higher else 'not higher'}; "
                   f"{'no' if clean else 'HAS'} wrong-team or wrong-quarter claims -> {verdict}"
                   + (f" ({left})" if verdict == "not reviewed yet" else ""))
    overall = "FAIL" if "FAIL" in verdicts else "pass" if verdicts == ["pass", "pass"] else "not reviewed yet"
    out.append(f"T3: {overall} (pass: both outline arms pass; otherwise drop M3b)")
    return "\n".join(out)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--run", action="store_true", help="write first drafts (spends 120b tokens); default: dry run")
    ap.add_argument("--arm", action="append", choices=list(ARMS), help="the arm(s) to run; default: all three")
    ap.add_argument("--sheet", action="store_true", help="rebuild the review file and sheet from the saved drafts")
    ap.add_argument("--tally", type=Path, help="a filled review sheet: errors per claim by arm")
    ap.add_argument("--out", type=Path, default=DOCS, help="where the drafts, review file, key and sheet go")
    ap.add_argument("--ledger", type=Path, default=LEDGER, help="the eval ledger (phase4_eval.py's format)")
    ap.add_argument("--labels", type=Path, help="Z5 labels (angle_eval's format): a real run needs them to pass "
                    "on --z5-fixtures")
    ap.add_argument("--z5-fixtures", type=Path, help="the fresh set the labels were made from (python -m "
                    "app.ai.angle_fixtures): a real run needs it")
    ap.add_argument("--redo", action="store_true", help="write again finals an arm has already drafted")
    args = ap.parse_args(argv)
    games = finals()
    if args.tally:
        print(tally(args.tally, json.loads((args.out / "phase4-t3-key.json").read_text(encoding="utf-8"))))
        return 0
    if args.sheet:
        print(build_review(load_drafts(args.out / "phase4-t3-drafts.json"), games, args.out))
        return 0
    arms = args.arm or list(ARMS)
    est = estimate(games)
    now = datetime.now(timezone.utc)
    print(f"T3: {len(games)} fixture finals x {len(arms)} arm(s), first drafts only, {MODEL} (counted prompts; "
          f"replies: medium {MEDIUM_REPLY[0]:,}-{MEDIUM_REPLY[1]:,} measured, low {REPLY['low'][0]:,}-"
          f"{REPLY['low'][1]:,} estimated)")
    for arm in arms:
        p, lo, hi = est[arm]
        print(f"  {arm:15} prompts {p:7,}   total {lo:7,} - {hi:7,}")
    print(f"  {'together':15} {'':15} total {sum(est[a][1] for a in arms):7,} - {sum(est[a][2] for a in arms):7,}")
    added = est["outline+medium"][0] - est["medium"][0]
    print(f"  the outline adds {added:,} prompt tokens over the {len(games)} finals ({added // len(games)} a final)")
    try:
        print(f"eval ledger: {ledger_spent(args.ledger, now):,} tokens in the last 24 h ({args.ledger})")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"eval ledger: can't read {args.ledger} ({type(exc).__name__})")
    done = Counter(d["arm"] for d in load_drafts(args.out / "phase4-t3-drafts.json").values())
    print("drafts saved: " + ", ".join(f"{a} {done[a]}/{len(games)}" for a in ARMS))
    z5 = z5_problem(args.labels, args.z5_fixtures)
    print(z5 or f"Z5 passes ({args.labels.name} on {args.z5_fixtures})")       # every z5_problem starts "Z5"
    if not args.run:
        print("dry run: nothing was sent (a real run needs --run)")
        return 0
    samples._load_keys()
    why = refusals(arms, est, args.ledger, now, args.labels, args.z5_fixtures)
    if why:
        print("refused: " + "; ".join(why))
        return 1
    return run(arms, games, args.out, args.ledger, sha256(args.labels), args.redo)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
