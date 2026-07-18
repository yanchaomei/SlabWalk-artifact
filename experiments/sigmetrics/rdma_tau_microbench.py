#!/usr/bin/env python3
"""Run controlled RDMA-read microbenchmarks for the SIGMETRICS cost model.

The experiment is intentionally below the ANN stack.  It uses perftest's
`ib_read_lat` and `ib_read_bw` to hold the operation primitive fixed while
varying payload size, MTU, NUMA placement, QP count, CQ moderation, and
outstanding reads.  The output is a tidy CSV consumed by
`rdma_tau_plot.py`.
"""

from __future__ import annotations

import argparse
import csv
import re
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path


LAT_RE = re.compile(
    r"^\s*(?P<size>\d+)\s+(?P<iters>\d+)\s+"
    r"(?P<t_min>[0-9.]+)\s+(?P<t_max>[0-9.]+)\s+"
    r"(?P<t_typical>[0-9.]+)\s+(?P<t_avg>[0-9.]+)\s+"
    r"(?P<t_stdev>[0-9.]+)\s+(?P<p99>[0-9.]+)\s+(?P<p999>[0-9.]+)\s*$"
)
BW_RE = re.compile(
    r"^\s*(?P<size>\d+)\s+(?P<iters>\d+)\s+"
    r"(?P<bw_peak>[0-9.]+)\s+(?P<bw_avg>[0-9.]+)\s+(?P<msg_rate>[0-9.]+)\s*$"
)
HEADER_PATTERNS = {
    "qps": re.compile(r"Number of qps\s*:\s*(\d+)"),
    "mtu": re.compile(r"Mtu\s*:\s*(\d+)\[B\]"),
    "outs": re.compile(r"Outstand reads\s*:\s*(\d+)"),
    "cq_mod": re.compile(r"CQ Moderation\s*:\s*(\d+)"),
}


@dataclass(frozen=True)
class RunSpec:
    sweep: str
    label: str
    tool: str
    size: int
    iters: int
    mtu: int
    outs: int
    qps: int = 1
    cq_mod: int = 1
    client_numa: int = 1
    server_numa: int = 1
    notes: str = ""


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def ssh(host: str, command: str, timeout_s: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["ssh", "-o", "BatchMode=yes", host, command],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout_s,
    )


def remote_shell_command(spec: RunSpec, args: argparse.Namespace, *, server: bool) -> str:
    prefix = ["numactl", f"--preferred={spec.server_numa if server else spec.client_numa}"]
    common = [
        spec.tool,
        "-F",
        "-d",
        args.device,
        "-x",
        str(args.gid_index),
        "-s",
        str(spec.size),
        "-n",
        str(spec.iters),
        "-m",
        str(spec.mtu),
        "-o",
        str(spec.outs),
        "-p",
        str(args.port),
    ]
    if spec.tool == "ib_read_bw":
        common.extend(["-q", str(spec.qps), "-Q", str(spec.cq_mod), "--report_gbits"])
    if not server:
        common.append(args.mn_ip)
    return " ".join(shlex.quote(part) for part in [*prefix, *common])


def parse_header(output: str, key: str) -> str:
    match = HEADER_PATTERNS[key].search(output)
    return match.group(1) if match else ""


def parse_perftest(spec: RunSpec, output: str) -> dict[str, str]:
    row: dict[str, str] = {
        "avg_us": "",
        "p99_us": "",
        "p999_us": "",
        "stdev_us": "",
        "bw_peak_gbps": "",
        "bw_avg_gbps": "",
        "msg_rate_mpps": "",
        "reported_mtu": parse_header(output, "mtu"),
        "reported_outs": parse_header(output, "outs"),
        "reported_qps": parse_header(output, "qps"),
        "reported_cq_mod": parse_header(output, "cq_mod"),
    }
    pattern = LAT_RE if spec.tool == "ib_read_lat" else BW_RE
    for line in output.splitlines():
        match = pattern.match(line)
        if not match:
            continue
        data = match.groupdict()
        if spec.tool == "ib_read_lat":
            row.update(
                {
                    "avg_us": data["t_avg"],
                    "p99_us": data["p99"],
                    "p999_us": data["p999"],
                    "stdev_us": data["t_stdev"],
                }
            )
        else:
            row.update(
                {
                    "bw_peak_gbps": data["bw_peak"],
                    "bw_avg_gbps": data["bw_avg"],
                    "msg_rate_mpps": data["msg_rate"],
                }
            )
        return row
    raise ValueError(f"could not parse perftest output for {spec}")


