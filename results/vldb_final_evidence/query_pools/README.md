# Query-Pool Evidence Contract

The primary frontier compares SHINE, SlabWalk, and d-HNSW on the same first
10,000 logical query and ground-truth rows for each 10M dataset.  Physical
formats differ by implementation (`fbin`/`bin` versus `fvecs`/`ivecs`), so a
byte-for-byte file comparison is insufficient.

Each `query_pool_fingerprint` manifest records two identities:

- `canonical_sha256` and `canonical_ids_sha256` hash normalized row content
  and must agree across all three systems for a dataset.
- `file_sha256` hashes the exact implementation-specific physical file and may
  differ across formats.

`aggregate_frontier_repeats.py --query-pools ...` joins the matching manifest
into every completed raw frontier row.  The final evidence validator checks
the manifest filename and SHA, source paths, canonical hashes, and physical
file hashes for every row before plotting.  This join is performed after the
campaign; it does not imply that the search binary emitted the hashes.

The final aggregation must copy each original campaign CSV below
`frontier/raw_sources/` and use `frontier/` as its output directory.  The
aggregator records contained inputs as relative paths.  The validator rejects
absolute paths, directory traversal, missing files, and any retained CSV whose
recomputed SHA-256 differs from the row-level source hash.

The final frontier provenance directory must also retain campaign manifests,
runner snapshots, and raw run files.  Those artifacts establish which path
mapping produced each campaign, while this directory establishes logical and
physical query-pool identity.  TTI10M additionally requires
`tti_exact_groundtruth_spotcheck.json`, which recomputes exact inner-product
top-10 sets for query rows 0, 4999, and 9999 before the campaign is accepted.
