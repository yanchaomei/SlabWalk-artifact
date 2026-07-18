#!/usr/bin/env python3
"""Repartition a single-memory-node GraphBeyond HNSW dump offline."""

from __future__ import annotations

import argparse
import mmap
import os
import re
import stat
import struct
import sys
import tempfile
from pathlib import Path
from typing import Iterator, Sequence


FILE_HEADER_SIZE = 16
REMOTE_NODE_SHIFT = 48
REMOTE_OFFSET_LIMIT = 1 << REMOTE_NODE_SHIFT
REMOTE_OFFSET_MASK = REMOTE_OFFSET_LIMIT - 1
MAX_MEMORY_NODES = 1 << 16

U32 = struct.Struct("<I")
U64 = struct.Struct("<Q")
FILE_HEADER = struct.Struct("<QQ")
RECORD_META = struct.Struct("<QII")
INPUT_NAME = re.compile(
    r"^(?P<prefix>index_m(?P<m>[0-9]+)_efc(?P<efc>[0-9]+))_node1_of1[.]dat$"
)


class RepartitionError(ValueError):
    """Raised when the input or requested output layout is invalid."""


class Record:
    __slots__ = ("uid", "level", "old_offset", "size", "owner", "new_offset")

    def __init__(self, uid: int, level: int, old_offset: int, size: int) -> None:
        self.uid = uid
        self.level = level
        self.old_offset = old_offset
        self.size = size
        self.owner = -1
        self.new_offset = -1


def align8(value: int) -> int:
    return (value + 7) & ~7


def record_size(dim: int, m: int, level: int) -> int:
    m0 = 2 * m
    base = RECORD_META.size + dim * 4 + U32.size + m0 * U64.size
    upper = U32.size + m * U64.size
    return align8(base + level * upper)


def pointer_parts(raw_pointer: int) -> tuple[int, int]:
    return raw_pointer >> REMOTE_NODE_SHIFT, raw_pointer & REMOTE_OFFSET_MASK


def output_paths(input_path: Path, output_dir: Path, m: int, shards: int) -> list[Path]:
    match = INPUT_NAME.fullmatch(input_path.name)
    if match is None:
        raise RepartitionError(
            "input filename must match index_m<M>_efc<E>_node1_of1.dat"
        )
    filename_m = int(match.group("m"))
    if filename_m != m:
        raise RepartitionError(
            f"--m={m} does not match m={filename_m} encoded in the input filename"
        )

    prefix = match.group("prefix")
    return [
        output_dir / f"{prefix}_node{owner + 1}_of{shards}.dat"
        for owner in range(shards)
    ]


def ensure_output_is_available(paths: Sequence[Path], force: bool) -> None:
    if force:
        return
    existing = [path for path in paths if path.exists() or path.is_symlink()]
    if existing:
        rendered = ", ".join(str(path) for path in existing)
        raise RepartitionError(
            f"refusing to overwrite existing output without --force: {rendered}"
        )


def parse_records(
    data: mmap.mmap,
    file_size: int,
    dim: int,
    m: int,
) -> tuple[int, int, list[Record]]:
    if file_size < FILE_HEADER_SIZE:
        raise RepartitionError(
            f"input bounds: file is {file_size} bytes, smaller than the 16-byte header"
        )

    free_ptr, entry_pointer = FILE_HEADER.unpack_from(data, 0)
    if free_ptr != file_size:
        raise RepartitionError(
            f"invalid free_ptr {free_ptr}: expected exact input file size {file_size}"
        )
    if free_ptr <= FILE_HEADER_SIZE:
        raise RepartitionError(
            "invalid free_ptr: dump has no records, but an MN0 entry pointer is required"
        )
    if free_ptr > REMOTE_OFFSET_LIMIT:
        raise RepartitionError(
            f"invalid free_ptr {free_ptr}: one-MN offsets must fit in 48 bits"
        )

    records: list[Record] = []
    seen_uids: set[int] = set()
    walk = FILE_HEADER_SIZE
    m0 = 2 * m

    while walk < free_ptr:
        if free_ptr - walk < RECORD_META.size:
            raise RepartitionError(
                f"record at offset {walk} has a truncated header outside input bounds"
            )

        _, uid, level = RECORD_META.unpack_from(data, walk)
        size = record_size(dim, m, level)
        end = walk + size
        if end > free_ptr:
            raise RepartitionError(
                f"record uid {uid} at offset {walk} extends to {end}, "
                f"past free_ptr {free_ptr}"
            )
        if uid in seen_uids:
            raise RepartitionError(f"duplicate uid {uid} at record offset {walk}")
        seen_uids.add(uid)

        neighborlist_offset = walk + RECORD_META.size + dim * 4
        for current_level in range(level + 1):
            capacity = m0 if current_level == 0 else m
            count = U32.unpack_from(data, neighborlist_offset)[0]
            if count > capacity:
                raise RepartitionError(
                    f"neighbor count {count} exceeds capacity {capacity} for uid {uid} "
                    f"level {current_level}"
                )
            neighborlist_offset += U32.size + capacity * U64.size

        records.append(Record(uid=uid, level=level, old_offset=walk, size=size))
        walk = end

    if walk != free_ptr:
        raise RepartitionError(
            f"record walk ended at {walk}, not exact free_ptr {free_ptr}"
        )
    return free_ptr, entry_pointer, records


