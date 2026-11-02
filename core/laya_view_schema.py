"""Fixed generation relations and structural state guards; no live activation."""

def tables(text,sha,uuid,integer):
    return {
      'views':f'''CREATE TABLE views(
        {sha('view_id')} PRIMARY KEY, {uuid('worktree_id')} REFERENCES worktrees(worktree_id),
        {text('branch_key')}, UNIQUE(worktree_id,branch_key))''',
      'generation_slots':f'''CREATE TABLE generation_slots(
        ordinal INTEGER PRIMARY KEY AUTOINCREMENT CHECK(ordinal>0),
        {uuid('build_id')} UNIQUE, {sha('view_id')} REFERENCES views(view_id),
        {sha('manifest_digest')}, {text('stage_json')} CHECK(json_valid(stage_json)),
        {integer('created_ns')})''',
      'generations':f'''CREATE TABLE generations(
        ordinal INTEGER PRIMARY KEY REFERENCES generation_slots(ordinal) CHECK(ordinal>0),
        {text('gen_id')} UNIQUE CHECK(length(gen_id)=100 AND substr(gen_id,1,2)='g-'
          AND substr(gen_id,35,2)='-b' AND substr(gen_id,3,32) NOT GLOB '*[^0-9a-f]*'
          AND substr(gen_id,37)=binding_digest),
        {sha('view_id')} REFERENCES views(view_id), parent_gen TEXT REFERENCES generations(gen_id),
        head_oid TEXT, {sha('checkout_digest')}, {text('checkout_evidence')} CHECK(json_valid(checkout_evidence)),
        {uuid('scan_id')}, {sha('policy_digest')}, {integer('revocation_epoch')},
        {text('state')} CHECK(state IN ('building','prepared','published')),
        {sha('binding_digest')}, {integer('created_ns')}, UNIQUE(gen_id,view_id))''',
      'wiki_members':f'''CREATE TABLE wiki_members(
        {text('gen_id')} REFERENCES generations(gen_id), {text('path')},
        {sha('blob_sha')} REFERENCES wiki_blobs(blob_sha), {text('display_title')},
        {integer('eligible')} CHECK(eligible IN (0,1)), trust_digest TEXT,
        CHECK(eligible=0 OR trust_digest IS NOT NULL),
        PRIMARY KEY(gen_id,path), UNIQUE(gen_id,path,blob_sha),
        FOREIGN KEY(trust_digest,blob_sha) REFERENCES card_attestations(trust_digest,blob_sha))''',
      'source_members':f'''CREATE TABLE source_members(
        {text('gen_id')} REFERENCES generations(gen_id), {text('path')},
        {sha('fingerprint_id')} REFERENCES source_fingerprints(fingerprint_id),
        {text('presence')} CHECK(presence IN ('present','held_missing')),
        PRIMARY KEY(gen_id,path))''',
      'card_sources':f'''CREATE TABLE card_sources(
        {text('gen_id')}, {text('wiki_path')}, {integer('ordinal')},
        {sha('fingerprint_id')} REFERENCES source_fingerprints(fingerprint_id),
        {text('spans_json')} CHECK(json_valid(spans_json)), PRIMARY KEY(gen_id,wiki_path,ordinal),
        FOREIGN KEY(gen_id,wiki_path) REFERENCES wiki_members(gen_id,path))''',
      'generation_seals':f'''CREATE TABLE generation_seals(
        {text('gen_id')} PRIMARY KEY REFERENCES generations(gen_id),
        {sha('membership_digest')}, {sha('sources_digest')}, {sha('trust_digest')},
        {sha('lexical_digest')}, {sha('fts_storage_digest')}, {sha('vectors_digest')},
        {sha('binding_digest')}, {integer('eligible_count')},
        {integer('vector_member_count')} CHECK(vector_member_count=0), {integer('sealed_ns')})''',
      'view_heads':f'''CREATE TABLE view_heads(
        {sha('view_id')} PRIMARY KEY REFERENCES views(view_id), {text('gen_id')},
        {integer('epoch',1)}, FOREIGN KEY(gen_id,view_id) REFERENCES generations(gen_id,view_id))''',
    }


