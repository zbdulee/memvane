// Wiki card browser and side-by-side source viewer (docs/design-local-web-ui.md §4.3.3).
// API calls arrive as props (`loadList`, `loadCard`, `loadSource`) so the page
// carries no client binding of its own. The open card lives in the URL hash
// (`#wiki?target=…&card=…`), which is also the shape of in-card links.

import {useEffect, useMemo, useRef, useState} from 'preact/hooks';
import type {
  SourceSpan,
  TargetId,
  WikiCardDetail,
  WikiCardMeta,
  WikiLedgerSummary,
  WikiListFilter,
  WikiStaleReason,
  WikiListResponse,
  WikiSourceResponse,
  WorktreeNode,
} from '../api/types';
import {Badge} from '../components/Badge';
import {SafeMarkdown, cardHash} from '../components/SafeMarkdown';
import {formatClock, formatCount, formatRelative, safeText} from '../text';

export interface WikiListQuery {
  status: WikiListFilter;
  q: string;
  limit: number;
}

export type LineRange = {startLine: number; endLine: number};

export interface WikiLoaders {
  loadList: (targetId: TargetId, query: WikiListQuery) => Promise<WikiListResponse>;
  loadCard: (targetId: TargetId, rel: string) => Promise<WikiCardDetail>;
  loadSource: (targetId: TargetId, rel: string, highlights: readonly LineRange[]) => Promise<WikiSourceResponse>;
}

export interface WikiPageProps extends WikiLoaders {
  selected: WorktreeNode | null;
}

type Load<T> = {state: 'idle'} | {state: 'loading'} | {state: 'loaded'; data: T} | {state: 'failed'; message: string};

const LIST_LIMIT = 200;
const SEARCH_DELAY_MS = 300;

const FILTERS: readonly {id: WikiListFilter; label: string; title?: string}[] = [
  {id: 'all', label: '전체'},
  {id: 'verified', label: 'verified'},
  {id: 'generated', label: 'generated'},
  {id: 'pending', label: '검수·교체 대기'},
  {
    id: 'stale',
    label: '원문 불일치',
    title: '원문 삭제: 카드가 인용한 원문 파일이 사라짐 · 원문 변경: 원문 내용이 카드가 기록한 해시와 다름',
  },
  {id: 'hidden', label: 'skipPaths 숨김'},
  {id: 'over_budget', label: '700토큰 초과'},
];

const STALE_BADGE: Record<WikiStaleReason, {tone: 'red' | 'amber'; label: string; title: string}> = {
  source_deleted: {tone: 'red', label: '원문 삭제', title: '카드가 인용한 원문 파일이 이 체크아웃에 없습니다.'},
  source_changed: {tone: 'amber', label: '원문 변경', title: '원문 내용이 카드가 기록한 해시와 다릅니다.'},
};

/** Ledger counts; `filter` names the card list filter that shows the affected cards, when one does. */
const LEDGER_LABELS: readonly {key: keyof WikiLedgerSummary; label: string; filter: WikiListFilter | null; hint: string}[] = [
  {key: 'compileJobs', label: 'Compile 대기', filter: null, hint: '아직 카드로 만들어지지 않은 compile 작업입니다. 게시되면 목록에 나타납니다.'},
  {key: 'verifyJobs', label: '검수 대기', filter: 'pending', hint: '누르면 검수·교체 대기 카드만 봅니다.'},
  {key: 'publishTxns', label: '게시 진행', filter: null, hint: '게시 중인 트랜잭션입니다. 끝나면 목록에 반영됩니다.'},
  {key: 'refreshEvents', label: '원문 변경 대기', filter: 'stale', hint: '누르면 원문과 어긋난 카드만 봅니다.'},
  {key: 'dedupPairs', label: '중복 후보', filter: null, hint: '중복 후보 쌍은 "위키 중복 정리해줘" 요청(wiki-dedup)으로 정리합니다. 목록 필터는 없습니다.'},
];

