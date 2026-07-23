"""Airflow DAG: orchestrate one video ingestion as retriable steps.

    download >> transcript >> store >> index

Each task has retries; `download` carries an SLA. State flows between tasks via
XCom (JSON dicts). The business logic lives in `media_ingestion.orchestration.steps`
so it stays testable without Airflow. Trigger with a config: {"url": "...", "languages": ["fr"]}.
"""

from __future__ import annotations

from datetime import timedelta

import pendulum
from airflow.decorators import dag, task

from media_ingestion.orchestration import steps

DEFAULT_ARGS = {
    "retries": 3,
    "retry_delay": timedelta(seconds=30),
    "retry_exponential_backoff": True,
}


@dag(
    dag_id="ingest_video",
    schedule=None,  # triggered on demand (manually or via API/TriggerDagRun)
    start_date=pendulum.datetime(2026, 1, 1, tz="UTC"),
    catchup=False,
    default_args=DEFAULT_ARGS,
    params={"url": "", "languages": ["fr", "en"]},
    tags=["toumai", "ingestion"],
)
def ingest_video():
    @task(sla=timedelta(minutes=30))
    def download(**context) -> dict:
        p = context["params"]
        return steps.download_step(p["url"], p["languages"])

    @task
    def transcript(ctx: dict) -> dict:
        return steps.transcript_step(ctx)

    @task
    def store(ctx: dict) -> dict:
        return steps.store_step(ctx)

    @task
    def index(ctx: dict) -> dict:
        return steps.index_step(ctx)

    index(store(transcript(download())))


ingest_video()
