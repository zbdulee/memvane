// Safe Markdown renderer (docs/design-local-web-ui.md §3.3). A small tokenizer
// builds Preact VNodes directly: raw HTML is never interpreted (it renders as
// text), links keep only `https://` and in-app `#wiki?target=…&card=…` targets,
// and every text node passes through safeText/safeMultiline so control and
// Bidi override characters show as visible escapes.

import type {ComponentChildren, VNode} from 'preact';
import {safeMultiline, safeText} from '../text';

export interface SafeMarkdownProps {
  markdown: string;
  /** Target of the card being shown; enables relative `.md` and `[[slug]]` card links. */
  targetId?: string;
  /** Wiki-relative path of the card being shown; relative links resolve from its folder. */
  cardRel?: string;
  /** Maps a `[[slug]]` to a wiki-relative card path, or null when no card matches. */
  resolveCard?: (name: string) => string | null;
}

interface LinkContext {
  targetId: string | undefined;
  cardRel: string | undefined;
  resolveCard: ((name: string) => string | null) | undefined;
}

const TARGET_ID = /^[0-9a-f]{32}(:[0-9a-f]{12})?$/;
const MAX_CARD_REL = 1024;

/** The in-app hash of one card, or null when the target or path is not acceptable. */
export function cardHash(targetId: string, rel: string): string | null {
  if (!TARGET_ID.test(targetId) || !safeCardRel(rel)) return null;
  return `#wiki?target=${encodeURIComponent(targetId)}&card=${encodeURIComponent(rel)}`;
}

function safeCardRel(rel: string): boolean {
  if (rel.length === 0 || rel.length > MAX_CARD_REL || rel.startsWith('/') || rel.includes('\\')) return false;
  if (/[\u0000-\u001f\u007f-\u009f\u2028\u2029]/.test(rel)) return false;
  return rel.endsWith('.md') && rel.split('/').every(part => part !== '' && part !== '.' && part !== '..');
}

/** Normalizes a relative `.md` link against the folder of `cardRel`; null when it leaves the wiki. */
function resolveRelative(href: string, cardRel: string): string | null {
  const clean = href.split('#')[0]?.split('?')[0] ?? '';
  let decoded: string;
  try {
    decoded = decodeURIComponent(clean);
  } catch {
    return null;
  }
  const parts = cardRel.split('/').slice(0, -1);
  for (const part of decoded.split('/')) {
    if (part === '' || part === '.') continue;
    if (part === '..') {
      if (parts.length === 0) return null;
      parts.pop();
    } else {
      parts.push(part);
    }
  }
  const rel = parts.join('/');
  return safeCardRel(rel) ? rel : null;
}

