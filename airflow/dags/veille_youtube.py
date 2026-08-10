"""Daily veille DAG — triggers the TOUMAI channel surveillance.

A single HTTP task calls ``POST /veille/run`` on the ingestion API (connection
``toumai_api``, set via ``AIRFLOW_CONN_TOUMAI_API``). The API scans every watched
channel, de-duplicates their recent uploads against already-ingested videos and
queues the newcomers on Kafka — Airflow is only the scheduler/trigger here.

The real work (download + transcription) is done by the existing Kafka worker, so
this DAG stays a thin, dependency-free HTTP call.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from airflow import DAG
from airflow.providers.http.operators.http import HttpOperator

default_args = {
    "owner": "toumai",
    "retries": 2,
    "retry_delay": timedelta(minutes=5),
}

with DAG(
    dag_id="veille_youtube",
    description="Scan quotidien des chaînes surveillées et mise en file des nouvelles vidéos",
    default_args=default_args,
    # Chaque matin à 06:00 (heure du scheduler).
    schedule="0 6 * * *",
    start_date=datetime(2026, 1, 1),
    catchup=False,
    max_active_runs=1,
    tags=["toumai", "veille"],
) as dag:
    run_veille = HttpOperator(
        task_id="run_veille",
        http_conn_id="toumai_api",
        endpoint="/veille/run",
        method="POST",
        # 2xx = succès ; log la réponse JSON (récap {checked, queued, per_channel}).
        response_check=lambda response: response.status_code < 300,
        log_response=True,
    )
