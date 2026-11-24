"""Offline regression checks for native fan cycle selection."""

import unittest

import numpy as np

from select_fan_cycle import ADD, COVERAGE, SCREEN, Activity, observe, select_cycle


def take(schedule, intervals=None):
    times = [10_000]
    for step in intervals or [50] * (len(schedule) - 1):
        times.append(times[-1] + step)
    frames = [{"qpc": time, "present": 20_000 + i} for i, time in enumerate(times)]
    roles = {"N": set(), "B": {COVERAGE, ADD, SCREEN}, "C": {COVERAGE}, "L": {SCREEN}, "D": {ADD},
             "V": {SCREEN}, "E": {COVERAGE, ADD, SCREEN}}
    activity = [Activity(frozenset(roles[token]), frozenset(roles[token] if token != "V" else ()),
                         int(token not in "NV"), token == "E") for token in schedule]
    return select_cycle(frames, activity, 1000)


class FanCycleTests(unittest.TestCase):
    def test_repeat_timing_survives_invisible_draws_and_short_draw_free_gap(self):
        schedule = "NNNNNCVNNNDLNNNNNNNNNNNC"
        steps = [50] * (len(schedule) - 1)
        steps[-7] = 31
        selected = take(schedule, steps)
        self.assertTrue(selected["repeatTimingUsed"])
        self.assertEqual(selected["onset"]["sourceIndex"], 5)
        self.assertEqual(selected["lastDraw"]["sourceIndex"], 11)
        self.assertEqual(selected["cleanPlate"]["sourceIndex"], 4)
        self.assertEqual(selected["drawFreeFramesWithinCast"], 3)
        self.assertEqual(selected["drawFramesWithoutMeasuredChange"], 1)
        self.assertEqual(selected["followingTimestamp"]["sourceIndex"], len(schedule) - 5)
        self.assertEqual(selected["followingTimestamp"]["qpc"], 10_000 + sum(steps[:-4]))

    def test_stalled_present_does_not_make_one_idle_sample_a_separator(self):
        schedule = "NNNNNCNDLNNNNNN"
        steps = [50] * (len(schedule) - 1)
        steps[6] = 350
        selected = take(schedule, steps)
        self.assertEqual(selected["onset"]["sourceIndex"], 5)
        self.assertEqual(selected["lastDraw"]["sourceIndex"], 8)

    def test_partial_start_is_rejected_or_skipped(self):
        with self.assertRaisesRegex(ValueError, "before onset"):
            take("BNNNNNNNNN")
        self.assertEqual(take("BNNNNNBNNNNNNN")["onset"]["sourceIndex"], 6)

    def test_partial_end_and_missing_following_timestamp_are_rejected(self):
        for schedule in ("NNNNNBNV", "NNNNNBNNNN"):
            with self.subTest(schedule=schedule), self.assertRaisesRegex(ValueError, "missing following timestamp"):
                take(schedule)
        selected = take("NNNNNBNNNNN")
        self.assertFalse(selected["repeatTimingUsed"])
        self.assertEqual(selected["last"]["sourceIndex"], 9)
        self.assertEqual(selected["followingTimestamp"]["sourceIndex"], 10)

    def test_no_effect_or_only_one_role_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "no qualifying native fan draws"):
            take("NNNNNNNNNNNN")
        with self.assertRaisesRegex(ValueError, "missing measured roles"):
            take("NNNNNLNNNNNN")

    def test_effect_on_crop_edge_rejects_interval(self):
        with self.assertRaisesRegex(ValueError, "crop edge at present 20005"):
            take("NNNNNENNNNNN")

    def test_coverage_alpha_without_rgb_change_still_touches_crop_edge(self):
        before = np.ones((3, 3, 4))
        after = before.copy()
        after[0, 1, 3] = 0.5
        entries = [{"role": COVERAGE, "before": before, "after": after,
                    "row": {"srcAlphaBlend": 1, "dstAlphaBlend": 6, "alphaBlendOp": 1, "writeMask": 15}}]
        measured = observe(entries, {"width": 3, "height": 3})
        self.assertEqual(measured.measured_roles, {COVERAGE})
        self.assertEqual(measured.support_pixels, 1)
        self.assertTrue(measured.touches_edge)


if __name__ == "__main__":
    unittest.main()
