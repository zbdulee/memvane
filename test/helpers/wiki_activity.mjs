// Fixture location of the wiki activity log (core/wiki_activity_log.py).
//
// Synthetic fixtures have no selected install, so the log falls back to the
// per-user auto-context state under HOME, keyed like scan_state.project_key
// (sha256 of "<resolved project root>\n"). Computed independently of the
// Python module so a test catches a log written anywhere else.
import { createHash } from 'node:crypto';
import { existsSync, readFileSync, realpathSync } from 'node:fs';
import { homedir } from 'node:os';
import { join } from 'node:path';

export function activityLogPath(project) {
  const key = createHash('sha256').update(`${realpathSync(project)}\n`).digest('hex');
  return join(homedir(), '.local', 'state', 'memvane', 'auto-context', 'wiki-activity', `${key}.log`);
}

export function readActivityLog(project) {
  const path = activityLogPath(project);
  return existsSync(path) ? readFileSync(path, 'utf8') : '';
}
