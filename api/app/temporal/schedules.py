"""Create or update the Temporal Schedules. Safe to rerun: an existing schedule is updated in place.

    python -m app.temporal.schedules          # create/update, then print them
    python -m app.temporal.schedules --show   # print only (step 3.2 diffs this before and after a rerun)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import timedelta

from temporalio.client import (Client, Schedule, ScheduleActionStartWorkflow, ScheduleAlreadyRunningError,
                               ScheduleCalendarSpec, ScheduleOverlapPolicy, SchedulePolicy, ScheduleRange,
                               ScheduleSpec, ScheduleUpdate)

from .workflows import TASK_QUEUE, HeadlinesWorkflow, ScheduleSyncWorkflow
from .worker import connect

TZ = "America/Los_Angeles"
LEAGUES = [x.strip() for x in os.environ.get("ENABLED_LEAGUES", "nfl").split(",") if x.strip()]

# schedule id -> (workflow, Pacific hours at minute 0)
SCHEDULES = {
    "schedule-sync": (ScheduleSyncWorkflow, [6]),
    "headlines": (HeadlinesWorkflow, [7, 17]),
}


def build(schedule_id: str) -> Schedule:
    wf, hours = SCHEDULES[schedule_id]
    return Schedule(
        action=ScheduleActionStartWorkflow(wf.run, LEAGUES, id=schedule_id, task_queue=TASK_QUEUE),
        spec=ScheduleSpec(calendars=[ScheduleCalendarSpec(hour=[ScheduleRange(h) for h in hours],
                                                          minute=[ScheduleRange(0)], second=[ScheduleRange(0)])],
                          time_zone_name=TZ),
        # Laptop or server off at run time: run once when it's back, within 12 hours. Never two at once.
        policy=SchedulePolicy(overlap=ScheduleOverlapPolicy.SKIP, catchup_window=timedelta(hours=12)),
    )


async def ensure(client: Client) -> None:
    for sid in SCHEDULES:
        schedule = build(sid)
        try:
            await client.create_schedule(sid, schedule)
            print(f"created {sid}")
        except ScheduleAlreadyRunningError:
            await client.get_schedule_handle(sid).update(lambda _inp, s=schedule: ScheduleUpdate(schedule=s))
            print(f"updated {sid}")


async def show(client: Client) -> list[dict]:
    """A stable description of each schedule (no timestamps or run counts), for diffing."""
    out = []
    for sid in SCHEDULES:
        d = await client.get_schedule_handle(sid).describe()
        s = d.schedule
        out.append({
            "id": sid,
            "workflow": s.action.workflow if isinstance(s.action, ScheduleActionStartWorkflow) else None,
            "args": [a.data.decode() for a in s.action.args] if isinstance(s.action, ScheduleActionStartWorkflow) else None,
            "task_queue": getattr(s.action, "task_queue", None),
            "time_zone": s.spec.time_zone_name,
            "calendars": [{"hour": [r.start for r in c.hour], "minute": [r.start for r in c.minute]}
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
    print(json.dumps(await show(client), indent=1, sort_keys=True))


if __name__ == "__main__":
    asyncio.run(main())
