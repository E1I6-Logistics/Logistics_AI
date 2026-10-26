"""Code owns zone/wait selection; KEV owns scores, actions and routes."""
from itertools import permutations
import unittest

from backend.app.services.kev_mapf import Graph, KevMAPF, parse_robot_state, plan_mapf_from_route_results
from tests.test_kev_score_priority import Scores, graph_for, requests_for


class TracedScores(Scores):
    def __init__(self, scores, *, action=None, detours=None):
        super().__init__(scores, detours=detours)
        self.questions, self.action = [], action

    def pick(self, state, candidates, instructions, question_id):
        self.questions.append((question_id, dict(candidates)))
        if self.action and self.action in candidates:
            return self.action
        return super().pick(state, candidates, instructions, question_id)


class CodeSelectionTests(unittest.TestCase):
    def engine(self, paths, **config):
        live = TracedScores({rid: 100-i for i, rid in enumerate(sorted(paths))})
        graph = Graph(graph_for(paths))
        robots = parse_robot_state({'robots': [dict(id=rid, current_node=p[0], goal_node=p[-1], path=p)
                                                for rid, p in paths.items()]}, graph, live)
        return KevMAPF(graph, robots, live, config), live

    def test_no_conflict_has_no_kev_calls(self):
        engine, live = self.engine({'a': [0, 1], 'b': [2, 3]})
        self.assertEqual(engine.resolve()['decisions'], [])
        self.assertEqual((live.questions, live.batches), ([], []))

    def test_zone_order_occupancy_then_entry_steps_then_id(self):
        paths = {'a': [0, 3, 4, 7, 8], 'b': [1, 7, 5, 3, 9]}
        zones = {'node:7': {'kind': 'node', 'node': 7, 'robots': ['a', 'b']},
                 'node:3': {'kind': 'node', 'node': 3, 'robots': ['a', 'b']}}
        for order in permutations(zones):
            engine, live = self.engine(paths)
            supplied = {z: zones[z] for z in order}
            self.assertEqual(engine._select_zone(supplied)[0], 'node:3')
            engine.occupied['node:7'] = 'b'
            self.assertEqual(engine._select_zone(supplied)[0], 'node:7')
            engine.occupied.clear()
            engine.robots['b']['path'] = [1, 2, 7, 3, 9]
            self.assertEqual(engine._select_zone(supplied)[0], 'node:3')
            engine.robots['a']['path'] = [0, 2, 3, 7, 8]
            engine.robots['b']['path'] = [1, 7, 5, 3, 9]
            self.assertEqual(engine._select_zone(supplied)[0], 'node:7')
            self.assertFalse(live.questions)

    def test_processed_zone_pairs_are_not_selected_again(self):
        paths = {'a': [0, 3, 4, 7, 8], 'b': [1, 3, 5, 7, 9], 'c': [2, 3, 6, 7, 10]}
        engine, live = self.engine(paths, wait_allowed_nodes=[0, 1, 2], max_reroutes=0)
        engine.reroutes = 0
        result = engine.resolve()
        self.assertEqual(len(result['decisions']), 6)
        self.assertEqual(len({(d['zone'], *d['robots']) for d in result['decisions']}), 6)
        self.assertTrue(all(q.startswith('action_') for q, _ in live.questions))
        self.assertEqual(len(live.batches), 1)

    def test_departing_pair_does_not_hide_other_approaching_pair(self):
        engine, live = self.engine({'a': [3, 4], 'b': [1, 3, 5], 'c': [2, 3, 6]},
                                  wait_allowed_nodes=[1, 2])
        result = engine.resolve()
        self.assertEqual([(d['priority_robot'], d['yield_robot']) for d in result['decisions']], [('b', 'c')])
        self.assertEqual(len(live.batches), 1)

    def test_wait_chooses_closest_safe_node_without_kev_pick(self):
        engine, live = self.engine({'a': [0, 3, 4], 'b': [1, 2, 3, 5]}, wait_allowed_nodes=[1, 2])
        result = engine.resolve()
        self.assertEqual(result['decisions'][0]['wait_node'], 2)
        self.assertEqual([q for q, _ in live.questions], ['action_0'])

    def test_wait_prefers_no_route_obstruction_over_closest_node(self):
        engine, _ = self.engine({'a': [0, 3, 4], 'b': [1, 2, 3, 5], 'c': [6, 2, 7]},
                                wait_allowed_nodes=[1, 2])
        result = engine.resolve()
        self.assertEqual(next(d for d in result['decisions'] if d['zone'] == 'node:3')['wait_node'], 1)
        self.assertEqual(next(d for d in result['decisions'] if d['zone'] == 'node:3')['wait_path_overlaps'], [])

    def test_wait_filters_policy_occupancy_reservations_and_blocks(self):
        paths = {'a': [0, 3, 4], 'b': [1, 2, 6, 7, 8, 3, 5], 'c': [9, 10]}
        engine, _ = self.engine(paths, wait_allowed_nodes=[1, 2, 6, 7], blocked_nodes=[1],
                                zone_occupancy={'node:2': 'external'}, wait_node_reservations={'6': ['external']})
        info = {'kind': 'node', 'node': 3, 'robots': ['a', 'b']}
        self.assertEqual(engine._wait_candidates(engine.robots['b'], info, 'a'), [7])
        engine.robots['c']['current_node'] = 7
        self.assertEqual(engine._wait_candidates(engine.robots['b'], info, 'a'), [])

    def test_no_wait_or_reachable_detour_offers_hold_only(self):
        engine, live = self.engine({'a': [0, 3, 4], 'b': [1, 3, 5]}, wait_allowed_nodes=[])
        result = engine.resolve()
        self.assertEqual(result['status'], 'BLOCKED')
        self.assertEqual(set(live.questions[0][1]), {'action_hold'})
        self.assertEqual(result['metrics']['reroutes'], 0)

    def test_wait_never_parks_on_priority_route(self):
        engine, live = self.engine({'a': [0, 2, 3, 4], 'b': [1, 2, 3, 5]}, wait_allowed_nodes=[2])
        result = engine.resolve()
        self.assertEqual(result['status'], 'BLOCKED')
        self.assertNotIn('action_wait', live.questions[0][1])

    def test_reroute_failure_holds_even_when_wait_was_available(self):
        paths = {'a': [0, 3, 4], 'b': [1, 2, 3, 5], 'c': [6, 9]}
        live = TracedScores({'a': 90, 'b': 70, 'c': 50}, action='action_reroute')
        result = plan_mapf_from_route_results(graph_for(paths, [(2, 6), (6, 5)]), requests_for(paths),
                                               planner=live, config={'wait_allowed_nodes': [1, 2]})
        self.assertEqual(result['status'], 'BLOCKED')
        self.assertEqual(result['decisions'][0]['reason'], 'REROUTE_FAILED')
        self.assertIn('action_wait', live.questions[0][1])


if __name__ == '__main__':
    unittest.main()
