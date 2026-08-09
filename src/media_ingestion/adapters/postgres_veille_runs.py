"""VeilleRunLogPort backed by PostgreSQL (SQLAlchemy Core).

Append-only history of veille passes: each row records when the veille ran, how
many channels it checked, how many videos it queued, and a per-channel breakdown
(stored as JSONB). Powers the monitoring log on the /ui/veille page.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Column,
    DateTime,
    Integer,
    MetaData,
    Table,
    create_engine,
    func,
    select,
)
from sqlalchemy.dialects.postgresql import JSONB

from ..domain.models import VeilleRun

_metadata = MetaData()

veille_runs = Table(
    "veille_runs",
    _metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("ran_at", DateTime(timezone=True), nullable=False),
    Column("checked", Integer, nullable=False),
    Column("queued", Integer, nullable=False),
    Column("detail", JSONB, nullable=False),
)


class PostgresVeilleRunLog:
    def __init__(self, dsn: str) -> None:
        self._engine = create_engine(dsn, future=True)

    def create_schema(self) -> None:
        _metadata.create_all(self._engine)

    def record(self, checked: int, queued: int, detail: list[dict]) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                veille_runs.insert().values(
                    ran_at=datetime.now(timezone.utc),
                    checked=checked,
                    queued=queued,
                    detail=detail,
                )
            )

    def list_recent(self, limit: int = 20) -> list[VeilleRun]:
        stmt = select(veille_runs).order_by(veille_runs.c.ran_at.desc()).limit(limit)
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).mappings().all()
        return [
            VeilleRun(
                run_id=r["id"],
                ran_at=r["ran_at"] or datetime.now(timezone.utc),
                checked=r["checked"],
                queued=r["queued"],
                detail=r["detail"] or [],
            )
            for r in rows
        ]

    def stats(self) -> dict:
        stmt = select(
            func.count().label("runs"),
            func.coalesce(func.sum(veille_runs.c.queued), 0).label("queued_total"),
            func.max(veille_runs.c.ran_at).label("last_ran_at"),
        )
        with self._engine.connect() as conn:
            row = conn.execute(stmt).mappings().one()
        return {
            "runs": int(row["runs"]),
            "queued_total": int(row["queued_total"]),
            "last_ran_at": row["last_ran_at"],
        }
