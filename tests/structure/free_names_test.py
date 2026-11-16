"""Synthetic compiler symbols remain distinct from explicit name reads."""
import unittest
from unittest import mock

from tests.structure import free_names


class ConditionalAnnotationSymbolTests(unittest.TestCase):
    def test_generated_symbol_is_ignored_but_explicit_read_is_reported(self):
        real_symtable = free_names.symtable.symtable

        class Generated:
            def get_name(self): return '__conditional_annotations__'
            def is_assigned(self): return False
            def is_imported(self): return False
            def is_declared_global(self): return False
            def is_referenced(self): return True
            def is_global(self): return True

        class Table:
            def __init__(self, table): self.table = table
            def get_symbols(self): return [*self.table.get_symbols(), Generated()]
            def get_children(self): return self.table.get_children()

        with mock.patch.object(free_names.symtable, 'symtable',
                               side_effect=lambda *a: Table(real_symtable(*a))):
            self.assertEqual(free_names.check_source('value = 1\n', 'sample.py'), ([], None))
            findings, _ = free_names.check_source('value = __conditional_annotations__\n', 'sample.py')
            self.assertEqual(findings, ['sample.py:1:__conditional_annotations__'])
