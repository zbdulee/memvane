"""Fill-job progress relations: immutable identity/targets, forward-only cursor.

Jobs are progress metadata only. Execution ownership stays with the content
filler's OS lease; there is no DB lease, TTL or delete path here.
"""

MAX_TARGETS = 20000
MAX_TARGET_BYTES = 4 * 1024 * 1024


def tables(text, sha, uuid, integer):
    return {
      'fill_jobs': f'''CREATE TABLE fill_jobs(
        {sha('job_id')} PRIMARY KEY, {uuid('incarnation')}, {sha('view_id')},
        {uuid('worktree_id')} REFERENCES worktrees(worktree_id),
        {sha('recipe_id')} REFERENCES chunk_recipes(recipe_id),
        {sha('profile_id')} REFERENCES vector_profiles(profile_id),
        {sha('manifest_digest')}, {sha('targets_digest')},
        {text('identity_json')} CHECK(json_valid(identity_json)),
        {integer('target_count')} CHECK(target_count<={MAX_TARGETS}),
        {integer('cursor')} CHECK(cursor<=target_count), {integer('created_ns')})''',
      'fill_targets': f'''CREATE TABLE fill_targets(
        {sha('job_id')} REFERENCES fill_jobs(job_id),
        {integer('ordinal')} CHECK(ordinal<{MAX_TARGETS}), {text('path')},
        {sha('blob_sha')} REFERENCES wiki_blobs(blob_sha),
        {integer('byte_length')} CHECK(byte_length<={MAX_TARGET_BYTES}),
        PRIMARY KEY(job_id,ordinal), UNIQUE(job_id,path))''',
    }


def immutable_keys():
    return {'fill_targets': (('job_id', 'ordinal'), ('job_id', 'path'))}


IDENTITY = ('job_id', 'incarnation', 'view_id', 'worktree_id', 'recipe_id', 'profile_id',
            'manifest_digest', 'targets_digest', 'identity_json', 'target_count', 'created_ns')


def statements():
    changed = ' OR '.join(f'NEW.{c} IS NOT OLD.{c}' for c in IDENTITY)
    return [
      # Fresh jobs start at cursor 0; an existing job_id refuses REPLACE.
      '''CREATE TRIGGER fill_job_insert BEFORE INSERT ON fill_jobs
        WHEN NEW.cursor!=0 OR EXISTS(SELECT 1 FROM fill_jobs WHERE job_id=NEW.job_id)
        BEGIN SELECT RAISE(ABORT,'fill_job_insert_invalid'); END''',
      f'''CREATE TRIGGER fill_job_cursor BEFORE UPDATE ON fill_jobs
        WHEN {changed} OR typeof(NEW.cursor)!='integer'
          OR NEW.cursor<=OLD.cursor OR NEW.cursor>OLD.target_count
          OR NEW.cursor>(SELECT COUNT(*) FROM fill_targets WHERE job_id=OLD.job_id)
        BEGIN SELECT RAISE(ABORT,'fill_cursor_invalid'); END''',
      '''CREATE TRIGGER fill_job_delete BEFORE DELETE ON fill_jobs
        BEGIN SELECT RAISE(ABORT,'immutable_fill_job'); END''',
      # Targets exist only for a not-yet-advanced job and never exceed its count.
      '''CREATE TRIGGER fill_target_insert BEFORE INSERT ON fill_targets
        WHEN COALESCE((SELECT cursor=0 AND NEW.ordinal<target_count FROM fill_jobs
          WHERE job_id=NEW.job_id),0)=0
        BEGIN SELECT RAISE(ABORT,'fill_target_invalid'); END''',
    ]
