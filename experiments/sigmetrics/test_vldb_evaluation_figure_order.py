import re
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
MAIN_TEX = REPO_ROOT / "paper_vldb" / "main.tex"


class VldbEvaluationFigureOrderTest(unittest.TestCase):
    def test_each_control_figure_follows_the_question_it_answers(self) -> None:
        tex = MAIN_TEX.read_text()
        q1 = tex.index(r"\subsection{Q1: End-to-End Frontier}")
        q2 = tex.index(r"\subsection{Q2: Access Cost and Scaling}")
        q3 = tex.index(r"\subsection{Q3: Index Cost and Deployment}")
        q4 = tex.index(
            r"\subsection{Q4: Construction, Offline Refresh, and Boundaries}"
        )
        access = tex.index(r"\label{fig:eval-scaling-ablation}")
        index_cost = tex.index(r"\label{fig:eval-index-cost}")
        lifecycle = tex.index(r"\label{fig:eval-boundary}")

        self.assertLess(q1, q2)
        self.assertLess(q2, access)
        self.assertLess(access, q3)
        self.assertLess(q3, index_cost)
        self.assertLess(index_cost, q4)
        self.assertLess(q4, lifecycle)

    def test_figure_widths_preserve_the_visual_hierarchy(self) -> None:
        tex = MAIN_TEX.read_text()
        widths = {
            path: float(width)
            for width, path in re.findall(
                r"\\includegraphics\[width=([0-9.]+)\\textwidth\]"
                r"\{figs/([^}]+)\}",
                tex,
            )
        }

        for name in (
            "fig_physical_units.pdf",
            "overview.pdf",
            "fig_slab_layout.pdf",
            "fig_search_placement.pdf",
            "fig_construction_refresh.pdf",
        ):
            with self.subTest(name=name):
                self.assertGreaterEqual(widths[name], 0.84)
                self.assertLessEqual(widths[name], 0.88)

        self.assertEqual(widths["eval_frontier_curves.pdf"], 0.94)
        self.assertEqual(widths["eval_access_scaling.pdf"], 0.84)
        self.assertEqual(widths["eval_index_cost.pdf"], 0.84)
        self.assertEqual(widths["eval_lifecycle_boundaries.pdf"], 0.84)

    def test_lifecycle_figure_follows_q4_without_blocking_related_text(self) -> None:
        tex = MAIN_TEX.read_text()
        q4 = tex.index(
            r"\subsection{Q4: Construction, Offline Refresh, and Boundaries}"
        )
        lifecycle = tex.index("figs/eval_lifecycle_boundaries.pdf")
        related = tex.index(r"\section{Related Work}")
        self.assertLess(q4, lifecycle)
        self.assertLess(lifecycle, related)
        self.assertNotIn(r"\FloatBarrier", tex[lifecycle:related])


if __name__ == "__main__":
    unittest.main()
