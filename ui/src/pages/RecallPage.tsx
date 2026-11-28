import {useEffect, useRef, useState} from 'preact/hooks';
import {ApiError} from '../api/client';
import type {
  DryRunRecallResponse,
  HostName,
  InstallId,
  RecallEventItem,
  RecallOutcome,
  RecallStageTimings,
  RecallsResponse,
  TargetId,
  WorktreeNode,
} from '../api/types';
import {Badge, type Tone} from '../components/Badge';
import {formatClock, formatCount, formatMs, formatRelative, safeText} from '../text';

// ---------------------------------------------------------------- props

/** One timeline request: `GET /api/installs/<installId>/recalls?hours=&outcome=&host=&target=`. */
export interface RecallQuery {
  installId: InstallId;
  hours: number;
  outcome: RecallOutcome | null;
  host: HostName | null;
  target: TargetId | null;
}

/** Body of `POST /api/targets/<targetId>/query-playground`. */
export interface PlaygroundRequest {
  prompt: string;
  useSemantic: boolean;
  useSelector: boolean;
  timeoutMs?: number;
}

export interface RecallPageProps {
  selected: WorktreeNode | null;
  loadRecalls: (query: RecallQuery) => Promise<RecallsResponse>;
  runPlayground: (targetId: TargetId, body: PlaygroundRequest) => Promise<DryRunRecallResponse>;
  /** Opens the card in the Wiki tab. */
  onOpenCard: (path: string) => void;
  /** Changes when new events arrived (SSE `events_updated`); the timeline reloads. */
  refreshToken?: number;
}

// ---------------------------------------------------------------- shared bits

const MAX_PROMPT_CHARS = 32768;
const HOURS = [1, 6, 24, 72, 168] as const;
const HOSTS: readonly HostName[] = ['claude', 'codex', 'hermes', 'gemini'];
const OUTCOMES: readonly RecallOutcome[] = ['served', 'empty', 'refused'];
const OUTCOME_TONE: Record<RecallOutcome, Tone> = {served: 'green', empty: 'muted', refused: 'red'};

function semanticTone(status: string | null): Tone {
  switch (status) {
    case 'complete':
      return 'green';
    case 'partial':
    case 'lexical_only':
      return 'amber';
    case 'provider_absent':
    case null:
      return 'muted';
    default:
      return 'purple';
  }
}

function errorText(error: unknown): string {
  if (error instanceof ApiError) return `${error.code} (HTTP ${error.status})`;
  return error instanceof Error ? error.message : String(error);
}

type StyleMap = Record<string, string>;

/** Applies styles through the CSSOM (`style-src 'self'` allows no style attribute). */
function styled(styles: StyleMap) {
  return (el: HTMLElement | null) => {
    if (el === null) return;
    for (const [name, value] of Object.entries(styles)) el.style.setProperty(name, value);
  };
}

const ROW: StyleMap = {display: 'flex', 'flex-wrap': 'wrap', gap: '8px', 'align-items': 'center'};
const STACK: StyleMap = {display: 'flex', 'flex-direction': 'column', gap: '10px'};

function OutcomeBadge({outcome}: {outcome: string}) {
  const tone = (OUTCOMES as readonly string[]).includes(outcome) ? OUTCOME_TONE[outcome as RecallOutcome] : 'purple';
  return <Badge tone={tone}>{safeText(outcome) || '—'}</Badge>;
}

function SemanticBadge({status, covered, eligible}: {status: string | null; covered: number | null; eligible: number | null}) {
  const coverage = covered !== null && eligible !== null ? ` ${formatCount(covered)}/${formatCount(eligible)}` : '';
  return (
    <Badge tone={semanticTone(status)} mono title="semantic · vectorCoveredMembers/vectorEligibleMembers">
      {safeText(status ?? 'semantic —')}{coverage}
    </Badge>
  );
}

function CardLink({path, onOpenCard}: {path: string; onOpenCard: (path: string) => void}) {
  return (
    <button type="button" class="btn btn-sm mono" title={safeText(path)} onClick={() => onOpenCard(path)}>
      {safeText(path)}
    </button>
  );
}

// ---------------------------------------------------------------- latency bar

const STAGES: readonly {key: keyof RecallStageTimings; label: string; color: string; seconds?: true}[] = [
  {key: 'pinMs', label: 'pin', color: 'var(--accent)'},
  {key: 'lexicalMs', label: 'lexical', color: 'var(--green)'},
  {key: 'stillCurrentMs', label: 'stillCurrent', color: 'var(--text-muted)'},
  {key: 'checkoutMs', label: 'checkout', color: 'var(--amber)'},
  {key: 'queryEmbeddingMs', label: 'queryEmbedding', color: 'var(--purple)'},
  {key: 'semanticMs', label: 'semantic', color: 'var(--red)'},
  {key: 'selectorResidentSeconds', label: 'selectorResident', color: 'var(--border-strong)', seconds: true},
];