def run_one(spec: RunSpec, rep: int, args: argparse.Namespace) -> dict[str, str]:
    args.port += 1
    session = f"sw_tau_{args.port}"
    server_log = f"/tmp/{session}.out"
    server_cmd = remote_shell_command(spec, args, server=True)
    client_cmd = remote_shell_command(spec, args, server=False)
    start_cmd = (
        f"tmux kill-session -t {shlex.quote(session)} 2>/dev/null; "
        f"rm -f {shlex.quote(server_log)}; "
        f"tmux new-session -d -s {shlex.quote(session)} "
        f"{shlex.quote(server_cmd + ' > ' + server_log + ' 2>&1')}"
    )
    stop_cmd = f"tmux kill-session -t {shlex.quote(session)} 2>/dev/null; cat {shlex.quote(server_log)} 2>/dev/null || true"

    if args.dry_run:
        print(f"[dry-run] {args.mn_host}: {server_cmd}")
        print(f"[dry-run] {args.cn_host}: {client_cmd}")
        return {}

    started = ssh(args.mn_host, start_cmd, args.ssh_timeout)
    if started.returncode != 0:
        raise RuntimeError(f"server start failed: {started.stderr.strip()}")
    time.sleep(args.server_warmup_s)
    client = ssh(args.cn_host, f"timeout {args.run_timeout_s} {client_cmd}", args.run_timeout_s + 10)
    server = ssh(args.mn_host, stop_cmd, args.ssh_timeout)
    if client.returncode != 0:
        raise RuntimeError(
            "client run failed\n"
            f"cmd: {client_cmd}\n"
            f"stdout:\n{client.stdout}\n"
            f"stderr:\n{client.stderr}\n"
            f"server:\n{server.stdout}\n{server.stderr}"
        )
    parsed = parse_perftest(spec, client.stdout)
    return {
        "sweep": spec.sweep,
        "label": spec.label,
        "rep": str(rep),
        "tool": spec.tool,
        "size": str(spec.size),
        "iters": str(spec.iters),
        "mtu": str(spec.mtu),
        "outs": str(spec.outs),
        "qps": str(spec.qps),
        "cq_mod": str(spec.cq_mod),
        "client_numa": str(spec.client_numa),
        "server_numa": str(spec.server_numa),
        "cn_host": args.cn_host,
        "mn_host": args.mn_host,
        "mn_ip": args.mn_ip,
        "device": args.device,
        "gid_index": str(args.gid_index),
        "port": str(args.port),
        "notes": spec.notes,
        **parsed,
    }


def build_specs(args: argparse.Namespace) -> list[RunSpec]:
    specs: list[RunSpec] = []

    for size in args.payload_sizes:
        specs.append(
            RunSpec(
                sweep="payload_latency",
                label=f"{size}B",
                tool="ib_read_lat",
                size=size,
                iters=args.lat_iters,
                mtu=4096,
                outs=16,
                notes="fixed one read/op; payload sweep",
            )
        )

    for mtu in [1024, 2048, 4096]:
        specs.append(
            RunSpec(
                sweep="mtu_latency",
                label=f"MTU {mtu}",
                tool="ib_read_lat",
                size=256,
                iters=args.lat_iters,
                mtu=mtu,
                outs=16,
                notes="fixed 256B read; MTU sweep",
            )
        )

    for numa in [0, 1]:
        specs.append(
            RunSpec(
                sweep="numa_latency",
                label=f"preferred {numa}",
                tool="ib_read_lat",
                size=256,
                iters=args.lat_iters,
                mtu=4096,
                outs=16,
                client_numa=numa,
                server_numa=numa,
                notes="same preferred NUMA node on CN and MN",
            )
        )

    for outs in [1, 4, 16]:
        specs.append(
            RunSpec(
                sweep="outs_msg_rate",
                label=f"outs {outs}",
                tool="ib_read_bw",
                size=256,
                iters=args.bw_iters,
                mtu=4096,
                outs=outs,
                qps=1,
                cq_mod=1,
                notes="fixed one QP; outstanding read sweep",
            )
        )

    for qps in [1, 2, 4, 8]:
        for cq_mod in [1, 16]:
            specs.append(
                RunSpec(
                    sweep="qp_cq_msg_rate",
                    label=f"{qps}QP CQ{cq_mod}",
                    tool="ib_read_bw",
                    size=256,
                    iters=args.bw_iters,
                    mtu=4096,
                    outs=16,
                    qps=qps,
                    cq_mod=cq_mod,
                    notes="fixed 256B read; QP/CQ-moderation sweep",
                )
            )
    return specs


def write_rows(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "sweep",
        "label",
        "rep",
        "tool",
        "size",
        "iters",
        "mtu",
        "outs",
        "qps",
        "cq_mod",
        "client_numa",
        "server_numa",
        "avg_us",
        "p99_us",
        "p999_us",
        "stdev_us",
        "bw_peak_gbps",
        "bw_avg_gbps",
        "msg_rate_mpps",
        "reported_mtu",
        "reported_outs",
        "reported_qps",
        "reported_cq_mod",
        "cn_host",
        "mn_host",
        "mn_ip",
        "device",
        "gid_index",
        "port",
        "notes",
    ]
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cn-host", default="SKV_1")
    parser.add_argument("--mn-host", default="SKV_4")
    parser.add_argument("--mn-ip", default="10.0.0.64")
    parser.add_argument("--device", default="mlx5_1")
    parser.add_argument("--gid-index", type=int, default=3)
    parser.add_argument("--port", type=int, default=19100)
    parser.add_argument("--reps", type=int, default=3)
    parser.add_argument("--lat-iters", type=int, default=5000)
    parser.add_argument("--bw-iters", type=int, default=50000)
    parser.add_argument("--payload-sizes", type=int, nargs="+", default=[64, 128, 256, 512, 1024, 2048, 4096, 8192, 16384])
    parser.add_argument("--out", type=Path, default=repo_root() / "results" / "sigmetrics_rdma_tau_microbench" / "rdma_tau_raw.csv")
    parser.add_argument("--server-warmup-s", type=float, default=1.0)
    parser.add_argument("--ssh-timeout", type=int, default=20)
    parser.add_argument("--run-timeout-s", type=int, default=60)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    specs = build_specs(args)
    rows: list[dict[str, str]] = []
    total = len(specs) * args.reps
    done = 0
    for rep in range(1, args.reps + 1):
        for spec in specs:
            done += 1
            print(f"[{done:03d}/{total:03d}] {spec.sweep} {spec.label} rep={rep}", flush=True)
            row = run_one(spec, rep, args)
            if row:
                rows.append(row)
                write_rows(args.out, rows)
    if rows:
        write_rows(args.out, rows)
        print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
