"""Fixed generation relations and structural state guards; no live activation."""


def row_delta_statements():
    """Draft rollback delta references; retention and delete authority are separate."""
    result = [
      '''CREATE TABLE s_deltas (
        view_id TEXT NOT NULL, ordinal INTEGER NOT NULL,
        rel TEXT NOT NULL CHECK(rel IN ('card','source','card_source')),
        key_json TEXT NOT NULL CHECK(json_valid(key_json)),
        old_json TEXT CHECK(old_json IS NULL OR json_valid(old_json)),
        new_json TEXT CHECK(new_json IS NULL OR json_valid(new_json)),
        PRIMARY KEY(view_id,ordinal,rel,key_json),
        FOREIGN KEY(view_id,ordinal) REFERENCES s_generations(view_id,ordinal))''',
      '''CREATE TABLE s_delta_blobs (
        view_id TEXT NOT NULL, ordinal INTEGER NOT NULL, rel TEXT NOT NULL, key_json TEXT NOT NULL,
        side TEXT NOT NULL CHECK(side IN ('old','new')), blob_sha TEXT NOT NULL REFERENCES wiki_blobs(blob_sha),
        PRIMARY KEY(view_id,ordinal,rel,key_json,side,blob_sha),
        FOREIGN KEY(view_id,ordinal,rel,key_json) REFERENCES s_deltas(view_id,ordinal,rel,key_json))''',
      'CREATE INDEX s_delta_blobs_by_blob ON s_delta_blobs(blob_sha)',
      '''CREATE TABLE s_delta_observations (
        view_id TEXT NOT NULL, ordinal INTEGER NOT NULL, rel TEXT NOT NULL, key_json TEXT NOT NULL,
        side TEXT NOT NULL CHECK(side IN ('old','new')),
        observation_id TEXT NOT NULL REFERENCES s_source_observations(observation_id),
        PRIMARY KEY(view_id,ordinal,rel,key_json,side,observation_id),
        FOREIGN KEY(view_id,ordinal,rel,key_json) REFERENCES s_deltas(view_id,ordinal,rel,key_json))''',
      'CREATE INDEX s_delta_observations_by_observation ON s_delta_observations(observation_id)',
    ]
    for table in ('s_deltas', 's_delta_blobs', 's_delta_observations'):
        result.append(f'''CREATE TRIGGER {table}_immutable BEFORE UPDATE ON {table}
          BEGIN SELECT RAISE(ABORT,'delta_immutable'); END''')
    return result


