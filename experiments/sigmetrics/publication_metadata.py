#!/usr/bin/env python3
"""Reproducible timestamps and PDF metadata for the final publication path."""

from __future__ import annotations

import os
from datetime import datetime, timezone


def publication_datetime() -> datetime:
    raw = os.environ.get("SOURCE_DATE_EPOCH")
    if raw is None:
        return datetime.now(timezone.utc)
    try:
        epoch = int(raw)
    except ValueError as exc:
        raise ValueError("SOURCE_DATE_EPOCH must be a non-negative integer") from exc
    if epoch < 0:
        raise ValueError("SOURCE_DATE_EPOCH must be a non-negative integer")
    return datetime.fromtimestamp(epoch, timezone.utc)


def publication_timestamp() -> str:
    return publication_datetime().isoformat()


def pdf_metadata() -> dict[str, datetime]:
    return {"CreationDate": publication_datetime()}
