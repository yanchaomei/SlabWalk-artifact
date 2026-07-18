#!/usr/bin/env python3
"""Build and verify the minimal deterministic PVLDB Overleaf source archive."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

import publish_vldb_release as release
import verify_publication_pdf as pdf_verifier


FIXED_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
CORE_FILES = (
    "ACM-Reference-Format.bst",
    "acmart.cls",
    "generated_claims.tex",
    "main.tex",
    "pvldb.sty",
    "refs.bib",
)
GRAPHICS_RE = re.compile(
    r"\\includegraphics(?:\s*\[[^\]]*\])?\s*\{([^{}]+)\}"
)
GENERATED_CLAIMS_INPUT_RE = re.compile(
    r"\\input\s*\{\s*generated_claims\.tex\s*\}"
)
PREAMBLE_CONDITIONAL_RE = re.compile(
    r"\\(?:if[A-Za-z@]*|else|fi)(?![A-Za-z@])"
)
PREAMBLE_GROUP_RE = re.compile(
    r"\\(?:begingroup|endgroup|bgroup|egroup)(?![A-Za-z@])"
)
PRIMITIVE_CONDITIONAL_RE = re.compile(
    r"\\(?:if|ifcat|ifnum|ifdim|ifodd|ifvmode|ifhmode|ifmmode|ifinner|"
    r"ifvoid|ifhbox|ifvbox|ifx|ifeof|iftrue|iffalse|ifcase|ifdefined|"
    r"ifcsname|iffontchar|unless|else|or|fi)(?![A-Za-z@])"
)
COMMAND_DEFINITION_RE = re.compile(
    r"\\(?:newcommand|renewcommand|providecommand|DeclareRobustCommand|"
    r"NewDocumentCommand|RenewDocumentCommand|ProvideDocumentCommand|"
    r"DeclareDocumentCommand|DeclareMathOperator)\*?\s*\{?\s*\\([A-Za-z@]+)"
)
PRIMITIVE_DEFINITION_RE = re.compile(
    r"\\(?:def|gdef|edef|xdef|let|futurelet|chardef|mathchardef|"
    r"countdef|dimendef|skipdef|muskipdef|toksdef)\s*\\([A-Za-z@]+)"
)
DYNAMIC_CONTROL_RE = re.compile(
    r"\\(?:csname|catcode|escapechar|endlinechar|scantokens|ExplSyntaxOn|"
    r"uppercase|lowercase)(?![A-Za-z@])|\^\^"
)
HIDDEN_ENVIRONMENT_RE = re.compile(
    r"\\begin\s*\{\s*(?:comment|verbatim\*?|Verbatim|lstlisting|minted|"
    r"filecontents\*?|lrbox)\s*\}"
)
ENVIRONMENT_CONTROL_RE = re.compile(
    r"\\(?:excludecomment|includecomment|newenvironment|renewenvironment|"
    r"provideenvironment|NewDocumentEnvironment|RenewDocumentEnvironment|"
    r"ProvideDocumentEnvironment|DeclareDocumentEnvironment)\*?(?![A-Za-z@])"
)
PROTECTED_COMMANDS = frozenset({
    "begin",
    "documentclass",
    "end",
    "includegraphics",
    "input",
    "newcommand",
    "figure",
    "endfigure",
})


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def uncommented_tex_text(text: str) -> str:
    lines: list[str] = []
    for line in text.splitlines():
        comment_at: int | None = None
        for index, character in enumerate(line):
            if character != "%":
                continue
            backslashes = 0
            cursor = index - 1
            while cursor >= 0 and line[cursor] == "\\":
                backslashes += 1
                cursor -= 1
            if backslashes % 2 == 0:
                comment_at = index
                break
        lines.append(line if comment_at is None else line[:comment_at])
    return "\n".join(lines)


def uncommented_tex(path: Path) -> str:
    return uncommented_tex_text(path.read_text())


def brace_depths(tex: str) -> tuple[int, ...]:
    depths: list[int] = []
    depth = 0
    for index, character in enumerate(tex):
        depths.append(depth)
        if character not in "{}":
            continue
        backslashes = 0
        cursor = index - 1
        while cursor >= 0 and tex[cursor] == "\\":
            backslashes += 1
            cursor -= 1
        if backslashes % 2:
            continue
        if character == "{":
            depth += 1
        else:
            depth -= 1
            if depth < 0:
                raise ValueError("main.tex has an unmatched closing brace")
    depths.append(depth)
    if depth != 0:
        raise ValueError("main.tex has unbalanced braces")
    return tuple(depths)


def validate_restricted_tex(tex: str) -> tuple[str, tuple[int, ...]]:
    uncommented = uncommented_tex_text(tex)
    if DYNAMIC_CONTROL_RE.search(uncommented):
        raise ValueError("main.tex contains a forbidden dynamic control sequence")
    if HIDDEN_ENVIRONMENT_RE.search(uncommented):
        raise ValueError("main.tex contains a content-hiding environment")
    if ENVIRONMENT_CONTROL_RE.search(uncommented):
        raise ValueError("main.tex contains a forbidden environment control")
    if PRIMITIVE_CONDITIONAL_RE.search(uncommented):
        raise ValueError("main.tex contains a primitive conditional")
    for pattern in (COMMAND_DEFINITION_RE, PRIMITIVE_DEFINITION_RE):
        for match in pattern.finditer(uncommented):
            command = match.group(1)
            if command.startswith("Claim"):
                raise ValueError("main.tex contains a generated claim macro override")
            if command in PROTECTED_COMMANDS:
                raise ValueError(
                    f"main.tex contains a protected command redefinition: {command}"
                )
    return uncommented, brace_depths(uncommented)


def referenced_figures_from_tex(tex: str) -> tuple[str, ...]:
    tex, depths = validate_restricted_tex(tex)
    figures = set()
    matches = list(GRAPHICS_RE.finditer(tex))
    if any(depths[match.start()] != 0 for match in matches):
        raise ValueError("every submission figure must be a top-level figure reference")
    for match in matches:
        raw = match.group(1)
        relative = PurePosixPath(raw.strip())
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or not relative.parts
            or relative.parts[0] != "figs"
        ):
            raise ValueError(f"figure path must be portable and under figs/: {raw}")
        if relative.suffix.lower() != ".pdf":
            raise ValueError(f"submission figures must be explicit PDFs: {raw}")
        figures.add(relative.as_posix())
    if len(matches) != 9 or len(figures) != 9:
        raise ValueError(
            f"submission must reference exactly nine unique PDF figures, got {len(figures)}"
        )
    return tuple(sorted(figures))


def referenced_figures(paper_dir: Path) -> tuple[str, ...]:
    return referenced_figures_from_tex((paper_dir / "main.tex").read_text())


def validate_generated_claims_usage(main_tex: str) -> None:
    uncommented, depths = validate_restricted_tex(main_tex)
    document_marker = r"\begin{document}"
    if uncommented.count(document_marker) != 1:
        raise ValueError("main.tex must contain exactly one document boundary")
    preamble, _ = uncommented.split(document_marker, 1)
    if PREAMBLE_CONDITIONAL_RE.search(preamble):
        raise ValueError("generated claims input must not be inside a conditional preamble")
    if PREAMBLE_GROUP_RE.search(preamble):
        raise ValueError("generated claims input must not be inside a grouped preamble")
    matches = list(GENERATED_CLAIMS_INPUT_RE.finditer(uncommented))
    if len(matches) != 1 or matches[0].start() >= len(preamble):
        raise ValueError(
            "main.tex must contain exactly one active preamble input of generated_claims.tex"
        )
    match = matches[0]
    line_start = uncommented.rfind("\n", 0, match.start()) + 1
    line_end = uncommented.find("\n", match.end())
    if line_end < 0:
        line_end = len(uncommented)
    if (
        depths[match.start()] != 0
        or uncommented[line_start:line_end].strip()
        != r"\input{generated_claims.tex}"
    ):
        raise ValueError(
            "generated_claims.tex must be loaded by one standalone top-level preamble input"
        )


def member_names(main_tex: str) -> tuple[str, ...]:
    validate_generated_claims_usage(main_tex)
    members = tuple(sorted((*CORE_FILES, *referenced_figures_from_tex(main_tex))))
    if len(members) != 15:
        raise ValueError(f"submission archive must contain 15 files, got {len(members)}")
    return members


def submission_members(paper_dir: Path) -> tuple[str, ...]:
    members = member_names((paper_dir / "main.tex").read_text())
    for relative in members:
        source = paper_dir / relative
        if source.is_symlink() or not source.is_file() or source.stat().st_size == 0:
            raise ValueError(f"missing, empty, or symbolic submission source: {source}")
    return members


def submission_snapshot(
    paper_dir: Path,
    release_entries: dict[str, dict[str, Any]],
    release_snapshots: dict[str, bytes],
    paper_prefix: str,
) -> tuple[tuple[str, ...], dict[str, bytes]]:
    main_target = f"{paper_prefix}/main.tex"
    main_bytes = release_snapshots.get(main_target)
    if not main_bytes:
        raise ValueError("main.tex is missing from the verified release snapshot")
    main_tex = main_bytes.decode()
    members = member_names(main_tex)
    snapshots: dict[str, bytes] = {}
    for relative in members:
        target = f"{paper_prefix}/{relative}"
        data = release_snapshots.get(target)
        if not data:
            raise ValueError(
                f"submission member is missing from the verified release snapshot: {relative}"
            )
        snapshots[relative] = data

    for relative, data in snapshots.items():
        target = f"{paper_prefix}/{relative}"
        record = release_entries.get(target)
        if (
            not isinstance(record, dict)
            or record.get("size_bytes") != len(data)
            or record.get("sha256") != sha256_bytes(data)
        ):
            raise ValueError(f"submission member is not release-bound: {relative}")
    return members, snapshots


def zip_info(relative: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(relative, date_time=FIXED_TIMESTAMP)
    info.create_system = 3
    info.external_attr = (stat.S_IFREG | 0o644) << 16
    info.compress_type = zipfile.ZIP_DEFLATED
    info.extra = b""
    info.comment = b""
    return info


def verify_package(
    archive_path: Path,
    expected_members: tuple[str, ...],
    expected_bytes: dict[str, bytes],
) -> None:
    with zipfile.ZipFile(archive_path) as archive:
        infos = archive.infolist()
        if tuple(info.filename for info in infos) != expected_members:
            raise ValueError("Overleaf archive member order or inventory mismatch")
        if archive.testzip() is not None:
            raise ValueError("Overleaf archive CRC verification failed")
        for info in infos:
            if (
                info.date_time != FIXED_TIMESTAMP
                or info.create_system != 3
                or info.external_attr >> 16 != stat.S_IFREG | 0o644
                or info.extra != b""
                or info.compress_type != zipfile.ZIP_DEFLATED
            ):
                raise ValueError(f"non-deterministic ZIP metadata: {info.filename}")
            if archive.read(info) != expected_bytes[info.filename]:
                raise ValueError(f"archive/snapshot byte mismatch: {info.filename}")


def verify_snapshot_pdfs(
    members: tuple[str, ...], snapshots: dict[str, bytes]
) -> None:
    pdf_members = [
        name for name in members if PurePosixPath(name).suffix.lower() == ".pdf"
    ]
    with tempfile.TemporaryDirectory(prefix="vldb-package-pdf-") as tmp:
        root = Path(tmp)
        for index, relative in enumerate(pdf_members):
            path = root / f"figure_{index}.pdf"
            path.write_bytes(snapshots[relative])
            try:
                pdf_verifier.verify(path)
            except Exception as exc:
                raise ValueError(
                    f"publication PDF verification failed: {relative}"
                ) from exc


def build_package(
    paper_dir: Path,
    output: Path,
    *,
    release_manifest: Path,
    force: bool = False,
) -> dict[str, Any]:
    paper_dir = paper_dir.resolve()
    if output.is_symlink():
        raise ValueError(f"output must not be a symbolic link: {output}")
    output = output.resolve(strict=False)
    if not paper_dir.is_dir():
        raise ValueError(f"paper directory does not exist: {paper_dir}")
    repo_root = paper_dir.parent
    if release_manifest.is_symlink() or not release_manifest.is_file():
        raise ValueError(f"missing regular release manifest: {release_manifest}")
    release_manifest = release_manifest.resolve(strict=True)
    manifest_bytes = release_manifest.read_bytes()
    _, release_snapshots = release.capture_release_snapshot(
        repo_root, manifest_bytes
    )
    manifest = json.loads(manifest_bytes)
    release_entries = manifest["entries"]
    protected_paths = {release_manifest}
    protected_paths.update(
        release.target_path(repo_root, target).resolve(strict=False)
        for target in release_entries
    )
    if output in protected_paths:
        raise ValueError(f"output aliases a protected release input: {output}")
    paper_prefix = paper_dir.relative_to(repo_root).as_posix()
    members, snapshots = submission_snapshot(
        paper_dir, release_entries, release_snapshots, paper_prefix
    )
    expected_pdf_targets = {
        f"{paper_prefix}/{relative}"
        for relative in members
        if PurePosixPath(relative).suffix.lower() == ".pdf"
    }
    if set(manifest.get("publication_pdf_targets", [])) != expected_pdf_targets:
        raise ValueError(
            "release manifest publication PDF targets do not match the submission figures"
        )
    verify_snapshot_pdfs(members, snapshots)
    if output.exists() and not force:
        raise ValueError(f"output already exists (use --force): {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.parent / f".{output.name}.tmp.{os.getpid()}"
    if temporary.exists():
        raise ValueError(f"temporary package path already exists: {temporary}")
    try:
        with zipfile.ZipFile(
            temporary,
            mode="x",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
        ) as archive:
            for relative in members:
                archive.writestr(
                    zip_info(relative),
                    snapshots[relative],
                    compress_type=zipfile.ZIP_DEFLATED,
                    compresslevel=9,
                )
        verify_package(temporary, members, snapshots)
        os.replace(temporary, output)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return {
        "archive": str(output),
        "bytes": output.stat().st_size,
        "sha256": sha256(output),
        "members": list(members),
        "release_manifest_sha256": sha256_bytes(manifest_bytes),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--paper-dir", type=Path, required=True)
    parser.add_argument("--release-manifest", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    report = build_package(
        args.paper_dir,
        args.out,
        release_manifest=args.release_manifest,
        force=args.force,
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
