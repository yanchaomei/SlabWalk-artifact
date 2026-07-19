#!/usr/bin/env python3
"""Build a deterministic, allowlisted SlabWalk public-artifact tree."""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import re
import shutil
import tempfile
from pathlib import Path, PurePosixPath
from typing import Iterable

import vldb_evidence_bundle as evidence_bundle
import seal_vldb_multicn_campaign as multicn_seal


ROOT_FILES = {
    ".gitignore": ".gitignore",
    "PUBLIC_ARTIFACT_README.md": "README.md",
    "ARTIFACT.md": "ARTIFACT.md",
    "requirements.txt": "requirements.txt",
    "graphbeyond/LICENSE": "LICENSE",
}

TREE_ROOTS = (
    "graphbeyond",
    "experiments/sigmetrics",
    "experiments/tools",
    "results/vldb_final_evidence",
)

OPTIONAL_SEALED_MULTICN_ROOTS = (
    "results/vldb_multicn_formal_20260719f",
)

PAPER_FILES = (
    "paper_vldb/main.tex",
    "paper_vldb/refs.bib",
    "paper_vldb/acmart.cls",
    "paper_vldb/pvldb.sty",
    "paper_vldb/ACM-Reference-Format.bst",
    "paper_vldb/generated_claims.tex",
    "paper_vldb/CLAIM_EVIDENCE_LEDGER.md",
    "paper_vldb/PVLDB_SCOPE_SELF_ASSESSMENT.md",
    "paper_vldb/figs/gen_vldb_design_figures.py",
)

IGNORED_DIRECTORY_NAMES = {
    ".git",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
    "build",
}

IGNORED_FILE_NAMES = {".DS_Store"}
IGNORED_SUFFIXES = {".a", ".dylib", ".o", ".pyc", ".so"}

SENSITIVE_BYTE_PATTERNS = (
    re.compile(rb"-----BEGIN (?:OPENSSH|RSA|EC|DSA) PRIVATE KEY-----"),
    re.compile(rb"(?:ghp|gho|ghu|ghs|github_pat)_[A-Za-z0-9_]{20,}"),
    re.compile(rb"AKIA[0-9A-Z]{16}"),
    re.compile(rb"(?:^|[/\\])id_rsa(?:$|[.\s/\\])"),
)

IPV4_PATTERN = re.compile(
    r"(?<![0-9])(?:25[0-5]|2[0-4][0-9]|1?[0-9]{1,2})"
    r"(?:\.(?:25[0-5]|2[0-4][0-9]|1?[0-9]{1,2})){3}(?![0-9])"
)

NETWORK_CONTEXT_PATTERN = re.compile(
    r"(?:\b(?:ssh|scp|rsync|host|endpoint|remote[_-]?ip|server[_-]?ip|"
    r"client[_-]?ip|address|addr)\b|://|@[0-9])",
    re.IGNORECASE,
)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_relative(path: Path, root: Path) -> str:
    relative = path.relative_to(root).as_posix()
    if relative != PurePosixPath(relative).as_posix() or relative.startswith("../"):
        raise ValueError(f"noncanonical artifact path: {relative}")
    return relative


def ignored(path: Path, tree_root: Path) -> bool:
    relative = path.relative_to(tree_root)
    if any(part in IGNORED_DIRECTORY_NAMES or part.startswith("build-") for part in relative.parts[:-1]):
        return True
    return path.name in IGNORED_FILE_NAMES or path.suffix in IGNORED_SUFFIXES


def regular_files(
    tree_root: Path, *, preserve_ignored: bool = False
) -> Iterable[Path]:
    if tree_root.is_symlink() or not tree_root.is_dir():
        raise ValueError(f"missing regular artifact directory: {tree_root}")
    for path in sorted(tree_root.rglob("*")):
        if not preserve_ignored and ignored(path, tree_root):
            continue
        if path.is_symlink():
            raise ValueError(f"symbolic link in public artifact input: {path}")
        if path.is_file():
            yield path


