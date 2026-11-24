"""Cross-domain fixtures for a compiler driving the verifier's durable work.

Use the verifier's own owner, quota and snapshot operations so assertions see
the same identities and bytes as a verifier stopped during an actual claim.
"""
from memvane.wiki.verify.tests import support as verifier

waiting_reviews = verifier.waiting_reviews
deferred_replacements = verifier.deferred_replacements
retained_batches = verifier.retained_batches
full_copy_quota = verifier.full_copy_quota


def held_replacement(queue, sha):
    matching = [work for work in waiting_reviews(queue)
                if work.state['binding']['job'].get('pendingPath') and
                any(row['sha256'] == sha for row in work.state['binding']['job']['sourceRevisions'])]
    work, = matching
    return work


def during_native_review(queue, bound, work, callback):
    return verifier.during_native_review(queue, bound, work, callback)
