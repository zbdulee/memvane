"""Shared learning types."""
from __future__ import annotations

# File name of the frozen learning-profile install proposal under the
# learning namespace (``learning_profile.INSTALL``).
LEARNING_PROFILE_INSTALL = 'learning-profile-install.json'

# Reasons in ``select_published``'s result that the selected recall reads.
# ``selected`` is the selector's final choice (zero cards included) and
# ``no_candidates`` an empty published pool: both answer the recall.
# ``candidate_invalid``: the pool broke the published-row contract and is not
# served. Any other reason means the selector could not answer.
SELECTOR_ANSWERS = ('selected', 'no_candidates')
SELECTOR_CANDIDATE_INVALID = 'candidate_invalid'
