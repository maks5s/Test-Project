"""Standalone worker process.

Run with ``python -m app.worker``. The worker shares an image with the API but
never serves HTTP: it claims durable jobs from PostgreSQL, generates drafts, and
later delivers approved replies. Multiple worker containers can run at once
because claiming uses ``FOR UPDATE SKIP LOCKED`` and the rate limiter lives in
the database.

SIGTERM/SIGINT stop new claims and let in-flight jobs finish, so a container
restart does not abandon a half-finished LLM call. If a process is killed hard,
the lease reaper returns its jobs to the queue.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import socket
import time
import uuid
from types import FrameType

import httpx

from app import jobs, status
from app.config import Settings, get_settings
from app.db import SessionFactory, get_engine, get_sessionmaker
from app.models import Job
from app.services.delivery import process_send_job
from app.services.generation import process_generation_job

logger = logging.getLogger(__name__)


class Worker:
    """Async worker with bounded concurrency and graceful shutdown."""

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        session_factory: SessionFactory | None = None,
        http_client: httpx.AsyncClient | None = None,
        worker_id: str | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.session_factory = session_factory or get_sessionmaker()
        self.worker_id = worker_id or _default_worker_id()
        self._owns_http_client = http_client is None
        self.http_client = http_client or httpx.AsyncClient(
            timeout=self.settings.llm_timeout_seconds,
            follow_redirects=False,
        )
        self._stop = asyncio.Event()
        self._active: set[asyncio.Task[None]] = set()
        self._last_reap = 0.0

    def request_stop(self) -> None:
        self._stop.set()

    async def run_once(self, *, limit: int | None = None) -> int:
        """Claim and process one batch; used by tests and by ``run``."""
        await self._reap_if_due()
        claimed = await self._claim(limit or self.settings.worker_concurrency)
        if not claimed:
            return 0
        await asyncio.gather(*(self._run_job(job) for job in claimed))
        return len(claimed)

    async def run(self) -> None:
        logger.info(
            "worker %s started: concurrency=%s poll=%ss lease=%ss",
            self.worker_id,
            self.settings.worker_concurrency,
            self.settings.worker_poll_interval_seconds,
            self.settings.job_lease_seconds,
        )
        while not self._stop.is_set():
            await self._reap_if_due()

            capacity = self.settings.worker_concurrency - len(self._active)
            claimed: list[Job] = []
            if capacity > 0:
                claimed = await self._claim(capacity)

            for job in claimed:
                task = asyncio.create_task(self._run_job(job), name=f"job-{job.id}")
                self._active.add(task)
                task.add_done_callback(self._active.discard)

            if not claimed:
                try:
                    await asyncio.wait_for(
                        self._stop.wait(),
                        timeout=self.settings.worker_poll_interval_seconds,
                    )
                except TimeoutError:
                    pass

        if self._active:
            logger.info("waiting for %s in-flight job(s)", len(self._active))
            await asyncio.gather(*tuple(self._active), return_exceptions=True)
        logger.info("worker %s stopped", self.worker_id)

    async def aclose(self) -> None:
        if self._owns_http_client:
            await self.http_client.aclose()

    async def _claim(self, limit: int) -> list[Job]:
        async with self.session_factory() as session:
            claimed = await jobs.claim_jobs(
                session,
                worker_id=self.worker_id,
                limit=limit,
                settings=self.settings,
            )
        if claimed:
            logger.info("claimed %s job(s)", len(claimed))
        return claimed

    async def _reap_if_due(self) -> None:
        now = time.monotonic()
        if now - self._last_reap < self.settings.job_reaper_interval_seconds:
            return
        self._last_reap = now
        try:
            async with self.session_factory() as session:
                reaped = await jobs.reap_expired_jobs(session, settings=self.settings)
            if reaped:
                logger.warning("reaped %s expired job(s)", reaped)
        except Exception:
            logger.exception("lease reaper failed")

    async def _run_job(self, job: Job) -> None:
        try:
            if job.type == status.JOB_GENERATE_DRAFT:
                await process_generation_job(
                    job,
                    session_factory=self.session_factory,
                    settings=self.settings,
                    worker_id=self.worker_id,
                    http_client=self.http_client,
                )
            elif job.type == status.JOB_SEND_REPLY:
                await process_send_job(
                    job,
                    session_factory=self.session_factory,
                    settings=self.settings,
                    worker_id=self.worker_id,
                    http_client=self.http_client,
                )
            else:
                raise RuntimeError(f"unsupported job type: {job.type}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("job %s crashed", job.id)
            try:
                async with self.session_factory() as session:
                    retrying = await jobs.fail_job(
                        session,
                        job.id,
                        worker_id=self.worker_id,
                        error=f"{type(exc).__name__}: {exc}",
                        settings=self.settings,
                    )
                if not retrying:
                    logger.error("job %s moved to failed", job.id)
            except Exception:
                logger.exception("could not record crash for job %s", job.id)


def _default_worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


async def _run() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    settings = get_settings()
    worker = Worker(settings=settings)
    loop = asyncio.get_running_loop()

    def stop(signum: int, _frame: FrameType | None = None) -> None:
        logger.info("received signal %s; finishing in-flight jobs", signum)
        loop.call_soon_threadsafe(worker.request_stop)

    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, stop, signum)
        except NotImplementedError:  # Windows
            signal.signal(signum, stop)

    try:
        await worker.run()
    finally:
        await worker.aclose()
        await get_engine().dispose()


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
