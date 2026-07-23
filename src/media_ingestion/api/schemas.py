"""API request/response models."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class ProcessRequest(BaseModel):
    url: str
    languages: list[str] | None = None


class ProcessAccepted(BaseModel):
    job_id: str
    status: str


class JobResponse(BaseModel):
    job_id: str
    url: str
    languages: list[str]
    status: str
    result_uri: str | None = None
    error: str | None = None
    created_at: datetime
    updated_at: datetime
