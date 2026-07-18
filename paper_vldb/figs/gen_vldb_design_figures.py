#!/usr/bin/env python3
"""Generate the five editable design figures used by the PVLDB paper.

PDF output requires pypdf, pdffonts, and either Inkscape or rsvg-convert.
Publication PDFs must retain embedded, non-Type-3 text fonts.
"""

from __future__ import annotations

import argparse
import csv
from html import escape
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET


FIG_DIR = Path(__file__).resolve().parent
REPO_ROOT = FIG_DIR.parents[1]
CACHE_CSV = (
    REPO_ROOT
    / "results"
    / "vldb_final_evidence"
    / "cache_control"
    / "summary"
    / "summary.csv"
)
PROFILE_CSV = (
    REPO_ROOT
    / "results"
    / "vldb_final_evidence"
    / "query_profile"
    / "summary"
    / "summary.csv"
)

INK = "#263442"
NAVY = "#0D426A"
MUTED = "#667085"
GRID = "#A1AAB6"
PALE = "#F7F9FB"
GREY = "#E9EDF2"
WHITE = "#FFFFFF"
BLUE = "#0D6EB5"
BLUE_L = "#DCECF8"
TEAL = "#147A73"
TEAL_L = "#D9EEEC"
GREEN = "#2F7D46"
GREEN_L = "#DDEEDC"
ORANGE = "#CF6B00"
ORANGE_L = "#FFE3C2"
PURPLE = "#5B3F8F"
PURPLE_L = "#E7DCF3"
RED = "#C24F4B"
RED_L = "#F6D8D2"

_SVG_NS = "http://www.w3.org/2000/svg"
_SVG_ROOT = f"{{{_SVG_NS}}}svg"
_SVG_TEXT = f"{{{_SVG_NS}}}text"
_SVG_STYLE = f"{{{_SVG_NS}}}style"
_NON_RENDERING_CONTAINERS = {
    f"{{{_SVG_NS}}}{name}"
    for name in (
        "defs",
        "metadata",
        "style",
        "script",
        "title",
        "desc",
        "symbol",
        "clipPath",
        "mask",
        "pattern",
    )
}
_RESTRICTED_STYLE = re.compile(
    r"(?:^|;)\s*(?:font(?:-size)?|display|visibility|opacity|fill-opacity|fill)\s*:",
    re.IGNORECASE,
)
_NUMERIC_OPACITY = re.compile(
    r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?"
)
_OPAQUE_FILL = re.compile(r"#[0-9A-Fa-f]{6}")
_TYPE_3_FONT = re.compile(r"\btype\s*3\b", re.IGNORECASE)
_AUTO_ARROW = object()
_MARKER_BY_COLOR = {
    INK: "aInk",
    NAVY: "aNavy",
    BLUE: "aBlue",
    TEAL: "aTeal",
    GREEN: "aGreen",
    ORANGE: "aOrange",
    PURPLE: "aPurple",
    RED: "aRed",
    MUTED: "aMuted",
}


FIGURE_CONTRACTS = {
    "fig_physical_units": (
        (1440, 540),
        (
            "Node / vector",
            "Expansion",
            "Routed partition",
            "Measured motivation: same path, more cache",
            "cache budget (%)",
            "n=5; 95% CI",
            "posts remaining",
            "QPS retained",
            "CN self-time",
            "payload L+3V",
            "state beam/cache",
            "state route+p3",
            "route+fetch",
        ),
        18,
    ),
    "overview": (
        (1440, 610),
        (
            "online query",
            "authoritative HNSW",
            "Slab regions",
            "registered bytes only",
            "level-0 seed",
            "B[42]",
            "prefix[42]",
            "top-R",
        ),
        20,
    ),
    "fig_slab_layout": (
        (1440, 570),
        (
            "fN",
            "hot prefix",
            "d(u)",
            "fixed capacity 5",
            "pay for d(u), not capacity",
            "B[u]",
            "nbr_slot",
            "nbr_rptr",
            "nbr_qvec",
            "static budget map",
            "recorded in descriptor",
            "metric + dimension guard",
            "exact rerank",
            "approx. SIMD score",
            "evaluated TTI inner-product boundary",
        ),
        20,
    ),
    "fig_search_placement": (
        (1440, 570),
        (
            "resident upper graph",
            "READ B[u]",
            "top-R",
            "u mod S",
            "cold fallback",
            "owners striped",
        ),
        20,
    ),
    "fig_construction_refresh": (
        (1440, 550),
        (
            "64 MiB scan",
            "write + verify descriptor",
            "service paused",
            "rewrite touched B[u]",
            "full-region byte compare",
        ),
        18,
    ),
}


def _style_properties(style: str) -> dict[str, str]:
    properties = {}
    for declaration in style.split(";"):
        name, separator, value = declaration.partition(":")
        if not separator:
            continue
        value = re.sub(r"\s*!important\s*$", "", value, flags=re.IGNORECASE)
        properties[name.strip().lower()] = value.strip().lower()
    return properties


def _is_nonpositive_opacity(value, property_name: str) -> bool:
    if value is None:
        return False
    original = value
    value = value.strip()
    if value.endswith("%"):
        value = value[:-1].strip()
    if _NUMERIC_OPACITY.fullmatch(value) is None:
        raise ValueError(f"malformed {property_name} value {original!r}")
    return float(value) <= 0.0


def _has_visible_fill(element: ET.Element) -> bool:
    fill = element.get("fill")
    return fill is not None and _OPAQUE_FILL.fullmatch(fill) is not None


def _is_hidden(element: ET.Element) -> bool:
    display = element.get("display", "").strip().lower()
    visibility = element.get("visibility", "").strip().lower()
    style = _style_properties(element.get("style", ""))
    opacity_hidden = _is_nonpositive_opacity(element.get("opacity"), "opacity")
    fill_opacity_hidden = _is_nonpositive_opacity(
        element.get("fill-opacity"), "fill-opacity"
    )
    style_opacity_hidden = _is_nonpositive_opacity(style.get("opacity"), "opacity")
    style_fill_opacity_hidden = _is_nonpositive_opacity(
        style.get("fill-opacity"), "fill-opacity"
    )
    return (
        display == "none"
        or visibility in {"hidden", "collapse"}
        or opacity_hidden
        or fill_opacity_hidden
        or style.get("display") == "none"
        or style.get("visibility") in {"hidden", "collapse"}
        or style_opacity_hidden
        or style_fill_opacity_hidden
    )


def _renderable_text_elements(root: ET.Element) -> list[ET.Element]:
    elements = []

    def visit(element: ET.Element, suppressed: bool) -> None:
        suppressed = (
            suppressed
            or element.tag in _NON_RENDERING_CONTAINERS
            or _is_hidden(element)
        )
        if element.tag == _SVG_TEXT and not suppressed:
            elements.append(element)
        for child in element:
            visit(child, suppressed)

    visit(root, False)
    return elements


def _renderable_text(element: ET.Element) -> str:
    chunks = []

    def collect(node: ET.Element, suppressed: bool) -> None:
        suppressed = (
            suppressed
            or node.tag in _NON_RENDERING_CONTAINERS
            or _is_hidden(node)
            or (node.get("fill") is not None and not _has_visible_fill(node))
        )
        if suppressed:
            return
        if node.text:
            chunks.append(node.text)
        for child in node:
            collect(child, suppressed)
            if child.tail:
                chunks.append(child.tail)

    collect(element, False)
    return " ".join(" ".join(chunks).split())


def visible_text(root: ET.Element) -> str:
    labels = [
        _renderable_text(element)
        for element in _renderable_text_elements(root)
        if _has_visible_fill(element)
    ]
    return " ".join(" ".join(labels).split())


def _validate_slab_layout_semantics(root: ET.Element) -> None:
    labeled: dict[str, list[ET.Element]] = {}
    for element in _renderable_text_elements(root):
        if _has_visible_fill(element):
            labeled.setdefault(_renderable_text(element), []).append(element)

    def one(label: str, *, below: float | None = None) -> ET.Element:
        matches = labeled.get(label, [])
        if below is not None:
            matches = [item for item in matches if float(item.get("y", "0")) > below]
        if len(matches) != 1:
            raise ValueError(
                f"fig_slab_layout: expected one {label!r} label, got {len(matches)}"
            )
        return matches[0]

    def coordinate(element: ET.Element, axis: str) -> float:
        value = element.get(axis)
        if value is None:
            raise ValueError(f"fig_slab_layout: {axis} coordinate is required")
        return float(value)

    def font_size(element: ET.Element) -> float:
        value = element.get("font-size")
        if value is None:
            raise ValueError("fig_slab_layout: visible labels require font-size")
        return float(value)

    panel_titles = (
        "(a) degree-bounded materialization",
        "(b) live-prefix records",
        "(c) configured scoring code",
        "(d) canonical B[u] byte layout",
    )
    for label in panel_titles:
        if font_size(one(label)) < 23:
            raise ValueError(f"fig_slab_layout: panel title {label!r} must be >= 23")

    operational_labels = (
        "in-degree rank",
        "hot prefix",
        "Slabs",
        "fixed capacity 5: B[42], d(42)=3, live IDs {23,57,81}",
        "variable d(u): 3 live entries",
        "pay for d(u), not capacity",
        "offline configuration",
        "metric + dimension guard",
        "evaluated TTI inner-product boundary",
        "next hop",
        "exact rerank",
        "approx. SIMD score",
    )
    for label in operational_labels:
        if font_size(one(label)) < 22:
            raise ValueError(
                f"fig_slab_layout: operational label {label!r} must be >= 22"
            )

    header = one("header")
    count = one("count=3")
    bits = one("bits")
    flags = one("flags")
    field_y = [coordinate(item, "y") for item in (count, bits, flags)]
    field_x = [coordinate(item, "x") for item in (count, bits, flags)]
    if max(field_y) - min(field_y) > 0.1 or not field_x[0] < field_x[1] < field_x[2]:
        raise ValueError(
            "fig_slab_layout: header fields must be ordered count, bits, flags"
        )
    if coordinate(header, "y") >= min(field_y):
        raise ValueError(
            "fig_slab_layout: header label must sit above count, bits, flags"
        )

    field_labels = ("nbr_slot", "nbr_rptr", "nbr_qvec")
    field_columns = []
    for label in field_labels:
        column = sorted(labeled.get(label, []), key=lambda item: coordinate(item, "y"))
        if len(column) != 3:
            raise ValueError(
                f"fig_slab_layout: expected three repeated {label!r} fields"
            )
        field_columns.append(column)

    left_brackets = sorted(labeled.get("<", []), key=lambda item: coordinate(item, "y"))
    right_brackets = sorted(labeled.get(">", []), key=lambda item: coordinate(item, "y"))
    if len(left_brackets) != 3 or len(right_brackets) != 3:
        raise ValueError("fig_slab_layout: every neighbor tuple needs angle brackets")

    for row_index, identifier in enumerate((23, 57, 81)):
        row_id = one(str(identifier), below=400)
        slot, rptr, qvec = (column[row_index] for column in field_columns)
        row = (left_brackets[row_index], slot, rptr, qvec, right_brackets[row_index])
        row_y = [coordinate(item, "y") for item in row]
        row_x = [coordinate(item, "x") for item in row]
        if max(row_y) - min(row_y) > 0.1 or row_y[0] != coordinate(row_id, "y"):
            raise ValueError(
                f"fig_slab_layout: ID {identifier} must align with its byte tuple"
            )
        if not coordinate(row_id, "x") < row_x[0] < row_x[1] < row_x[2] < row_x[3] < row_x[4]:
            raise ValueError(
                "fig_slab_layout: tuple order must be "
                "<nbr_slot nbr_rptr nbr_qvec> with the ID outside"
            )

    if any(label in labeled for label in ("ID 23", "ID 57", "ID 81")):
        raise ValueError("fig_slab_layout: row IDs cannot be stored peer fields")

    exact_rerank = one("exact rerank")
    approx_score = one("approx. SIMD score")
    if coordinate(exact_rerank, "x") != coordinate(field_columns[1][0], "x"):
        raise ValueError("fig_slab_layout: exact rerank must consume nbr_rptr")
    if coordinate(approx_score, "x") != coordinate(field_columns[2][0], "x"):
        raise ValueError("fig_slab_layout: approximate SIMD scoring must consume nbr_qvec")


def _validate_search_placement_semantics(root: ET.Element) -> None:
    labeled: dict[str, list[ET.Element]] = {}
    for element in _renderable_text_elements(root):
        if _has_visible_fill(element):
            labeled.setdefault(_renderable_text(element), []).append(element)

    def one(label: str) -> ET.Element:
        matches = labeled.get(label, [])
        if len(matches) != 1:
            raise ValueError(
                f"fig_search_placement: expected one {label!r} label, got {len(matches)}"
            )
        return matches[0]

    def coordinate(element: ET.Element, axis: str) -> float:
        value = element.get(axis)
        if value is None:
            raise ValueError(f"fig_search_placement: {axis} coordinate is required")
        return float(value)

    def font_size(element: ET.Element) -> float:
        value = element.get("font-size")
        if value is None:
            raise ValueError(
                "fig_search_placement: visible labels require font-size"
            )
        return float(value)

    def roles(role: str) -> list[ET.Element]:
        return [element for element in root.iter() if element.get("data-role") == role]

    def group_text(group: ET.Element) -> str:
        chunks = [
            _renderable_text(element)
            for element in group.iter(_SVG_TEXT)
            if _has_visible_fill(element)
        ]
        return " ".join(" ".join(chunks).split())

    panel_titles = (
        "(a) resident upper graph",
        "(b) one-read expansion",
        "(c) block-cyclic placement",
    )
    for label in panel_titles:
        if font_size(one(label)) < 23:
            raise ValueError(
                f"fig_search_placement: panel title {label!r} must be >= 23"
            )

    principal_labels = (
        "exact fp32 descent",
        "D: 0 remote READs",
        "pop u",
        "prefix[u]",
        "READ B[u]",
        "approx. SIMD score",
        "beam update",
        "top-R survivors",
        "READ authoritative fp32",
        "exact rerank",
        "owner(u) = u mod S",
        "no router",
    )
    for label in principal_labels:
        if font_size(one(label)) < 22:
            raise ValueError(
                f"fig_search_placement: principal label {label!r} must be >= 22"
            )

    operations = tuple(
        one(label)
        for label in (
            "pop u",
            "prefix[u]",
            "READ B[u]",
            "approx. SIMD score",
            "beam update",
        )
    )
    operation_x = [coordinate(element, "x") for element in operations]
    if operation_x != sorted(operation_x) or len(set(operation_x)) != len(operation_x):
        raise ValueError(
            "fig_search_placement: expansion operations must read left-to-right"
        )

    loops = roles("beam-loop")
    if len(loops) != 1:
        raise ValueError("fig_search_placement: expected one beam-loop arrow")
    loop = loops[0]
    points = [
        tuple(float(value) for value in point.split(","))
        for point in loop.get("points", "").split()
    ]
    if (
        len(points) < 4
        or loop.get("marker-end") != "url(#aGreen)"
        or points[-1][0] >= points[0][0]
        or abs(points[-1][0] - coordinate(one("pop u"), "x")) > 1.0
    ):
        raise ValueError(
            "fig_search_placement: beam loop must point from update back to pop"
        )

    beam_exits = roles("beam-exit")
    if len(beam_exits) != 1:
        raise ValueError(
            "fig_search_placement: beam termination must feed top-R survivors"
        )
    beam_exit = beam_exits[0]
    if (
        beam_exit.get("data-from") != "beam update"
        or beam_exit.get("data-to") != "top-R survivors"
        or beam_exit.get("marker-end") != "url(#aPurple)"
        or float(beam_exit.get("y2", "0")) <= float(beam_exit.get("y1", "0"))
    ):
        raise ValueError(
            "fig_search_placement: top-R causality must start at terminated beam state"
        )

    resident = roles("resident-boundary")
    seed = roles("level0-seed")
    if len(resident) != 1 or len(seed) != 1:
        raise ValueError(
            "fig_search_placement: resident boundary and level-0 seed are required"
        )
    seed_rects = [element for element in seed[0] if element.tag == f"{{{_SVG_NS}}}rect"]
    if len(seed_rects) != 1:
        raise ValueError("fig_search_placement: level-0 seed must be one object")
    resident_bottom = float(resident[0].get("y", "0")) + float(
        resident[0].get("height", "0")
    )
    if float(seed_rects[0].get("y", "0")) <= resident_bottom:
        raise ValueError(
            "fig_search_placement: level-0 seed cannot be inside resident state"
        )
    one("remote level 0")
    one("not resident")

    approximate = roles("approx-stage")
    exact = roles("exact-stage")
    if len(approximate) != 1 or len(exact) != 1:
        raise ValueError(
            "fig_search_placement: approximate and exact stages must be distinct"
        )
    approx_rect = next(
        (element for element in approximate[0] if element.tag == f"{{{_SVG_NS}}}rect"),
        None,
    )
    exact_rect = next(
        (element for element in exact[0] if element.tag == f"{{{_SVG_NS}}}rect"),
        None,
    )
    if (
        approx_rect is None
        or exact_rect is None
        or approx_rect.get("stroke") != ORANGE
        or exact_rect.get("stroke") != BLUE
    ):
        raise ValueError(
            "fig_search_placement: approximate scoring and exact rerank need semantic strokes"
        )
    one("qvec: approximate")
    one("rptr: fp32 address")
    one("d(u) live entries")
    one("done")
    one("{23,57} + rptr")

    rptr_legends = roles("rptr-legend")
    if len(rptr_legends) != 1 or any(
        element.get("marker-end") is not None for element in rptr_legends[0].iter()
    ):
        raise ValueError(
            "fig_search_placement: rptr legend must not feed top-R survivors"
        )
    rerank_reads = roles("rerank-read-edge")
    exact_edges = roles("exact-rerank-edge")
    if (
        len(rerank_reads) != 1
        or rerank_reads[0].get("data-from") != "top-R survivors"
        or rerank_reads[0].get("data-to") != "READ authoritative fp32"
        or rerank_reads[0].get("marker-end") != "url(#aPurple)"
        or len(exact_edges) != 1
        or exact_edges[0].get("data-from") != "READ authoritative fp32"
        or exact_edges[0].get("data-to") != "exact rerank"
        or exact_edges[0].get("marker-end") != "url(#aBlue)"
    ):
        raise ValueError(
            "fig_search_placement: rerank chain must be top-R, fp32 READ, exact rerank"
        )

    entries = roles("slab-entry")
    if {entry.get("data-id") for entry in entries} != {"23", "57", "81"}:
        raise ValueError(
            "fig_search_placement: B[42] must expose live IDs 23, 57, and 81"
        )
    for entry in entries:
        label = entry.get("data-id")
        content = group_text(entry)
        if label not in content or "qvec" not in content or "rptr" not in content:
            raise ValueError(
                f"fig_search_placement: live entry {label} needs ID, qvec, and rptr"
            )

    fallbacks = roles("cold-fallback")
    if len(fallbacks) != 1:
        raise ValueError("fig_search_placement: expected one cold fallback lane")
    fallback_content = group_text(fallbacks[0])
    if "missing B[96]" not in fallback_content or "authoritative list/vector path" not in fallback_content:
        raise ValueError(
            "fig_search_placement: cold fallback must reach the authoritative path"
        )
    fallback_arrows = [
        element
        for element in fallbacks[0].iter()
        if element.get("marker-end") is not None
    ]
    if len(fallback_arrows) < 2 or any(
        element.get("stroke-dasharray") is None for element in fallback_arrows
    ):
        raise ValueError(
            "fig_search_placement: cold fallback arrows must remain dashed"
        )
    fallback_rejoins = roles("fallback-rejoin")
    if (
        len(fallback_rejoins) != 1
        or fallback_rejoins[0] not in set(fallbacks[0].iter())
        or fallback_rejoins[0].get("data-to") != "beam update"
        or fallback_rejoins[0].get("marker-end") != "url(#aRed)"
        or fallback_rejoins[0].get("stroke-dasharray") is None
    ):
        raise ValueError(
            "fig_search_placement: cold fallback must rejoin the beam update"
        )
    one("return to beam")

    records = roles("placement-record")
    if len(records) != 12:
        raise ValueError(
            f"fig_search_placement: expected 12 intact placement records, got {len(records)}"
        )
    seen = set()
    for record_group in records:
        index = int(record_group.get("data-record", "-1"))
        owner = record_group.get("data-owner")
        if index in seen or owner != f"MN{index % 3}":
            raise ValueError(
                f"fig_search_placement: B[{index}] is not intact on owner MN{index % 3}"
            )
        seen.add(index)
        child_rects = [
            element
            for element in record_group
            if element.tag == f"{{{_SVG_NS}}}rect"
        ]
        if len(child_rects) != 1 or group_text(record_group) != f"B[{index}]":
            raise ValueError(
                f"fig_search_placement: B[{index}] must be one complete record object"
            )
    if seen != set(range(12)):
        raise ValueError("fig_search_placement: placement must cover B[0] through B[11]")

    direct_reads = roles("direct-read")
    expected_reads = {("1", "MN1"), ("2", "MN2")}
    actual_reads = {
        (element.get("data-record"), element.get("data-owner"))
        for element in direct_reads
    }
    if actual_reads != expected_reads or any(
        element.get("marker-end") != "url(#aGreen)" for element in direct_reads
    ):
        raise ValueError(
            "fig_search_placement: CN workers must read distinct owners directly"
        )
    workers = roles("cn-worker")
    if len(workers) != 2:
        raise ValueError("fig_search_placement: expected two visible CN workers")
    for worker in workers:
        worker_rects = [
            element for element in worker if element.tag == f"{{{_SVG_NS}}}rect"
        ]
        command = one(f'READ B[{worker.get("data-record")}]')
        if (
            len(worker_rects) != 1
            or float(worker_rects[0].get("width", "0")) < 124
            or font_size(command) != 20
        ):
            raise ValueError(
                "fig_search_placement: CN READ labels need 20pt type and padded boxes"
            )
    if roles("router"):
        raise ValueError("fig_search_placement: a central router is forbidden")
    one("each B[u] intact no split / no inter-MN assembly")
    one("one global HNSW beam")

    content = visible_text(root)
    one("owners striped")
    if "bytes striped" in content:
        raise ValueError(
            "fig_search_placement: cost rail cannot imply split record bytes"
        )
    for forbidden in ("construction", "epoch", "publish"):
        if re.search(rf"\b{forbidden}\b", content, re.IGNORECASE):
            raise ValueError(
                f"fig_search_placement: lifecycle token {forbidden!r} is out of scope"
            )


def _validate_construction_refresh_semantics(root: ET.Element) -> None:
    content = visible_text(root)

    def roles(role: str) -> list[ET.Element]:
        return [
            element
            for element in root.iter()
            if role in element.get("data-role", "").split()
        ]

    required_roles = {
        "authoritative-source": 2,
        "build-stage": 6,
        "descriptor-commit": 1,
        "service-gate": 1,
        "replay-suffix": 1,
        "touched-set": 1,
        "verification": 1,
        "measured-control": 4,
    }
    for role, expected in required_roles.items():
        actual = len(roles(role))
        if actual != expected:
            raise ValueError(
                f"fig_construction_refresh: expected {expected} {role!r} "
                f"groups, got {actual}"
            )

    stages = sorted(
        int(group.get("data-stage", "-1")) for group in roles("build-stage")
    )
    if stages != [1, 2, 3, 4, 5, 6]:
        raise ValueError(
            "fig_construction_refresh: build stages must be numbered 1-6"
        )

    for forbidden in (
        "epoch",
        "scratch copy",
        "pointer switch",
        "concurrent update",
        "lock-free",
        "crash recovery",
    ):
        if forbidden in content.lower():
            raise ValueError(
                f"fig_construction_refresh: unsupported claim {forbidden!r}"
            )


