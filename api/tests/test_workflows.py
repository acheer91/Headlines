"""GameWorkflow, ScheduleSync and Headlines against Temporal's time-skipping test server (steps 3.5, 3.6):
days of timers pass in seconds. Activities are mocks with the real names, so no ESPN, database or Docker.
The test server is downloaded on first use and needs an x86 machine (not the ARM server).

SAVE_HISTORIES=1 writes each GameWorkflow history to tests/fixtures/histories/ for the replay test."""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import pytest_asyncio
from temporalio import activity
from temporalio.client import WorkflowFailureError
from temporalio.service import RPCError
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from app.temporal.models import GameInput, GameRef, GameState, TextJob, Times
from app.temporal.starter import Starter
from app.temporal.workflows import (AI_QUEUE, TASK_QUEUE, WORKFLOWS, GameWorkflow, HeadlinesWorkflow, LeftoverWorkflow,
                                    PreviewBatchWorkflow, ScheduleSyncWorkflow, WriteTextWorkflow)

HISTORIES = Path(__file__).parent / "fixtures" / "histories"
pytestmark = pytest.mark.asyncio(loop_scope="module")


def close(a: datetime, b: datetime) -> bool:
    """Activity times are when the task was scheduled: the timer's fire time plus a few milliseconds."""
    return abs(a - b) < timedelta(seconds=2)


class Fake:
    """Scripted activity results, and a log of when (in workflow time) each activity ran."""

    def __init__(self, states=None, grades=None, grade_failures=0, sync=None):
        self.calls: list[tuple[str, datetime]] = []
        self.states = list(states or [])       # fetch_game_state results in order; the last one repeats
        self.grades = list(grades or [[24, 17]])
        self.grade_failures = grade_failures
        self.sync = sync or []
        self.news = 0

    def _log(self, name):
        self.calls.append((name, activity.info().current_attempt_scheduled_time))

    def times(self, name):
        return [t for n, t in self.calls if n == name]

    def activities(self):
        @activity.defn(name="save_line")
        async def save_line(league: str, espn_id: str) -> None:
            self._log("save_line")

        @activity.defn(name="generate_preview")
        async def generate_preview(league: str, espn_id: str) -> None:
            self._log("generate_preview")

        @activity.defn(name="fetch_game_state")
        async def fetch_game_state(league: str, espn_id: str) -> GameState:
            self._log("fetch_game_state")
            return self.states.pop(0) if len(self.states) > 1 else self.states[0]

        @activity.defn(name="fetch_summary")
        async def fetch_summary(league: str, espn_id: str) -> None:
            self._log("fetch_summary")

        @activity.defn(name="grade_game")
        async def grade_game(league: str, espn_id: str) -> list[int]:
            self._log("grade_game")
            if self.grade_failures:
                self.grade_failures -= 1
                raise ApplicationError("database hiccup")
            return self.grades.pop(0) if len(self.grades) > 1 else self.grades[0]

        @activity.defn(name="sync_schedule")
        async def sync_schedule(league: str) -> list[GameRef]:
            self._log("sync_schedule")
            return self.sync

        @activity.defn(name="fetch_news")
        async def fetch_news(league: str) -> int:
            self._log("fetch_news")
            self.news += 3
            return 3

        return [save_line, generate_preview, fetch_game_state, fetch_summary, grade_game, sync_schedule, fetch_news]


def st(state, start, hs=None, as_=None, postponed=False, completed=None):
    return GameState(state, hs, as_, start.isoformat(), postponed,
                     state == "post" and not postponed if completed is None else completed)


@pytest_asyncio.fixture(loop_scope="module")
async def env():
    """A test server per test (~1 s each). Shared by the module, a game's AI children (Phase 4) left the next
    test's clock stuck after the long full-game test (2026-09-29), as ScheduleSync's tests once did."""
    e = await WorkflowEnvironment.start_time_skipping()
    yield e
    await e.shutdown()


