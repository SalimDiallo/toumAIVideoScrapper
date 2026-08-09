"""CLI entry point: `toumai-veille` — run one veille pass over watched channels.

Lists each active channel's recent uploads and queues the new ones as ingestion
jobs (the Kafka worker then downloads/transcribes them). Meant for manual/local
runs and as a fallback to the Airflow DAG, which triggers the same logic through
``POST /veille/run``.
"""

from __future__ import annotations

import argparse
import sys

from ..bootstrap import (
    build_channel_resolver,
    build_channel_store,
    build_job_store,
    build_publisher,
    build_veille_run_log,
)
from ..config import Settings
from ..logging_setup import configure_logging


def build_use_case(settings: Settings):
    from ..application.watch_channels import WatchChannelsUseCase

    return WatchChannelsUseCase(
        channel_store=build_channel_store(settings),
        resolver=build_channel_resolver(settings),
        job_store=build_job_store(settings),
        publisher=build_publisher(settings),
        topic=settings.topic_job_requested,
        default_languages=list(settings.languages),
        recent_limit=settings.veille_recent_limit,
        run_log=build_veille_run_log(settings),
    )


def main(argv: list[str] | None = None) -> int:
    settings = Settings()
    parser = argparse.ArgumentParser(prog="toumai-veille", description=__doc__)
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    configure_logging(args.log_level)
    summary = build_use_case(settings).run_once()

    print(f"veille : {summary['checked']} chaîne(s) vérifiée(s), {summary['queued']} job(s) créé(s)")
    for row in summary["per_channel"]:
        label = row.get("name") or row["channel_key"]
        print(f"  - {label}: {row['new']} nouvelle(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
