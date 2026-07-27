"""MetadataRepositoryPort backed by PostgreSQL (SQLAlchemy Core).

Catalog of ingested videos — one idempotent row per video_id. This is the
queryable index; the raw artifacts live in object storage (MinIO Bronze).
"""

from __future__ import annotations

import structlog
from sqlalchemy import (
    Column,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    select,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert

from ..domain.models import IngestionResult

log = structlog.get_logger(__name__)

_metadata = MetaData()

videos = Table(
    "videos",
    _metadata,
    Column("video_id", String, primary_key=True),
    Column("url", Text, nullable=False),
    Column("title", Text),
    Column("channel", Text),
    Column("duration_s", Integer),
    Column("upload_date", String),
    Column("language", String, index=True),
    Column("transcript_status", String),
    Column("transcript_source", String),
    Column("storage_uri", Text),
    Column("created_at", DateTime(timezone=True)),
)


class PostgresMetadataRepository:
    def __init__(self, dsn: str) -> None:
        # dsn example: postgresql+psycopg://toumai:toumai@localhost:5432/toumai
        self._engine = create_engine(dsn, future=True)

    def create_schema(self) -> None:
        _metadata.create_all(self._engine)

    def upsert(self, result: IngestionResult, language: str, storage_uri: str) -> None:
        m = result.metadata
        values = {
            "video_id": m.video_id,
            "url": m.url,
            "title": m.title,
            "channel": m.channel,
            "duration_s": m.duration_s,
            "upload_date": m.upload_date,
            "language": language,
            "transcript_status": result.transcript_status.value,
            "transcript_source": result.transcript.source.value if result.transcript else None,
            "storage_uri": storage_uri,
            "created_at": result.created_at,
        }
        stmt = pg_insert(videos).values(**values)
        stmt = stmt.on_conflict_do_update(
            index_elements=["video_id"],
            set_={k: stmt.excluded[k] for k in values if k != "video_id"},
        )
        with self._engine.begin() as conn:
            conn.execute(stmt)

    def list(self, *, language: str | None = None, limit: int = 50, offset: int = 0) -> list[dict]:
        stmt = select(videos).order_by(videos.c.created_at.desc()).limit(limit).offset(offset)
        if language is not None:
            stmt = stmt.where(videos.c.language == language)
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).mappings().all()
        return [dict(r) for r in rows]

    def get(self, video_id: str) -> dict | None:
        stmt = select(videos).where(videos.c.video_id == video_id)
        with self._engine.connect() as conn:
            row = conn.execute(stmt).mappings().first()
        return dict(row) if row is not None else None

    def delete(self, video_id: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(videos.delete().where(videos.c.video_id == video_id))
