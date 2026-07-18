#!/usr/bin/env bash
# Prepare BIGANN first-10M in the GraphBeyond/SlabWalk binary format.
#
# Output layout:
#   $GB_DATA/sift10m/base.fbin
#   $GB_DATA/sift10m/queries/query-uniform.fbin
#   $GB_DATA/sift10m/queries/groundtruth-uniform.bin
#
# The script first reuses the d-HNSW-prepared SIFT10M files when present,
# then falls back to streaming BIGANN downloads.  It is intended to run on
# skv-node1 before building the GraphBeyond index dump.
set -euo pipefail

GB_DATA=${GB_DATA:-/home/kvgroup/chaomei/hnsw-data}
DHSIFT_DIR=${DHSIFT_DIR:-/home/kvgroup/chaomei/d-HNSW/datasets/sift10M}
OUT_DIR=${OUT_DIR:-$GB_DATA/sift10m}
BIGANN_URL_BASE=${BIGANN_URL_BASE:-ftp://ftp.irisa.fr/local/texmex/corpus}

mkdir -p "$OUT_DIR/queries" "$OUT_DIR/raw"

have_outputs() {
  [[ -s "$OUT_DIR/base.fbin" && -s "$OUT_DIR/queries/query-uniform.fbin" && -s "$OUT_DIR/queries/groundtruth-uniform.bin" ]]
}

if have_outputs; then
  echo "SIFT10M GraphBeyond files already exist under $OUT_DIR"
  exit 0
fi

python3 - "$OUT_DIR" "$DHSIFT_DIR" "$BIGANN_URL_BASE" <<'PY'
import gzip
import os
import shutil
import struct
import subprocess
import sys
import tarfile
from pathlib import Path

import numpy as np

out = Path(sys.argv[1])
dh = Path(sys.argv[2])
base_url = sys.argv[3].rstrip("/")
queries = out / "queries"
raw = out / "raw"
out.mkdir(parents=True, exist_ok=True)
queries.mkdir(parents=True, exist_ok=True)
raw.mkdir(parents=True, exist_ok=True)


def stream_fvecs_to_fbin(src: Path, dst: Path) -> None:
    if dst.exists() and dst.stat().st_size > 0:
        return
    size = src.stat().st_size
    with src.open("rb") as f:
        head = f.read(4)
        if len(head) != 4:
            raise SystemExit(f"empty fvecs file: {src}")
        dim = struct.unpack("<i", head)[0]
        vec_size = 4 + dim * 4
        if size % vec_size != 0:
            raise SystemExit(f"{src} size {size} is not divisible by fvec size {vec_size}")
        n = size // vec_size
        f.seek(0)
        with dst.open("wb") as g:
            g.write(struct.pack("<II", n, dim))
            for i in range(n):
                d = struct.unpack("<i", f.read(4))[0]
                if d != dim:
                    raise SystemExit(f"dimension mismatch in {src} at vector {i}: {d} != {dim}")
                g.write(f.read(dim * 4))
                if i and i % 1_000_000 == 0:
                    print(f"converted {i}/{n} fvecs from {src.name}", flush=True)
    print(f"wrote {dst} ({n} x {dim})")


def stream_ivecs_to_ibin(src: Path, dst: Path) -> None:
    if dst.exists() and dst.stat().st_size > 0:
        return
    size = src.stat().st_size
    with src.open("rb") as f:
        head = f.read(4)
        if len(head) != 4:
            raise SystemExit(f"empty ivecs file: {src}")
        dim = struct.unpack("<i", head)[0]
        vec_size = 4 + dim * 4
        if size % vec_size != 0:
            raise SystemExit(f"{src} size {size} is not divisible by ivec size {vec_size}")
        n = size // vec_size
        f.seek(0)
        with dst.open("wb") as g:
            g.write(struct.pack("<II", n, dim))
            for i in range(n):
                d = struct.unpack("<i", f.read(4))[0]
                if d != dim:
                    raise SystemExit(f"dimension mismatch in {src} at query {i}: {d} != {dim}")
                g.write(f.read(dim * 4))
    print(f"wrote {dst} ({n} x {dim})")


def stream_bigann_base_to_fbin(dst: Path, limit: int = 10_000_000) -> None:
    if dst.exists() and dst.stat().st_size > 0:
        return
    url = f"{base_url}/bigann_base.bvecs.gz"
    print(f"streaming first {limit} BIGANN base vectors from {url}", flush=True)
    curl = subprocess.Popen(["curl", "-L", "--fail", url], stdout=subprocess.PIPE)
    assert curl.stdout is not None
    count = 0
    dim = None
    try:
        with gzip.GzipFile(fileobj=curl.stdout) as gz, dst.open("wb") as fout:
            # BIGANN SIFT is fixed 128-D; write the GraphBeyond header first.
            fout.write(struct.pack("<II", limit, 128))
            while count < limit:
                raw_dim = gz.read(4)
                if not raw_dim:
                    break
                if len(raw_dim) != 4:
                    raise SystemExit(f"truncated bvec header at vector {count}")
                cur_dim = struct.unpack("<i", raw_dim)[0]
                if dim is None:
                    dim = cur_dim
                    if dim != 128:
                        raise SystemExit(f"unexpected BIGANN dimension {dim}")
                elif cur_dim != dim:
                    raise SystemExit(f"dimension mismatch at vector {count}: {cur_dim} != {dim}")
                vec = gz.read(dim)
                if len(vec) != dim:
                    raise SystemExit(f"truncated bvec payload at vector {count}")
                arr = np.frombuffer(vec, dtype=np.uint8).astype(np.float32)
                fout.write(arr.tobytes())
                count += 1
                if count % 1_000_000 == 0:
                    print(f"streamed {count}/{limit} base vectors", flush=True)
    finally:
        if curl.poll() is None:
            curl.terminate()
            try:
                curl.wait(timeout=5)
            except subprocess.TimeoutExpired:
                curl.kill()
    if count != limit:
        raise SystemExit(f"expected {limit} base vectors, got {count}")
    print(f"wrote {dst} ({count} x 128)")


def download_query_to_fbin(dst: Path) -> None:
    if dst.exists() and dst.stat().st_size > 0:
        return
    url = f"{base_url}/bigann_query.bvecs.gz"
    bvecs = raw / "bigann_query.bvecs"
    if not bvecs.exists():
        print(f"downloading {url}", flush=True)
        curl = subprocess.Popen(["curl", "-L", "--fail", url], stdout=subprocess.PIPE)
        assert curl.stdout is not None
        with gzip.GzipFile(fileobj=curl.stdout) as gz, bvecs.open("wb") as f:
            shutil.copyfileobj(gz, f)
        if curl.wait() != 0:
            raise SystemExit("curl failed for bigann_query.bvecs.gz")
    # Convert bvecs directly into GraphBeyond fbin.
    size = bvecs.stat().st_size
    with bvecs.open("rb") as f:
        dim = struct.unpack("<i", f.read(4))[0]
        vec_size = 4 + dim
        n = size // vec_size
        f.seek(0)
        with dst.open("wb") as g:
            g.write(struct.pack("<II", n, dim))
            for i in range(n):
                d = struct.unpack("<i", f.read(4))[0]
                if d != dim:
                    raise SystemExit(f"query dim mismatch at {i}: {d} != {dim}")
                vec = f.read(dim)
                np.frombuffer(vec, dtype=np.uint8).astype(np.float32).tofile(g)
    print(f"wrote {dst} ({n} x {dim})")


def ensure_groundtruth(dst: Path) -> None:
    if dst.exists() and dst.stat().st_size > 0:
        return
    candidates = [
        dh / "gnd" / "idx_10M.ivecs",
        out / "gnd" / "idx_10M.ivecs",
        raw / "gnd" / "idx_10M.ivecs",
    ]
    src = next((p for p in candidates if p.exists()), None)
    if src is None:
        tar_path = raw / "bigann_gnd.tar.gz"
        if not tar_path.exists():
            url = f"{base_url}/bigann_gnd.tar.gz"
            print(f"downloading {url}", flush=True)
            subprocess.check_call(["curl", "-L", "--fail", "-o", str(tar_path), url])
        with tarfile.open(tar_path) as tar:
            tar.extractall(raw)
        src = next((p for p in candidates if p.exists()), None)
        if src is None:
            found = list(raw.rglob("idx_10M.ivecs"))
            if found:
                src = found[0]
    if src is None:
        raise SystemExit("could not find idx_10M.ivecs after ground-truth preparation")
    stream_ivecs_to_ibin(src, dst)


base_out = out / "base.fbin"
query_out = queries / "query-uniform.fbin"
gt_out = queries / "groundtruth-uniform.bin"

if (dh / "bigann_base.fvecs").exists():
    stream_fvecs_to_fbin(dh / "bigann_base.fvecs", base_out)
else:
    stream_bigann_base_to_fbin(base_out)

if (dh / "bigann_query.fvecs").exists():
    stream_fvecs_to_fbin(dh / "bigann_query.fvecs", query_out)
else:
    download_query_to_fbin(query_out)

ensure_groundtruth(gt_out)

for path in [base_out, query_out, gt_out]:
    if not path.exists() or path.stat().st_size == 0:
        raise SystemExit(f"missing output {path}")
print("SIFT10M GraphBeyond preparation complete")
PY