@pytest_asyncio.fixture(loop_scope="module")
async def fresh_env():
    """Its own test server: the ScheduleSync tests start and terminate many workflows, and sharing one
    server with the game tests occasionally left time-skipping stuck (seen 1 run in 3 on Windows)."""
    e = await WorkflowEnvironment.start_time_skipping()
    yield e
    await e.shutdown()


async def run_game(env, fake, start, preview=None, during=None, name=None, ai=None):
    """Start a GameWorkflow, optionally run `during(handle)` while it waits, return (result, handle). A final
    starts the recap's WriteTextWorkflow (Phase 4), so an `ai` queue worker always runs: without one, its
    waiting activity stops the time-skipping server from skipping the regrade's hour."""
    preview = preview or start - timedelta(hours=5)
    inp = GameInput("nfl", uuid.uuid4().hex[:8], start.isoformat(), preview.isoformat())
    ai = ai or FakeAI()
    async with (Worker(env.client, task_queue=TASK_QUEUE, workflows=WORKFLOWS, activities=fake.activities()),
                Worker(env.client, task_queue=AI_QUEUE, activities=ai.activities())):
        handle = await env.client.start_workflow(GameWorkflow.run, inp, id=f"nfl-{inp.espn_id}", task_queue=TASK_QUEUE)
        if during:
            await during(handle)
        result = await handle.result()
        # Let the recap the game started (possibly right before it closed) finish while the ai worker is up;
        # a leftover waiting AI task would stop time-skipping for the next test.
        try:
            await env.client.get_workflow_handle(f"ai-recap-nfl-{inp.espn_id}").result()
        except RPCError:
            pass                              # no recap: the game never reached a played final
        if name and os.environ.get("SAVE_HISTORIES"):
            HISTORIES.mkdir(parents=True, exist_ok=True)
            (HISTORIES / f"{name}.json").write_text((await handle.fetch_history()).to_json())
    return result, handle


async def now(env) -> datetime:
    return (await env.get_current_time()).astimezone(timezone.utc)


# ---------- 3.5: a full game ----------

async def test_full_game_lifecycle(env):
    t0 = await now(env)
    start = t0 + timedelta(days=3)
    fake = Fake(states=[st("in", start, 7, 3), st("in", start, 14, 10), st("post", start, 24, 17)],
                grades=[[24, 17], [24, 20]])
    result, _ = await run_game(env, fake, start, name="full_game")
    saves = fake.times("save_line")
    # First sight, then daily (T-2d, T-1d), then 30 minutes before kickoff.
    assert len(saves) == 4
    assert close(saves[-1], start - timedelta(minutes=30))
    assert all(b - a >= timedelta(minutes=30) for a, b in zip(saves, saves[1:]))
    # Preview once, on game morning, before kickoff.
    [preview] = fake.times("generate_preview")
    assert close(preview, start - timedelta(hours=5))
    # Status polls from kickoff, every 2.5 minutes.
    polls = fake.times("fetch_game_state")
    assert close(polls[0], start) and close(polls[1], start + timedelta(minutes=2.5)) and len(polls) == 3
    # Graded on final, then regraded an hour later after the score changed.
    grades = fake.times("grade_game")
    assert len(grades) == 2 and close(grades[1], grades[0] + timedelta(hours=1))
    assert len(fake.times("fetch_summary")) == 2
    assert result == "graded [24, 17], regraded [24, 20] after a stat correction"


async def test_no_stat_correction_closes_after_one_regrade(env):
    start = await now(env) + timedelta(hours=2)
    fake = Fake(states=[st("post", start, 21, 20)], grades=[[21, 20]])
    result, _ = await run_game(env, fake, start, name="short_notice_game")
    assert result == "graded [21, 20]"
    assert len(fake.times("grade_game")) == 2
    # Started 2 hours out: saved at first sight and again 30 minutes before kickoff.
    assert len(fake.times("save_line")) == 2
    # The 8 AM preview had already passed: it runs at once, before kickoff.
    assert len(fake.times("generate_preview")) == 1


