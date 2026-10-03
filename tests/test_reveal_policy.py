import unittest

from recap.reveal_policy import RevealPolicy


class RevealPolicyTests(unittest.TestCase):
    def test_empty_status_never_auto_opens_even_on_return(self):
        policy = RevealPolicy()
        for now, idle in ((100, 60), (200, 300), (201, 0), (500, 90)):
            self.assertEqual(policy.step(now, "status:empty", True, idle,
                                        automatic_allowed=False), "closed")
        self.assertIsNone(policy._pending_id)
        self.assertIsNone(policy.last_presented_id)

    def test_manual_empty_status_keeps_history_and_leave_timing(self):
        policy = RevealPolicy(last_presented_id="old", last_auto_wall=100)
        self.assertEqual(policy.step(200, "status:empty", True, 0,
            hovering=True, manual=True, automatic_allowed=False), "open")
        self.assertEqual(policy.step(201, "status:empty", True, 0,
            automatic_allowed=False), "open")
        self.assertEqual(policy.step(203, "status:empty", True, 0,
            automatic_allowed=False), "closed")
        self.assertEqual(policy.last_presented_id, "old")
        self.assertEqual(policy.last_auto_wall, 100)
        self.assertEqual(policy.step(1400, "new", True, 60), "open")

    def test_expiry_closes_content_and_does_not_auto_replace_with_status(self):
        policy = RevealPolicy()
        self.assertEqual(policy.step(100, "old", True, 60), "open")
        self.assertEqual(policy.step(101, "status:empty", True, 60,
            automatic_allowed=False), "closed")
        self.assertIsNone(policy._pending_id)

    def test_model_controls_the_required_rest(self):
        quick = RevealPolicy(idle_before_open=30)
        quiet = RevealPolicy(idle_before_open=150)
        self.assertEqual(quick.step(100, "a", True, 45), "open")
        self.assertEqual(quiet.step(100, "a", True, 45), "closed")
        self.assertEqual(quiet.step(210, "a", True, 155), "open")

    def test_model_long_rest_does_not_conflict_with_away_detection(self):
        policy = RevealPolicy(idle_before_open=300)
        self.assertEqual(policy.step(100, "a", True, 200), "closed")
        self.assertEqual(policy.step(201, "a", True, 301), "open")

    def test_expired_snapshot_cancels_pending_summary(self):
        policy = RevealPolicy()
        self.assertEqual(policy.step(100, "a", True, 0), "closed")
        self.assertEqual(policy.step(101, None, False, 0), "closed")
        self.assertEqual(policy.step(160, None, True, 60), "closed")
        self.assertIsNone(policy._pending_id)

    def test_busy_user_waits_for_short_rest(self):
        policy = RevealPolicy()
        for now in (0, 30, 120, 600):
            self.assertEqual(policy.step(now, "a", True, 0), "closed")
        self.assertEqual(policy.step(660, "a", True, 60), "open")
        self.assertEqual(policy.last_presented_id, "a")

    def test_away_user_opens_only_on_return(self):
        policy = RevealPolicy()
        self.assertEqual(policy.step(100, "a", True, 180), "closed")
        self.assertEqual(policy.step(200, "a", True, 280), "closed")
        self.assertEqual(policy.step(201, "a", True, 1), "open")
        self.assertEqual(policy.last_auto_wall, 201)

    def test_sleep_or_disallowed_return_does_not_consume_candidate(self):
        policy = RevealPolicy()
        self.assertEqual(policy.step(100, "a", False, 180), "closed")
        self.assertEqual(policy.step(10000, "a", False, 9000), "closed")
        self.assertEqual(policy.step(10001, "a", False, 0), "closed")
        self.assertIsNone(policy.last_presented_id)
        self.assertEqual(policy.step(10002, "a", True, 1), "open")

    def test_hover_defers_expiry_and_leave_has_two_seconds(self):
        policy = RevealPolicy(duration=15)
        self.assertEqual(policy.step(100, "a", True, 60), "open")
        self.assertEqual(policy.step(114, "a", True, 0, hovering=True), "open")
        self.assertEqual(policy.step(150, "a", True, 0, hovering=True), "open")
        self.assertEqual(policy.step(151, "a", True, 0), "open")
        self.assertEqual(policy.step(152.9, "a", True, 0), "open")
        self.assertEqual(policy.step(153, "a", True, 0), "closed")

    def test_early_hover_leave_overrides_long_reading_duration(self):
        policy = RevealPolicy(duration=25)
        self.assertEqual(policy.step(100, "a", True, 0, hovering=True, manual=True), "open")
        self.assertEqual(policy.step(101, "a", True, 0), "open")
        self.assertEqual(policy.step(102.9, "a", True, 0), "open")
        self.assertEqual(policy.step(103, "a", True, 0), "closed")

    def test_hover_return_cancels_leave_and_next_leave_restarts_countdown(self):
        policy = RevealPolicy(duration=25)
        self.assertEqual(policy.step(100, "a", True, 60), "open")
        self.assertEqual(policy.step(100.5, "a", True, 0, hovering=True), "open")
        self.assertEqual(policy.step(101, "a", True, 0), "open")
        self.assertEqual(policy.step(102, "a", True, 0, hovering=True), "open")
        self.assertEqual(policy.step(104, "a", True, 0, hovering=True), "open")
        self.assertEqual(policy.step(105, "a", True, 0), "open")
        self.assertEqual(policy.step(106.9, "a", True, 0), "open")
        self.assertEqual(policy.step(107, "a", True, 0), "closed")

    def test_automatic_open_without_hover_keeps_reading_duration(self):
        policy = RevealPolicy(duration=25)
        self.assertEqual(policy.step(100, "a", True, 60), "open")
        self.assertEqual(policy.step(103, "a", True, 0), "open")
        self.assertEqual(policy.step(124.9, "a", True, 0), "open")
        self.assertEqual(policy.step(125, "a", True, 0), "closed")

    def test_manual_can_review_same_id_but_obeys_allowed(self):
        policy = RevealPolicy(last_presented_id="a")
        self.assertEqual(policy.step(100, "a", True, 0), "closed")
        self.assertEqual(policy.step(101, "a", True, 0, manual=True), "open")
        self.assertEqual(policy.step(130, "a", True, 0), "closed")
        self.assertEqual(policy.step(131, "a", False, 0, manual=True), "closed")
        self.assertEqual(policy.step(132, "a", True, 0, manual=True), "open")
        self.assertEqual(policy.last_auto_wall, 0)

    def test_cooldown_keeps_new_candidate_and_same_id_never_repeats(self):
        policy = RevealPolicy(cooldown=1)
        self.assertEqual(policy.step(0, "a", True, 60), "open")
        self.assertEqual(policy.step(30, "a", True, 60), "closed")
        self.assertEqual(policy.step(100, "b", True, 60), "closed")
        self.assertEqual(policy.step(1199, "b", True, 60), "closed")
        self.assertEqual(policy.step(1200, "b", True, 60), "open")
        self.assertEqual(policy.step(3000, "b", True, 60), "closed")
        self.assertEqual(policy.step(5000, "a", True, 60), "closed")

    def test_disallowed_closes_and_dismiss_does_not_disable_future_ids(self):
        policy = RevealPolicy()
        self.assertEqual(policy.step(100, "a", True, 60), "open")
        self.assertEqual(policy.step(101, "b", False, 60), "closed")
        self.assertEqual(policy.step(102, "b", True, 60), "closed")
        self.assertEqual(policy.step(1300, "b", True, 60), "open")
        self.assertEqual(policy.step(1301, "b", True, 0, dismiss=True), "closed")
        self.assertEqual(policy.step(3000, "b", True, 60), "closed")
        self.assertEqual(policy.step(3001, "c", True, 60), "open")

    def test_duration_bounds_and_saved_presentation_state(self):
        self.assertEqual(RevealPolicy(duration=1).duration, 15)
        self.assertEqual(RevealPolicy(duration=100).duration, 25)
        policy = RevealPolicy(last_auto_wall=100, last_presented_id="a")
        self.assertEqual(policy.step(1299, "b", True, 60), "closed")
        self.assertEqual(policy.step(1300, "b", True, 60), "open")


if __name__ == "__main__":
    unittest.main()
