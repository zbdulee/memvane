"""A prompt recall leaves out the cards its session already holds.

``HostInstallCase`` plus three cards that share one marker. The session record
(``edit-recall-sessions/<sha256(session id)>.json``) is read before the search
and written after the injection; every assertion is an event field, a record
field or a marker file count.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import time
import unittest
from unittest import mock

from memvane.host import edit_recall, session_cards
from memvane.host.tests.support import HostInstallCase, context

SHARED = 'zqsharedx'
TOPICS = ('glacier', 'mosaic', 'lantern')
ASK = 'which protocol keeps ' + SHARED + '?'


class SessionCase(HostInstallCase):
    def setUp(self):
        super().setUp()
        self.note = self.fixture.write_note('notes/shared.md', 'Shared field notes.\n')
        for topic in TOPICS:
            self.write_card(topic)
        self.fixture.commit('shared cards')
        self.install.settle()

    def write_card(self, topic, extra=''):
        return self.fixture.write_card(
            f'concepts/{topic}.md', title=topic.title() + ' protocol', sources=[self.note],
            body=f'The {topic} protocol keeps marker token zq{topic}x and {SHARED} for recall checks.{extra}\n')

    def recall(self, session='s-one', prompt=ASK, host='claude', **payload):
        """``(context, recall event)`` of one prompt recall."""
        before = len(self.install.events())
        fields = {'prompt': prompt, 'hook_event_name': 'UserPromptSubmit', **payload}
        if session is not None:
            fields['session_id'] = session
        answer = self.serve('recall', fields, host)
        [event] = [row for row in self.install.events()[before:] if row.get('event') == 'recall']
        return context(answer, host), event

    def session_start(self, session, source=None, host='claude'):
        before = len(self.install.events())
        payload = {'hook_event_name': 'SessionStart', 'session_id': session}
        if source is not None:
            payload['source'] = source
        self.serve('update-queued', payload, host)
        [event] = [row for row in self.install.events()[before:] if row.get('event') == 'update-queued']
        return event

    def state_dir(self):
        from memvane.diagnostics.api import state_root_for_decision
        path = state_root_for_decision(self.decision) / edit_recall.SESSION_DIR
        path.mkdir(mode=0o700, exist_ok=True)
        return path

    def record_path(self, session):
        return self.state_dir() / (hashlib.sha256(session.encode()).hexdigest() + '.json')

    def record(self, session):
        return json.loads(self.record_path(session).read_text())

    def markers(self, session=None):
        prefix = '' if session is None else self.record_path(session).name
        return sorted(name for name in os.listdir(self.state_dir()) if '.reset.' in name and name.startswith(prefix))

    def drain(self, session='s-one', **payload):
        """Recall until one comes back empty: ``(events, injected (path, sha) in order)``."""
        events, injected = [], []
        for _ in range(len(TOPICS) + 3):
            _text, event = self.recall(session, **payload)
            events.append(event)
            if event['outcome'] != 'served':
                break
            self.assertEqual(event['sessionExcluded'], len(injected), event)
            injected += [(card['path'], card['sha']) for card in event['cards']]
        return events, injected


class PromptExclusion(SessionCase):
    def test_each_recall_leaves_out_what_the_session_holds_until_nothing_is_left(self):
        events, injected = self.drain()
        # Exactly the three cards the question matches: once they are held, the query is not
        # widened to single terms, so the fixture's own card (one common word) is never injected.
        self.assertEqual(sorted(path.rsplit('/', 1)[-1] for path, _sha in injected),
                         sorted(topic + '.md' for topic in TOPICS))
        self.assertEqual((events[0]['sessionState'], events[0]['sessionExcluded']), ('none', 0))
        self.assertTrue(all(event['sessionState'] == 'ok' for event in events[1:]), events)
        last = events[-1]
        self.assertEqual((last['outcome'], last['reason'], last['sessionExcluded']),
                         ('empty', 'no_candidates_after_exclusion', len(injected)), last)
        # The row of a recall the exclusion emptied reads like any other row without candidates.
        self.assertEqual((last['selection'], last['candidates'], last['generation']),
                         ('no_candidates', 0, events[0]['generation']), last)
        self.assertTrue(events[0]['generation'])
        for event in events:
            self.assertNotIn('semanticCosine', event)
            self.assertNotIn('semanticSources', event)
        record = self.record('s-one')
        self.assertEqual((record['v'], len(record['cards']), set(record['by'])), (1, len(injected), {'p'}))
        self.assertEqual(record['seq'], len(events) - 1)
        self.assertEqual(record['at'], sorted(record['at']))
        # Another session starts from nothing; a query no excluded card matches is an ordinary empty recall.
        _text, other = self.recall('s-two')
        self.assertEqual((other['outcome'], other['sessionExcluded']), ('served', 0))
        _text, unrelated = self.recall('s-one', prompt='what does zqnowherex mean?')
        self.assertEqual((unrelated['outcome'], unrelated['reason'], unrelated['sessionExcluded']),
                         ('empty', 'no_candidates', len(injected)), unrelated)

    def test_a_question_whose_matches_are_all_held_is_not_widened_to_single_terms(self):
        # The fixture's own card shares one word ("marker") with the question, never the shared token.
        self.drain()
        text, event = self.recall()
        self.assertEqual((text, event['outcome'], event['reason']), ('', 'empty', 'no_candidates_after_exclusion'))
        # Without held cards the same kind of miss still falls back to single terms.
        text, other = self.recall('s-fresh', prompt='which marker does zqnowherex keep?')
        self.assertEqual((other['outcome'], other['sessionExcluded']), ('served', 0), other)
        self.assertIn('zqheronx', text)
        # Nor does holding cards stop it for a question none of them matched in the first place.
        text, held = self.recall(prompt='which marker does zqnowherex keep?')
        self.assertEqual((held['outcome'], held['sessionExcluded']), ('served', len(TOPICS)), held)
        self.assertIn('zqheronx', text)

    def test_a_card_that_changed_comes_back(self):
        _events, injected = self.drain()
        self.write_card(TOPICS[0], extra=' Revised once.')
        self.fixture.commit('revise')
        self.install.settle()
        text, event = self.recall()
        self.assertEqual(event['outcome'], 'served', event)
        [card] = event['cards']
        self.assertTrue(card['path'].endswith(f'concepts/{TOPICS[0]}.md'), card)
        self.assertNotIn((card['path'], card['sha']), injected)
        self.assertIn('Revised once', text)

    def test_a_session_without_an_id_and_hermes_are_served_as_before(self):
        first, event = self.recall(None)
        again, repeated = self.recall(None)
        self.assertEqual((event['sessionState'], repeated['sessionState'], first), ('none', 'none', again))
        self.assertEqual(os.listdir(self.state_dir()), [])
        one, hermes = self.recall('h-one', host='hermes')
        two, hermes_again = self.recall('h-one', host='hermes')
        self.assertEqual(one, two)
        self.assertIn(SHARED, one)
        self.assertNotIn('sessionExcluded', hermes)
        self.assertNotIn('sessionState', hermes_again)
        self.assertEqual(os.listdir(self.state_dir()), [])


class Concurrency(SessionCase):
    def test_a_held_record_costs_one_repeat_and_never_the_answer(self):
        _text, first = self.recall()
        fd = os.open(str(self.record_path('s-one')), os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            with mock.patch.object(session_cards, '_sleep') as waited:
                text, held = self.recall()
        finally:
            os.close(fd)
        # The write was tried the fixed number of times, with a wait between tries only.
        self.assertEqual([call.args for call in waited.call_args_list],
                         [(session_cards.WRITE_RETRY_SECONDS,)] * (session_cards.WRITE_ATTEMPTS - 1))
        self.assertEqual((held['outcome'], held['sessionExcluded'], held['sessionState']),
                         ('served', 0, 'write_skipped'), held)
        self.assertEqual(held['cards'], first['cards'])
        self.assertIn(SHARED, text)
        self.assertEqual(self.record('s-one')['seq'], 1)
        # The lock is free again: the record is read and written as usual.
        _text, after = self.recall()
        self.assertEqual((after['sessionState'], after['sessionExcluded']), ('ok', len(first['cards'])))

    def test_a_reader_reports_a_held_record_as_busy(self):
        self.recall()
        fd = os.open(str(self.record_path('s-one')), os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            self.assertEqual(session_cards.exclusions(self.decision, {'session_id': 's-one'}, 'claude'), ([], 'busy'))
        finally:
            os.close(fd)
        pairs, state = session_cards.exclusions(self.decision, {'session_id': 's-one'}, 'claude')
        self.assertEqual((state, len(pairs)), ('ok', self.record('s-one')['seq'] and len(self.record('s-one')['cards'])))


class Reset(SessionCase):
    def test_a_compaction_starts_the_session_over_and_a_resume_does_not(self):
        _text, first = self.recall()
        event = self.session_start('s-one', 'resume')
        self.assertEqual((event.get('sessionSource'), event.get('sessionReset')), ('resume', None))
        self.assertEqual(self.markers('s-one'), [])
        _text, kept = self.recall()
        self.assertEqual(kept['sessionExcluded'], len(first['cards']))
        for source in ('compact', 'clear'):
            with self.subTest(source=source):
                event = self.session_start('s-one', source)
                self.assertEqual((event['sessionSource'], event['sessionReset']), (source, source))
                self.assertEqual(len(self.markers('s-one')), 1)
                _text, fresh = self.recall()
                self.assertEqual((fresh['sessionExcluded'], fresh['sessionState'], fresh['outcome']),
                                 (0, 'reset_pending', 'served'), fresh)
                self.assertEqual(fresh['cards'], first['cards'])
                # The recall's own write applied the marker: only its cards remain.
                self.assertEqual(self.markers('s-one'), [])
                record = self.record('s-one')
                self.assertEqual(sorted(tuple(card) for card in record['cards']),
                                 sorted((card['path'], card['sha']) for card in fresh['cards']))
                _text, after = self.recall()
                self.assertEqual((after['sessionState'], after['sessionExcluded']), ('ok', len(fresh['cards'])))

    def test_a_session_start_without_a_reset_source_or_session_writes_no_marker(self):
        self.recall()
        for source in (None, 'startup', 'resume', 'something-else', 7):
            payload = {'hook_event_name': 'SessionStart', 'session_id': 's-one'}
            if source is not None:
                payload['source'] = source
            self.serve('update-queued', payload)
        self.serve('update-queued', {'hook_event_name': 'SessionStart', 'source': 'compact'})
        self.serve('update-queued', {'hook_event_name': 'SessionStart', 'source': 'compact', 'session_id': 'h'},
                   'hermes')
        self.assertEqual(self.markers(), [])
        other = [row for row in self.install.events() if row.get('event') == 'update-queued'][3]
        self.assertEqual(other.get('sessionSource'), 'other')

    def test_a_compaction_while_the_record_is_held_still_leaves_its_marker(self):
        self.recall()
        fd = os.open(str(self.record_path('s-one')), os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            event = self.session_start('s-one', 'compact')
        finally:
            os.close(fd)
        self.assertEqual(event['sessionReset'], 'compact')
        self.assertEqual(len(self.markers('s-one')), 1)

    def test_a_marker_written_while_one_is_applied_stays_for_the_next_writer(self):
        self.recall()
        self.assertTrue(session_cards.mark_reset(self.decision, 's-one'))
        with edit_recall.session_state(self.decision, 's-one') as (row, save):
            self.assertEqual(row['cards'], [])
            self.assertTrue(session_cards.mark_reset(self.decision, 's-one'))
            save()
        self.assertEqual(len(self.markers('s-one')), 1)
        # A holder that never saved leaves the marker it saw.
        with edit_recall.session_state(self.decision, 's-one') as (row, _save):
            self.assertEqual(row['cards'], [])
        self.assertEqual(len(self.markers('s-one')), 1)

    def test_markers_older_than_a_day_are_swept(self):
        for session in ('gone-1', 'gone-2', 'fresh'):
            self.assertTrue(session_cards.mark_reset(self.decision, session))
        old = time.time() - session_cards.MARKER_TTL_SECONDS - 60
        for name in self.markers():
            if not name.startswith(self.record_path('fresh').name):
                os.utime(self.state_dir() / name, (old, old))
        self.recall('another')
        self.assertEqual(self.markers(), self.markers('fresh'))
        self.assertEqual(len(self.markers('fresh')), 1)


class Contexts(SessionCase):
    def seed(self, session, by, seq=1, at=None):
        """A record holding every shared card, injected into context ``by``."""
        _text, event = self.recall('seed-' + session)
        cards = [[card['path'], card['sha']] for card in event['cards']]
        row = {'v': 1, 'cards': cards, 'edits': [], 'updated': time.time()}
        if by is not None:
            row.update(seq=seq, at=[seq if at is None else at] * len(cards), by=[by] * len(cards))
        path = self.record_path(session)
        path.write_text(json.dumps(row))
        os.chmod(path, 0o600)
        return len(cards)

    def excluded(self, session, host='claude'):
        _text, event = self.recall(session, host=host)
        # With every match held the recall is empty; with none held it serves them again.
        self.assertEqual(event['outcome'], 'empty' if event['sessionExcluded'] else 'served', event)
        return event['sessionExcluded']

    def test_only_the_main_context_entries_are_left_out(self):
        for by, expected in (('p', True), ('e', True), ('e?', False), ('e:0a1b2c3d', False)):
            with self.subTest(by=by):
                count = self.seed('ctx-' + by, by)
                event_text, event = self.recall('ctx-' + by)
                self.assertEqual(event['sessionExcluded'], count if expected else 0, event)
                self.assertEqual((event['outcome'], event['reason']),
                                 ('empty', 'no_candidates_after_exclusion') if expected else ('served', 'selected'),
                                 event)

    def test_a_record_of_an_older_bundle_or_a_damaged_one_excludes_nothing(self):
        self.seed('old-shape', None)
        self.assertEqual(self.excluded('old-shape'), 0)
        self.record_path('broken').write_text('{"v":1,"cards":[["a","b"]')
        os.chmod(self.record_path('broken'), 0o600)
        self.assertEqual(self.excluded('broken'), 0)
        row = {'v': 1, 'cards': [['../x.md', 'a' * 64], ['wiki/.hidden/x.md', 'b' * 64], ['ok/x.md', 'Z' * 64]],
               'edits': [], 'seq': 1, 'at': [1, 1, 1], 'by': ['p', 'p', 'p']}
        self.record_path('odd').write_text(json.dumps(row))
        os.chmod(self.record_path('odd'), 0o600)
        self.assertEqual(self.excluded('odd'), 0)

    def test_the_window_ends_an_edit_entry_and_every_entry_of_a_host_without_a_signal(self):
        window = session_cards.WINDOW
        cases = (('claude', 'p', window + 5, True), ('claude', 'e', window - 1, True),
                 ('claude', 'e', window, False), ('codex', 'p', window - 1, True), ('codex', 'p', window, False))
        for index, (host, by, age, expected) in enumerate(cases):
            with self.subTest(host=host, by=by, age=age):
                session = 'win-%d' % index
                count = self.seed(session, by, seq=age + 1, at=1)
                self.assertEqual(self.excluded(session, host), count if expected else 0)


class EditContexts(SessionCase):
    def edit(self, session, host='claude', **extra):
        from memvane.host import host_response as response
        from memvane.host.tests.integration.test_edit_recall import EDIT_TEXT, NEW_FILE, edit_payload
        self.fixture.write_note(NEW_FILE, EDIT_TEXT)
        tool = 'Write' if host == 'claude' else 'apply_patch'
        with response.learning_capture() as slot:
            answer = self.serve('posttool', edit_payload(session, tool, **extra), host)
        [event] = [row for row in self.install.events()
                   if row.get('event') == 'recall.edit' and row.get('session') == session]
        return answer, event, list(slot)

    def test_a_subagent_edit_injection_stays_out_of_the_parents_prompt_recall(self):
        answer, event, capture = self.edit('sub', agent_id='agent-7')
        self.assertIn('zqheronx', context(answer))
        self.assertEqual((event['agentScoped'], event['injectedBy']),
                         (True, 'e:' + hashlib.sha256(b'agent-7').hexdigest()[:8]), event)
        self.assertEqual(len(capture), 1)
        text, prompt = self.recall('sub', prompt='what marker does the emerald heron keep?')
        self.assertEqual((prompt['sessionExcluded'], prompt['outcome']), (0, 'served'), prompt)
        self.assertIn('zqheronx', text)
        self.assertEqual(self.record('sub')['by'], [event['injectedBy'], 'p'])

    def test_an_edit_of_unknown_context_is_not_left_out_and_a_main_context_edit_is(self):
        _answer, event, _capture = self.edit('unknown')
        self.assertEqual((event['agentScoped'], event['injectedBy']), (False, 'e?'), event)
        _text, prompt = self.recall('unknown', prompt='what marker does the emerald heron keep?')
        self.assertEqual((prompt['sessionExcluded'], prompt['outcome']), (0, 'served'))
        _answer, event, _capture = self.edit('main', host='codex')
        self.assertEqual((event['agentScoped'], event['injectedBy']), (False, 'e'), event)
        _text, prompt = self.recall('main', prompt='what marker does the emerald heron keep?', host='codex')
        # The heron card the edit recall injected was the question's only match: nothing else is injected.
        self.assertEqual((prompt['sessionExcluded'], prompt['outcome'], prompt['reason'], prompt.get('cards', [])),
                         (1, 'empty', 'no_candidates_after_exclusion', []), prompt)


class Rules(unittest.TestCase):
    def row(self, entries, seq):
        return {'cards': [[path, 'a' * 64] for path, _at, _by in entries], 'seq': seq,
                'at': [at for _path, at, _by in entries], 'by': [by for _path, _at, by in entries]}

    def test_an_edit_sees_its_own_context_only(self):
        row = self.row([('p.md', 1, 'p'), ('e.md', 1, 'e'), ('u.md', 1, 'e?'), ('a.md', 1, 'e:aaaaaaaa'),
                        ('b.md', 1, 'e:bbbbbbbb')], 1)
        names = lambda context, host='claude': sorted(path for path, _sha in session_cards.seen(row, context, host))
        self.assertEqual(names('p'), ['e.md', 'p.md'])
        self.assertEqual(names('e'), ['e.md', 'p.md', 'u.md'])
        self.assertEqual(names('e?'), ['e.md', 'p.md', 'u.md'])
        self.assertEqual(names('e:aaaaaaaa'), ['a.md'])

    def test_the_edit_context_comes_from_the_agent_id(self):
        self.assertEqual(session_cards.edit_context('claude', {}), ('e?', False))
        self.assertEqual(session_cards.edit_context('codex', {}), ('e', False))
        by, scoped = session_cards.edit_context('claude', {'agent_id': 'agent-7'})
        self.assertEqual((by, scoped), ('e:' + hashlib.sha256(b'agent-7').hexdigest()[:8], True))
        self.assertEqual(session_cards.edit_context('claude', {'agentId': 'agent-7'}), (by, True))
        self.assertEqual(session_cards.edit_context('claude', {'agent_id': 'bad id\n'}), ('e?', False))

    def test_a_recall_that_injected_counts_once_and_a_reinjected_pair_moves_to_now(self):
        row = session_cards.loaded(None)
        session_cards.record(row, [], 'p')
        self.assertEqual(row['seq'], 0)
        session_cards.record(row, [('a.md', 'a' * 64), ('b.md', 'b' * 64)], 'p')
        session_cards.record(row, [('a.md', 'a' * 64)], 'e?')
        session_cards.record(row, [('a.md', 'a' * 64)], 'p')
        self.assertEqual((row['seq'], row['cards'], row['at'], row['by']),
                         (3, [['b.md', 'b' * 64], ['a.md', 'a' * 64], ['a.md', 'a' * 64]], [1, 2, 3], ['p', 'e?', 'p']))
        self.assertEqual(session_cards.loaded(session_cards.dumped(dict(row, edits=[])) | {'edits': []}),
                         dict(row, v=1, edits=[]))

    def test_the_record_keeps_the_newest_pairs(self):
        row = session_cards.loaded(None)
        for index in range(session_cards.MAX_CARDS + 10):
            session_cards.record(row, [('c%04d.md' % index, '%064x' % index)], 'p')
        kept = session_cards.dumped(row)
        self.assertEqual((len(kept['cards']), len(kept['at']), len(kept['by'])), (session_cards.MAX_CARDS,) * 3)
        self.assertEqual(kept['cards'][0][0], 'c0010.md')
