#!/usr/bin/env python3
"""Fail closed when a generated paper figure is not publication-safe."""

from __future__ import annotations

import argparse
import math
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from pypdf import PdfReader


FONT_TAIL = re.compile(
    r"\s+(?P<embedded>yes|no)\s+(?P<subset>yes|no)\s+"
    r"(?P<unicode>yes|no)\s+\d+\s+\d+\s*$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class PdfStructure:
    pages: int
    width_points: float
    height_points: float


@dataclass(frozen=True)
class FontRecord:
    name: str
    embedded: bool
    subset: bool
    unicode_map: bool


def verify_pdf_structure(path: Path) -> PdfStructure:
    if not path.is_file() or path.stat().st_size == 0:
        raise ValueError(f"missing or empty PDF: {path}")
    reader = PdfReader(path)
    if reader.is_encrypted:
        raise ValueError(f"publication figure must not be encrypted: {path}")
    pages = len(reader.pages)
    if pages != 1:
        raise ValueError(f"publication figure must contain exactly one page: {path}")
    box = reader.pages[0].mediabox
    width = float(box.width)
    height = float(box.height)
    if not all(math.isfinite(value) and value > 0 for value in (width, height)):
        raise ValueError(f"publication figure has an invalid media box: {path}")
    if width <= height:
        raise ValueError(f"publication figure must use a landscape page: {path}")
    return PdfStructure(pages=pages, width_points=width, height_points=height)


def parse_pdffonts(output: str) -> list[FontRecord]:
    records: list[FontRecord] = []
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if (
            not line
            or line.lower().startswith("name ")
            or all(character in "- " for character in line)
        ):
            continue
        match = FONT_TAIL.search(line)
        if match is None:
            raise ValueError(f"unable to parse pdffonts row: {line}")
        prefix = line[: match.start()].strip()
        if re.search(r"\bType\s+3\b", prefix, re.IGNORECASE):
            raise ValueError(f"Type 3 font is not allowed: {line}")
        embedded = match.group("embedded").lower() == "yes"
        if not embedded:
            raise ValueError(f"font is not embedded: {line}")
        records.append(
            FontRecord(
                name=prefix.split()[0],
                embedded=embedded,
                subset=match.group("subset").lower() == "yes",
                unicode_map=match.group("unicode").lower() == "yes",
            )
        )
    if not records:
        raise ValueError("pdffonts reported no fonts")
    return records


def verify_fonts(path: Path) -> list[FontRecord]:
    executable = shutil.which("pdffonts")
    if executable is None:
        raise ValueError("pdffonts is required to validate publication figures")
    result = subprocess.run(
        [executable, str(path)],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise ValueError(f"pdffonts failed for {path}: {detail}")
    return parse_pdffonts(result.stdout)


def verify(path: Path) -> tuple[PdfStructure, list[FontRecord]]:
    return verify_pdf_structure(path), verify_fonts(path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf", type=Path, nargs="+")
    args = parser.parse_args()
    for path in args.pdf:
        structure, fonts = verify(path)
        print(
            f"verified {path}: pages={structure.pages} "
            f"size={structure.width_points:.3f}x{structure.height_points:.3f}pt "
            f"embedded_fonts={len(fonts)}"
        )


if __name__ == "__main__":
    main()