def immutable_keys():
    return {'generation_slots':(('ordinal',),('build_id',)), 'views':(('view_id',),('worktree_id','branch_key')),
            'wiki_members':(('gen_id','path'),), 'source_members':(('gen_id','path'),),
            'card_sources':(('gen_id','wiki_path','ordinal'),),
            'generation_seals':(('gen_id',),)}


def statements():
    result=[]
    for name in ('wiki_members','source_members','card_sources','generation_seals'):
        result.append(f'''CREATE TRIGGER {name}_building BEFORE INSERT ON {name}
          WHEN COALESCE((SELECT state FROM generations WHERE gen_id=NEW.gen_id),'missing')!='building'
          BEGIN SELECT RAISE(ABORT,'generation_not_building'); END''')
    result.extend([
      '''CREATE TRIGGER generation_insert BEFORE INSERT ON generations
        WHEN NEW.state!='building' OR EXISTS(SELECT 1 FROM generations WHERE gen_id=NEW.gen_id OR ordinal=NEW.ordinal)
          OR (NEW.parent_gen IS NOT NULL AND NOT EXISTS(
            SELECT 1 FROM generations p WHERE p.gen_id=NEW.parent_gen AND p.view_id=NEW.view_id AND p.state='published'))
        BEGIN SELECT RAISE(ABORT,'generation_insert_invalid'); END''',
      '''CREATE TRIGGER generation_delete BEFORE DELETE ON generations
        BEGIN SELECT RAISE(ABORT,'immutable_generation'); END''',
      '''CREATE TRIGGER generation_transition BEFORE UPDATE ON generations
        WHEN NOT ((OLD.state='building' AND NEW.state='prepared') OR (OLD.state='prepared' AND NEW.state='published'))
          OR NEW.ordinal IS NOT OLD.ordinal OR NEW.gen_id IS NOT OLD.gen_id
          OR NEW.view_id IS NOT OLD.view_id OR NEW.parent_gen IS NOT OLD.parent_gen
          OR NEW.head_oid IS NOT OLD.head_oid OR NEW.checkout_digest IS NOT OLD.checkout_digest
          OR NEW.checkout_evidence IS NOT OLD.checkout_evidence OR NEW.scan_id IS NOT OLD.scan_id
          OR NEW.policy_digest IS NOT OLD.policy_digest OR NEW.revocation_epoch IS NOT OLD.revocation_epoch
          OR NEW.binding_digest IS NOT OLD.binding_digest OR NEW.created_ns IS NOT OLD.created_ns
          OR NOT EXISTS(SELECT 1 FROM generation_seals s WHERE s.gen_id=NEW.gen_id AND s.binding_digest=NEW.binding_digest)
        BEGIN SELECT RAISE(ABORT,'generation_transition_invalid'); END''',
      '''CREATE TRIGGER view_head_insert BEFORE INSERT ON view_heads
        WHEN NEW.epoch!=1 OR EXISTS(SELECT 1 FROM view_heads WHERE view_id=NEW.view_id)
          OR NOT EXISTS(SELECT 1 FROM generations g JOIN generation_seals s ON s.gen_id=g.gen_id
            WHERE g.gen_id=NEW.gen_id AND g.view_id=NEW.view_id AND g.state='published')
        BEGIN SELECT RAISE(ABORT,'view_head_invalid'); END''',
      '''CREATE TRIGGER view_head_update BEFORE UPDATE ON view_heads
        WHEN NEW.view_id IS NOT OLD.view_id OR NEW.epoch!=OLD.epoch+1
          OR NOT EXISTS(SELECT 1 FROM generations g JOIN generation_seals s ON s.gen_id=g.gen_id
            WHERE g.gen_id=NEW.gen_id AND g.view_id=NEW.view_id AND g.state='published')
        BEGIN SELECT RAISE(ABORT,'view_head_invalid'); END''',
      '''CREATE TRIGGER view_head_delete BEFORE DELETE ON view_heads
        BEGIN SELECT RAISE(ABORT,'immutable_view_head'); END''',
      '''CREATE INDEX generation_by_view ON generations(view_id,ordinal)''',
      '''CREATE INDEX member_by_blob ON wiki_members(blob_sha,gen_id,path)''',
      '''CREATE INDEX source_member_by_fingerprint ON source_members(fingerprint_id,gen_id,path)''',
      '''CREATE INDEX card_source_by_fingerprint ON card_sources(fingerprint_id,gen_id,wiki_path)''',
    ])
    return result
