"""Offline checks for the validation oracle, not endpoint acceptance evidence."""

import unittest

from metrics_support import Snapshot


class ExpositionOracle(unittest.TestCase):
    def test_label_escapes_round_trip(self):
        snapshot = Snapshot('rustybouncer_node_role{node="a\\\"\\\\\\nmetrics",role="primary"} 1\n')
        self.assertEqual(snapshot.value('node_role', node='a"\\\nmetrics', role='primary'), 1)

    def test_rejects_invalid_numbers_labels_duplicates_and_foreign_metrics(self):
        cases = [
            'rustybouncer_clients_active NaN',
            'rustybouncer_clients_active -1',
            'rustybouncer_clients_active +Inf',
            'rustybouncer_node_role{node="bad\\q"} 1',
            'rustybouncer_node_role{node="a",node="b"} 1',
            'other_metric 1',
            'rustybouncer_clients_active 0\nrustybouncer_clients_active 0',
            'rustybouncer_clients_active 0 1234',
        ]
        for text in cases:
            with self.subTest(text=text), self.assertRaises(AssertionError):
                Snapshot(text)

    def test_monotonic_counter_and_histogram_detection(self):
        first = Snapshot('rustybouncer_clients_accepted_total 2\nrustybouncer_query_duration_seconds_count 1')
        increased = Snapshot('rustybouncer_clients_accepted_total 3\nrustybouncer_query_duration_seconds_count 2')
        increased.monotonic(first)
        with self.assertRaises(AssertionError):
            first.monotonic(increased)

    def test_exact_inventory_required(self):
        with self.assertRaises(AssertionError):
            Snapshot('rustybouncer_clients_active 0').validate(('a', 'b', 'c'))