/** An allowed href (`https://` or an in-app card hash), or null to render the link as text. */
export function safeHref(raw: string, ctx: Partial<LinkContext> = {}): string | null {
  // Browsers drop tabs and newlines inside a URL; strip them (and blanks) before judging the scheme.
  const href = raw.replace(/[\u0000-\u0020\u007f]/g, '');
  if (/^https:\/\//i.test(href)) {
    try {
      const url = new URL(href);
      return url.protocol === 'https:' && url.hostname !== '' ? url.href : null;
    } catch {
      return null;
    }
  }
  if (href.startsWith('#wiki?')) {
    const params = new URLSearchParams(href.slice('#wiki?'.length));
    const keys = [...params.keys()];
    const target = params.get('target');
    const card = params.get('card');
    if (keys.length !== 2 || target === null || card === null) return null;
    return cardHash(target, card);
  }
  // Anything with a scheme (javascript:, data:, vbscript:, http:, file:, …) or a protocol-relative URL is dropped.
  if (/^[a-z][a-z0-9+.-]*:/i.test(href) || href.startsWith('//')) return null;
  if (ctx.targetId !== undefined && ctx.cardRel !== undefined && /\.md(?:[#?].*)?$/i.test(href)) {
    const rel = resolveRelative(href, ctx.cardRel);
    return rel === null ? null : cardHash(ctx.targetId, rel);
  }
  return null;
}

// ---------------------------------------------------------------- inline

type InlineRule = {pattern: RegExp; render: (match: RegExpExecArray, ctx: LinkContext, key: number) => ComponentChildren};

function link(href: string | null, label: ComponentChildren, key: number): ComponentChildren {
  if (href === null) return <span key={key} class="md-link-dropped">{label}</span>;
  if (!href.startsWith('#')) {
    return (
      <a key={key} href={href} target="_blank" rel="noopener noreferrer" class="md-link md-link-external">
        {label}<span class="md-external" aria-hidden="true"> ↗</span>
      </a>
    );
  }
  return <a key={key} href={href} class="md-link md-link-card">{label}</a>;
}

const INLINE_RULES: readonly InlineRule[] = [
  {pattern: /`([^`\n]+)`/y, render: (m, _ctx, key) => <code key={key} class="md-code">{safeText(m[1])}</code>},
  {
    pattern: /\[\[([^\]\n]{1,200})\]\]/y,
    render: (m, ctx, key) => {
      const name = (m[1] ?? '').trim();
      const rel = ctx.resolveCard?.(name) ?? null;
      const href = rel !== null && ctx.targetId !== undefined ? cardHash(ctx.targetId, rel) : null;
      return link(href, safeText(name), key);
    },
  },
  {
    pattern: /\[([^\]\n]{1,500})\]\(([^()\s]{1,2048})(?:\s+"[^"\n]*")?\)/y,
    render: (m, ctx, key) => link(safeHref(m[2] ?? '', ctx), renderInline(m[1] ?? '', ctx), key),
  },
  {
    pattern: /<(https:\/\/[^\s<>]{1,2048})>/y,
    render: (m, ctx, key) => link(safeHref(m[1] ?? '', ctx), safeText(m[1]), key),
  },
  {pattern: /\*\*([^*\n](?:[^\n]*?[^*\n])?)\*\*/y, render: (m, ctx, key) => <strong key={key}>{renderInline(m[1] ?? '', ctx)}</strong>},
  {pattern: /__([^_\n](?:[^\n]*?[^_\n])?)__/y, render: (m, ctx, key) => <strong key={key}>{renderInline(m[1] ?? '', ctx)}</strong>},
  {pattern: /~~([^~\n]+)~~/y, render: (m, ctx, key) => <del key={key}>{renderInline(m[1] ?? '', ctx)}</del>},
  {pattern: /\*([^*\s](?:[^*\n]*[^*\s])?)\*/y, render: (m, ctx, key) => <em key={key}>{renderInline(m[1] ?? '', ctx)}</em>},
  {pattern: /(?<![A-Za-z0-9])_([^_\s](?:[^_\n]*[^_\s])?)_(?![A-Za-z0-9])/y, render: (m, ctx, key) => <em key={key}>{renderInline(m[1] ?? '', ctx)}</em>},
];

const INLINE_START = /[`[<*_~]/g;

function renderInline(text: string, ctx: LinkContext): ComponentChildren[] {
  const out: ComponentChildren[] = [];
  let plain = '';
  let index = 0;
  const flush = () => {
    if (plain !== '') out.push(safeText(plain));
    plain = '';
  };
  while (index < text.length) {
    INLINE_START.lastIndex = index;
    const next = INLINE_START.exec(text);
    if (next === null) {
      plain += text.slice(index);
      break;
    }
    plain += text.slice(index, next.index);
    index = next.index;
    let matched = false;
    for (const rule of INLINE_RULES) {
      rule.pattern.lastIndex = index;
      const match = rule.pattern.exec(text);
      if (match !== null && match.index === index) {
        flush();
        out.push(rule.render(match, ctx, out.length));
        index += match[0].length;
        matched = true;
        break;
      }
    }
    if (!matched) {
      plain += text[index];
      index += 1;
    }
  }
  flush();
  return out;
}

// ---------------------------------------------------------------- blocks

const FENCE = /^ {0,3}(`{3,}|~{3,})\s*([\w+-]*)\s*$/;
const HEADING = /^ {0,3}(#{1,6})\s+(.*?)\s*#*\s*$/;
const RULE = /^ {0,3}([-*_])(\s*\1){2,}\s*$/;
const BULLET = /^\s*[-*+]\s+(.*)$/;
const ORDERED = /^\s*(\d{1,9})[.)]\s+(.*)$/;
const QUOTE = /^ {0,3}>\s?(.*)$/;
const TABLE_SEP = /^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$/;

function cells(line: string): string[] {
  const trimmed = line.trim().replace(/^\|/, '').replace(/\|$/, '');
  return trimmed.split('|').map(cell => cell.trim());
}

function heading(level: number, children: ComponentChildren, key: number): VNode {
  // Card headings sit under the page and card titles, so they start at h3.
  switch (Math.min(6, level + 2)) {
    case 3: return <h3 key={key} class="md-h">{children}</h3>;
    case 4: return <h4 key={key} class="md-h">{children}</h4>;
    case 5: return <h5 key={key} class="md-h">{children}</h5>;
    default: return <h6 key={key} class="md-h">{children}</h6>;
  }
}

function isBlockStart(line: string): boolean {
  return FENCE.test(line) || HEADING.test(line) || RULE.test(line) || BULLET.test(line)
    || ORDERED.test(line) || QUOTE.test(line);
}

function renderBlocks(lines: readonly string[], ctx: LinkContext, depth: number): VNode[] {
  const out: VNode[] = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i] ?? '';
    if (line.trim() === '') {
      i += 1;
      continue;
    }
    const fence = FENCE.exec(line);
    if (fence !== null) {
      const marker = fence[1] ?? '```';
      const body: string[] = [];
      i += 1;
      while (i < lines.length && !(lines[i] ?? '').trim().startsWith(marker)) {
        body.push(lines[i] ?? '');
        i += 1;
      }
      i += 1;
      out.push(<pre key={out.length} class="code-block md-pre"><code>{safeMultiline(body.join('\n'))}</code></pre>);
      continue;
    }
    const head = HEADING.exec(line);
    if (head !== null) {
      out.push(heading((head[1] ?? '#').length, renderInline(head[2] ?? '', ctx), out.length));
      i += 1;
      continue;
    }
    if (RULE.test(line)) {
      out.push(<hr key={out.length} class="md-hr" />);
      i += 1;
      continue;
    }
    if (QUOTE.test(line)) {
      const body: string[] = [];
      while (i < lines.length && QUOTE.test(lines[i] ?? '')) {
        body.push(QUOTE.exec(lines[i] ?? '')?.[1] ?? '');
        i += 1;
      }
      const inner = depth < 8 ? renderBlocks(body, ctx, depth + 1) : [<p key={0}>{safeText(body.join(' '))}</p>];
      out.push(<blockquote key={out.length} class="md-quote">{inner}</blockquote>);
      continue;
    }
    if (BULLET.test(line) || ORDERED.test(line)) {
      const ordered = !BULLET.test(line);
      const items: ComponentChildren[] = [];
      const start = ordered ? Number(ORDERED.exec(line)?.[1] ?? '1') : 1;
      while (i < lines.length) {
        const current = lines[i] ?? '';
        const item = ordered ? ORDERED.exec(current) : BULLET.exec(current);
        if (item === null) {
          // A lazily continued item line joins the previous item.
          if (current.trim() !== '' && /^\s{2,}\S/.test(current) && items.length > 0) {
            items.push(<br key={`${items.length}-br`} />, ...renderInline(current.trim(), ctx));
            i += 1;
            continue;
          }
          break;
        }
        const text = (ordered ? item[2] : item[1]) ?? '';
        const task = /^\[([ xX])\]\s+(.*)$/.exec(text);
        items.push(
          <li key={items.length} class={task === null ? undefined : 'md-task'}>
            {task === null ? renderInline(text, ctx) : [task[1] === ' ' ? '☐ ' : '☑ ', ...renderInline(task[2] ?? '', ctx)]}
          </li>,
        );
        i += 1;
      }
      out.push(ordered
        ? <ol key={out.length} class="md-list" start={start}>{items}</ol>
        : <ul key={out.length} class="md-list">{items}</ul>);
      continue;
    }
    if (line.includes('|') && TABLE_SEP.test(lines[i + 1] ?? '')) {
      const header = cells(line);
      const rows: string[][] = [];
      i += 2;
      while (i < lines.length && (lines[i] ?? '').includes('|') && (lines[i] ?? '').trim() !== '') {
        rows.push(cells(lines[i] ?? ''));
        i += 1;
      }
      out.push(
        <div key={out.length} class="table-wrap md-table">
          <table>
            <thead><tr>{header.map((cell, c) => <th key={c}>{renderInline(cell, ctx)}</th>)}</tr></thead>
            <tbody>
              {rows.map((row, r) => (
                <tr key={r}>{header.map((_, c) => <td key={c}>{renderInline(row[c] ?? '', ctx)}</td>)}</tr>
              ))}
            </tbody>
          </table>
        </div>,
      );
      continue;
    }
    const para: string[] = [line.trim()];
    i += 1;
    while (i < lines.length && (lines[i] ?? '').trim() !== '' && !isBlockStart(lines[i] ?? '')) {
      para.push((lines[i] ?? '').trim());
      i += 1;
    }
    out.push(<p key={out.length} class="md-p">{renderInline(para.join(' '), ctx)}</p>);
  }
  return out;
}

export function SafeMarkdown({markdown, targetId, cardRel, resolveCard}: SafeMarkdownProps) {
  const ctx: LinkContext = {targetId, cardRel, resolveCard};
  const lines = markdown.replace(/\r\n?/g, '\n').split('\n');
  return <div class="md-body">{renderBlocks(lines, ctx, 0)}</div>;
}
