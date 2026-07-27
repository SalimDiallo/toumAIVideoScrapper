"""JobStorePort backed by PostgreSQL (SQLAlchemy Core).

Cross-process job status: the API writes PENDING, the worker moves it to
RUNNING/COMPLETED/FAILED, and GET /jobs/{id} reads it back.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    ARRAY,
    Column,
    DateTime,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    select,
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
    Column("created_at", DateTime(timezone=True)),
    Column("updated_at", DateTime(timezone=True)),
)


class PostgresJobStore:
    def __init__(self, dsn: str) -> None:
        self._engine = create_engine(dsn, future=True)

    def create_schema(self) -> None:
        _metadata.create_all(self._engine)

    def create(self, job: Job) -> None:
        stmt = pg_insert(jobs).values(
            job_id=job.job_id,
            url=job.url,
            languages=job.languages,
            status=job.status.value,
            result_uri=job.result_uri,
            error=job.error,
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

    @staticmethod
    def _to_job(row) -> Job:
        return Job(
            job_id=row["job_id"],
            url=row["url"],
            languages=list(row["languages"] or []),
            status=JobStatus(row["status"]),
            result_uri=row["result_uri"],
            error=row["error"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

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