function LatencyBar({timings, totalMs}: {timings: RecallStageTimings; totalMs: number | null}) {
  const parts: {id: string; label: string; color: string; ms: number}[] = STAGES.flatMap(stage => {
    const raw = timings[stage.key];
    if (raw === null || raw === undefined || !Number.isFinite(raw) || raw <= 0) return [];
    return [{id: stage.key, label: stage.label, color: stage.color, ms: stage.seconds === true ? raw * 1000 : raw}];
  });
  const staged = parts.reduce((sum, part) => sum + part.ms, 0);
  const scale = Math.max(staged, totalMs ?? 0);
  if (parts.length === 0 || scale <= 0) return <span class="muted">단계 시간 없음</span>;
  // Time the stages do not account for (hook start-up, I/O, rendering) as its own segment.
  const rest = totalMs === null ? 0 : totalMs - staged;
  if (rest >= 1) parts.push({id: 'overhead', label: '기타/오버헤드', color: 'var(--text-secondary)', ms: rest});
  const summary = parts.map(part => `${part.label} ${formatMs(part.ms)}`).join(' · ');
  return (
    <div ref={styled({display: 'flex', 'flex-direction': 'column', gap: '3px', 'min-width': '180px'})}>
      <div
        role="img"
        aria-label={summary}
        title={summary}
        ref={styled({display: 'flex', height: '8px', width: '100%', 'border-radius': '4px', overflow: 'hidden',
          background: 'var(--bg-surface-2)', border: '1px solid var(--border)'})}
      >
        {parts.map(part => (
          <span key={part.id} title={`${part.label} ${formatMs(part.ms)}`}
            ref={styled({width: `${((part.ms / scale) * 100).toFixed(2)}%`, background: part.color, height: '100%'})} />
        ))}
      </div>
      <div class="mono muted" ref={styled({'font-size': '10.5px'})}>{summary}</div>
    </div>
  );
}

// ---------------------------------------------------------------- timeline

function TimelineRow({item, onOpenCard}: {item: RecallEventItem; onOpenCard: (path: string) => void}) {
  return (
    <tr>
      <td class="nowrap">
        <div title={formatClock(item.ts)}>{formatRelative(item.ts)}</div>
        <div class="cell-sub mono">{formatClock(item.ts)}</div>
      </td>
      <td>
        <div class="cell-badges">
          <Badge tone="muted">{item.host}</Badge>
          {item.event === 'recall.edit' ? <Badge tone="purple" mono>edit</Badge> : null}
        </div>
      </td>
      <td>
        <div class="cell-badges">
          <OutcomeBadge outcome={item.outcome} />
          <span class="mono">{safeText(item.reason)}</span>
        </div>
        {item.notes.length > 0
          ? <div class="cell-badges" ref={styled({'margin-top': '4px'})}>
            {item.notes.map(note => <Badge key={note} tone="muted" mono>{safeText(note)}</Badge>)}
          </div>
          : null}
      </td>
      <td>
        <div class="cell-badges">
          <SemanticBadge status={item.semantic} covered={item.vectorCoveredMembers} eligible={item.vectorEligibleMembers} />
          {item.degraded.map(kind => <Badge key={kind} tone="amber" mono>degraded: {kind}</Badge>)}
        </div>
      </td>
      <td>
        <LatencyBar timings={item.timings} totalMs={item.totalMs} />
        <div class="cell-sub mono">total {formatMs(item.totalMs)}</div>
      </td>
      <td class="mono">
        <div title={safeText(item.prompt.sha256)}>{safeText(item.prompt.sha256.slice(0, 12)) || '—'}</div>
        <div class="cell-sub">{formatCount(item.prompt.chars)}자</div>
      </td>
      <td>
        {item.cards.length === 0
          ? <span class="muted">—</span>
          : <div class="cell-badges">{item.cards.map(card => <CardLink key={card.path} path={card.path} onOpenCard={onOpenCard} />)}</div>}
      </td>
    </tr>
  );
}