# ---------- 3.6: edge cases ----------

async def test_kickoff_moved_by_signal(env):
    t0 = await now(env)
    start = t0 + timedelta(days=2)
    new_start = start + timedelta(days=1, hours=3)
    fake = Fake(states=[st("post", new_start, 30, 27)])

    async def move(handle):
        await env.sleep(timedelta(hours=6))
        await handle.signal(GameWorkflow.reschedule, Times(new_start.isoformat(), (new_start - timedelta(hours=5)).isoformat()))
        assert (await handle.query(GameWorkflow.times)).start_iso == new_start.isoformat()

    result, _ = await run_game(env, fake, start, during=move, name="kickoff_moved")
    assert result.startswith("graded")
    assert close(fake.times("fetch_game_state")[0], new_start)     # watched from the new kickoff, not the old
    assert close(fake.times("save_line")[-1], new_start - timedelta(minutes=30))
    [preview] = fake.times("generate_preview")
    assert close(preview, new_start - timedelta(hours=5))


async def test_same_times_signal_changes_nothing(env):
    """ScheduleSync signals every running game daily with the same times: no extra line saves."""
    start = await now(env) + timedelta(days=1, hours=12)
    fake = Fake(states=[st("post", start, 10, 3)])
    base = Fake()

    async def ping(handle):
        for _ in range(3):
            await env.sleep(timedelta(hours=4))
            await handle.signal(GameWorkflow.reschedule, Times(start.isoformat(), (start - timedelta(hours=5)).isoformat()))

    await run_game(env, fake, start, during=ping)
    await run_game(env, base, await now(env) + timedelta(days=1, hours=12), during=None)
    assert len(fake.times("save_line")) == len(base.times("save_line"))


async def test_bad_runbook_signal_is_ignored(env):
    start = await now(env) + timedelta(hours=3)
    fake = Fake(states=[st("post", start, 14, 7)])

    async def typo(handle):
        await env.sleep(timedelta(minutes=10))
        await handle.signal(GameWorkflow.reschedule, Times("next sunday", None))
        await handle.signal(GameWorkflow.reschedule, Times("2026-10-04T17:00:00", None))    # no UTC offset

    result, _ = await run_game(env, fake, start, during=typo)
    assert result.startswith("graded")
    assert close(fake.times("fetch_game_state")[0], start)


async def test_postponed_then_rescheduled(env):
    start = await now(env) + timedelta(hours=1)
    new_start = start + timedelta(days=3)
    fake = Fake(states=[st("post", start, 0, 0, postponed=True), st("post", new_start, 17, 13)])

    async def reschedule(handle):
        await env.sleep(timedelta(days=2))
        await handle.signal(GameWorkflow.reschedule, Times(new_start.isoformat(), None))

    result, _ = await run_game(env, fake, start, during=reschedule, name="postponed_rescheduled")
    assert result.startswith("graded")
    polls = fake.times("fetch_game_state")
    assert close(polls[0], start) and close(polls[1], new_start)
    # Runbook-style signal without a preview time keeps the same lead: 5 hours before the new kickoff.
    assert close(fake.times("generate_preview")[-1], new_start - timedelta(hours=5))


async def test_postponed_without_new_date_closes_unresolved(env):
    start = await now(env) + timedelta(hours=1)
    fake = Fake(states=[st("post", start, 0, 0, postponed=True)])
    result, _ = await run_game(env, fake, start)
    assert result == "unresolved: postponed, no new date in 14 days"
    assert not fake.times("grade_game")


async def test_canceled_game_is_not_graded(env):
    start = await now(env) + timedelta(hours=1)
    fake = Fake(states=[st("post", start, 0, 0, completed=False)])
    result, _ = await run_game(env, fake, start)
    assert result == "not played (canceled)" and not fake.times("grade_game")


