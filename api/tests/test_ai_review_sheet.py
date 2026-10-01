"""The claim-by-claim review sheet, built from the saved Sep 30 eval output (3dacc26). Skipped without git history."""
import csv
import re
import subprocess

import pytest

from app.ai import replay, review_sheet


@pytest.fixture(scope="module")
def md():
    try:
        return replay.saved_output("3dacc26")
    except (subprocess.CalledProcessError, FileNotFoundError):
        pytest.skip("saved output at 3dacc26 isn't available (no git history here)")


def test_one_row_per_claim_failed_texts_skipped(md):
    data, skipped = review_sheet.rows(md)
    assert len(data) > 150 and all(len(r) == len(review_sheet.COLUMNS) for r in data)
    assert [r[0] for r in data[:3]] == ["1", "2", "3"]
    assert {r[1] for r in data} == {"Previews", "Recaps", "Headlines"}
    assert any("Seattle Seahawks 31" in s for s in skipped) and any("no_sources" in s for s in skipped)
    assert not any("Seattle" in r[2] for r in data)            # a failed text shows the fallback line: not reviewed
    assert all(r[6] == r[7] == r[8] == "" for r in data)     # verdict, cause and note are the reviewers' to fill


def test_claims_come_with_the_facts_or_the_article_they_rest_on(md):
    data, _ = review_sheet.rows(md)
    swift = next(r for r in data if "128 rushing yards from D'Andre Swift" in r[4])
    assert "Swift, RB: 20 CAR, 84 YDS" in swift[5] and "Total yards" not in swift[5]        # the player's own line
    edge = next(r for r in data if r[3].endswith("edges") and "79 points" in r[4])
    assert edge[5].startswith("https://www.espn.com/")                                       # an edge cites its article
    assert next(r for r in data if r[1] == "Headlines")[5].startswith("https://")


def test_write_and_tally(md, tmp_path):
    out = tmp_path / "sheet.csv"
    n, _ = review_sheet.write_sheet(md, out)
    rows = list(csv.reader(out.open(encoding="utf-8-sig")))
    assert len(rows) == n + 1 and rows[0][6].startswith("verdict") and rows[0][7].startswith("cause")
    for r, (verdict, cause) in zip(rows[1:5], [("wrong", "temporal claim"), ("unsupported", "invented cause"),
                                               ("ok", ""), ("wrong", "temporal claim")]):
        r[6], r[7] = verdict, cause
    csv.writer(out.open("w", encoding="utf-8-sig", newline="")).writerows(rows)
    text = review_sheet.tally(out)
    assert text.startswith("3 errors in 1 text(s); 4 of ") and re.search(r"temporal claim\s+2", text) and re.search(r"invented cause\s+1", text)