function Timeline({selected, loadRecalls, onOpenCard, refreshToken}: {
  selected: WorktreeNode;
  loadRecalls: RecallPageProps['loadRecalls'];
  onOpenCard: (path: string) => void;
  refreshToken: number;
}) {
  const [hours, setHours] = useState<number>(24);
  const [outcome, setOutcome] = useState<RecallOutcome | null>(null);
  const [host, setHost] = useState<HostName | null>(null);
  const [reload, setReload] = useState(0);
  const [items, setItems] = useState<RecallEventItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const latest = useRef(0);
  // The latest callback, so a parent re-render with a new closure does not refetch.
  const load = useRef(loadRecalls);
  load.current = loadRecalls;

  useEffect(() => {
    const ticket = ++latest.current;
    setLoading(true);
    setError(null);
    load.current({installId: selected.installId, hours, outcome, host, target: selected.targetId})
      .then(response => {
        if (ticket !== latest.current) return;
        setItems(response.items);
      })
      .catch((err: unknown) => {
        if (ticket !== latest.current) return;
        setError(errorText(err));
      })
      .finally(() => {
        if (ticket === latest.current) setLoading(false);
      });
  }, [selected.installId, selected.targetId, hours, outcome, host, reload, refreshToken]);

  return (
    <section class="card" aria-label="최근 Recall 타임라인">
      <div class="card-header">
        <div>
          <div class="card-title">최근 Recall 타임라인</div>
          <p class="card-sub">
            {safeText(selected.name)} · {selected.kind === 'main' ? 'main checkout' : 'linked worktree'} 기준. 프롬프트는 sha256·글자 수만 표시합니다.
          </p>
        </div>
        <div class="card-actions">
          <select aria-label="기간" value={String(hours)} onChange={event => setHours(Number(event.currentTarget.value))}>
            {HOURS.map(value => <option key={value} value={String(value)}>{value < 24 ? `${value}시간` : `${value / 24}일`}</option>)}
          </select>
          <select aria-label="outcome" value={outcome ?? ''}
            onChange={event => setOutcome(event.currentTarget.value === '' ? null : event.currentTarget.value as RecallOutcome)}>
            <option value="">모든 outcome</option>
            {OUTCOMES.map(value => <option key={value} value={value}>{value}</option>)}
          </select>
          <select aria-label="호스트" value={host ?? ''}
            onChange={event => setHost(event.currentTarget.value === '' ? null : event.currentTarget.value as HostName)}>
            <option value="">모든 호스트</option>
            {HOSTS.map(value => <option key={value} value={value}>{value}</option>)}
          </select>
          <button type="button" class="btn btn-sm" disabled={loading} onClick={() => setReload(value => value + 1)}>
            {loading ? '불러오는 중…' : '새로고침'}
          </button>
        </div>
      </div>
      {error !== null ? <div class="inline-error mono">타임라인을 불러오지 못했습니다: {safeText(error)}</div> : null}
      {items === null
        ? (error === null ? <div class="empty-note">불러오는 중…</div> : null)
        : items.length === 0
          ? <div class="empty-note">이 조건의 recall 이벤트가 없습니다.</div>
          : (
            <div class="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>시각</th>
                    <th>호스트</th>
                    <th>outcome / reason / notes</th>
                    <th>semantic · degraded</th>
                    <th>단계별 지연</th>
                    <th>프롬프트</th>
                    <th>주입 카드</th>
                  </tr>
                </thead>
                <tbody>
                  {items.map((item, index) => <TimelineRow key={`${item.ts}-${item.session}-${index}`} item={item} onOpenCard={onOpenCard} />)}
                </tbody>
              </table>
            </div>
          )}
    </section>
  );
}

// ---------------------------------------------------------------- playground

interface StageCell {
  rank: number | null;
  value: number | null;
}

interface CompareRow {
  path: string;
  lexical: (StageCell | null)[];
  semantic: StageCell | null;
  fused: StageCell | null;
  picked: boolean;
  decision: string | null;
}

function compareRows(result: DryRunRecallResponse): CompareRow[] {
  const rows = new Map<string, CompareRow>();
  const lanes = result.lexicalLists.length;
  const row = (path: string): CompareRow => {
    let found = rows.get(path);
    if (found === undefined) {
      found = {path, lexical: Array.from({length: lanes}, () => null), semantic: null, fused: null, picked: false, decision: null};
      rows.set(path, found);
    }
    return found;
  };
  result.fusedList.forEach(item => { row(item.path).fused = {rank: item.rank, value: item.rrfScore}; });
  result.lexicalLists.forEach((lane, index) => lane.rows.forEach(item => {
    row(item.path).lexical[index] = {rank: item.rank, value: item.score};
  }));
  (result.semanticList ?? []).forEach(item => { row(item.path).semantic = {rank: item.rank, value: item.cosine}; });
  result.selectorResult.decisions.forEach(item => {
    const target = row(item.path);
    target.picked = item.picked;
    target.decision = item.reason;
  });
  const best = (entry: CompareRow) => Math.min(
    entry.fused?.rank ?? Infinity,
    entry.semantic?.rank ?? Infinity,
    ...entry.lexical.map(cell => cell?.rank ?? Infinity),
  );
  return [...rows.values()].sort((a, b) =>
    (a.fused?.rank ?? Infinity) - (b.fused?.rank ?? Infinity) || best(a) - best(b) || a.path.localeCompare(b.path));
}

