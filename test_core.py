import unittest

import numpy as np

from core import (
    calculate_cost,
    combine_scores,
    cosine_scores,
    estimate_run,
    extract_json,
    format_time,
    parse_srt,
    parse_timecode,
    role_adjusted_score,
    subtitles_for_window,
)


class CoreTests(unittest.TestCase):
    def test_time_helpers(self):
        self.assertEqual(format_time(65.25), "01:05.250")
        self.assertEqual(parse_timecode("01:05.5"), 65.5)
        self.assertEqual(parse_timecode("01:01:05"), 3665)
        self.assertIsNone(parse_timecode("bad"))

    def test_srt(self):
        cues = parse_srt("1\n00:00:01,000 --> 00:00:03,000\n鍵は知らない\n")
        self.assertEqual(subtitles_for_window(cues, 2, 4), "鍵は知らない")

    def test_json_and_scores(self):
        self.assertEqual(extract_json("```json\n{\"ok\": true}\n```"), {"ok": True})
        scores = cosine_scores(np.array([1, 0]), np.array([[1, 0], [0, 1]]))
        self.assertAlmostEqual(scores[0], 1.0)
        self.assertGreater(combine_scores(0.8, 0.9, 0.8, 0.7, 0.9), combine_scores(0.8, 0.1, 0.1, 0.1, 0.1))

    def test_cost_and_estimate(self):
        cost = calculate_cost(1_000_000, 1_000_000, "gemini-3.7-flash", 150)
        self.assertAlmostEqual(cost["total_usd"], 4.5)
        self.assertAlmostEqual(cost["total_jpy"], 675)
        estimate = estimate_run(300, 6, 80, 5, 4, 1, "gemini-3.7-flash")
        self.assertEqual(estimate["coarse_count"], 50)
        self.assertEqual(estimate["detail_count"], 45)

    def test_role_adjustment(self):
        self.assertAlmostEqual(role_adjusted_score(0.8, "伏線候補"), 0.8)
        self.assertAlmostEqual(role_adjusted_score(0.8, "単なる関連"), 0.16)
        self.assertEqual(role_adjusted_score(0.8, "Payoff"), 0.0)


if __name__ == "__main__":
    unittest.main()
