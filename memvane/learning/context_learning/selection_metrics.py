"""Actual selection omission for an explicitly adjudicated bounded universe.

Caller supplies reviewed necessary IDs and actual emitted keep IDs. No classifier
prediction is treated as keep/drop. Retrieval misses outside the universe remain
unknown; fallback must provide the actual final IDs, not hypothetical Laya IDs.
"""

def evaluate_selection(*, reference_ids, necessary_ids, pool_ids, final_ids, fallback):
    def ids(values):
        if not isinstance(values,list) or len(values)>60 or any(not isinstance(v,str) or not v for v in values) or len(set(values))!=len(values):
            raise ValueError('invalid_selection_ids')
        return set(values)
    universe=ids(reference_ids);needed=ids(necessary_ids);pool=ids(pool_ids);kept=ids(final_ids)
    if not needed<=universe or not pool<=universe or not kept<=universe or type(fallback) is not bool:
        raise ValueError('invalid_selection_membership')
    def rate(a,b):return a/b if b else None
    observed=needed & pool
    return {'reference_scope':'adjudicated-pooled-universe', 'outside_universe':'unknown',
            'necessary_reference_count':len(needed), 'necessary_retrieved_count':len(observed),
            'retrieval_coverage':rate(len(observed),len(needed)),
            'necessary_actually_excluded_count':len(observed-kept),
            'conditional_actual_omission_rate':rate(len(observed-kept),len(observed)),
            'necessary_absent_from_final_count':len(needed-kept),
            'end_to_end_reference_omission_rate':rate(len(needed-kept),len(needed)),
            'actual_kept_count':len(kept), 'actual_dropped_pool_count':len(pool-kept),
            'fallback':fallback}