def assign_output_layout(
    records: list[Record],
    shards: int,
) -> tuple[list[list[Record]], list[int], dict[int, int]]:
    records.sort(key=lambda record: record.uid)
    records_by_owner: list[list[Record]] = [[] for _ in range(shards)]
    free_ptrs = [FILE_HEADER_SIZE] * shards
    new_pointer_by_old_offset: dict[int, int] = {}

    for record in records:
        owner = record.uid % shards
        new_offset = free_ptrs[owner]
        end = new_offset + record.size
        if end > REMOTE_OFFSET_LIMIT:
            raise RepartitionError(
                f"output shard {owner} exceeds the 48-bit RemotePtr offset space"
            )

        record.owner = owner
        record.new_offset = new_offset
        records_by_owner[owner].append(record)
        free_ptrs[owner] = end
        new_pointer_by_old_offset[record.old_offset] = (
            owner << REMOTE_NODE_SHIFT
        ) | new_offset

    return records_by_owner, free_ptrs, new_pointer_by_old_offset


def live_pointer_positions(
    data: mmap.mmap,
    record: Record,
    dim: int,
    m: int,
) -> Iterator[tuple[int, int, int]]:
    neighborlist_offset = record.old_offset + RECORD_META.size + dim * 4
    for level in range(record.level + 1):
        capacity = 2 * m if level == 0 else m
        count = U32.unpack_from(data, neighborlist_offset)[0]
        for index in range(count):
            pointer_offset = neighborlist_offset + U32.size + index * U64.size
            yield pointer_offset, level, index
        neighborlist_offset += U32.size + capacity * U64.size


def remap_pointer(
    raw_pointer: int,
    new_pointer_by_old_offset: dict[int, int],
    description: str,
) -> int:
    memory_node, old_offset = pointer_parts(raw_pointer)
    if memory_node != 0:
        raise RepartitionError(
            f"invalid {description} pointer 0x{raw_pointer:016x}: "
            f"expected source memory node 0, got {memory_node}"
        )
    try:
        return new_pointer_by_old_offset[old_offset]
    except KeyError as error:
        raise RepartitionError(
            f"invalid {description} pointer 0x{raw_pointer:016x}: "
            f"offset {old_offset} is not a record start"
        ) from error


def validate_pointers(
    data: mmap.mmap,
    entry_pointer: int,
    records: Sequence[Record],
    new_pointer_by_old_offset: dict[int, int],
    dim: int,
    m: int,
) -> int:
    new_entry_pointer = remap_pointer(entry_pointer, new_pointer_by_old_offset, "entry")
    for record in records:
        for pointer_offset, level, index in live_pointer_positions(
            data, record, dim, m
        ):
            raw_pointer = U64.unpack_from(data, pointer_offset)[0]
            remap_pointer(
                raw_pointer,
                new_pointer_by_old_offset,
                f"live neighbor for uid {record.uid} level {level} index {index}",
            )
    return new_entry_pointer


def rewritten_record(
    data: mmap.mmap,
    record: Record,
    new_pointer_by_old_offset: dict[int, int],
    dim: int,
    m: int,
) -> bytearray:
    record_bytes = bytearray(data[record.old_offset : record.old_offset + record.size])
    for pointer_offset, _, _ in live_pointer_positions(data, record, dim, m):
        raw_pointer = U64.unpack_from(data, pointer_offset)[0]
        _, old_offset = pointer_parts(raw_pointer)
        U64.pack_into(
            record_bytes,
            pointer_offset - record.old_offset,
            new_pointer_by_old_offset[old_offset],
        )
    return record_bytes


