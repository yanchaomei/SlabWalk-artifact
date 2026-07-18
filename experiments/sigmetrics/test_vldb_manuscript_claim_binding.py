from __future__ import annotations

import re
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
MAIN_TEX = REPO_ROOT / "paper_vldb" / "main.tex"

REQUIRED_CLAIM_MACROS = {
    "ClaimFrontierRecallFloor",
    "ClaimFrontierRecallTolerance",
    "ClaimFrontierQpsMin",
    "ClaimFrontierQpsMax",
    "ClaimFrontierPostsMin",
    "ClaimFrontierPostsMax",
    "ClaimDhnswDeepRecall",
    "ClaimDhnswSiftRecall",
    "ClaimDhnswTtiRecall",
    "ClaimCachePostReduction",
    "ClaimCacheQpsLoss",
    "ClaimProfileDistanceShare",
    "ClaimColocationQpsLoss",
    "ClaimColocationPostIncrease",
    "ClaimColocationByteIncrease",
    "ClaimColocationRecall",
    "ClaimResidentNodes",
    "ClaimResidentBytesMB",
    "ClaimResidentQpsGain",
    "ClaimResidentRemotePosts",
    "ClaimBudgetFiveGiB",
    "ClaimBudgetFullGiB",
    "ClaimWorkerOneKqps",
    "ClaimWorkerFortyKqps",
    "ClaimWorkerGain",
    "ClaimWorkerRecall",
    "ClaimLegacyAmplification",
    "ClaimFixedAmplification",
    "ClaimVariableAmplification",
    "ClaimVariableSidecarGiB",
    "ClaimVariableCnRssGiB",
    "ClaimVariableOneMnQps",
    "ClaimVariableFiveMnQps",
    "ClaimVariableOneMnRssGiB",
    "ClaimVariableFiveMnRssGiB",
    "ClaimVariableReadGini",
    "ClaimRdmaPayloadSmallMeanUs",
    "ClaimRdmaPayloadLargeMeanUs",
    "ClaimRdmaPayloadSmallTailUs",
    "ClaimRdmaPayloadLargeTailUs",
    "ClaimRdmaPayloadMeanSpan",
    "ClaimRdmaPayloadTailSpan",
    "ClaimRdmaQpOneMops",
    "ClaimRdmaQpTwoMops",
    "ClaimRdmaMtuMeanSpan",
    "ClaimRdmaNumaDiffPct",
    "ClaimRdmaOutsOneMops",
    "ClaimRdmaOutsSixteenMops",
    "ClaimCoroOneKqps",
    "ClaimCoroFourKqps",
    "ClaimCoroFourTailMs",
    "ClaimCoroSixteenTailMs",
    "ClaimCoroFourToSixteenGainPct",
    "ClaimTopKQpsSpanPct",
    "ClaimZipfQpsDiffPct",
    "ClaimZipfTailDiffPct",
    "ClaimTopologyRecall",
    "ClaimTopologyLoopbackKqps",
    "ClaimTopologyRemoteKqps",
    "ClaimTopologyLoopbackLatencyMs",
    "ClaimTopologyRemoteLatencyMs",
    "ClaimTopologyRemoteNetworkMs",
    "ClaimRefreshMinRecords",
    "ClaimRefreshMaxRecords",
    "ClaimRefreshRecall",
    "ClaimTtiSqEightRecall",
    "ClaimTtiRqTwoRecall",
    "ClaimTtiRqFourRecall",
    "ClaimTtiFpRecall",
    "ClaimTtiFpQps",
    "ClaimTtiFpPosts",
    "ClaimBuildOneMSiftSeconds",
    "ClaimBuildOneMDeepSeconds",
    "ClaimBuildOneMGistSeconds",
    "ClaimBuildOneMSiftCiSeconds",
    "ClaimBuildOneMDeepCiSeconds",
    "ClaimBuildOneMGistCiSeconds",
    "ClaimBuildOneMSiftRssGiB",
    "ClaimBuildOneMDeepRssGiB",
    "ClaimBuildOneMGistRssGiB",
    "ClaimBuildOneMSiftRegionGiB",
    "ClaimBuildOneMDeepRegionGiB",
    "ClaimBuildOneMGistRegionGiB",
    "ClaimBuildTenMDeepSeconds",
    "ClaimBuildTenMSiftSeconds",
    "ClaimBuildTenMTtiSeconds",
    "ClaimBuildTenMDeepCiSeconds",
    "ClaimBuildTenMSiftCiSeconds",
    "ClaimBuildTenMTtiCiSeconds",
    "ClaimBuildTenMDeepResidentSeconds",
    "ClaimBuildTenMSiftResidentSeconds",
    "ClaimBuildTenMTtiResidentSeconds",
    "ClaimBuildTenMDeepGiB",
    "ClaimBuildTenMSiftGiB",
    "ClaimBuildTenMTtiGiB",
}

