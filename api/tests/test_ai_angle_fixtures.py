"""Z5's fresh-set tooling (Oct 2): angle_fixtures builds a week's finals from ESPN as recap fixtures plus a blind label
sheet, and angle_eval --fixtures scores them. Offline: ESPN is faked with saved payloads (summary_post.json is ESPN's
summary of LAC @ BUF, the game in tests/fixtures/ai/final_lac_buf.json)."""
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app import espn
from app.ai import angle_eval, angle_fixtures, facts

FIX = Path(__file__).parent / "fixtures"
LAC_BUF = json.loads((FIX / "ai" / "final_lac_buf.json").read_text(encoding="utf-8"))
SUMMARY = json.loads((FIX / "summary_post.json").read_text(encoding="utf-8"))
NOW = datetime(2026, 10, 6, 12, tzinfo=timezone.utc)


def _team(t, tid):
    return {"id": tid, "abbreviation": t["abbr"], "displayName": t["name"], "shortDisplayName": t["short"],
            "logo": t["logo"], "color": t["color"]}


def _event(espn_id, away, home, state="post", detail="Final", name="STATUS_FINAL", completed=True):
    """A scoreboard event for two of the fixture's teams (each a fixture side: abbr, name, short, logo, color)."""
    status = {"period": 4, "displayClock": "0:00",
              "type": {"state": state, "completed": completed, "shortDetail": detail, "name": name}}
    sides = [{"homeAway": "home", "team": _team(home, "2")}, {"homeAway": "away", "team": _team(away, "24")}]
    if state != "pre":
        sides[0]["score"], sides[1]["score"] = str(home.get("score") or 0), str(away.get("score") or 0)
    return {"id": espn_id, "date": "2026-09-27T17:00Z",
            "competitions": [{"competitors": sides, "status": status, "venue": {"fullName": LAC_BUF["venue"]},
                              "broadcasts": [{"names": [LAC_BUF["broadcast"]]}]}]}


NYJ = {"abbr": "NYJ", "name": "New York Jets", "short": "Jets", "logo": None, "color": None}
MIA = {"abbr": "MIA", "name": "Miami Dolphins", "short": "Dolphins", "logo": None, "color": None}


def _board(*events, week=3):
    return {"season": {"year": 2026, "type": 2}, "week": {"number": week}, "events": list(events)}


@pytest.fixture(autouse=True)
def down_switch(monkeypatch):
    """main() turns ESPN's down switch off for the run (a batch); put it back for the other tests."""
    monkeypatch.setattr(espn, "DOWN_SECONDS", espn.DOWN_SECONDS)


@pytest.fixture
def espn_week(monkeypatch):
    """Week 3 with the LAC @ BUF final, an upcoming game and a canceled one. Returns the board and the calls made."""
    board = _board(_event("401872953", LAC_BUF["away"], LAC_BUF["home"]),
                   _event("401872999", NYJ, MIA, state="pre", detail="10/4 - 1:00 PM EDT", completed=False),
                   _event("401872998", MIA, NYJ, detail="Canceled", name="STATUS_CANCELED", completed=False))
    calls = []

    def scoreboard(league, **kw):
        calls.append(("scoreboard", league, kw))
        return board

    def summary(league, event_id, **kw):
        calls.append(("summary", league, event_id))
        assert event_id == "401872953", "a summary was fetched for a game that isn't final"
        return SUMMARY

    monkeypatch.setattr(espn, "fetch_scoreboard", scoreboard)
    monkeypatch.setattr(espn, "fetch_summary", summary)
    return board, calls


def test_a_final_is_the_api_payload(tmp_path, espn_week):
    # The fixture final_lac_buf.json is GET /api/games/3 as the laptop served it on Sep 30. Built from ESPN alone it
    # is the same payload, bets included, but for the database id and when the summary was fetched.
    _, calls = espn_week
    out = tmp_path / "wk3"
    m = angle_fixtures.build(2026, 3, out, now=NOW)
    got = json.loads((out / "final_lac_buf.json").read_text(encoding="utf-8"))
    assert {k for k in set(got) | set(LAC_BUF) if got.get(k) != LAC_BUF.get(k)} == {"id", "summary_updated_at"}
    assert got["id"] is None and got["summary_updated_at"] == NOW.isoformat()
    assert facts.recap_facts(got) == facts.recap_facts(LAC_BUF)
    assert facts.recap_outline(got) == facts.recap_outline(LAC_BUF)
    assert calls == [("scoreboard", "nfl", {"week": 3, "season_type": 2, "dates": "2026", "base_url": espn.BASE}),
                     ("summary", "nfl", "401872953")]
    assert [(f["fixture"], f["espn_id"], f["game"]) for f in m["finals"]] == [("final_lac_buf", "401872953",
                                                                              "LAC @ BUF")]
    assert m["finals"][0]["sha256"] == angle_fixtures.sha256(out / "final_lac_buf.json")
    assert m["skipped"] == [{"game": "MIA @ NYJ", "espn_id": "401872998", "why": "not played (Canceled)"},
                            {"game": "NYJ @ MIA", "espn_id": "401872999", "why": "not final (10/4 - 1:00 PM EDT)"}]
    assert m["facts_py_sha256"] == angle_fixtures.sha256(Path(facts.__file__))
    assert json.loads((out / "manifest.json").read_text(encoding="utf-8")) == m
    assert sorted(p.name for p in out.iterdir()) == ["final_lac_buf.json", "labels_sheet.md", "manifest.json"]