async def test_game_never_going_final(env):
    start = await now(env) + timedelta(hours=1)
    fake = Fake(states=[st("in", start, 3, 0)])
    result, _ = await run_game(env, fake, start)
    assert result == "unresolved: never went final"
    polls = fake.times("fetch_game_state")
    # Each poll adds a few real milliseconds, so allow a minute of drift over the week.
    assert abs(polls[-1] - polls[0] - timedelta(days=7)) < timedelta(minutes=1)
    # 2.5-minute polls for the first 8 hours (193 polls), hourly after (160 more).
    assert close(polls[1], polls[0] + timedelta(minutes=2.5))
    assert close(polls[-1], polls[-2] + timedelta(hours=1))
    assert len(polls) == 353


async def test_delayed_kickoff_goes_back_to_waiting(env):
    """ESPN still says pre-game at kickoff with a later start (flexed or delayed): back to waiting."""
    start = await now(env) + timedelta(hours=1)
    later = start + timedelta(hours=4)
    fake = Fake(states=[st("pre", later), st("post", later, 20, 10)])
    result, _ = await run_game(env, fake, start)
    assert result.startswith("graded")
    polls = fake.times("fetch_game_state")
    assert len(polls) == 2 and close(polls[0], start) and close(polls[1], later)
    assert close(fake.times("save_line")[-1], later - timedelta(minutes=30))


async def test_grading_fails_then_recovers(env):
    start = await now(env) + timedelta(hours=1)
    fake = Fake(states=[st("post", start, 27, 24)], grades=[[27, 24]], grade_failures=2)
    result, _ = await run_game(env, fake, start)
    assert result == "graded [27, 24]"
    assert len(fake.times("grade_game")) == 4          # 2 failures + success, then the regrade


async def test_grading_failing_five_times_fails_the_workflow(env):
    start = await now(env) + timedelta(hours=1)
    fake = Fake(states=[st("post", start, 27, 24)], grade_failures=5)
    with pytest.raises(WorkflowFailureError):
        await run_game(env, fake, start)
    assert len(fake.times("grade_game")) == 5


# ---------- ScheduleSync and Headlines ----------

async def test_schedule_sync_starts_one_workflow_per_game(fresh_env):
    env = fresh_env
    t0 = await now(env)
    refs = [GameRef("nfl", f"sync{uuid.uuid4().hex[:6]}{i}", (t0 + timedelta(days=4 + i)).isoformat(),
                    (t0 + timedelta(days=4 + i, hours=-5)).isoformat()) for i in range(3)]
    fake = Fake(states=[st("in", t0)], sync=refs)
    worker = Worker(env.client, task_queue=TASK_QUEUE, workflows=WORKFLOWS,
                    activities=[*fake.activities(), Starter(env.client).ensure_game_workflows])
    async with worker:
        n1 = await env.client.execute_workflow(ScheduleSyncWorkflow.run, ["nfl"], id=f"sync-{uuid.uuid4()}",
                                               task_queue=TASK_QUEUE)
        runs1 = [(await env.client.get_workflow_handle(f"nfl-{r.espn_id}").describe()).run_id for r in refs]
        n2 = await env.client.execute_workflow(ScheduleSyncWorkflow.run, ["nfl"], id=f"sync-{uuid.uuid4()}",
                                               task_queue=TASK_QUEUE)
        runs2 = [(await env.client.get_workflow_handle(f"nfl-{r.espn_id}").describe()).run_id for r in refs]
        assert n1 == n2 == 3
        assert runs1 == runs2                                # the rerun started nothing new
        for r in refs:
            d = await env.client.get_workflow_handle(f"nfl-{r.espn_id}").describe()
            assert d.status.name == "RUNNING"
            await env.client.get_workflow_handle(f"nfl-{r.espn_id}").terminate("test cleanup")
        # A terminated workflow may be restarted (allow duplicate failed only) ...
        n3 = await env.client.execute_workflow(ScheduleSyncWorkflow.run, ["nfl"], id=f"sync-{uuid.uuid4()}",
                                               task_queue=TASK_QUEUE)
        assert n3 == 3
        for r in refs:
            await env.client.get_workflow_handle(f"nfl-{r.espn_id}").terminate("test cleanup")


