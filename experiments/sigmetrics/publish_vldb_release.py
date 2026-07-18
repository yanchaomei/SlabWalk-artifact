#!/usr/bin/env python3
"""Publish a gated VLDB figure/claim bundle with a marker installed last."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import uuid
from pathlib import Path, PurePosixPath
from typing import Any, Callable

import render_vldb_claims_tex as claims_renderer
import verify_publication_pdf as pdf_verifier


Replace = Callable[[Path, Path], None]
PUBLICATION_PDF_COUNT = 9


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def require_source(path: Path) -> Path:
    if path.is_symlink() or not path.is_file() or path.stat().st_size == 0:
        raise ValueError(f"release source must be a non-empty regular file: {path}")
    return path.resolve(strict=True)


def target_path(repo_root: Path, relative_text: str) -> Path:
    relative = PurePosixPath(relative_text)
    if relative.as_posix() != relative_text:
        raise ValueError(f"release target must use canonical POSIX spelling: {relative_text}")
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise ValueError(f"release target must be repository-relative: {relative_text}")
    current = repo_root
    for part in relative.parts[:-1]:
        current = current / part
        if current.exists() and current.is_symlink():
            raise ValueError(f"release target crosses a symbolic link: {relative_text}")
    target = repo_root.joinpath(*relative.parts)
    if target.exists() and target.is_symlink():
        raise ValueError(f"release target is a symbolic link: {relative_text}")
    return target


def load_gate_bytes(data: bytes) -> dict[str, Any]:
    gate = json.loads(data)
    if gate.get("kind") != "vldb_final_evidence_gate":
        raise ValueError("evidence gate kind mismatch")
    if gate.get("ready_for_plotting") is not True:
        raise ValueError("evidence gate is not ready for plotting")
    return gate


def load_gate(path: Path) -> dict[str, Any]:
    return load_gate_bytes(path.read_bytes())


def validate_binding_bytes(
    gate_bytes: bytes, claims_bytes: bytes, generated_claims_bytes: bytes
) -> str:
    load_gate_bytes(gate_bytes)
    gate_sha = sha256_bytes(gate_bytes)
    claim_data = json.loads(claims_bytes)
    if claim_data.get("kind") != "vldb_manuscript_claims":
        raise ValueError("manuscript claim kind mismatch")
    if claim_data.get("gate_sha256") != gate_sha:
        raise ValueError("claims are not bound to the selected evidence gate")
    expected_generated = claims_renderer.render_bytes(claims_bytes)
    if generated_claims_bytes != expected_generated:
        raise ValueError(
            "generated LaTeX claims do not match the deterministic rendering"
        )
    return gate_sha


def validate_bindings(gate: Path, claims: Path, generated_claims: Path) -> str:
    return validate_binding_bytes(
        gate.read_bytes(), claims.read_bytes(), generated_claims.read_bytes()
    )


def verify_records(
    repo_root: Path, records: dict[str, dict[str, Any]]
) -> None:
    for relative, record in records.items():
        if not isinstance(record, dict):
            raise ValueError(f"invalid release entry: {relative}")
        path = target_path(repo_root, relative)
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"missing regular release target: {relative}")
        if path.stat().st_size != record.get("size_bytes"):
            raise ValueError(f"release target size mismatch: {relative}")
        if sha256(path) != record.get("sha256"):
            raise ValueError(f"release target hash mismatch: {relative}")


def capture_records(
    repo_root: Path, records: dict[str, dict[str, Any]]
) -> dict[str, bytes]:
    snapshots: dict[str, bytes] = {}
    for relative, record in records.items():
        if not isinstance(record, dict):
            raise ValueError(f"invalid release entry: {relative}")
        path = target_path(repo_root, relative)
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"missing regular release target: {relative}")
        data = path.read_bytes()
        if len(data) != record.get("size_bytes"):
            raise ValueError(f"release target size mismatch: {relative}")
        if sha256_bytes(data) != record.get("sha256"):
            raise ValueError(f"release target hash mismatch: {relative}")
        snapshots[relative] = data
    return snapshots


def find_source_target(
    normalized_entries: list[tuple[str, Path]], source: Path, label: str
) -> str:
    source_resolved = source.resolve(strict=True)
    matches = [target for target, candidate in normalized_entries if candidate == source_resolved]
    if len(matches) != 1:
        raise ValueError(f"release entries must contain exactly one {label} source")
    return matches[0]


def capture_release_snapshot(
    repo_root: Path, manifest_bytes: bytes
) -> tuple[dict[str, Any], dict[str, bytes]]:
    repo_root = repo_root.resolve(strict=True)
    manifest = json.loads(manifest_bytes)
    if manifest.get("kind") != "vldb_release_bundle":
        raise ValueError("release manifest kind mismatch")
    entries = manifest.get("entries")
    if not isinstance(entries, dict) or not entries:
        raise ValueError("release manifest has no entries")
    snapshots = capture_records(repo_root, entries)

    pdf_targets = manifest.get("publication_pdf_targets")
    if (
        not isinstance(pdf_targets, list)
        or len(pdf_targets) != PUBLICATION_PDF_COUNT
        or not all(isinstance(target, str) for target in pdf_targets)
        or len(set(pdf_targets)) != len(pdf_targets)
    ):
        raise ValueError(
            "release manifest must contain exactly nine unique publication PDF targets"
        )
    for target in pdf_targets:
        if target not in entries or PurePosixPath(target).suffix.lower() != ".pdf":
            raise ValueError(f"invalid release manifest PDF target: {target}")

    for field in ("gate_target", "claims_target", "generated_claims_target"):
        if manifest.get(field) not in entries:
            raise ValueError(f"release manifest is missing {field}")
    gate_target = manifest["gate_target"]
    claims_target = manifest["claims_target"]
    generated_target = manifest["generated_claims_target"]
    gate_sha = validate_binding_bytes(
        snapshots[gate_target],
        snapshots[claims_target],
        snapshots[generated_target],
    )
    if manifest.get("gate_sha256") != gate_sha:
        raise ValueError("release manifest gate hash mismatch")
    if manifest.get("claims_sha256") != sha256_bytes(snapshots[claims_target]):
        raise ValueError("release manifest claims hash mismatch")
    verification = {
        "kind": "vldb_release_verification",
        "entries_verified": len(entries),
        "gate_sha256": gate_sha,
        "manifest_sha256": sha256_bytes(manifest_bytes),
    }
    return verification, snapshots


def verify_release_snapshot(
    repo_root: Path, manifest_bytes: bytes
) -> dict[str, Any]:
    verification, _ = capture_release_snapshot(repo_root, manifest_bytes)
    return verification


def verify_release(repo_root: Path, manifest_path: Path) -> dict[str, Any]:
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ValueError(f"missing regular release manifest: {manifest_path}")
    return verify_release_snapshot(repo_root, manifest_path.read_bytes())


def publish(
    *,
    repo_root: Path,
    gate: Path,
    claims: Path,
    generated_claims: Path,
    entries: list[tuple[str, Path]],
    manifest_out: Path,
    pdf_targets: list[str] | tuple[str, ...],
    replace_fn: Replace = os.replace,
) -> dict[str, Any]:
    repo_root = repo_root.resolve(strict=True)
    manifest_parent = manifest_out.parent
    manifest_parent.mkdir(parents=True, exist_ok=True)
    manifest_resolved_parent = manifest_parent.resolve(strict=True)
    try:
        manifest_resolved_parent.relative_to(repo_root)
    except ValueError as exc:
        raise ValueError("release manifest must be inside the repository") from exc
    if manifest_out.exists() and manifest_out.is_symlink():
        raise ValueError("release manifest must not be a symbolic link")
    manifest_out.unlink(missing_ok=True)
    fsync_directory(manifest_parent)

    prepared: list[tuple[str, Path, Path]] = []
    manifest_temporary = manifest_out.with_name(
        f".{manifest_out.name}.tmp-{uuid.uuid4().hex}"
    )
    try:
        gate = require_source(gate)
        claims = require_source(claims)
        generated_claims = require_source(generated_claims)

        normalized: list[tuple[str, Path]] = []
        seen: set[str] = set()
        seen_resolved_targets: set[Path] = set()
        for relative, source in entries:
            if relative in seen:
                raise ValueError(f"duplicate release target: {relative}")
            seen.add(relative)
            target = target_path(repo_root, relative)
            resolved_target = target.resolve(strict=False)
            if resolved_target in seen_resolved_targets:
                raise ValueError(f"duplicate resolved release target: {relative}")
            seen_resolved_targets.add(resolved_target)
            if target.resolve(strict=False) == manifest_out.resolve(strict=False):
                raise ValueError("release manifest must not also be a release target")
            normalized.append((relative, require_source(source)))
        gate_target = find_source_target(normalized, gate, "gate")
        claims_target = find_source_target(normalized, claims, "claims")
        generated_target = find_source_target(
            normalized, generated_claims, "generated claims"
        )

        for relative, source in normalized:
            target = target_path(repo_root, relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(f".{target.name}.release-{uuid.uuid4().hex}")
            with source.open("rb") as src, temporary.open("xb") as dst:
                shutil.copyfileobj(src, dst, length=1024 * 1024)
                dst.flush()
                os.fsync(dst.fileno())
            os.chmod(temporary, stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH)
            prepared.append((relative, temporary, target))

        staged = {relative: temporary for relative, temporary, _ in prepared}
        gate_sha = validate_bindings(
            staged[gate_target], staged[claims_target], staged[generated_target]
        )
        normalized_pdf_targets = tuple(pdf_targets)
        if (
            len(normalized_pdf_targets) != PUBLICATION_PDF_COUNT
            or len(set(normalized_pdf_targets)) != PUBLICATION_PDF_COUNT
        ):
            raise ValueError(
                "release must contain exactly nine unique publication PDF targets"
            )
        for relative in normalized_pdf_targets:
            target_path(repo_root, relative)
            if relative not in staged or PurePosixPath(relative).suffix.lower() != ".pdf":
                raise ValueError(f"invalid publication PDF target: {relative}")
            try:
                pdf_verifier.verify(staged[relative])
            except Exception as exc:
                raise ValueError(
                    f"publication PDF verification failed: {relative}"
                ) from exc
        records = {
            relative: {
                "sha256": sha256(temporary),
                "size_bytes": temporary.stat().st_size,
            }
            for relative, temporary, _ in prepared
        }
        manifest = {
            "kind": "vldb_release_bundle",
            "gate_sha256": gate_sha,
            "claims_sha256": sha256(staged[claims_target]),
            "gate_target": gate_target,
            "claims_target": claims_target,
            "generated_claims_target": generated_target,
            "publication_pdf_targets": list(normalized_pdf_targets),
            "entries": records,
        }

        for _, temporary, target in prepared:
            replace_fn(temporary, target)
            fsync_directory(target.parent)

        verify_records(repo_root, records)
        validate_bindings(
            target_path(repo_root, gate_target),
            target_path(repo_root, claims_target),
            target_path(repo_root, generated_target),
        )
        for relative in normalized_pdf_targets:
            try:
                pdf_verifier.verify(target_path(repo_root, relative))
            except Exception as exc:
                raise ValueError(
                    f"published PDF verification failed: {relative}"
                ) from exc

        with manifest_temporary.open("x") as handle:
            json.dump(manifest, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(
            manifest_temporary,
            stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH,
        )
        replace_fn(manifest_temporary, manifest_out)
        fsync_directory(manifest_parent)
        verification = verify_release(repo_root, manifest_out)
    except BaseException:
        manifest_out.unlink(missing_ok=True)
        fsync_directory(manifest_parent)
        raise
    finally:
        for _, temporary, _ in prepared:
            temporary.unlink(missing_ok=True)
        manifest_temporary.unlink(missing_ok=True)

    return {
        **manifest,
        "manifest_sha256": verification["manifest_sha256"],
        "entries_verified": verification["entries_verified"],
    }


def parse_entry(raw: str) -> tuple[str, Path]:
    if "=" not in raw:
        raise argparse.ArgumentTypeError("entry must be TARGET=SOURCE")
    target, source = raw.split("=", 1)
    if not target or not source:
        raise argparse.ArgumentTypeError("entry must be TARGET=SOURCE")
    return target, Path(source)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--gate", type=Path, required=True)
    parser.add_argument("--claims", type=Path, required=True)
    parser.add_argument("--generated-claims", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--entry", action="append", type=parse_entry, required=True)
    parser.add_argument("--pdf-target", action="append", required=True)
    args = parser.parse_args()
    report = publish(
        repo_root=args.repo_root,
        gate=args.gate,
        claims=args.claims,
        generated_claims=args.generated_claims,
        entries=args.entry,
        pdf_targets=args.pdf_target,
        manifest_out=args.manifest,
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
