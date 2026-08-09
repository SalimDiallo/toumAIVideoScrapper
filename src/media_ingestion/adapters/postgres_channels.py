"""ChannelWatchStorePort backed by PostgreSQL (SQLAlchemy Core).

Registry of the channels under daily surveillance. The veille reads the active
channels, lists their recent uploads and queues the new ones; the management UI
adds/removes channels and shows the last check time.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
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

from ..domain.models import WatchedChannel

_metadata = MetaData()

watched_channels = Table(
    "watched_channels",
    _metadata,
    Column("channel_key", String, primary_key=True),
    Column("url", Text, nullable=False),
    Column("name", Text),
    Column("active", Boolean, nullable=False, default=True),
    Column("added_at", DateTime(timezone=True)),
    Column("last_checked_at", DateTime(timezone=True)),
)


class PostgresChannelWatchStore:
    def __init__(self, dsn: str) -> None:
        self._engine = create_engine(dsn, future=True)

    def create_schema(self) -> None:
        _metadata.create_all(self._engine)

    def add(self, channel: WatchedChannel) -> None:
        stmt = pg_insert(watched_channels).values(
            channel_key=channel.channel_key,
            url=channel.url,
            name=channel.name,
            active=channel.active,
            added_at=channel.added_at,
            last_checked_at=channel.last_checked_at,
        )
        # idempotent: re-adding an existing channel is a no-op
        stmt = stmt.on_conflict_do_nothing(index_elements=["channel_key"])
        with self._engine.begin() as conn:
            conn.execute(stmt)

    def list_active(self) -> list[WatchedChannel]:
        return self._list(active_only=True)

    def list_all(self) -> list[WatchedChannel]:
        return self._list(active_only=False)

    def _list(self, *, active_only: bool) -> list[WatchedChannel]:
        stmt = select(watched_channels).order_by(watched_channels.c.added_at.desc())
        if active_only:
            stmt = stmt.where(watched_channels.c.active.is_(True))
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).mappings().all()
        return [self._to_channel(r) for r in rows]

    def remove(self, channel_key: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                watched_channels.delete().where(
                    watched_channels.c.channel_key == channel_key
                )
            )

    def set_active(self, channel_key: str, active: bool) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                watched_channels.update()
                .where(watched_channels.c.channel_key == channel_key)
                .values(active=active)
            )

    def mark_checked(self, channel_key: str, when: datetime) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                watched_channels.update()
                .where(watched_channels.c.channel_key == channel_key)
                .values(last_checked_at=when)
            )

    @staticmethod
    def _to_channel(row) -> WatchedChannel:
        return WatchedChannel(
            channel_key=row["channel_key"],
            url=row["url"],
            name=row["name"],
            active=row["active"] if row["active"] is not None else True,
            added_at=row["added_at"] or datetime.now(timezone.utc),
            last_checked_at=row["last_checked_at"],
        )