def test_the_label_sheet_is_blind_and_its_reply_scores(tmp_path, espn_week, capsys):
    out, sheet_path = tmp_path / "wk3", tmp_path / "sheets" / "z5_sheet.md"
    sheet_path.parent.mkdir()
    angle_fixtures.build(2026, 3, out, sheet_path, now=NOW)
    assert not (out / "labels_sheet.md").exists()                  # --labels-sheet put it elsewhere
    sheet = sheet_path.read_text(encoding="utf-8")
    game = angle_eval.finals(out)["lac_buf"]
    assert "## lac_buf: Los Angeles Chargers at Buffalo Bills" in sheet
    assert all(f"- {line}\n" in sheet for line in facts.recap_facts(game)["facts"])
    assert all(f"- `{k}`: {v}\n" in sheet for k, v in facts.ANGLES.items())
    # Blind: nothing of code's pick. Not its frame or lines as an outline, and the ids come alphabetically, not in
    # the order the rules try them.
    plan = facts.recap_outline(game)
    assert plan["frame"] not in sheet and "OUTLINE" not in sheet and "Angle:" not in sheet
    listed = [k for k in sheet.split("## Angles")[1].split("## Reply")[0].split("`")[1::2]]
    assert listed == sorted(facts.ANGLES) != list(facts.ANGLES)
    # The reply block is a labels file angle_eval reads as it is, once each "<one id>" is filled in.
    reply = json.loads(sheet.split("```json\n")[1].split("```")[0])
    assert reply == {"angles": sorted(facts.ANGLES), "lac_buf": "<one id>"}
    labels = tmp_path / "labels.json"
    labels.write_text(json.dumps(reply | {"lac_buf": plan["angle"]}), encoding="utf-8")
    assert angle_eval.main(["--fixtures", str(out), "--labels", str(labels)]) == 0
    assert "code matched 1/1 labelled finals (Z5 bar: 1 of 1): PASS" in capsys.readouterr().out


def test_nothing_is_written_under_tests(monkeypatch, capsys):
    def no_espn(*a, **kw):
        raise AssertionError("ESPN was called")
    monkeypatch.setattr(espn, "fetch_scoreboard", no_espn)
    where = angle_fixtures.TESTS / "fixtures" / "z5_week"
    with pytest.raises(ValueError, match="under tests/"):
        angle_fixtures.build(2026, 4, where)
    assert angle_fixtures.main(["--season", "2026", "--week", "4", "--out", str(where)]) == 2
    assert "under tests/" in capsys.readouterr().err and not where.exists()