function Cell({cell, digits}: {cell: StageCell | null; digits: number}) {
  if (cell === null) return <td class="muted">—</td>;
  return (
    <td class="mono nowrap">
      #{cell.rank ?? '?'} <span class="muted">{cell.value === null ? '—' : cell.value.toFixed(digits)}</span>
    </td>
  );
}

function StageTable({result, onOpenCard}: {result: DryRunRecallResponse; onOpenCard: (path: string) => void}) {
  const rows = compareRows(result);
  if (rows.length === 0) return <div class="empty-note">후보 카드가 없습니다.</div>;
  const lanes = result.lexicalLists;
  return (
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>카드</th>
            {lanes.length === 0
              ? <th>Lexical FTS5</th>
              : lanes.map((lane, index) => (
                <th key={index} title={safeText(lane.query)}>Lexical FTS5 {lanes.length > 1 ? `#${index + 1}` : ''} · bm25</th>
              ))}
            <th>Semantic · 코사인</th>
            <th>Hybrid RRF</th>
            <th>Selector</th>
          </tr>
        </thead>
        <tbody>
          {rows.map(row => (
            <tr key={row.path} class={row.picked ? 'selected-row' : ''}>
              <td><CardLink path={row.path} onOpenCard={onOpenCard} /></td>
              {lanes.length === 0 ? <td class="muted">—</td> : row.lexical.map((cell, index) => <Cell key={index} cell={cell} digits={3} />)}
              <Cell cell={row.semantic} digits={4} />
              <Cell cell={row.fused} digits={5} />
              <td>
                {row.decision === null
                  ? <span class="muted">—</span>
                  : <Badge tone={row.picked ? 'green' : 'muted'} mono>{row.picked ? '최종 · ' : ''}{safeText(row.decision)}</Badge>}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

const GENERATION_CHARS = 14;

function GenerationBadge({generation}: {generation: string}) {
  const short = generation.length > GENERATION_CHARS ? `${generation.slice(0, GENERATION_CHARS)}…` : generation;
  return <Badge tone="muted" mono title={`generation ${safeText(generation)}`}>gen {safeText(short)}</Badge>;
}

const RUN_SHORTCUT = typeof navigator !== 'undefined' && /Mac|iPhone|iPad/.test(navigator.platform) ? '⌘ + Enter' : 'Ctrl + Enter';

function PlaygroundResult({result, onOpenCard}: {result: DryRunRecallResponse; onOpenCard: (path: string) => void}) {
  const selector = result.selectorResult;
  const total = result.timings['totalMs'];
  return (
    <div ref={styled(STACK)}>
      <div class="cell-badges">
        <Badge tone="blue" mono>route: {safeText(result.route) || '—'}</Badge>
        {result.linked ? <Badge tone="purple">linked view</Badge> : null}
        <OutcomeBadge outcome={result.outcome} />
        <Badge tone="muted" mono>reason: {safeText(result.reason) || '—'}</Badge>
        <SemanticBadge status={result.semanticStatus} covered={result.vectorCoveredMembers} eligible={result.vectorEligibleMembers} />
        {result.fallbackReason !== null ? <Badge tone="amber" mono>fallback: {safeText(result.fallbackReason)}</Badge> : null}
        {result.generation !== null ? <GenerationBadge generation={result.generation} /> : null}
        {total !== undefined ? <Badge tone="muted" mono>{formatMs(total)}</Badge> : null}
      </div>
      {result.notes.length > 0
        ? <div class="cell-badges">{result.notes.map(note => <Badge key={note} tone="muted" mono>{safeText(note)}</Badge>)}</div>
        : null}
      {result.lexicalLists.length > 0
        ? (
          <div class="card-sub mono">
            {result.lexicalLists.map((lane, index) => (
              <div key={index}>FTS #{index + 1}: {safeText(lane.query) || '—'} ({formatCount(lane.rows.length)}건)</div>
            ))}
          </div>
        )
        : null}
      <div ref={styled(ROW)}>
        <strong>Selector</strong>
        <Badge tone={selector.ran ? 'green' : 'muted'}>{selector.ran ? '실행됨' : '실행 안 함'}</Badge>
        <span class="mono">{safeText(selector.reason) || '—'}</span>
        {selector.checkpoint !== null ? <Badge tone="muted" mono>checkpoint {safeText(selector.checkpoint)}</Badge> : null}
        <span class="muted">최종 {formatCount(selector.final.length)}장</span>
      </div>
      <StageTable result={result} onOpenCard={onOpenCard} />
    </div>
  );
}

function Playground({selected, runPlayground, onOpenCard}: {
  selected: WorktreeNode;
  runPlayground: RecallPageProps['runPlayground'];
  onOpenCard: (path: string) => void;
}) {
  const [prompt, setPrompt] = useState('');
  const [useSemantic, setUseSemantic] = useState(true);
  const [useSelector, setUseSelector] = useState(false);
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState<DryRunRecallResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const latest = useRef(0);

  useEffect(() => {
    latest.current += 1;
    setResult(null);
    setError(null);
    setRunning(false);
  }, [selected.targetId]);

  const trimmed = prompt.trim();
  const tooLong = prompt.length > MAX_PROMPT_CHARS;
  const run = () => {
    if (trimmed === '' || tooLong || running) return;
    const ticket = ++latest.current;
    setRunning(true);
    setError(null);
    runPlayground(selected.targetId, {prompt, useSemantic, useSelector})
      .then(response => {
        if (ticket === latest.current) setResult(response);
      })
      .catch((err: unknown) => {
        if (ticket === latest.current) setError(errorText(err));
      })
      .finally(() => {
        if (ticket === latest.current) setRunning(false);
      });
  };

  return (
    <section class="card" aria-label="Query Playground">
      <div class="card-header">
        <div>
          <div class="card-title">Query Playground</div>
          <p class="card-sub">부작용 없는 dry run: 이벤트 기록·wake·provider/selector 기동 없이 4단계 결과를 비교합니다.</p>
        </div>
      </div>
      <div ref={styled(STACK)}>
        <textarea
          aria-label="질의 프롬프트"
          rows={3}
          maxLength={MAX_PROMPT_CHARS}
          placeholder="recall에 보낼 프롬프트"
          value={prompt}
          onInput={event => setPrompt(event.currentTarget.value)}
          onKeyDown={event => {
            if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) run();
          }}
          ref={styled({width: '100%', 'box-sizing': 'border-box', resize: 'vertical', padding: '8px 10px',
            background: 'var(--bg-input)', border: '1px solid var(--border)', 'border-radius': 'var(--radius-sm)',
            'font-family': 'var(--font-mono)', 'font-size': '12px', color: 'var(--text-primary)'})}
        />
        <div ref={styled(ROW)}>
          <label ref={styled(ROW)} title="useSemantic: 임베딩 코사인 단계를 함께 실행합니다">
            <input type="checkbox" checked={useSemantic} onChange={event => setUseSemantic(event.currentTarget.checked)} />
            Semantic 검색
          </label>
          <label ref={styled(ROW)} title="useSelector: 학습된 Selector로 최종 카드를 고릅니다">
            <input type="checkbox" checked={useSelector} onChange={event => setUseSelector(event.currentTarget.checked)} />
            Selector 적용
          </label>
          <span class="muted mono">{formatCount(prompt.length)} / {formatCount(MAX_PROMPT_CHARS)}</span>
          <button type="button" class="btn" disabled={trimmed === '' || tooLong || running} onClick={run} title={RUN_SHORTCUT}>
            {running ? '실행 중…' : '실행'}
          </button>
          <span class="muted kbd-hint" aria-hidden="true"><kbd>{RUN_SHORTCUT}</kbd>로 실행</span>
        </div>
        {error !== null ? <div class="inline-error mono">dry run 실패: {safeText(error)}</div> : null}
        {result !== null ? <PlaygroundResult result={result} onOpenCard={onOpenCard} /> : null}
      </div>
    </section>
  );
}

// ---------------------------------------------------------------- page

export function RecallPage({selected, loadRecalls, runPlayground, onOpenCard, refreshToken}: RecallPageProps) {
  if (selected === null) {
    return (
      <section class="card placeholder" aria-label="Recall 인스펙터">
        <div class="card-title">Recall 인스펙터 & Query Playground</div>
        <p class="card-sub">왼쪽에서 프로젝트나 워크트리를 선택하면 그 체크아웃의 recall 타임라인과 Playground가 열립니다.</p>
      </section>
    );
  }
  return (
    <>
      <Timeline selected={selected} loadRecalls={loadRecalls} onOpenCard={onOpenCard} refreshToken={refreshToken ?? 0} />
      <Playground selected={selected} runPlayground={runPlayground} onOpenCard={onOpenCard} />
    </>
  );
}