def row_source_statements():
    """Draft source evidence and intervals; scope proof and GC guards are separate."""
    return [
      '''CREATE TABLE s_source_content (
        content_id TEXT PRIMARY KEY, source_sha TEXT NOT NULL,
        byte_length INTEGER NOT NULL CHECK(byte_length>=0), UNIQUE(source_sha))''',
      '''CREATE TABLE s_source_observations (
        observation_id TEXT PRIMARY KEY, worktree_id TEXT NOT NULL REFERENCES worktrees(worktree_id),
        branch_key TEXT NOT NULL, path TEXT NOT NULL COLLATE BINARY,
        tracked_state TEXT NOT NULL CHECK(tracked_state IN ('tracked','untracked','unknown')),
        binding_state TEXT NOT NULL CHECK(binding_state IN ('observed','legacy_unbound')),
        content_id TEXT REFERENCES s_source_content(content_id),
        legacy_fingerprint_id TEXT REFERENCES source_fingerprints(fingerprint_id),
        CHECK((content_id IS NULL)<>(legacy_fingerprint_id IS NULL)))''',
      '''CREATE TRIGGER s_source_observations_duplicate BEFORE INSERT ON s_source_observations
        WHEN EXISTS(SELECT 1 FROM s_source_observations o WHERE o.worktree_id=NEW.worktree_id
          AND o.branch_key=NEW.branch_key AND o.path=NEW.path AND o.tracked_state=NEW.tracked_state
          AND o.binding_state=NEW.binding_state AND o.content_id IS NEW.content_id
          AND o.legacy_fingerprint_id IS NEW.legacy_fingerprint_id)
        BEGIN SELECT RAISE(ABORT,'source_observation_duplicate'); END''',
      '''CREATE TRIGGER s_source_observations_immutable BEFORE UPDATE ON s_source_observations
        BEGIN SELECT RAISE(ABORT,'source_observation_immutable'); END''',
      '''CREATE TRIGGER s_source_content_immutable BEFORE UPDATE ON s_source_content
        BEGIN SELECT RAISE(ABORT,'source_content_immutable'); END''',
      '''CREATE TABLE s_attestation_sources (
        trust_digest TEXT NOT NULL REFERENCES card_attestations(trust_digest),
        ordinal INTEGER NOT NULL CHECK(ordinal>=0 AND ordinal<64),
        observation_id TEXT NOT NULL REFERENCES s_source_observations(observation_id),
        spans_json TEXT NOT NULL CHECK(json_valid(spans_json)),
        PRIMARY KEY(trust_digest,ordinal), UNIQUE(trust_digest,observation_id))''',
      '''CREATE TABLE s_sources (
        view_id TEXT NOT NULL REFERENCES views(view_id), path TEXT NOT NULL COLLATE BINARY,
        gen_from INTEGER NOT NULL, gen_to INTEGER, observation_id TEXT NOT NULL REFERENCES s_source_observations(observation_id),
        PRIMARY KEY(view_id,path,gen_from), CHECK(gen_to IS NULL OR gen_to>gen_from))''',
      'CREATE UNIQUE INDEX s_sources_open ON s_sources(view_id,path) WHERE gen_to IS NULL',
      'CREATE INDEX s_sources_gc ON s_sources(view_id,gen_to) WHERE gen_to IS NOT NULL',
      '''CREATE TRIGGER s_sources_scope BEFORE INSERT ON s_sources
        WHEN NOT EXISTS(SELECT 1 FROM views v JOIN s_source_observations o
          ON o.worktree_id=v.worktree_id AND o.branch_key=v.branch_key
          WHERE v.view_id=NEW.view_id AND o.observation_id=NEW.observation_id AND o.path=NEW.path)
        BEGIN SELECT RAISE(ABORT,'source_scope_invalid'); END''',
      '''CREATE TRIGGER s_sources_no_overlap BEFORE INSERT ON s_sources
        WHEN EXISTS(SELECT 1 FROM s_sources s WHERE s.view_id=NEW.view_id AND s.path=NEW.path
          AND (NEW.gen_to IS NULL OR s.gen_from<NEW.gen_to)
          AND (s.gen_to IS NULL OR NEW.gen_from<s.gen_to))
        BEGIN SELECT RAISE(ABORT,'source_interval_overlap'); END''',
      '''CREATE TRIGGER s_sources_close_only BEFORE UPDATE ON s_sources
        WHEN OLD.gen_to IS NOT NULL OR NEW.gen_to IS NULL OR NEW.view_id IS NOT OLD.view_id
          OR NEW.path IS NOT OLD.path OR NEW.gen_from IS NOT OLD.gen_from
          OR NEW.observation_id IS NOT OLD.observation_id
        BEGIN SELECT RAISE(ABORT,'source_interval_immutable'); END''',
      '''CREATE TABLE s_card_sources (
        view_id TEXT NOT NULL REFERENCES views(view_id), card_path TEXT NOT NULL COLLATE BINARY,
        source_path TEXT NOT NULL COLLATE BINARY, gen_from INTEGER NOT NULL, gen_to INTEGER,
        observation_id TEXT NOT NULL REFERENCES s_source_observations(observation_id),
        spans_json TEXT NOT NULL CHECK(json_valid(spans_json)),
        PRIMARY KEY(view_id,card_path,source_path,gen_from), CHECK(gen_to IS NULL OR gen_to>gen_from))''',
      'CREATE UNIQUE INDEX s_card_sources_open ON s_card_sources(view_id,card_path,source_path) WHERE gen_to IS NULL',
      'CREATE INDEX s_card_sources_gc ON s_card_sources(view_id,gen_to) WHERE gen_to IS NOT NULL',
      '''CREATE TRIGGER s_card_sources_scope BEFORE INSERT ON s_card_sources
        WHEN NOT EXISTS(SELECT 1 FROM views v JOIN s_source_observations o
          ON o.worktree_id=v.worktree_id AND o.branch_key=v.branch_key
          WHERE v.view_id=NEW.view_id AND o.observation_id=NEW.observation_id AND o.path=NEW.source_path)
        BEGIN SELECT RAISE(ABORT,'card_source_scope_invalid'); END''',
      '''CREATE TRIGGER s_card_sources_no_overlap BEFORE INSERT ON s_card_sources
        WHEN EXISTS(SELECT 1 FROM s_card_sources s WHERE s.view_id=NEW.view_id
          AND s.card_path=NEW.card_path AND s.source_path=NEW.source_path
          AND (NEW.gen_to IS NULL OR s.gen_from<NEW.gen_to)
          AND (s.gen_to IS NULL OR NEW.gen_from<s.gen_to))
        BEGIN SELECT RAISE(ABORT,'card_source_interval_overlap'); END''',
      '''CREATE TRIGGER s_card_sources_close_only BEFORE UPDATE ON s_card_sources
        WHEN OLD.gen_to IS NOT NULL OR NEW.gen_to IS NULL OR NEW.view_id IS NOT OLD.view_id
          OR NEW.card_path IS NOT OLD.card_path OR NEW.source_path IS NOT OLD.source_path
          OR NEW.gen_from IS NOT OLD.gen_from OR NEW.observation_id IS NOT OLD.observation_id
          OR NEW.spans_json IS NOT OLD.spans_json
        BEGIN SELECT RAISE(ABORT,'card_source_interval_immutable'); END''',
    ]


