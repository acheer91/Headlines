"""Replay saved GameWorkflow histories against the current workflow code (step 3.12). A change to workflow
logic that isn't wrapped in workflow.patched() makes replay fail here, before it can break running games.

Fixtures in tests/fixtures/histories/: synthetic ones from the time-skipping tests
(SAVE_HISTORIES=1 pytest tests/test_workflows.py) and real games exported from the server with
    docker compose exec temporal-admin-tools temporal workflow show -w nfl-<espn_id> -o json > <file>.json
"""
from pathlib import Path

import pytest
from temporalio.client import WorkflowHistory
from temporalio.worker import Replayer

from app.temporal.workflows import WORKFLOWS

pytestmark = pytest.mark.asyncio
HISTORIES = sorted((Path(__file__).parent / "fixtures" / "histories").glob("*.json"))


@pytest.mark.skipif(not HISTORIES, reason="no saved histories")
@pytest.mark.parametrize("path", HISTORIES, ids=lambda p: p.stem)
async def test_replay(path):
    history = WorkflowHistory.from_json(path.stem, path.read_text(encoding="utf-8"))
    await Replayer(workflows=WORKFLOWS).replay_workflow(history)