const WIDE_LAYOUT = '(min-width: 1200px)';

function failure(error: unknown): string {
  if (error instanceof Error) {
    const code = (error as Error & {code?: unknown}).code;
    return typeof code === 'string' && code !== '' ? `${code}: ${error.message}` : error.message;
  }
  return '불러오지 못했습니다.';
}

/** The `card` hash parameter of the current URL when it belongs to `targetId`. */
function cardFromHash(targetId: TargetId | null): string | null {
  const raw = window.location.hash;
  const question = raw.indexOf('?');
  if (targetId === null || question === -1 || !raw.startsWith('#wiki')) return null;
  const params = new URLSearchParams(raw.slice(question + 1));
  return params.get('target') === targetId ? params.get('card') : null;
}

function useCardParam(targetId: TargetId | null): string | null {
  const [card, setCard] = useState<string | null>(() => cardFromHash(targetId));
  useEffect(() => {
    const update = () => setCard(cardFromHash(targetId));
    update();
    window.addEventListener('hashchange', update);
    return () => window.removeEventListener('hashchange', update);
  }, [targetId]);
  return card;
}

function openCard(targetId: TargetId, rel: string | null): void {
  const next = rel === null ? `#wiki?target=${encodeURIComponent(targetId)}` : cardHash(targetId, rel);
  if (next !== null && window.location.hash !== next) window.location.hash = next;
}

// ---------------------------------------------------------------- list

function CardBadges({card}: {card: WikiCardMeta}) {
  return (
    <div class="cell-badges">
      <Badge tone={card.status === 'verified' ? 'green' : 'amber'}>{card.status}</Badge>
      {card.staleReason !== null
        ? <Badge tone={STALE_BADGE[card.staleReason].tone} title={STALE_BADGE[card.staleReason].title}>{STALE_BADGE[card.staleReason].label}</Badge>
        : null}
      {card.hidden ? <Badge tone="muted">skipPaths 숨김</Badge> : null}
      {card.overBudget ? <Badge tone="purple">700토큰 초과</Badge> : null}
    </div>
  );
}