def _validate_construction_refresh_semantics_legacy(root: ET.Element) -> None:
    labeled: dict[str, list[ET.Element]] = {}
    for element in _renderable_text_elements(root):
        if _has_visible_fill(element):
            labeled.setdefault(_renderable_text(element), []).append(element)

    def one(label: str) -> ET.Element:
        matches = labeled.get(label, [])
        if len(matches) != 1:
            raise ValueError(
                f"fig_construction_refresh: expected one {label!r} label, "
                f"got {len(matches)}"
            )
        return matches[0]

    def coordinate(element: ET.Element, axis: str) -> float:
        value = element.get(axis)
        if value is None:
            raise ValueError(
                f"fig_construction_refresh: {axis} coordinate is required"
            )
        return float(value)

    def font_size(element: ET.Element) -> float:
        value = element.get("font-size")
        if value is None:
            raise ValueError(
                "fig_construction_refresh: visible labels require font-size"
            )
        return float(value)

    def roles(role: str) -> list[ET.Element]:
        return [
            element
            for element in root.iter()
            if role in element.get("data-role", "").split()
        ]

    def group_text(group: ET.Element) -> str:
        chunks = [
            _renderable_text(element)
            for element in group.iter(_SVG_TEXT)
            if _has_visible_fill(element)
        ]
        return " ".join(" ".join(chunks).split())

    def direct_rects(group: ET.Element) -> list[ET.Element]:
        return [element for element in group if element.tag == f"{{{_SVG_NS}}}rect"]

    def first_rect(group: ET.Element) -> ET.Element:
        rectangles = direct_rects(group)
        if not rectangles:
            raise ValueError(
                "fig_construction_refresh: semantic groups need a direct rectangle"
            )
        return rectangles[0]

    title = one("Construction and refresh of a derived access structure")
    if font_size(title) < 28:
        raise ValueError("fig_construction_refresh: figure title must be >= 28")

    panel_titles = (
        "(a) full construction",
        "(b) differential refresh",
        "measured feasibility",
    )
    for label in panel_titles:
        if font_size(one(label)) < 23:
            raise ValueError(
                f"fig_construction_refresh: panel title {label!r} must be >= 23"
            )

    stage_contract = (
        ("1", "scan / count", "graph-counts"),
        ("2", "choose fN", "rank-cutoff"),
        ("3", "configure code", "code-config"),
        ("4", "encode / pack", "live-varblock"),
        ("5", "place records", "mn-stripes"),
        ("6", "publish epoch e", "epoch-metadata"),
    )
    stages = sorted(
        roles("full-build-stage"),
        key=lambda group: int(group.get("data-stage", "-1")),
    )
    if len(stages) != len(stage_contract):
        raise ValueError(
            "fig_construction_refresh: full build requires exactly six stages"
        )
    stage_x = []
    for stage, (number, label, object_kind) in zip(stages, stage_contract):
        if stage.get("data-stage") != number:
            raise ValueError(
                "fig_construction_refresh: full-build stages must be numbered 1-6"
            )
        operation = one(label)
        if operation not in set(stage.iter()) or font_size(operation) < 22:
            raise ValueError(
                f"fig_construction_refresh: stage {number} operation is not principal text"
            )
        objects = [
            element
            for element in stage.iter()
            if element.get("data-role") == "stage-object"
        ]
        if len(objects) != 1 or objects[0].get("data-kind") != object_kind:
            raise ValueError(
                f"fig_construction_refresh: stage {number} needs {object_kind!r}"
            )
        if number in {"3", "4", "6"}:
            header = first_rect(stage)
            header_bottom = coordinate(header, "y") + coordinate(header, "height")
            header_right = coordinate(header, "x") + coordinate(header, "width")
            lines = [
                "".join(span.itertext())
                for span in operation
                if span.tag == f"{{{_SVG_NS}}}tspan"
            ]
            final_baseline = coordinate(operation, "y") + sum(
                float(span.get("dy", "0"))
                for span in operation
                if span.tag == f"{{{_SVG_NS}}}tspan"
            )
            estimated_width = max((len(line) for line in lines), default=0) * 22 * 0.58
            if (
                coordinate(header, "height") < 54
                or len(lines) != 2
                or header_bottom - final_baseline < 8
                or header_right - coordinate(operation, "x") - estimated_width < 10
            ):
                raise ValueError(
                    f"fig_construction_refresh: stage {number} header needs multiline padding"
                )
        stage_x.append(coordinate(operation, "x"))
    if stage_x != sorted(stage_x) or len(set(stage_x)) != len(stage_x):
        raise ValueError(
            "fig_construction_refresh: six full-build stages must read left-to-right"
        )

    sources = roles("authoritative-source")
    if len(sources) != 1:
        raise ValueError(
            "fig_construction_refresh: expected one authoritative HNSW source"
        )
    source_content = group_text(sources[0])
    if "authoritative HNSW" not in source_content or "topology unchanged" not in source_content:
        raise ValueError(
            "fig_construction_refresh: authoritative HNSW must be explicitly unchanged"
        )
    if coordinate(first_rect(sources[0]), "x") >= stage_x[0]:
        raise ValueError(
            "fig_construction_refresh: authoritative HNSW must precede the six stages"
        )

    principal_labels = (
        "published epoch e",
        "touched Slab IDs",
        "scratch epoch e+1",
        "reuse",
        "rebuilt",
        "published epoch e+1",
        "readers open one published epoch",
    )
    for label in principal_labels:
        if font_size(one(label)) < 22:
            raise ValueError(
                f"fig_construction_refresh: principal label {label!r} must be >= 22"
            )

    published = roles("published-epoch")
    by_epoch = {group.get("data-epoch"): group for group in published}
    if set(by_epoch) != {"e", "e+1"} or len(published) != 2:
        raise ValueError(
            "fig_construction_refresh: refresh needs before-e and after-e+1 states"
        )
    if (
        by_epoch["e"].get("data-phase") != "before"
        or by_epoch["e+1"].get("data-phase") != "after"
    ):
        raise ValueError(
            "fig_construction_refresh: published epochs need explicit before/after phases"
        )

    scratch = roles("scratch-epoch")
    if len(scratch) != 1 or first_rect(scratch[0]).get("stroke-dasharray") is None:
        raise ValueError(
            "fig_construction_refresh: scratch epoch e+1 needs one dashed boundary"
        )
    before_x = coordinate(first_rect(by_epoch["e"]), "x")
    scratch_x = coordinate(first_rect(scratch[0]), "x")
    after_x = coordinate(first_rect(by_epoch["e+1"]), "x")
    if not before_x < scratch_x < after_x:
        raise ValueError(
            "fig_construction_refresh: scratch e+1 must precede published e+1"
        )

    changed_authoritative = roles("changed-authoritative-record")
    touch_mappings = roles("touch-mapping")
    touched_slab_ids = roles("touched-slab-id")
    mapping_edges = roles("touch-mapping-edge")
    epoch_slab_records = roles("epoch-slab-record")
    if (
        len(changed_authoritative) != 2
        or {item.get("data-id") for item in changed_authoritative} != {"1", "3"}
        or {group_text(item) for item in changed_authoritative} != {"L[1]*", "L[3]*"}
    ):
        raise ValueError(
            "fig_construction_refresh: authoritative changes must be L[1]* and L[3]*"
        )
    published_members = [set(group.iter()) for group in published]
    if any(
        item in members
        for item in changed_authoritative
        for members in published_members
    ):
        raise ValueError(
            "fig_construction_refresh: authoritative records cannot live in a published epoch"
        )
    if any(re.search(r"B\[[^]]+\]", group_text(item)) for item in changed_authoritative):
        raise ValueError(
            "fig_construction_refresh: authoritative records cannot use B[...] notation"
        )

    if len(touch_mappings) != 1:
        raise ValueError(
            "fig_construction_refresh: expected one explicit touched-Slab mapping step"
        )
    mapping_members = set(touch_mappings[0].iter())
    if (
        {item.get("data-id") for item in touched_slab_ids} != {"1", "3"}
        or {group_text(item) for item in touched_slab_ids} != {"B[1]", "B[3]"}
        or any(item not in mapping_members for item in touched_slab_ids)
    ):
        raise ValueError(
            "fig_construction_refresh: mapping must expose touched Slab IDs B[1] and B[3]"
        )
    expected_mappings = {("L[1]*", "B[1]"), ("L[3]*", "B[3]")}
    actual_mappings = {
        (edge.get("data-from"), edge.get("data-to")) for edge in mapping_edges
    }
    if actual_mappings != expected_mappings or any(
        edge.get("marker-end") != "url(#aRed)" for edge in mapping_edges
    ):
        raise ValueError(
            "fig_construction_refresh: require explicit L[1]/L[3] to B[1]/B[3] arrows"
        )

    epoch_e_members = set(by_epoch["e"].iter())
    scratch_members = set(scratch[0].iter())
    epoch_e_records = [item for item in epoch_slab_records if item in epoch_e_members]
    scratch_records = [item for item in epoch_slab_records if item in scratch_members]
    if (
        len(epoch_slab_records) != 8
        or {item.get("data-id") for item in epoch_e_records} != {"0", "1", "2", "3"}
        or {item.get("data-id") for item in scratch_records} != {"0", "1", "2", "3"}
        or any(item.get("data-epoch") != "e" for item in epoch_e_records)
        or any(item.get("data-epoch") != "e+1" for item in scratch_records)
    ):
        raise ValueError(
            "fig_construction_refresh: epoch e and scratch e+1 each need B[0..3]"
        )
    for item in epoch_slab_records:
        expected_label = f'B[{item.get("data-id")}]'
        record_content = group_text(item)
        if record_content != expected_label or re.search(r"L\[[^]]+\]", record_content):
            raise ValueError(
                "fig_construction_refresh: epoch Slab records require only B[...] notation"
            )

    pointer_switches = roles("epoch-pointer-switch")
    if (
        len(pointer_switches) != 1
        or pointer_switches[0].get("data-from") != "scratch epoch e+1"
        or pointer_switches[0].get("data-to") != "published epoch e+1"
        or pointer_switches[0].get("marker-end") != "url(#aPurple)"
    ):
        raise ValueError(
            "fig_construction_refresh: expected one purple e+1 pointer switch"
        )

    touched = roles("touched-record")
    reused = roles("reused-record")
    rebuilt = roles("rebuilt-record")
    if (
        {item.get("data-id") for item in touched} != {"1", "3"}
        or {item.get("data-id") for item in rebuilt} != {"1", "3"}
        or {item.get("data-id") for item in reused} != {"0", "2"}
    ):
        raise ValueError(
            "fig_construction_refresh: touched/rebuilt IDs and reused IDs are inconsistent"
        )
    if any(item not in scratch_members for item in (*reused, *rebuilt)):
        raise ValueError(
            "fig_construction_refresh: reused and rebuilt records must live in scratch e+1"
        )
    for item in touched:
        tile = first_rect(item)
        if (
            item.get("data-pattern") != "cross"
            or item.get("data-state") != "stale-selection"
            or tile.get("fill") != RED_L
            or tile.get("stroke") != RED
        ):
            raise ValueError(
                "fig_construction_refresh: touched epoch records need stale-selection marks"
            )
    marker_badges = roles("touched-marker-badge")
    if (
        len(marker_badges) != 2
        or {item.get("data-id") for item in marker_badges} != {"1", "3"}
    ):
        raise ValueError(
            "fig_construction_refresh: touched epoch records need two marker badges"
        )
    for item in touched:
        tile = first_rect(item)
        badges = [
            element
            for element in item.iter()
            if "touched-marker-badge" in element.get("data-role", "").split()
        ]
        if len(badges) != 1 or badges[0].get("data-id") != item.get("data-id"):
            raise ValueError(
                "fig_construction_refresh: each touched record needs its own marker badge"
            )
        badge = badges[0]
        circles = [
            element
            for element in badge
            if element.tag == f"{{{_SVG_NS}}}circle"
        ]
        badge_lines = [
            element for element in badge if element.tag == f"{{{_SVG_NS}}}line"
        ]
        if len(circles) != 1 or len(badge_lines) != 2:
            raise ValueError(
                "fig_construction_refresh: touched marker badge needs one circle and one X"
            )
        tile_x = coordinate(tile, "x")
        tile_y = coordinate(tile, "y")
        tile_w = coordinate(tile, "width")
        tile_h = coordinate(tile, "height")
        circle_element = circles[0]
        center_x = coordinate(circle_element, "cx")
        center_y = coordinate(circle_element, "cy")
        radius = coordinate(circle_element, "r")
        badge_bounds = (
            center_x - radius,
            center_y - radius,
            center_x + radius,
            center_y + radius,
        )
        label_bounds = (tile_x + 2, tile_y + 4, tile_x + tile_w - 2, tile_y + tile_h - 3)
        intersects_label = (
            badge_bounds[0] < label_bounds[2]
            and badge_bounds[2] > label_bounds[0]
            and badge_bounds[1] < label_bounds[3]
            and badge_bounds[3] > label_bounds[1]
        )
        if (
            intersects_label
            or badge_bounds[3] > tile_y
            or center_x < tile_x + tile_w * 0.65
        ):
            raise ValueError(
                "fig_construction_refresh: touched marker badge intersects record label space"
            )
        for badge_line in badge_lines:
            endpoints = (
                coordinate(badge_line, "x1"),
                coordinate(badge_line, "y1"),
                coordinate(badge_line, "x2"),
                coordinate(badge_line, "y2"),
            )
            if not (
                badge_bounds[0] <= endpoints[0] <= badge_bounds[2]
                and badge_bounds[1] <= endpoints[1] <= badge_bounds[3]
                and badge_bounds[0] <= endpoints[2] <= badge_bounds[2]
                and badge_bounds[1] <= endpoints[3] <= badge_bounds[3]
            ):
                raise ValueError(
                    "fig_construction_refresh: marker X must remain inside its badge"
                )
    for item in reused:
        tile = first_rect(item)
        if (
            item.get("data-pattern") != "diagonal"
            or tile.get("fill") != GREY
            or tile.get("stroke") != MUTED
            or tile.get("stroke-dasharray") is None
        ):
            raise ValueError(
                "fig_construction_refresh: reused records need gray dashed patterns"
            )
    for item in rebuilt:
        rectangles = direct_rects(item)
        if (
            item.get("data-outline") != "double"
            or len(rectangles) < 2
            or rectangles[0].get("fill") != GREEN_L
            or rectangles[0].get("stroke") != GREEN
        ):
            raise ValueError(
                "fig_construction_refresh: rebuilt records need green double outlines"
            )

    epoch_reuse_inputs = roles("epoch-reuse-input")
    touch_rebuild_inputs = roles("touch-rebuild-input")
    if len(epoch_reuse_inputs) != 1 or len(touch_rebuild_inputs) != 1:
        raise ValueError(
            "fig_construction_refresh: scratch needs explicit reuse and rebuild inputs"
        )
    epoch_rect = first_rect(by_epoch["e"])
    scratch_rect = first_rect(scratch[0])
    mapping_rect = first_rect(touch_mappings[0])
    epoch_right = coordinate(epoch_rect, "x") + coordinate(epoch_rect, "width")
    epoch_bottom = coordinate(epoch_rect, "y") + coordinate(epoch_rect, "height")
    scratch_left = coordinate(scratch_rect, "x")
    mapping_right = coordinate(mapping_rect, "x") + coordinate(mapping_rect, "width")
    reuse_centers = {
        coordinate(first_rect(item), "y") + coordinate(first_rect(item), "height") / 2
        for item in reused
    }
    rebuild_centers = {
        coordinate(first_rect(item), "y") + coordinate(first_rect(item), "height") / 2
        for item in rebuilt
    }
    if len(reuse_centers) != 1 or len(rebuild_centers) != 1:
        raise ValueError(
            "fig_construction_refresh: reuse and rebuild records need aligned rows"
        )
    reuse_y = reuse_centers.pop()
    rebuild_y = rebuild_centers.pop()
    reuse_edge = epoch_reuse_inputs[0]
    if (
        reuse_edge.tag != f"{{{_SVG_NS}}}line"
        or reuse_edge.get("data-from") != "published epoch e"
        or reuse_edge.get("data-to") != "reuse B[0],B[2]"
        or reuse_edge.get("stroke") != MUTED
        or reuse_edge.get("marker-end") != "url(#aMuted)"
        or abs(coordinate(reuse_edge, "x1") - epoch_right) > 0.1
        or not scratch_left - 8 <= coordinate(reuse_edge, "x2") < scratch_left
        or abs(coordinate(reuse_edge, "y1") - reuse_y) > 0.1
        or abs(coordinate(reuse_edge, "y2") - reuse_y) > 0.1
    ):
        raise ValueError(
            "fig_construction_refresh: epoch-e reuse edge must terminate at the gray row"
        )

    rebuild_edge = touch_rebuild_inputs[0]
    rebuild_points = [
        tuple(float(value) for value in point.split(","))
        for point in rebuild_edge.get("points", "").split()
    ]
    mapping_centers = {
        coordinate(first_rect(item), "y") + coordinate(first_rect(item), "height") / 2
        for item in touched_slab_ids
    }
    mapping_y = sum(mapping_centers) / len(mapping_centers)
    if (
        rebuild_edge.tag != f"{{{_SVG_NS}}}polyline"
        or rebuild_edge.get("data-from") != "touched Slab IDs"
        or rebuild_edge.get("data-to") != "rebuilt B[1],B[3]"
        or rebuild_edge.get("stroke") != GREEN
        or rebuild_edge.get("marker-end") != "url(#aGreen)"
        or len(rebuild_points) < 6
        or rebuild_points[0] != (mapping_right, mapping_y)
        or rebuild_points[-1] != (scratch_left - 4, rebuild_y)
        or max(point[1] for point in rebuild_points) < epoch_bottom + 4
    ):
        raise ValueError(
            "fig_construction_refresh: touched-ID rebuild edge must bypass epoch e and hit green row"
        )

    messages = roles("reader-view-message")
    reader_views = roles("reader-view")
    if (
        len(messages) != 1
        or group_text(messages[0]) != "readers open one published epoch"
        or len(labeled.get("readers open one published epoch", [])) != 1
        or len(reader_views) != 1
        or reader_views[0].get("data-to") != "published epoch e+1"
    ):
        raise ValueError(
            "fig_construction_refresh: readers must open exactly one published epoch"
        )

    constraint_labels = {
        "MN bytes",
        "CN state",
        "bytes/q",
        "recall target",
        "build/refresh cost",
    }
    constraint_checks = roles("constraint-check")
    if (
        len(constraint_checks) != 5
        or {item.get("data-name") for item in constraint_checks} != constraint_labels
        or {group_text(item) for item in constraint_checks} != constraint_labels
    ):
        raise ValueError(
            "fig_construction_refresh: expected exactly five measured feasibility checks"
        )

    one("offline / read-mostly refresh")
    one("derived structure: discard / rebuild")
    content = visible_text(root)
    forbidden_patterns = {
        "optimizer": r"\boptimi[sz]er\b",
        "online tuning": r"\bonline tuning\b",
        "automatic": r"\bautomatic(?:ally)?\b",
        "universal": r"\buniversal\b",
        "transactional": r"\btransactions?\b|\btransactional\b",
        "atomic": r"\batomic(?:ally)?\b",
        "crash recovery": r"\bcrash\b|\brecovery\b",
        "lock-free": r"\block[- ]free\b",
        "concurrent": r"\bconcurrent(?:ly)?\b",
        "dual epoch": r"\bboth epochs\b|\bdual[- ]epoch\b",
    }
    for claim, pattern in forbidden_patterns.items():
        if re.search(pattern, content, re.IGNORECASE):
            raise ValueError(
                f"fig_construction_refresh: forbidden {claim!r} claim token"
            )


def validate_svg(name: str, svg: str) -> None:
    dimensions, required_tokens, minimum_font_size = FIGURE_CONTRACTS[name]
    root = ET.fromstring(svg)
    if root.tag != _SVG_ROOT:
        raise ValueError(f"{name}: root must be an SVG element in the SVG namespace")

    expected_view_box = f"0 0 {dimensions[0]} {dimensions[1]}"
    actual_view_box = root.get("viewBox")
    if actual_view_box != expected_view_box:
        raise ValueError(
            f"{name}: expected viewBox {expected_view_box!r}, got {actual_view_box!r}"
        )

    for element in root.iter():
        if element.tag == _SVG_STYLE:
            raise ValueError(f"{name}: CSS style elements are not supported")
        if element.get("class") is not None:
            raise ValueError(f"{name}: class-based styling is not supported")
        _is_nonpositive_opacity(element.get("opacity"), "opacity")
        _is_nonpositive_opacity(element.get("fill-opacity"), "fill-opacity")
        style = element.get("style")
        if style and _RESTRICTED_STYLE.search(style):
            raise ValueError(f"{name}: restricted inline style {style!r}")

    text_elements = _renderable_text_elements(root)
    content = visible_text(root)
    missing_tokens = [token for token in required_tokens if token not in content]
    if missing_tokens:
        raise ValueError(f"{name}: missing visible text tokens {missing_tokens}")
    if name == "fig_slab_layout":
        _validate_slab_layout_semantics(root)
    if name == "fig_search_placement":
        _validate_search_placement_semantics(root)
    if name == "fig_construction_refresh":
        _validate_construction_refresh_semantics(root)

    for element in text_elements:
        fill = element.get("fill")
        if not _has_visible_fill(element):
            raise ValueError(f"{name}: renderable text has non-visible fill {fill!r}")
        value = element.get("font-size")
        if value is None:
            raise ValueError(f"{name}: renderable text is missing a font-size")
        match = re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", value)
        if match is None:
            raise ValueError(f"{name}: renderable text has non-numeric font-size {value!r}")
        if float(value) < minimum_font_size:
            raise ValueError(
                f"{name}: font-size {value} is below minimum {minimum_font_size}"
            )
        for descendant in element.iter():
            if descendant is not element and descendant.get("font-size") is not None:
                raise ValueError(f"{name}: descendant font-size overrides are not supported")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", choices=sorted(GENERATORS), help="generate one figure")
    parser.add_argument(
        "--cache-summary",
        type=Path,
        default=CACHE_CSV,
        help="validated n=5 cache-control summary used by Figure 1",
    )
    parser.add_argument(
        "--profile-summary",
        type=Path,
        default=PROFILE_CSV,
        help="validated frozen-binary SIFT1M CPU profile used by Figure 1",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=FIG_DIR,
        help="directory for generated SVG and PDF files",
    )
    parser.add_argument(
        "--svg-only",
        action="store_true",
        help="validate and write SVG source without invoking a PDF renderer",
    )
    return parser.parse_args()


