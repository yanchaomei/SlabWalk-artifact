#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import shutil
import struct
import subprocess
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path

import numpy as np


SOURCE = Path(__file__).with_name("convert_hnswlib_dump.cc")
HNSWLIB_HEADER = struct.Struct("<QQQQQQiIQQQdQ")
GRAPHBEYOND_HEADER = struct.Struct("<QQ")
GRAPHBEYOND_META = struct.Struct("<QII")
ENTRY_NODE_BIT = 1 << 16


@dataclass(frozen=True)
class HnswlibHeader:
    offset_level0: int
    max_elements: int
    count: int
    size_data_per_element: int
    label_offset: int
    data_offset: int
    max_level: int
    entry_internal_id: int
    max_m: int
    max_m0: int
    m: int
    multiplier: float
    ef_construction: int


@dataclass(frozen=True)
class SourceNode:
    label: int
    vector: bytes
    deleted: bool
    neighbors: tuple[tuple[int, ...], ...]


@dataclass(frozen=True)
class OutputNode:
    offset: int
    header: int
    uid: int
    level: int
    vector: bytes
    neighbors: tuple[tuple[int, ...], ...]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_hnswlib(path: Path, dim: int) -> tuple[HnswlibHeader, tuple[SourceNode, ...]]:
    data = path.read_bytes()
    self_header = HnswlibHeader(*HNSWLIB_HEADER.unpack_from(data, 0))
    upper_stride = 4 + self_header.max_m * 4
    upper_cursor = HNSWLIB_HEADER.size + self_header.count * self_header.size_data_per_element
    upper_levels: list[tuple[tuple[int, ...], ...]] = []

    for _ in range(self_header.count):
        link_size = struct.unpack_from("<I", data, upper_cursor)[0]
        upper_cursor += 4
        if link_size % upper_stride:
            raise AssertionError("malformed hnswlib fixture")
        levels: list[tuple[int, ...]] = []
        for _ in range(link_size // upper_stride):
            count = struct.unpack_from("<H", data, upper_cursor)[0]
            levels.append(
                tuple(
                    struct.unpack_from("<I", data, upper_cursor + 4 + 4 * index)[0]
                    for index in range(count)
                )
            )
            upper_cursor += upper_stride
        upper_levels.append(tuple(levels))

    if upper_cursor != len(data):
        raise AssertionError(f"source walk {upper_cursor} != file size {len(data)}")

    nodes: list[SourceNode] = []
    base_start = HNSWLIB_HEADER.size
    for internal_id in range(self_header.count):
        base = base_start + internal_id * self_header.size_data_per_element
        list0 = base + self_header.offset_level0
        count0 = struct.unpack_from("<H", data, list0)[0]
        deleted = bool(data[list0 + 2] & 1)
        neighbors0 = tuple(
            struct.unpack_from("<I", data, list0 + 4 + 4 * index)[0]
            for index in range(count0)
        )
        vector = data[
            base + self_header.data_offset : base + self_header.data_offset + dim * 4
        ]
        label = struct.unpack_from("<Q", data, base + self_header.label_offset)[0]
        nodes.append(
            SourceNode(
                label=label,
                vector=vector,
                deleted=deleted,
                neighbors=(neighbors0, *upper_levels[internal_id]),
            )
        )
    return self_header, tuple(nodes)


def align8(value: int) -> int:
    return (value + 7) & ~7


def make_adjacency(count: int, levels: tuple[int, ...]) -> tuple[tuple[tuple[int, ...], ...], ...]:
    by_level = [
        [internal_id for internal_id, node_level in enumerate(levels) if node_level >= level]
        for level in range(max(levels) + 1)
    ]
    adjacency: list[tuple[tuple[int, ...], ...]] = []
    for internal_id, node_level in enumerate(levels):
        node_neighbors: list[tuple[int, ...]] = [
            tuple(dict.fromkeys(((internal_id - 1) % count, (internal_id + 1) % count,
                                 (internal_id + 5) % count, (internal_id + 13) % count)))
        ]
        for level in range(1, node_level + 1):
            members = by_level[level]
            position = members.index(internal_id)
            node_neighbors.append(
                tuple(dict.fromkeys((members[position - 1], members[(position + 1) % len(members)])))
            )
        adjacency.append(tuple(node_neighbors))
    return tuple(adjacency)


def write_hnswlib_v080_fixture(
    path: Path,
    vectors: np.ndarray,
    labels: np.ndarray,
    levels: tuple[int, ...],
    adjacency: tuple[tuple[tuple[int, ...], ...], ...],
    *,
    m: int,
    ef_construction: int,
    entry_internal_id: int,
    deleted: frozenset[int] = frozenset(),
) -> None:
    count, dim = vectors.shape
    max_m0 = 2 * m
    level0_stride = 4 + max_m0 * 4
    upper_stride = 4 + m * 4
    data_offset = level0_stride
    label_offset = data_offset + dim * 4
    element_stride = label_offset + 8
    header = HNSWLIB_HEADER.pack(
        0,
        count,
        count,
        element_stride,
        label_offset,
        data_offset,
        max(levels),
        entry_internal_id,
        m,
        max_m0,
        m,
        1.0 / np.log(float(m)),
        ef_construction,
    )
    output = bytearray(header)

    for internal_id in range(count):
        base = bytearray(element_stride)
        neighbors0 = adjacency[internal_id][0]
        struct.pack_into("<H", base, 0, len(neighbors0))
        if internal_id in deleted:
            base[2] = 1
        for index, neighbor in enumerate(neighbors0):
            struct.pack_into("<I", base, 4 + 4 * index, neighbor)
        base[data_offset : data_offset + dim * 4] = vectors[internal_id].tobytes()
        struct.pack_into("<Q", base, label_offset, int(labels[internal_id]))
        output.extend(base)

    for internal_id, level in enumerate(levels):
        output.extend(struct.pack("<I", level * upper_stride))
        for current_level in range(1, level + 1):
            block = bytearray(upper_stride)
            neighbors = adjacency[internal_id][current_level]
            struct.pack_into("<H", block, 0, len(neighbors))
            for index, neighbor in enumerate(neighbors):
                struct.pack_into("<I", block, 4 + 4 * index, neighbor)
            output.extend(block)

    path.write_bytes(output)


def parse_graphbeyond(path: Path, dim: int, m: int) -> tuple[int, int, tuple[OutputNode, ...]]:
    data = path.read_bytes()
    free_ptr, entry = GRAPHBEYOND_HEADER.unpack_from(data, 0)
    if free_ptr != len(data):
        raise AssertionError(f"free_ptr {free_ptr} != file size {len(data)}")

    nodes: list[OutputNode] = []
    walk = GRAPHBEYOND_HEADER.size
    while walk < free_ptr:
        header, uid, level = GRAPHBEYOND_META.unpack_from(data, walk)
        vector_start = walk + GRAPHBEYOND_META.size
        vector = data[vector_start : vector_start + dim * 4]
        list_cursor = vector_start + dim * 4
        neighbors: list[tuple[int, ...]] = []
        for current_level in range(level + 1):
            capacity = 2 * m if current_level == 0 else m
            count = struct.unpack_from("<I", data, list_cursor)[0]
            neighbors.append(
                tuple(
                    struct.unpack_from("<Q", data, list_cursor + 4 + 8 * index)[0]
                    for index in range(count)
                )
            )
            list_cursor += 4 + 8 * capacity
        nodes.append(OutputNode(walk, header, uid, level, vector, tuple(neighbors)))
        walk = align8(list_cursor)

    if walk != free_ptr:
        raise AssertionError(f"output walk {walk} != free_ptr {free_ptr}")
    return free_ptr, entry, tuple(nodes)


class ConvertHnswlibDumpTests(unittest.TestCase):
    DIM = 8
    M = 4
    EFC = 32
    COUNT = 64

    @classmethod
    def setUpClass(cls) -> None:
        compiler = shutil.which("c++") or shutil.which("g++") or shutil.which("clang++")
        if compiler is None:
            raise unittest.SkipTest("no C++ compiler available")
        cls.build_dir = tempfile.TemporaryDirectory()
        cls.binary = Path(cls.build_dir.name) / "convert_hnswlib_dump"
        compile_result = subprocess.run(
            [compiler, "-std=c++20", "-O2", "-Wall", "-Wextra", str(SOURCE), "-o", str(cls.binary)],
            text=True,
            capture_output=True,
            check=False,
        )
        if compile_result.returncode:
            raise AssertionError(
                f"converter compilation failed\nstdout:\n{compile_result.stdout}\n"
                f"stderr:\n{compile_result.stderr}"
            )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.build_dir.cleanup()

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        rng = np.random.default_rng(20260714)
        self.vectors = rng.standard_normal((self.COUNT, self.DIM), dtype=np.float32)
        self.labels = np.arange(self.COUNT, dtype=np.uint64)[::-1]
        self.levels = tuple(
            2 if internal_id in (6, 30, 54) else 1 if internal_id % 7 == 0 else 0
            for internal_id in range(self.COUNT)
        )
        self.entry_internal_id = 6
        self.adjacency = make_adjacency(self.COUNT, self.levels)
        self.source = self.root / "source.hnswlib"
        self.output = self.root / f"index_m{self.M}_efc{self.EFC}_node1_of1.dat"
        self.manifest = self.root / "conversion.json"
        self.make_index(self.source, self.labels)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def make_index(
        self,
        path: Path,
        labels: np.ndarray,
        *,
        deleted: frozenset[int] = frozenset(),
    ) -> None:
        write_hnswlib_v080_fixture(
            path,
            self.vectors[: len(labels)],
            labels,
            self.levels[: len(labels)],
            make_adjacency(len(labels), self.levels[: len(labels)]),
            m=self.M,
            ef_construction=self.EFC,
            entry_internal_id=min(self.entry_internal_id, len(labels) - 1),
            deleted=deleted,
        )

    def run_converter(
        self,
        *,
        source: Path | None = None,
        output: Path | None = None,
        dim: int | None = None,
        force: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        command = [
            str(self.binary),
            "--input",
            str(source or self.source),
            "--output",
            str(output or self.output),
            "--dim",
            str(dim or self.DIM),
            "--expect-m",
            str(self.M),
            "--expect-ef-construction",
            str(self.EFC),
            "--manifest",
            str(self.manifest),
        ]
        if force:
            command.append("--force")
        return subprocess.run(command, text=True, capture_output=True, check=False)

    def assert_success(self, result: subprocess.CompletedProcess[str]) -> None:
        self.assertEqual(
            result.returncode,
            0,
            msg=f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
        )

    def assert_error(self, result: subprocess.CompletedProcess[str], text: str) -> None:
        self.assertNotEqual(result.returncode, 0, msg=result.stdout)
        self.assertIn(text.lower(), result.stderr.lower())

    def test_converts_official_v080_multilevel_layout_without_changing_graph(self) -> None:
        source_header, source_nodes = parse_hnswlib(self.source, self.DIM)
        self.assertGreater(source_header.max_level, 0)
        self.assertTrue(any(len(node.neighbors) > 1 for node in source_nodes))

        result = self.run_converter()
        self.assert_success(result)
        _, entry, output_nodes = parse_graphbeyond(self.output, self.DIM, self.M)
        self.assertEqual(len(output_nodes), len(source_nodes))

        pointer_to_internal_id = {node.offset: index for index, node in enumerate(output_nodes)}
        self.assertEqual(pointer_to_internal_id[entry], source_header.entry_internal_id)
        for internal_id, (source_node, output_node) in enumerate(zip(source_nodes, output_nodes)):
            self.assertEqual(output_node.uid, source_node.label)
            self.assertEqual(output_node.level, len(source_node.neighbors) - 1)
            self.assertEqual(output_node.vector, source_node.vector)
            self.assertEqual(bool(output_node.header & ENTRY_NODE_BIT), internal_id == source_header.entry_internal_id)
            self.assertEqual(output_node.header & ~ENTRY_NODE_BIT, 0)
            converted_adjacency = tuple(
                tuple(pointer_to_internal_id[pointer] for pointer in level)
                for level in output_node.neighbors
            )
            self.assertEqual(converted_adjacency, source_node.neighbors)

        manifest = json.loads(self.manifest.read_text())
        self.assertEqual(manifest["format"], "graphbeyond-hnsw-single-mn-v1")
        self.assertEqual(manifest["source_format"], "hnswlib-0.8.0-native-64le")
        self.assertEqual(manifest["count"], self.COUNT)
        self.assertEqual(manifest["dim"], self.DIM)
        self.assertEqual(manifest["m"], self.M)
        self.assertEqual(manifest["ef_construction"], self.EFC)
        self.assertEqual(manifest["source_sha256"], sha256(self.source))
        self.assertEqual(manifest["output_sha256"], sha256(self.output))
        self.assertEqual(
            manifest["post_write_validation"],
            "full_graph_payload_and_pointers",
        )

    def test_output_is_byte_deterministic_and_overwrite_requires_force(self) -> None:
        self.assert_success(self.run_converter())
        original = self.output.read_bytes()
        refused = self.run_converter()
        self.assert_error(refused, "overwrite")
        self.assertEqual(self.output.read_bytes(), original)
        self.assert_success(self.run_converter(force=True))
        self.assertEqual(self.output.read_bytes(), original)

    def test_rejects_dimension_mismatch(self) -> None:
        self.assert_error(self.run_converter(dim=self.DIM + 1), "layout")

    def test_rejects_deleted_nodes(self) -> None:
        deleted_source = self.root / "deleted.hnswlib"
        self.make_index(deleted_source, self.labels, deleted=frozenset({17}))
        self.assert_error(self.run_converter(source=deleted_source), "deleted")

    def test_rejects_labels_that_do_not_fit_graphbeyond_uid(self) -> None:
        labels = np.arange(16, dtype=np.uint64)
        labels[7] = (1 << 32) + 7
        source = self.root / "wide-label.hnswlib"
        self.make_index(source, labels)
        self.assert_error(self.run_converter(source=source), "32-bit")


if __name__ == "__main__":
    unittest.main(verbosity=2)