SUPERSEDED_LITERALS = (
    r"9\.99--13\.46",
    r"17\.13--27\.22",
    r"83\.2\\%",
    r"23\.2\\%",
    r"5\.16\\%",
    r"0\.923 on DEEP",
    r"0\.905 on SIFT",
    r"0\.601 on TTI",
    r"1\.24 to 14\.74",
    r"\$11\.9\\times\$",
    r"Recall@10 0\.989",
    r"62,529",
    r"32\.0\\,MB",
    r"20\.62\\pm0\.07",
    r"18\.49\\pm0\.05",
    r"25\.48\\pm0\.05",
    r"4\.83, 4\.71, and 8\.51",
    r"4\.30, 3\.35, and 7\.88",
    r"4\.24 to 5\.57",
    r"4\.41 to 5\.78",
    r"3\.83\\,M READs/s",
    r"7\.21\\,M READs/s",
    r"0\.26 to 3\.86\\,Mops/s",
    r"6\.72 to\s+17\.87\\,kQPS",
    r"2\.70 to 13\.20\\,ms",
    r"4\.19 to 0\.294\\,kQPS",
    r"2\.20 to 18\.46\\,ms",
    r"17\.94\\,ms",
    r"6\.4\$--\$12\.4",
    r"recall 0\.97662",
    r"0\.864/0\.815/0\.862 recall",
    r"0\.961 recall and 260 QPS",
    r"3,870\s+posts/query",
)


def uncommented_tex(path: Path) -> str:
    return "\n".join(
        re.sub(r"(?<!\\)%.*$", "", line) for line in path.read_text().splitlines()
    )


class VldbManuscriptClaimBindingTest(unittest.TestCase):
    def test_main_inputs_generated_claims_and_consumes_every_gated_group(self) -> None:
        source = uncommented_tex(MAIN_TEX)
        self.assertIn(r"\input{generated_claims.tex}", source)
        missing = sorted(
            name for name in REQUIRED_CLAIM_MACROS if rf"\{name}" not in source
        )
        self.assertEqual(missing, [], f"main.tex does not consume claim macros: {missing}")

    def test_main_contains_no_superseded_hand_typed_claim_literals(self) -> None:
        source = uncommented_tex(MAIN_TEX)
        for pattern in SUPERSEDED_LITERALS:
            with self.subTest(pattern=pattern):
                self.assertIsNone(
                    re.search(pattern, source),
                    f"superseded hand-typed claim remains in main.tex: {pattern}",
                )

    def test_claim_macros_do_not_swallow_following_word_spacing(self) -> None:
        source = uncommented_tex(MAIN_TEX)
        unsafe = re.findall(r"\\Claim[A-Za-z]+\s+[A-Za-z]", source)
        self.assertEqual(
            unsafe,
            [],
            f"claim macro must use {{}} before a following word: {unsafe}",
        )

    def test_q3_float_follows_its_subsection_and_precedes_q4(self) -> None:
        source = uncommented_tex(MAIN_TEX)
        figure = source.index(r"\includegraphics[width=0.84\textwidth]{figs/eval_index_cost.pdf}")
        heading = source.index(r"\subsection{Q3: Index Cost and Deployment}")
        next_heading = source.index(
            r"\subsection{Q4: Construction, Offline Refresh, and Boundaries}"
        )
        self.assertLess(heading, figure)
        self.assertLess(figure, next_heading)

    def test_q4_float_follows_its_subsection_and_precedes_the_barrier(self) -> None:
        source = uncommented_tex(MAIN_TEX)
        figure = source.index(
            r"\includegraphics[width=0.84\textwidth]{figs/eval_lifecycle_boundaries.pdf}"
        )
        heading = source.index(
            r"\subsection{Q4: Construction, Offline Refresh, and Boundaries}"
        )
        barrier = source.index(r"\FloatBarrier", heading)
        self.assertLess(heading, figure)
        self.assertLess(figure, barrier)


if __name__ == "__main__":
    unittest.main()
