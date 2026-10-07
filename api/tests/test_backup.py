"""The nightly backup activity (Temporal, replacing scripts/backup.sh in cron): the dump is checked before it is uploaded."""
import gzip
import io
import random

import pytest
from temporalio.exceptions import ApplicationError

from app.temporal import activities, schedules

HEAD, TRAILER = b"-- PostgreSQL database cluster dump\n", b"\n-- PostgreSQL database cluster dump complete\n"
DUMP = HEAD + random.Random(1).randbytes(200_000) + TRAILER      # random bytes: it does not compress away


class FakeDump:
    def __init__(self, body: bytes, code: int = 0):
        self.stdout, self.code, self.returncode = io.BytesIO(body), code, code

    def wait(self):
        return self.code


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("BACKUP_URL", "https://objects.example/p/abc/o/")
    monkeypatch.setenv("DATABASE_URL", "postgresql://scores:p%40ss@db:5432/scores")
    uploads, popen = [], []

    class Resp:
        def raise_for_status(self):
            pass

    monkeypatch.setattr(activities.httpx, "put", lambda url, content, timeout: (uploads.append((url, content)), Resp())[1])

    def fake_popen(cmd, stdout, stderr, env):
        popen.append((cmd, env))
        return FakeDump(DUMP)
    monkeypatch.setattr(activities.subprocess, "Popen", fake_popen)
    return uploads, popen


def test_a_good_dump_is_gzipped_and_uploaded(env):
    uploads, popen = env
    out = activities.backup_database()
    (url, body), = uploads
    assert url.startswith("https://objects.example/p/abc/o/scores-") and url.endswith("Z.sql.gz")
    assert gzip.decompress(body) == DUMP                                   # what was dumped is what was uploaded
    assert out.startswith(url.rsplit("/", 1)[1]) and "bytes" in out
    cmd, penv = popen[0]
    assert cmd[:2] == ["pg_dumpall", "-h"] and cmd[2] == "db" and "scores" in cmd
    assert penv["PGPASSWORD"] == "p@ss"                                    # the url-encoded password, decoded


def test_a_failed_dump_is_not_uploaded(env, monkeypatch):
    uploads, _ = env
    monkeypatch.setattr(activities.subprocess, "Popen", lambda *a, **k: FakeDump(DUMP, code=1))
    with pytest.raises(ApplicationError, match="pg_dumpall exited 1"):
        activities.backup_database()
    assert uploads == []


@pytest.mark.parametrize("body", [b"tiny", DUMP.replace(b"database cluster dump complete", b"cut off here")], ids=["tiny", "cut off"])
def test_a_tiny_or_cut_off_dump_is_not_uploaded(env, monkeypatch, body):
    uploads, _ = env
    monkeypatch.setattr(activities.subprocess, "Popen", lambda *a, **k: FakeDump(body))
    with pytest.raises(ApplicationError, match="incomplete"):
        activities.backup_database()
    assert uploads == []


def test_no_backup_url_is_a_failure_that_is_not_retried(env, monkeypatch):
    monkeypatch.setenv("BACKUP_URL", "")
    with pytest.raises(ApplicationError) as exc:
        activities.backup_database()
    assert exc.value.non_retryable


def test_the_backup_runs_nightly_at_3_30_am_pacific():
    wf, args, hours, minute, days = schedules._schedules()["backup"]
    assert (wf.__name__, args, hours, minute, days) == ("BackupWorkflow", [], [3], 30, None)
    assert activities.backup_database in activities.ALL
