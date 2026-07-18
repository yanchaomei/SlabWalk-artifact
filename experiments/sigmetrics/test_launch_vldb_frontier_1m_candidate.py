import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "experiments" / "sigmetrics" / "launch_vldb_frontier_1m_candidate.sh"


class VldbFrontier1mCandidateLauncherTest(unittest.TestCase):
    def test_launcher_is_fail_closed_and_uses_the_formal_seven_dataset_grid(self):
        text = SCRIPT.read_text()
        required = (
            'EXPECTED_BINARY_SHA=${EXPECTED_BINARY_SHA:?',
            'GB_BIN=${GB_BIN:?',
            'OUT_ROOT=${OUT_ROOT:?',
            '[[ "$(hostname)" == "skv-node1" ]]',
            'PHASES=sw',
            'REPEATS=5',
            'DATASETS_SW="SIFT1M GIST1M DEEP1M BIGANN1M SPACEV1M TURING1M TEXT1M"',
            'SIFT1_EFS="48 64 80 100 150"',
            'GIST1_EFS="100 200 300 400 600"',
            'DEEP1_EFS="30 50 80 100 150"',
            'BIGANN1_EFS="48 64 80 100 150"',
            'SPACEV1_EFS="100 200 300 400 600"',
            'TURING1_EFS="200 400 600 900 1200"',
            'TEXT1_EFS="100 150 200 300 500"',
            'exec bash "$SCRIPT_DIR/run_frontier_repeated.sh"',
        )
        for token in required:
            self.assertIn(token, text)

    def test_launcher_checks_the_candidate_bytes_before_creating_evidence(self):
        text = SCRIPT.read_text()
        sha_check = text.index('sha256sum "$GB_BIN"')
        mkdir = text.index('mkdir -p "$(dirname "$OUT_ROOT")"')
        self.assertLess(sha_check, mkdir)
        self.assertIn('[[ ! -e "$OUT_ROOT" ]]', text)


if __name__ == "__main__":
    unittest.main()