def row_generation_statements():
    """Draft generation/head structure; seals and deletion authority are separate."""
    return [
      '''CREATE TABLE s_generations (
        ordinal INTEGER PRIMARY KEY AUTOINCREMENT, gen_id TEXT NOT NULL UNIQUE,
        view_id TEXT NOT NULL REFERENCES views(view_id), epoch INTEGER NOT NULL CHECK(epoch>0),
        prior_ordinal INTEGER, kind TEXT NOT NULL CHECK(kind IN ('normal','rollback')),
        capture_json TEXT NOT NULL CHECK(json_valid(capture_json)), policy_digest TEXT NOT NULL,
        skip_digest TEXT NOT NULL, recipe_id TEXT REFERENCES chunk_recipes(recipe_id),
        profile_id TEXT REFERENCES vector_profiles(profile_id),
        coverage TEXT NOT NULL CHECK(coverage IN ('lexical','complete')),
        member_digest TEXT NOT NULL, source_digest TEXT NOT NULL, logical_fts_digest TEXT NOT NULL,
        physical_fts_digest TEXT NOT NULL,
        member_acc TEXT NOT NULL CHECK(json_valid(member_acc)), fts_acc TEXT NOT NULL CHECK(json_valid(fts_acc)),
        seal_digest TEXT NOT NULL, UNIQUE(view_id,epoch), UNIQUE(view_id,ordinal), UNIQUE(gen_id,view_id),
        CHECK((recipe_id IS NULL)=(profile_id IS NULL)),
        CHECK(coverage='lexical' OR recipe_id IS NOT NULL))''',
      '''CREATE TABLE s_heads (
        view_id TEXT PRIMARY KEY REFERENCES views(view_id), ordinal INTEGER NOT NULL,
        epoch INTEGER NOT NULL CHECK(epoch>0),
        FOREIGN KEY(view_id,ordinal) REFERENCES s_generations(view_id,ordinal))''',
      '''CREATE TRIGGER s_generations_immutable BEFORE UPDATE ON s_generations
        BEGIN SELECT RAISE(ABORT,'generation_immutable'); END''',
      '''CREATE TRIGGER s_heads_insert BEFORE INSERT ON s_heads
        WHEN NEW.epoch!=1 OR NOT EXISTS(SELECT 1 FROM s_generations g
          WHERE g.view_id=NEW.view_id AND g.ordinal=NEW.ordinal AND g.epoch=1
            AND g.prior_ordinal IS NULL AND g.ordinal=(
              SELECT MAX(ordinal) FROM s_generations WHERE view_id=NEW.view_id))
        BEGIN SELECT RAISE(ABORT,'view_head_invalid'); END''',
      '''CREATE TRIGGER s_heads_update BEFORE UPDATE ON s_heads
        WHEN NEW.view_id IS NOT OLD.view_id OR NEW.epoch!=OLD.epoch+1 OR NEW.ordinal<=OLD.ordinal
          OR NOT EXISTS(SELECT 1 FROM s_generations g
            WHERE g.view_id=NEW.view_id AND g.ordinal=NEW.ordinal AND g.epoch=NEW.epoch
              AND g.prior_ordinal=OLD.ordinal AND g.ordinal=(
                SELECT MAX(ordinal) FROM s_generations WHERE view_id=NEW.view_id))
        BEGIN SELECT RAISE(ABORT,'view_head_invalid'); END''',
      '''CREATE TRIGGER s_heads_delete BEFORE DELETE ON s_heads
        BEGIN SELECT RAISE(ABORT,'view_head_immutable'); END''',
    ]


