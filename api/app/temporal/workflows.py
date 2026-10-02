"""Temporal workflows. They decide and wait; every network or database call is an activity.

Determinism rules (replay re-runs this code against the recorded history): no datetime.now(), time,
random, network, database, files, environment or zoneinfo here; use workflow.now() and workflow timers.
Times arrive as UTC ISO strings. Once workflows are running, wrap every change to this file's logic in
workflow.patched() and run the replay test (tests/test_replay.py) before deploying.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import ActivityError, WorkflowAlreadyStartedError
from temporalio.workflow import ParentClosePolicy

with workflow.unsafe.imports_passed_through():
    from . import activities as act
    from .models import GameInput, GameRef, GameState, TextJob, Times

__all__ = ["GameWorkflow", "GameInput", "ScheduleSyncWorkflow", "HeadlinesWorkflow", "WriteTextWorkflow",
           "PreviewBatchWorkflow", "LeftoverWorkflow", "WORKFLOWS"]

TASK_QUEUE = "scores"
AI_QUEUE = "ai"        # Phase 4: AI text activities, AI_AT_ONCE (1) at a time, never delaying ESPN work

# ESPN policy: 30 s per attempt, retries from 10 s doubling to a 10-minute cap, giving up after 6 hours.
ESPN_POLICY = dict(
    start_to_close_timeout=timedelta(seconds=30),
    schedule_to_close_timeout=timedelta(hours=6),
    retry_policy=RetryPolicy(initial_interval=timedelta(seconds=10), backoff_coefficient=2.0,
                             maximum_interval=timedelta(minutes=10)),
)
# Grading: 5 attempts, then the workflow fails (loudly: it shows as Failed in the UI).
GRADE_POLICY = dict(
    start_to_close_timeout=timedelta(seconds=60),
    retry_policy=RetryPolicy(initial_interval=timedelta(seconds=10), backoff_coefficient=2.0,
                             maximum_interval=timedelta(minutes=5), maximum_attempts=5),
)
NO_RETRY = dict(start_to_close_timeout=timedelta(seconds=30), retry_policy=RetryPolicy(maximum_attempts=1))
# One AI text: a write can wait minutes for quota. A rate limit retries after the wait Groq names (the activity sets
# next_retry_delay); anything else backs off from 5 minutes. Up to 8 tries over at most 12 hours.
TEXT_POLICY = dict(
    start_to_close_timeout=timedelta(minutes=15),
    schedule_to_close_timeout=timedelta(hours=12),
    retry_policy=RetryPolicy(initial_interval=timedelta(minutes=5), backoff_coefficient=2.0,
                             maximum_interval=timedelta(hours=3), maximum_attempts=8),
)
# Is this final on the pre-write list? A database read; if it keeps failing the recap is skipped here and the nightly
# job (or the page open) writes it.
RECAP_DUE_POLICY = dict(
    start_to_close_timeout=timedelta(seconds=30),
    retry_policy=RetryPolicy(initial_interval=timedelta(seconds=10), maximum_attempts=3),
)
BATCH_AHEAD = timedelta(days=6)           # a midweek batch covers games through the following Monday night
LEFTOVER_PREVIEWS = timedelta(days=3)     # the nightly job: previews for the next 3 days ...
LEFTOVER_RECAPS = timedelta(days=2)       # ... and recaps for the last 2

LINE_EVERY = timedelta(days=1)            # save the line on first sight, then daily ...
LAST_LINE_BEFORE = timedelta(minutes=30)  # ... and a last time 30 minutes before kickoff
PREVIEW_LEAD = timedelta(hours=1)         # an early kickoff (London, 6:30 AM PT) moves the 8 AM preview ahead of it
WATCH_EVERY = timedelta(minutes=2.5)      # status polls after kickoff (Adam, 2026-09-28: cheap, so often) ...
WATCH_SLOW_AFTER = timedelta(hours=8)     # ... hourly after 8 hours ...
WATCH_SLOW_EVERY = timedelta(hours=1)
WATCH_GIVE_UP = timedelta(days=7)         # ... and closed as unresolved after 7 days
POSTPONED_WAIT = timedelta(days=14)       # a postponed game waits this long for a new date
REGRADE_AFTER = timedelta(hours=1)        # late score corrections (Adam, 2026-09-28: 1 hour is enough)


def _dt(iso: str) -> datetime:
    return datetime.fromisoformat(iso)


@workflow.defn
class GameWorkflow:
    """One per game (ID <league>-<espn_id>): saves the line, runs the preview step, waits for kickoff,
    watches for the final, grades, and regrades once an hour later."""

    @workflow.init
    def __init__(self, inp: GameInput) -> None:
        # Set from the input before any signal is handled (signal-with-start delivers one at once).
        self._times = Times(inp.start_iso, inp.preview_iso)
        self._version = 0                        # bumped by every reschedule that changes the times
        self._last_save: datetime | None = None
        self._last_call_for: str | None = None   # kickoff the T-30 line save was taken for
        self._preview_for: str | None = None     # preview time the preview step ran for

    @workflow.signal
    def reschedule(self, times: Times) -> None:
        """New kickoff (and preview) time. Identical times, which ScheduleSync sends daily, change nothing."""
        start = times.start_iso
        preview = times.preview_iso
        try:
            if preview is None:   # runbook signal with only a kickoff: keep the same lead time
                preview = (_dt(start) - (_dt(self._times.start_iso) - _dt(self._times.preview_iso))).isoformat()
            changed = _dt(start) != _dt(self._times.start_iso) or _dt(preview) != _dt(self._times.preview_iso)
            if _dt(start).tzinfo is None or _dt(preview).tzinfo is None:
                raise ValueError("time without a UTC offset")
        except (TypeError, ValueError) as exc:
            # A mistyped runbook signal must not wedge the workflow: ignore it and say so.
            workflow.logger.error("reschedule ignored, bad times %r: %s", times, exc)
            return
        if changed:
            workflow.logger.info("reschedule: kickoff %s -> %s", self._times.start_iso, start)
            self._times = Times(start, preview)
            self._version += 1

    @workflow.query
    def times(self) -> Times:
        return self._times

    async def _wait_for_change(self, since: int, timeout: timedelta) -> bool:
        """Durable timer that a reschedule signal interrupts. True if the times changed after version `since`
        (read before the activities that preceded this wait, so a signal during one of them isn't missed)."""
        try:
            await workflow.wait_condition(lambda: self._version != since, timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    @workflow.run
    async def run(self, inp: GameInput) -> str:
        self._league, self._espn_id = inp.league, inp.espn_id
        while True:
            await self._before_kickoff()
            outcome = await self._watch()
            if outcome != "replan":
                workflow.logger.info("%s-%s closed: %s", inp.league, inp.espn_id, outcome)
                return outcome

    # ---------- before kickoff: line saves and the preview step ----------

    async def _before_kickoff(self) -> None:
        while True:
            seen = self._version
            now = workflow.now()
            start = _dt(self._times.start_iso)
            if now >= start:
                return
            last_call = start - LAST_LINE_BEFORE
            preview = min(_dt(self._times.preview_iso), start - PREVIEW_LEAD)
            last_call_due = now >= last_call and self._last_call_for != self._times.start_iso
            if self._last_save is None or now - self._last_save >= LINE_EVERY or last_call_due:
                await self._save_line()
                self._last_save = now
                if now >= last_call:
                    self._last_call_for = self._times.start_iso
                continue
            if now >= preview and self._preview_for != self._times.preview_iso:
                self._preview_for = self._times.preview_iso
                try:
                    await workflow.execute_activity(act.generate_preview, args=[self._league, self._espn_id], **NO_RETRY)
                except ActivityError as exc:
                    workflow.logger.warning("preview failed: %s", exc.cause or exc)
                continue
            wake = [self._last_save + LINE_EVERY, start]
            if self._last_call_for != self._times.start_iso:
                wake.append(last_call)
            if self._preview_for != self._times.preview_iso:
                wake.append(preview)
            nxt = min(t for t in wake if t > now)
            await self._wait_for_change(seen, nxt - now)

    async def _save_line(self) -> None:
        try:
            await workflow.execute_activity(act.save_line, args=[self._league, self._espn_id], **ESPN_POLICY)
        except ActivityError as exc:
            # ESPN down for 6 hours: skip this save rather than fail the game; the next one may work.
            workflow.logger.warning("save_line gave up: %s", exc.cause or exc)

    # ---------- after kickoff: watch for the final ----------

    async def _watch(self) -> str:
        kickoff = workflow.now()
        while True:
            seen = self._version
            st: GameState | None = None
            try:
                st = await workflow.execute_activity(act.fetch_game_state, args=[self._league, self._espn_id],
                                                     result_type=GameState, **ESPN_POLICY)
            except ActivityError as exc:
                workflow.logger.warning("fetch_game_state gave up: %s", exc.cause or exc)
            now = workflow.now()
            if st is not None:
                if st.postponed:
                    return await self._postponed(seen)
                if st.state == "post":
                    return await self._final() if st.completed else "not played (canceled)"
                if st.state == "pre" and _dt(st.start_iso) > now:
                    # ESPN moved the kickoff later (a delay, or a flexed game got its time): go back to waiting.
                    self._set_start(st.start_iso)
                    return "replan"
            elapsed = now - kickoff
            if elapsed >= WATCH_GIVE_UP:
                workflow.logger.error("%s-%s never went final in 7 days", self._league, self._espn_id)
                return "unresolved: never went final"
            await self._wait_for_change(seen, WATCH_EVERY if elapsed < WATCH_SLOW_AFTER else WATCH_SLOW_EVERY)
            # Kickoff moved later by a signal and the game hasn't started: go back to waiting. (Before any
            # change, the kickoff is at or before the watch start, so this never fires by itself.)
            if (st is None or st.state == "pre") and _dt(self._times.start_iso) > workflow.now():
                return "replan"

    def _set_start(self, start_iso: str) -> None:
        lead = _dt(self._times.start_iso) - _dt(self._times.preview_iso)
        self._times = Times(start_iso, (_dt(start_iso) - lead).isoformat())

    async def _postponed(self, seen: int) -> str:
        workflow.logger.info("%s-%s postponed: waiting for a new date", self._league, self._espn_id)
        if await self._wait_for_change(seen, POSTPONED_WAIT):
            return "replan"
        return "unresolved: postponed, no new date in 14 days"

    async def _final(self) -> str:
        graded = await self._grade()
        if workflow.patched("ai-recap"):
            await self._start_recap()
        await workflow.sleep(REGRADE_AFTER)
        regraded = await self._grade()
        if regraded != graded:
            if workflow.patched("ai-recap"):
                # A stat correction changed the score: the recap's basis changed, so it is rewritten once.
                await self._start_recap()
            return f"graded {graded}, regraded {regraded} after a stat correction"
        return f"graded {graded}"

    async def _start_recap(self) -> None:
        """Start the recap's WriteTextWorkflow, only for a game written ahead (the pre-write list, an AI league):
        every other recap is written when its page is opened, so no workflow is started for it."""
        try:
            due = await workflow.execute_activity(act.recap_due, args=[self._league, self._espn_id],
                                                  result_type=bool, **RECAP_DUE_POLICY)
        except ActivityError as exc:
            workflow.logger.warning("recap_due gave up: %s", exc.cause or exc)
            return
        if due:
            await start_text(TextJob("recap", self._league, self._espn_id, "final"))

    async def _grade(self) -> list[int]:
        try:
            await workflow.execute_activity(act.fetch_summary, args=[self._league, self._espn_id], **ESPN_POLICY)
        except ActivityError as exc:
            # Grading still works from the stored scoreboard score and saved lines.
            workflow.logger.warning("fetch_summary gave up: %s", exc.cause or exc)
        # No catch: after 5 failed attempts the workflow fails and shows in the UI.
        return await workflow.execute_activity(act.grade_game, args=[self._league, self._espn_id],
                                               result_type=list[int], **GRADE_POLICY)


@workflow.defn
class ScheduleSyncWorkflow:
    """Daily: save two weeks of schedule, then make sure every game not yet final has its GameWorkflow."""

    @workflow.run
    async def run(self, leagues: list[str]) -> int:
        ensured = 0
        for league in leagues:
            refs = await workflow.execute_activity(act.sync_schedule, league, result_type=list[GameRef],
                                                   **ESPN_POLICY)
            ensured += await workflow.execute_activity(
                "ensure_game_workflows", refs, result_type=int,
                start_to_close_timeout=timedelta(minutes=2),
                retry_policy=RetryPolicy(initial_interval=timedelta(seconds=10), maximum_attempts=10))
        return ensured


@workflow.defn
class HeadlinesWorkflow:
    """Twice a day: store new ESPN news (no AI; Phase 4 writes the feed from it)."""

    @workflow.run
    async def run(self, leagues: list[str]) -> int:
        added = 0
        for league in leagues:
            added += await workflow.execute_activity(act.fetch_news, league, result_type=int, **ESPN_POLICY)
        if workflow.patched("ai-headlines"):
            await start_text(TextJob("headlines", ",".join(leagues), None, "schedule"),
                             f"ai-headlines-{workflow.now():%Y%m%d-%H%M}")   # replays identically; one per run
        return added


# ---------- Phase 4: AI text ----------

def text_workflow_id(job: TextJob) -> str:
    return f"ai-{job.kind}-{job.league}-{job.espn_id}"


async def start_text(job: TextJob, workflow_id: str | None = None) -> bool:
    """Start WriteTextWorkflow for one text and don't wait for it (it can wait hours for quota). The parent may
    finish first: the child keeps going. False when that text is already being written (same workflow ID)."""
    try:
        await workflow.start_child_workflow(
            WriteTextWorkflow.run, job, id=workflow_id or text_workflow_id(job), task_queue=TASK_QUEUE,
            parent_close_policy=ParentClosePolicy.ABANDON,
            id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE)
        return True
    except WorkflowAlreadyStartedError:
        return False


@workflow.defn
class WriteTextWorkflow:
    """One AI text, written durably: the write_text activity on the `ai` queue, retried for up to 12 hours when
    every model is rate-limited. Its ID (ai-<kind>-<league>-<espn_id>) makes a second start a no-op."""

    @workflow.run
    async def run(self, job: TextJob) -> str:
        try:
            return await workflow.execute_activity(act.write_text, job, task_queue=AI_QUEUE, result_type=str,
                                                   **TEXT_POLICY)
        except ActivityError as exc:
            # Out of tries: the row stays failed, the page shows template text and the nightly job tries again.
            workflow.logger.warning("%s gave up: %s", text_workflow_id(job), exc.cause or exc)
            return f"gave up: {exc.cause or exc}"


async def _start_all(leagues: list[str], what: str, ahead: timedelta, reason: str) -> int:
    started = 0
    for league in leagues:
        ids = await workflow.execute_activity(act.texts_to_write, args=[league, what, int(ahead.total_seconds() // 3600)],
                                              result_type=list[str], **ESPN_POLICY)
        kind = "preview" if what == "previews" else "recap"
        for espn_id in ids:
            started += await start_text(TextJob(kind, league, espn_id, reason))
    return started


@workflow.defn
class PreviewBatchWorkflow:
    """Midweek (Adam, 2026-09-29): write the coming weekend's previews ahead. It only starts one WriteTextWorkflow
    per game; the `ai` queue (one text at a time, one writer) is what spaces them over the writer's minute."""

    @workflow.run
    async def run(self, leagues: list[str]) -> int:
        return await _start_all(leagues, "previews", BATCH_AHEAD, "midweek")


@workflow.defn
class LeftoverWorkflow:
    """Nightly: anything still missing or failed (previews for the next 3 days, recaps for the last 2)."""

    @workflow.run
    async def run(self, leagues: list[str]) -> int:
        return (await _start_all(leagues, "previews", LEFTOVER_PREVIEWS, "nightly")
                + await _start_all(leagues, "recaps", LEFTOVER_RECAPS, "nightly"))


WORKFLOWS = [GameWorkflow, ScheduleSyncWorkflow, HeadlinesWorkflow, WriteTextWorkflow, PreviewBatchWorkflow,
             LeftoverWorkflow]