function CardList(props: {cards: readonly WikiCardMeta[]; active: string | null; onOpen: (rel: string) => void}) {
  if (props.cards.length === 0) return <div class="empty-note">조건에 맞는 카드가 없습니다.</div>;
  return (
    <div class="table-wrap wiki-list-scroll">
      <table>
        <thead><tr><th>카드</th><th>상태</th><th>토큰(추정)</th><th>수정</th></tr></thead>
        <tbody>
          {props.cards.map(card => (
            <tr
              key={card.rel}
              class={`clickable${props.active === card.rel ? ' selected-row' : ''}`}
              tabIndex={0}
              aria-selected={props.active === card.rel}
              onClick={() => props.onOpen(card.rel)}
              onKeyDown={event => {
                if (event.key === 'Enter') props.onOpen(card.rel);
              }}
            >
              <td>
                <div class="cell-name"><strong>{safeText(card.title)}</strong></div>
                <div class="cell-sub mono" title={safeText(card.rel)}>{safeText(card.rel)}</div>
              </td>
              <td><CardBadges card={card} /></td>
              <td class="mono num">{formatCount(card.tokens)}</td>
              <td class="nowrap" title={formatClock(card.updatedAt)}>{formatRelative(card.updatedAt)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function LedgerSummary(props: {
  ledger: WikiLedgerSummary;
  worktree: boolean;
  ledgerFrom: TargetId;
  filter: WikiListFilter;
  onFilter: (filter: WikiListFilter) => void;
}) {
  return (
    <div class="wiki-ledger" aria-label="위키 원장 요약">
      {props.worktree
        ? <Badge tone="amber" title={`ledger: ${props.ledgerFrom}`}>Compile·Backfill·위키 원장 기록은 main checkout 기준</Badge>
        : null}
      <div class="cell-badges">
        {LEDGER_LABELS.map(item => {
          const count = props.ledger[item.key];
          const tone = count > 0 ? 'blue' : 'muted';
          const target = item.filter;
          const active = target !== null && props.filter === target;
          // An active filter keeps its button at zero, so it can be turned off where it was turned on.
          if (target === null || (count === 0 && !active)) {
            return <Badge key={item.key} tone={tone} title={item.hint}>{item.label} {formatCount(count)}</Badge>;
          }
          return (
            <button
              key={item.key}
              type="button"
              class={`badge badge-${tone} badge-btn${active ? ' badge-btn-active' : ''}`}
              title={item.hint}
              aria-pressed={active}
              onClick={() => props.onFilter(active ? 'all' : target)}
            >
              {item.label} {formatCount(count)}
            </button>
          );
        })}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- detail

function sourcePaths(detail: WikiCardDetail): string[] {
  const paths = new Set<string>(Object.keys(detail.frontmatter.sourceRevisions));
  for (const span of detail.frontmatter.sourceSpans) paths.add(span.path);
  return [...paths].sort();
}

function spansOf(spans: readonly SourceSpan[], path: string): LineRange[] {
  return spans.filter(span => span.path === path).map(span => ({startLine: span.startLine, endLine: span.endLine}));
}

/** The card rel a frontmatter `related` entry names, or null when it is not a known card. */
function relatedRel(item: string, resolveCard: (name: string) => string | null): string | null {
  let clean = item.trim();
  if (clean.startsWith('[[') && clean.endsWith(']]')) clean = clean.slice(2, -2).split('|')[0]?.trim() ?? '';
  if (clean.startsWith('.memvane/wiki/')) clean = clean.slice('.memvane/wiki/'.length);
  if (clean === '') return null;
  const direct = resolveCard(clean);
  if (direct !== null) return direct;
  const stem = clean.split('/').pop()?.replace(/\.md$/, '') ?? '';
  const byStem = stem === '' ? null : resolveCard(stem);
  if (byStem !== null) return byStem;
  // A `.md` path outside the loaded list (filtered or truncated) still opens through the card endpoint.
  return clean.endsWith('.md') ? clean : null;
}

function Frontmatter({detail, targetId, resolveCard}: {
  detail: WikiCardDetail;
  targetId: TargetId;
  resolveCard: (name: string) => string | null;
}) {
  const front = detail.frontmatter;
  const revisions = Object.entries(front.sourceRevisions);
  return (
    <div class="table-wrap wiki-frontmatter">
      <table>
        <tbody>
          <tr><th>status</th><td><Badge tone={front.status === 'verified' ? 'green' : 'amber'}>{front.status}</Badge></td></tr>
          <tr><th>createdBy</th><td class="mono">{front.createdBy === null ? '—' : safeText(front.createdBy)}</td></tr>
          <tr>
            <th>sourceRevisions</th>
            <td class="mono">
              {revisions.length === 0 ? '—' : revisions.map(([path, sha]) => (
                <div key={path}>{safeText(path)} <span class="muted">{safeText(sha.slice(0, 12))}</span></div>
              ))}
            </td>
          </tr>
          <tr>
            <th>sourceSpans</th>
            <td class="mono">
              {front.sourceSpans.length === 0 ? '—' : front.sourceSpans.map((span, index) => (
                <div key={index}>{safeText(span.path)}:{span.startLine}-{span.endLine}</div>
              ))}
            </td>
          </tr>
          <tr>
            <th>related</th>
            <td>
              {front.related.length === 0 ? '—' : (
                <div class="cell-badges">
                  {front.related.map(item => {
                    const rel = relatedRel(item, resolveCard);
                    const href = rel === null ? null : cardHash(targetId, rel);
                    return href === null
                      ? <Badge key={item} tone="muted" mono>{safeText(item)}</Badge>
                      : <a key={item} href={href} class="badge badge-blue badge-btn mono" title={safeText(rel)}>{safeText(item)}</a>;
                  })}
                </div>
              )}
            </td>
          </tr>
          <tr><th>aliases</th><td>{front.aliases.length === 0 ? '—' : front.aliases.map(safeText).join(', ')}</td></tr>
        </tbody>
      </table>
    </div>
  );
}

function CardDetail(props: {
  detail: WikiCardDetail;
  targetId: TargetId;
  resolveCard: (name: string) => string | null;
  openSource: string | null;
  onSource: (path: string) => void;
}) {
  const {detail, targetId} = props;
  const paths = sourcePaths(detail);
  return (
    <div class="wiki-detail">
      <Frontmatter detail={detail} targetId={targetId} resolveCard={props.resolveCard} />
      {paths.length > 0 ? (
        <div class="card-actions wiki-sources">
          {paths.map(path => (
            <button
              key={path}
              type="button"
              class="btn btn-sm"
              aria-pressed={props.openSource === path}
              onClick={() => props.onSource(path)}
            >
              원문 보기 · <span class="mono">{safeText(path)}</span>
            </button>
          ))}
        </div>
      ) : null}
      {detail.blocks.length === 0 ? <div class="empty-note">본문이 비어 있습니다.</div> : null}
      {detail.blocks.map((block, index) => (
        <section key={index} class={`wiki-block wiki-block-${block.kind}`} aria-label={block.kind === 'auto' ? 'auto 블록' : '수동 블록'}>
          <div class="wiki-block-label">
            {block.kind === 'auto'
              ? <Badge tone="blue">auto 블록 · 자동 생성</Badge>
              : <Badge tone="purple">수동 보존 블록</Badge>}
          </div>
          <SafeMarkdown markdown={block.markdown} targetId={targetId} cardRel={detail.rel} resolveCard={props.resolveCard} />
        </section>
      ))}
    </div>
  );
}

function SourceView({source, onClose}: {source: Load<WikiSourceResponse>; onClose: () => void}) {
  const firstHighlight = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    firstHighlight.current?.scrollIntoView({block: 'center'});
  }, [source]);
  if (source.state === 'idle') return null;
  const header = (
    <div class="card-header">
      <div class="card-title">원문</div>
      <button type="button" class="btn btn-sm" onClick={onClose}>닫기</button>
    </div>
  );
  if (source.state === 'loading') return <section class="card wiki-source">{header}<div class="muted">불러오는 중…</div></section>;
  if (source.state === 'failed') return <section class="card wiki-source">{header}<div class="inline-error text-red">{safeText(source.message)}</div></section>;
  const data = source.data;
  const marked = (line: number) => data.highlights.some(span => line >= span.startLine && line <= span.endLine);
  const first = data.highlights[0]?.startLine ?? null;
  return (
    <section class="card wiki-source" aria-label="원문 나란히 보기">
      {header}
      <div class="card-sub mono">{safeText(data.rel)} · {formatCount(data.lines.length)}줄{data.truncated ? ' · 256 KiB에서 잘림' : ''}</div>
      <div class="code-block wiki-source-lines">
        {data.lines.map((text, index) => {
          const line = index + 1;
          const hit = marked(line);
          return (
            <div
              key={line}
              class={`src-line${hit ? ' src-line-hl' : ''}`}
              ref={line === first ? firstHighlight : null}
            >
              <span class="src-no muted">{line}</span>
              <span class="src-text">{hit ? <mark>{safeText(text)}</mark> : safeText(text)}</span>
            </div>
          );
        })}
      </div>
    </section>
  );
}

// ---------------------------------------------------------------- page

export function WikiPage({selected, loadList, loadCard, loadSource}: WikiPageProps) {
  const targetId = selected?.targetId ?? null;
  const [filter, setFilter] = useState<WikiListFilter>('all');
  const [query, setQuery] = useState('');
  const [appliedQuery, setAppliedQuery] = useState('');
  const [list, setList] = useState<Load<WikiListResponse>>({state: 'idle'});
  const [detail, setDetail] = useState<Load<WikiCardDetail>>({state: 'idle'});
  const [sourcePath, setSourcePath] = useState<string | null>(null);
  const [source, setSource] = useState<Load<WikiSourceResponse>>({state: 'idle'});
  const activeCard = useCardParam(targetId);
  const detailRef = useRef<HTMLDivElement | null>(null);
  const listRequest = useRef(0);
  const cardRequest = useRef(0);
  const sourceRequest = useRef(0);

  useEffect(() => {
    const timer = window.setTimeout(() => setAppliedQuery(query.trim()), SEARCH_DELAY_MS);
    return () => window.clearTimeout(timer);
  }, [query]);

  useEffect(() => {
    setSourcePath(null);
    setSource({state: 'idle'});
  }, [targetId, activeCard]);

  // Narrow screens stack the detail under a list of up to 200 rows; bring it into view on open.
  // Wide screens show it beside the list, so only its top needs to be visible: a detail whose top
  // was scrolled above the scroll area (a long card read to its end) is brought back to its top,
  // since 'nearest' leaves an element taller than the viewport where it is.
  useEffect(() => {
    const element = detailRef.current;
    if (activeCard === null || element === null) return;
    const wide = typeof window.matchMedia === 'function' && window.matchMedia(WIDE_LAYOUT).matches;
    const scroller = element.closest('.main-scroll');
    const areaTop = scroller === null ? 0 : scroller.getBoundingClientRect().top;
    const topHidden = element.getBoundingClientRect().top < areaTop;
    element.scrollIntoView({block: wide && !topHidden ? 'nearest' : 'start'});
  }, [activeCard]);

  useEffect(() => {
    if (targetId === null) {
      setList({state: 'idle'});
      return;
    }
    const request = ++listRequest.current;
    setList({state: 'loading'});
    loadList(targetId, {status: filter, q: appliedQuery, limit: LIST_LIMIT}).then(
      data => {
        if (request === listRequest.current) setList({state: 'loaded', data});
      },
      error => {
        if (request === listRequest.current) setList({state: 'failed', message: failure(error)});
      },
    );
  }, [targetId, filter, appliedQuery, loadList]);

  useEffect(() => {
    if (targetId === null || activeCard === null) {
      setDetail({state: 'idle'});
      return;
    }
    const request = ++cardRequest.current;
    setDetail({state: 'loading'});
    loadCard(targetId, activeCard).then(
      data => {
        if (request === cardRequest.current) setDetail({state: 'loaded', data});
      },
      error => {
        if (request === cardRequest.current) setDetail({state: 'failed', message: failure(error)});
      },
    );
  }, [targetId, activeCard, loadCard]);

  const cards = list.state === 'loaded' ? list.data.cards : [];
  const resolveCard = useMemo(() => {
    const byStem = new Map<string, string>();
    for (const card of cards) {
      const stem = card.rel.split('/').pop()?.replace(/\.md$/, '') ?? '';
      byStem.set(stem, byStem.has(stem) ? '' : card.rel);
    }
    return (name: string): string | null => {
      if (name.endsWith('.md') && cards.some(card => card.rel === name)) return name;
      const rel = byStem.get(name);
      return rel === undefined || rel === '' ? null : rel;
    };
  }, [cards]);

  const showSource = (path: string) => {
    if (targetId === null || detail.state !== 'loaded') return;
    if (sourcePath === path) {
      setSourcePath(null);
      setSource({state: 'idle'});
      return;
    }
    const request = ++sourceRequest.current;
    setSourcePath(path);
    setSource({state: 'loading'});
    loadSource(targetId, path, spansOf(detail.data.frontmatter.sourceSpans, path)).then(
      data => {
        if (request === sourceRequest.current) setSource({state: 'loaded', data});
      },
      error => {
        if (request === sourceRequest.current) setSource({state: 'failed', message: failure(error)});
      },
    );
  };

  if (selected === null || targetId === null) {
    return (
      <section class="card" aria-label="Wiki 카드 브라우저">
        <div class="card-title">Wiki 카드 브라우저</div>
        <div class="empty-note">왼쪽에서 프로젝트 또는 워크트리를 선택하면 그 체크아웃의 위키 카드를 보여 줍니다.</div>
      </section>
    );
  }

  const worktree = selected.kind === 'worktree';
  const open = activeCard !== null;
  return (
    <div class={`wiki-layout${open ? ' wiki-layout-open' : ''}`}>
      <section class="card wiki-list-card" aria-label="Wiki 카드 목록">
        <div class="card-header">
          <div>
            <div class="card-title">Wiki 카드 · {safeText(selected.name)}</div>
            <div class="card-sub">카드와 원문은 선택한 체크아웃에서 읽습니다.</div>
          </div>
          <div class="card-actions">
            <Badge tone={worktree ? 'purple' : 'blue'}>{worktree ? 'linked worktree' : 'main'}</Badge>
          </div>
        </div>
        {list.state === 'loaded'
          ? (
            <LedgerSummary
              ledger={list.data.ledger}
              worktree={worktree}
              ledgerFrom={list.data.ledgerFrom}
              filter={filter}
              onFilter={setFilter}
            />
          )
          : worktree ? <Badge tone="amber">Compile·Backfill·위키 원장 기록은 main checkout 기준</Badge> : null}
        <div class="wiki-toolbar">
          <div class="card-actions" role="group" aria-label="카드 필터">
            {FILTERS.map(item => (
              <button
                key={item.id}
                type="button"
                class={`btn btn-sm${filter === item.id ? ' active' : ''}`}
                aria-pressed={filter === item.id}
                {...(item.title !== undefined ? {title: item.title} : {})}
                onClick={() => setFilter(item.id)}
              >
                {item.label}
              </button>
            ))}
          </div>
          <input
            type="search"
            class="sidebar-search wiki-search"
            placeholder="제목·경로·본문 검색"
            aria-label="카드 검색"
            maxLength={256}
            value={query}
            onInput={event => setQuery((event.currentTarget as HTMLInputElement).value)}
          />
        </div>
        {list.state === 'loading' || list.state === 'idle' ? <div class="muted">불러오는 중…</div> : null}
        {list.state === 'failed' ? <div class="inline-error text-red">{safeText(list.message)}</div> : null}
        {list.state === 'loaded' ? (
          <>
            <CardList cards={list.data.cards} active={activeCard} onOpen={rel => openCard(targetId, rel)} />
            {list.data.truncated
              ? <div class="card-sub">목록이 {formatCount(LIST_LIMIT)}개에서 잘렸습니다. 필터나 검색으로 좁혀 보세요.</div>
              : null}
          </>
        ) : null}
      </section>
      {activeCard !== null ? (
        <div class={`wiki-split${sourcePath !== null ? ' wiki-split-open' : ''}`} ref={detailRef}>
          <section class="card" aria-label="카드 상세">
            <div class="card-header">
              <div>
                <div class="card-title mono">{safeText(activeCard)}</div>
              </div>
              <button type="button" class="btn btn-sm" onClick={() => openCard(targetId, null)}>닫기</button>
            </div>
            {detail.state === 'loading' || detail.state === 'idle' ? <div class="muted">불러오는 중…</div> : null}
            {detail.state === 'failed' ? <div class="inline-error text-red">{safeText(detail.message)}</div> : null}
            {detail.state === 'loaded' ? (
              <CardDetail
                detail={detail.data}
                targetId={targetId}
                resolveCard={resolveCard}
                openSource={sourcePath}
                onSource={showSource}
              />
            ) : null}
          </section>
          <SourceView
            source={source}
            onClose={() => {
              setSourcePath(null);
              setSource({state: 'idle'});
            }}
          />
        </div>
      ) : null}
    </div>
  );
}
