#!/usr/bin/env python3
"""Atomically assemble the final five-repeat Slab build-cost evidence bundle."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

import summarize_slab_build_cost as build_summary


DATASETS = ("SIFT1M", "DEEP1M", "GIST1M")
REPEATS = 5
SHA256_LENGTH = 64
ADMISSION_INPUTS = (
    "promotion_report",
    "frontier_cells",
    "candidate_frontier",
    "baseline_frontier",
)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_sha(value: str, label: str) -> None:
    if len(value) != SHA256_LENGTH or any(ch not in "0123456789abcdef" for ch in value):
        raise ValueError(f"{label} is not a lowercase SHA-256")


def campaign_raw(root: Path) -> Path:
    raw = root / "raw"
    return raw if raw.is_dir() else root


def read_construction_admission(
    manifest: dict[str, object],
    dataset: str,
    expected_gate_sha: str | None,
    expected_scope: str | None,
) -> dict[str, object] | None:
    if (expected_gate_sha is None) != (expected_scope is None):
        raise ValueError("expected admission gate SHA and scope must be set together")
    admission = manifest.get("admission")
    if admission is None:
        if expected_gate_sha is not None:
            raise ValueError(f"{dataset}: source campaign lacks construction admission")
        return None
    if not isinstance(admission, dict):
        raise ValueError(f"{dataset}: invalid construction admission record")
    gate_sha = str(admission.get("sha256", ""))
    validate_sha(gate_sha, f"{dataset} admission gate SHA")
    if expected_gate_sha is not None and gate_sha != expected_gate_sha:
        raise ValueError(f"{dataset}: construction admission gate SHA mismatch")
    scope = str(admission.get("scope", ""))
    if expected_scope is not None and scope != expected_scope:
        raise ValueError(f"{dataset}: construction admission scope mismatch")
    if (
        admission.get("kind") != "vldb_construction_candidate_gate_v1"
        or admission.get("construction_ready") is not True
        or admission.get("general_promotion_ready") is not False
        or scope != "construction_measurements_only"
    ):
        raise ValueError(f"{dataset}: construction admission contract mismatch")
    gate_path = Path(str(admission.get("path", ""))).resolve()
    if not gate_path.is_file() or file_sha256(gate_path) != gate_sha:
        raise ValueError(f"{dataset}: construction admission gate SHA drift")
    try:
        gate = json.loads(gate_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{dataset}: invalid construction admission gate") from exc
    if (
        not isinstance(gate, dict)
        or gate.get("kind") != admission["kind"]
        or gate.get("construction_ready") is not True
        or gate.get("general_promotion_ready") is not False
        or gate.get("scope") != scope
        or gate.get("failures") != []
    ):
        raise ValueError(f"{dataset}: construction gate content mismatch")
    inputs = gate.get("inputs")
    if not isinstance(inputs, dict) or set(inputs) != set(ADMISSION_INPUTS):
        raise ValueError(f"{dataset}: construction gate input contract mismatch")
    verified_inputs: dict[str, dict[str, str]] = {}
    for name in ADMISSION_INPUTS:
        record = inputs.get(name)
        if not isinstance(record, dict) or set(record) != {"path", "sha256"}:
            raise ValueError(f"{dataset}: invalid {name} admission input")
        source = Path(str(record["path"])).resolve()
        source_sha = str(record["sha256"])
        validate_sha(source_sha, f"{dataset} {name} admission input SHA")
        if not source.is_file() or file_sha256(source) != source_sha:
            raise ValueError(f"{dataset}: {name} admission input SHA drift")
        verified_inputs[name] = {"path": str(source), "sha256": source_sha}
    return {
        "kind": admission["kind"],
        "path": str(gate_path),
        "sha256": gate_sha,
        "scope": scope,
        "construction_ready": True,
        "general_promotion_ready": False,
        "inputs": verified_inputs,
    }


def read_campaign(
    root: Path,
    dataset: str,
    expected_binary_sha: str,
    expected_source_tree_sha: str | None = None,
    expected_admission_gate_sha: str | None = None,
    expected_admission_scope: str | None = None,
) -> tuple[Path, dict[str, object], dict[str, object] | None]:
    raw = campaign_raw(root)
    manifest_path = raw / "campaign.json"
    if not manifest_path.is_file():
        raise ValueError(f"{dataset}: missing source campaign manifest {manifest_path}")
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{dataset}: invalid source campaign manifest") from exc
    if not isinstance(manifest, dict):
        raise ValueError(f"{dataset}: source campaign manifest is not an object")
    runner_sha = str(manifest.get("script_sha256", ""))
    validate_sha(runner_sha, f"{dataset} runner script SHA")
    if str(manifest.get("binary_sha256", "")) != expected_binary_sha:
        raise ValueError(f"{dataset}: source campaign binary SHA mismatch")
    if dataset not in list(manifest.get("datasets", [])):
        raise ValueError(f"{dataset}: source campaign does not include the dataset")
    if int(manifest.get("repeats", 0)) != REPEATS:
        raise ValueError(f"{dataset}: source campaign must request five repeats")
    if (
        int(manifest.get("builder_threads", 0)) != 20
        or int(manifest.get("query_threads", 0)) != 1
        or int(manifest.get("query_coroutines", 0)) != 1
        or manifest.get("layout") != "packed_fixed"
        or manifest.get("measurement") != "derived_build_only"
    ):
        raise ValueError(f"{dataset}: source campaign protocol mismatch")
    if expected_source_tree_sha is not None:
        validate_sha(expected_source_tree_sha, "expected source tree SHA")
        source = manifest.get("source")
        if not isinstance(source, dict):
            raise ValueError(f"{dataset}: source campaign lacks source-tree identity")
        if str(source.get("tree_sha256", "")) != expected_source_tree_sha:
            raise ValueError(f"{dataset}: source tree SHA mismatch")
        if (
            int(source.get("file_count", 0)) <= 0
            or source.get("layout") not in {"repository", "graphbeyond_project"}
            or not isinstance(source.get("tree_scope"), list)
            or not source["tree_scope"]
        ):
            raise ValueError(f"{dataset}: invalid source-tree identity")
    admission = read_construction_admission(
        manifest,
        dataset,
        expected_admission_gate_sha,
        expected_admission_scope,
    )
    return raw, manifest, admission


def discover_complete_runs(raw: Path, dataset: str) -> list[tuple[int, Path, Path, Path]]:
    runs: list[tuple[int, Path, Path, Path]] = []
    for repeat in range(REPEATS):
        json_path = raw / f"{dataset}_r{repeat}.json"
        err_path = raw / f"{dataset}_r{repeat}.err"
        mn_err_path = raw / f"{dataset}_r{repeat}.mn.err"
        if not all(path.is_file() and path.stat().st_size > 0 for path in (json_path, err_path, mn_err_path)):
            raise ValueError(f"{dataset}: five complete repeats are required; repeat {repeat} is incomplete")
        parsed = build_summary.parse_run(json_path)
        if parsed.dataset != dataset or parsed.repeat != repeat:
            raise ValueError(f"{dataset}: repeat identity mismatch in {json_path}")
        runs.append((repeat, json_path, err_path, mn_err_path))
    return runs


def copy_regular_tree(source: Path, destination: Path) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for path in sorted(source.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"refusing symlink in retained evidence: {path}")
        if not path.is_file():
            continue
        relative = path.relative_to(source)
        retained = destination / relative
        retained.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, retained)
        records.append(
            {
                "source": str(path.resolve()),
                "retained": retained.as_posix(),
                "sha256": file_sha256(retained),
                "size_bytes": retained.stat().st_size,
            }
        )
    return records


def rewrite_paths(rows: list[dict[str, object]], staging: Path, out_dir: Path) -> None:
    source_prefix = str(staging)
    final_prefix = str(out_dir)
    for row in rows:
        for key, value in row.items():
            if isinstance(value, str):
                row[key] = value.replace(source_prefix, final_prefix)


def write_sha256s(root: Path) -> None:
    output = root / "SHA256SUMS"
    paths = sorted(path for path in root.rglob("*") if path.is_file() and path != output)
    output.write_text(
        "".join(
            f"{file_sha256(path)}  {path.relative_to(root).as_posix()}\n"
            for path in paths
        )
    )


def assemble(
    sift_campaign: Path,
    deep_campaign: Path,
    gist_campaign: Path,
    out_dir: Path,
    *,
    expected_binary_sha: str,
    expected_source_tree_sha: str | None = None,
    expected_admission_gate_sha: str | None = None,
    expected_admission_scope: str | None = None,
    excluded_campaigns: list[Path] | None = None,
) -> None:
    validate_sha(expected_binary_sha, "expected binary SHA")
    if expected_source_tree_sha is not None:
        validate_sha(expected_source_tree_sha, "expected source tree SHA")
    if (expected_admission_gate_sha is None) != (expected_admission_scope is None):
        raise ValueError("expected admission gate SHA and scope must be set together")
    if expected_admission_gate_sha is not None:
        validate_sha(expected_admission_gate_sha, "expected admission gate SHA")
    if out_dir.exists():
        raise ValueError(f"output already exists: {out_dir}")
    assembler_path = Path(__file__).resolve()
    assembler_sha = file_sha256(assembler_path)
    summary_path = Path(build_summary.__file__).resolve()
    summary_sha = file_sha256(summary_path)
    campaign_roots = {
        "SIFT1M": sift_campaign,
        "DEEP1M": deep_campaign,
        "GIST1M": gist_campaign,
    }
    inputs: dict[
        str,
        tuple[
            Path,
            dict[str, object],
            dict[str, object] | None,
            list[tuple[int, Path, Path, Path]],
        ],
    ] = {}
    for dataset in DATASETS:
        raw, manifest, admission = read_campaign(
            campaign_roots[dataset], dataset, expected_binary_sha,
            expected_source_tree_sha,
            expected_admission_gate_sha,
            expected_admission_scope,
        )
        inputs[dataset] = (
            raw,
            manifest,
            admission,
            discover_complete_runs(raw, dataset),
        )
    admissions = [inputs[dataset][2] for dataset in DATASETS]
    admission = admissions[0]
    if any(item != admission for item in admissions[1:]):
        raise ValueError("source campaigns do not share one construction admission")

    out_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = out_dir.parent / f".{out_dir.name}.staging.{os.getpid()}"
    if staging.exists():
        raise ValueError(f"staging path already exists: {staging}")
    staging.mkdir()
    try:
        raw_out = staging / "raw"
        source_out = staging / "provenance" / "source_campaigns"
        raw_out.mkdir(parents=True)
        source_records: list[dict[str, object]] = []
        retained_runs: list[dict[str, object]] = []
        for dataset in DATASETS:
            raw, manifest, _, runs = inputs[dataset]
            manifest_dir = source_out / dataset
            manifest_dir.mkdir(parents=True)
            copied_manifests: list[dict[str, str]] = []
            for source_manifest in sorted(raw.glob("campaign*.json")):
                retained_manifest = manifest_dir / source_manifest.name
                shutil.copy2(source_manifest, retained_manifest)
                copied_manifests.append(
                    {
                        "path": retained_manifest.relative_to(staging).as_posix(),
                        "sha256": file_sha256(retained_manifest),
                    }
                )
            primary = manifest_dir / "campaign.json"
            source_record = {
                    "dataset": dataset,
                    "campaign_id": str(manifest.get("campaign_id", "")),
                    "source_root": str(campaign_roots[dataset].resolve()),
                    "retained_manifest": primary.relative_to(staging).as_posix(),
                    "retained_manifest_sha256": file_sha256(primary),
                    "runner_script_sha256": str(manifest["script_sha256"]),
                    "manifests": copied_manifests,
                }
            if expected_source_tree_sha is not None:
                source_record["source_tree_sha256"] = expected_source_tree_sha
            if admission is not None:
                source_record["admission_gate_sha256"] = admission["sha256"]
                source_record["admission_scope"] = admission["scope"]
            source_records.append(source_record)
            for repeat, json_path, err_path, mn_err_path in runs:
                for kind, source in (
                    ("json", json_path),
                    ("err", err_path),
                    ("mn_err", mn_err_path),
                ):
                    retained = raw_out / source.name
                    shutil.copy2(source, retained)
                    retained_runs.append(
                        {
                            "dataset": dataset,
                            "repeat": repeat,
                            "kind": kind,
                            "source": str(source.resolve()),
                            "retained": retained.relative_to(staging).as_posix(),
                            "sha256": file_sha256(retained),
                            "size_bytes": retained.stat().st_size,
                        }
                    )

        excluded_records: list[dict[str, object]] = []
        for index, excluded in enumerate(excluded_campaigns or []):
            if not excluded.is_dir():
                raise ValueError(f"excluded campaign is not a directory: {excluded}")
            destination = staging / "provenance" / "excluded" / f"{index:02d}_{excluded.name}"
            records = copy_regular_tree(excluded, destination)
            for record in records:
                record["retained"] = Path(str(record["retained"])).relative_to(staging).as_posix()
            excluded_records.append(
                {
                    "source_root": str(excluded.resolve()),
                    "retained_root": destination.relative_to(staging).as_posix(),
                    "files": records,
                }
            )

        retained_admission = None
        if admission is not None:
            admission_dir = staging / "provenance" / "admission"
            inputs_dir = admission_dir / "inputs"
            inputs_dir.mkdir(parents=True)
            source_gate = Path(str(admission["path"]))
            retained_gate = admission_dir / "construction_gate.json"
            shutil.copy2(source_gate, retained_gate)
            retained_inputs: dict[str, dict[str, str]] = {}
            for name in ADMISSION_INPUTS:
                record = admission["inputs"][name]
                source = Path(str(record["path"]))
                suffix = source.suffix if source.suffix else ".dat"
                retained = inputs_dir / f"{name}{suffix}"
                shutil.copy2(source, retained)
                retained_inputs[name] = {
                    "source": str(source),
                    "retained": retained.relative_to(staging).as_posix(),
                    "sha256": str(record["sha256"]),
                }
            retained_admission = {
                "kind": admission["kind"],
                "scope": admission["scope"],
                "construction_ready": True,
                "general_promotion_ready": False,
                "source_gate": str(source_gate),
                "retained_gate": retained_gate.relative_to(staging).as_posix(),
                "gate_sha256": admission["sha256"],
                "inputs": retained_inputs,
            }

        provenance = {
            "kind": "vldb_build_cost_provenance_v1",
            "assembled_utc": datetime.now(timezone.utc).isoformat(),
            "assembler": {
                "path": str(assembler_path),
                "sha256": assembler_sha,
            },
            "summarizer": {
                "path": str(summary_path),
                "sha256": summary_sha,
            },
            "source_campaigns": source_records,
            "retained_runs": retained_runs,
            "excluded_campaigns": excluded_records,
            "admission": retained_admission,
        }
        provenance_path = staging / "PROVENANCE.json"
        provenance_path.write_text(json.dumps(provenance, indent=2, sort_keys=True) + "\n")

        source_ids = "\n".join(str(item["campaign_id"]) for item in source_records)
        bundle_id = hashlib.sha256(source_ids.encode()).hexdigest()[:12]
        campaign = {
            "kind": "vldb_build_cost_bundle_v1",
            "campaign_id": f"vldb-build-cost-bundle-{bundle_id}",
            "binary_sha256": expected_binary_sha,
            "script_sha256": assembler_sha,
            "script_role": "bundle_assembler",
            "summary_script_sha256": summary_sha,
            "repeats": REPEATS,
            "datasets": list(DATASETS),
            "builder_threads": 20,
            "query_threads": 1,
            "query_coroutines": 1,
            "layout": "packed_fixed",
            "measurement": "derived_build_only",
            "source_campaigns": [
                {
                    "dataset": item["dataset"],
                    "campaign_id": item["campaign_id"],
                    "retained_manifest": item["retained_manifest"],
                    "retained_manifest_sha256": item["retained_manifest_sha256"],
                    "runner_script_sha256": item["runner_script_sha256"],
                    **(
                        {
                            "admission_gate_sha256": item[
                                "admission_gate_sha256"
                            ],
                            "admission_scope": item["admission_scope"],
                        }
                        if admission is not None
                        else {}
                    ),
                }
                for item in source_records
            ],
            "provenance_path": "PROVENANCE.json",
            "provenance_sha256": file_sha256(provenance_path),
            "admission": None,
        }
        if retained_admission is not None:
            campaign["admission"] = {
                "kind": retained_admission["kind"],
                "scope": retained_admission["scope"],
                "construction_ready": True,
                "general_promotion_ready": False,
                "retained_gate": retained_admission["retained_gate"],
                "gate_sha256": retained_admission["gate_sha256"],
                "inputs": {
                    name: {
                        "retained": record["retained"],
                        "sha256": record["sha256"],
                    }
                    for name, record in retained_admission["inputs"].items()
                },
            }
        if expected_source_tree_sha is not None:
            campaign["source_tree_sha256"] = expected_source_tree_sha
            for source in campaign["source_campaigns"]:
                source["source_tree_sha256"] = expected_source_tree_sha
        (raw_out / "campaign.json").write_text(
            json.dumps(campaign, indent=2, sort_keys=True) + "\n"
        )

        parsed = build_summary.collect_runs(raw_out)
        grouped = build_summary.validate_matrix(parsed, list(DATASETS), REPEATS)
        run_rows, summary_rows, stage_rows = build_summary.summarize(grouped)
        rewrite_paths(run_rows, staging, out_dir)
        rewrite_paths(summary_rows, staging, out_dir)
        rewrite_paths(stage_rows, staging, out_dir)
        build_summary.write_csv(staging / "runs.csv", run_rows)
        build_summary.write_csv(staging / "summary.csv", summary_rows)
        build_summary.write_csv(staging / "stage_breakdown.csv", stage_rows)
        build_summary.write_readme(staging / "README.md", summary_rows)
        with (staging / "README.md").open("a") as handle:
            handle.write(
                "Final promotion is performed by `assemble_vldb_build_cost.py`; "
                "`PROVENANCE.json` records the assembler, summarizer, source "
                "campaigns, exclusions, and every retained run file.\n"
            )
        write_sha256s(staging)
        staging.rename(out_dir)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sift-campaign", type=Path, required=True)
    parser.add_argument("--deep-campaign", type=Path, required=True)
    parser.add_argument("--gist-campaign", type=Path, required=True)
    parser.add_argument("--excluded-campaign", type=Path, action="append", default=[])
    parser.add_argument("--expected-binary-sha", required=True)
    parser.add_argument("--expected-source-tree-sha")
    parser.add_argument("--expected-admission-gate-sha")
    parser.add_argument("--expected-admission-scope")
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args()
    assemble(
        args.sift_campaign,
        args.deep_campaign,
        args.gist_campaign,
        args.out_dir,
        expected_binary_sha=args.expected_binary_sha,
        expected_source_tree_sha=args.expected_source_tree_sha,
        expected_admission_gate_sha=args.expected_admission_gate_sha,
        expected_admission_scope=args.expected_admission_scope,
        excluded_campaigns=args.excluded_campaign,
    )
    print(f"assembled final VLDB build-cost bundle: {args.out_dir}")


if __name__ == "__main__":
    main()
