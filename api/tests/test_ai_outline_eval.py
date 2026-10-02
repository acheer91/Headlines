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
                                     ({"angles": ["blowout"], "phi_chicago": "blowout"}, "no such fixture")])
def test_bad_labels_are_refused(tmp_path, bad, why):
    path = tmp_path / "labels.json"
    path.write_text(json.dumps(bad), encoding="utf-8")
    with pytest.raises(ValueError, match=why):
        angle_eval.read_labels(path)


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


def test_a_real_run_is_refused_over_the_ledger_on_weekends_or_without_120b(tmp_path, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-not-a-key")
    monkeypatch.setattr(client, "WRITERS", [])
    est = outline_eval.estimate(angle_eval.finals())
    assert outline_eval.refusals(["outline+low"], est, ledger(tmp_path, 30_000), THURSDAY) == []
    assert "under 100,000" in outline_eval.refusals(["medium"], est, ledger(tmp_path, 100_000), THURSDAY)[0]
    assert "one arm a day" in outline_eval.refusals(["medium"], est, ledger(tmp_path, 40_000), THURSDAY)[0]
    assert "one arm a day" in outline_eval.refusals(list(outline_eval.ARMS), est, ledger(tmp_path, 0), THURSDAY)[0]
    old = ledger(tmp_path, 99_000, at=datetime(2026, 9, 30, 17, tzinfo=timezone.utc))      # 25 h before
    assert outline_eval.refusals(["outline+low"], est, old, THURSDAY) == []
    assert "weekdays only" in outline_eval.refusals(["outline+low"], est, ledger(tmp_path, 0, SATURDAY), SATURDAY)[0]
    assert "can't be read" in outline_eval.refusals(["outline+low"], est, tmp_path / "missing.json", THURSDAY)[0]
    monkeypatch.setattr(client, "WRITERS", ["qwen/qwen3.8-27b"])
    assert "not openai/gpt-oss-120b" in outline_eval.refusals(["outline+low"], est, ledger(tmp_path, 0), THURSDAY)[0]
    monkeypatch.setattr(client, "WRITERS", [])
    monkeypatch.delenv("GROQ_API_KEY")
    assert outline_eval.refusals(["outline+low"], est, ledger(tmp_path, 0), THURSDAY) == ["GROQ_API_KEY is not set"]


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


def test_wilson_interval():
    assert outline_eval.wilson(0, 0) == (0.0, 1.0)
    lo, hi = outline_eval.wilson(5, 100)
    assert lo == pytest.approx(0.0215, abs=1e-3) and hi == pytest.approx(0.1118, abs=1e-3)