def marker(name: str, color: str) -> str:
    return (
        f'<marker id="{name}" markerWidth="18" markerHeight="14" refX="16" '
        f'refY="7" orient="auto" markerUnits="userSpaceOnUse">'
        f'<path d="M0,0 L18,7 L0,14 z" fill="{color}"/></marker>'
    )


def canvas(width: int, height: int, include_navy: bool = True) -> list[str]:
    markers = [marker("aInk", INK)]
    if include_navy:
        markers.append(marker("aNavy", NAVY))
    markers.extend(
        (
            marker("aBlue", BLUE),
            marker("aTeal", TEAL),
            marker("aGreen", GREEN),
            marker("aOrange", ORANGE),
            marker("aPurple", PURPLE),
            marker("aRed", RED),
            marker("aMuted", MUTED),
        )
    )
    return [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
        f'width="{width}" height="{height}" font-family="Helvetica,Arial,sans-serif">',
        "<defs>",
        *markers,
        "</defs>",
        f'<rect width="{width}" height="{height}" fill="{WHITE}"/>',
    ]


def rect(x, y, w, h, fill=WHITE, stroke=INK, sw=2.4, rx=8, dash=None) -> str:
    dashed = f' stroke-dasharray="{dash}"' if dash else ""
    return (
        f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" '
        f'fill="{fill}" stroke="{stroke}" stroke-width="{sw}"{dashed}/>'
    )


def line(x1, y1, x2, y2, color=INK, sw=3.0, arrow=None, dash=None) -> str:
    attrs = f' marker-end="url(#{arrow})"' if arrow else ""
    if dash:
        attrs += f' stroke-dasharray="{dash}"'
    return (
        f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" '
        f'stroke="{color}" stroke-width="{sw}"{attrs}/>'
    )


def path(d, color=INK, sw=3.0, fill="none", arrow=None, dash=None) -> str:
    attrs = f' marker-end="url(#{arrow})"' if arrow else ""
    if dash:
        attrs += f' stroke-dasharray="{dash}"'
    return f'<path d="{d}" fill="{fill}" stroke="{color}" stroke-width="{sw}"{attrs}/>'


def circle(cx, cy, r, fill=WHITE, stroke=INK, sw=2.4) -> str:
    return (
        f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="{fill}" '
        f'stroke="{stroke}" stroke-width="{sw}"/>'
    )


def text(x, y, value, size=22, color=INK, weight=None, anchor="start", style=None) -> str:
    attrs = (
        f'x="{x}" y="{y}" font-size="{size}" fill="{color}" '
        f'text-anchor="{anchor}"'
    )
    if weight:
        attrs += f' font-weight="{weight}"'
    if style:
        attrs += f' font-style="{style}"'
    lines = str(value).split("\n")
    if len(lines) == 1:
        return f'<text {attrs}>{escape(lines[0])}</text>'
    spans = []
    for index, item in enumerate(lines):
        dy = "0" if index == 0 else f"{size * 1.17:.1f}"
        spans.append(f'<tspan x="{x}" dy="{dy}">{escape(item)}</tspan>')
    return f'<text {attrs}>{"".join(spans)}</text>'


def centered(x, y, w, h, value, size=22, color=INK, weight=None) -> str:
    lines = str(value).split("\n")
    total = size + (len(lines) - 1) * size * 1.17
    first_y = y + (h - total) / 2 + size * 0.82
    attrs = (
        f'x="{x + w / 2}" y="{first_y:.1f}" font-size="{size}" '
        f'fill="{color}" text-anchor="middle"'
    )
    if weight:
        attrs += f' font-weight="{weight}"'
    spans = []
    for index, item in enumerate(lines):
        dy = "0" if index == 0 else f"{size * 1.17:.1f}"
        spans.append(f'<tspan x="{x + w / 2}" dy="{dy}">{escape(item)}</tspan>')
    return f'<text {attrs}>{"".join(spans)}</text>'


def box(out, x, y, w, h, label, fill=WHITE, stroke=INK, size=21, weight="700", rx=8, sw=2.4):
    out.append(rect(x, y, w, h, fill, stroke, sw, rx))
    if label:
        out.append(centered(x, y, w, h, label, size, INK, weight))


def tab(out, x, y, w, label, fill, stroke, size=22):
    out.append(rect(x, y, w, 42, fill, stroke, 2.2, 8))
    out.append(centered(x, y, w, 42, label, size, stroke, "700"))


def chip(out, x, y, w, h, label, fill, size=20):
    out.append(rect(x, y, w, h, fill, fill, 0, 7))
    out.append(centered(x, y, w, h, label, size, WHITE, "700"))


def panel(out, x, y, w, h, label, stroke=INK, fill=WHITE, dash=None, title_size=20):
    # Keep conceptual regions open; reserve closed boxes for physical records.
    if fill != WHITE:
        out.append(
            f'<path d="M{x},{y + 12} Q{x},{y} {x + 12},{y} '
            f'H{x + w - 26} L{x + w},{y + 26} V{y + h - 12} '
            f'Q{x + w},{y + h} {x + w - 12},{y + h} H{x + 22} '
            f'L{x},{y + h - 22} Z" fill="{fill}" stroke="none"/>'
        )
    out.append(line(x + 8, y + 43, x + w - 10, y + 43, GRID, 1.2, dash=dash))
    out.append(line(x, y + 8, x, y + 38, stroke, 4.0, dash=dash))
    if label:
        out.append(text(x + 16, y + 30, label, title_size, stroke, "700"))


def node(out, x, y, label, fill=WHITE, stroke=NAVY, r=15, size=20):
    out.append(circle(x, y, r, fill, stroke, 2.4))
    if label is not None:
        out.append(text(x, y + 7, label, size, INK, "700", "middle"))


def elbow(out, points, color=INK, sw=2.8, arrow=_AUTO_ARROW, dash=None):
    if arrow is _AUTO_ARROW:
        try:
            arrow = _MARKER_BY_COLOR[color]
        except KeyError:
            raise ValueError(f"no automatic arrow marker for color {color!r}") from None
    attrs = f' marker-end="url(#{arrow})"' if arrow else ""
    if dash:
        attrs += f' stroke-dasharray="{dash}"'
    coordinates = " ".join(f"{x},{y}" for x, y in points)
    out.append(
        f'<polyline points="{coordinates}" fill="none" stroke="{color}" '
        f'stroke-width="{sw}" stroke-linejoin="round"{attrs}/>'
    )


def record(out, x, y, widths, labels, fills, h=62, size=17, strokes=None):
    strokes = strokes or [INK] * len(widths)
    cursor = x
    for width, label, fill, stroke in zip(widths, labels, fills, strokes):
        out.append(rect(cursor, y, width, h, fill, stroke, 2.0, 0))
        if label:
            out.append(centered(cursor, y, width, h, label, size, INK, "700"))
        cursor += width


def field_row(out, x, y, fields, h=42, size=20):
    widths = [field[0] for field in fields]
    labels = [field[1] for field in fields]
    fills = [field[2] for field in fields]
    strokes = [field[3] if len(field) > 3 else INK for field in fields]
    record(out, x, y, widths, labels, fills, h, size, strokes)


def graph(out, points, edges, fill=TEAL_L, stroke=TEAL, radius=10):
    for left, right in edges:
        out.append(line(points[left][0], points[left][1], points[right][0], points[right][1], GRID, 2.0))
    for x, y in points:
        out.append(circle(x, y, radius, fill, stroke, 2.6))


def measured_cache_control() -> tuple[
    tuple[int, ...],
    tuple[float, ...],
    tuple[float, ...],
    tuple[float, ...],
    tuple[float, ...],
    float,
]:
    with CACHE_CSV.open(newline="") as handle:
        rows = {row["condition"]: row for row in csv.DictReader(handle)}
    if set(rows) != {"off", "c5", "c20", "c50"}:
        raise ValueError("Figure 1 requires the complete formal cache-control matrix")
    if any(int(row["n"]) != 5 for row in rows.values()):
        raise ValueError("Figure 1 requires five measured repeats per cache ratio")
    conditions = ("off", "c5", "c20", "c50")
    ratios = (0, 5, 20, 50)
    for condition, ratio in zip(conditions, ratios):
        if int(rows[condition]["cache_ratio_pct"]) != ratio:
            raise ValueError(f"Figure 1 cache-ratio mismatch for {condition}")
    posts = tuple(float(rows[condition]["posts_per_query_mean"]) for condition in conditions)
    posts_ci = tuple(
        float(rows[condition]["posts_per_query_ci95"]) for condition in conditions
    )
    qps = tuple(float(rows[condition]["qps_mean"]) for condition in conditions)
    qps_ci = tuple(float(rows[condition]["qps_ci95"]) for condition in conditions)
    if any(value <= 0 for value in (*posts, *qps)) or any(
        value < 0 for value in (*posts_ci, *qps_ci)
    ):
        raise ValueError("Figure 1 cache-control values must be positive")
    with PROFILE_CSV.open(newline="") as handle:
        profile_rows = list(csv.DictReader(handle))
    if len(profile_rows) != 1:
        raise ValueError("Figure 1 requires one validated SIFT1M CPU profile")
    profile = profile_rows[0]
    if (
        profile.get("dataset") != "SIFT1M"
        or profile.get("method") != "SHINE-derived"
        or int(profile.get("query_rows", 0)) != 200000
        or int(profile.get("lost_samples", -1)) != 0
        or profile.get("distance_symbol") != "l2"
    ):
        raise ValueError("Figure 1 CPU-profile protocol mismatch")
    useful = float(profile["distance_self_percent"])
    if not 0.0 < useful < 100.0:
        raise ValueError("Figure 1 CPU-profile distance share is invalid")
    return ratios, posts, posts_ci, qps, qps_ci, useful


def physical_units_panel_layout() -> str:
    (
        cache_ratios,
        cache_posts,
        cache_posts_ci,
        cache_qps,
        cache_qps_ci,
        useful,
    ) = measured_cache_control()
    candidate_id = 42
    neighbor_ids = (23, 57, 81)
    partition_extra_ids = (36, 68, 94)
    neighbor_csv = ",".join(str(identifier) for identifier in neighbor_ids)
    logical_label = f"u={candidate_id}  N={{{neighbor_csv}}}"
    list_label = f"L[{candidate_id}]"
    vector_labels = tuple(f"V[{identifier}]" for identifier in neighbor_ids)
    expansion_label = f"B[{candidate_id}]"
    logical_ids = (candidate_id, *neighbor_ids)

    out = canvas(1440, 540)
    out.append(text(720, 36, "One expansion, three physical units", 28, INK, "700", "middle"))

    columns = [
        (28, 250, "Node / vector", RED, RED_L),
        (304, 250, "Expansion", GREEN, GREEN_L),
        (580, 250, "Routed partition", ORANGE, ORANGE_L),
    ]
    cost_cells = [
        ("ops 4", "payload L+3V", "state beam/cache", "step global"),
        ("ops 1", f"payload B{candidate_id}", "state beam", "step global"),
        ("route+fetch", "payload p3", "state route+p3", "step local"),
    ]
    for column, costs in zip(columns, cost_cells):
        x, width, label, stroke, tint = column
        operation, payload, state, graph_step = costs
        panel(out, x, 50, width, 480, label, stroke, PALE, title_size=21)
        out.append(text(x + 12, 119, "SEARCH", 20, stroke, "700"))
        out.append(line(x + 10, 286, x + width - 10, 286, GRID, 1.4))
        out.append(text(x + 12, 319, "PHYSICAL OBJECT", 20, stroke, "700"))
        out.append(line(x + 10, 450, x + width - 10, 450, GRID, 1.4))
        for row_y, metric in zip(
            (469, 488, 507, 526),
            (operation, state, payload, graph_step),
        ):
            out.append(circle(x + 12, row_y - 6, 4, tint, stroke, 1.8))
            out.append(text(x + 24, row_y, metric, 18, stroke, "700"))

    # The same candidate and neighbor IDs are repeated in all three search rows.
    for x, _, _, stroke, _ in columns:
        out.append(text(x + 125, 149, logical_label, 20, stroke, "700", "middle"))

    # Node/vector: retrieve one list, then three independent vectors.
    box(out, 40, 158, 130, 32, f"READ {list_label}", WHITE, RED, 20, rx=5, sw=2.2)
    out.append(line(172, 174, 196, 174, RED, 2.8, "aRed"))
    box(out, 200, 158, 66, 32, "IDs", RED_L, RED, 20, rx=5, sw=2.2)
    out.append(text(153, 211, "3 vector READs", 20, RED, "700", "middle"))
    for x, label in zip((38, 120, 202), vector_labels):
        box(out, x, 218, 66, 32, label, RED_L, RED, 20, rx=5, sw=2.2)
    out.append(line(153, 250, 153, 255, PURPLE, 2.4))
    box(out, 42, 256, 222, 24, "authoritative topology", PURPLE_L, PURPLE, 20, rx=4, sw=2.0)

    # Expansion: one contiguous record derived from the authoritative topology.
    box(out, 316, 158, 226, 54, f"{expansion_label}=<hdr,IDs,\ncodes,rptr>", GREEN_L, GREEN, 20, rx=5, sw=2.4)
    out.append(line(429, 212, 429, 219, GREEN, 2.6))
    box(out, 334, 220, 190, 28, "in-place score", WHITE, GREEN, 20, rx=4, sw=2.0)
    out.append(line(429, 248, 429, 255, PURPLE, 2.4))
    box(out, 318, 256, 222, 24, "authoritative topology", PURPLE_L, PURPLE, 20, rx=4, sw=2.0)

    # Routed partition: route, fetch p3, and search its deserialized local graph.
    box(out, 592, 158, 112, 32, "route/meta", WHITE, ORANGE, 20, rx=5, sw=2.2)
    out.append(line(706, 174, 750, 174, ORANGE, 2.8, "aOrange"))
    box(out, 754, 158, 64, 32, "p3", ORANGE_L, ORANGE, 20, rx=5, sw=2.2)
    out.append(line(786, 190, 786, 198, ORANGE, 2.5))
    box(out, 615, 199, 180, 30, "serialized p3", ORANGE_L, ORANGE, 20, rx=4, sw=2.0)
    out.append(line(705, 229, 705, 239, ORANGE, 2.5))
    box(out, 586, 240, 116, 32, "deserialize", WHITE, ORANGE, 20, rx=4, sw=2.0)
    out.append(line(704, 256, 714, 256, ORANGE, 2.8, "aOrange"))
    box(out, 718, 231, 106, 49, "local\nsub-HNSW", ORANGE_L, ORANGE, 20, rx=4, sw=2.0)

    # Four separately addressed records versus one contiguous expansion record.
    node_records = tuple(
        (x, 55, label)
        for x, label in zip((36, 96, 156, 216), (list_label, *vector_labels))
    )
    for x, width, label in node_records:
        box(out, x, 350, width, 42, label, RED_L, RED, 20, rx=3, sw=2.0)
        out.append(line(x + width / 2, 392, x + width / 2, 406, RED, 2.0))
    box(out, 97, 408, 112, 30, "4 posts", RED_L, RED, 20, rx=4, sw=2.0)

    out.append(text(318, 350, expansion_label, 20, GREEN, "700"))
    record(
        out,
        314,
        358,
        [40, 96, 56, 42],
        ["hdr", f"IDs\n{neighbor_csv}", "codes", "rptr"],
        [GREY, BLUE_L, ORANGE_L, PURPLE_L],
        58,
        20,
        [INK, BLUE, ORANGE, PURPLE],
    )
    box(out, 373, 416, 112, 28, "1 post", GREEN_L, GREEN, 20, rx=4, sw=2.0)

    # SlabWalk-original p3 topology with an independent two-digit ID set.
    out.append(rect(588, 337, 234, 104, WHITE, ORANGE, 2.0, 6, "6 5"))
    out.append(text(598, 360, "p3", 20, ORANGE, "700"))
    partition_nodes = {
        candidate_id: (706, 385),
        neighbor_ids[0]: (660, 355),
        neighbor_ids[1]: (762, 357),
        neighbor_ids[2]: (760, 414),
        partition_extra_ids[0]: (624, 400),
        partition_extra_ids[1]: (674, 421),
        partition_extra_ids[2]: (802, 392),
    }
    partition_edges = (
        (candidate_id, neighbor_ids[0]),
        (candidate_id, neighbor_ids[1]),
        (candidate_id, neighbor_ids[2]),
        (candidate_id, partition_extra_ids[1]),
        (neighbor_ids[0], partition_extra_ids[0]),
        (neighbor_ids[0], partition_extra_ids[1]),
        (neighbor_ids[1], neighbor_ids[2]),
        (neighbor_ids[1], partition_extra_ids[2]),
        (neighbor_ids[2], partition_extra_ids[1]),
        (neighbor_ids[2], partition_extra_ids[2]),
        (partition_extra_ids[0], partition_extra_ids[1]),
    )
    for left, right in partition_edges:
        x1, y1 = partition_nodes[left]
        x2, y2 = partition_nodes[right]
        out.append(line(x1, y1, x2, y2, ORANGE, 1.8))
    for identifier, (x, y) in partition_nodes.items():
        fill = ORANGE_L if identifier in logical_ids else WHITE
        node(out, x, y, str(identifier), fill, ORANGE, 14, 20)

    # The same-path controls make the motivation visible without changing the unit.
    post_reduction = 100.0 * (1.0 - cache_posts[-1] / cache_posts[0])
    qps_loss = 100.0 * (1.0 - cache_qps[-1] / cache_qps[0])
    panel(
        out,
        866,
        70,
        546,
        378,
        "Measured motivation: same path, more cache",
        NAVY,
        PALE,
    )
    box(
        out,
        888,
        116,
        240,
        50,
        f"{post_reduction:.1f}% fewer posts/query",
        BLUE_L,
        BLUE,
        18,
        rx=6,
        sw=2.2,
    )
    box(
        out,
        1150,
        116,
        240,
        50,
        f"{qps_loss:.1f}% lower QPS",
        RED_L,
        RED,
        18,
        rx=6,
        sw=2.2,
    )

    out.append(text(1139, 194, "cache budget (%)", 18, NAVY, "700", "middle"))
    out.append(text(1390, 194, "n=5; 95% CI", 18, MUTED, "600", "end"))
    plot_x = (940, 1080, 1220, 1360)
    normalized_posts = tuple(100.0 * value / cache_posts[0] for value in cache_posts)
    normalized_posts_ci = tuple(
        100.0 * value / cache_posts[0] for value in cache_posts_ci
    )
    normalized_qps = tuple(100.0 * value / cache_qps[0] for value in cache_qps)
    normalized_qps_ci = tuple(100.0 * value / cache_qps[0] for value in cache_qps_ci)
    plot_y = lambda value: 220.0 + (100.0 - value) * 0.88
    out.append(line(920, 220, 1380, 220, GRID, 1.4, dash="5 4"))
    out.append(line(920, 308, 1380, 308, GRID, 1.0))
    out.append(text(912, 226, "100%", 18, MUTED, "600", "end"))
    out.append(text(912, 314, "0", 18, MUTED, "600", "end"))
    for values, ci_values, color in (
        (normalized_posts, normalized_posts_ci, BLUE),
        (normalized_qps, normalized_qps_ci, RED),
    ):
        points_y = tuple(plot_y(value) for value in values)
        for index in range(len(values) - 1):
            out.append(
                line(
                    plot_x[index],
                    points_y[index],
                    plot_x[index + 1],
                    points_y[index + 1],
                    color,
                    3.0,
                )
            )
        for ratio, px, py, value, ci in zip(
            cache_ratios, plot_x, points_y, values, ci_values
        ):
            ci_top = plot_y(value + ci)
            ci_bottom = plot_y(value - ci)
            out.append(line(px, ci_top, px, ci_bottom, color, 1.4))
            out.append(line(px - 4, ci_top, px + 4, ci_top, color, 1.4))
            out.append(line(px - 4, ci_bottom, px + 4, ci_bottom, color, 1.4))
            out.append(circle(px, py, 5.3, WHITE, color, 2.4))
            out.append(text(px, 328, str(ratio), 18, INK, "600", "middle"))
    out.append(line(954, 348, 984, 348, BLUE, 3.0))
    out.append(circle(969, 348, 4.5, WHITE, BLUE, 2.0))
    out.append(text(994, 354, "posts remaining", 18, BLUE, "700"))
    out.append(line(1170, 348, 1200, 348, RED, 3.0))
    out.append(circle(1185, 348, 4.5, WHITE, RED, 2.0))
    out.append(text(1210, 354, "QPS retained", 18, RED, "700"))

    out.append(text(888, 382, "CN self-time (200K queries, 3,000 samples)", 18, NAVY, "700"))
    out.append(rect(888, 394, 502, 20, GREY, GRID, 1.0, 4))
    out.append(rect(888, 394, 502 * useful / 100.0, 20, BLUE, BLUE, 0, 4))
    out.append(text(888, 438, f"useful distance: {useful:.1f}%", 18, BLUE, "700"))
    out.append(text(1390, 438, f"other CN work: {100.0 - useful:.1f}%", 18, MUTED, "700", "end"))
    out.append("</svg>")
    return "\n".join(out)


