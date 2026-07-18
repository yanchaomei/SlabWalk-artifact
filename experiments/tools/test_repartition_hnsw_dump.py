#!/usr/bin/env python3
from __future__ import annotations

import struct
import subprocess
import sys
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path


SCRIPT = Path(__file__).with_name("repartition_hnsw_dump.py")
DIM = 3
M = 16
EFC = 17
INPUT_NAME = f"index_m{M}_efc{EFC}_node1_of1.dat"
OFFSET_MASK = (1 << 48) - 1


def align8(value: int) -> int:
    return (value + 7) & ~7


def record_size(level: int, dim: int = DIM, m: int = M) -> int:
    m0 = 2 * m
    return align8(16 + dim * 4 + 4 + m0 * 8 + level * (4 + m * 8))


@dataclass(frozen=True)
class NodeSpec:
    uid: int
    header: int
    level: int
    vector: bytes
    neighbors: tuple[tuple[int, ...], ...]


@dataclass(frozen=True)
class BuildInfo:
    offsets: dict[int, int]
    count_positions: dict[tuple[int, int], int]
    pointer_positions: dict[tuple[int, int, int], int]


@dataclass(frozen=True)
class ParsedRecord:
    offset: int
    header: int
    uid: int
    level: int
    vector: bytes
    neighbor_pointers: tuple[tuple[int, ...], ...]


@dataclass(frozen=True)
class ParsedDump:
    free_ptr: int
    entry: int
    records: tuple[ParsedRecord, ...]


def make_specs() -> tuple[list[NodeSpec], int]:
    levels = {0: 2, 1: 1, 2: 0, 3: 1, 4: 0, 5: 2, 6: 0, 7: 1, 8: 0, 9: 1, 10: 0, 11: 2}
    level1 = [0, 1, 3, 5, 7, 9, 11]
    level2 = [0, 5, 11]
    input_order = [7, 0, 11, 3, 1, 8, 5, 2, 10, 4, 9, 6]
    entry_uid = 5

    specs_by_uid: dict[int, NodeSpec] = {}
    for uid in range(12):
        neighbors: list[tuple[int, ...]] = [
            ((uid - 1) % 12, (uid + 1) % 12, (uid + 4) % 12)
        ]
        if levels[uid] >= 1:
            pos = level1.index(uid)
            neighbors.append((level1[pos - 1], level1[(pos + 1) % len(level1)]))
        if levels[uid] >= 2:
            pos = level2.index(uid)
            neighbors.append((level2[pos - 1], level2[(pos + 1) % len(level2)]))

        header = 0xA5A5000000000000 | (uid << 24) | uid
        if uid == entry_uid:
            header |= 1 << 16
        vector = struct.pack(
            "<III", 0x3F000000 + uid, 0x7FC00000 + uid, 0x80000000 + uid
        )
        specs_by_uid[uid] = NodeSpec(
            uid=uid,
            header=header,
            level=levels[uid],
            vector=vector,
            neighbors=tuple(neighbors),
        )

    return [specs_by_uid[uid] for uid in input_order], entry_uid


def build_dump(path: Path, specs: list[NodeSpec], entry_uid: int) -> BuildInfo:
    offsets: dict[int, int] = {}
    walk = 16
    for spec in specs:
        offsets[spec.uid] = walk
        walk += record_size(spec.level)

    data = bytearray(walk)
    struct.pack_into("<QQ", data, 0, walk, offsets[entry_uid])
    count_positions: dict[tuple[int, int], int] = {}
    pointer_positions: dict[tuple[int, int, int], int] = {}

    for spec in specs:
        offset = offsets[spec.uid]
        size = record_size(spec.level)
        for index in range(size):
            data[offset + index] = (spec.uid * 29 + index * 17 + 3) & 0xFF

        struct.pack_into("<QII", data, offset, spec.header, spec.uid, spec.level)
        data[offset + 16 : offset + 16 + DIM * 4] = spec.vector

        list_offset = offset + 16 + DIM * 4
        for level, neighbor_uids in enumerate(spec.neighbors):
            capacity = 2 * M if level == 0 else M
            count_positions[(spec.uid, level)] = list_offset
            struct.pack_into("<I", data, list_offset, len(neighbor_uids))
            for index, neighbor_uid in enumerate(neighbor_uids):
                pointer_offset = list_offset + 4 + index * 8
                pointer_positions[(spec.uid, level, index)] = pointer_offset
                struct.pack_into("<Q", data, pointer_offset, offsets[neighbor_uid])
            list_offset += 4 + capacity * 8

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return BuildInfo(offsets, count_positions, pointer_positions)


