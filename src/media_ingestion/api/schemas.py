"""API request/response models."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class ProcessRequest(BaseModel):
    url: str
    languages: list[str] | None = None


class ProcessAccepted(BaseModel):
    job_id: str
    status: str


class PlaylistRequest(BaseModel):
    # A playlist URL (…/playlist?list=…, watch?v=…&list=…) or a bare playlist id.
    playlist: str
    languages: list[str] | None = None


class BatchItem(BaseModel):
    url: str
    job_id: str
    languages: list[str]


class BatchAccepted(BaseModel):
    accepted: int
    jobs: list[BatchItem]
    errors: list[str] = []


class JobResponse(BaseModel):
    job_id: str
    url: str
    languages: list[str]
    status: str
    result_uri: str | None = None
    error: str | None = None
    created_at: datetime
    updated_at: datetime


class VideoItem(BaseModel):
    model_config = ConfigDict(extra="ignore")

    video_id: str
    url: str
    title: str | None = None
    channel: str | None = None
    duration_s: int | None = None
    upload_date: str | None = None
    language: str | None = None
    transcript_status: str | None = None
    transcript_source: str | None = None
    storage_uri: str | None = None
    created_at: datetime | None = None