def overview_panel_layout() -> str:
    candidate_id = 42
    neighbor_ids = (23, 57, 81)
    upper_extra_ids = (34, 66, 92)
    cold_candidate_id = 96
    cold_neighbor_ids = upper_extra_ids[:2]
    score_values = (".18", ".24", ".31")
    neighbor_csv = ",".join(str(identifier) for identifier in neighbor_ids)
    candidate_label = f"u={candidate_id}"
    slab_label = f"B[{candidate_id}]"
    prefix_label = f"prefix[{candidate_id}]"

    client_zone = (24, 62, 170, 104)
    cn_zone = (24, 178, 170, 154)
    mn_zone = (24, 344, 170, 230)
    online = (210, 62, 1204, 244)
    layout = (210, 320, 1204, 184)
    offline = (210, 518, 1204, 56)

    out = canvas(1440, 610)
    out.append(text(720, 38, "SlabWalk end-to-end", 28, INK, "700", "middle"))
    for owner, bounds, fill, stroke in (
        ("clients", client_zone, PALE, NAVY),
        ("cn-local", cn_zone, GREEN_L, GREEN),
        ("mn-resident", mn_zone, WHITE, GREEN),
    ):
        zone = rect(*bounds, fill, stroke, 0, 0)
        out.append(
            zone.replace(
                "<rect ",
                f'<rect data-role="ownership-zone" data-owner="{owner}" ',
                1,
            )
        )
        out.append(
            line(
                bounds[0],
                bounds[1] + 8,
                bounds[0],
                bounds[1] + bounds[3] - 8,
                stroke,
                4.0,
            )
        )
    panel(out, *online, "online query", NAVY, PALE, title_size=20)
    panel(out, *layout, "PHYSICAL ORGANIZATION", NAVY, PALE, title_size=20)
    panel(out, *offline, "", ORANGE, WHITE, title_size=20)

    # Separate ownership zones prevent client, CN-local, and MN-resident state from nesting visually.
    out.append(text(109, 91, "Clients", 20, NAVY, "700", "middle"))
    for center_x in (72, 109, 146):
        out.append(circle(center_x, 119, 9, BLUE_L, NAVY, 2.0))
        out.append(path(f"M{center_x - 13},146 Q{center_x},126 {center_x + 13},146", NAVY, 2.4))
    out.append(line(109, 151, 109, 174, GREEN, 2.6, "aGreen"))
    out.append(text(109, 201, "CN-local state", 20, GREEN, "700", "middle"))
    for offset in (12, 6, 0):
        out.append(rect(46 + offset, 221 - offset, 108, 82, WHITE, GREEN, 2.2, 6))
    out.append(rect(46, 221, 108, 82, GREEN_L, GREEN, 2.4, 6))
    cn_points = ((69, 250), (99, 236), (130, 249), (84, 272), (118, 272))
    for left, right in ((0, 1), (1, 2), (0, 3), (1, 3), (1, 4), (2, 4), (3, 4)):
        out.append(line(*cn_points[left], *cn_points[right], GREEN, 1.8))
    for x, y in cn_points:
        out.append(circle(x, y, 5, WHITE, GREEN, 1.8))
    for x in (58, 86, 114):
        out.append(rect(x, 284, 24, 11, WHITE, GREEN, 1.6, 1))
    out.append(text(109, 367, "MN-resident", 20, GREEN, "700", "middle"))
    for offset in (12, 6, 0):
        out.append(rect(46 + offset, 389 - offset, 108, 98, WHITE, GREEN, 2.2, 6))
    out.append(rect(46, 389, 108, 98, WHITE, GREEN, 2.4, 6))
    for y, fill, stroke in (
        (407, GREEN_L, GREEN),
        (431, ORANGE_L, ORANGE),
        (455, PURPLE_L, PURPLE),
    ):
        out.append(rect(58, y, 84, 16, fill, stroke, 1.8, 2))
    out.append(text(109, 521, "registered bytes\nonly", 20, GREEN, "700", "middle"))

    # The request descends resident upper layers, then hands off a level-0 seed.
    elbow(out, [(194, 130), (204, 130), (204, 170), (214, 170)], NAVY, 2.6)
    out.append(circle(238, 170, 21, BLUE_L, NAVY, 2.6))
    out.append(text(238, 177, "q", 22, NAVY, "700", "middle"))
    out.append(rect(264, 94, 208, 110, GREEN_L, GREEN, 2.2, 7))
    out.append(text(368, 116, "resident upper graph", 20, GREEN, "700", "middle"))
    out.append(text(282, 148, "L2", 20, GREEN, "700"))
    out.append(text(282, 187, "L1", 20, GREEN, "700"))
    upper_nodes = {
        upper_extra_ids[0]: (350, 142),
        upper_extra_ids[1]: (336, 180),
        upper_extra_ids[2]: (408, 180),
    }
    upper_edges = (
        (upper_extra_ids[0], upper_extra_ids[1]),
        (upper_extra_ids[0], upper_extra_ids[2]),
        (upper_extra_ids[1], upper_extra_ids[2]),
    )
    for left, right in upper_edges:
        x1, y1 = upper_nodes[left]
        x2, y2 = upper_nodes[right]
        out.append(line(x1, y1, x2, y2, GREEN, 2.0))
    out.append(line(260, 166, 335, 143, NAVY, 2.6, "aNavy"))
    for identifier, (x, y) in upper_nodes.items():
        node(out, x, y, str(identifier), WHITE, GREEN, 14, 20)

    # Concrete lookup, one-read expansion, score rows, beam state, and rerank.
    out.append(text(519, 275, "level-0 seed", 20, GREEN, "700", "middle"))
    box(out, 478, 201, 82, 42, candidate_label, GREEN_L, GREEN, 20, rx=5, sw=2.2)
    elbow(out, [(422, 180), (454, 180), (454, 222), (474, 222)], GREEN, 2.8)
    out.append(rect(578, 166, 126, 92, WHITE, PURPLE, 2.4, 6))
    out.append(text(641, 192, prefix_label, 20, PURPLE, "700", "middle"))
    field_row(
        out,
        579,
        207,
        [(48, "MN0", PURPLE_L, PURPLE), (36, "off", WHITE, PURPLE), (40, "len", WHITE, PURPLE)],
        36,
        20,
    )
    out.append(line(560, 222, 574, 222, PURPLE, 2.8, "aPurple"))
    box(out, 718, 192, 106, 58, "MN0:\noff,len", PURPLE_L, PURPLE, 20, rx=5, sw=2.2)
    out.append(line(704, 222, 714, 222, PURPLE, 2.8, "aPurple"))
    box(out, 840, 197, 126, 50, f"READ {slab_label}", GREEN_L, GREEN, 20, rx=5, sw=2.4)
    out.append(line(824, 222, 836, 222, GREEN, 2.8, "aGreen"))
    out.append(text(1030, 154, "SIMD scores", 20, ORANGE, "700", "middle"))
    for row_index, (identifier, score) in enumerate(zip(neighbor_ids, score_values)):
        field_row(
            out,
            978,
            164 + row_index * 30,
            [(48, str(identifier), ORANGE_L, ORANGE), (58, score, WHITE, ORANGE)],
            28,
            20,
        )
    out.append(line(966, 222, 974, 222, ORANGE, 2.8, "aOrange"))
    out.append(rect(1098, 171, 130, 78, GREEN_L, GREEN, 2.4, 6))
    out.append(text(1163, 196, "beam update", 20, GREEN, "700", "middle"))
    field_row(
        out,
        1111,
        208,
        [(34, str(identifier), WHITE, GREEN) for identifier in neighbor_ids],
        31,
        20,
    )
    out.append(line(1084, 222, 1098, 222, GREEN, 2.8, "aGreen"))
    box(out, 1240, 171, 102, 78, "top-R\nrptr reads", PURPLE_L, PURPLE, 20, rx=5, sw=2.4)
    out.append(line(1228, 222, 1240, 222, PURPLE, 2.8, "aPurple"))
    out.append(circle(1384, 210, 25, GREEN_L, GREEN, 2.6))
    out.append(text(1384, 217, "top-k", 20, GREEN, "700", "middle"))
    out.append(line(1342, 210, 1359, 210, GREEN, 2.8, "aGreen"))

    # Beam iteration is kept above the data-path labels and returns to candidate selection.
    out.append(
        path(
            "M1163,171 C1163,136 1118,126 1064,126 L540,126 C516,126 519,164 519,197",
            GREEN,
            2.8,
            "none",
            "aGreen",
        )
    )
    out.append(text(828, 117, "next candidate", 20, GREEN, "700", "middle"))

    # Online actions map vertically to their exact passive-memory objects.
    out.append(line(641, 258, 641, 386, PURPLE, 2.5, "aPurple"))
    elbow(out, [(903, 247), (903, 278), (820, 278), (820, 390)], GREEN, 2.7)
    elbow(
        out,
        [(1291, 249), (1291, 340), (1342, 340), (1342, 380), (1256, 380), (1256, 398)],
        PURPLE,
        2.5,
    )

    # Physical organization: authoritative source, address metadata, and striped records.
    out.append(text(232, 376, "MN: authoritative HNSW", 20, BLUE, "700"))
    field_row(
        out,
        232,
        391,
        [
            (74, f"L[{candidate_id}]", BLUE_L, BLUE),
            *((54, str(identifier), WHITE, BLUE) for identifier in neighbor_ids),
        ],
        42,
        20,
    )
    out.append(text(528, 376, "CN: prefix tables", 20, PURPLE, "700"))
    for offset in (12, 6, 0):
        out.append(rect(528 + offset, 389 + offset, 178, 70, WHITE, PURPLE, 2.0, 4))
    out.append(text(617, 414, prefix_label, 20, PURPLE, "700", "middle"))
    field_row(
        out,
        537,
        421,
        [(52, "MN0", PURPLE_L, PURPLE), (48, "off", WHITE, PURPLE), (60, "len", WHITE, PURPLE)],
        32,
        20,
    )

    out.append(text(920, 376, "MN: Slab regions", 20, GREEN, "700", "middle"))
    slab_widths = [48, 62, 44, 96, 62, 48]
    slab_strokes = [GREEN, GREEN, GREEN, GREEN, ORANGE, PURPLE]
    slab_fills = [GREEN_L, GREEN_L, WHITE, GREEN_L, ORANGE_L, PURPLE_L]
    slab_rows = (
        (390, "MN0", slab_label, neighbor_csv),
        (426, "MN1", f"B[{neighbor_ids[1]}]", f"{candidate_id},{neighbor_ids[2]}"),
    )
    for y, owner, block, identifiers in slab_rows:
        record(
            out,
            740,
            y,
            slab_widths,
            [owner, block, "hdr", identifiers, "codes", "rptr"],
            slab_fills,
            30,
            20,
            slab_strokes,
        )
    out.append(rect(850, 462, 74, 28, WHITE, RED, 2.2, 3, "7 5"))
    out.append(centered(850, 462, 74, 28, f"c={cold_candidate_id}", 20, RED, "700"))
    out.append(line(924, 476, 940, 476, RED, 2.2, "aRed", "7 5"))
    out.append(rect(944, 462, 160, 28, WHITE, RED, 2.2, 3, "7 5"))
    out.append(centered(944, 462, 160, 28, f"missing B[{cold_candidate_id}]", 20, RED, "700"))
    record(
        out,
        286,
        462,
        [70, 52, 52],
        [f"L[{cold_candidate_id}]", *(str(identifier) for identifier in cold_neighbor_ids)],
        [BLUE_L, WHITE, WHITE],
        28,
        20,
        [BLUE, BLUE, BLUE],
    )

    out.append(text(1122, 376, "MN: fp32 vectors", 20, BLUE, "700"))
    for x, identifier in zip((1122, 1214, 1306), neighbor_ids):
        box(out, x, 390, 84, 62, f"V[{identifier}]\nfp32", BLUE_L, BLUE, 20, rx=4, sw=2.2)
    box(out, 1132, 462, 104, 30, "descriptor", PURPLE_L, PURPLE, 20, rx=4, sw=2.0)
    record(
        out,
        1250,
        462,
        [70, 70],
        [f"V[{identifier}]" for identifier in cold_neighbor_ids],
        [BLUE_L, BLUE_L],
        30,
        20,
        [BLUE, BLUE],
    )

    # A missing cold record falls back to its blue list and fp32 vector records.
    out.append(
        path(
            "M944,476 L934,476 L934,500 L830,500",
            RED,
            2.6,
            "none",
            None,
            "7 5",
        )
    )
    out.append(
        path(
            "M808,500 L320,500 L320,494",
            RED,
            2.6,
            "none",
            "aRed",
            "7 5",
        )
    )
    out.append(
        path(
            "M1104,476 L1110,476 L1110,500",
            RED,
            2.6,
            "none",
            None,
            "7 5",
        )
    )
    out.append(path("M1110,500 L1176,500", RED, 2.6, "none", None, "7 5"))
    out.append(
        path(
            "M1192,500 L1355,500 L1355,494",
            RED,
            2.6,
            "none",
            "aRed",
            "7 5",
        )
    )
    out.append(text(590, 492, "cold fallback", 20, RED, "700", "middle"))

    # Compact offline preview: Figure 5 owns lifecycle detail.
    offline_centers = (244, 432, 620, 808, 996, 1184)
    offline_labels = (
        "scan",
        "rank",
        "encode",
        "pack",
        "write",
        "verify descriptor",
    )
    for index, (center_x, label) in enumerate(zip(offline_centers, offline_labels), start=1):
        out.append(circle(center_x, 546, 13, ORANGE_L, ORANGE, 2.2))
        out.append(text(center_x, 553, str(index), 20, ORANGE, "700", "middle"))
        out.append(text(center_x + 23, 553, label, 20, INK, "700"))
        if index < len(offline_centers):
            out.append(line(center_x + 112, 546, offline_centers[index] - 18, 546, ORANGE, 2.4, "aOrange"))
    out.append(line(244, 433, 244, 531, BLUE, 2.5, "aBlue"))
    elbow(out, [(1002, 532), (1002, 510), (819, 510), (819, 456)], GREEN, 2.5)
    out.append(line(1184, 532, 1184, 494, PURPLE, 2.5, "aPurple"))
    out.append("</svg>")
    return "\n".join(out)


def slab_layout_panel_layout() -> str:
    out = canvas(1440, 570)
    out.append(text(720, 36, "Expansion-oriented physical layout", 28, INK, "700", "middle"))

    materialization = (24, 60, 680, 224)
    varblock = (720, 60, 696, 224)
    codes = (24, 300, 456, 238)
    record_panel = (496, 300, 920, 238)
    panel(
        out,
        *materialization,
        "(a) degree-bounded materialization",
        NAVY,
        PALE,
        title_size=23,
    )
    panel(out, *varblock, "(b) live-prefix records", NAVY, PALE, title_size=23)
    panel(out, *codes, "(c) configured scoring code", NAVY, PALE, title_size=23)
    panel(
        out,
        *record_panel,
        "(d) canonical B[u] byte layout",
        NAVY,
        PALE,
        title_size=23,
    )

    # (a) Build-time ranking chooses a materialized prefix; the cold tail stays authoritative.
    out.append(text(48, 122, "in-degree rank", 22, ORANGE, "700"))
    out.append(line(204, 115, 660, 115, ORANGE, 2.6, "aOrange"))
    out.append(text(48, 154, "IDs", 22, MUTED, "700"))
    for x, label in ((130, "42"), (230, "57"), (330, "81"), (520, "96")):
        box(out, x, 132, 80, 34, label, WHITE, ORANGE, 20, rx=5, sw=2.0)
    out.append(text(466, 130, "fN", 22, RED, "700"))
    out.append(line(456, 124, 456, 224, RED, 2.4, dash="7 5"))
    out.append(text(48, 184, "hot prefix", 22, GREEN, "700"))
    out.append(text(48, 208, "Slabs", 22, GREEN, "700"))
    for x, label in ((122, "B[42]"), (222, "B[57]"), (322, "B[81]")):
        box(out, x, 184, 96, 36, label, GREEN_L, GREEN, 22, rx=5, sw=2.2)
        out.append(line(x + 48, 166, x + 48, 182, GREEN, 2.2, "aGreen"))
    out.append(line(560, 166, 560, 182, RED, 2.2, "aRed"))
    box(out, 464, 184, 220, 36, "authoritative fallback", RED_L, RED, 20, rx=5, sw=2.0)
    box(out, 48, 228, 290, 50, "build-time policy\n+ measured coverage", GREY, NAVY, 22, rx=5, sw=1.8)
    box(out, 350, 228, 334, 50, "static budget map\nrecorded in descriptor", PURPLE_L, PURPLE, 22, rx=5, sw=1.8)

    # (b) Removing fixed slots makes the physical record track the live degree.
    out.append(text(744, 121, "fixed capacity 5: B[42], d(42)=3, live IDs {23,57,81}", 22, MUTED, "700"))
    record(
        out,
        744,
        130,
        [100, 78, 78, 78, 98, 98],
        ["B[42]", "23", "57", "81", "pad", "pad"],
        [GREEN_L, TEAL_L, TEAL_L, TEAL_L, RED_L, RED_L],
        42,
        20,
        [GREEN, TEAL, TEAL, TEAL, RED, RED],
    )
    out.append(path("M1078,174 L1078,182 L1274,182 L1274,174", RED, 2.2))
    out.append(text(1176, 204, "padding removed", 22, RED, "700", "middle"))
    out.append(text(744, 229, "variable d(u): 3 live entries", 22, GREEN, "700"))
    record(
        out,
        744,
        236,
        [100, 78, 78, 78],
        ["B[42]", "23", "57", "81"],
        [GREEN_L, TEAL_L, TEAL_L, TEAL_L],
        40,
        20,
        [GREEN, TEAL, TEAL, TEAL],
    )
    box(out, 1092, 236, 294, 40, "pay for d(u), not capacity", GREEN_L, GREEN, 22, rx=5, sw=2.0)

    # (c) The serving code is configured offline and guarded by metric and dimension.
    box(out, 44, 344, 412, 30, "offline configuration", GREY, NAVY, 22, rx=5, sw=1.8)
    box(out, 44, 380, 412, 30, "metric + dimension guard", ORANGE_L, ORANGE, 22, rx=5, sw=1.8)
    box(out, 44, 416, 112, 38, "sq8", BLUE_L, BLUE, 22, rx=5, sw=2.0)
    out.append(text(172, 442, "evaluated low-d L2", 22, BLUE, "700"))
    box(out, 44, 460, 150, 38, "RaBitQ-2", GREEN_L, GREEN, 22, rx=5, sw=2.0)
    out.append(text(208, 486, "evaluated 960d L2", 22, GREEN, "700"))
    out.append(line(44, 508, 456, 508, RED, 2.4, dash="7 5"))
    out.append(text(250, 526, "evaluated TTI inner-product boundary", 22, RED, "700", "middle"))

    # (d) Addressing resolves one live-prefix record with canonical neighbor fields.
    box(out, 518, 360, 120, 34, "prefix[42]", BLUE_L, BLUE, 22, rx=5, sw=2.0)
    out.append(line(640, 377, 662, 377, BLUE, 2.4, "aBlue"))
    box(out, 666, 360, 150, 34, "offset, length", WHITE, BLUE, 22, rx=5, sw=2.0)
    out.append(line(818, 377, 838, 377, BLUE, 2.4, "aBlue"))
    box(out, 842, 360, 88, 34, "B[42]", GREEN_L, GREEN, 22, rx=5, sw=2.0)
    out.append(line(932, 377, 958, 377, GREEN, 2.4, "aGreen"))
    out.append(text(1097, 350, "header", 22, INK, "700", "middle"))
    out.append(path("M962,354 L962,357 L1232,357 L1232,354", INK, 2.0))
    record(
        out,
        962,
        360,
        [110, 78, 82],
        ["count=3", "bits", "flags"],
        [GREY, GREY, GREY],
        34,
        22,
    )

    consumer_fields = (
        (610, 150, "next hop", BLUE_L, BLUE),
        (760, 175, "exact rerank", PURPLE_L, PURPLE),
        (935, 215, "approx. SIMD score", ORANGE_L, ORANGE),
    )
    out.append(text(570, 424, "ID", 22, MUTED, "700", "middle"))
    for x, width, label, fill, stroke in consumer_fields:
        box(out, x, 400, width, 32, label, fill, stroke, 22, rx=4, sw=1.8)
    for center_x, color in ((685, BLUE), (847.5, PURPLE), (1042.5, ORANGE)):
        out.append(line(center_x, 432, center_x, 438, color, 2.0))

    for y, identifier in ((438, 23), (470, 57), (502, 81)):
        out.append(text(570, y + 22, identifier, 20, INK, "700", "middle"))
        out.append(text(600, y + 22, "<", 22, INK, "700", "middle"))
        record(
            out,
            610,
            y,
            [150, 175, 215],
            ["nbr_slot", "nbr_rptr", "nbr_qvec"],
            [BLUE_L, PURPLE_L, ORANGE_L],
            30,
            22,
            [BLUE, PURPLE, ORANGE],
        )
        out.append(text(1160, y + 22, ">", 22, INK, "700", "middle"))
    box(out, 1176, 414, 218, 50, "live prefix only\ncount = 3", GREEN_L, GREEN, 22, rx=6, sw=2.0)
    box(out, 1176, 474, 218, 50, "fixed padding = 0", WHITE, RED, 22, rx=6, sw=2.0)
    out.append("</svg>")
    return "\n".join(out)


