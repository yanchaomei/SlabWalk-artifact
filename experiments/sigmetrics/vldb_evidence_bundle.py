#!/usr/bin/env python3
"""Freeze experiment harnesses and seal recursively verifiable evidence bundles."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
ROLE_RE = re.compile(r"^[a-z][a-z0-9_]*$")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _contained(root: Path, raw: str, label: str) -> Path:
    relative = Path(raw)
    if relative.is_absolute():
        raise ValueError(f"{label} path escapes bundle root")
    resolved = (root / relative).resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as error:
        raise ValueError(f"{label} path escapes bundle root") from error
    return resolved


def _manifest_files(root: Path, manifest: Path) -> set[Path]:
    seal = root / "SEALED.json"
    return {
        path.resolve()
        for path in root.rglob("*")
        if path.is_file()
        and path.resolve() != manifest.resolve()
        and path.resolve() != seal.resolve()
    }


def verify_manifest(manifest: Path, *, exact: bool = True) -> dict[str, str]:
    manifest = manifest.resolve()
    root = manifest.parent
    if not manifest.is_file():
        raise ValueError(f"missing SHA256SUMS: {manifest}")
    entries: dict[Path, str] = {}
    for line_number, line in enumerate(manifest.read_text().splitlines(), start=1):
        if "  " not in line:
            raise ValueError(f"malformed SHA256SUMS line {line_number}")
        digest, raw = line.split("  ", 1)
        if not SHA256_RE.fullmatch(digest) or not raw:
            raise ValueError(f"malformed SHA256SUMS line {line_number}")
        artifact = _contained(root, raw, "SHA256SUMS")
        if artifact in entries:
            raise ValueError("duplicate SHA256SUMS entry")
        entries[artifact] = digest
    if exact and set(entries) != _manifest_files(root, manifest):
        raise ValueError("SHA256SUMS does not cover the complete bundle tree")
    for artifact, digest in entries.items():
        if not artifact.is_file() or _sha256(artifact) != digest:
            raise ValueError(f"SHA256SUMS mismatch: {artifact}")
    return {path.relative_to(root).as_posix(): digest for path, digest in entries.items()}


def snapshot_harness(output_dir: Path, entries: dict[str, Path]) -> dict[str, Any]:
    output_dir = output_dir.resolve()
    if output_dir.exists() and any(output_dir.iterdir()):
        raise ValueError(f"refusing non-empty harness directory: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)
    if not entries:
        raise ValueError("harness snapshot requires at least one entry")
    records: dict[str, dict[str, Any]] = {}
    destinations: set[str] = set()
    for role, source_raw in sorted(entries.items()):
        if ROLE_RE.fullmatch(role) is None:
            raise ValueError(f"invalid harness role: {role}")
        source = Path(source_raw).resolve()
        if not source.is_file():
            raise ValueError(f"missing harness source: {source}")
        name = f"{role}__{source.name}"
        if name in destinations:
            raise ValueError(f"duplicate harness destination: {name}")
        destinations.add(name)
        destination = output_dir / name
        destination.write_bytes(source.read_bytes())
        executable = os.access(source, os.X_OK)
        destination.chmod(0o555 if executable else 0o444)
        records[role] = {
            "path": name,
            "source_path": str(source),
            "bytes": destination.stat().st_size,
            "sha256": _sha256(destination),
            "executable": executable,
        }
    payload = {"schema_version": 1, "entries": records}
    manifest = output_dir / "harness.json"
    manifest.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    payload["manifest_sha256"] = _sha256(manifest)
    return payload


def verify_harness(
    manifest: Path, *, expected_manifest_sha256: str | None = None
) -> dict[str, Any]:
    manifest = manifest.resolve()
    if not manifest.is_file():
        raise ValueError(f"missing harness manifest: {manifest}")
    actual_manifest_sha = _sha256(manifest)
    if expected_manifest_sha256 is not None:
        if not SHA256_RE.fullmatch(expected_manifest_sha256):
            raise ValueError("expected harness manifest SHA is malformed")
        if actual_manifest_sha != expected_manifest_sha256:
            raise ValueError("harness manifest SHA drift")
    payload = json.loads(manifest.read_text())
    if payload.get("schema_version") != 1 or not isinstance(payload.get("entries"), dict):
        raise ValueError("unsupported harness manifest")
    root = manifest.parent
    expected_paths = {manifest.resolve()}
    for role, record in payload["entries"].items():
        if ROLE_RE.fullmatch(str(role)) is None or not isinstance(record, dict):
            raise ValueError("malformed harness entry")
        path = _contained(root, str(record.get("path", "")), "harness")
        expected_paths.add(path)
        digest = str(record.get("sha256", ""))
        if not SHA256_RE.fullmatch(digest) or not path.is_file() or _sha256(path) != digest:
            raise ValueError(f"harness SHA drift: {role}")
        if path.stat().st_size != int(record.get("bytes", -1)):
            raise ValueError(f"harness size drift: {role}")
    actual_paths = {path.resolve() for path in root.iterdir() if path.is_file()}
    if actual_paths != expected_paths:
        raise ValueError("harness directory contains untracked files")
    payload["manifest_sha256"] = actual_manifest_sha
    return payload


def _write_manifest(root: Path) -> Path:
    manifest = root / "SHA256SUMS"
    seal = root / "SEALED.json"
    if manifest.exists() or seal.exists():
        raise ValueError(f"refusing to reseal existing bundle: {root}")
    files = sorted(
        path
        for path in root.rglob("*")
        if path.is_file() and path != manifest and path != seal
    )
    manifest.write_text(
        "".join(
            f"{_sha256(path)}  {path.relative_to(root).as_posix()}\n"
            for path in files
        )
    )
    return manifest


def _verify_nested_manifests(root: Path, *, exclude_root: bool) -> None:
    root_manifest = (root / "SHA256SUMS").resolve()
    for manifest in sorted(root.rglob("SHA256SUMS")):
        if exclude_root and manifest.resolve() == root_manifest:
            continue
        try:
            verify_manifest(manifest)
        except ValueError as error:
            raise ValueError(f"nested SHA256SUMS validation failed: {manifest}: {error}") from error


def seal_bundle(root: Path, campaign_path: Path) -> dict[str, Any]:
    root = root.resolve()
    campaign_path = campaign_path.resolve()
    if not root.is_dir():
        raise ValueError(f"missing bundle root: {root}")
    try:
        campaign_path.relative_to(root)
    except ValueError as error:
        raise ValueError("campaign path escapes bundle root") from error
    campaign = json.loads(campaign_path.read_text())
    fingerprint = str(campaign.get("protocol_fingerprint", ""))
    if not SHA256_RE.fullmatch(fingerprint):
        raise ValueError("campaign protocol fingerprint is missing or malformed")
    campaign_id = str(campaign.get("campaign_id", ""))
    campaign_uuid = str(campaign.get("campaign_uuid", ""))
    if not campaign_id or not campaign_uuid:
        raise ValueError("campaign identity is incomplete")
    _verify_nested_manifests(root, exclude_root=True)
    manifest = _write_manifest(root)
    entries = verify_manifest(manifest)
    seal = {
        "schema_version": 1,
        "campaign_id": campaign_id,
        "campaign_uuid": campaign_uuid,
        "protocol_fingerprint": fingerprint,
        "root_manifest": "SHA256SUMS",
        "root_manifest_sha256": _sha256(manifest),
        "sealed_file_count": len(entries),
        "sealed_utc": datetime.now(timezone.utc).isoformat(),
    }
    (root / "SEALED.json").write_text(json.dumps(seal, indent=2, sort_keys=True) + "\n")
    verify_bundle(root)
    return seal


def verify_bundle(root: Path) -> dict[str, Any]:
    root = root.resolve()
    seal_path = root / "SEALED.json"
    manifest = root / "SHA256SUMS"
    campaign_path = root / "campaign.json"
    if not seal_path.is_file() or not campaign_path.is_file():
        raise ValueError("bundle is missing SEALED.json or campaign.json")
    seal = json.loads(seal_path.read_text())
    campaign = json.loads(campaign_path.read_text())
    if seal.get("schema_version") != 1:
        raise ValueError("unsupported evidence seal")
    if str(seal.get("root_manifest_sha256", "")) != _sha256(manifest):
        raise ValueError("root manifest SHA does not match SEALED.json")
    for field in ("campaign_id", "campaign_uuid", "protocol_fingerprint"):
        if str(seal.get(field, "")) != str(campaign.get(field, "")):
            raise ValueError(f"sealed {field} does not match campaign")
    entries = verify_manifest(manifest)
    if int(seal.get("sealed_file_count", -1)) != len(entries):
        raise ValueError("sealed file count does not match complete bundle tree")
    _verify_nested_manifests(root, exclude_root=True)
    return seal


def _parse_entry(raw: str) -> tuple[str, Path]:
    if "=" not in raw:
        raise argparse.ArgumentTypeError("entry must be ROLE=PATH")
    role, path = raw.split("=", 1)
    return role, Path(path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    snapshot = commands.add_parser("snapshot")
    snapshot.add_argument("--out-dir", type=Path, required=True)
    snapshot.add_argument("--entry", action="append", type=_parse_entry, required=True)

    verify_h = commands.add_parser("verify-harness")
    verify_h.add_argument("--manifest", type=Path, required=True)
    verify_h.add_argument("--expected-manifest-sha")

    seal = commands.add_parser("seal")
    seal.add_argument("--root", type=Path, required=True)
    seal.add_argument("--campaign", type=Path, required=True)

    verify = commands.add_parser("verify")
    verify.add_argument("--root", type=Path, required=True)

    args = parser.parse_args()
    if args.command == "snapshot":
        entries = dict(args.entry)
        if len(entries) != len(args.entry):
            raise ValueError("duplicate harness role")
        result = snapshot_harness(args.out_dir, entries)
    elif args.command == "verify-harness":
        result = verify_harness(
            args.manifest,
            expected_manifest_sha256=args.expected_manifest_sha,
        )
    elif args.command == "seal":
        result = seal_bundle(args.root, args.campaign)
    else:
        result = verify_bundle(args.root)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