def row_card_statements():
    """Draft card interval constraints; authority and GC guards are separate."""
    return [
      '''CREATE TABLE s_cards (
        view_id TEXT NOT NULL REFERENCES views(view_id), path TEXT NOT NULL COLLATE BINARY,
        gen_from INTEGER NOT NULL, gen_to INTEGER, blob_sha TEXT NOT NULL REFERENCES wiki_blobs(blob_sha),
        title TEXT NOT NULL, eligible INTEGER NOT NULL CHECK(eligible IN (0,1)),
        trust_digest TEXT, metadata_json TEXT NOT NULL CHECK(json_valid(metadata_json)),
        PRIMARY KEY(view_id,path,gen_from), CHECK(gen_to IS NULL OR gen_to>gen_from),
        CHECK(eligible=0 OR trust_digest IS NOT NULL),
        FOREIGN KEY(trust_digest,blob_sha) REFERENCES card_attestations(trust_digest,blob_sha))''',
      'CREATE UNIQUE INDEX s_cards_open ON s_cards(view_id,path) WHERE gen_to IS NULL',
      'CREATE INDEX s_cards_gc ON s_cards(view_id,gen_to) WHERE gen_to IS NOT NULL',
      '''CREATE TRIGGER s_cards_no_overlap BEFORE INSERT ON s_cards
        WHEN EXISTS (SELECT 1 FROM s_cards c WHERE c.view_id=NEW.view_id AND c.path=NEW.path
          AND (NEW.gen_to IS NULL OR c.gen_from<NEW.gen_to)
          AND (c.gen_to IS NULL OR NEW.gen_from<c.gen_to))
        BEGIN SELECT RAISE(ABORT,'card_interval_overlap'); END''',
      '''CREATE TRIGGER s_cards_close_only BEFORE UPDATE ON s_cards
        WHEN OLD.gen_to IS NOT NULL OR NEW.gen_to IS NULL
          OR NEW.view_id IS NOT OLD.view_id OR NEW.path IS NOT OLD.path
          OR NEW.gen_from IS NOT OLD.gen_from OR NEW.blob_sha IS NOT OLD.blob_sha
          OR NEW.title IS NOT OLD.title OR NEW.eligible IS NOT OLD.eligible
          OR NEW.trust_digest IS NOT OLD.trust_digest OR NEW.metadata_json IS NOT OLD.metadata_json
        BEGIN SELECT RAISE(ABORT,'card_interval_immutable'); END''',
    ]