def search_placement_panel_layout() -> str:
    out = canvas(1440, 570)
    out.append(text(24, 38, "One-read search and placement", 28, INK, "700"))
    for x, width, label, color in (
        (826, 120, "D = 0", GREEN),
        (956, 120, "c = 1", BLUE),
        (1086, 150, "R bounded", PURPLE),
        (1246, 170, "owners striped", TEAL),
    ):
        chip(out, x, 8, width, 38, label, color, 20)

    panels = (
        (24, 62, 350, 454, "(a) resident upper graph"),
        (390, 62, 642, 454, "(b) one-read expansion"),
        (1048, 62, 368, 454, "(c) block-cyclic placement"),
    )
    for x, y, width, height, label in panels:
        panel(out, x, y, width, height, label, NAVY, PALE, title_size=23)

    # (a) Upper layers are exact CN state; level 0 begins outside that boundary.
    resident_boundary = rect(44, 112, 310, 224, GREEN_L, GREEN, 2.4, 7)
    out.append(
        resident_boundary.replace(
            "<rect ", '<rect data-role="resident-boundary" ', 1
        )
    )
    out.append(text(60, 140, "resident upper graph", 22, GREEN, "700"))
    out.append(text(60, 169, "exact fp32 descent", 22, NAVY, "700"))
    out.append(text(60, 214, "L2", 20, GREEN, "700"))
    out.append(text(60, 286, "L1", 20, GREEN, "700"))
    upper_nodes = {34: (190, 204), 66: (146, 274), 92: (272, 274)}
    for left, right in ((34, 66), (34, 92), (66, 92)):
        out.append(line(*upper_nodes[left], *upper_nodes[right], GREEN, 2.2))
    for identifier, (x, y) in upper_nodes.items():
        node(out, x, y, str(identifier), WHITE, GREEN, 17, 20)
    out.append(line(202, 220, 257, 258, NAVY, 2.6, "aNavy"))
    elbow(out, [(272, 292), (272, 352), (278, 352), (278, 381)], GREEN, 2.8)
    out.append(text(60, 361, "exact local seed", 22, GREEN, "700"))
    out.append(rect(44, 374, 310, 72, WHITE, BLUE, 2.2, 6, "7 5"))
    out.append(text(60, 401, "remote level 0", 22, BLUE, "700"))
    out.append(text(60, 429, "not resident", 20, MUTED, "700"))
    out.append('<g data-role="level0-seed">')
    box(out, 218, 386, 116, 42, "u=42", BLUE_L, BLUE, 22, rx=5, sw=2.2)
    out.append("</g>")
    box(out, 44, 460, 310, 38, "D: 0 remote READs", GREEN_L, GREEN, 22, rx=5, sw=2.2)

    # (b) One global beam iteration consumes one complete Slab record.
    out.append(text(1012, 103, "one global HNSW beam", 20, NAVY, "700", "end"))
    out.append(rect(410, 126, 76, 62, GREEN_L, GREEN, 2.2, 6))
    out.append(text(448, 151, "pop u", 22, INK, "700", "middle"))
    out.append(text(448, 178, "u=42", 20, GREEN, "700", "middle"))
    box(out, 502, 126, 110, 62, "prefix[u]", PURPLE_L, PURPLE, 22, rx=6, sw=2.2)
    box(out, 628, 126, 118, 62, "READ B[u]", GREEN_L, GREEN, 22, rx=6, sw=2.4)
    out.append('<g data-role="approx-stage">')
    out.append(rect(762, 126, 124, 62, ORANGE_L, ORANGE, 2.4, 6))
    out.append(centered(762, 126, 124, 62, "approx. SIMD\nscore", 22, INK, "700"))
    out.append("</g>")
    out.append('<g data-role="beam-stage">')
    out.append(rect(902, 126, 110, 62, GREEN_L, GREEN, 2.4, 6))
    out.append(centered(902, 126, 110, 62, "beam\nupdate", 22, INK, "700"))
    out.append("</g>")
    for left, right, color, marker_id in (
        (486, 498, PURPLE, "aPurple"),
        (612, 624, GREEN, "aGreen"),
        (746, 758, ORANGE, "aOrange"),
        (886, 898, GREEN, "aGreen"),
    ):
        out.append(line(left, 157, right, 157, color, 2.8, marker_id))
    out.append(
        '<polyline data-role="beam-loop" data-from="beam update" data-to="pop u" '
        'points="957,126 957,109 448,109 448,122" fill="none" '
        f'stroke="{GREEN}" stroke-width="2.8" stroke-linejoin="round" '
        'marker-end="url(#aGreen)"/>'
    )
    out.append(rect(650, 97, 118, 24, WHITE, WHITE, 0, 4))
    out.append(centered(650, 97, 118, 24, "pop next", 20, GREEN, "700"))

    elbow(out, [(687, 188), (687, 206), (451, 206), (451, 226)], GREEN, 2.4)
    out.append(text(526, 224, "ID", 20, MUTED, "700", "middle"))
    out.append(text(599, 224, "qvec", 20, ORANGE, "700", "middle"))
    out.append(text(695, 224, "rptr", 20, PURPLE, "700", "middle"))
    out.append(text(824, 214, "d(u) live entries", 20, ORANGE, "700", "middle"))
    out.append(text(970, 214, "done", 20, PURPLE, "700"))
    box(out, 410, 230, 82, 100, "B[42]\nd(u)=3", GREEN_L, GREEN, 22, rx=5, sw=2.2)
    for row, identifier in enumerate((23, 57, 81)):
        y = 230 + row * 34
        out.append(f'<g data-role="slab-entry" data-id="{identifier}">')
        record(
            out,
            500,
            y,
            [52, 94, 98],
            [str(identifier), "qvec", "rptr"],
            [TEAL_L, ORANGE_L, PURPLE_L],
            32,
            20,
            [TEAL, ORANGE, PURPLE],
        )
        out.append("</g>")
    out.append(rect(410, 342, 160, 42, ORANGE_L, ORANGE, 2.0, 5))
    out.append(centered(410, 342, 160, 42, "qvec:\napproximate", 20, ORANGE, "700"))
    out.append('<g data-role="rptr-legend">')
    out.append(rect(580, 342, 164, 42, PURPLE_L, PURPLE, 2.0, 5))
    out.append(centered(580, 342, 164, 42, "rptr:\nfp32 address", 20, PURPLE, "700"))
    out.append("</g>")

    out.append('<g data-role="topr-stage">')
    out.append(rect(762, 224, 250, 56, PURPLE_L, PURPLE, 2.2, 6))
    out.append(text(887, 247, "top-R survivors", 22, PURPLE, "700", "middle"))
    out.append(text(887, 272, "{23,57} + rptr", 20, INK, "700", "middle"))
    out.append("</g>")
    out.append(
        '<line data-role="beam-exit" data-from="beam update" '
        'data-to="top-R survivors" x1="957" y1="188" x2="957" y2="224" '
        f'stroke="{PURPLE}" stroke-width="2.6" marker-end="url(#aPurple)"/>'
    )
    out.append(
        '<line data-role="rerank-read-edge" data-from="top-R survivors" '
        'data-to="READ authoritative fp32" x1="887" y1="280" '
        f'x2="887" y2="290" stroke="{PURPLE}" stroke-width="2.6" '
        'marker-end="url(#aPurple)"/>'
    )
    out.append(rect(762, 294, 250, 52, BLUE_L, BLUE, 2.2, 6))
    out.append(centered(762, 294, 250, 52, "READ authoritative\nfp32", 22, BLUE, "700"))
    out.append(
        '<line data-role="exact-rerank-edge" data-from="READ authoritative fp32" '
        'data-to="exact rerank" x1="887" y1="346" x2="887" y2="356" '
        f'stroke="{BLUE}" stroke-width="2.6" marker-end="url(#aBlue)"/>'
    )
    out.append('<g data-role="exact-stage">')
    out.append(rect(762, 360, 154, 42, BLUE_L, BLUE, 2.4, 6))
    out.append(centered(762, 360, 154, 42, "exact rerank", 22, BLUE, "700"))
    out.append("</g>")
    out.append(line(916, 381, 946, 381, GREEN, 2.6, "aGreen"))
    out.append(circle(978, 381, 25, GREEN_L, GREEN, 2.4))
    out.append(text(978, 388, "top-k", 20, GREEN, "700", "middle"))

    out.append('<g data-role="cold-fallback">')
    out.append(rect(410, 410, 602, 90, WHITE, RED, 2.0, 6, "7 5"))
    out.append(text(424, 436, "cold fallback", 20, RED, "700"))
    out.append(text(870, 436, "authoritative list/vector path", 20, BLUE, "700", "end"))
    out.append(rect(424, 448, 140, 36, WHITE, RED, 2.0, 4, "7 5"))
    out.append(centered(424, 448, 140, 36, "missing B[96]", 20, RED, "700"))
    out.append(line(564, 466, 580, 466, RED, 2.4, "aRed", "7 5"))
    box(out, 584, 448, 70, 36, "L[96]", BLUE_L, BLUE, 20, rx=4, sw=2.0)
    out.append(line(654, 466, 670, 466, RED, 2.4, "aRed", "7 5"))
    record(
        out,
        674,
        448,
        [98, 98],
        ["V[34]", "V[66]"],
        [BLUE_L, BLUE_L],
        36,
        20,
        [BLUE, BLUE],
    )
    out.append(line(870, 466, 886, 466, RED, 2.4, "aRed", "7 5"))
    out.append(rect(890, 442, 108, 48, WHITE, RED, 2.0, 5, "7 5"))
    out.append(centered(890, 442, 108, 48, "return to\nbeam", 20, RED, "700"))
    out.append(
        '<polyline data-role="fallback-rejoin" data-from="cold fallback" '
        'data-to="beam update" points="944,442 1020,442 1020,157 1012,157" '
        f'fill="none" stroke="{RED}" stroke-width="2.2" '
        'stroke-dasharray="7 5" stroke-linejoin="round" '
        'marker-end="url(#aRed)"/>'
    )
    out.append("</g>")

    # (c) Whole records are striped by ID; workers address owners directly.
    out.append(text(1066, 122, "owner(u) = u mod S", 22, TEAL, "700"))
    out.append(text(1398, 122, "S=3", 20, TEAL, "700", "end"))
    out.append(text(1066, 151, "no router", 22, NAVY, "700"))
    out.append(text(1398, 151, "direct owner READs", 20, GREEN, "700", "end"))
    for x, width, worker, record_id, owner in (
        (1134, 130, "CN-A", 1, "MN1"),
        (1274, 124, "CN-B", 2, "MN2"),
    ):
        out.append(
            f'<g data-role="cn-worker" data-record="{record_id}" '
            f'data-owner="{owner}">'
        )
        out.append(rect(x, 164, width, 54, GREEN_L, GREEN, 2.2, 5))
        out.append(text(x + width / 2, 186, worker, 22, INK, "700", "middle"))
        out.append(text(x + width / 2, 210, f"READ B[{record_id}]", 20, INK, "700", "middle"))
        out.append("</g>")

    stripe_x = (1066, 1172, 1278)
    owner_records = ((0, 3, 6, 9), (1, 4, 7, 10), (2, 5, 8, 11))
    for owner, (x, identifiers) in enumerate(zip(stripe_x, owner_records)):
        out.append(rect(x, 240, 98, 220, WHITE, TEAL, 2.2, 6))
        out.append(text(x + 49, 270, f"MN{owner}", 22, TEAL, "700", "middle"))
        for row, identifier in enumerate(identifiers):
            y = 282 + row * 40
            out.append(
                f'<g data-role="placement-record" data-record="{identifier}" '
                f'data-owner="MN{owner}">'
            )
            box(out, x + 7, y, 84, 31, f"B[{identifier}]", GREEN_L, GREEN, 20, rx=4, sw=1.9)
            out.append("</g>")

    out.append(
        '<polyline data-role="direct-read" data-record="1" data-owner="MN1" '
        'points="1264,191 1269,191 1269,297 1263,297" fill="none" '
        f'stroke="{GREEN}" stroke-width="2.6" stroke-linejoin="round" '
        'marker-end="url(#aGreen)"/>'
    )
    out.append(
        '<polyline data-role="direct-read" data-record="2" data-owner="MN2" '
        'points="1398,191 1404,191 1404,297 1369,297" fill="none" '
        f'stroke="{GREEN}" stroke-width="2.6" stroke-linejoin="round" '
        'marker-end="url(#aGreen)"/>'
    )
    box(
        out,
        1066,
        466,
        332,
        42,
        "each B[u] intact\nno split / no inter-MN assembly",
        TEAL_L,
        TEAL,
        20,
        rx=5,
        sw=2.0,
    )
    out.append("</svg>")
    return "\n".join(out)


def construction_refresh_legacy() -> str:
    out = canvas(1440, 550)
    out.append(
        text(
            720,
            36,
            "Construction and refresh of a derived access structure",
            28,
            INK,
            "700",
            "middle",
        )
    )

    full_build = (24, 62, 1392, 214)
    refresh = (24, 292, 1092, 224)
    constraints = (1132, 292, 284, 224)
    out.append(rect(*full_build, WHITE, INK, 2.4, 8))
    out.append(rect(*refresh, PALE, MUTED, 2.4, 8))
    out.append(rect(*constraints, WHITE, NAVY, 2.4, 8))
    out.append(text(42, 91, "(a) full construction", 24, NAVY, "700"))
    out.append(text(42, 321, "(b) differential refresh", 24, GREEN, "700"))
    out.append(text(1150, 322, "measured feasibility", 23, NAVY, "700"))
    out.append(text(1150, 348, "checked deployment point", 20, MUTED, "700"))

    # One authoritative graph feeds six concrete, left-to-right build states.
    out.append('<g data-role="authoritative-source">')
    out.append(rect(42, 102, 140, 158, BLUE_L, NAVY, 2.4, 7))
    out.append(centered(46, 106, 132, 44, "authoritative\nHNSW", 20, NAVY, "700"))
    graph_points = [(60, 175), (88, 154), (116, 173), (76, 199), (120, 199), (158, 175)]
    graph(
        out,
        graph_points,
        [(0, 1), (1, 2), (0, 3), (1, 4), (2, 5), (3, 4), (4, 5)],
        BLUE_L,
        NAVY,
        6,
    )
    box(out, 50, 216, 124, 40, "topology\nunchanged", WHITE, NAVY, 20, rx=4, sw=1.8)
    out.append("</g>")

    stage_x = (196, 397, 598, 799, 1000, 1201)
    stage_w = 184
    stage_data = (
        ("1", "scan / count", "graph-counts", BLUE_L, BLUE),
        ("2", "choose fN", "rank-cutoff", ORANGE_L, ORANGE),
        ("3", "configure code", "code-config", ORANGE_L, ORANGE),
        ("4", "encode / pack", "live-varblock", PURPLE_L, PURPLE),
        ("5", "place records", "mn-stripes", TEAL_L, TEAL),
        ("6", "publish epoch e", "epoch-metadata", PURPLE_L, PURPLE),
    )
    for index, (x, (number, operation, object_kind, fill, stroke)) in enumerate(
        zip(stage_x, stage_data)
    ):
        out.append(f'<g data-role="full-build-stage" data-stage="{number}">')
        out.append(rect(x, 101, stage_w, 54, fill, stroke, 2.2, 6))
        out.append(circle(x + 18, 128, 13, stroke, stroke, 1.6))
        out.append(text(x + 18, 135, number, 20, WHITE, "700", "middle"))
        operation_display = {
            "configure code": "configure\ncode",
            "encode / pack": "encode /\npack",
            "publish epoch e": "publish\nepoch e",
        }.get(operation, operation)
        operation_y = 119 if "\n" in operation_display else 136
        out.append(text(x + 38, operation_y, operation_display, 22, INK, "700"))
        out.append(f'<g data-role="stage-object" data-kind="{object_kind}">')

        if number == "1":
            points = [
                (x + 18, 186),
                (x + 43, 167),
                (x + 69, 185),
                (x + 28, 214),
                (x + 68, 214),
            ]
            graph(out, points, [(0, 1), (1, 2), (0, 3), (1, 4), (3, 4)], BLUE_L, BLUE, 5)
            for bar_index, height in enumerate((54, 42, 31, 20)):
                out.append(
                    rect(
                        x + 102 + bar_index * 19,
                        226 - height,
                        12,
                        height,
                        BLUE_L,
                        BLUE,
                        1.5,
                        1,
                    )
                )
            out.append(text(x + 132, 257, "d(u)", 20, BLUE, "700", "middle"))
        elif number == "2":
            for bar_index, height in enumerate((62, 50, 39, 28, 18)):
                selected = bar_index < 3
                out.append(
                    rect(
                        x + 17 + bar_index * 29,
                        229 - height,
                        17,
                        height,
                        ORANGE_L if selected else GREY,
                        ORANGE if selected else GRID,
                        1.5,
                        1,
                    )
                )
            out.append(line(x + 96, 159, x + 96, 235, RED, 2.0, dash="6 4"))
            out.append(text(x + 96, 260, "fN=3", 20, RED, "700", "middle"))
        elif number == "3":
            out.append(rect(x + 15, 164, 154, 86, ORANGE_L, ORANGE, 2.0, 5))
            out.append(line(x + 15, 193, x + 169, 193, ORANGE, 1.4))
            out.append(line(x + 15, 221, x + 169, 221, ORANGE, 1.4))
            out.append(text(x + 28, 187, "metric L2", 20, INK, "700"))
            out.append(text(x + 28, 215, "code sq8", 20, INK, "700"))
            out.append(text(x + 28, 243, "R=32", 20, INK, "700"))
        elif number == "4":
            out.append(text(x + 92, 179, "d(u)=3 live", 20, GREEN, "700", "middle"))
            record(
                out,
                x + 14,
                190,
                [32, 40, 40, 40],
                ["h", "23", "57", "81"],
                [PURPLE_L, GREEN_L, GREEN_L, GREEN_L],
                40,
                20,
                [PURPLE, GREEN, GREEN, GREEN],
            )
            out.append(text(x + 90, 258, "varblock B[u]", 20, PURPLE, "700", "middle"))
        elif number == "5":
            out.append(text(x + 92, 179, "whole B[u]", 20, TEAL, "700", "middle"))
            for owner, stripe_x in enumerate((x + 14, x + 68, x + 122)):
                out.append(rect(stripe_x, 187, 48, 57, WHITE, TEAL, 1.8, 4))
                out.append(text(stripe_x + 24, 208, f"MN{owner}", 20, TEAL, "700", "middle"))
                for slot in range(3):
                    out.append(
                        rect(
                            stripe_x + 5 + slot * 13,
                            218,
                            11,
                            18,
                            GREEN_L,
                            GREEN,
                            1.1,
                            1,
                        )
                    )
            out.append(text(x + 92, 263, "u mod S", 20, TEAL, "700", "middle"))
        else:
            box(out, x + 15, 164, 154, 28, "epoch e", PURPLE_L, PURPLE, 20, rx=4, sw=1.8)
            box(out, x + 15, 198, 154, 28, "prefix", BLUE_L, BLUE, 20, rx=4, sw=1.8)
            box(out, x + 15, 232, 154, 28, "code + S", GREEN_L, GREEN, 20, rx=4, sw=1.8)

        out.append("</g>")
        out.append("</g>")
        if index < len(stage_x) - 1:
            next_x = stage_x[index + 1]
            out.append(
                f'<path d="M{x + stage_w + 2},120 L{next_x - 3},127 '
                f'L{x + stage_w + 2},134 Z" fill="{stroke}" stroke="none"/>'
            )
    out.append(
        f'<path d="M184,120 L193,127 L184,134 Z" fill="{BLUE}" stroke="none"/>'
    )

    # Authoritative list changes map to derived Slab IDs before either epoch.
    out.append('<g data-role="changed-authoritative-source">')
    out.append(rect(42, 340, 130, 126, WHITE, NAVY, 2.2, 7))
    out.append(text(107, 360, "authoritative\nHNSW lists", 20, NAVY, "700", "middle"))
    for identifier, record_y in ((1, 394), (3, 430)):
        out.append(
            f'<g data-role="changed-authoritative-record" data-id="{identifier}">'
        )
        out.append(rect(52, record_y, 110, 28, RED_L, RED, 2.0, 4))
        out.append(centered(52, record_y, 110, 28, f"L[{identifier}]*", 20, INK, "700"))
        out.append("</g>")
    out.append("</g>")

    out.append('<g data-role="touch-mapping">')
    out.append(rect(188, 340, 130, 126, WHITE, ORANGE, 2.2, 7))
    out.append(text(253, 360, "touched\nSlab IDs", 22, ORANGE, "700", "middle"))
    for identifier, record_y in ((1, 394), (3, 430)):
        out.append(
            f'<g data-role="touched-slab-id" data-id="{identifier}">'
        )
        out.append(rect(198, record_y, 110, 28, RED_L, RED, 2.0, 4))
        out.append(centered(198, record_y, 110, 28, f"B[{identifier}]", 20, INK, "700"))
        out.append("</g>")
        out.append(
            f'<line data-role="touch-mapping-edge" data-from="L[{identifier}]*" '
            f'data-to="B[{identifier}]" x1="164" y1="{record_y + 14}" '
            f'x2="194" y2="{record_y + 14}" stroke="{RED}" stroke-width="2.4" '
            'marker-end="url(#aRed)"/>'
        )
    out.append("</g>")

    # Epoch e contains derived Slab records only; touched records are cross-marked.
    out.append('<g data-role="published-epoch" data-epoch="e" data-phase="before">')
    out.append(rect(334, 340, 212, 126, WHITE, PURPLE, 2.2, 7))
    box(out, 342, 348, 196, 38, "published epoch e", PURPLE_L, PURPLE, 22, rx=5, sw=1.8)
    out.append(text(440, 407, "derived Slab", 20, PURPLE, "700", "middle"))
    for identifier, tile_x in enumerate((342, 390, 438, 486)):
        touched_role = " touched-record" if identifier in {1, 3} else ""
        pattern = ' data-pattern="cross"' if identifier in {1, 3} else ""
        state = ' data-state="stale-selection"' if identifier in {1, 3} else ""
        out.append(
            f'<g data-role="epoch-slab-record{touched_role}" data-epoch="e" '
            f'data-id="{identifier}"{pattern}{state}>'
        )
        record_fill = RED_L if identifier in {1, 3} else GREY
        record_stroke = RED if identifier in {1, 3} else MUTED
        out.append(rect(tile_x, 430, 44, 28, record_fill, record_stroke, 2.0, 4))
        out.append(centered(tile_x, 430, 44, 28, f"B[{identifier}]", 20, INK, "700"))
        if identifier in {1, 3}:
            out.append(
                f'<g data-role="touched-marker-badge" data-id="{identifier}">'
            )
            out.append(circle(tile_x + 37, 422, 7, RED, RED, 1.4))
            out.append(line(tile_x + 34, 419, tile_x + 40, 425, WHITE, 1.8))
            out.append(line(tile_x + 40, 419, tile_x + 34, 425, WHITE, 1.8))
            out.append("</g>")
        out.append("</g>")
    out.append("</g>")

    out.append(
        '<line data-role="epoch-reuse-input" data-from="published epoch e" '
        'data-to="reuse B[0],B[2]" x1="546" y1="396" x2="558" y2="396" '
        f'stroke="{MUTED}" stroke-width="2.8" marker-end="url(#aMuted)"/>'
    )
    out.append(
        '<polyline data-role="touch-rebuild-input" data-from="touched Slab IDs" '
        'data-to="rebuilt B[1],B[3]" '
        'points="318,426 326,426 326,471 554,471 554,439 558,439" '
        f'fill="none" stroke="{GREEN}" stroke-width="2.8" '
        'stroke-linejoin="round" marker-end="url(#aGreen)"/>'
    )
    out.append('<g data-role="scratch-epoch">')
    out.append(rect(562, 340, 270, 126, WHITE, PURPLE, 2.2, 7, "8 5"))
    out.append(text(578, 370, "scratch epoch e+1", 23, PURPLE, "700"))
    out.append(text(578, 407, "reuse", 22, MUTED, "700"))
    out.append(text(578, 450, "rebuilt", 22, GREEN, "700"))
    for identifier, tile_x in ((0, 660), (2, 708)):
        out.append(
            f'<g data-role="reused-record epoch-slab-record" data-epoch="e+1" '
            f'data-id="{identifier}" data-pattern="diagonal">'
        )
        out.append(rect(tile_x, 379, 42, 34, GREY, MUTED, 1.8, 4, "5 3"))
        out.append(line(tile_x + 3, 407, tile_x + 18, 383, GRID, 1.4))
        out.append(line(tile_x + 20, 409, tile_x + 35, 383, GRID, 1.4))
        out.append(centered(tile_x, 379, 42, 34, f"B[{identifier}]", 20, INK, "700"))
        out.append("</g>")
    for identifier, tile_x in ((1, 660), (3, 708)):
        out.append(
            f'<g data-role="rebuilt-record epoch-slab-record" data-epoch="e+1" '
            f'data-id="{identifier}" data-outline="double">'
        )
        out.append(rect(tile_x, 422, 42, 34, GREEN_L, GREEN, 3.0, 4))
        out.append(rect(tile_x + 4, 426, 34, 26, "none", GREEN, 1.3, 2))
        out.append(centered(tile_x, 422, 42, 34, f"B[{identifier}]", 20, INK, "700"))
        out.append("</g>")
    out.append("</g>")

    out.append(text(871, 380, "pointer\nswitch", 20, PURPLE, "700", "middle"))
    out.append(
        '<line data-role="epoch-pointer-switch" data-from="scratch epoch e+1" '
        'data-to="published epoch e+1" x1="838" y1="425" x2="900" y2="425" '
        f'stroke="{PURPLE}" stroke-width="3.2" marker-end="url(#aPurple)"/>'
    )
    out.append('<g data-role="published-epoch" data-epoch="e+1" data-phase="after">')
    out.append(rect(910, 348, 186, 118, WHITE, PURPLE, 2.4, 7))
    box(out, 918, 358, 170, 48, "published\nepoch e+1", PURPLE_L, PURPLE, 22, rx=5, sw=2.0)
    out.append(circle(930, 436, 8, BLUE_L, NAVY, 1.8))
    out.append(line(930, 444, 930, 457, NAVY, 2.0))
    out.append(text(948, 448, "reader", 20, NAVY, "700"))
    out.append(
        '<line data-role="reader-view" data-to="published epoch e+1" '
        'x1="1050" y1="448" x2="1050" y2="413" '
        f'stroke="{PURPLE}" stroke-width="2.6" marker-end="url(#aPurple)"/>'
    )
    out.append("</g>")

    out.append(text(42, 500, "offline / read-mostly refresh", 20, MUTED, "700"))
    out.append(text(334, 500, "derived structure: discard / rebuild", 20, MUTED, "700"))
    out.append('<g data-role="reader-view-message">')
    out.append(text(1096, 500, "readers open one published epoch", 22, PURPLE, "700", "end"))
    out.append("</g>")

    # Five measured guardrails, deliberately presented without tuning controls.
    check_rows = (
        (372, "MN bytes"),
        (401, "CN state"),
        (430, "bytes/q"),
        (459, "recall target"),
        (488, "build/refresh cost"),
    )
    for center_y, label in check_rows:
        out.append(f'<g data-role="constraint-check" data-name="{label}">')
        out.append(circle(1162, center_y, 9, GREEN_L, GREEN, 1.8))
        out.append(
            path(
                f"M1157,{center_y} L1161,{center_y + 4} L1168,{center_y - 5}",
                GREEN,
                2.2,
            )
        )
        out.append(text(1184, center_y + 7, label, 20, INK, "700"))
        out.append("</g>")

    out.append("</svg>")
    return "\n".join(out)


