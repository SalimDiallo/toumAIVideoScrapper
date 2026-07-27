"""JobStorePort backed by PostgreSQL (SQLAlchemy Core).

Cross-process job status: the API writes PENDING, the worker moves it to
RUNNING/COMPLETED/FAILED, and GET /jobs/{id} reads it back.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta, timezone

from sqlalchemy import (
    ARRAY,
    Column,
    DateTime,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    func,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert

from ..domain.models import Job, JobStatus

_metadata = MetaData()

jobs = Table(
    "jobs",
    _metadata,
    Column("job_id", String, primary_key=True),
    Column("url", Text, nullable=False),
    Column("languages", ARRAY(String)),
    Column("status", String, nullable=False),
    Column("result_uri", Text),
    Column("error", Text),
    Column("video_id", String, index=True),
    Column("created_at", DateTime(timezone=True)),
    Column("updated_at", DateTime(timezone=True)),
)


class PostgresJobStore:
    def __init__(self, dsn: str) -> None:
        self._engine = create_engine(dsn, future=True)

    def create_schema(self) -> None:
        _metadata.create_all(self._engine)
        # Lightweight, idempotent migration for DBs created before `video_id`.
        with self._engine.begin() as conn:
            conn.execute(text("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS video_id VARCHAR"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_jobs_video_id ON jobs (video_id)"))

    def create(self, job: Job) -> None:
        stmt = pg_insert(jobs).values(
            job_id=job.job_id,
            url=job.url,
            languages=job.languages,
            status=job.status.value,
            result_uri=job.result_uri,
            error=job.error,
            video_id=job.video_id,
            created_at=job.created_at,
            updated_at=job.updated_at,
        )
        # idempotent: re-POSTing the same job_id is a no-op
        stmt = stmt.on_conflict_do_nothing(index_elements=["job_id"])
        with self._engine.begin() as conn:
            conn.execute(stmt)

    def get(self, job_id: str) -> Job | None:
        with self._engine.connect() as conn:
            row = conn.execute(select(jobs).where(jobs.c.job_id == job_id)).mappings().first()
        return self._to_job(row) if row is not None else None

    def list(
        self, *, status: JobStatus | None = None, limit: int = 50, offset: int = 0
    ) -> list[Job]:
        stmt = select(jobs).order_by(jobs.c.created_at.desc()).limit(limit).offset(offset)
        if status is not None:
            stmt = stmt.where(jobs.c.status == status.value)
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).mappings().all()
        return [self._to_job(r) for r in rows]

    def existing_video_ids(self, video_ids: Iterable[str]) -> set[str]:
        """video_ids already tied to a non-failed job (pending/running/completed)."""
        wanted = {v for v in video_ids if v}
        if not wanted:
            return set()
        stmt = (
            select(jobs.c.video_id)
            .where(jobs.c.video_id.in_(wanted), jobs.c.status != JobStatus.FAILED.value)
            .distinct()
        )
        with self._engine.connect() as conn:
            return {row[0] for row in conn.execute(stmt) if row[0]}

    @staticmethod
    def _to_job(row) -> Job:
        return Job(
            job_id=row["job_id"],
            url=row["url"],
            languages=list(row["languages"] or []),
            status=JobStatus(row["status"]),
            result_uri=row["result_uri"],
            error=row["error"],
            video_id=row["video_id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def counts_by_status(self) -> dict[str, int]:
        """Total number of jobs per status, e.g. {"pending": 3, "completed": 12}."""
        stmt = select(jobs.c.status, func.count()).group_by(jobs.c.status)
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).all()
        return {status: count for status, count in rows}

    def timeseries(self, *, days: int = 14) -> list[dict]:
        """Per-day job counts over the last `days`, for the evolution chart.

        Returns rows ordered oldest-first:
        [{"day": "2026-07-20", "total": 8, "completed": 6, "failed": 1}, ...]
        Days with no activity are omitted; the UI backfills the gaps.
        """
        since = datetime.now(timezone.utc) - timedelta(days=days)
        day = func.date_trunc("day", jobs.c.created_at)
        stmt = (
            select(
                day.label("day"),
                func.count().label("total"),
                func.count().filter(jobs.c.status == JobStatus.COMPLETED.value).label("completed"),
                func.count().filter(jobs.c.status == JobStatus.FAILED.value).label("failed"),
            )
            .where(jobs.c.created_at >= since)
            .group_by(day)
            .order_by(day)
        )
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).mappings().all()
        return [
            {
                "day": r["day"].date().isoformat(),
                "total": r["total"],
                "completed": r["completed"],
                "failed": r["failed"],
            }
            for r in rows
        ]

    def update_status(
        self,
        job_id: str,
        status: JobStatus,
        *,
        result_uri: str | None = None,
        error: str | None = None,
    ) -> None:
        values: dict = {"status": status.value, "updated_at": datetime.now(timezone.utc)}
        if result_uri is not None:
            values["result_uri"] = result_uri
        if error is not None:
            values["error"] = error
        with self._engine.begin() as conn:
            conn.execute(jobs.update().where(jobs.c.job_id == job_id).values(**values))

    def delete(self, job_id: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(jobs.delete().where(jobs.c.job_id == job_id))
