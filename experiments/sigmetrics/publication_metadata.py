#!/usr/bin/env python3
"""Reproducible timestamps and PDF metadata for the final publication path."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


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


def normalize_publication_paths(value: Any, root: Path) -> Any:
    """Make paths inside a publication tree stable after relocation."""
    publication_root = root.resolve(strict=True)

    def normalize(current: Any) -> Any:
        if isinstance(current, dict):
            return {key: normalize(item) for key, item in current.items()}
        if isinstance(current, list):
            return [normalize(item) for item in current]
        if not isinstance(current, str):
            return current
        candidate = Path(current)
        if not candidate.is_absolute():
            return current
        try:
            return candidate.resolve(strict=False).relative_to(
                publication_root
            ).as_posix()
        except ValueError:
            return current

    return normalize(value)


def pdf_metadata() -> dict[str, datetime]:
    return {"CreationDate": publication_datetime()}
