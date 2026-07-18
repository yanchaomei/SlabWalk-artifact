import csv
import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[2]
GENERATOR_PATH = REPO_ROOT / "paper_vldb" / "figs" / "gen_vldb_design_figures.py"
SPEC = importlib.util.spec_from_file_location("gen_vldb_design_figures_cache", GENERATOR_PATH)
GENERATOR = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(GENERATOR)


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


class PhysicalUnitsCacheSweepTest(unittest.TestCase):
    def test_figure_consumes_and_labels_the_complete_cache_sweep(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache = root / "cache.csv"
            profile = root / "profile.csv"
            write_csv(cache, [
                {
                    "condition": condition,
                    "cache_ratio_pct": ratio,
                    "n": 5,
                    "posts_per_query_mean": posts,
                    "posts_per_query_ci95": posts_ci,
                    "qps_mean": qps,
                    "qps_ci95": qps_ci,
                }
                for condition, ratio, posts, posts_ci, qps, qps_ci in (
                    ("off", 0, 1000, 10, 700, 7),
                    ("c5", 5, 600, 12, 660, 6),
                    ("c20", 20, 400, 8, 610, 5),
                    ("c50", 50, 250, 5, 525, 4),
                )
            ])
            write_csv(profile, [{
                "dataset": "SIFT1M",
                "method": "SHINE-derived",
                "query_rows": 200000,
                "lost_samples": 0,
                "distance_symbol": "l2",
                "distance_self_percent": 18.25,
            }])

            with mock.patch.object(GENERATOR, "CACHE_CSV", cache), mock.patch.object(
                GENERATOR, "PROFILE_CSV", profile
            ):
                ratios, posts, posts_ci, qps, qps_ci, useful = (
                    GENERATOR.measured_cache_control()
                )
                svg = GENERATOR.physical_units()

            self.assertEqual(ratios, (0, 5, 20, 50))
            self.assertEqual(posts, (1000.0, 600.0, 400.0, 250.0))
            self.assertEqual(posts_ci, (10.0, 12.0, 8.0, 5.0))
            self.assertEqual(qps, (700.0, 660.0, 610.0, 525.0))
            self.assertEqual(qps_ci, (7.0, 6.0, 5.0, 4.0))
            self.assertAlmostEqual(useful, 18.25)
            self.assertIn("Measured motivation: same path, more cache", svg)
            self.assertIn("n=5; 95% CI", svg)
            self.assertIn("75.0% fewer posts/query", svg)
            self.assertIn("25.0% lower QPS", svg)
            self.assertIn("cache budget (%)", svg)
            self.assertIn("useful distance: 18.2%", svg)
            self.assertIn("other CN work: 81.8%", svg)


if __name__ == "__main__":
    unittest.main()