def test_a_folder_with_other_games_is_refused(tmp_path, espn_week):
    out = tmp_path / "wk3"
    out.mkdir()
    (out / "final_phi_chi.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="other games' fixtures \\(final_phi_chi.json\\)"):
        angle_fixtures.build(2026, 3, out, now=NOW)
    assert sorted(p.name for p in out.iterdir()) == ["final_phi_chi.json"]
    angle_fixtures.build(2026, 3, tmp_path / "again", now=NOW)
    angle_fixtures.build(2026, 3, tmp_path / "again", now=NOW)      # a rerun of the same week overwrites its own


@pytest.mark.parametrize("change,why", [
    (lambda b: b["events"].append({"id": "401872997", "competitions": [{}]}), "don't parse"),
    (lambda b: b.update(week={"number": 4}), r"not \(2026, 3, 2\)"),
    (lambda b: b.update(events=[]), "no games"),
])
def test_a_week_that_cant_be_read_whole_writes_nothing(tmp_path, espn_week, change, why):
    change(espn_week[0])
    with pytest.raises(ValueError, match=why):
        angle_fixtures.build(2026, 3, tmp_path / "wk3", now=NOW)
    assert not (tmp_path / "wk3").exists()


def test_a_summary_for_another_game_stops_the_run(tmp_path, espn_week, monkeypatch):
    other = json.loads(json.dumps(SUMMARY))
    other["header"]["id"] = "401872000"
    monkeypatch.setattr(espn, "fetch_summary", lambda league, event_id, **kw: other)
    with pytest.raises(espn.ESPNError, match="summary for 401872000 returned for game 401872953"):
        angle_fixtures.build(2026, 3, tmp_path / "wk3", now=NOW)
    assert not (tmp_path / "wk3").exists()


def test_a_summary_still_live_is_skipped(tmp_path, espn_week, monkeypatch, capsys):
    live = json.loads(json.dumps(SUMMARY))
    live["header"]["competitions"][0]["status"]["type"].update(state="in", completed=False)
    monkeypatch.setattr(espn, "fetch_summary", lambda league, event_id, **kw: live)
    m = angle_fixtures.build(2026, 3, tmp_path / "wk3", now=NOW)
    assert m["finals"] == [] and m["sheet"] is None
    assert m["skipped"][0] == {"game": "LAC @ BUF", "espn_id": "401872953",
                               "why": "ESPN's summary isn't final yet: run again"}
    assert not list((tmp_path / "wk3").glob("final_*.json")) and not (tmp_path / "wk3" / "labels_sheet.md").exists()


def test_main_lists_the_finals_and_the_skipped(tmp_path, espn_week, capsys):
    out = tmp_path / "wk3"
    assert angle_fixtures.main(["--season", "2026", "--week", "3", "--out", str(out)]) == 0
    printed = capsys.readouterr().out
    assert "NFL 2026 week 3: 1 finals written" in printed and "final_lac_buf.json  LAC @ BUF" in printed
    assert "skipped NYJ @ MIA (ESPN 401872999): not final" in printed and "labels_sheet.md" in printed
    espn_week[0]["events"] = espn_week[0]["events"][1:]                 # only the upcoming and canceled games
    assert angle_fixtures.main(["--season", "2026", "--week", "3", "--out", str(tmp_path / "early")]) == 1
    assert "no finals yet" in capsys.readouterr().out


# ---------- angle_eval --fixtures ----------

def test_angle_eval_scores_another_folder(tmp_path, capsys):
    names = ["final_ne_jax", "final_sea_wsh", "final_lac_buf"]
    for n in names:
        (tmp_path / f"{n}.json").write_bytes((FIX / "ai" / f"{n}.json").read_bytes())
    (tmp_path / "manifest.json").write_text("{}", encoding="utf-8")           # only final_*.json are finals
    assert list(angle_eval.finals(tmp_path)) == ["lac_buf", "ne_jax", "sea_wsh"]
    assert angle_eval.angles_file(tmp_path)["fixtures"] == ["lac_buf", "ne_jax", "sea_wsh"]
    assert len(angle_eval.finals()) == 16                                     # the default is unchanged
    labels = tmp_path / "labels.json"
    labels.write_text(json.dumps({"angles": list(facts.ANGLES), "ne_jax": "blowout", "sea_wsh": "close_finish",
                                  "lac_buf": "late_lead_change"}), encoding="utf-8")
    rows = angle_eval.evaluate(angle_eval.read_labels(labels, tmp_path), tmp_path)
    assert [(r["game"], r["match"]) for r in rows] == [("lac_buf", False), ("ne_jax", True), ("sea_wsh", True)]
    assert not angle_eval.passed(rows)                                        # 2 of 3 is under 85%
    assert angle_eval.main(["--fixtures", str(tmp_path), "--labels", str(labels)]) == 0
    assert "code matched 2/3 labelled finals (Z5 bar: 3 of 3): FAIL" in capsys.readouterr().out
    # A label for a final that isn't in the folder is refused, as for the 16.
    labels.write_text(json.dumps({"angles": list(facts.ANGLES), "phi_chi": "blowout"}), encoding="utf-8")
    with pytest.raises(ValueError, match="no such fixture final"):
        angle_eval.read_labels(labels, tmp_path)
    assert angle_eval.read_labels(labels) == {"phi_chi": "blowout"}
    empty = tmp_path / "empty"
    empty.mkdir()
    assert angle_eval.main(["--fixtures", str(empty)]) == 2
    assert "no final_*.json" in capsys.readouterr().err


def test_the_bar_is_85_percent_rounded_up():
    assert angle_eval.PASS_BAR == angle_eval.bar(16) == 14
    assert [angle_eval.bar(n) for n in (1, 13, 14, 15, 20)] == [1, 12, 12, 13, 17]
    assert not angle_eval.passed([])
    row = lambda match: {"label": "blowout", "match": match}
    assert angle_eval.passed([row(True)] * 13 + [row(False)] * 2)            # 13 of 15
    assert not angle_eval.passed([row(True)] * 12 + [row(False)] * 3)        # 12 of 15
