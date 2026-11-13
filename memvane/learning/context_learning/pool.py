"""Bounded shadow policy. Does not select production context or call models."""
from copy import deepcopy

RAW_LIMIT = 30
MAX_TOP_K = 15


def expand(phases, *, top_k, retrieve, prepare, hard_verdict, fresh_verdict,
           cutoff, is_wiki, hierarchical=False):
    if type(top_k) is not int or not 1 <= top_k <= MAX_TOP_K:
        raise ValueError('invalid_candidate_limit')
    expanded=[]; decisions={}; seen=set(); accepted=[]
    for phase in phases:
        # Reuse only an exhaustively short response or a full fixture/over-return.
        # A full 8-result live response cannot prove that rank 9 is absent.
        cached=phase.get('unbounded_results')
        if cached is not None:
            count=len(cached)
            hits=deepcopy(cached[:RAW_LIMIT])
        elif phase['returned_count'] < 8:
            count=phase['returned_count']
            hits=deepcopy(phase['results'][:RAW_LIMIT])
        else:
            hits=retrieve(phase, RAW_LIMIT)
            if hits is None:
                raise ValueError('expanded_query_failed')
            count=len(hits)
            hits=deepcopy(hits[:RAW_LIMIT])
        prepare(hits)
        hits.sort(key=lambda r:r.get('score',0),reverse=True)
        name=phase['name'];scope=phase['wiki_scoped'];threshold=cutoff(name)
        hard={id(h):hard_verdict(h,scope) for h in hits}
        eligible=[h for h in hits if hard[id(h)]=='eligible' and h.get('score',0)>=threshold]
        # Preserve the existing single rescue before freshness, never after it.
        if not eligible and any(h.get('score',0)>=threshold for h in hits):
            safe=[h for h in hits if hard[id(h)]=='eligible']
            wiki=[h for h in safe if is_wiki(h)] if hierarchical else []
            eligible=(wiki or safe)[:1]
        if hierarchical:
            wiki=[h for h in eligible if is_wiki(h)]
            eligible=wiki or eligible
        chosen={id(h) for h in eligible}
        for h in hits:
            cid=h.get('file','');cid=cid[6:] if cid.startswith('qmd://') else cid
            key=id(h)
            reason=hard[id(h)]
            if reason=='eligible' and id(h) not in chosen:
                reason='score_or_wiki_priority'
            if reason=='eligible':
                reason=fresh_verdict(h,scope)
            if reason=='eligible' and cid in seen:
                reason='duplicate'
            if reason=='eligible' and len(accepted)>=top_k:
                reason='candidate_budget'
            if reason=='eligible':
                seen.add(cid);accepted.append(cid)
            decisions[key]=reason
        expanded.append(dict(name=name,results=hits,wiki_scoped=scope,returned_count=count))
    # A wiki-primary and raw fallback are observed only if baseline queried both.
    # Preserve wiki priority in the combined observation pool as well.
    if hierarchical:
        wiki_ids={h.get('file','').removeprefix('qmd://') for p in expanded for h in p['results']
                  if is_wiki(h) and decisions.get(id(h))=='eligible'}
        if wiki_ids:
            for p in expanded:
                for h in p['results']:
                    key=id(h)
                    if decisions.get(key)=='eligible' and not is_wiki(h):decisions[key]='wiki_priority'
    def verdict(hit,scope):
        # observe calls phases serially, but names may contain the same ID;
        # attach policy reason to each copied hit instead of ambiguous ID lookup.
        return hit['_learning_pool_verdict']
    for p in expanded:
        for h in p['results']:
            h['_learning_pool_verdict']=decisions[id(h)]
    return expanded,verdict
