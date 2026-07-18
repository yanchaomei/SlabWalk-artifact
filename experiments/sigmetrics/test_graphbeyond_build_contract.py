import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[2]


class GraphBeyondBuildContractTest(unittest.TestCase):
    def test_cmake_fails_early_for_required_cpp20_and_onetbb_features(self):
        top = (ROOT / "graphbeyond" / "CMakeLists.txt").read_text()
        rdma = (ROOT / "graphbeyond" / "rdma-library" / "CMakeLists.txt").read_text()

        self.assertIn("GB_HAS_ATOMIC_SHARED_PTR", top)
        self.assertIn("std::atomic<std::shared_ptr<int>>", top)
        self.assertIn("GB_HAS_ONETBB_HEADERS", rdma)
        self.assertIn("oneapi/tbb/concurrent_queue.h", rdma)

    def test_cmake_links_an_onetbb_abi_probe_before_building_targets(self):
        rdma = (ROOT / "graphbeyond" / "rdma-library" / "CMakeLists.txt").read_text()

        self.assertIn("find_library(GB_TBB_LIBRARY", rdma)
        self.assertIn("CMAKE_REQUIRED_LIBRARIES", rdma)
        self.assertIn("GB_HAS_ONETBB_ABI", rdma)
        self.assertIn("oneapi::tbb::concurrent_vector<int>", rdma)
        self.assertIn("graphbeyond_tbb", rdma)

    def test_documented_build_pins_the_validated_compiler(self):
        readme = (ROOT / "graphbeyond" / "README.md").read_text()
        self.assertIn("-DCMAKE_CXX_COMPILER=g++-12", readme)

    def test_exact_byte_rank_uses_the_deterministic_parallel_indegree_path(self):
        build = (ROOT / "graphbeyond" / "src" / "lavd" / "build.hh").read_text()

        self.assertIn('#include "lavd/parallel_indegree.hh"', build)
        self.assertIn("parallel_accumulate_indegree_u32", build)
        self.assertNotIn("const auto indegree = lavd::parallel_indegree_u32", build)

    def test_payload_accounting_fails_before_descriptor_commit(self):
        build = (ROOT / "graphbeyond" / "src" / "lavd" / "build.hh").read_text()

        accounting = build.index("LAVD-native: precommit payload-byte mismatch")
        commit = build.index("Header publication is the sidecar commit point")
        descriptor_write = build.index("write_descriptor(ph, native_descriptor)")
        self.assertLess(accounting, commit)
        self.assertLess(accounting, descriptor_write)
        self.assertNotIn(
            "actual/planned write-byte mismatch",
            build[descriptor_write:],
        )

    def test_physical_evidence_uses_versioned_field_scopes(self):
        evidence = (
            ROOT / "graphbeyond" / "src" / "common" / "evidence_hash.hh"
        ).read_text()
        build = (ROOT / "graphbeyond" / "src" / "lavd" / "build.hh").read_text()

        self.assertIn("PHYSICAL_HASH_VERSION = 2u", evidence)
        for field in (
            "header_hash_scope",
            "descriptor_hash_scope",
            "map_hash_scope",
            "offset_table_hash_scope",
            "record_payload_hash_scope",
            "selected_uid_hash_scope",
            "budget_map_owner_mn",
        ):
            self.assertIn(f'\\\"{field}\\\"', build)
        self.assertNotIn('\\\"hash_scope\\\":\\\"rdma_write_source_bytes\\\"', build)

    def test_staged_assembler_uses_aligned_worker_scratch(self):
        build = (ROOT / "graphbeyond" / "src" / "lavd" / "build.hh").read_text()

        self.assertIn("worker_record_scratch", build)
        self.assertIn("std::memcpy(stage + stage_offset", build)
        self.assertNotIn(
            "static_cast<size_t>(record_bytes),\n                      stage + stage_offset",
            build,
        )

    def test_single_mn_fixed_layout_has_parallel_staged_publication(self):
        build = (ROOT / "graphbeyond" / "src" / "lavd" / "build.hh").read_text()

        for token in (
            "staged_fixed_build",
            "next_fixed_staged_slot_range",
            "parallel_for_u32",
            "stage + static_cast<size_t>(relative) * stride",
            '\\\"mode\\\":\\\"staged_fixed\\\"',
            "region_range_fits(remote_offset, range.bytes",
            "!budget && !vb_on",
        ):
            self.assertIn(token, build)

    def test_initiator_reuses_the_validated_rabitq_rotation(self):
        build = (ROOT / "graphbeyond" / "src" / "lavd" / "build.hh").read_text()
        rabitq = (ROOT / "graphbeyond" / "src" / "lavd" / "rabitq.hh").read_text()

        for token in (
            "init_shared_reusing_rotation",
            "reusable.rotation_seed == seed",
            "reusable.P.size() == expected",
            "rotation_reused=",
        ):
            self.assertIn(token, build + rabitq)

    def test_single_mn_hands_the_authoritative_snapshot_to_resident_navigation(self):
        build = (ROOT / "graphbeyond" / "src" / "lavd" / "build.hh").read_text()
        single = build.split("inline Quantizer build_neighborhood(", 1)[1].split(
            "inline Quantizer build_neighborhood_multi(", 1
        )[0]

        for token in (
            "retain_authoritative_snapshot",
            "publish_authoritative_snapshot",
            "snapshot_shards.push_back(scratch)",
            "scratch = nullptr",
            "retained authoritative snapshot for resident upper graph: shards=1",
        ):
            self.assertIn(token, single)

    def test_full_budget_does_not_build_a_cold_fallback_reverse_index(self):
        build = (ROOT / "graphbeyond" / "src" / "lavd" / "build.hh").read_text()

        self.assertIn(
            "g_rev_index_per_mn.clear();\n  if (materialization_map_on) {",
            build,
        )

    def test_descriptor_abi_check_guards_stride_narrowing(self):
        build = (ROOT / "graphbeyond" / "src" / "lavd" / "build.hh").read_text()

        guard = build.index("descriptor ABI cannot represent the configured stride")
        match = build.index("descriptor_record_abi_matches", guard)
        self.assertLess(guard, match)

    def test_hardening_suites_are_registered_with_ctest(self):
        top = (ROOT / "graphbeyond" / "CMakeLists.txt").read_text()

        self.assertIn("include(CTest)", top)
        self.assertIn("-UNDEBUG", top)
        for name in (
            "evidence_hash_test",
            "materialization_policy_test",
            "native_descriptor_test",
            "staged_io_test",
            "query_result_fingerprint_test",
            "rabitq_rotation_reuse_test",
            "build_snapshot_test",
            "vldb_binary_ab_contract",
            "vldb_query_profile_contract",
            "graphbeyond_build_contract",
        ):
            self.assertIn(name, top)
        self.assertIn('ENVIRONMENT "PYTHONPATH=${GB_REPO_ROOT}"', top)


if __name__ == "__main__":
    unittest.main()
