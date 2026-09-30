"""The Temporal worker: every workflow and activity on task queue `scores`, and the AI text activity on `ai`
(at most 3 at once, one per free Groq model, so AI work never delays ESPN work).

    python -m app.temporal.worker
"""
from __future__ import annotations

import asyncio
import logging
import os
from concurrent.futures import ThreadPoolExecutor

from temporalio.api.workflowservice.v1 import DescribeNamespaceRequest
from temporalio.client import Client
from temporalio.worker import Worker

from .. import espn
from ..ai import quota
from . import activities
from .starter import Starter, start_text
from .workflows import AI_QUEUE, TASK_QUEUE, WORKFLOWS

TEMPORAL_ADDRESS = os.environ.get("TEMPORAL_ADDRESS", "localhost:7233")
TEMPORAL_NAMESPACE = os.environ.get("TEMPORAL_NAMESPACE", "default")
AI_AT_ONCE = int(os.environ.get("AI_AT_ONCE", "3"))
log = logging.getLogger("worker")


async def connect() -> Client:
    """Connect, waiting for the server and its namespace (both may still be starting after a reboot)."""
    for attempt in range(1, 61):
        try:
            client = await Client.connect(TEMPORAL_ADDRESS, namespace=TEMPORAL_NAMESPACE)
            await client.workflow_service.describe_namespace(DescribeNamespaceRequest(namespace=TEMPORAL_NAMESPACE))
            return client
        except Exception as exc:  # noqa: BLE001
            log.warning("Temporal at %s not reachable (attempt %d): %s", TEMPORAL_ADDRESS, attempt, exc)
            await asyncio.sleep(5)
    raise SystemExit(f"Temporal at {TEMPORAL_ADDRESS} never came up")


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # Nobody waits on a phone here, and Temporal does the retrying: don't skip calls after one failure.
    espn.disable_down_switch()
    client = await connect()
    if os.environ.get("AI_QUOTA") == "db":
        quota.install()               # share the free-tier quota with the api through Postgres
    loop = asyncio.get_running_loop()
    # The 8 AM preview step runs in a thread; it starts the refresh workflow through this loop's client.
    activities.start_text = lambda job: asyncio.run_coroutine_threadsafe(start_text(client, job), loop).result(30)
    # Threads for the synchronous activities; the database pool has 8 connections (ESPN work 5, AI text 3).
    with (ThreadPoolExecutor(max_workers=5, thread_name_prefix="activity") as pool,
          ThreadPoolExecutor(max_workers=AI_AT_ONCE, thread_name_prefix="ai") as ai_pool):
        worker = Worker(client, task_queue=TASK_QUEUE, workflows=WORKFLOWS,
                        activities=[*activities.ALL, Starter(client).ensure_game_workflows],
                        activity_executor=pool, max_concurrent_activities=5)
        ai_worker = Worker(client, task_queue=AI_QUEUE, activities=activities.AI,
                           activity_executor=ai_pool, max_concurrent_activities=AI_AT_ONCE)
        log.info("worker polling task queues %s and %s at %s", TASK_QUEUE, AI_QUEUE, TEMPORAL_ADDRESS)
        await asyncio.gather(worker.run(), ai_worker.run())


if __name__ == "__main__":
    asyncio.run(main())
