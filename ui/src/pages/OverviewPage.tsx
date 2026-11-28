import {useEffect, useMemo, useState} from 'preact/hooks';
import {ApiError, getInstallErrors} from '../api/client';
import type {
  ErrorEventItem,
  InstallId,
  OverviewResponse,
  SloVerdict,
  TabId,
  WorktreeNode,
} from '../api/types';
import {
  CHECKOUT_STATE,
  DORMANT_TOGGLE_TITLE,
  branchText,
  dormantToggleLabel,
  dotTone,
  isDormant,
  isServable,
  shownNodes,
  stateTitle,
  unservableTitle,
} from '../checkoutState';
import {useShowDormant} from '../dormantPref';
import {Badge, Dot, type Tone} from '../components/Badge';
import {navigate, routeHash} from '../router';
import {nodesInScope} from '../scope';
import {formatClock, formatCount, formatMs, formatRelative, safeMultiline, safeText} from '../text';

interface Props {
  overview: OverviewResponse;
  selected: WorktreeNode | null;
  tab: TabId;
  reconciling: boolean;
  onReconcile: () => void;
}

const SLO_TONE: Record<SloVerdict, Tone> = {ok: 'green', over: 'red', 'no-data': 'muted'};

function SloBadge({slo}: {slo: SloVerdict}) {
  return <Badge tone={SLO_TONE[slo]}>{slo}</Badge>;
}

function worstSlo(values: readonly SloVerdict[]): SloVerdict {
  if (values.includes('over')) return 'over';
  if (values.includes('ok')) return 'ok';
  return 'no-data';
}

function latest(values: readonly (string | null)[]): string | null {
  let best: string | null = null;
  let bestAt = -Infinity;
  for (const value of values) {
    if (value === null) continue;
    const at = Date.parse(value);
    if (!Number.isNaN(at) && at > bestAt) {
      best = value;
      bestAt = at;
    }
  }
  return best;
}

function StateBadge({node}: {node: WorktreeNode}) {
  const info = CHECKOUT_STATE[node.viewState];
  if (node.viewState === 'ready') {
    return <Badge tone={node.kind === 'main' ? 'blue' : 'green'} title={stateTitle(node)}>{node.kind === 'main' ? 'main' : 'linked'}</Badge>;
  }
  return <Badge tone={info.tone} title={stateTitle(node)}>{info.label}</Badge>;
}

function Kpis({scoped, overview}: {scoped: readonly WorktreeNode[]; overview: OverviewResponse}) {
  // Counts are the active checkouts whatever the toggle shows; hidden ones are counted apart.
  const nodes = scoped.filter(node => !isDormant(node));
  const dormant = scoped.length - nodes.length;
  const mains = nodes.filter(node => node.kind === 'main');
  const worktrees = nodes.length - mains.length;
  // Main checkouts own the install's published cards; a worktree-only scope counts its own view.
  const cardNodes = mains.length > 0 ? mains : nodes;
  const cards = cardNodes.reduce(
    (sum, node) => ({
      verified: sum.verified + node.cards.verified,
      generated: sum.generated + node.cards.generated,
      pending: sum.pending + node.cards.pending,
    }),
    {verified: 0, generated: 0, pending: 0},
  );
  const installIds = new Set(nodes.map(node => node.installId));
  const installs = overview.installs.filter(install => installIds.has(install.installId));
  const single = installs.length === 1 ? installs[0] : undefined;
  // Recall counts belong to the install; every checkout row repeats its install's number.
  const recalls = installs.reduce((sum, install) => sum + install.recalls24h, 0);
  const slo = worstSlo(installs.length > 0 ? installs.map(install => install.slo) : nodes.map(node => node.slo));
  const lastSync = latest(nodes.map(node => node.lastSyncAt));
  return (
    <section class="kpi-grid" aria-label="핵심 지표">
      <div class="kpi-box">
        <div class="kpi-label"><span>선택 범위 체크아웃</span><Badge tone="muted" mono>{overview.snapshot.state}</Badge></div>
        <div class="kpi-value num">{formatCount(nodes.length)}</div>
        <div class="kpi-foot">
          <span>프로젝트 {formatCount(mains.length)}</span>
          <span>워크트리 {formatCount(worktrees)}</span>
          {dormant > 0 ? <span title={DORMANT_TOGGLE_TITLE}>해제·없음 {formatCount(dormant)}</span> : null}
        </div>
      </div>
      <div class="kpi-box">
        <div class="kpi-label"><span>24h Recall</span><SloBadge slo={slo} /></div>
        <div class="kpi-value num">{formatCount(recalls)}<span class="kpi-unit"> 건</span></div>
        <div class="kpi-foot mono">
          {single !== undefined
            ? <><span>p50 {formatMs(single.p50Ms)}</span><span>p95 {formatMs(single.p95Ms)}</span></>
            : <span>p50/p95는 프로젝트를 선택하면 표시</span>}
        </div>
      </div>
      <div class="kpi-box">
        <div class="kpi-label"><span>위키 카드</span><Badge tone="blue">verified</Badge></div>
        <div class="kpi-value num">{formatCount(cards.verified)}</div>
        <div class="kpi-foot"><span>generated {formatCount(cards.generated)}</span><span>pending {formatCount(cards.pending)}</span></div>
      </div>
      <div class="kpi-box">
        <div class="kpi-label"><span>마지막 wake.sync</span></div>
        <div class="kpi-value">{formatRelative(lastSync)}</div>
        <div class="kpi-foot mono"><span>{formatClock(lastSync)}</span></div>
      </div>
    </section>
  );
}

