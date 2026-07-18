"""Test-only factory for a chained worker-campaign audit trail."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


def json_bytes(value: object) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()


def sha_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def protocol_fingerprint(protocol: dict[str, object]) -> str:
    return sha_bytes(json.dumps(protocol, sort_keys=True, separators=(",", ":")).encode())


def manifest(campaign_id: str, protocol: dict[str, object], **pointers: str) -> dict[str, object]:
    return {
        "campaign_id": campaign_id,
        "protocol": protocol,
        "protocol_fingerprint": protocol_fingerprint(protocol),
        **pointers,
    }


def write_campaign_audit(
    root: Path,
    *,
    campaign_id: str,
    slabwalk_sha: str,
    dhnsw_sha: str,
) -> None:
    root.mkdir(parents=True, exist_ok=True)
    parser0, parser1, parser2 = (character * 64 for character in "123")
    assembler0, assembler1, assembler2 = (character * 64 for character in "456")
    runner0, runner1 = (character * 64 for character in "78")
    protocol = {
        "slabwalk_binary_sha256": slabwalk_sha,
        "dhnsw_client_binary_sha256": dhnsw_sha,
        "workers": [1, 8, 16, 40],
        "repeats": 5,
        "dhnsw_parser_sha256": parser0,
        "assembler_sha256": assembler0,
        "dhnsw_runner_sha256": runner0,
    }
    m0 = manifest(campaign_id, protocol)

    protocol1 = dict(protocol, dhnsw_parser_sha256=parser1)
    m1 = manifest(campaign_id, protocol1, parser_amendment="parser_amendment.json")
    parser_record = {
        "campaign_id": campaign_id,
        "old_parser_sha256": parser0,
        "new_parser_sha256": parser1,
        "original_manifest_sha256": sha_bytes(json_bytes(m0)),
        "amended_manifest_sha256": sha_bytes(json_bytes(m1)),
        "reparsed_runs": [],
    }

    protocol2 = dict(protocol1, assembler_sha256=assembler1)
    m2 = manifest(
        campaign_id,
        protocol2,
        parser_amendment="parser_amendment.json",
        assembler_amendment="assembler_amendment.json",
    )
    assembler_record = {
        "campaign_id": campaign_id,
        "old_tool_sha256": assembler0,
        "new_tool_sha256": assembler1,
        "original_manifest_sha256": sha_bytes(json_bytes(m1)),
        "amended_manifest_sha256": sha_bytes(json_bytes(m2)),
    }

    protocol3 = dict(protocol2, dhnsw_parser_sha256=parser2)
    m3 = manifest(
        campaign_id,
        protocol3,
        parser_amendment="parser_amendment.json",
        assembler_amendment="assembler_amendment.json",
        parser_amendment_v2="parser_amendment_v2.json",
    )
    parser_v2_record = {
        "campaign_id": campaign_id,
        "old_parser_sha256": parser1,
        "new_parser_sha256": parser2,
        "original_manifest_sha256": sha_bytes(json_bytes(m2)),
        "amended_manifest_sha256": sha_bytes(json_bytes(m3)),
        "revalidated_runs": [],
    }

    protocol4 = dict(protocol3, dhnsw_runner_sha256=runner1)
    m4 = manifest(
        campaign_id,
        protocol4,
        parser_amendment="parser_amendment.json",
        assembler_amendment="assembler_amendment.json",
        parser_amendment_v2="parser_amendment_v2.json",
        dhnsw_runner_amendment="dhnsw_runner_amendment.json",
    )
    runner_record = {
        "campaign_id": campaign_id,
        "old_tool_sha256": runner0,
        "new_tool_sha256": runner1,
        "original_manifest_sha256": sha_bytes(json_bytes(m3)),
        "amended_manifest_sha256": sha_bytes(json_bytes(m4)),
    }

    protocol5 = dict(protocol4, assembler_sha256=assembler2)
    m5 = manifest(
        campaign_id,
        protocol5,
        parser_amendment="parser_amendment.json",
        assembler_amendment="assembler_amendment.json",
        parser_amendment_v2="parser_amendment_v2.json",
        dhnsw_runner_amendment="dhnsw_runner_amendment.json",
        assembler_amendment_v2="assembler_amendment_v2.json",
    )
    assembler_v2_record = {
        "campaign_id": campaign_id,
        "old_tool_sha256": assembler1,
        "new_tool_sha256": assembler2,
        "original_manifest_sha256": sha_bytes(json_bytes(m4)),
        "amended_manifest_sha256": sha_bytes(json_bytes(m5)),
    }

    files = {
        "campaign.before-parser-amendment.json": m0,
        "parser_amendment.json": parser_record,
        "campaign.before-assembler-amendment.json": m1,
        "assembler_amendment.json": assembler_record,
        "campaign.before-parser-amendment-v2.json": m2,
        "parser_amendment_v2.json": parser_v2_record,
        "campaign.before-dhnsw-runner-amendment.json": m3,
        "dhnsw_runner_amendment.json": runner_record,
        "campaign.before-assembler-amendment-v2.json": m4,
        "assembler_amendment_v2.json": assembler_v2_record,
        "campaign.json": m5,
    }
    for name, value in files.items():
        (root / name).write_bytes(json_bytes(value))

    archive = root / "failed_runs/dhnsw/w40/r0-before-runner-fix"
    archive.mkdir(parents=True)
    failed_log = archive / "deep1M_ef200_client.log"
    failed_log.write_text("excluded interleaved output\n")
    failed_record = {
        "campaign_id": campaign_id,
        "campaign_manifest_sha256": sha_bytes(json_bytes(m2)),
        "source": "raw/dhnsw/w40/r0",
        "archive": "failed_runs/dhnsw/w40/r0-before-runner-fix",
        "reason": "synthetic interleaving fixture",
        "files": [
            {
                "path": failed_log.name,
                "size_bytes": failed_log.stat().st_size,
                "sha256": hashlib.sha256(failed_log.read_bytes()).hexdigest(),
            }
        ],
    }
    (root / "failed_run_archive_w40_r0_before-runner-fix.json").write_bytes(
        json_bytes(failed_record)
    )
