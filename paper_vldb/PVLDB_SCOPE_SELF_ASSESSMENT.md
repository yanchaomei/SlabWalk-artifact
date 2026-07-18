# SlabWalk PVLDB Scope Self-Assessment

This note is a submission-form aid, not manuscript text.  Recheck section and
figure numbers after the final evidence-driven rewrite.

## Core Data-Management Contribution

SlabWalk introduces an expansion-oriented physical access structure for a
graph-based vector index whose authoritative state resides in disaggregated
memory.  The contribution is a change in physical organization and access
path: conventional systems retrieve either individual node/vector records or
routed graph partitions, whereas SlabWalk materializes the data required by one
HNSW expansion as one remotely retrievable record.  This organization changes
access amplification, storage amplification, query execution, construction,
and deployment policy without changing the logical HNSW graph.

The data-management problem and abstraction appear in Sections 1--3.  The
record organization, access path, ownership rules, and construction boundary
appear in Sections 4--6.  Section 7 evaluates query frontiers, access cost,
storage and memory accounting, scale-out placement, construction, and offline
rematerialization.

## Connection to Database Research

The paper builds on graph-based vector access methods and vector-database
systems, including HNSW, DiskANN, SPANN, Starling, Milvus, and Manu.  It
distinguishes SSD page/block organizations from disaggregated-memory access
units and compares three remote physical organizations: node/vector access
(SHINE-derived), routed partitions (d-HNSW), and expansion records (SlabWalk).
Related work in Section 8 is organized by physical unit and remedy rather than
by hardware alone.

## Evaluation in a Data-Management Context

The evaluation uses vector-search workloads and reports recall--throughput
frontiers under a common 10K query pool, not only RDMA microbenchmarks.  It also
accounts for derived-index bytes, compute-node DRAM, passive-memory placement,
construction time, query tail latency, remote operations, transferred bytes,
and scoped refresh cost.  The final primary comparison contains repeated
DEEP10M, SIFT10M, and TTI10M frontiers for all three physical organizations;
independent RDMA controls validate the cost regime in which the access
structure operates.

## Suggested CMT SAR Text

SlabWalk addresses the physical design of graph-based vector indexes over
disaggregated memory.  Its central contribution is an expansion-oriented
physical access structure derived from an authoritative HNSW index: one remote
record contains the neighbor identities, scoring payloads, and rerank pointers
required by one atomic graph decision.  This changes the storage organization
and query access path, reducing access amplification while explicitly bounding
storage, compute-node memory, network traffic, construction, and offline
rematerialization cost.  Sections 2--6 define the abstraction, cost model,
record layout, query processing, placement, and lifecycle boundary.  Section 7
compares node/vector, routed-partition, and expansion organizations on repeated
vector-search workloads and closes the query-performance results against
physical resource accounting.  Section 8 connects the work to HNSW, vector
database systems, and page-, node-, and partition-oriented vector indexes.
