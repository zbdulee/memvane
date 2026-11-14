"""recall.types.ReaderResult keeps the frozen, field-equal value of query_endpoint."""
import unittest

from memvane.recall import reader_dispatch
from memvane.recall.types import ReaderResult

FIELDS = ('store_format', 'endpoint_id', 'reader_epoch', 'data_generation', 'rows',
          'semantic_status', 'title_only', 'source_locations')


def result(**changes):
    values = dict(store_format='schema3', endpoint_id='e' * 64, reader_epoch=3,
                  data_generation='g' * 64, rows=(('wiki/a.md', 'h' * 64),), semantic_status='hybrid',
                  title_only=frozenset(), source_locations=())
    values.update(changes)
    return ReaderResult(*[values[name] for name in FIELDS])


class ReaderResultValue(unittest.TestCase):
    def test_dispatch_returns_the_declared_type(self):
        self.assertIs(reader_dispatch.ReaderResult, ReaderResult)

    def test_fields_in_order(self):
        value = result()
        self.assertEqual(ReaderResult.__slots__, FIELDS)
        self.assertEqual((value.reader_epoch, value.data_generation, len(value.rows)), (3, 'g' * 64, 1))

    def test_frozen(self):
        value = result()
        for name in ('rows', 'extra'):
            with self.assertRaises(AttributeError):
                setattr(value, name, 1)
        with self.assertRaises(AttributeError):
            del value.rows
        self.assertEqual(len(value.rows), 1)

    def test_equal_and_hashed_by_fields(self):
        self.assertEqual(result(), result())
        self.assertEqual(len({result(), result()}), 1)
        self.assertNotEqual(result(), result(reader_epoch=4))
        self.assertNotEqual(result(), ('schema3',))


if __name__ == '__main__':
    unittest.main()