def verify_sealed_evidence(tree_root: Path) -> None:
    """Require every nested evidence seal to remain recursively complete."""
    for seal in sorted(tree_root.rglob("SEALED.json")):
        bundle = seal.parent
        if not (bundle / "SHA256SUMS").is_file() or not (
            bundle / "campaign.json"
        ).is_file():
            raise ValueError(f"incomplete sealed evidence bundle: {bundle}")
        evidence_bundle.verify_bundle(bundle)


def require_regular(path: Path) -> Path:
    if path.is_symlink():
        raise ValueError(f"symbolic link in public artifact input: {path}")
    if not path.is_file():
        raise ValueError(f"missing regular artifact file: {path}")
    return path


def release_entry_paths(repo_root: Path) -> list[Path]:
    manifest_path = require_regular(
        repo_root / "results/vldb_final_evidence/release_bundle.json"
    )
    manifest = json.loads(manifest_path.read_text())
    if manifest.get("kind") != "vldb_release_bundle":
        raise ValueError("release manifest kind mismatch")
    entries = manifest.get("entries")
    targets = manifest.get("publication_pdf_targets")
    if not isinstance(entries, dict) or not entries:
        raise ValueError("release manifest has no entries")
    if not isinstance(targets, list) or not targets:
        raise ValueError("release manifest has no publication PDF targets")
    paths: dict[str, Path] = {}
    for raw, record in entries.items():
        relative = PurePosixPath(raw) if isinstance(raw, str) else None
        if (
            relative is None
            or relative.is_absolute()
            or ".." in relative.parts
            or relative.as_posix() != raw
            or not isinstance(record, dict)
        ):
            raise ValueError(f"invalid release entry: {raw!r}")
        path = require_regular(repo_root / relative)
        path.resolve(strict=True).relative_to(repo_root)
        data = path.read_bytes()
        if (
            record.get("sha256") != sha256_bytes(data)
            or record.get("size_bytes") != len(data)
        ):
            raise ValueError(f"release entry digest or size mismatch: {raw}")
        paths[raw] = path
    if len(paths) != len(entries):
        raise ValueError("duplicate release entry")
    for raw in targets:
        if (
            not isinstance(raw, str)
            or PurePosixPath(raw).suffix.lower() != ".pdf"
            or raw not in paths
        ):
            raise ValueError(f"invalid publication PDF target: {raw!r}")
    if len(targets) != len(set(targets)):
        raise ValueError("duplicate publication PDF target")
    return list(paths.values())


def collect_sources(repo_root: Path) -> dict[str, Path]:
    repo_root = repo_root.resolve(strict=True)
    sources: dict[str, Path] = {}

    def add(target: str, source: Path) -> None:
        target = PurePosixPath(target).as_posix()
        source = require_regular(source)
        source.resolve(strict=True).relative_to(repo_root)
        if target in sources:
            if sources[target] != source:
                raise ValueError(f"duplicate artifact target: {target}")
            return
        sources[target] = source

    for source_name, target_name in ROOT_FILES.items():
        add(target_name, repo_root / source_name)

    for tree_name in TREE_ROOTS:
        tree_root = repo_root / tree_name
        preserve_ignored = tree_name == "results/vldb_final_evidence"
        if preserve_ignored:
            verify_sealed_evidence(tree_root)
        for source in regular_files(tree_root, preserve_ignored=preserve_ignored):
            add(canonical_relative(source, repo_root), source)

    for tree_name in OPTIONAL_SEALED_MULTICN_ROOTS:
        tree_root = repo_root / tree_name
        if not tree_root.exists():
            continue
        multicn_seal.verify_campaign(tree_root)
        for source in regular_files(tree_root, preserve_ignored=True):
            add(canonical_relative(source, repo_root), source)

    for relative in PAPER_FILES:
        path = repo_root / relative
        if path.exists():
            add(relative, path)
    for path in release_entry_paths(repo_root):
        add(canonical_relative(path, repo_root), path)

    return dict(sorted(sources.items()))


def sensitive_reasons(relative: str, data: bytes) -> list[str]:
    reasons = [
        pattern.pattern.decode(errors="replace")
        for pattern in SENSITIVE_BYTE_PATTERNS
        if pattern.search(data)
    ]
    text = data.decode("utf-8", errors="ignore")
    public_ips: set[str] = set()
    for line in text.splitlines():
        if not NETWORK_CONTEXT_PATTERN.search(line):
            continue
        public_ips.update(
            match.group(0)
            for match in IPV4_PATTERN.finditer(line)
            if ipaddress.ip_address(match.group(0)).is_global
        )
    reasons.extend(
        f"public IPv4 address {address}" for address in sorted(public_ips)
    )
    return [f"{relative}: {reason}" for reason in reasons]