def construction_refresh_row_layout() -> str:
    out = canvas(1440, 550)
    out.append(
        text(
            720,
            34,
            "Construction and offline refresh",
            28,
            INK,
            "700",
            "middle",
        )
    )

    panel(
        out,
        18,
        52,
        1404,
        238,
        "(a) derive a self-describing physical access structure",
        NAVY,
        WHITE,
        title_size=23,
    )
    panel(
        out,
        18,
        305,
        1404,
        225,
        "(b) fixed-stride offline replay control",
        GREEN,
        PALE,
        title_size=23,
    )

    # Full build source.
    out.append('<g data-role="authoritative-source" data-phase="build">')
    out.append(rect(34, 94, 140, 176, BLUE_L, NAVY, 2.2, 7))
    out.append(text(104, 116, "authoritative\nHNSW", 20, NAVY, "700", "middle"))
    graph(
        out,
        [(56, 173), (85, 148), (116, 170), (67, 207), (107, 215), (151, 190)],
        [(0, 1), (1, 2), (0, 3), (1, 4), (2, 5), (3, 4), (4, 5)],
        BLUE_L,
        NAVY,
        6,
    )
    out.append(text(104, 235, "topology", 20, NAVY, "700", "middle"))
    out.append(text(104, 258, "unchanged", 20, NAVY, "700", "middle"))
    out.append("</g>")

    stage_specs = (
        (190, 164, "64 MiB scan", BLUE_L, BLUE),
        (364, 172, "select fN", ORANGE_L, ORANGE),
        (546, 185, "pack B[u] (20T)", PURPLE_L, PURPLE),
        (741, 170, "owner u % S", TEAL_L, TEAL),
        (921, 170, "WRITE extents", GREEN_L, GREEN),
        (1101, 294, "write + verify descriptor", PURPLE_L, PURPLE),
    )
    for number, (x, width, label, fill, stroke) in enumerate(stage_specs, 1):
        out.append(f'<g data-role="build-stage" data-stage="{number}">')
        out.append(rect(x, 94, width, 46, fill, stroke, 2.0, 5))
        out.append(circle(x + 17, 117, 12, stroke, stroke, 1.4))
        out.append(text(x + 17, 124, str(number), 20, WHITE, "700", "middle"))
        out.append(text(x + 36, 124, label, 20, INK, "700"))

        if number == 1:
            graph(
                out,
                [(x + 18, 180), (x + 46, 159), (x + 76, 180), (x + 31, 215), (x + 72, 216)],
                [(0, 1), (1, 2), (0, 3), (1, 4), (3, 4)],
                BLUE_L,
                BLUE,
                5,
            )
            for idx, height in enumerate((56, 43, 30, 18)):
                out.append(rect(x + 104 + idx * 15, 225 - height, 10, height, BLUE_L, BLUE, 1.2, 1))
            out.append(text(x + 82, 250, "validate + offsets", 18, BLUE, "700", "middle"))
            out.append(text(x + 82, 270, "snapshot reused", 18, GREEN, "700", "middle"))
        elif number == 2:
            for idx, height in enumerate((63, 52, 39, 27, 16)):
                hot = idx < 3
                out.append(rect(x + 14 + idx * 27, 221 - height, 16, height,
                                ORANGE_L if hot else GREY,
                                ORANGE if hot else GRID, 1.4, 1))
            out.append(line(x + 91, 151, x + 91, 228, RED, 2.0, dash="6 4"))
            out.append(text(x + 43, 246, "fN=3", 18, RED, "700", "middle"))
            out.append(text(x + 126, 246, "b=sq8", 18, INK, "700", "middle"))
            out.append(text(x + 43, 268, "R=32", 18, INK, "700", "middle"))
            out.append(text(x + 126, 268, "S=3", 18, INK, "700", "middle"))
        elif number == 3:
            out.append(text(x + 92, 160, "live degree = 3", 20, GREEN, "700", "middle"))
            record(
                out,
                x + 13,
                174,
                [30, 40, 40, 40],
                ["hdr", "id", "qvec", "rptr"],
                [PURPLE_L, BLUE_L, ORANGE_L, GREEN_L],
                39,
                20,
                [PURPLE, BLUE, ORANGE, GREEN],
            )
            out.append(text(x + 92, 255, "20 ranges | whole records", 18, PURPLE, "700", "middle"))
        elif number == 4:
            for owner, stripe_x in enumerate((x + 12, x + 64, x + 116)):
                out.append(rect(stripe_x, 157, 43, 86, WHITE, TEAL, 1.8, 4))
                out.append(text(stripe_x + 21, 178, f"MN{owner}", 20, TEAL, "700", "middle"))
                for row in range(3):
                    out.append(rect(stripe_x + 6, 188 + row * 16, 31, 12, GREEN_L, GREEN, 1.0, 1))
            out.append(text(x + 85, 263, "whole B[u]", 18, TEAL, "700", "middle"))
        elif number == 5:
            for row, record_id in enumerate((0, 3, 6)):
                y = 157 + row * 31
                out.append(rect(x + 13, y, 54, 23, GREEN_L, GREEN, 1.6, 3))
                out.append(centered(x + 13, y, 54, 23, f"B[{record_id}]", 20, INK, "700"))
                out.append(line(x + 70, y + 11, x + 145, y + 11, GREEN, 2.2, arrow="aGreen"))
            out.append(text(x + 85, 261, "data first", 18, GREEN, "700", "middle"))
        else:
            out.append('<g data-role="descriptor-commit">')
            out.append(rect(x + 12, 151, width - 24, 108, WHITE, PURPLE, 2.0, 5))
            fields = (
                ("magic | v2 | layout | code", BLUE_L, BLUE),
                ("N | S | policy | map shift", ORANGE_L, ORANGE),
                ("packed bytes | MN tables", GREEN_L, GREEN),
            )
            for row, (field, field_fill, field_stroke) in enumerate(fields):
                fy = 158 + row * 31
                out.append(rect(x + 19, fy, width - 38, 25, field_fill, field_stroke, 1.4, 2))
                out.append(text(x + 29, fy + 19, field, 18, INK, "700"))
            out.append("</g>")
        out.append("</g>")

    for (x, width, _, _, stroke), (next_x, _, _, _, _) in zip(stage_specs, stage_specs[1:]):
        mid = (x + width + next_x) / 2
        out.append(path(f"M{mid - 4},109 L{mid + 5},117 L{mid - 4},125 Z", stroke, 0, fill=stroke))
    out.append(path("M179,109 L188,117 L179,125 Z", NAVY, 0, fill=NAVY))

    # Offline replay control. Serving is explicitly gated for the whole row.
    out.append('<g data-role="service-gate">')
    box(out, 878, 311, 258, 31, "service paused", RED_L, RED, 20, rx=4, sw=2.0)
    out.append("</g>")

    out.append('<g data-role="authoritative-source" data-phase="replay">')
    out.append(rect(34, 350, 180, 158, WHITE, NAVY, 0, 0))
    out.append(line(34, 356, 34, 390, NAVY, 4.0))
    out.append(text(124, 369, "authoritative", 20, NAVY, "700", "middle"))
    out.append(text(124, 391, "HNSW", 20, NAVY, "700", "middle"))
    for idx in range(6):
        suffix = idx >= 4
        x = 44 + (idx % 3) * 54
        y = 400 + (idx // 3) * 43
        out.append(rect(x, y, 46, 30, ORANGE_L if suffix else BLUE_L,
                        ORANGE if suffix else BLUE, 1.7, 3))
        out.append(centered(x, y, 46, 30, f"L[{idx}]" + ("*" if suffix else ""), 20, INK, "700"))
    out.append(text(124, 496, "base | suffix", 18, MUTED, "700", "middle"))
    out.append("</g>")

    out.append('<g data-role="replay-suffix">')
    out.append(rect(228, 350, 170, 158, WHITE, ORANGE, 0, 0))
    out.append(line(228, 356, 228, 390, ORANGE, 4.0))
    out.append(text(313, 373, "replay suffix", 20, ORANGE, "700", "middle"))
    box(out, 244, 391, 138, 31, "new={4,5}", WHITE, ORANGE, 20, rx=4, sw=1.7)
    out.append(text(313, 447, "new + N0(new)", 18, INK, "700", "middle"))
    out.append(text(313, 479, "final L0 neighbors", 18, MUTED, "700", "middle"))
    out.append("</g>")

    out.append('<g data-role="touched-set">')
    out.append(rect(412, 350, 180, 158, WHITE, RED, 0, 0))
    out.append(line(412, 356, 412, 390, RED, 4.0))
    out.append(text(502, 373, "touched B[u]", 20, RED, "700", "middle"))
    for idx, record_id in enumerate((1, 3, 4, 5)):
        x = 426 + (idx % 2) * 78
        y = 393 + (idx // 2) * 47
        out.append(rect(x, y, 66, 31, RED_L, RED, 1.8, 4))
        out.append(centered(x, y, 66, 31, f"B[{record_id}]", 20, INK, "700"))
    out.append(text(502, 487, "fixed-stride only", 18, RED, "700", "middle"))
    out.append("</g>")

    out.append(rect(606, 350, 260, 158, WHITE, GREEN, 0, 0))
    out.append(line(606, 356, 606, 390, GREEN, 4.0))
    out.append(text(736, 373, "rewrite touched B[u]", 20, GREEN, "700", "middle"))
    for row, record_id in enumerate((1, 3, 4)):
        y = 385 + row * 31
        out.append(rect(620, y, 52, 25, RED_L, RED, 1.6, 3))
        out.append(centered(620, y, 52, 25, f"B[{record_id}]", 18, INK, "700"))
        out.append(line(678, y + 12, 708, y + 12, GREEN, 2.0, arrow="aGreen"))
        out.append(rect(715, y, 132, 25, GREEN_L, GREEN, 1.8, 3))
        out.append(centered(715, y, 132, 25, "pack + WRITE", 18, INK, "700"))
    out.append(text(736, 500, "same quantizer | in place", 18, MUTED, "700", "middle"))

    out.append('<g data-role="verification">')
    out.append(rect(880, 350, 230, 158, WHITE, PURPLE, 0, 0))
    out.append(line(880, 356, 880, 390, PURPLE, 4.0))
    out.append(text(995, 373, "full-region byte compare", 18, PURPLE, "700", "middle"))
    for idx in range(6):
        x = 896 + idx * 33
        out.append(rect(x, 397, 30, 31, GREEN_L, GREEN, 1.4, 2))
        out.append(centered(x, 397, 30, 31, str(idx), 20, INK, "700"))
    out.append(text(995, 454, "rewrite == fresh build", 18, GREEN, "700", "middle"))
    out.append(text(995, 486, "then admit queries", 18, NAVY, "700", "middle"))
    out.append("</g>")

    out.append(rect(1124, 350, 265, 158, WHITE, NAVY, 0, 0))
    out.append(line(1124, 356, 1124, 390, NAVY, 4.0))
    out.append(text(1256, 373, "measured control", 20, NAVY, "700", "middle"))
    controls = (
        "6.4-12.4 records/node",
        "read frac 1.2%-63.8%",
        "mismatches = 0",
        "recall = 0.97662",
    )
    for row, label in enumerate(controls):
        out.append(f'<g data-role="measured-control" data-row="{row + 1}">')
        cy = 398 + row * 25
        out.append(circle(1143, cy - 4, 7, GREEN_L, GREEN, 1.4))
        out.append(path(f"M1139,{cy - 4} L1143,{cy} L1148,{cy - 8}", GREEN, 1.8))
        out.append(text(1157, cy + 3, label, 18, INK, "700"))
        out.append("</g>")
    out.append(text(1256, 498, "varblock: full rebuild", 18, RED, "700", "middle"))

    panel_edges = ((214, 228), (398, 412), (592, 606), (866, 880), (1110, 1124))
    for x1, x2 in panel_edges:
        out.append(line(x1, 429, x2 - 4, 429, MUTED, 2.4, arrow="aMuted"))

    out.append("</svg>")
    return "\n".join(out)


def construction_refresh() -> str:
    out = canvas(1440, 550)
    out.append(text(720, 34, "Construction and offline refresh", 28, INK, "700", "middle"))
    out.append(line(908, 54, 908, 532, GRID, 1.4))

    # Left state: derive a self-describing structure.  The path bends once so
    # the figure can show concrete stage outputs without shrinking labels.
    out.append(line(24, 54, 24, 88, NAVY, 4.0))
    out.append(text(40, 81, "(a) full construction from authoritative HNSW", 23, NAVY, "700"))
    out.append('<g data-role="authoritative-source" data-phase="build">')
    out.append(rect(38, 108, 144, 160, BLUE_L, NAVY, 2.0, 5))
    out.append(text(110, 132, "authoritative", 20, NAVY, "700", "middle"))
    out.append(text(110, 155, "HNSW", 20, NAVY, "700", "middle"))
    graph(out, [(60, 196), (92, 174), (126, 196), (72, 232), (116, 234), (156, 212)], ((0, 1), (1, 2), (0, 3), (1, 4), (2, 5), (3, 4), (4, 5)), BLUE_L, NAVY, 6)
    out.append(text(110, 246, "topology\nunchanged", 18, NAVY, "700", "middle"))
    out.append("</g>")

    stages = (
        (204, 108, 188, 150, "1", "64 MiB scan", BLUE_L, BLUE),
        (414, 108, 202, 150, "2", "select fN", ORANGE_L, ORANGE),
        (638, 108, 244, 150, "3", "pack B[u] (20T)", PURPLE_L, PURPLE),
        (638, 302, 244, 164, "4", "owner u mod S", TEAL_L, TEAL),
        (414, 302, 202, 164, "5", "WRITE extents", GREEN_L, GREEN),
        (38, 302, 354, 164, "6", "write + verify descriptor", PURPLE_L, PURPLE),
    )
    for x, y, width, height, number, label, fill, stroke in stages:
        out.append(f'<g data-role="build-stage" data-stage="{number}">')
        out.append(line(x, y, x, y + 34, stroke, 4.0))
        out.append(circle(x + 18, y + 18, 13, stroke, stroke, 1.2))
        out.append(text(x + 18, y + 25, number, 20, WHITE, "700", "middle"))
        out.append(text(x + 40, y + 26, label, 20, stroke, "700"))

        if number == "1":
            graph(out, [(x + 24, y + 80), (x + 56, y + 58), (x + 92, y + 80), (x + 36, y + 116), (x + 84, y + 118)], ((0, 1), (1, 2), (0, 3), (1, 4), (3, 4)), BLUE_L, BLUE, 5)
            for index, bar_h in enumerate((66, 50, 34, 20)):
                out.append(rect(x + 122 + index * 14, y + 128 - bar_h, 10, bar_h, BLUE_L, BLUE, 1.1, 1))
            out.append(text(x + width / 2, y + 143, "validate + offsets", 18, BLUE, "700", "middle"))
        elif number == "2":
            for index, bar_h in enumerate((72, 60, 46, 30, 18)):
                hot = index < 3
                out.append(rect(x + 20 + index * 31, y + 124 - bar_h, 20, bar_h, ORANGE_L if hot else GREY, ORANGE if hot else GRID, 1.3, 1))
            out.append(line(x + 113, y + 48, x + 113, y + 132, RED, 1.8, dash="6 4"))
            out.append(text(x + 62, y + 145, "fN=3   R=32", 18, INK, "700", "middle"))
            out.append(text(x + 158, y + 145, "b=sq8", 18, INK, "700", "middle"))
        elif number == "3":
            out.append(text(x + 122, y + 56, "live degree = 3", 18, GREEN, "700", "middle"))
            record(out, x + 28, y + 70, [42, 54, 68, 56], ["hdr", "id", "qvec", "rptr"], [PURPLE_L, BLUE_L, ORANGE_L, GREEN_L], 46, 18, [PURPLE, BLUE, ORANGE, GREEN])
            out.append(text(x + 122, y + 140, "20 ranges | whole records", 18, PURPLE, "700", "middle"))
        elif number == "4":
            for owner, stripe_x in enumerate((x + 26, x + 96, x + 166)):
                out.append(rect(stripe_x, y + 48, 56, 92, WHITE, TEAL, 1.7, 3))
                out.append(text(stripe_x + 28, y + 70, f"MN{owner}", 18, TEAL, "700", "middle"))
                for row in range(3):
                    out.append(rect(stripe_x + 8, y + 80 + row * 18, 40, 13, GREEN_L, GREEN, 1.0, 1))
            out.append(text(x + 122, y + 158, "each B[u] stays intact", 18, TEAL, "700", "middle"))
        elif number == "5":
            for row, record_id in enumerate((0, 3, 6)):
                yy = y + 50 + row * 34
                box(out, x + 28, yy, 64, 26, f"B[{record_id}]", GREEN_L, GREEN, 18, rx=2, sw=1.5)
                out.append(line(x + 100, yy + 13, x + 170, yy + 13, GREEN, 2.0, "aGreen"))
            out.append(text(x + width / 2, y + 158, "data before metadata", 18, GREEN, "700", "middle"))
        else:
            out.append('<g data-role="descriptor-commit">')
            fields = (
                ("magic | v2 | layout | code", BLUE_L, BLUE),
                ("N | S | policy | map shift", ORANGE_L, ORANGE),
                ("packed bytes | MN tables", GREEN_L, GREEN),
            )
            for row, (field, field_fill, field_stroke) in enumerate(fields):
                yy = y + 48 + row * 34
                out.append(rect(x + 24, yy, width - 48, 27, field_fill, field_stroke, 1.4, 2))
                out.append(text(x + 36, yy + 20, field, 18, INK, "700"))
            out.append(text(x + width / 2, y + 158, "read back before serving", 18, PURPLE, "700", "middle"))
            out.append("</g>")
        out.append("</g>")

    out.append(path("M184,178 H200", NAVY, 2.3, arrow="aNavy"))
    out.append(path("M394,178 H410", BLUE, 2.3, arrow="aBlue"))
    out.append(path("M618,178 H634", ORANGE, 2.3, arrow="aOrange"))
    out.append(path("M760,260 V298", PURPLE, 2.3, arrow="aPurple"))
    out.append(path("M634,384 H620", TEAL, 2.3, arrow="aTeal"))
    out.append(path("M410,384 H396", GREEN, 2.3, arrow="aGreen"))

    # Right state: a deliberately narrower offline replay control.  It shows
    # the before/after record sets and keeps the service gate visible.
    out.append(line(930, 54, 930, 88, GREEN, 4.0))
    out.append(text(946, 81, "(b) fixed-stride offline replay control", 23, GREEN, "700"))
    out.append('<g data-role="service-gate">')
    box(out, 1198, 96, 204, 34, "service paused", RED_L, RED, 20, rx=4, sw=1.8)
    out.append("</g>")

    out.append('<g data-role="authoritative-source" data-phase="replay">')
    out.append(line(946, 126, 946, 158, NAVY, 4.0))
    out.append(text(1037, 126, "authoritative\nHNSW", 20, NAVY, "700", "middle"))
    for index in range(6):
        suffix = index >= 4
        x = 950 + (index % 3) * 58
        y = 158 + (index // 3) * 42
        out.append(rect(x, y, 50, 30, ORANGE_L if suffix else BLUE_L, ORANGE if suffix else BLUE, 1.6, 3))
        out.append(centered(x, y, 50, 30, f"L[{index}]" + ("*" if suffix else ""), 18, INK, "700"))
    out.append(text(1037, 246, "base + suffix", 18, MUTED, "700", "middle"))
    out.append("</g>")

    out.append('<g data-role="replay-suffix">')
    out.append(line(1138, 126, 1138, 158, ORANGE, 4.0))
    out.append(text(1215, 148, "replay suffix", 20, ORANGE, "700", "middle"))
    box(out, 1154, 158, 122, 34, "new={4,5}", WHITE, ORANGE, 18, rx=3, sw=1.6)
    out.append(text(1215, 219, "new + N0(new)", 18, INK, "700", "middle"))
    out.append(text(1215, 246, "recomputed L0", 18, MUTED, "700", "middle"))
    out.append("</g>")

    out.append('<g data-role="touched-set">')
    out.append(line(1294, 126, 1294, 158, RED, 4.0))
    out.append(text(1350, 148, "touched B[u]", 20, RED, "700", "middle"))
    for index, record_id in enumerate((1, 3, 4, 5)):
        x = 1308 + (index % 2) * 58
        y = 158 + (index // 2) * 42
        box(out, x, y, 50, 30, f"B[{record_id}]", RED_L, RED, 18, rx=3, sw=1.5)
    out.append(text(1358, 246, "rewrite set", 18, RED, "700", "middle"))
    out.append("</g>")

    out.append(line(944, 272, 1404, 272, GRID, 1.2))
    out.append(text(960, 304, "rewrite touched B[u]", 20, GREEN, "700"))
    for row, record_id in enumerate((1, 3, 4)):
        y = 318 + row * 34
        box(out, 960, y, 56, 26, f"B[{record_id}]", RED_L, RED, 18, rx=2, sw=1.4)
        out.append(line(1020, y + 13, 1046, y + 13, GREEN, 2.0, "aGreen"))
        box(out, 1050, y, 122, 26, "pack + WRITE", GREEN_L, GREEN, 18, rx=2, sw=1.5)

    out.append('<g data-role="verification">')
    out.append(text(1196, 304, "full-region byte compare", 20, PURPLE, "700"))
    for index in range(6):
        x = 1198 + index * 33
        out.append(rect(x, 320, 30, 31, GREEN_L, GREEN, 1.3, 2))
        out.append(centered(x, 320, 30, 31, str(index), 18, INK, "700"))
    out.append(text(1296, 378, "rewrite == fresh build", 18, GREEN, "700", "middle"))
    out.append(text(1296, 404, "then admit queries", 18, NAVY, "700", "middle"))
    out.append("</g>")

    out.append(line(944, 424, 1404, 424, GRID, 1.2))
    out.append(text(960, 453, "measured control", 20, NAVY, "700"))
    controls = ("6.4-12.4 records/node", "read frac 1.2%-63.8%", "mismatches = 0", "recall = 0.97662")
    for index, label in enumerate(controls):
        x = 960 + (index % 2) * 224
        y = 482 + (index // 2) * 31
        out.append(f'<g data-role="measured-control" data-row="{index + 1}">')
        out.append(circle(x + 7, y - 5, 7, GREEN_L, GREEN, 1.3))
        out.append(path(f"M{x + 3},{y - 5} L{x + 7},{y - 1} L{x + 12},{y - 10}", GREEN, 1.6))
        out.append(text(x + 20, y + 2, label, 18, INK, "700"))
        out.append("</g>")
    out.append(text(1398, 540, "varblock: full rebuild", 18, RED, "700", "end"))

    out.append("</svg>")
    return "\n".join(out)


def search_placement() -> str:
    out = canvas(1440, 570)
    out.append(text(300, 34, "One-read search and placement", 28, INK, "700", "middle"))
    for x, width, label, fill in (
        (816, 120, "D = 0", GREEN),
        (946, 120, "c = 1", BLUE),
        (1076, 144, "R bounded", PURPLE),
        (1230, 180, "owners striped", TEAL),
    ):
        chip(out, x, 8, width, 38, label, fill, 20)

    # Resident state ends at the level-0 boundary; the exact upper descent is
    # drawn as a compact graph rather than another pipeline box.
    out.append(line(24, 56, 24, 88, GREEN, 4.0))
    out.append(text(40, 62, "(a) resident\nupper graph", 23, GREEN, "700"))
    out.append('<g data-role="resident-boundary">')
    out.append(rect(40, 102, 244, 208, GREEN_L, GREEN, 2.0, 5))
    out.append(text(60, 130, "resident upper graph", 20, GREEN, "700"))
    out.append(text(60, 160, "exact fp32 descent", 22, NAVY, "700"))
    upper = {34: (166, 196), 66: (104, 252), 92: (232, 252)}
    for left, right in ((34, 66), (34, 92), (66, 92)):
        out.append(line(*upper[left], *upper[right], GREEN, 2.0))
    for identifier, (x, y) in upper.items():
        node(out, x, y, str(identifier), WHITE, GREEN, 14, 20)
    out.append(text(60, 204, "L2", 20, GREEN, "700"))
    out.append(text(60, 263, "L1", 20, GREEN, "700"))
    out.append("</g>")
    elbow(out, [(232, 252), (232, 334), (176, 334), (176, 342)], GREEN, 2.2)
    out.append(text(60, 298, "exact local seed", 20, GREEN, "700"))
    out.append('<g data-role="level0-seed">')
    out.append(rect(40, 344, 244, 84, WHITE, BLUE, 1.8, 4, "6 4"))
    out.append(text(58, 374, "remote level 0", 22, BLUE, "700"))
    out.append(text(58, 406, "not resident", 20, MUTED, "700"))
    out.append(text(228, 390, "u=42", 20, BLUE, "700", "middle"))
    out.append("</g>")
    box(out, 40, 446, 244, 42, "D: 0 remote READs", GREEN_L, GREEN, 22, rx=4, sw=2.0)
    out.append(text(40, 516, "replicated per CN\nscales with upper graph", 20, MUTED, "700"))

    # Main query timeline.  RDMA and CN work occupy separate lanes, making the
    # one-read overlap and exact tail explicit.
    out.append(line(310, 56, 310, 88, NAVY, 4.0))
    out.append(text(326, 81, "(b) one-read expansion", 23, NAVY, "700"))
    out.append(text(326, 116, "CN beam", 20, MUTED, "700"))
    out.append(text(326, 232, "record", 20, MUTED, "700"))
    out.append(text(326, 358, "exact tail", 20, MUTED, "700"))
    out.append(line(398, 122, 1046, 122, GRID, 1.0))
    out.append(line(398, 238, 1046, 238, GRID, 1.0))
    out.append(line(398, 364, 1046, 364, GRID, 1.0))

    box(out, 334, 136, 82, 54, "pop u", GREEN_L, GREEN, 22, rx=4, sw=2.0)
    out.append(text(375, 184, "u=42", 20, GREEN, "700", "middle"))
    out.append(line(416, 163, 428, 163, PURPLE, 2.4, "aPurple"))
    box(out, 430, 136, 104, 54, "prefix[u]", PURPLE_L, PURPLE, 22, rx=4, sw=2.0)
    out.append(line(534, 163, 546, 163, GREEN, 2.4, "aGreen"))
    box(out, 548, 136, 110, 54, "READ B[u]", GREEN_L, GREEN, 22, rx=4, sw=2.0)
    out.append(line(658, 163, 670, 163, ORANGE, 2.4, "aOrange"))
    out.append('<g data-role="approx-stage">')
    out.append(rect(672, 136, 132, 54, ORANGE_L, ORANGE, 2.2, 4))
    out.append(centered(672, 136, 132, 54, "approx. SIMD\nscore", 22, INK, "700"))
    out.append("</g>")
    out.append(line(804, 163, 816, 163, GREEN, 2.4, "aGreen"))
    box(out, 818, 136, 126, 54, "beam\nupdate", GREEN_L, GREEN, 22, rx=4, sw=2.0)
    out.append(text(680, 108, "one global HNSW beam", 22, NAVY, "700", "middle"))
    out.append(
        '<polyline data-role="beam-loop" points="881,136 881,94 375,94 375,136" '
        f'fill="none" stroke="{GREEN}" stroke-width="2.4" stroke-linejoin="round" marker-end="url(#aGreen)"/>'
    )
    out.append(text(852, 220, "done", 20, PURPLE, "700"))

    out.append(text(438, 260, "d(u) live entries", 20, ORANGE, "700"))
    box(out, 334, 272, 92, 102, "B[42]\nd(u)=3", GREEN_L, GREEN, 20, rx=4, sw=2.0)
    for row, identifier in enumerate((23, 57, 81)):
        y = 272 + row * 34
        out.append(f'<g data-role="slab-entry" data-id="{identifier}">')
        record(out, 438, y, [48, 86, 92], [str(identifier), "qvec", "rptr"], [TEAL_L, ORANGE_L, PURPLE_L], 32, 20, [TEAL, ORANGE, PURPLE])
        out.append("</g>")
    box(out, 438, 382, 132, 42, "qvec:\napproximate", ORANGE_L, ORANGE, 20, rx=3, sw=1.8)
    out.append('<g data-role="rptr-legend">')
    box(out, 580, 382, 142, 42, "rptr:\nfp32 address", PURPLE_L, PURPLE, 20, rx=3, sw=1.8)
    out.append("</g>")

    out.append('<g data-role="topr-stage">')
    out.append(rect(724, 250, 270, 58, PURPLE_L, PURPLE, 2.0, 4))
    out.append(text(859, 275, "top-R survivors", 22, PURPLE, "700", "middle"))
    out.append(text(859, 300, "{23,57} + rptr", 20, INK, "700", "middle"))
    out.append("</g>")
    out.append(
        '<line data-role="beam-exit" data-from="beam update" data-to="top-R survivors" '
        f'x1="881" y1="190" x2="881" y2="250" stroke="{PURPLE}" stroke-width="2.4" marker-end="url(#aPurple)"/>'
    )
    out.append(
        '<line data-role="rerank-read-edge" data-from="top-R survivors" data-to="READ authoritative fp32" '
        f'x1="859" y1="308" x2="859" y2="326" stroke="{PURPLE}" stroke-width="2.4" marker-end="url(#aPurple)"/>'
    )
    box(out, 740, 330, 238, 50, "READ authoritative fp32", BLUE_L, BLUE, 22, rx=4, sw=2.0)
    out.append(
        '<line data-role="exact-rerank-edge" data-from="READ authoritative fp32" data-to="exact rerank" '
        f'x1="859" y1="380" x2="859" y2="396" stroke="{BLUE}" stroke-width="2.4" marker-end="url(#aBlue)"/>'
    )
    out.append('<g data-role="exact-stage">')
    out.append(rect(774, 400, 170, 44, BLUE_L, BLUE, 2.2, 4))
    out.append(centered(774, 400, 170, 44, "exact rerank", 22, INK, "700"))
    out.append("</g>")
    out.append(line(944, 422, 960, 422, GREEN, 2.4, "aGreen"))
    out.append(circle(984, 422, 23, GREEN_L, GREEN, 2.2))
    out.append(text(984, 429, "top-k", 20, GREEN, "700", "middle"))

    out.append('<g data-role="cold-fallback">')
    out.append(rect(326, 462, 696, 82, WHITE, RED, 1.8, 4, "7 5"))
    out.append(text(340, 486, "cold fallback", 20, RED, "700"))
    out.append(text(490, 486, "authoritative list/vector path", 20, BLUE, "700"))
    box(out, 500, 496, 136, 34, "missing B[96]", WHITE, RED, 20, rx=3, sw=1.6)
    out.append(line(636, 513, 652, 513, RED, 2.0, "aRed", "7 5"))
    box(out, 656, 496, 72, 34, "L[96]", BLUE_L, BLUE, 20, rx=3, sw=1.6)
    out.append(line(728, 513, 744, 513, RED, 2.0, "aRed", "7 5"))
    record(out, 748, 496, [78, 78], ["V[34]", "V[66]"], [BLUE_L, BLUE_L], 34, 20, [BLUE, BLUE])
    box(out, 920, 494, 90, 42, "return to\nbeam", WHITE, RED, 20, rx=3, sw=1.6)
    out.append(
        '<polyline data-role="fallback-rejoin" data-from="cold fallback" data-to="beam update" '
        'points="965,494 1036,494 1036,163 944,163" fill="none" '
        f'stroke="{RED}" stroke-width="2.0" stroke-dasharray="7 5" stroke-linejoin="round" marker-end="url(#aRed)"/>'
    )
    out.append("</g>")

    # Right: direct owner reads over intact, block-cyclic records.
    out.append(line(1050, 56, 1050, 88, TEAL, 4.0))
    out.append(text(1066, 81, "(c) block-cyclic placement", 23, TEAL, "700"))
    out.append(text(1066, 112, "owner(u) = u mod S", 22, TEAL, "700"))
    out.append(text(1406, 112, "S=3", 20, TEAL, "700", "end"))
    out.append(text(1066, 140, "no router", 22, NAVY, "700"))

    workers = ((1068, 1, "MN1", "CN-A"), (1238, 2, "MN2", "CN-B"))
    for x, record_id, owner, label in workers:
        out.append(f'<g data-role="cn-worker" data-record="{record_id}" data-owner="{owner}">')
        out.append(rect(x, 154, 150, 54, GREEN_L, GREEN, 2.0, 4))
        out.append(text(x + 75, 176, label, 20, INK, "700", "middle"))
        out.append(text(x + 75, 200, f"READ B[{record_id}]", 20, INK, "700", "middle"))
        out.append("</g>")

    stripe_x = (1066, 1178, 1290)
    owner_records = ((0, 3, 6, 9), (1, 4, 7, 10), (2, 5, 8, 11))
    for owner, (x, identifiers) in enumerate(zip(stripe_x, owner_records)):
        out.append(rect(x, 236, 100, 228, WHITE, TEAL, 1.8, 4))
        out.append(text(x + 50, 263, f"MN{owner}", 20, TEAL, "700", "middle"))
        for row, identifier in enumerate(identifiers):
            y = 278 + row * 42
            out.append(f'<g data-role="placement-record" data-record="{identifier}" data-owner="MN{owner}">')
            out.append(rect(x + 8, y, 84, 31, GREEN_L, GREEN, 1.7, 3))
            out.append(centered(x + 8, y, 84, 31, f"B[{identifier}]", 20, INK, "700"))
            out.append("</g>")

    out.append(
        '<polyline data-role="direct-read" data-record="1" data-owner="MN1" '
        'points="1218,181 1228,181 1228,293 1270,293" fill="none" '
        f'stroke="{GREEN}" stroke-width="2.2" stroke-linejoin="round" marker-end="url(#aGreen)"/>'
    )
    out.append(
        '<polyline data-role="direct-read" data-record="2" data-owner="MN2" '
        'points="1388,181 1402,181 1402,293 1382,293" fill="none" '
        f'stroke="{GREEN}" stroke-width="2.2" stroke-linejoin="round" marker-end="url(#aGreen)"/>'
    )
    box(out, 1066, 480, 324, 48, "each B[u] intact\nno split / no inter-MN assembly", TEAL_L, TEAL, 20, rx=4, sw=1.8)
    out.append("</svg>")
    return "\n".join(out)


def slab_layout() -> str:
    out = canvas(1440, 570)
    out.append(text(720, 34, "Expansion-oriented physical layout", 28, INK, "700", "middle"))

    # Top left: the static budget is a rank cut, not a runtime cache.
    out.append(line(26, 54, 26, 86, NAVY, 4.0))
    out.append(text(42, 79, "(a) degree-bounded materialization", 23, NAVY, "700"))
    out.append(text(44, 112, "in-degree rank", 22, ORANGE, "700"))
    out.append(line(44, 123, 420, 123, ORANGE, 2.4, "aOrange"))
    out.append(text(48, 220, "IDs", 20, MUTED, "700"))
    bars = ((42, 72), (57, 60), (81, 48), (96, 27), (34, 18))
    for index, (identifier, height) in enumerate(bars):
        x = 84 + index * 68
        hot = index < 3
        out.append(rect(x, 202 - height, 44, height, ORANGE_L if hot else GREY, ORANGE if hot else GRID, 1.5, 1))
        out.append(text(x + 22, 220, str(identifier), 20, INK, "700", "middle"))
    out.append(line(288, 126, 288, 226, RED, 2.0, dash="6 4"))
    out.append(text(300, 146, "fN", 22, RED, "700"))
    out.append(text(48, 250, "hot prefix", 22, GREEN, "700"))
    out.append(text(44, 276, "Slabs", 22, GREEN, "700"))
    for x, label in zip((116, 214, 312), ("B[42]", "B[57]", "B[81]")):
        box(out, x, 236, 88, 42, label, GREEN_L, GREEN, 20, rx=3, sw=1.8)
    box(out, 44, 290, 164, 42, "build-time policy", BLUE_L, NAVY, 20, rx=3, sw=1.8)
    box(out, 216, 290, 204, 42, "static budget map\nrecorded in descriptor", PURPLE_L, PURPLE, 20, rx=3, sw=1.8)

    # Top right: serialize consecutive live-prefix records into the remote
    # region and zoom one record below.
    out.append(line(454, 54, 454, 86, GREEN, 4.0))
    out.append(text(470, 79, "(b) live-prefix records", 23, GREEN, "700"))
    out.append(text(470, 111, "fixed capacity 5: B[42], d(42)=3, live IDs {23,57,81}", 22, MUTED, "700"))
    record(out, 470, 124, [100, 106, 106, 106, 106, 106], ["B[42]", "23", "57", "81", "pad", "pad"], [GREEN_L, TEAL_L, TEAL_L, TEAL_L, RED_L, RED_L], 42, 20, [GREEN, TEAL, TEAL, TEAL, RED, RED])
    out.append(path("M894,171 V184 H1106 V171", RED, 2.0))
    out.append(text(1000, 205, "padding removed", 20, RED, "700", "middle"))

    out.append(text(470, 234, "variable d(u): 3 live entries", 22, GREEN, "700"))
    record(out, 470, 246, [100, 106, 106, 106], ["B[42]", "23", "57", "81"], [GREEN_L, TEAL_L, TEAL_L, TEAL_L], 42, 20, [GREEN, TEAL, TEAL, TEAL])
    box(out, 916, 246, 270, 42, "pay for d(u), not capacity", GREEN_L, GREEN, 22, rx=4, sw=2.0)

    out.append(text(1210, 111, "serialized remote region", 20, PURPLE, "700", "middle"))
    record(out, 1198, 124, [68, 68, 68], ["B[42]", "B[57]", "B[81]"], [GREEN_L, GREEN_L, GREEN_L], 42, 20, [GREEN, GREEN, GREEN])
    out.append(line(1232, 166, 1232, 206, GREEN, 1.8, "aGreen"))
    out.append(line(1300, 166, 1300, 206, GREEN, 1.8, "aGreen"))
    out.append(line(1368, 166, 1368, 206, GREEN, 1.8, "aGreen"))
    out.append(text(1288, 229, "prefix -> contiguous B[u]", 20, PURPLE, "700", "middle"))
    box(out, 1198, 246, 204, 42, "authoritative fallback", RED_L, RED, 20, rx=4, sw=1.8)

    out.append(line(24, 350, 1416, 350, GRID, 1.2))

    # Bottom left: code selection is an offline guard on the materialized
    # payload; it is not part of the per-query path.
    out.append(line(26, 360, 26, 392, ORANGE, 4.0))
    out.append(text(42, 385, "(c) configured scoring code", 23, ORANGE, "700"))
    box(out, 44, 398, 354, 34, "offline configuration", BLUE_L, NAVY, 22, rx=3, sw=1.8)
    box(out, 44, 438, 354, 34, "metric + dimension guard", ORANGE_L, ORANGE, 22, rx=3, sw=1.8)
    box(out, 44, 478, 112, 30, "sq8", BLUE_L, BLUE, 21, rx=3, sw=1.8)
    out.append(text(172, 501, "low-d L2", 20, BLUE, "700"))
    box(out, 44, 512, 144, 30, "RaBitQ-2", GREEN_L, GREEN, 20, rx=3, sw=1.8)
    out.append(text(204, 535, "960d L2", 20, GREEN, "700"))
    out.append(text(44, 556, "evaluated TTI inner-product boundary", 22, RED, "700"))

    # Bottom right: canonical bytes.  IDs label the logical neighbor; the
    # tuple itself stores the next-hop slot, fp32 rerank pointer, and code.
    out.append(line(430, 360, 430, 392, PURPLE, 4.0))
    out.append(text(446, 385, "(d) canonical B[u] byte layout", 23, PURPLE, "700"))
    box(out, 448, 398, 126, 36, "prefix[42]", BLUE_L, BLUE, 20, rx=3, sw=1.8)
    out.append(line(576, 416, 596, 416, BLUE, 2.2, "aBlue"))
    box(out, 598, 398, 138, 36, "offset, length", WHITE, BLUE, 20, rx=3, sw=1.8)
    out.append(line(738, 416, 758, 416, BLUE, 2.2, "aBlue"))
    box(out, 760, 398, 94, 36, "B[42]", GREEN_L, GREEN, 20, rx=3, sw=1.8)
    out.append(line(856, 416, 876, 416, GREEN, 2.2, "aGreen"))
    out.append(text(1120, 382, "header", 22, INK, "700", "middle"))
    record(out, 878, 398, [142, 116, 116], ["count=3", "bits", "flags"], [GREY, GREY, GREY], 36, 20, [INK, INK, INK])

    out.append(text(860, 454, "next hop", 22, BLUE, "700", "middle"))
    out.append(text(1060, 454, "exact rerank", 22, PURPLE, "700", "middle"))
    out.append(text(1260, 454, "approx. SIMD score", 22, ORANGE, "700", "middle"))
    for row, identifier in enumerate((23, 57, 81)):
        y = 484 + row * 38
        out.append(text(486, y, str(identifier), 20, INK, "700", "middle"))
        out.append(text(534, y, "<", 20, INK, "700", "middle"))
        out.append(text(1390, y, ">", 20, INK, "700", "middle"))
        out.append(rect(548, y - 25, 624, 30, BLUE_L, BLUE, 1.2, 0))
        out.append(rect(948, y - 25, 224, 30, PURPLE_L, PURPLE, 1.2, 0))
        out.append(rect(1172, y - 25, 176, 30, ORANGE_L, ORANGE, 1.2, 0))
        # Re-emit the field labels after their fills so they remain visible.
        out.append(text(860, y, "nbr_slot", 20, BLUE, "700", "middle"))
        out.append(text(1060, y, "nbr_rptr", 20, PURPLE, "700", "middle"))
        out.append(text(1260, y, "nbr_qvec", 20, ORANGE, "700", "middle"))

    out.append("</svg>")
    return "\n".join(out)


def overview() -> str:
    out = canvas(1440, 610)
    out.append(text(720, 34, "SlabWalk end-to-end", 28, INK, "700", "middle"))

    # Ownership stays in a narrow left rail.  The main body uses two aligned
    # lanes so every online operation points to the physical object it reads.
    ownership = (
        (24, 62, 166, 98, "clients", PALE, NAVY),
        (24, 178, 166, 146, "cn-local", GREEN_L, GREEN),
        (24, 342, 166, 226, "mn-resident", WHITE, GREEN),
    )
    for x, y, width, height, owner, fill, stroke in ownership:
        shape = rect(x, y, width, height, fill, stroke, 1.5, 0)
        out.append(shape.replace("<rect ", f'<rect data-role="ownership-zone" data-owner="{owner}" ', 1))
        out.append(line(x, y + 8, x, y + height - 8, stroke, 4.0))

    out.append(text(107, 88, "Clients", 20, NAVY, "700", "middle"))
    for center_x in (68, 107, 146):
        out.append(circle(center_x, 115, 8, BLUE_L, NAVY, 1.8))
        out.append(path(f"M{center_x - 12},143 Q{center_x},126 {center_x + 12},143", NAVY, 2.1))
    out.append(line(107, 151, 107, 173, GREEN, 2.4, "aGreen"))

    out.append(text(107, 204, "CN-local state", 20, GREEN, "700", "middle"))
    for offset in (10, 5, 0):
        out.append(rect(48 + offset, 224 - offset, 112, 76, WHITE, GREEN, 1.8, 4))
    cpoints = ((70, 250), (102, 235), (137, 250), (84, 278), (122, 278))
    graph(out, cpoints, ((0, 1), (1, 2), (0, 3), (1, 3), (1, 4), (2, 4), (3, 4)), WHITE, GREEN, 5)
    for x in (62, 94, 126):
        out.append(rect(x, 287, 24, 8, WHITE, GREEN, 1.2, 0))

    out.append(text(107, 370, "MN-resident", 20, GREEN, "700", "middle"))
    for offset in (10, 5, 0):
        out.append(rect(48 + offset, 392 - offset, 112, 102, WHITE, GREEN, 1.8, 4))
    for y, fill, stroke in ((410, GREEN_L, GREEN), (436, ORANGE_L, ORANGE), (462, PURPLE_L, PURPLE)):
        out.append(rect(61, y, 86, 16, fill, stroke, 1.5, 1))
    out.append(text(107, 514, "registered bytes\nonly", 20, GREEN, "700", "middle"))

    # Search lane.
    out.append(rect(210, 62, 1192, 240, PALE, NAVY, 1.3, 0))
    out.append(line(210, 62, 210, 104, NAVY, 4.0))
    out.append(text(226, 90, "online query", 20, NAVY, "700"))

    out.append(circle(245, 176, 22, BLUE_L, NAVY, 2.4))
    out.append(text(245, 184, "q", 22, NAVY, "700", "middle"))
    elbow(out, [(190, 116), (204, 116), (204, 176), (220, 176)], NAVY, 2.4)

    out.append(rect(286, 105, 214, 116, GREEN_L, GREEN, 2.0, 5))
    out.append(text(393, 126, "resident upper graph", 20, GREEN, "700", "middle"))
    upper = {34: (375, 155), 66: (332, 193), 92: (446, 193)}
    for left, right in ((34, 66), (34, 92), (66, 92)):
        out.append(line(*upper[left], *upper[right], GREEN, 1.8))
    for identifier, (x, y) in upper.items():
        node(out, x, y, str(identifier), WHITE, GREEN, 13, 20)
    out.append(text(305, 163, "L2", 20, GREEN, "700"))
    out.append(text(305, 202, "L1", 20, GREEN, "700"))
    elbow(out, [(500, 193), (520, 193), (520, 236), (548, 236)], GREEN, 2.4)
    out.append(text(480, 260, "level-0 seed", 20, GREEN, "700", "middle"))

    box(out, 550, 218, 94, 40, "u=42", GREEN_L, GREEN, 20, rx=4, sw=2.0)
    out.append(line(644, 238, 664, 238, PURPLE, 2.4, "aPurple"))
    out.append(text(722, 112, "CN address", 20, PURPLE, "700", "middle"))
    record(out, 664, 126, [100, 42, 46], ["prefix[42]", "off", "len"], [PURPLE_L, WHITE, WHITE], 38, 20, [PURPLE, PURPLE, PURPLE])
    elbow(out, [(758, 164), (758, 188), (786, 188)], PURPLE, 2.2)
    box(out, 786, 168, 100, 40, "MN0\noff,len", PURPLE_L, PURPLE, 20, rx=4, sw=2.0)
    out.append(line(886, 188, 910, 188, GREEN, 2.6, "aGreen"))
    box(out, 912, 166, 132, 44, "READ B[42]", GREEN_L, GREEN, 20, rx=4, sw=2.2)

    out.append(text(1118, 117, "SIMD scores", 20, ORANGE, "700", "middle"))
    for row, (identifier, score) in enumerate(((23, ".18"), (57, ".24"), (81, ".31"))):
        record(out, 1060, 132 + row * 34, [58, 58], [str(identifier), score], [ORANGE_L, WHITE], 32, 20, [ORANGE, ORANGE])
    out.append(line(1176, 183, 1196, 183, GREEN, 2.4, "aGreen"))
    box(out, 1198, 151, 110, 64, "beam update\n23 57 81", GREEN_L, GREEN, 20, rx=4, sw=2.0)
    out.append(line(1308, 183, 1324, 183, PURPLE, 2.4, "aPurple"))
    box(out, 1314, 143, 70, 80, "top-R\nrptr\nreads", PURPLE_L, PURPLE, 20, rx=4, sw=2.0)
    out.append(path("M1254,151 Q1260,100 996,100 Q948,100 948,164", GREEN, 2.2, arrow="aGreen"))
    out.append(text(1124, 95, "next candidate", 20, GREEN, "700", "middle"))
    out.append(circle(1360, 258, 24, GREEN_L, GREEN, 2.2))
    out.append(text(1360, 266, "top-k", 20, GREEN, "700", "middle"))
    elbow(out, [(1355, 223), (1355, 232), (1360, 232)], GREEN, 2.2)

    # Physical-organization lane, aligned with the online objects above.
    out.append(rect(210, 320, 1192, 216, WHITE, NAVY, 1.3, 0))
    out.append(line(210, 320, 210, 362, NAVY, 4.0))
    out.append(text(226, 348, "PHYSICAL ORGANIZATION", 20, NAVY, "700"))

    out.append(text(230, 382, "MN: authoritative HNSW", 20, BLUE, "700"))
    record(out, 230, 398, [76, 64, 64, 64], ["L[42]", "23", "57", "81"], [BLUE_L, WHITE, WHITE, WHITE], 42, 20, [BLUE, BLUE, BLUE, BLUE])
    record(out, 286, 458, [76, 64, 64], ["L[96]", "34", "66"], [BLUE_L, WHITE, WHITE], 38, 20, [BLUE, BLUE, BLUE])
    elbow(out, [(244, 198), (244, 396)], BLUE, 2.0)

    out.append(text(540, 382, "CN: prefix tables", 20, PURPLE, "700"))
    for offset in (12, 6, 0):
        out.append(rect(540 + offset, 398 - offset, 204, 62, WHITE, PURPLE, 1.8, 4))
    record(out, 548, 408, [92, 48, 48], ["prefix[42]", "off", "len"], [PURPLE_L, WHITE, WHITE], 38, 20, [PURPLE, PURPLE, PURPLE])
    elbow(out, [(712, 164), (712, 395)], PURPLE, 2.0)

    out.append(text(786, 382, "MN: Slab regions", 20, GREEN, "700"))
    record(out, 786, 398, [48, 58, 40, 108, 58, 46], ["MN0", "B[42]", "hdr", "23,57,81", "codes", "rptr"], [TEAL_L, GREEN_L, GREY, GREEN_L, ORANGE_L, PURPLE_L], 42, 20, [TEAL, GREEN, INK, GREEN, ORANGE, PURPLE])
    record(out, 786, 448, [48, 58, 40, 108, 58, 46], ["MN1", "B[57]", "hdr", "42,81", "codes", "rptr"], [TEAL_L, GREEN_L, GREY, GREEN_L, ORANGE_L, PURPLE_L], 38, 20, [TEAL, GREEN, INK, GREEN, ORANGE, PURPLE])
    elbow(out, [(978, 210), (978, 395)], GREEN, 2.0)

    out.append(text(1180, 382, "MN: fp32 vectors", 20, BLUE, "700"))
    for x, label in zip((1150, 1226, 1302), ("V[23]", "V[57]", "V[81]")):
        box(out, x, 398, 68, 50, label + "\nfp32", BLUE_L, BLUE, 20, rx=4, sw=1.8)
    box(out, 1150, 466, 98, 32, "descriptor", PURPLE_L, PURPLE, 20, rx=3, sw=1.8)
    box(out, 1260, 466, 58, 32, "V[34]", BLUE_L, BLUE, 20, rx=2, sw=1.6)
    box(out, 1324, 466, 58, 32, "V[66]", BLUE_L, BLUE, 20, rx=2, sw=1.6)
    elbow(out, [(1355, 223), (1355, 392)], PURPLE, 2.0)

    # Cold records remain reachable through the authoritative path.
    out.append(rect(508, 502, 504, 26, WHITE, RED, 1.6, 2, "6 4"))
    out.append(text(520, 522, "cold fallback", 20, RED, "700"))
    out.append(text(856, 522, "missing B[96] -> L[96] + V[34],V[66]", 20, RED, "700", "middle"))

    # Offline derivation is a thin footer, not a third architecture panel.
    out.append(line(210, 554, 1402, 554, GRID, 1.2))
    stages = ((246, "1", "scan"), (442, "2", "rank"), (638, "3", "encode"), (834, "4", "pack"), (1030, "5", "write"), (1226, "6", "verify descriptor"))
    for index, (x, number, label) in enumerate(stages):
        out.append(circle(x, 580, 14, ORANGE_L, ORANGE, 2.0))
        out.append(text(x, 587, number, 20, ORANGE, "700", "middle"))
        out.append(text(x + 24, 587, label, 20, INK, "700"))
        if index < len(stages) - 1:
            next_x = stages[index + 1][0]
            out.append(line(x + 92, 580, next_x - 20, 580, ORANGE, 2.0, "aOrange"))

    out.append("</svg>")
    return "\n".join(out)


def physical_units() -> str:
    (
        cache_ratios,
        cache_posts,
        cache_posts_ci,
        cache_qps,
        cache_qps_ci,
        useful,
    ) = measured_cache_control()
    post_reduction = 100.0 * (1.0 - cache_posts[-1] / cache_posts[0])
    qps_loss = 100.0 * (1.0 - cache_qps[-1] / cache_qps[0])

    out = canvas(1440, 540)
    out.append(text(720, 34, "Physical units for one HNSW expansion", 28, INK, "700", "middle"))

    # A shared matrix makes the logical decision, retrieval path, and stored
    # object directly comparable.  The three columns repeat the same u=42
    # expansion instead of presenting three unrelated architectures.
    out.append(rect(24, 88, 820, 182, PALE, GRID, 1.2, 0))
    out.append(rect(24, 286, 820, 142, WHITE, GRID, 1.2, 0))
    out.append(text(34, 110, "SEARCH PATH", 18, MUTED, "700"))
    out.append(text(34, 308, "REMOTE OBJECT", 18, MUTED, "700"))
    out.append(text(34, 451, "COST VECTOR", 18, MUTED, "700"))
    out.append(line(24, 438, 844, 438, GRID, 1.2))

    columns = (
        (48, 244, "Node / vector", RED, RED_L),
        (312, 244, "Expansion", GREEN, GREEN_L),
        (576, 244, "Routed partition", ORANGE, ORANGE_L),
    )
    for x, width, label, color, tint in columns:
        out.append(line(x - 10, 62, x - 10, 80, color, 4.0))
        out.append(text(x, 78, label, 22, color, "700"))
        out.append(text(x + width / 2, 132, "u=42   N={23,57,81}", 20, color, "700", "middle"))
        out.append(line(x + width + 10, 88, x + width + 10, 428, GRID, 1.0))

    # Node/vector: the graph decision is fragmented into one list and three
    # separately addressed vectors.
    box(out, 52, 150, 118, 34, "READ L[42]", WHITE, RED, 20, rx=4, sw=2.0)
    out.append(line(172, 167, 196, 167, RED, 2.6, "aRed"))
    box(out, 200, 150, 70, 34, "IDs", RED_L, RED, 20, rx=4, sw=2.0)
    out.append(text(170, 211, "3 vector READs", 20, RED, "700", "middle"))
    for x, label in zip((54, 128, 202), ("V[23]", "V[57]", "V[81]")):
        box(out, x, 222, 66, 34, label, RED_L, RED, 20, rx=3, sw=1.9)
    for x, label in zip((50, 108, 166, 224), ("L[42]", "V[23]", "V[57]", "V[81]")):
        box(out, x, 330, 54, 42, label, RED_L, RED, 18, rx=2, sw=1.8)
        out.append(line(x + 27, 372, x + 27, 390, RED, 1.8))
    box(out, 88, 390, 164, 28, "4 posted READs", WHITE, RED, 18, rx=3, sw=1.8)

    # Expansion: one contiguous record returns every dependency needed to
    # score the current neighborhood and select the next beam state.
    box(out, 326, 151, 216, 56, "READ B[42]\n<hdr, IDs, codes, rptr>", GREEN_L, GREEN, 20, rx=4, sw=2.2)
    out.append(line(434, 207, 434, 218, GREEN, 2.4, "aGreen"))
    box(out, 346, 220, 176, 36, "in-place score", WHITE, GREEN, 20, rx=4, sw=1.9)
    out.append(text(434, 282, "authoritative topology", 18, PURPLE, "700", "middle"))
    out.append(text(324, 326, "B[42]", 20, GREEN, "700"))
    record(
        out,
        324,
        336,
        [38, 92, 54, 44],
        ["hdr", "IDs\n23,57,81", "codes", "rptr"],
        [GREY, BLUE_L, ORANGE_L, PURPLE_L],
        60,
        18,
        [INK, BLUE, ORANGE, PURPLE],
    )
    box(out, 360, 400, 150, 24, "1 posted READ", WHITE, GREEN, 18, rx=3, sw=1.8)

    # Partition: route once, fetch a serialized subgraph, then make a local
    # decision inside the routed coverage.
    box(out, 590, 150, 106, 34, "route/meta", WHITE, ORANGE, 19, rx=4, sw=1.9)
    out.append(line(698, 167, 728, 167, ORANGE, 2.6, "aOrange"))
    box(out, 732, 150, 72, 34, "p3", ORANGE_L, ORANGE, 20, rx=4, sw=1.9)
    box(out, 610, 195, 174, 32, "serialized p3", ORANGE_L, ORANGE, 19, rx=3, sw=1.8)
    out.append(line(697, 227, 697, 236, ORANGE, 2.2, "aOrange"))
    box(out, 586, 238, 110, 30, "deserialize", WHITE, ORANGE, 18, rx=3, sw=1.8)
    box(out, 708, 234, 108, 38, "local\nsub-HNSW", ORANGE_L, ORANGE, 18, rx=3, sw=1.8)
    out.append(rect(590, 320, 220, 100, WHITE, ORANGE, 1.8, 3, "6 4"))
    out.append(text(600, 342, "p3", 19, ORANGE, "700"))
    pnodes = {42: (696, 368), 23: (650, 342), 57: (752, 344), 81: (752, 397), 36: (614, 384), 68: (660, 404), 94: (792, 380)}
    pedges = ((42, 23), (42, 57), (42, 81), (42, 68), (23, 36), (23, 68), (57, 81), (57, 94), (81, 68), (81, 94))
    for left, right in pedges:
        out.append(line(*pnodes[left], *pnodes[right], ORANGE, 1.5))
    for identifier, (x, y) in pnodes.items():
        node(out, x, y, str(identifier), ORANGE_L if identifier in {42, 23, 57, 81} else WHITE, ORANGE, 12, 18)

    cost_rows = (
        (("ops 4", "state beam/cache", "payload L+3V", "step global"), RED),
        (("ops 1", "state beam", "payload B42", "step global"), GREEN),
        (("route+fetch", "state route+p3", "payload p3", "step local"), ORANGE),
    )
    for (x, width, _, _, _), (metrics, color) in zip(columns, cost_rows):
        for index, metric in enumerate(metrics):
            cy = 468 + index * 20
            out.append(circle(x + 5, cy - 5, 4, WHITE, color, 1.8))
            out.append(text(x + 16, cy, metric, 18, color, "700"))

    # Same-binary cache control: fewer remote posts do not help when cache
    # bookkeeping replaces completions on the same per-node access path.
    out.append(line(866, 62, 866, 92, NAVY, 4.0))
    out.append(text(882, 82, "Measured motivation: same path, more cache", 21, NAVY, "700"))
    box(out, 888, 104, 242, 48, f"{post_reduction:.1f}% fewer posts/query", BLUE_L, BLUE, 18, rx=5, sw=2.0)
    box(out, 1150, 104, 242, 48, f"{qps_loss:.1f}% lower QPS", RED_L, RED, 18, rx=5, sw=2.0)

    out.append(text(1140, 180, "cache budget (%)", 18, NAVY, "700", "middle"))
    out.append(text(1392, 180, "n=5; 95% CI", 18, MUTED, "600", "end"))
    plot_x = (940, 1080, 1220, 1360)
    normalized_posts = tuple(100.0 * value / cache_posts[0] for value in cache_posts)
    normalized_posts_ci = tuple(100.0 * value / cache_posts[0] for value in cache_posts_ci)
    normalized_qps = tuple(100.0 * value / cache_qps[0] for value in cache_qps)
    normalized_qps_ci = tuple(100.0 * value / cache_qps[0] for value in cache_qps_ci)
    plot_y = lambda value: 205.0 + (100.0 - value) * 0.86
    out.append(line(918, 205, 1382, 205, GRID, 1.2, dash="5 4"))
    out.append(line(918, 291, 1382, 291, GRID, 1.0))
    out.append(text(910, 211, "100%", 18, MUTED, "600", "end"))
    out.append(text(910, 297, "0", 18, MUTED, "600", "end"))
    for values, ci_values, color in ((normalized_posts, normalized_posts_ci, BLUE), (normalized_qps, normalized_qps_ci, RED)):
        ys = tuple(plot_y(value) for value in values)
        for index in range(3):
            out.append(line(plot_x[index], ys[index], plot_x[index + 1], ys[index + 1], color, 2.8))
        for ratio, px, py, value, ci in zip(cache_ratios, plot_x, ys, values, ci_values):
            top = plot_y(value + ci)
            bottom = plot_y(value - ci)
            out.append(line(px, top, px, bottom, color, 1.3))
            out.append(line(px - 4, top, px + 4, top, color, 1.3))
            out.append(line(px - 4, bottom, px + 4, bottom, color, 1.3))
            out.append(circle(px, py, 5, WHITE, color, 2.2))
            out.append(text(px, 316, str(ratio), 18, INK, "600", "middle"))
    out.append(line(948, 341, 978, 341, BLUE, 2.8))
    out.append(circle(963, 341, 4.5, WHITE, BLUE, 1.9))
    out.append(text(988, 347, "posts remaining", 18, BLUE, "700"))
    out.append(line(1170, 341, 1200, 341, RED, 2.8))
    out.append(circle(1185, 341, 4.5, WHITE, RED, 1.9))
    out.append(text(1210, 347, "QPS retained", 18, RED, "700"))

    out.append(text(888, 394, "CN self-time (200K queries, 3,000 samples)", 18, NAVY, "700"))
    out.append(rect(888, 408, 504, 20, GREY, GRID, 1.0, 3))
    out.append(rect(888, 408, 504 * useful / 100.0, 20, BLUE, BLUE, 0, 3))
    out.append(text(888, 454, f"useful distance: {useful:.1f}%", 18, BLUE, "700"))
    out.append(text(1392, 454, f"other CN work: {100.0 - useful:.1f}%", 18, MUTED, "700", "end"))
    out.append(text(888, 492, "Caching changes reuse; it does not change the retrieval unit.", 19, INK, "700"))
    out.append("</svg>")
    return "\n".join(out)


GENERATORS = {
    "fig_physical_units": physical_units,
    "overview": overview,
    "fig_slab_layout": slab_layout,
    "fig_search_placement": search_placement,
    "fig_construction_refresh": construction_refresh,
}


def normalize_pdf(source_pdf: Path, normalized_pdf: Path) -> None:
    try:
        from pypdf import PdfReader, PdfWriter
    except ImportError as exc:
        raise RuntimeError(
            "pypdf is required to normalize generated PDF metadata"
        ) from exc

    with source_pdf.open("rb") as source:
        reader = PdfReader(source)
        writer = PdfWriter()
        writer.clone_document_from_reader(reader)
        # Inkscape adds a transparency page group even when the figure has no
        # transparency. Removing it is rendering-neutral and prevents pdfTeX's
        # "multiple pdfs with page group" warning when two figures share a page.
        for page in writer.pages:
            if "/Group" in page:
                del page["/Group"]
        metadata = {
            str(key): str(value)
            for key, value in (reader.metadata or {}).items()
            if value is not None
        }
        metadata.update(
            {
                "/Producer": "SlabWalk deterministic figure generator",
                "/CreationDate": "D:20000101000000Z",
                "/ModDate": "D:20000101000000Z",
            }
        )
        writer.add_metadata(metadata)
        with normalized_pdf.open("wb") as target:
            writer.write(target)

    with normalized_pdf.open("rb") as normalized:
        if any("/Group" in page for page in PdfReader(normalized).pages):
            raise RuntimeError(
                "generated publication PDF still contains a page group"
            )


def verify_publication_fonts(pdf_path: Path) -> None:
    pdffonts = shutil.which("pdffonts")
    if not pdffonts:
        raise RuntimeError(
            "pdffonts is required to verify publication PDFs are Type-3-free"
        )

    result = subprocess.run(
        [pdffonts, str(pdf_path)],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        suffix = f": {detail}" if detail else ""
        raise RuntimeError(f"pdffonts failed while checking publication PDF{suffix}")

    lines = [line.strip() for line in result.stdout.splitlines()[2:] if line.strip()]
    if not lines:
        raise RuntimeError(
            "publication PDFs must retain searchable text with embedded fonts"
        )

    type_3_lines = [line for line in lines if _TYPE_3_FONT.search(line)]
    if type_3_lines:
        details = " | ".join(type_3_lines)
        raise RuntimeError(
            f"Type 3 fonts are not allowed in publication PDFs: {details}"
        )

    unembedded = []
    for line in lines:
        columns = line.split()
        if len(columns) < 5:
            raise RuntimeError(f"cannot parse pdffonts row: {line}")
        # The final five fields are emb/sub/uni/object-ID/generation regardless
        # of whether the font type itself occupies one or two columns.
        if columns[-5].lower() != "yes":
            unembedded.append(line)
    if unembedded:
        details = " | ".join(unembedded)
        raise RuntimeError(
            f"publication PDF contains unembedded fonts: {details}"
        )


def sibling_pdf_temp(pdf_path: Path, stage: str) -> Path:
    temporary = tempfile.NamedTemporaryFile(
        prefix=f".{pdf_path.stem}.{stage}.",
        suffix=".tmp.pdf",
        dir=pdf_path.parent,
        delete=False,
    )
    temporary.close()
    return Path(temporary.name)


def svg_renderer_mode() -> str:
    mode = os.environ.get("SLABWALK_SVG_RENDERER", "auto").strip().lower()
    if mode not in {"auto", "inkscape", "rsvg"}:
        raise ValueError(
            "SLABWALK_SVG_RENDERER must be one of: auto, inkscape, rsvg"
        )
    return mode


def render(svg_path: Path) -> None:
    pdf_path = svg_path.with_suffix(".pdf")
    rendered_pdf = None
    normalized_pdf = None
    raw_pdf = None
    try:
        rendered_pdf = sibling_pdf_temp(pdf_path, "render")
        normalized_pdf = sibling_pdf_temp(pdf_path, "normalized")
        renderer_mode = svg_renderer_mode()
        inkscape = (
            shutil.which("inkscape")
            if renderer_mode in {"auto", "inkscape"}
            else None
        )
        if renderer_mode == "inkscape" and not inkscape:
            raise RuntimeError("SLABWALK_SVG_RENDERER=inkscape but Inkscape is unavailable")
        if inkscape:
            # Keep labels as text. Current Inkscape embeds the resolved
            # TrueType/CID subsets; the font gate below rejects unsafe output.
            subprocess.run(
                [
                    inkscape,
                    "--export-type=pdf",
                    f"--export-filename={rendered_pdf}",
                    str(svg_path),
                ],
                check=True,
            )
        else:
            rsvg = shutil.which("rsvg-convert")
            if not rsvg:
                raise RuntimeError("Inkscape or rsvg-convert is required")
            gs = shutil.which("gs")
            if gs:
                raw_pdf = sibling_pdf_temp(pdf_path, "rsvg")
                subprocess.run(
                    [rsvg, "-f", "pdf", "-o", str(raw_pdf), str(svg_path)],
                    check=True,
                )
                subprocess.run(
                    [
                        gs,
                        "-q",
                        "-dNOPAUSE",
                        "-dBATCH",
                        "-sDEVICE=pdfwrite",
                        "-dCompatibilityLevel=1.5",
                        "-dPDFSETTINGS=/prepress",
                        f"-sOutputFile={rendered_pdf}",
                        str(raw_pdf),
                    ],
                    check=True,
                )
            else:
                subprocess.run(
                    [rsvg, "-f", "pdf", "-o", str(rendered_pdf), str(svg_path)],
                    check=True,
                )

        normalize_pdf(rendered_pdf, normalized_pdf)
        verify_publication_fonts(normalized_pdf)
        normalized_pdf.replace(pdf_path)
    finally:
        for temporary in (raw_pdf, rendered_pdf, normalized_pdf):
            if temporary is not None and temporary.exists():
                temporary.unlink()


def main() -> None:
    global CACHE_CSV, PROFILE_CSV
    args = parse_args()
    CACHE_CSV = args.cache_summary
    PROFILE_CSV = args.profile_summary
    args.output_dir.mkdir(parents=True, exist_ok=True)
    names = (args.only,) if args.only else GENERATORS
    for name in names:
        generator = GENERATORS[name]
        svg = generator()
        validate_svg(name, svg)
        svg_path = args.output_dir / f"{name}.svg"
        svg_path.write_text(svg, encoding="utf-8")
        if args.svg_only:
            print(f"wrote {svg_path.name}")
        else:
            render(svg_path)
            print(f"wrote {svg_path.name} and {svg_path.with_suffix('.pdf').name}")


if __name__ == "__main__":
    main()
