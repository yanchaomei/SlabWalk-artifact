# Authoritative 10M Graph Construction

This bundle records the authoritative HNSW graphs used by the graph-preserving
SIFT10M and TTI10M frontier paths. It is distinct from the repeated Slab
derived-build evidence in `../build_cost/`.

## Protocol

- Builder: hnswlib 0.8.0, `M=16`, `ef_construction=100`, seed 47.
- Labels equal source row IDs, so graph neighbors can be compared without a
  hidden relabeling table.
- SIFT10M uses squared L2; TTI10M uses inner product.
- One graph is built per dataset. SHINE and SlabWalk load the same converted
  GraphBeyond dump; d-HNSW retains its native partitioned index.
- The converter changes only the physical serialization. It checks every
  vector, level, neighbor ID, pointer target, unused slot, alignment byte, and
  file header after writing. Both manifests report `graph_preserved=true` and
  `post_write_validation=full_graph_payload_and_pointers`.

## Identities

| Dataset | Source SHA-256 | hnswlib SHA-256 | GraphBeyond dump SHA-256 | Dump bytes |
|---|---|---|---|---:|
| SIFT10M | `91846887bbede67c4f9ddb0c47617b44e2efa32007cb32f24262d0033a55784b` | `3d931fd5dfdfbdbcc9899468a3e387c429d8193a883b18f4a028312cfc01f2cc` | `0564d0851ad8239dfe38001c0eadeba7f37a912415b2fc92e37f35f7afda4975` | 8,005,659,304 |
| TTI10M | `b4f3cbdff1dbba0971fb2e1af7bc2a377e9597cfa349c1a141b1066e10df84f6` | `690b51020c4b63a66064b5887c2d236b7675352c2c0a4e1c10c0710c7b17a144` | `efa55196ce5420107683f07a577bc222f0b42ff0684887bc16f120ec6644cfb3` | 10,885,664,672 |

All `build.rc`, `conversion.rc`, and `pipeline.rc` files contain `0`. The large
binary dumps are not copied into this repository. Their canonical copies were
published on `skv-node2` by a temporary-file, full-SHA, no-overwrite protocol;
the formal frontier uses SHA-identical copies on `skv-node3` because node2's
128 GiB HugeTLB reservation is unavailable to the frozen `NOHUGEPAGES` MN
binary.