def parse_dump(path: Path, dim: int = DIM, m: int = M) -> ParsedDump:
    data = path.read_bytes()
    free_ptr, entry = struct.unpack_from("<QQ", data, 0)
    if free_ptr != len(data):
        raise AssertionError(f"free_ptr {free_ptr} != file size {len(data)}")

    records: list[ParsedRecord] = []
    walk = 16
    while walk < free_ptr:
        header, uid, level = struct.unpack_from("<QII", data, walk)
        vector = data[walk + 16 : walk + 16 + dim * 4]
        list_offset = walk + 16 + dim * 4
        neighbor_pointers: list[tuple[int, ...]] = []
        for current_level in range(level + 1):
            capacity = 2 * m if current_level == 0 else m
            count = struct.unpack_from("<I", data, list_offset)[0]
            pointers = tuple(
                struct.unpack_from("<Q", data, list_offset + 4 + index * 8)[0]
                for index in range(count)
            )
            neighbor_pointers.append(pointers)
            list_offset += 4 + capacity * 8

        records.append(
            ParsedRecord(
                offset=walk,
                header=header,
                uid=uid,
                level=level,
                vector=vector,
                neighbor_pointers=tuple(neighbor_pointers),
            )
        )
        walk += record_size(level, dim, m)

    if walk != free_ptr:
        raise AssertionError(f"record walk {walk} != free_ptr {free_ptr}")
    return ParsedDump(free_ptr, entry, tuple(records))


def output_paths(output_dir: Path, shards: int) -> list[Path]:
    prefix = f"index_m{M}_efc{EFC}"
    return [
        output_dir / f"{prefix}_node{owner + 1}_of{shards}.dat"
        for owner in range(shards)
    ]


class RepartitionHnswDumpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.specs, self.entry_uid = make_specs()
        self.spec_by_uid = {spec.uid: spec for spec in self.specs}
        self.input_path = self.root / "input" / INPUT_NAME
        self.build_info = build_dump(self.input_path, self.specs, self.entry_uid)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def run_cli(
        self,
        input_path: Path,
        output_dir: Path,
        shards: int,
        *,
        force: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        command = [
            sys.executable,
            str(SCRIPT),
            "--input",
            str(input_path),
            "--output-dir",
            str(output_dir),
            "--dim",
            str(DIM),
            "--m",
            str(M),
            "--shards",
            str(shards),
        ]
        if force:
            command.append("--force")
        return subprocess.run(command, text=True, capture_output=True, check=False)

    def assert_cli_success(self, result: subprocess.CompletedProcess[str]) -> None:
        self.assertEqual(
            result.returncode,
            0,
            msg=f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
        )

    def assert_cli_error(
        self,
        result: subprocess.CompletedProcess[str],
        expected_text: str,
    ) -> None:
        self.assertNotEqual(result.returncode, 0, msg=result.stdout)
        self.assertIn(expected_text.lower(), result.stderr.lower())

    def assert_valid_partition(self, output_dir: Path, shards: int) -> None:
        paths = output_paths(output_dir, shards)
        parsed = [parse_dump(path) for path in paths]
        pointer_to_uid = {
            (owner << 48) | record.offset: record.uid
            for owner, dump in enumerate(parsed)
            for record in dump.records
        }

        expected_entry_owner = self.entry_uid % shards
        expected_entry_record = next(
            record
            for record in parsed[expected_entry_owner].records
            if record.uid == self.entry_uid
        )
        expected_entry = (expected_entry_owner << 48) | expected_entry_record.offset

        for owner, dump in enumerate(parsed):
            expected_uids = sorted(
                uid for uid in self.spec_by_uid if uid % shards == owner
            )
            self.assertEqual([record.uid for record in dump.records], expected_uids)
            self.assertEqual(dump.entry, expected_entry)
            self.assertEqual(pointer_to_uid[dump.entry], self.entry_uid)
            self.assertEqual(dump.entry >> 48, expected_entry_owner)

            expected_size = 16 + sum(
                record_size(self.spec_by_uid[uid].level) for uid in expected_uids
            )
            self.assertEqual(dump.free_ptr, expected_size)
            self.assertEqual(paths[owner].stat().st_size, expected_size)

            for record in dump.records:
                spec = self.spec_by_uid[record.uid]
                self.assertEqual(record.header, spec.header)
                self.assertEqual(record.uid, spec.uid)
                self.assertEqual(record.level, spec.level)
                self.assertEqual(record.vector, spec.vector)
                adjacency_by_uid = tuple(
                    tuple(pointer_to_uid[pointer] for pointer in level)
                    for level in record.neighbor_pointers
                )
                self.assertEqual(adjacency_by_uid, spec.neighbors)
                for level in record.neighbor_pointers:
                    for pointer in level:
                        target_uid = pointer_to_uid[pointer]
                        self.assertEqual(pointer >> 48, target_uid % shards)
                        self.assertGreaterEqual(pointer & OFFSET_MASK, 16)

    def malformed_copy(self, case_name: str) -> tuple[Path, bytearray]:
        path = self.root / case_name / INPUT_NAME
        data = bytearray(self.input_path.read_bytes())
        path.parent.mkdir(parents=True, exist_ok=True)
        return path, data

    def test_repartitions_multilevel_graph_for_three_and_five_shards(self) -> None:
        for shards in (3, 5):
            with self.subTest(shards=shards):
                output_dir = self.root / f"out-{shards}"
                result = self.run_cli(self.input_path, output_dir, shards)
                self.assert_cli_success(result)
                self.assert_valid_partition(output_dir, shards)

    def test_output_is_byte_deterministic(self) -> None:
        first_dir = self.root / "deterministic-a"
        second_dir = self.root / "deterministic-b"
        self.assert_cli_success(self.run_cli(self.input_path, first_dir, 5))
        self.assert_cli_success(self.run_cli(self.input_path, second_dir, 5))

        first_bytes = [path.read_bytes() for path in output_paths(first_dir, 5)]
        second_bytes = [path.read_bytes() for path in output_paths(second_dir, 5)]
        self.assertEqual(first_bytes, second_bytes)

    def test_rejects_free_ptr_outside_input_bounds(self) -> None:
        path, data = self.malformed_copy("bad-free-ptr")
        struct.pack_into("<Q", data, 0, len(data) + 8)
        path.write_bytes(data)

        result = self.run_cli(path, self.root / "bad-free-ptr-out", 3)
        self.assert_cli_error(result, "free_ptr")

    def test_rejects_record_that_extends_past_free_ptr(self) -> None:
        path, data = self.malformed_copy("truncated-record")
        del data[-1]
        struct.pack_into("<Q", data, 0, len(data))
        path.write_bytes(data)

        result = self.run_cli(path, self.root / "truncated-record-out", 3)
        self.assert_cli_error(result, "record")

    def test_rejects_neighbor_count_above_capacity(self) -> None:
        path, data = self.malformed_copy("bad-count")
        count_offset = self.build_info.count_positions[(0, 0)]
        struct.pack_into("<I", data, count_offset, 2 * M + 1)
        path.write_bytes(data)

        result = self.run_cli(path, self.root / "bad-count-out", 3)
        self.assert_cli_error(result, "count")

    def test_rejects_duplicate_uid(self) -> None:
        path, data = self.malformed_copy("duplicate-uid")
        first_uid = self.specs[0].uid
        second_offset = self.build_info.offsets[self.specs[1].uid]
        struct.pack_into("<I", data, second_offset + 8, first_uid)
        path.write_bytes(data)

        result = self.run_cli(path, self.root / "duplicate-uid-out", 3)
        self.assert_cli_error(result, "duplicate uid")

    def test_rejects_malformed_entry_and_live_neighbor_pointers(self) -> None:
        cases: list[tuple[str, int, int]] = [
            ("entry-not-record", 8, self.build_info.offsets[self.entry_uid] + 4),
            (
                "neighbor-not-record",
                self.build_info.pointer_positions[(0, 0, 0)],
                self.build_info.offsets[1] + 4,
            ),
            (
                "neighbor-not-mn0",
                self.build_info.pointer_positions[(0, 0, 0)],
                (1 << 48) | self.build_info.offsets[1],
            ),
        ]
        for case_name, pointer_offset, raw_pointer in cases:
            with self.subTest(case=case_name):
                path, data = self.malformed_copy(case_name)
                struct.pack_into("<Q", data, pointer_offset, raw_pointer)
                path.write_bytes(data)

                result = self.run_cli(path, self.root / f"{case_name}-out", 3)
                self.assert_cli_error(result, "pointer")

    def test_refuses_overwrite_without_force(self) -> None:
        output_dir = self.root / "overwrite"
        self.assert_cli_success(self.run_cli(self.input_path, output_dir, 3))
        paths = output_paths(output_dir, 3)
        original_bytes = [path.read_bytes() for path in paths]

        refused = self.run_cli(self.input_path, output_dir, 3)
        self.assert_cli_error(refused, "overwrite")
        self.assertEqual([path.read_bytes() for path in paths], original_bytes)

        self.assert_cli_success(
            self.run_cli(self.input_path, output_dir, 3, force=True)
        )
        self.assertEqual([path.read_bytes() for path in paths], original_bytes)


if __name__ == "__main__":
    unittest.main(verbosity=2)
