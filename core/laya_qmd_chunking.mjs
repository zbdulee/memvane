// Regex/token chunking port from pinned QMD fork store.ts (MIT).
// A caller supplies the tokenizer. This module never loads a model or QMD store.
// Markdown regex mode is supported; AST mode and QMD's lossy detokenize
// truncation branch fail closed rather than silently changing source content.

export const CHUNK_SIZE_TOKENS = 900;
export const CHUNK_OVERLAP_TOKENS = 135;
export const CHUNK_WINDOW_TOKENS = 200;
export const CHUNK_SIZE_CHARS = CHUNK_SIZE_TOKENS * 4;
export const CHUNK_OVERLAP_CHARS = CHUNK_OVERLAP_TOKENS * 4;
export const CHUNK_WINDOW_CHARS = CHUNK_WINDOW_TOKENS * 4;

export function extractQmdMarkdownTitle(content, filename) {
  if (typeof content !== 'string' || typeof filename !== 'string')
    throw new LayaChunkingError('invalid_title_source');
  const match = content.match(/^##?\s+(.+)$/m);
  if (match) {
    const title = (match[1] || '').trim();
    if (title === '📝 Notes' || title === 'Notes') {
      const next = content.match(/^##\s+(.+)$/m);
      if (next?.[1]) return next[1].trim();
    }
    return title;
  }
  return filename.replace(/\.[^.]+$/, '').split('/').pop() || filename;
}

export class LayaChunkingError extends Error {
  constructor(code) {
    super(code);
    this.name = 'LayaChunkingError';
    this.code = code;
  }
}

const BREAK_PATTERNS = [
  [/\n#{1}(?!#)/g, 100, 'h1'],
  [/\n#{2}(?!#)/g, 90, 'h2'],
  [/\n#{3}(?!#)/g, 80, 'h3'],
  [/\n#{4}(?!#)/g, 70, 'h4'],
  [/\n#{5}(?!#)/g, 60, 'h5'],
  [/\n#{6}(?!#)/g, 50, 'h6'],
  [/\n```/g, 80, 'codeblock'],
  [/\n(?:---|\*\*\*|___)\s*\n/g, 60, 'hr'],
  [/\n\n+/g, 20, 'blank'],
  [/\n[-*]\s/g, 5, 'list'],
  [/\n\d+\.\s/g, 5, 'numlist'],
  [/\n/g, 1, 'newline'],
];

export function scanBreakPoints(text) {
  const seen = new Map();
  for (const [pattern, score, type] of BREAK_PATTERNS) {
    for (const match of text.matchAll(pattern)) {
      const pos = match.index;
      const old = seen.get(pos);
      if (!old || score > old.score) seen.set(pos, {pos, score, type});
    }
  }
  return [...seen.values()].sort((a, b) => a.pos - b.pos);
}

export function findCodeFences(text) {
  const regions = [];
  let start = null;
  for (const match of text.matchAll(/\n```/g)) {
    if (start === null) start = match.index;
    else {
      regions.push({start, end: match.index + match[0].length});
      start = null;
    }
  }
  if (start !== null) regions.push({start, end: text.length});
  return regions;
}

export function findBestCutoff(points, target, windowChars, fences = []) {
  let bestScore = -1;
  let bestPos = target;
  for (const point of points) {
    if (point.pos < target - windowChars) continue;
    if (point.pos > target) break;
    if (fences.some(f => point.pos > f.start && point.pos < f.end)) continue;
    const distance = (target - point.pos) / windowChars;
    const score = point.score * (1 - distance * distance * 0.7);
    if (score > bestScore) {
      bestScore = score;
      bestPos = point.pos;
    }
  }
  return bestPos;
}

function atSurrogatePair(content, pos) {
  if (pos <= 0 || pos >= content.length) return false;
  const high = content.charCodeAt(pos - 1);
  const low = content.charCodeAt(pos);
  return high >= 0xd800 && high <= 0xdbff && low >= 0xdc00 && low <= 0xdfff;
}

function adjustSurrogateBoundary(content, pos, floor) {
  if (!atSurrogatePair(content, pos)) return pos;
  return pos - 1 > floor ? pos - 1 : pos + 1;
}

export function chunkDocument(content, maxChars = CHUNK_SIZE_CHARS,
                              overlapChars = CHUNK_OVERLAP_CHARS,
                              windowChars = CHUNK_WINDOW_CHARS) {
  if (content.length <= maxChars) return [{text: content, pos: 0}];
  const points = scanBreakPoints(content);
  const fences = findCodeFences(content);
  const chunks = [];
  let pos = 0;
  while (pos < content.length) {
    const target = Math.min(pos + maxChars, content.length);
    let end = target;
    if (end < content.length) {
      const cut = findBestCutoff(points, target, windowChars, fences);
      if (cut > pos && cut <= target) end = cut;
    }
    if (end <= pos) end = Math.min(pos + maxChars, content.length);
    end = adjustSurrogateBoundary(content, end, pos);
    chunks.push({text: content.slice(pos, end), pos});
    if (end >= content.length) break;
    let next = end - overlapChars;
    if (next <= pos) next = end;
    pos = adjustSurrogateBoundary(content, next, pos);
  }
  return chunks;
}

function stripUnpairedSurrogates(text) {
  let start = 0, end = text.length;
  if (end > 0) {
    const first = text.charCodeAt(0);
    if (first >= 0xdc00 && first <= 0xdfff) start = 1;
    else if (first >= 0xd800 && first <= 0xdbff) {
      const second = end > 1 ? text.charCodeAt(1) : NaN;
      if (!(second >= 0xdc00 && second <= 0xdfff)) start = 1;
    }
  }
  if (end > start) {
    const last = text.charCodeAt(end - 1);
    if (last >= 0xd800 && last <= 0xdbff) end--;
    else if (last >= 0xdc00 && last <= 0xdfff) {
      const prev = end - 1 > start ? text.charCodeAt(end - 2) : NaN;
      if (!(prev >= 0xd800 && prev <= 0xdbff)) end--;
    }
  }
  return text.slice(start, end);
}

function clampOverlap(value, maxChars) {
  if (maxChars <= 1) return 0;
  return Math.max(0, Math.min(maxChars - 1, Math.floor(value)));
}

export async function chunkDocumentByTokens(content, tokenizer, strategy = 'regex') {
  if (typeof content !== 'string' || content.length > 5_000_000)
    throw new LayaChunkingError('invalid_content');
  if (strategy !== 'regex') throw new LayaChunkingError('unsupported_chunk_strategy');
  if (!tokenizer || typeof tokenizer.tokenize !== 'function')
    throw new LayaChunkingError('invalid_tokenizer');
  const overlapChars = CHUNK_OVERLAP_TOKENS * 3;
  const windowChars = CHUNK_WINDOW_TOKENS * 3;
  const firstPass = chunkDocument(content, CHUNK_SIZE_TOKENS * 3,
                                  overlapChars, windowChars);
  const results = [];
  async function visit(text, pos, depth) {
    if (depth > 32) throw new LayaChunkingError('chunk_recursion_limit');
    const tokens = await tokenizer.tokenize(text);
    if (!tokens || !Number.isSafeInteger(tokens.length) || tokens.length < 0)
      throw new LayaChunkingError('invalid_tokenize_result');
    if (tokens.length <= CHUNK_SIZE_TOKENS || text.length <= 1) {
      const safe = stripUnpairedSurrogates(text);
      if (safe) results.push({text: safe, pos, tokens: tokens.length});
      return;
    }
    const ratio = text.length / tokens.length;
    let maxChars = Math.floor(CHUNK_SIZE_TOKENS * ratio * 0.95);
    if (!Number.isFinite(maxChars) || maxChars < 1) maxChars = Math.floor(text.length / 2);
    maxChars = Math.max(1, Math.min(text.length - 1, maxChars));
    let overlap = clampOverlap(overlapChars * ratio / 2, maxChars);
    let window = Math.max(0, Math.floor(windowChars * ratio / 2));
    let parts = chunkDocument(text, maxChars, overlap, window);
    if (parts.length <= 1 || parts[0].text.length === text.length) {
      maxChars = Math.max(1, Math.floor(text.length / 2));
      overlap = window = 0;
      parts = chunkDocument(text, maxChars, overlap, window);
    }
    if (parts.length <= 1 || parts[0].text.length === text.length)
      throw new LayaChunkingError('token_truncation_fallback');
    for (const part of parts)
      await visit(text.slice(part.pos, part.pos + part.text.length), pos + part.pos, depth + 1);
  }
  for (const part of firstPass) await visit(part.text, part.pos, 0);
  return results;
}
