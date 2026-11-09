"""Process ownership and complete inventory checks for the comparison runner."""
import subprocess
import unittest
from unittest.mock import Mock

from benchmarks.compare import TARGETS, order, stop


class ComparisonSafety(unittest.TestCase):
    def test_every_repetition_contains_each_target_once(self):
        orders = []
        for clients in (10, 100):
            for repetition in (1, 2, 3):
                sequence = order(repetition, clients)
                self.assertEqual(len(sequence), 5)
                self.assertEqual(set(sequence), set(TARGETS))
                orders.append(sequence)
        self.assertGreater(len(set(orders)), 3)
        self.assertEqual(sum(map(len, orders)), 30)

    def test_stubborn_owned_process_is_killed_and_joined(self):
        process = Mock()
        process.poll.return_value = None
        process.wait.side_effect = [subprocess.TimeoutExpired('owned', 5), 0]
        stop(process)
        process.terminate.assert_called_once()
        process.kill.assert_called_once()
        self.assertEqual(process.wait.call_count, 2)
        for call in process.wait.call_args_list:
            self.assertEqual(call.kwargs, {'timeout': 5})

    def test_completed_process_is_not_signaled(self):
        process = Mock()
        process.poll.return_value = 0
        stop(process)
        process.terminate.assert_not_called()
        process.kill.assert_not_called()
