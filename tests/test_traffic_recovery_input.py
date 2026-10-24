from copy import deepcopy
import json
import unittest

from backend.app.services.traffic_recovery_input import format_guidance_state, path_occupancy_facts


class RecoveryInputTests(unittest.TestCase):
    def state(self):
        return dict(robots=[dict(robot_id="robot1", current_node="3", original_start="3", original_goal="0", has_movement_request=True, stage="INITIAL"),
                            dict(robot_id="robot2", current_node="11", original_start="11", original_goal="11", has_movement_request=False, stage="INITIAL"),
                            dict(robot_id="robot3", current_node="10", original_start="10", original_goal="10", has_movement_request=False, stage="INITIAL")],
                    chosen_prefix=[], recent_checks=[], wait_allowed_nodes=["3", "11", "10", "6", "8", "0"],
                    blocked_nodes=[], blocked_edges=[], remaining_checks=128, remaining_seconds=10)

    def criteria(self):
        return {"KEEP": json.dumps(dict(robot_id="robot1", path=["3", "11", "7", "9", "10", "0"])),
                "YIELD": json.dumps(dict(robot_id="robot2", path=["11", "6"]))}

    def test_uses_current_hypothetical_positions_and_excludes_own_occupancy(self):
        state = self.state()
        self.assertEqual(path_occupancy_facts(state, self.criteria()), {
            "KEEP": [{"node": "11", "robot_id": "robot2"}, {"node": "10", "robot_id": "robot3"}], "YIELD": [],
        })
        state["robots"][1].update(current_node="6", stage="YIELDED")
        self.assertEqual(path_occupancy_facts(state, self.criteria())["KEEP"], [{"node": "10", "robot_id": "robot3"}])
        state["robots"][2].update(current_node="8", stage="YIELDED")
        self.assertEqual(path_occupancy_facts(state, self.criteria())["KEEP"], [])

    def test_adds_facts_without_modifying_candidates_source_or_claiming_validity(self):
        state, criteria = self.state(), self.criteria()
        before = deepcopy((state, criteria))
        formatted = format_guidance_state(state, criteria, "occupancy_facts")
        self.assertEqual(set(formatted["offered_path_occupants"]), set(criteria))
        self.assertIn("NOT_A_RESERVATION_VERDICT", formatted["fact_scope"])
        self.assertEqual((state, criteria), before)
        self.assertEqual(format_guidance_state(state, criteria), state)

    def test_compact_format_retains_goal_stage_and_separates_past_checks(self):
        text = format_guidance_state(self.state(), self.criteria(), "compact")
        self.assertIn("N11 occupied by robot2", text)
        self.assertIn("N10 occupied by robot3", text)
        self.assertIn("goal N0", text)
        self.assertIn("stage=INITIAL", text)
        self.assertIn("may differ from CURRENT", text)
        self.assertIn("YIELD: no other robot", text)


if __name__ == "__main__":
    unittest.main()
