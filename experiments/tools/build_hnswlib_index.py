#!/usr/bin/env python3
"""Build a resumable hnswlib index from GraphBeyond fbin/fvecs data."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import resource
import socket
import struct
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import hnswlib
import numpy as np


class BuildError(ValueError):
    """Raised when the source or requested build protocol is invalid."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def peak_rss_bytes() -> int:
    value = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    return value if sys.platform == "darwin" else value * 1024


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(16 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_json(path: Path, record: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    temporary.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    os.replace(temporary, path)


def hnswlib_version() -> str:
    try:
        return importlib.metadata.version("hnswlib")
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


@dataclass
class MatrixSource:
    path: Path
    source_format: str
    count: int
    dim: int
    matrix: np.ndarray

    def batches(self, start: int, batch_size: int) -> Iterator[tuple[int, int, np.ndarray]]:
        for begin in range(start, self.count, batch_size):
            end = min(begin + batch_size, self.count)
            values = np.ascontiguousarray(self.matrix[begin:end], dtype=np.float32)
            yield begin, end, values


def read_fbin(path: Path) -> MatrixSource:
    if path.stat().st_size < 8:
        raise BuildError("fbin source is smaller than its 8-byte header")
    with path.open("rb") as source:
        count, dim = struct.unpack("<II", source.read(8))
    if count == 0 or dim == 0:
        raise BuildError("fbin count and dimension must be positive")
    expected = 8 + count * dim * 4
    if path.stat().st_size != expected:
        raise BuildError(
            f"fbin size {path.stat().st_size} does not match count={count}, "
            f"dimension={dim}, expected={expected}"
        )
    matrix = np.memmap(path, mode="r", dtype="<f4", offset=8, shape=(count, dim))
    return MatrixSource(path, "fbin", count, dim, matrix)


def read_fvecs(path: Path) -> MatrixSource:
    if path.stat().st_size < 4:
        raise BuildError("fvecs source is smaller than one row header")
    with path.open("rb") as source:
        dim = struct.unpack("<I", source.read(4))[0]
    if dim == 0:
        raise BuildError("fvecs dimension must be positive")
    row_bytes = 4 + dim * 4
    if path.stat().st_size % row_bytes:
        raise BuildError(
            f"fvecs size is not a whole number of dimension={dim} rows"
        )
    count = path.stat().st_size // row_bytes
    raw = np.memmap(path, mode="r", dtype="<i4", shape=(count, dim + 1))
    validation_batch = 1_000_000
    for begin in range(0, count, validation_batch):
        end = min(begin + validation_batch, count)
        if not np.all(raw[begin:end, 0] == dim):
            bad = begin + int(np.flatnonzero(raw[begin:end, 0] != dim)[0])
            raise BuildError(
                f"fvecs row {bad} has a dimension header different from {dim}"
            )
    matrix = raw[:, 1:].view("<f4")
    return MatrixSource(path, "fvecs", count, dim, matrix)


def read_source(path: Path, source_format: str) -> MatrixSource:
    resolved = source_format
    if resolved == "auto":
        resolved = path.suffix.lower().lstrip(".")
    if resolved == "fbin":
        return read_fbin(path)
    if resolved == "fvecs":
        return read_fvecs(path)
    raise BuildError("source format must be fbin or fvecs")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--source-format", choices=("auto", "fbin", "fvecs"), default="auto")
    parser.add_argument("--space", choices=("l2", "ip"), required=True)
    parser.add_argument("--m", type=int, default=16)
    parser.add_argument("--ef-construction", type=int, default=100)
    parser.add_argument("--threads", type=int, default=max(1, os.cpu_count() or 1))
    parser.add_argument("--batch-size", type=int, default=250_000)
    parser.add_argument("--random-seed", type=int, default=47)
    parser.add_argument("--checkpoint-every-batches", type=int, default=0)
    parser.add_argument("--checkpoint", type=Path)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--force", action="store_true")
    return parser.parse_args()


def validate_args(args: argparse.Namespace) -> None:
    for name in ("m", "ef_construction", "threads", "batch_size"):
        if getattr(args, name) <= 0:
            raise BuildError(f"--{name.replace('_', '-')} must be positive")
    if args.checkpoint_every_batches < 0:
        raise BuildError("--checkpoint-every-batches cannot be negative")
    if not args.base.is_file():
        raise BuildError(f"base file does not exist: {args.base}")
    if len({args.base.resolve(), args.output.resolve(), args.manifest.resolve()}) != 3:
        raise BuildError("base, output, and manifest paths must be distinct")


def build(args: argparse.Namespace) -> dict[str, object]:
    validate_args(args)
    progress_path = Path(str(args.manifest) + ".progress")
    checkpoint = args.checkpoint or Path(str(args.output) + ".checkpoint")
    artifacts = (args.output, args.manifest, progress_path)
    if not args.force and any(path.exists() for path in artifacts):
        raise BuildError("refusing to overwrite existing output, manifest, or progress without --force")
    if args.resume:
        if not checkpoint.is_file():
            raise BuildError(f"--resume requires checkpoint: {checkpoint}")
        if args.output.exists():
            raise BuildError("--resume refuses an already published output")
    elif checkpoint.exists():
        if args.force:
            checkpoint.unlink()
        else:
            raise BuildError("checkpoint exists; use --resume or --force")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.manifest.parent.mkdir(parents=True, exist_ok=True)
    checkpoint.parent.mkdir(parents=True, exist_ok=True)
    source = read_source(args.base, args.source_format)
    started_utc = utc_now()
    started = time.monotonic()
    temporary_output = args.output.with_name(f".{args.output.name}.tmp.{os.getpid()}")
    temporary_checkpoint = checkpoint.with_name(f".{checkpoint.name}.tmp.{os.getpid()}")
    completed = 0
    batch_number = 0

    protocol: dict[str, object] = {
        "status": "running",
        "started_utc": started_utc,
        "source_path": str(args.base.resolve()),
        "source_format": source.source_format,
        "output_path": str(args.output.resolve()),
        "checkpoint_path": str(checkpoint.resolve()),
        "count": source.count,
        "dim": source.dim,
        "space": args.space,
        "m": args.m,
        "ef_construction": args.ef_construction,
        "threads": args.threads,
        "batch_size": args.batch_size,
        "random_seed": args.random_seed,
        "checkpoint_every_batches": args.checkpoint_every_batches,
        "host": socket.gethostname(),
        "platform": platform.platform(),
        "python": platform.python_version(),
        "hnswlib_version": hnswlib_version(),
        "hnswlib_module": str(Path(hnswlib.__file__).resolve()),
        "label_policy": "external_label_equals_source_row_id",
    }

    def write_progress(status: str, *, error: str | None = None) -> None:
        elapsed = max(time.monotonic() - started, 1e-9)
        record = {
            **protocol,
            "status": status,
            "updated_utc": utc_now(),
            "completed": completed,
            "remaining": source.count - completed,
            "batch_number": batch_number,
            "elapsed_seconds": elapsed,
            "vectors_per_second": completed / elapsed,
            "peak_rss_bytes": peak_rss_bytes(),
            "checkpoint_present": checkpoint.is_file(),
        }
        if error is not None:
            record["error"] = error
        atomic_json(progress_path, record)

    try:
        index = hnswlib.Index(space=args.space, dim=source.dim)
        if args.resume:
            index.load_index(
                str(checkpoint),
                max_elements=source.count,
                allow_replace_deleted=False,
            )
            completed = int(index.get_current_count())
            if completed < 0 or completed > source.count:
                raise BuildError("checkpoint current count is outside source bounds")
        else:
            index.init_index(
                max_elements=source.count,
                ef_construction=args.ef_construction,
                M=args.m,
                random_seed=args.random_seed,
                allow_replace_deleted=False,
            )
        write_progress("running")

        for begin, end, values in source.batches(completed, args.batch_size):
            labels = np.arange(begin, end, dtype=np.uint64)
            index.add_items(values, labels, num_threads=args.threads)
            completed = end
            batch_number += 1
            if (
                args.checkpoint_every_batches > 0
                and batch_number % args.checkpoint_every_batches == 0
                and completed < source.count
            ):
                temporary_checkpoint.unlink(missing_ok=True)
                index.save_index(str(temporary_checkpoint))
                os.replace(temporary_checkpoint, checkpoint)
            write_progress("running")
            elapsed = time.monotonic() - started
            print(
                f"inserted {completed}/{source.count} "
                f"({completed / max(elapsed, 1e-9):.1f} vectors/s, "
                f"peak_rss={peak_rss_bytes() / (1024**3):.2f} GiB)",
                flush=True,
            )

        if completed != source.count or int(index.get_current_count()) != source.count:
            raise BuildError("hnswlib current count does not match the source count")
        temporary_output.unlink(missing_ok=True)
        index.save_index(str(temporary_output))
        with temporary_output.open("rb") as built_file:
            os.fsync(built_file.fileno())
        os.replace(temporary_output, args.output)
        checkpoint.unlink(missing_ok=True)

        finished_utc = utc_now()
        wall_seconds = time.monotonic() - started
        manifest = {
            **protocol,
            "status": "complete",
            "finished_utc": finished_utc,
            "completed": completed,
            "wall_seconds": wall_seconds,
            "vectors_per_second": completed / max(wall_seconds, 1e-9),
            "peak_rss_bytes": peak_rss_bytes(),
            "source_bytes": args.base.stat().st_size,
            "output_bytes": args.output.stat().st_size,
            "source_sha256": sha256(args.base),
            "output_sha256": sha256(args.output),
        }
        atomic_json(args.manifest, manifest)
        write_progress("complete")
        return manifest
    except BaseException as error:
        write_progress("failed", error=f"{type(error).__name__}: {error}")
        raise
    finally:
        temporary_output.unlink(missing_ok=True)
        temporary_checkpoint.unlink(missing_ok=True)


def main() -> int:
    try:
        args = parse_args()
        manifest = build(args)
    except (BuildError, OSError, RuntimeError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(json.dumps(manifest, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