function CheckoutTable({nodes, selected, tab}: {nodes: readonly WorktreeNode[]; selected: WorktreeNode | null; tab: TabId}) {
  return (
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>프로젝트 / 워크트리</th>
            <th>호스트</th>
            <th>상태</th>
            <th>위키 카드 (verified / generated / pending)</th>
            <th>마지막 wake.sync</th>
            <th>24h Recall · p50 / p95</th>
            <th>SLO</th>
            <th><span class="sr-only">바로 가기</span></th>
          </tr>
        </thead>
        <tbody>
          {nodes.map(node => {
            // A checkout the server cannot prove (404/409) opens neither a scope nor a shortcut.
            const servable = isServable(node);
            const blocked = servable ? undefined : unservableTitle(node);
            const rowClass = [servable ? 'clickable' : 'disabled-row', node.kind === 'worktree' ? 'wt-row' : '',
              isDormant(node) ? 'dormant-row' : '', selected?.targetId === node.targetId ? 'selected-row' : '']
              .filter(Boolean).join(' ');
            return (
            <tr
              key={node.targetId}
              class={rowClass}
              tabIndex={0}
              aria-disabled={servable ? undefined : true}
              {...(blocked !== undefined ? {title: blocked} : {})}
              onClick={() => {
                if (servable) navigate({tab, target: node.targetId});
              }}
              onKeyDown={event => {
                if (servable && event.key === 'Enter') navigate({tab, target: node.targetId});
              }}
            >
              <td>
                <div class="cell-name">
                  <Dot tone={dotTone(node)} />
                  <strong>{safeText(node.name)}</strong>
                  <Badge tone="muted" mono={!isDormant(node)}>{safeText(branchText(node))}</Badge>
                </div>
                <div class="cell-sub mono" title={safeText(node.displayRoot)}>{safeText(node.displayRoot)}</div>
              </td>
              <td>
                <div class="cell-badges">
                  {node.hosts.length === 0 ? '—' : node.hosts.map(host => <Badge key={host} tone="muted">{host}</Badge>)}
                </div>
              </td>
              <td><StateBadge node={node} /></td>
              <td class="mono num">
                {formatCount(node.cards.verified)}
                <span class="muted"> / {formatCount(node.cards.generated)} / {formatCount(node.cards.pending)}</span>
              </td>
              <td title={formatClock(node.lastSyncAt)}>{formatRelative(node.lastSyncAt)}</td>
              {node.kind === 'main'
                ? (
                  <>
                    <td class="mono num nowrap">
                      {formatCount(node.recalls24h)}건 · {formatMs(node.p50Ms)} / {formatMs(node.p95Ms)}
                    </td>
                    <td><SloBadge slo={node.slo} /></td>
                  </>
                )
                : (
                  <>
                    <td><span class="muted" title="recall 수치는 main checkout과 같은 설치 단위로 집계됩니다">설치 공유</span></td>
                    <td><span class="muted" title="SLO는 설치 단위로 판정됩니다">설치 공유</span></td>
                  </>
                )}
              <td class="cell-action nowrap">
                <div class="row-links">
                  {(['wiki', 'recall'] as const).map(target => (
                    servable
                      ? (
                        <a
                          key={target}
                          class="btn btn-sm"
                          href={routeHash({tab: target, target: node.targetId})}
                          onClick={event => event.stopPropagation()}
                          onKeyDown={event => event.stopPropagation()}
                        >
                          {target === 'wiki' ? 'Wiki' : 'Recall'}
                        </a>
                      )
                      : (
                        <span key={target} class="btn btn-sm" aria-disabled="true" title={blocked}>
                          {target === 'wiki' ? 'Wiki' : 'Recall'}
                        </span>
                      )
                  ))}
                </div>
              </td>
            </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

type DetailState =
  | {state: 'loading'; sig: string}
  | {state: 'loaded'; sig: string; items: ErrorEventItem[]}
  | {state: 'failed'; sig: string; message: string};

type ErrorKind = 'all' | 'refused' | 'error';

const ERROR_FILTERS: readonly {id: ErrorKind; label: string; title: string}[] = [
  {id: 'all', label: '전체', title: '모든 오류·거절 이벤트'},
  {id: 'refused', label: '거절', title: 'recall이 거절·축소된 이벤트(recall*)'},
  {id: 'error', label: '오류', title: 'recall 밖의 오류 이벤트(색인·위키·런타임 등)'},
];

function errorKind(item: ErrorEventItem): Exclude<ErrorKind, 'all'> {
  return item.event === 'recall' || item.event.startsWith('recall.') ? 'refused' : 'error';
}

interface ErrorGroup {
  key: string;
  /** The newest event of the group; its diagnostics stand for the group. */
  head: ErrorEventItem;
  count: number;
  hosts: string[];
}

function groupErrors(errors: readonly ErrorEventItem[]): ErrorGroup[] {
  const groups = new Map<string, ErrorGroup>();
  for (const item of errors) {
    const key = `${item.installId}|${item.event}|${item.reason}`;
    const group = groups.get(key);
    if (group === undefined) {
      groups.set(key, {key, head: item, count: 1, hosts: item.host === null ? [] : [item.host]});
      continue;
    }
    group.count += 1;
    if (item.ts > group.head.ts) group.head = item;
    if (item.host !== null && !group.hosts.includes(item.host)) group.hosts.push(item.host);
  }
  return [...groups.values()].sort((a, b) => (a.head.ts < b.head.ts ? 1 : a.head.ts > b.head.ts ? -1 : 0));
}

function errorKey(item: ErrorEventItem): string {
  return `${item.installId}|${item.diagId ?? ''}|${item.ts}|${item.event}`;
}

interface ErrorListProps {
  errors: readonly ErrorEventItem[];
  projectName: (id: InstallId) => string;
  /** `errors24h` of each install in scope: the whole 24 h count, of which `errors` shows the newest part. */
  totals: ReadonlyMap<InstallId, number>;
}

function ErrorList({errors, projectName, totals}: ErrorListProps) {
  const [details, setDetails] = useState<Readonly<Record<InstallId, DetailState>>>({});
  const [open, setOpen] = useState<ReadonlySet<string>>(new Set());
  const [kind, setKind] = useState<ErrorKind>('all');

  const sigOf = (id: InstallId): string => errors.filter(row => row.installId === id).map(errorKey).join('\n');
  const counts = useMemo(() => {
    const result: Record<ErrorKind, number> = {all: errors.length, refused: 0, error: 0};
    for (const item of errors) result[errorKind(item)] += 1;
    return result;
  }, [errors]);
  const groups = useMemo(
    () => groupErrors(kind === 'all' ? errors : errors.filter(item => errorKind(item) === kind)),
    [errors, kind],
  );

  const fetchInstall = async (installId: InstallId, sig: string) => {
    setDetails(prev => ({...prev, [installId]: {state: 'loading', sig}}));
    try {
      const response = await getInstallErrors(installId, {details: true});
      setDetails(prev => (prev[installId]?.sig === sig
        ? {...prev, [installId]: {state: 'loaded', sig, items: response.items}}
        : prev));
    } catch (error) {
      const message = error instanceof ApiError ? `${error.code}: ${error.message}` : '불러오지 못했습니다.';
      setDetails(prev => (prev[installId]?.sig === sig
        ? {...prev, [installId]: {state: 'failed', sig, message}}
        : prev));
    }
  };

  useEffect(() => {
    const staleIds = Object.keys(details).filter(id => details[id]?.sig !== sigOf(id));
    if (staleIds.length === 0) return;
    setDetails(prev => {
      const next = {...prev};
      for (const id of staleIds) delete next[id];
      return next;
    });
    for (const id of staleIds) {
      const prefix = `${id}|`;
      if (Array.from(open).some(key => key.startsWith(prefix))) {
        const sig = sigOf(id);
        if (sig !== '') void fetchInstall(id, sig);
      }
    }
  }, [errors]);

  const load = async (group: ErrorGroup) => {
    const next = new Set(open);
    if (next.has(group.key)) {
      next.delete(group.key);
      setOpen(next);
      return;
    }
    next.add(group.key);
    setOpen(next);
    const installId = group.head.installId;
    const sig = sigOf(installId);
    const current = details[installId];
    if (current !== undefined && current.sig === sig && current.state !== 'failed') return;
    await fetchInstall(installId, sig);
  };

  const detailFor = (item: ErrorEventItem) => {
    const state = details[item.installId];
    if (state === undefined || state.state === 'loading') return <span class="muted">불러오는 중…</span>;
    if (state.state === 'failed') return <span class="text-red">{safeText(state.message)}</span>;
    const match = state.items.find(row => row.diagId !== null && row.diagId === item.diagId)
      ?? state.items.find(row => row.ts === item.ts && row.event === item.event);
    return match?.detail === undefined
      ? <span class="muted">이 이벤트에는 상세 진단이 없습니다.</span>
      : <pre class="code-block">{safeMultiline(match.detail)}</pre>;
  };

  const totalLine = [...totals]
    .filter(([, total]) => total > 0)
    .map(([id, total]) => `${projectName(id)} ${formatCount(total)}건`)
    .join(', ');
  const sameEventTitle = (group: ErrorGroup): string => {
    const total = totals.get(group.head.installId);
    const suffix = total === undefined ? '' : ` · 이 설치의 24시간 오류·거절 합계 ${formatCount(total)}건`;
    return `표시된 최근 오류 중 같은 이벤트 ${formatCount(group.count)}건${suffix}`;
  };

  if (errors.length === 0) return <div class="empty-note">최근 24시간 오류·거부 이벤트가 없습니다.</div>;
  return (
    <>
      <div class="card-actions filter-row" role="group" aria-label="이벤트 종류 필터">
        {ERROR_FILTERS.map(item => (
          <button
            key={item.id}
            type="button"
            class={`btn btn-sm${kind === item.id ? ' active' : ''}`}
            aria-pressed={kind === item.id}
            title={`${item.title} · 개수는 표시된 최근 목록 기준`}
            onClick={() => setKind(item.id)}
          >
            {item.label} <span class="mono muted">{formatCount(counts[item.id])}</span>
          </button>
        ))}
      </div>
      <div class="card-sub">
        표시된 최근 {formatCount(errors.length)}건 기준 개수입니다.
        {totalLine === '' ? null : <> 설치별 24시간 오류·거절 합계: {safeText(totalLine)}</>}
      </div>
      {groups.length === 0
        ? <div class="empty-note">이 종류의 이벤트가 없습니다.</div>
        : (
          <div class="table-wrap">
            <table>
              <thead>
                <tr><th>최근 시각</th><th>프로젝트</th><th>호스트</th><th>이벤트 · 사유</th><th /></tr>
              </thead>
              <tbody>
                {groups.map(group => {
                  const item = group.head;
                  const expanded = open.has(group.key);
                  return [
                    <tr key={group.key}>
                      <td class="mono nowrap">{formatClock(item.ts)}</td>
                      <td><strong>{safeText(projectName(item.installId))}</strong></td>
                      <td>
                        <div class="cell-badges">
                          {group.hosts.length === 0 ? '—' : group.hosts.map(host => <Badge key={host} tone="muted">{host}</Badge>)}
                        </div>
                      </td>
                      <td>
                        <div class="cell-badges">
                          <Badge tone={errorKind(item) === 'refused' ? 'amber' : 'red'}>
                            {errorKind(item) === 'refused' ? '거절' : '오류'}
                          </Badge>
                          <span class="mono">{safeText(item.event)} · {safeText(item.reason)}</span>
                          {group.count > 1
                            ? <Badge tone="muted" mono title={sameEventTitle(group)}>×{group.count}</Badge>
                            : null}
                        </div>
                      </td>
                      <td class="cell-action">
                        <button type="button" class="btn btn-sm" onClick={() => void load(group)} aria-expanded={expanded}>
                          {expanded ? '접기' : '상세 진단'}
                        </button>
                      </td>
                    </tr>,
                    expanded
                      ? (
                        <tr key={`${group.key}-detail`} class="detail-row">
                          <td colSpan={5}>
                            {group.count > 1
                              ? <div class="card-sub">가장 최근 이벤트({formatClock(item.ts)})의 진단입니다.</div>
                              : null}
                            {detailFor(item)}
                          </td>
                        </tr>
                      )
                      : null,
                  ];
                })}
              </tbody>
            </table>
          </div>
        )}
    </>
  );
}

export function OverviewPage({overview, selected, tab, reconciling, onReconcile}: Props) {
  const [showDormant, setShowDormant] = useShowDormant();
  const scoped = nodesInScope(overview.projects, selected);
  // The sidebar's rule: active rows, the selected row, and a dormant main heading a shown worktree.
  const selectedId = selected?.targetId ?? null;
  const hidden = scoped.length - shownNodes(overview.projects, scoped, false, selectedId).length;
  const nodes = shownNodes(overview.projects, scoped, showDormant, selectedId);
  const installIds = new Set(scoped.map(node => node.installId));
  const errors = overview.recentErrors.filter(item => installIds.has(item.installId)
    && (selected?.kind !== 'worktree' || item.targetId === null || item.targetId === selected.targetId));
  const names = new Map(overview.installs.map(install => [install.installId, install.projectName]));
  const projectName = (id: InstallId) => names.get(id) ?? id.slice(0, 8);
  const totals = new Map(overview.installs
    .filter(install => installIds.has(install.installId))
    .map(install => [install.installId, install.errors24h] as const));
  return (
    <>
      <Kpis scoped={scoped} overview={overview} />
      <section class="card" aria-label="프로젝트와 워크트리 현황">
        <div class="card-header">
          <div>
            <div class="card-title">온보딩 프로젝트 & 연결 Git Worktree 현황</div>
            <div class="card-sub">행을 누르면 그 체크아웃으로 전환합니다. 기본 조회는 git을 실행하지 않습니다.</div>
          </div>
          <div class="card-actions">
            <span class="card-sub mono">마지막 재스캔 {formatRelative(overview.snapshot.lastReconcileAt)}</span>
            {hidden > 0
              ? (
                <button
                  type="button"
                  class="btn btn-sm"
                  aria-pressed={showDormant}
                  title={DORMANT_TOGGLE_TITLE}
                  onClick={() => setShowDormant(!showDormant)}
                >
                  {dormantToggleLabel(hidden, showDormant)}
                </button>
              )
              : null}
            <button type="button" class="btn btn-sm" onClick={onReconcile} disabled={reconciling}>
              {reconciling ? '재스캔 중…' : '상태 재스캔'}
            </button>
          </div>
        </div>
        {nodes.length === 0
          ? <div class="empty-note">표시할 체크아웃이 없습니다.</div>
          : <CheckoutTable nodes={nodes} selected={selected} tab={tab} />}
      </section>
      <section class="card" aria-label="최근 오류">
        <div class="card-header">
          <div>
            <div class="card-title">최근 오류·거부 이벤트</div>
            <div class="card-sub">기본은 요약만 보여 주고, 상세 진단은 요청할 때만 불러옵니다.</div>
          </div>
        </div>
        <ErrorList errors={errors} projectName={projectName} totals={totals} />
      </section>
    </>
  );
}