async def test_completed_game_is_never_reopened(fresh_env):
    env = fresh_env
    t0 = await now(env)
    start = t0 + timedelta(hours=1)
    fake = Fake(states=[st("post", start, 7, 6)])
    _, handle = await run_game(env, fake, start)
    espn_id = handle.id.removeprefix("nfl-")
    fake.sync = [GameRef("nfl", espn_id, start.isoformat(), (start - timedelta(hours=5)).isoformat())]
    worker = Worker(env.client, task_queue=TASK_QUEUE, workflows=WORKFLOWS,
                    activities=[*fake.activities(), Starter(env.client).ensure_game_workflows])
    async with worker:
        n = await env.client.execute_workflow(ScheduleSyncWorkflow.run, ["nfl"], id=f"sync-{uuid.uuid4()}",
                                              task_queue=TASK_QUEUE)
    assert n == 0
    assert (await handle.describe()).status.name == "COMPLETED"


async def test_headlines(env):
    fake = Fake()
    worker = Worker(env.client, task_queue=TASK_QUEUE, workflows=WORKFLOWS, activities=fake.activities())
    async with worker, Worker(env.client, task_queue=AI_QUEUE, activities=FakeAI().activities()):
        added = await env.client.execute_workflow(HeadlinesWorkflow.run, ["nfl"], id=f"headlines-{uuid.uuid4()}",
                                                  task_queue=TASK_QUEUE)
    assert added == 3 and len(fake.times("fetch_news")) == 1


# ---------- Phase 4: AI text ----------



class FakeAI:
    """write_text on the `ai` queue, scripted: `limited` rate-limited attempts first (with Groq's wait)."""

    def __init__(self, limited=0, wait=timedelta(hours=2), todo=None):
        self.jobs: list[tuple[TextJob, datetime]] = []
        self.limited = limited
        self.wait = wait
        self.todo = todo or {}

    def activities(self):
        @activity.defn(name="write_text")
        async def write_text(job: TextJob) -> str:
            self.jobs.append((job, activity.info().current_attempt_scheduled_time))
            if self.limited:
                self.limited -= 1
                raise ApplicationError("every model rate-limited", type="RateLimited", next_retry_delay=self.wait)
            return "ready"
        return [write_text]

    def texts_to_write(self):
        @activity.defn(name="texts_to_write")
        async def texts_to_write(league: str, what: str, hours: int) -> list[str]:
            return self.todo.get((league, what), [])
        return texts_to_write


def ai_worker(env, ai):
    return Worker(env.client, task_queue=AI_QUEUE, activities=ai.activities())


async def test_recap_written_at_the_final(env):
    start = await now(env) + timedelta(hours=2)
    fake, ai = Fake(states=[st("post", start, 21, 20)], grades=[[21, 20]]), FakeAI()
    result, handle = await run_game(env, fake, start, ai=ai)
    assert result == "graded [21, 20]"
    assert [(j.kind, j.reason) for j, _ in ai.jobs] == [("recap", "final")]
    assert close(ai.jobs[0][1], fake.times("grade_game")[0])            # right after the first grade


async def test_stat_correction_rewrites_the_recap_once(env):
    start = await now(env) + timedelta(hours=2)
    fake = Fake(states=[st("post", start, 24, 17)], grades=[[24, 17], [24, 20]])
    ai = FakeAI()
    result, handle = await run_game(env, fake, start, ai=ai)
    assert "regraded" in result
    assert len(ai.jobs) == 2 and close(ai.jobs[1][1], fake.times("grade_game")[1])