def row_stage_statements():
    """Draft row-format stage relations; not included in the active manifest yet."""
    return [
      '''CREATE TABLE s_stage_jobs (
        view_id TEXT PRIMARY KEY REFERENCES views(view_id), job_id TEXT NOT NULL UNIQUE,
        incarnation TEXT NOT NULL, base_gen_id TEXT REFERENCES s_generations(gen_id),
        base_epoch INTEGER NOT NULL CHECK(base_epoch>=0),
        capture_json TEXT NOT NULL CHECK(json_valid(capture_json)), policy_digest TEXT NOT NULL,
        scan_digest TEXT NOT NULL, scan_complete INTEGER NOT NULL CHECK(scan_complete=1),
        cursor_path TEXT COLLATE BINARY, state TEXT NOT NULL CHECK(state IN ('collecting','ready','abandon')),
        vector_spec_json TEXT CHECK(vector_spec_json IS NULL OR json_valid(vector_spec_json)),
        discard_relation TEXT CHECK(discard_relation IS NULL OR discard_relation IN ('card_source','source','item','job')),
        discard_key TEXT COLLATE BINARY,
        CHECK((base_gen_id IS NULL)=(base_epoch=0)),
        CHECK((state='abandon' AND discard_relation IS NOT NULL) OR
              (state<>'abandon' AND discard_relation IS NULL AND discard_key IS NULL)))''',
      '''CREATE TABLE s_stage_items (
        job_id TEXT NOT NULL REFERENCES s_stage_jobs(job_id), path TEXT NOT NULL COLLATE BINARY,
        action TEXT NOT NULL CHECK(action IN ('put','delete')), input_sha TEXT,
        blob_sha TEXT REFERENCES wiki_blobs(blob_sha), trust_digest TEXT,
        metadata_json TEXT CHECK(metadata_json IS NULL OR json_valid(metadata_json)),
        PRIMARY KEY(job_id,path), FOREIGN KEY(trust_digest,blob_sha) REFERENCES card_attestations(trust_digest,blob_sha),
        CHECK((action='delete' AND input_sha IS NULL AND blob_sha IS NULL AND trust_digest IS NULL AND metadata_json IS NULL)
           OR (action='put' AND input_sha IS NOT NULL AND blob_sha IS NOT NULL AND metadata_json IS NOT NULL)))''',
      'CREATE INDEX s_stage_items_by_blob ON s_stage_items(blob_sha)',
      '''CREATE TABLE s_stage_source_changes (
        job_id TEXT NOT NULL REFERENCES s_stage_jobs(job_id), path TEXT NOT NULL COLLATE BINARY,
        action TEXT NOT NULL CHECK(action IN ('put','delete')),
        observation_id TEXT REFERENCES s_source_observations(observation_id),
        PRIMARY KEY(job_id,path),
        CHECK((action='put' AND observation_id IS NOT NULL) OR (action='delete' AND observation_id IS NULL)))''',
      '''CREATE TABLE s_stage_card_source_changes (
        job_id TEXT NOT NULL REFERENCES s_stage_jobs(job_id), card_path TEXT NOT NULL COLLATE BINARY,
        source_path TEXT NOT NULL COLLATE BINARY, action TEXT NOT NULL CHECK(action IN ('put','delete')),
        observation_id TEXT REFERENCES s_source_observations(observation_id),
        spans_json TEXT CHECK(spans_json IS NULL OR json_valid(spans_json)),
        PRIMARY KEY(job_id,card_path,source_path),
        CHECK((action='put' AND observation_id IS NOT NULL AND spans_json IS NOT NULL)
           OR (action='delete' AND observation_id IS NULL AND spans_json IS NULL)))''',
    ]


# The view's in-flight generation: its newest row, not yet a head. A
# publication inserts it, writes its rows and moves the head in one transaction.
_IN_FLIGHT = """(SELECT g.ordinal FROM s_generations g WHERE g.view_id={view}
          AND g.ordinal=(SELECT MAX(ordinal) FROM s_generations WHERE view_id={view})
          AND NOT EXISTS(SELECT 1 FROM s_heads h WHERE h.view_id={view} AND h.ordinal=g.ordinal))"""


def row_tables(text,sha,uuid,integer):
    """View registrations and reserved deletion receipts."""
    return {
      'views':tables(text,sha,uuid,integer)['views'],
      # Reserved deletion/hold proof per view path; metadata only, no writer yet.
      'delete_receipts':f'''CREATE TABLE delete_receipts(
        {sha('receipt_id')} PRIMARY KEY, {sha('view_id')} REFERENCES views(view_id),
        {text('base_gen')}, {text('path')},
        {sha('fingerprint_id')} REFERENCES source_fingerprints(fingerprint_id),
        observed_head TEXT CHECK(observed_head IS NULL OR typeof(observed_head)='text'),
        {text('proof_json')} CHECK(json_valid(proof_json)),
        {text('decision')} CHECK(decision IN ('hold','retire')), {text('reason')}, {integer('created_ns')},
        FOREIGN KEY(base_gen,view_id) REFERENCES s_generations(gen_id,view_id))''',
    }


