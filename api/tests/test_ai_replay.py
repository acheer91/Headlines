"""The code checks, replayed on the two saved eval outputs (3dacc26: Sep 30 10:21 PM run, bffeb78: the rerun) against
the errors two reviewers found by hand (tests/fixtures/ai/known_errors.json). Needs the repo's git history for the
saved outputs; skipped without it."""
import subprocess

import pytest

from app.ai import replay


def _saved(ref):
    try:
        replay.saved_output(ref)
    except (subprocess.CalledProcessError, FileNotFoundError):
        pytest.skip(f"saved output at {ref} isn't available (no git history here)")


@pytest.mark.parametrize("ref,known,caught", [("3dacc26", 24, 24), ("bffeb78", 15, 13)])
def test_code_checks_catch_the_reviewed_errors_and_pass_the_clean_texts(ref, known, caught):
    _saved(ref)
    res = replay.evaluate(ref)
    assert res["known"] == known
    assert not res["unfound"]                       # every anchor is words from the text it describes
    assert res["caught"] >= caught, [(e["id"], e["anchor"]) for e in res["missed"]]
    assert not res["clean_flagged"], res["clean_flagged"]     # texts the reviewers passed are never flagged


def test_the_two_borderline_misses_are_the_known_ones():
    # "added a touchdown in the fourth" / "lone touchdown in the fourth" for 7 points: soft, borderline (reviewer).
    _saved("bffeb78")
    assert {e["id"] for e in replay.evaluate("bffeb78")["missed"]} <= {27, 32}
