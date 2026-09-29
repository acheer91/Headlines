"""The Temporal worker: runs every workflow and activity on task queue `scores`.

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
from . import activities
from .starter import Starter
from .workflows import TASK_QUEUE, WORKFLOWS

TEMPORAL_ADDRESS = os.environ.get("TEMPORAL_ADDRESS", "localhost:7233")
TEMPORAL_NAMESPACE = os.environ.get("TEMPORAL_NAMESPACE", "default")
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
    # Threads for the synchronous activities; the database pool has 8 connections.
    with ThreadPoolExecutor(max_workers=8, thread_name_prefix="activity") as pool:
        worker = Worker(client, task_queue=TASK_QUEUE, workflows=WORKFLOWS,
                        activities=[*activities.ALL, Starter(client).ensure_game_workflows],
                        activity_executor=pool, max_concurrent_activities=8)
        log.info("worker polling task queue %s at %s", TASK_QUEUE, TEMPORAL_ADDRESS)
        await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
