"""The activity that starts GameWorkflows. A workflow can't use a Temporal client, so ScheduleSync calls this.
Its own file because it imports the workflows, and workflows.py imports the activities."""
from __future__ import annotations

from temporalio import activity
from temporalio.client import Client
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError

from .models import GameInput, GameRef, Times
from .workflows import TASK_QUEUE, GameWorkflow


def workflow_id(league: str, espn_id: str) -> str:
    return f"{league}-{espn_id}"


class Starter:
    def __init__(self, client: Client):
        self.client = client

    @activity.defn(name="ensure_game_workflows")
    async def ensure_game_workflows(self, games: list[GameRef]) -> int:
        """Returns how many games now have a running workflow (new or already running)."""
        running = 0
        for g in games:
            try:
                # Signal-with-start: starts the workflow if it isn't running, otherwise only delivers the
                # reschedule signal with the latest times (a no-op in the workflow when nothing changed).
                await self.client.start_workflow(
                    GameWorkflow.run,
                    GameInput(g.league, g.espn_id, g.start_iso, g.preview_iso),
                    id=workflow_id(g.league, g.espn_id),
                    task_queue=TASK_QUEUE,
                    id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY,
                    start_signal="reschedule",
                    start_signal_args=[Times(g.start_iso, g.preview_iso)],
                )
                running += 1
            except WorkflowAlreadyStartedError:
                pass  # this game's workflow already completed; never reopen it
        activity.logger.info("%d of %d games have a running workflow", running, len(games))
        return running
