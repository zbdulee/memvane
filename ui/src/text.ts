// Display helpers. Preact escapes text nodes; these only make invisible or
// direction-changing characters visible so a path or branch name cannot
// disguise itself.

const BIDI = /[‪-‮⁦-⁩]/g;
// C0 controls except tab, DEL, and C1 controls.
const CONTROL = /[\u0000-\u0008\u000A-\u001F\u007F-\u009F]/g;

function codePoint(ch: string): string {
  return `<U+${(ch.codePointAt(0) ?? 0).toString(16).toUpperCase().padStart(4, '0')}>`;
}

// Multi-line blocks keep line feeds; carriage returns and other controls stay visible.
const CONTROL_MULTILINE = /[\u0000-\u0008\u000B-\u001F\u007F-\u009F]/g;

export function safeText(value: string | null | undefined): string {
  if (value === null || value === undefined) return '';
  return value.replace(BIDI, codePoint).replace(CONTROL, codePoint);
}

export function safeMultiline(value: string | null | undefined): string {
  if (value === null || value === undefined) return '';
  return value.replace(BIDI, codePoint).replace(CONTROL_MULTILINE, codePoint);
}

const NUMBER = new Intl.NumberFormat('ko-KR');

export function formatCount(value: number | null | undefined): string {
  return value === null || value === undefined ? '—' : NUMBER.format(value);
}

export function formatMs(value: number | null | undefined): string {
  return value === null || value === undefined ? '—' : `${NUMBER.format(Math.round(value))}ms`;
}

const RELATIVE = new Intl.RelativeTimeFormat('ko', {numeric: 'auto'});

export function formatRelative(iso: string | null | undefined, now: number = Date.now()): string {
  if (iso === null || iso === undefined) return '—';
  const at = Date.parse(iso);
  if (Number.isNaN(at)) return safeText(iso);
  const seconds = Math.round((at - now) / 1000);
  const abs = Math.abs(seconds);
  if (abs < 45) return '방금 전';
  if (abs < 3600) return RELATIVE.format(Math.round(seconds / 60), 'minute');
  if (abs < 86400) return RELATIVE.format(Math.round(seconds / 3600), 'hour');
  return RELATIVE.format(Math.round(seconds / 86400), 'day');
}

export function formatClock(iso: string | null | undefined): string {
  if (iso === null || iso === undefined) return '—';
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return safeText(iso);
  return at.toLocaleString('ko-KR', {hour12: false});
}

export function formatDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return '—';
  const total = Math.max(0, Math.floor(seconds));
  const minutes = Math.floor(total / 60);
  const rest = total % 60;
  return minutes > 0 ? `${minutes}분 ${rest.toString().padStart(2, '0')}초` : `${rest}초`;
}
