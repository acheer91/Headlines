"""The Z5 and T3 harnesses (M3, 2026-10-02) with a fake model: no tokens, no network, no database."""
import csv
import json
from datetime import datetime, timezone

import pytest

from app.ai import angle_eval, client, facts, outline_eval, review_sheet

WORDS = " ".join(["word"] * 70)
THURSDAY = datetime(2026, 10, 1, 18, tzinfo=timezone.utc)        # 11 AM Pacific
SATURDAY = datetime(2026, 10, 3, 18, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def no_model(monkeypatch):
    """Any model call fails the test unless a test puts a fake in."""
    def refuse(*a, **kw):
        raise AssertionError("a model was called")
    monkeypatch.setattr(client, "write", refuse)
    monkeypatch.setattr(client, "check", refuse)


# ---------- Z5 ----------

def test_the_labeller_gets_ids_and_meanings_only(tmp_path):
    out = tmp_path / "z5_angles.json"
    assert angle_eval.main(["--angles-out", str(out)]) == 0
    sent = json.loads(out.read_text(encoding="utf-8"))
    assert [a["id"] for a in sent["angles"]] == sorted(facts.ANGLES)            # alphabetical: not the priority
    assert set(sent["angles"][0]) == {"id", "meaning"} and len(sent["fixtures"]) == 16
    assert not any(str(n) in json.dumps(sent["angles"]) for n in (facts.COMEBACK_POINTS, facts.OUTGAINED_YARDS,
                                                                  facts.BLOWOUT_POINTS))


def test_labels_are_matched_and_counted(tmp_path, capsys):
    labels = {"angles": list(facts.ANGLES)} | {g: facts.recap_outline(f)["angle"]
                                                for g, f in angle_eval.finals().items()}
    labels["phi_chi"], labels["final_sea_wsh"] = "turnovers", "outgained_but_lost"   # one miss; the prefix is fine
    path = tmp_path / "labels.json"
    path.write_text(json.dumps(labels), encoding="utf-8")
    rows = angle_eval.evaluate(angle_eval.read_labels(path))
    assert sum(r["match"] for r in rows) == 15
    assert angle_eval.main(["--labels", str(path)]) == 0
    assert "code matched 15/16 labelled finals (Z5 bar: 14 of 16): PASS" in capsys.readouterr().out


@pytest.mark.parametrize("bad,why", [({"phi_chi": "blowout"}, "angles"),
                                     ({"angles": ["blowout", "upset"], "phi_chi": "blowout"}, "doesn't know"),
                                     ({"angles": ["blowout"], "phi_chi": "comeback"}, "outside"),
                                     ({"angles": ["blowout"], "phi_chicago": "blowout"}, "no such fixture"),
                                     ({"angles": [["blowout"]], "phi_chi": "blowout"}, "angle ids"),
                                     ({"angles": [{"meaning": "x"}], "phi_chi": "blowout"}, "angle ids"),
                                     (["blowout"], "JSON object")])
def test_bad_labels_are_refused(tmp_path, capsys, bad, why):
    path = tmp_path / "labels.json"
    path.write_text(json.dumps(bad), encoding="utf-8")
    with pytest.raises(ValueError, match=why):
        angle_eval.read_labels(path)
    assert angle_eval.main(["--labels", str(path)]) == 2 and "labels: " in capsys.readouterr().err   # no traceback


def test_labels_may_keep_the_angles_files_id_and_meaning_objects(tmp_path):
    # The labeller is sent angles_file(); one that keeps its {id, meaning} list must not crash read_labels.
    path = tmp_path / "labels.json"
    path.write_text(json.dumps({"angles": angle_eval.angles_file()["angles"], "phi_chi": "blowout"}), encoding="utf-8")
    assert angle_eval.read_labels(path) == {"phi_chi": "blowout"}


# ---------- T3 ----------

def ledger(tmp_path, tokens, at=THURSDAY):
    path = tmp_path / "phase4_tokens.json"
    path.write_text(json.dumps([{"at": at.isoformat(), "tokens": tokens, "run": "x"}]), encoding="utf-8")
    return path


def test_the_dry_run_is_the_default_and_sends_nothing(tmp_path, capsys):
    assert outline_eval.main(["--out", str(tmp_path), "--ledger", str(ledger(tmp_path, 0))]) == 0
    out = capsys.readouterr().out
    assert "dry run: nothing was sent" in out and "outline+low" in out
    assert not (tmp_path / "phase4-t3-drafts.json").exists()


def test_the_estimate_counts_the_outline_and_low_reasoning():
    games = angle_eval.finals()
    est = outline_eval.estimate(games)
    today = sum(client.prompt_tokens(outline_eval.MODEL, outline_eval.writer.recap_prompt(g)) for g in games.values())
    assert est["outline+medium"][0] > est["medium"][0] == today
    assert est["outline+low"][0] == est["outline+medium"][0] and est["outline+low"][2] < est["outline+medium"][1]
    assert all(hi < outline_eval.DAY_CAP for _, _, hi in est.values())          # one arm fits a day


def z5_labels(tmp_path, misses=0):
    """A Z5 labels file: code's own angle for every final, with `misses` of them labelled otherwise."""
    labels = {g: facts.recap_outline(f)["angle"] for g, f in angle_eval.finals().items()}
    for g in list(labels)[:misses]:
        labels[g] = "routine_win" if labels[g] != "routine_win" else "blowout"
    path = tmp_path / f"z5_{misses}.json"
    path.write_text(json.dumps({"angles": list(facts.ANGLES)} | labels), encoding="utf-8")
    return path


def test_a_real_run_is_refused_over_the_ledger_on_weekends_or_without_120b(tmp_path, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-not-a-key")
    monkeypatch.setattr(client, "WRITERS", [])
    est = outline_eval.estimate(angle_eval.finals())
    z5 = z5_labels(tmp_path)
    no = lambda arms, path, now=THURSDAY: outline_eval.refusals(arms, est, path, now, z5)
    assert no(["outline+low"], ledger(tmp_path, 30_000)) == []
    assert "under 100,000" in no(["medium"], ledger(tmp_path, 100_000))[0]
    assert "one arm a day" in no(["medium"], ledger(tmp_path, 40_000))[0]
    assert "one arm a day" in no(list(outline_eval.ARMS), ledger(tmp_path, 0))[0]
    old = ledger(tmp_path, 99_000, at=datetime(2026, 9, 30, 17, tzinfo=timezone.utc))      # 25 h before
    assert no(["outline+low"], old) == []
    assert "weekdays only" in no(["outline+low"], ledger(tmp_path, 0, SATURDAY), SATURDAY)[0]
    assert "can't be read" in no(["outline+low"], tmp_path / "missing.json")[0]
    monkeypatch.setattr(client, "WRITERS", ["qwen/qwen3.8-27b"])
    assert "not openai/gpt-oss-120b" in no(["outline+low"], ledger(tmp_path, 0))[0]
    monkeypatch.setattr(client, "WRITERS", [])
    monkeypatch.delenv("GROQ_API_KEY")
    assert no(["outline+low"], ledger(tmp_path, 0)) == ["GROQ_API_KEY is not set"]


def test_a_real_run_waits_for_z5(tmp_path, monkeypatch):
    # Review (Oct 2): Z5 failed 11/16 and nothing stopped a ~200K T3 run on an outline that couldn't ship.
    monkeypatch.setenv("GROQ_API_KEY", "test-not-a-key")
    monkeypatch.setattr(client, "WRITERS", [])
    est, book = outline_eval.estimate(angle_eval.finals()), ledger(tmp_path, 0)
    assert "Z5 hasn't passed" in outline_eval.refusals(["outline+low"], est, book, THURSDAY)[0]
    assert outline_eval.refusals(["outline+low"], est, book, THURSDAY, z5_labels(tmp_path, misses=2)) == []   # 14/16
    why = outline_eval.refusals(["outline+low"], est, book, THURSDAY, z5_labels(tmp_path, misses=5))
    assert why == ["Z5 fails on z5_5.json: code matched 11/16 labelled finals (needs 14 of 16); after a change to "
                   "the angle rules, label a fresh set"]
    partial = tmp_path / "partial.json"
    partial.write_text(json.dumps({"angles": ["blowout"], "phi_chi": "blowout"}), encoding="utf-8")
    assert "Z5 fails" in outline_eval.refusals(["outline+low"], est, book, THURSDAY, partial)[0]
    assert "Z5 labels" in outline_eval.refusals(["outline+low"], est, book, THURSDAY, tmp_path / "none.json")[0]
    calls = []
    monkeypatch.setattr(outline_eval, "run", lambda *a, **kw: calls.append(a) or 0)
    monkeypatch.setattr(outline_eval.samples, "_load_keys", lambda: None)
    argv = ["--run", "--arm", "outline+low", "--out", str(tmp_path), "--ledger", str(book)]
    monkeypatch.setattr(outline_eval, "datetime", type("D", (datetime,), {"now": staticmethod(lambda tz=None: THURSDAY)}))
    assert outline_eval.main(argv) == 1 and calls == []                                   # no labels: refused
    assert outline_eval.main(argv + ["--labels", str(z5_labels(tmp_path, misses=5))]) == 1 and calls == []
    labels = z5_labels(tmp_path)
    assert outline_eval.main(argv + ["--labels", str(labels)]) == 0
    assert calls[0][4] == outline_eval.sha256(labels)                                     # the run records the labels


def fake_writer(monkeypatch, reply):
    seen = []
    monkeypatch.setattr(client, "tokens_used", 0)

    def write(prompt, json_out=False, light=False, reasoning=None):
        seen.append((prompt, reasoning))
        client.tokens_used += 1000
        return json.dumps(reply) if isinstance(reply, dict) else reply
    monkeypatch.setattr(client, "write", write)
    return seen


def test_a_first_draft_is_written_once_at_the_arms_effort_and_code_checked(monkeypatch):
    g = angle_eval.finals()["lar_den"]
    seen = fake_writer(monkeypatch, {"recap": "Denver won 30-26. " + WORDS, "home": "Broncos.", "away": "Rams."})
    e = outline_eval.draft("outline+low", g)
    assert len(seen) == 1 and seen[0][1] == "low" and "OUTLINE" in seen[0][0]
    assert (e["status"], e["code_check"], e["angle"], e["tokens"]) == ("ready", "pass", "comeback", 1000)
    seen = fake_writer(monkeypatch, {"recap": "The Broncos dominated. " + WORDS, "home": "x", "away": "y"})
    e = outline_eval.draft("medium", g)
    assert seen[0][1] == "medium" and "OUTLINE" not in seen[0][0]
    assert e["status"] == "ready" and "dominated" in e["code_check"]          # logged, never rewritten
    fake_writer(monkeypatch, "not json")
    assert outline_eval.draft("medium", g)["reason"] == "reply was not JSON"


def test_the_review_file_is_blind_and_review_sheet_reads_it(tmp_path, monkeypatch):
    games = dict(list(angle_eval.finals().items())[:2])
    fake_writer(monkeypatch, {"recap": "First line.\n\nThe Teams played. " + WORDS, "home": "Home day.",
                              "away": "Away day."})
    drafts = {f"{arm}|{name}": outline_eval.draft(arm, g) | {"game": name}
              for arm in outline_eval.ARMS for name, g in games.items()}
    outline_eval.build_review(drafts, games, tmp_path)
    md = (tmp_path / "phase4-t3-review.md").read_text(encoding="utf-8")
    key = json.loads((tmp_path / "phase4-t3-key.json").read_text(encoding="utf-8"))
    assert len(key) == 6 and not any(arm in md for arm in outline_eval.ARMS) and "code_check" not in md
    rows, skipped = review_sheet.rows(md)
    assert not skipped and {r[2][-6:-1] for r in rows} == set(key)              # every text, by its code
    assert any(r[4] == "First line." for r in rows)                              # a recap's first paragraph too
    assert sum(r[3] == "recap" for r in rows) == 6 * 3                           # 3 sentences a recap

    # Reviewers fill the sheet; the tally splits it by arm.
    with (tmp_path / "phase4-t3-review-sheet.csv").open(encoding="utf-8-sig", newline="") as f:
        filled = list(csv.reader(f))
    for r in filled[1:]:
        arm = key[r[2][-6:-1]]["arm"]
        wrong = arm == "outline+low" and r[3] == "recap"
        r[6], r[7], r[8] = ("wrong", "wrong team", "Rams for Broncos") if wrong else ("ok", "", "")
    with (tmp_path / "filled.csv").open("w", encoding="utf-8-sig", newline="") as f:
        csv.writer(f).writerows(filled)
    out = outline_eval.tally(tmp_path / "filled.csv", key)
    assert "outline+medium - medium: +0.0%" in out and "not higher" in out
    assert "outline+low - medium" in out and "HIGHER than medium" in out and "HAS wrong-team" in out
    assert "outline+medium - medium: +0.0% (" in out and "claims -> pass" in out and out.endswith(
        "T3: FAIL (pass: both outline arms pass; otherwise drop M3b)")
    assert outline_eval.REVIEW_NOTE in md and "wrong quarter / order in a quarter" in filled[0][7]


def _sheet(tmp_path, rows):
    """A filled T3 sheet: rows of (code, verdict, cause, note)."""
    path = tmp_path / "filled.csv"
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["id", "section", "text", "part", "claim", "facts", "verdict", "cause", "note"])
        w.writerows([[i, "Recaps", f"Final: A 1, B 2 ({c})", "recap", "x", "", v, cause, note]
                     for i, (c, v, cause, note) in enumerate(rows, 1)])
    return path


KEY = {"T3-01": {"arm": "medium", "status": "ready"}, "T3-02": {"arm": "outline+medium", "status": "ready"},
       "T3-03": {"arm": "outline+low", "status": "ready"}, "T3-04": {"arm": "medium", "status": "failed"}}


def test_tally_gives_no_verdict_before_medium_is_reviewed(tmp_path):
    # Review (Oct 2): with medium unreviewed, wilson(0, 0) made "not higher" and both outline arms read "pass".
    out = outline_eval.tally(_sheet(tmp_path, [("T3-02", "ok", "", "")] * 10 + [("T3-03", "ok", "", "")] * 10), KEY)
    assert "-> pass" not in out and out.count("-> not reviewed yet") == 2
    assert "medium: 0 reviewed claims, 1 ready draft(s) with none" in out and "T3: not reviewed yet" in out
    full = [("T3-01", "ok", "", "")] * 10 + [("T3-02", "ok", "", "")] * 10 + [("T3-03", "ok", "", "")] * 10
    out = outline_eval.tally(_sheet(tmp_path, full), KEY)                      # T3-04 failed: nothing to review
    assert out.count("-> pass") == 2 and "T3: pass" in out
    out = outline_eval.tally(_sheet(tmp_path, full + [("T3-01", "wrong", "", "")]), KEY)     # an error with no cause
    assert out.count("-> not reviewed yet") == 2 and "1 error(s) without a T3 cause" in out


def test_tally_counts_the_cause_column_not_the_note(tmp_path):
    base = [("T3-01", "ok", "", "")] * 10 + [("T3-02", "ok", "", "")] * 10 + [("T3-03", "ok", "", "")] * 9
    # A wrong quarter given as the general sheet's "temporal claim", with no note: counted (it was a false pass).
    out = outline_eval.tally(_sheet(tmp_path, base + [("T3-03", "wrong", "temporal claim", "")]), KEY)
    assert "outline+low - medium" in out and "HAS wrong-team or wrong-quarter claims -> FAIL" in out
    # A wrong team whose note names a quarter is a wrong team, not also a wrong quarter.
    out = outline_eval.tally(_sheet(tmp_path, base + [("T3-03", "wrong", "wrong team", "third-quarter score")]), KEY)
    row = next(line for line in out.splitlines() if line.startswith("outline+low "))
    assert row.split()[-3:] == ["1", "0", "0"]                      # wrong team, wrong quarter, unreviewed drafts
    # Order inside a quarter is its own cause: an error, but not a wrong quarter.
    out = outline_eval.tally(_sheet(tmp_path, base + [("T3-03", "wrong", "order in a quarter", "")]), KEY)
    assert "outline+low - medium" in out and "no wrong-team or wrong-quarter claims" in out.splitlines()[-2]


def test_a_stopped_run_keeps_its_drafts_and_the_next_one_picks_up(tmp_path, monkeypatch):
    # Review (Oct 2): run() drafted saved finals again and was untested.
    games = dict(list(angle_eval.finals().items())[:4])
    names = list(games)
    book = ledger(tmp_path, 1000)
    first = {"arm": "outline+low", "game": names[0], "status": "ready", "tokens": 3000,
             "body": {"recap": "Rams won. " + WORDS, "home": "x", "away": "y"}}
    outline_eval.save_json(tmp_path / "phase4-t3-drafts.json", {f"outline+low|{names[0]}": first})
    asked = []

    def draft(arm, game):
        asked.append(next(n for n, g in games.items() if g is game))
        if len(asked) == 3:
            raise client.RateLimited("120b: today's budget is spent", 3600)
        return {"arm": arm, "status": "failed", "reason": "x", "tokens": 3000}
    monkeypatch.setattr(outline_eval, "draft", draft)
    assert outline_eval.run(["outline+low"], games, tmp_path, book, "abc") == 1
    assert asked == names[1:]                                                  # the saved final was skipped
    saved = outline_eval.load_drafts(tmp_path / "phase4-t3-drafts.json")
    assert sorted(saved) == sorted(f"outline+low|{n}" for n in names[:3]) and saved[f"outline+low|{names[1]}"]["z5"] == "abc"
    entries = json.loads(book.read_text(encoding="utf-8"))
    assert entries[-1]["tokens"] == 6000 and entries[-1]["z5_labels_sha256"] == "abc"
    asked.clear()
    monkeypatch.setattr(outline_eval, "draft", lambda arm, game: asked.append(game) or {"arm": arm, "status": "failed",
                                                                                         "tokens": 10})
    assert outline_eval.run(["outline+low"], games, tmp_path, book, "abc", redo=True) == 0 and len(asked) == 4


def test_wilson_interval():
    assert outline_eval.wilson(0, 0) == (0.0, 1.0)
    lo, hi = outline_eval.wilson(5, 100)
    assert lo == pytest.approx(0.0215, abs=1e-3) and hi == pytest.approx(0.1118, abs=1e-3)
