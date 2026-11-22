"""The state model is consistent data and names real writers."""
import importlib
import unittest

from memvane.learning.context_learning import state_model


def resolve(dotted):
    """The object ``module.attribute`` names, or the module itself; raises when absent."""
    try:
        return importlib.import_module(dotted)
    except ImportError:
        module, _, name = dotted.rpartition('.')
        return getattr(importlib.import_module(module), name)


class StateModel(unittest.TestCase):
    def test_every_machine_is_closed_over_its_states_and_events(self):
        self.assertEqual([machine.name for machine in state_model.MACHINES], ['M1', 'M1U', 'M1T', 'M2', 'M3', 'M4', 'M5', 'M6', 'M7', 'M15'])
        for machine in state_model.MACHINES:
            with self.subTest(machine=machine.name):
                self.assertEqual(len(set(machine.states)), len(machine.states))
                self.assertEqual(len(machine.cells()), len(machine.states) * len(machine.events))
                for (state, event), after in machine.transitions.items():
                    self.assertIn(state, machine.states)
                    self.assertIn(after, machine.states)
                    self.assertNotEqual(state, after, (state, event))
                # A terminal state is left only by the deletion of its row.
                leaving = {(state, event): after for (state, event), after in machine.transitions.items()
                           if state in machine.terminal}
                self.assertTrue(all(after == state_model.ABSENT for after in leaving.values()), leaving)
                # Every state is reached by some transition (a row starts absent).
                # (A mark's first state is the column's default; the first pointer is enrollment's.)
                reached = set(machine.transitions.values()) | {state_model.ABSENT, 'unmarked', 'incumbent'}
                self.assertEqual(set(machine.states) - reached, set())

    def test_an_unlisted_pair_keeps_its_state(self):
        self.assertEqual(state_model.M1.next('queued', 'claim'), 'first_running')
        self.assertEqual(state_model.M1.next('gold', 'claim'), 'gold')
        self.assertEqual(state_model.M2.next('answer_parked', 'recover'), 'answer_parked')
        self.assertEqual(state_model.M2.next('committed', 'reserve'), 'committed')

    def test_every_event_names_a_writer_that_exists(self):
        for machine in state_model.MACHINES:
            for event, where in machine.where.items():
                with self.subTest(machine=machine.name, event=event):
                    self.assertIsNotNone(resolve(where))

    def test_the_ledger_stages_are_the_ledger_module_stages(self):
        from memvane.learning.context_learning import general_gold_ledger as ledger
        self.assertEqual(set(state_model.M2.states) - {state_model.ABSENT}, set(ledger.STAGES))
        rearmed = {state for (state, event) in state_model.M2.transitions
                   if event == 'reserve' and state != state_model.ABSENT}
        self.assertEqual(rearmed, set(ledger.REARMABLE))

    def test_an_inconsistent_machine_is_refused(self):
        with self.assertRaises(ValueError):
            state_model.Machine('X', 'x', states=('a',), terminal=(), transitions={('a', 'go'): 'b'},
                                owner='', where={'go': 'os'}, store='', lock='')


if __name__ == '__main__':
    unittest.main()