def validate_public_payload(payloads: dict[str, bytes]) -> None:
    findings: list[str] = []
    for relative, data in payloads.items():
        findings.extend(sensitive_reasons(relative, data))
    if findings:
        raise ValueError("sensitive material in public artifact:\n" + "\n".join(findings))


def safe_existing_artifact(path: Path) -> bool:
    manifest = path / "artifact_manifest.json"
    if not path.is_dir() or not manifest.is_file() or manifest.is_symlink():
        return False
    try:
        return json.loads(manifest.read_text()).get("kind") == "slabwalk_public_artifact"
    except (json.JSONDecodeError, OSError):
        return False


def build_public_artifact(
    repo_root: Path, output: Path, *, force: bool = False
) -> dict[str, object]:
    repo_root = repo_root.resolve(strict=True)
    output = output.expanduser().resolve(strict=False)
    try:
        output.relative_to(repo_root)
    except ValueError:
        pass
    else:
        raise ValueError("public artifact output must be outside the source repository")

    if output.exists():
        git_metadata = output / ".git"
        if git_metadata.exists() or git_metadata.is_symlink():
            raise ValueError(
                f"refusing to replace Git checkout; build into a staging path: {output}"
            )
        if not force:
            raise FileExistsError(f"artifact output already exists: {output}")
        if not safe_existing_artifact(output):
            raise ValueError(f"refusing to replace non-artifact directory: {output}")

    sources = collect_sources(repo_root)
    payloads = {relative: source.read_bytes() for relative, source in sources.items()}
    validate_public_payload(payloads)
    records = {
        relative: {"bytes": len(data), "sha256": sha256_bytes(data)}
        for relative, data in payloads.items()
    }
    release_manifest = payloads["results/vldb_final_evidence/release_bundle.json"]
    diagnostic_campaigns = []
    for tree_name in OPTIONAL_SEALED_MULTICN_ROOTS:
        seal_name = f"{tree_name}/{multicn_seal.SEAL_NAME}"
        if seal_name not in payloads:
            continue
        seal = json.loads(payloads[seal_name])
        diagnostic_campaigns.append(
            {
                "campaign_id": seal["campaign_id"],
                "path": tree_name,
                "promotion_ready": seal["promotion_ready"],
                "seal_sha256": sha256_bytes(payloads[seal_name]),
            }
        )
    manifest = {
        "diagnostic_multicn_campaigns": diagnostic_campaigns,
        "file_count": len(records),
        "files": records,
        "kind": "slabwalk_public_artifact",
        "release_manifest_sha256": sha256_bytes(release_manifest),
        "schema_version": 1,
    }
    manifest_bytes = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
    checksums = "".join(
        f"{record['sha256']}  {relative}\n"
        for relative, record in records.items()
    ).encode()

    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(prefix=f".{output.name}.stage-", dir=output.parent)
    )
    try:
        for relative, data in payloads.items():
            target = temporary / PurePosixPath(relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            target.chmod(0o755 if os.access(sources[relative], os.X_OK) else 0o644)
        verify_sealed_evidence(temporary / "results/vldb_final_evidence")
        for tree_name in OPTIONAL_SEALED_MULTICN_ROOTS:
            tree_root = temporary / tree_name
            if tree_root.exists():
                multicn_seal.verify_campaign(tree_root)
        (temporary / "SHA256SUMS").write_bytes(checksums)
        (temporary / "artifact_manifest.json").write_bytes(manifest_bytes)
        if output.exists():
            shutil.rmtree(output)
        os.replace(temporary, output)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise

    return {
        "bytes": sum(record["bytes"] for record in records.values()),
        "file_count": len(records),
        "manifest_sha256": sha256_bytes(manifest_bytes),
        "output": str(output),
        "release_manifest_sha256": manifest["release_manifest_sha256"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    report = build_public_artifact(args.repo_root, args.out, force=args.force)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