def row_immutable_keys():
    return {'views':(('view_id',),('worktree_id','branch_key')), 'delete_receipts':(('receipt_id',),)}


def row_statements():
    """Generation, interval, delta and stage relations of the row format."""
    view_new = _IN_FLIGHT.format(view='NEW.view_id')
    result = [*row_generation_statements(), *row_card_statements(), *row_source_statements(),
              *row_delta_statements(), *row_stage_statements()]
    result.extend([
      # Ordinals never repeat, even after a generation row is collected; the
      # new generation continues exactly from the current head (or starts at 1).
      f'''CREATE TRIGGER s_generations_insert BEFORE INSERT ON s_generations
        WHEN NEW.ordinal<=COALESCE((SELECT seq FROM sqlite_sequence WHERE name='s_generations'),0)
          OR EXISTS(SELECT 1 FROM s_generations g WHERE g.view_id=NEW.view_id
            AND NOT EXISTS(SELECT 1 FROM s_heads h WHERE h.view_id=g.view_id AND h.ordinal=g.ordinal)
            AND g.ordinal>COALESCE((SELECT ordinal FROM s_heads WHERE view_id=NEW.view_id),0))
          OR NOT ((NOT EXISTS(SELECT 1 FROM s_heads WHERE view_id=NEW.view_id)
                   AND NEW.prior_ordinal IS NULL AND NEW.epoch=1)
              OR EXISTS(SELECT 1 FROM s_heads h WHERE h.view_id=NEW.view_id
                   AND NEW.prior_ordinal=h.ordinal AND NEW.epoch=h.epoch+1))
        BEGIN SELECT RAISE(ABORT,'generation_insert_invalid'); END''',
      f'''CREATE TRIGGER s_cards_in_flight BEFORE INSERT ON s_cards
        WHEN NEW.gen_from IS NOT {view_new}
        BEGIN SELECT RAISE(ABORT,'card_generation_invalid'); END''',
      f'''CREATE TRIGGER s_cards_close_in_flight BEFORE UPDATE ON s_cards
        WHEN NEW.gen_to IS NOT {_IN_FLIGHT.format(view='OLD.view_id')}
        BEGIN SELECT RAISE(ABORT,'card_generation_invalid'); END''',
      f'''CREATE TRIGGER s_deltas_in_flight BEFORE INSERT ON s_deltas
        WHEN NEW.ordinal IS NOT {view_new}
        BEGIN SELECT RAISE(ABORT,'delta_generation_invalid'); END''',
      # The head and its prior stay readable and reversible: their rows,
      # deltas and generation rows are never deleted.
      '''CREATE TRIGGER s_cards_retained BEFORE DELETE ON s_cards
        WHEN OLD.gen_to IS NULL OR OLD.gen_to>COALESCE((SELECT COALESCE(g.prior_ordinal,g.ordinal) FROM s_heads h JOIN s_generations g
          ON g.view_id=h.view_id AND g.ordinal=h.ordinal WHERE h.view_id=OLD.view_id),0)
        BEGIN SELECT RAISE(ABORT,'view_row_retained'); END''',
      '''CREATE TRIGGER s_sources_retained BEFORE DELETE ON s_sources
        WHEN OLD.gen_to IS NULL OR OLD.gen_to>COALESCE((SELECT COALESCE(g.prior_ordinal,g.ordinal) FROM s_heads h JOIN s_generations g
          ON g.view_id=h.view_id AND g.ordinal=h.ordinal WHERE h.view_id=OLD.view_id),0)
        BEGIN SELECT RAISE(ABORT,'view_row_retained'); END''',
      '''CREATE TRIGGER s_card_sources_retained BEFORE DELETE ON s_card_sources
        WHEN OLD.gen_to IS NULL OR OLD.gen_to>COALESCE((SELECT COALESCE(g.prior_ordinal,g.ordinal) FROM s_heads h JOIN s_generations g
          ON g.view_id=h.view_id AND g.ordinal=h.ordinal WHERE h.view_id=OLD.view_id),0)
        BEGIN SELECT RAISE(ABORT,'view_row_retained'); END''',
      '''CREATE TRIGGER s_deltas_retained BEFORE DELETE ON s_deltas
        WHEN (OLD.ordinal>=COALESCE((SELECT g.prior_ordinal FROM s_heads h JOIN s_generations g
          ON g.view_id=h.view_id AND g.ordinal=h.ordinal WHERE h.view_id=OLD.view_id),
          (SELECT ordinal FROM s_heads WHERE view_id=OLD.view_id), 0))
        BEGIN SELECT RAISE(ABORT,'view_delta_retained'); END''',
      '''CREATE TRIGGER s_delta_blobs_retained BEFORE DELETE ON s_delta_blobs
        WHEN (OLD.ordinal>=COALESCE((SELECT g.prior_ordinal FROM s_heads h JOIN s_generations g
          ON g.view_id=h.view_id AND g.ordinal=h.ordinal WHERE h.view_id=OLD.view_id),
          (SELECT ordinal FROM s_heads WHERE view_id=OLD.view_id), 0))
        BEGIN SELECT RAISE(ABORT,'view_delta_retained'); END''',
      '''CREATE TRIGGER s_delta_observations_retained BEFORE DELETE ON s_delta_observations
        WHEN (OLD.ordinal>=COALESCE((SELECT g.prior_ordinal FROM s_heads h JOIN s_generations g
          ON g.view_id=h.view_id AND g.ordinal=h.ordinal WHERE h.view_id=OLD.view_id),
          (SELECT ordinal FROM s_heads WHERE view_id=OLD.view_id), 0))
        BEGIN SELECT RAISE(ABORT,'view_delta_retained'); END''',
      '''CREATE TRIGGER s_generations_retained BEFORE DELETE ON s_generations
        WHEN (OLD.ordinal>=COALESCE((SELECT g.prior_ordinal FROM s_heads h JOIN s_generations g
          ON g.view_id=h.view_id AND g.ordinal=h.ordinal WHERE h.view_id=OLD.view_id),
          (SELECT ordinal FROM s_heads WHERE view_id=OLD.view_id), 0))
        BEGIN SELECT RAISE(ABORT,'view_generation_retained'); END''',
      # One bounded fill cursor per view and vector spec, bound to one head generation.
      '''CREATE TABLE s_fill_cursors (
        view_id TEXT NOT NULL REFERENCES views(view_id), recipe_id TEXT NOT NULL REFERENCES chunk_recipes(recipe_id),
        profile_id TEXT NOT NULL REFERENCES vector_profiles(profile_id), job_id TEXT NOT NULL,
        head_ordinal INTEGER NOT NULL, target_count INTEGER NOT NULL CHECK(target_count>=0),
        cursor INTEGER NOT NULL CHECK(cursor>=0 AND cursor<=target_count),
        PRIMARY KEY(view_id,recipe_id,profile_id))''',
      # Head FTS rowid of each path; changes with the head FTS in the same transaction.
      '''CREATE TABLE s_head_fts_map (
        view_id TEXT NOT NULL REFERENCES views(view_id), origin TEXT NOT NULL CHECK(origin IN ('base','sidecar')),
        path_key TEXT NOT NULL COLLATE BINARY, fts_rowid INTEGER NOT NULL,
        PRIMARY KEY(view_id,origin,path_key), UNIQUE(view_id,origin,fts_rowid))''',
      '''CREATE INDEX s_generations_by_view ON s_generations(view_id,ordinal)''',
      '''CREATE INDEX s_cards_by_blob ON s_cards(blob_sha,view_id)''',
    ])
    # Tables first, then indexes and triggers: creation order never changes the manifest.
    rank = lambda ddl: (0 if ddl.lstrip().upper().startswith('CREATE TABLE') else
                        1 if 'INDEX' in ddl.lstrip().upper().split('(')[0] else 2)
    result.sort(key=rank)
    return result