async def test_rate_limited_text_retries_after_groqs_wait(env):
    ai = FakeAI(limited=1, wait=timedelta(hours=2))
    job = TextJob("recap", "nfl", uuid.uuid4().hex[:8], "final")
    async with ai_worker(env, ai), Worker(env.client, task_queue=TASK_QUEUE, workflows=WORKFLOWS, activities=[]):
        out = await env.client.execute_workflow(WriteTextWorkflow.run, job, id=f"ai-t-{job.espn_id}",
                                                task_queue=TASK_QUEUE)
    # The time-skipping test server ignores next_retry_delay (it retries at once), so only the retry is checked
    # here. On the real server (laptop, Temporal 1.32, 2026-09-29) a 5 s next_retry_delay gave a 5.0 s gap.
    assert out == "ready" and len(ai.jobs) == 2


async def test_text_gives_up_without_failing_the_workflow(env):
    ai = FakeAI(limited=99, wait=timedelta(hours=1))
    job = TextJob("recap", "nfl", uuid.uuid4().hex[:8], "final")
    async with ai_worker(env, ai), Worker(env.client, task_queue=TASK_QUEUE, workflows=WORKFLOWS, activities=[]):
        out = await env.client.execute_workflow(WriteTextWorkflow.run, job, id=f"ai-t-{job.espn_id}",
                                                task_queue=TASK_QUEUE)
    assert out.startswith("gave up") and len(ai.jobs) == 8             # 8 tries, then the nightly job's turn


async def test_preview_batch_starts_one_text_per_game(env):
    ids = [uuid.uuid4().hex[:8] for _ in range(3)]
    ai = FakeAI(todo={("ncaaf", "previews"): ids})
    async with ai_worker(env, ai), Worker(env.client, task_queue=TASK_QUEUE, workflows=WORKFLOWS,
                                          activities=[ai.texts_to_write()]):
        started = await env.client.execute_workflow(PreviewBatchWorkflow.run, ["ncaaf"], id=f"b-{uuid.uuid4()}",
                                                    task_queue=TASK_QUEUE)
        for i in ids:
            await env.client.get_workflow_handle(f"ai-preview-ncaaf-{i}").result()
    assert started == 3 and sorted(j.espn_id for j, _ in ai.jobs) == sorted(ids)
    assert {(j.kind, j.reason) for j, _ in ai.jobs} == {("preview", "midweek")}


async def test_leftover_job_covers_previews_and_recaps(env):
    ai = FakeAI(todo={("nfl", "previews"): ["p1x"], ("nfl", "recaps"): ["r1x"]})
    async with ai_worker(env, ai), Worker(env.client, task_queue=TASK_QUEUE, workflows=WORKFLOWS,
                                          activities=[ai.texts_to_write()]):
        started = await env.client.execute_workflow(LeftoverWorkflow.run, ["nfl"], id=f"l-{uuid.uuid4()}",
                                                    task_queue=TASK_QUEUE)
        await env.client.get_workflow_handle("ai-preview-nfl-p1x").result()
        await env.client.get_workflow_handle("ai-recap-nfl-r1x").result()
    assert started == 2 and {(j.kind, j.reason) for j, _ in ai.jobs} == {("preview", "nightly"), ("recap", "nightly")}


async def test_headlines_run_writes_the_feed(env):
    fake, ai = Fake(), FakeAI()
    wid = f"headlines-{uuid.uuid4()}"
    async with ai_worker(env, ai), Worker(env.client, task_queue=TASK_QUEUE, workflows=WORKFLOWS,
                                          activities=fake.activities()):
        handle = await env.client.start_workflow(HeadlinesWorkflow.run, ["nfl", "ncaaf"], id=wid, task_queue=TASK_QUEUE)
        started = (await handle.describe()).start_time
        await handle.result()
        await env.client.get_workflow_handle(f"ai-headlines-{started:%Y%m%d-%H%M}").result()
        if os.environ.get("SAVE_HISTORIES"):
            history = await env.client.get_workflow_handle(wid).fetch_history()
            (HISTORIES / "headlines_ai.json").write_text(history.to_json())
    [(job, _)] = ai.jobs
    assert (job.kind, job.league, job.espn_id) == ("headlines", "nfl,ncaaf", None)
