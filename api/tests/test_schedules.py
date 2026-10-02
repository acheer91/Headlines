"""Schedules: ensure() creates what is listed and deletes only stale `ai-*` schedules. A fake client (no server);
the real create/update/list/delete path is rehearsed against a local dev server before a deploy."""
from types import SimpleNamespace

import pytest
from temporalio.service import RPCError, RPCStatusCode

from app.ai import scope
from app.temporal import schedules

pytestmark = pytest.mark.asyncio


class FakeClient:
    def __init__(self, existing=(), gone=()):
        self.existing = list(existing)       # ids already on the server
        self.gone = set(gone)                # listed, but already deleted (the listing lags)
        self.created, self.deleted = [], []

    async def create_schedule(self, sid, schedule):
        self.created.append(sid)

    async def list_schedules(self):
        async def it():
            for sid in self.existing:
                yield SimpleNamespace(id=sid)
        return it()

    def get_schedule_handle(self, sid):
        client = self

        class Handle:
            async def delete(self):
                if sid in client.gone:
                    raise RPCError("not found", RPCStatusCode.NOT_FOUND, b"")
                client.deleted.append(sid)
        return Handle()


@pytest.fixture()
def nfl_only(monkeypatch):
    monkeypatch.setattr(schedules, "LEAGUES", ["nfl", "ncaaf"])
    monkeypatch.setattr(scope, "AI_LEAGUES", {"nfl"})
    monkeypatch.setattr(schedules, "SCHEDULES", schedules._schedules())


async def test_stale_lists_only_ai_schedules_no_longer_wanted(nfl_only):
    client = FakeClient(["schedule-sync", "headlines", "ai-leftover", "ai-previews-nfl", "ai-previews-ncaaf",
                         "someone-elses-schedule"])
    assert await schedules.stale(client) == ["ai-previews-ncaaf"]


async def test_ensure_deletes_a_schedule_whose_league_left_ai_leagues(nfl_only, capsys):
    client = FakeClient(["ai-previews-ncaaf", "ai-previews-nfl", "unrelated"])
    await schedules.ensure(client)
    assert client.deleted == ["ai-previews-ncaaf"]                      # not ai-previews-nfl, not "unrelated"
    assert set(client.created) == set(schedules.SCHEDULES)
    assert "deleted ai-previews-ncaaf" in capsys.readouterr().out


async def test_ensure_turning_ai_off_removes_every_ai_schedule(monkeypatch):
    monkeypatch.setattr(schedules, "LEAGUES", ["nfl", "ncaaf"])
    monkeypatch.setattr(scope, "AI_LEAGUES", set())
    monkeypatch.setattr(schedules, "SCHEDULES", schedules._schedules())
    client = FakeClient(["schedule-sync", "headlines", "ai-leftover", "ai-previews-nfl"])
    await schedules.ensure(client)
    assert sorted(client.deleted) == ["ai-leftover", "ai-previews-nfl"]


async def test_a_schedule_already_gone_is_not_an_error(nfl_only):
    client = FakeClient(["ai-previews-ncaaf"], gone=["ai-previews-ncaaf"])
    await schedules.ensure(client)                                       # the listing lagged: no exception
    assert client.deleted == []


async def test_show_names_a_schedule_the_server_does_not_have_yet(nfl_only):
    class NoSchedules:
        def get_schedule_handle(self, sid):
            class Handle:
                async def describe(self):
                    raise RPCError("not found", RPCStatusCode.NOT_FOUND, b"")
            return Handle()
    out = await schedules.show(NoSchedules())
    assert out == [{"id": sid, "missing": True} for sid in schedules.SCHEDULES]     # the first deploy: no crash


async def test_show_still_raises_on_any_other_error(nfl_only):
    class Down:
        def get_schedule_handle(self, sid):
            class Handle:
                async def describe(self):
                    raise RPCError("unavailable", RPCStatusCode.UNAVAILABLE, b"")
            return Handle()
    with pytest.raises(RPCError):
        await schedules.show(Down())


async def test_nothing_is_deleted_when_nothing_is_stale(nfl_only):
    client = FakeClient(list(schedules.SCHEDULES))
    await schedules.ensure(client)
    assert client.deleted == []
