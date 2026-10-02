"""Create or update the Temporal Schedules. Safe to rerun: an existing schedule is updated in place.

    python -m app.temporal.schedules          # create/update, delete stale AI schedules, then print them
    python -m app.temporal.schedules --show   # print only (step 3.2 diffs this before and after a rerun)

A stale schedule is an `ai-*` one on the server that this file no longer lists: a league left AI_LEAGUES, or AI text
was turned off. Only the `ai-` prefix is ever deleted; every other schedule is left alone.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import timedelta

from temporalio.service import RPCError, RPCStatusCode
from temporalio.client import (Client, Schedule, ScheduleActionStartWorkflow, ScheduleAlreadyRunningError,
                               ScheduleCalendarSpec, ScheduleOverlapPolicy, SchedulePolicy, ScheduleRange,
                               ScheduleSpec, ScheduleUpdate)

from ..ai import scope as ai_scope
from .workflows import TASK_QUEUE, HeadlinesWorkflow, LeftoverWorkflow, PreviewBatchWorkflow, ScheduleSyncWorkflow
from .worker import connect

TZ = "America/Los_Angeles"
LEAGUES = [x.strip() for x in os.environ.get("ENABLED_LEAGUES", "nfl").split(",") if x.strip()]

WED, THU, FRI = 3, 4, 5      # Temporal's day_of_week: 0 = Sunday


def _schedules() -> dict:
    """schedule id -> (workflow, leagues, Pacific hours, minute, days of the week or None for every day).
    Phase 4 (Adam, 2026-09-29): previews are written midweek, NCAAF Wed and Thu evening, NFL Thu and Fri
    evening; the nightly job catches up at 9:30 PM. The AI schedules cover only AI_LEAGUES (CTO, 2026-10-01: NFL
    first): a league without AI text gets no schedule that would find nothing to do. Scores, news and headlines
    stay on ENABLED_LEAGUES."""
    ai = [lg for lg in LEAGUES if ai_scope.ai_league(lg)]
    out = {
        "schedule-sync": (ScheduleSyncWorkflow, LEAGUES, [6], 0, None),
        "headlines": (HeadlinesWorkflow, LEAGUES, [7, 17], 0, None),
    }
    if ai:
        out["ai-leftover"] = (LeftoverWorkflow, ai, [21], 30, None)
    if "ncaaf" in ai:
        out["ai-previews-ncaaf"] = (PreviewBatchWorkflow, ["ncaaf"], [19], 0, [WED, THU])
    if "nfl" in ai:
        out["ai-previews-nfl"] = (PreviewBatchWorkflow, ["nfl"], [19], 0, [THU, FRI])
    return out


SCHEDULES = _schedules()


def build(schedule_id: str) -> Schedule:
    wf, leagues, hours, minute, days = SCHEDULES[schedule_id]
    cal = ScheduleCalendarSpec(hour=[ScheduleRange(h) for h in hours], minute=[ScheduleRange(minute)],
                               second=[ScheduleRange(0)],
                               **({"day_of_week": [ScheduleRange(d) for d in days]} if days else {}))
    return Schedule(
        action=ScheduleActionStartWorkflow(wf.run, leagues, id=schedule_id, task_queue=TASK_QUEUE),
        spec=ScheduleSpec(calendars=[cal], time_zone_name=TZ),
        # Laptop or server off at run time: run once when it's back, within 12 hours. Never two at once.
        policy=SchedulePolicy(overlap=ScheduleOverlapPolicy.SKIP, catchup_window=timedelta(hours=12)),
    )


AI_PREFIX = "ai-"       # the only schedules a rerun may delete: the ones this file creates for AI text


async def stale(client: Client) -> list[str]:
    """`ai-*` schedules on the server that SCHEDULES no longer lists."""
    return sorted([d.id async for d in await client.list_schedules()
                   if d.id.startswith(AI_PREFIX) and d.id not in SCHEDULES])


async def ensure(client: Client) -> None:
    for sid in SCHEDULES:
        schedule = build(sid)
        try:
            await client.create_schedule(sid, schedule)
            print(f"created {sid}")
        except ScheduleAlreadyRunningError:
            await client.get_schedule_handle(sid).update(lambda _inp, s=schedule: ScheduleUpdate(schedule=s))
            print(f"updated {sid}")
    for sid in await stale(client):
        try:
            await client.get_schedule_handle(sid).delete()
            print(f"deleted {sid} (no longer listed)")
        except RPCError as exc:
            if exc.status != RPCStatusCode.NOT_FOUND:      # already gone (the listing lags): nothing to do
                raise


async def show(client: Client) -> list[dict]:
    """A stable description of each schedule (no timestamps or run counts), for diffing."""
    out = []
    for sid in SCHEDULES:
        try:
            d = await client.get_schedule_handle(sid).describe()
        except RPCError as exc:
            if exc.status != RPCStatusCode.NOT_FOUND:
                raise
            out.append({"id": sid, "missing": True})       # not created yet: the first run of the command makes it
            continue
        s = d.schedule
        out.append({
            "id": sid,
            "workflow": s.action.workflow if isinstance(s.action, ScheduleActionStartWorkflow) else None,
            "args": [a.data.decode() for a in s.action.args] if isinstance(s.action, ScheduleActionStartWorkflow) else None,
            "task_queue": getattr(s.action, "task_queue", None),
            "time_zone": s.spec.time_zone_name,
            "calendars": [{"hour": [r.start for r in c.hour], "minute": [r.start for r in c.minute],
                           "day_of_week": [f"{r.start}-{r.end or r.start}" for r in c.day_of_week]}
                          for c in s.spec.calendars],
            "overlap": s.policy.overlap.name,
            "paused": s.state.paused,
        })
    return out


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--show", action="store_true", help="print the schedules without changing them")
    a = ap.parse_args()
    client = await connect()
    if not a.show:
        await ensure(client)
    elif old := await stale(client):
        print(f"stale, a rerun would delete: {', '.join(old)}")
    print(json.dumps(await show(client), indent=1, sort_keys=True))


if __name__ == "__main__":
    asyncio.run(main())