def write_temporary_outputs(
    data: mmap.mmap,
    paths: Sequence[Path],
    records_by_owner: Sequence[Sequence[Record]],
    free_ptrs: Sequence[int],
    new_entry_pointer: int,
    new_pointer_by_old_offset: dict[int, int],
    dim: int,
    m: int,
    input_mode: int,
) -> list[Path]:
    temporary_paths: list[Path] = []
    try:
        for owner, target_path in enumerate(paths):
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{target_path.name}.",
                suffix=".tmp",
                dir=target_path.parent,
            )
            temporary_path = Path(temporary_name)
            temporary_paths.append(temporary_path)
            os.fchmod(descriptor, input_mode)

            with os.fdopen(descriptor, "wb", buffering=1024 * 1024) as output:
                output.write(FILE_HEADER.pack(free_ptrs[owner], new_entry_pointer))
                for record in records_by_owner[owner]:
                    output.write(
                        rewritten_record(
                            data,
                            record,
                            new_pointer_by_old_offset,
                            dim,
                            m,
                        )
                    )

            actual_size = temporary_path.stat().st_size
            if actual_size != free_ptrs[owner]:
                raise RepartitionError(
                    f"temporary output {temporary_path} has size {actual_size}, "
                    f"expected free_ptr {free_ptrs[owner]}"
                )
        return temporary_paths
    except BaseException:
        for temporary_path in temporary_paths:
            temporary_path.unlink(missing_ok=True)
        raise


def publish_outputs(
    temporary_paths: Sequence[Path],
    target_paths: Sequence[Path],
    force: bool,
) -> None:
    if force:
        for temporary_path, target_path in zip(temporary_paths, target_paths):
            os.replace(temporary_path, target_path)
        return

    published: list[Path] = []
    try:
        for temporary_path, target_path in zip(temporary_paths, target_paths):
            os.link(temporary_path, target_path)
            published.append(target_path)
        for temporary_path in temporary_paths:
            temporary_path.unlink()
    except FileExistsError as error:
        for target_path in published:
            target_path.unlink(missing_ok=True)
        raise RepartitionError(
            f"refusing to overwrite output created concurrently without --force: {error.filename}"
        ) from error


def repartition(
    input_path: Path,
    output_dir: Path,
    dim: int,
    m: int,
    shards: int,
    force: bool,
) -> tuple[int, list[Path], list[int]]:
    if dim <= 0:
        raise RepartitionError(f"--dim must be positive, got {dim}")
    if m <= 0:
        raise RepartitionError(f"--m must be positive, got {m}")
    if shards < 2:
        raise RepartitionError(f"--shards must be at least 2, got {shards}")
    if shards > MAX_MEMORY_NODES:
        raise RepartitionError(
            f"--shards must fit the 16-bit RemotePtr memory-node field, got {shards}"
        )

    paths = output_paths(input_path, output_dir, m, shards)
    output_dir.mkdir(parents=True, exist_ok=True)
    if not output_dir.is_dir():
        raise RepartitionError(f"output path is not a directory: {output_dir}")
    ensure_output_is_available(paths, force)

    temporary_paths: list[Path] = []
    with input_path.open("rb") as input_file:
        input_stat = os.fstat(input_file.fileno())
        file_size = input_stat.st_size
        if file_size == 0:
            raise RepartitionError("input bounds: dump is empty")

        with mmap.mmap(input_file.fileno(), 0, access=mmap.ACCESS_READ) as data:
            _, entry_pointer, records = parse_records(data, file_size, dim, m)
            records_by_owner, free_ptrs, new_pointer_by_old_offset = (
                assign_output_layout(records, shards)
            )
            new_entry_pointer = validate_pointers(
                data,
                entry_pointer,
                records,
                new_pointer_by_old_offset,
                dim,
                m,
            )
            temporary_paths = write_temporary_outputs(
                data,
                paths,
                records_by_owner,
                free_ptrs,
                new_entry_pointer,
                new_pointer_by_old_offset,
                dim,
                m,
                stat.S_IMODE(input_stat.st_mode),
            )

    try:
        publish_outputs(temporary_paths, paths, force)
    finally:
        for temporary_path in temporary_paths:
            temporary_path.unlink(missing_ok=True)

    return len(records), paths, free_ptrs


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Repartition a valid GraphBeyond node1_of1 HNSW dump without "
            "rebuilding or duplicating its graph."
        )
    )
    parser.add_argument("--input", required=True, type=Path, help="one-MN dump")
    parser.add_argument(
        "--output-dir", required=True, type=Path, help="directory for S output dumps"
    )
    parser.add_argument("--dim", required=True, type=int, help="vector dimension")
    parser.add_argument("--m", required=True, type=int, help="HNSW M (M0 is 2*M)")
    parser.add_argument("--shards", required=True, type=int, help="number of MN dumps")
    parser.add_argument(
        "--force", action="store_true", help="replace existing output dumps"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_argument_parser().parse_args(argv)
    try:
        count, paths, free_ptrs = repartition(
            input_path=args.input,
            output_dir=args.output_dir,
            dim=args.dim,
            m=args.m,
            shards=args.shards,
            force=args.force,
        )
    except (OSError, RepartitionError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    print(f"repartitioned {count} records into {args.shards} shards")
    for path, free_ptr in zip(paths, free_ptrs):
        print(f"{path}: {free_ptr} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
