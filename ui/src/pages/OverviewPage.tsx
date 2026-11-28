import {useEffect, useState} from 'preact/hooks';
import {ApiError, getInstallErrors} from '../api/client';
import type {
  ErrorEventItem,
  InstallId,
  OverviewResponse,
  SloVerdict,
  TabId,
  WorktreeNode,
} from '../api/types';
import {Badge, Dot, type Tone} from '../components/Badge';
import {navigate} from '../router';
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
  if (node.kind === 'main') return <Badge tone="blue">main</Badge>;
  switch (node.viewState) {
    case 'ready':
      return <Badge tone="green">linked</Badge>;
    case 'view_preparing':
      return <Badge tone="amber" mono>view_preparing</Badge>;
    case 'absent':
      return <Badge tone="muted">view 없음</Badge>;
    case 'unproved':
      return <Badge tone="red">미증명</Badge>;
  }
}

function Kpis({nodes, overview}: {nodes: readonly WorktreeNode[]; overview: OverviewResponse}) {
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
  const recalls = nodes.reduce((sum, node) => sum + node.recalls24h, 0);
  const slo = worstSlo(nodes.map(node => node.slo));
  const lastSync = latest(nodes.map(node => node.lastSyncAt));
  return (
    <section class="kpi-grid" aria-label="핵심 지표">
      <div class="kpi-box">
        <div class="kpi-label"><span>선택 범위 체크아웃</span><Badge tone="muted" mono>{overview.snapshot.state}</Badge></div>
        <div class="kpi-value num">{formatCount(nodes.length)}</div>
        <div class="kpi-foot"><span>프로젝트 {formatCount(mains.length)}</span><span>워크트리 {formatCount(worktrees)}</span></div>
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
          </tr>
        </thead>
        <tbody>
          {nodes.map(node => (
            <tr
              key={node.targetId}
              class={`clickable${node.kind === 'worktree' ? ' wt-row' : ''}${selected?.targetId === node.targetId ? ' selected-row' : ''}`}
              tabIndex={0}
              onClick={() => navigate({tab, target: node.targetId})}
              onKeyDown={event => {
                if (event.key === 'Enter') navigate({tab, target: node.targetId});
              }}
            >
              <td>
                <div class="cell-name">
                  <Dot tone={node.kind === 'worktree' && node.viewState !== 'ready' ? 'amber' : 'green'} />
                  <strong>{safeText(node.name)}</strong>
                  <Badge tone="muted" mono>⎇ {safeText(node.branch)}</Badge>
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
              <td class="mono num nowrap">
                {formatCount(node.recalls24h)}건 · {formatMs(node.p50Ms)} / {formatMs(node.p95Ms)}
              </td>
              <td><SloBadge slo={node.slo} /></td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

type DetailState =
  | {state: 'loading'; sig: string}
  | {state: 'loaded'; sig: string; items: ErrorEventItem[]}
  | {state: 'failed'; sig: string; message: string};

function errorKey(item: ErrorEventItem): string {
  return `${item.installId}|${item.diagId ?? ''}|${item.ts}|${item.event}`;
}

function ErrorList({errors, projectName}: {errors: readonly ErrorEventItem[]; projectName: (id: InstallId) => string}) {
  const [details, setDetails] = useState<Readonly<Record<InstallId, DetailState>>>({});
  const [open, setOpen] = useState<ReadonlySet<string>>(new Set());

  const sigOf = (id: InstallId): string => errors.filter(row => row.installId === id).map(errorKey).join('\n');

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

  const load = async (item: ErrorEventItem) => {
    const key = errorKey(item);
    const next = new Set(open);
    if (next.has(key)) {
      next.delete(key);
      setOpen(next);
      return;
    }
    next.add(key);
    setOpen(next);
    const sig = sigOf(item.installId);
    const current = details[item.installId];
    if (current !== undefined && current.sig === sig && current.state !== 'failed') return;
    await fetchInstall(item.installId, sig);
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

  if (errors.length === 0) return <div class="empty-note">최근 24시간 오류·거부 이벤트가 없습니다.</div>;
  return (
    <div class="table-wrap">
      <table>
        <thead>
          <tr><th>시각</th><th>프로젝트</th><th>호스트</th><th>이벤트 · 사유</th><th /></tr>
        </thead>
        <tbody>
          {errors.map(item => {
            const key = errorKey(item);
            const expanded = open.has(key);
            return [
              <tr key={key}>
                <td class="mono">{formatClock(item.ts)}</td>
                <td><strong>{safeText(projectName(item.installId))}</strong></td>
                <td>{item.host === null ? '—' : <Badge tone="muted">{item.host}</Badge>}</td>
                <td class="mono">{safeText(item.event)} · {safeText(item.reason)}</td>
                <td class="cell-action">
                  <button type="button" class="btn btn-sm" onClick={() => void load(item)} aria-expanded={expanded}>
                    {expanded ? '접기' : '상세 오류 로드'}
                  </button>
                </td>
              </tr>,
              expanded ? <tr key={`${key}-detail`} class="detail-row"><td colSpan={5}>{detailFor(item)}</td></tr> : null,
            ];
          })}
        </tbody>
      </table>
    </div>
  );
}

export function OverviewPage({overview, selected, tab, reconciling, onReconcile}: Props) {
  const nodes = nodesInScope(overview.projects, selected);
  const installIds = new Set(nodes.map(node => node.installId));
  const errors = overview.recentErrors.filter(item => installIds.has(item.installId)
    && (selected?.kind !== 'worktree' || item.targetId === null || item.targetId === selected.targetId));
  const names = new Map(overview.installs.map(install => [install.installId, install.projectName]));
  const projectName = (id: InstallId) => names.get(id) ?? id.slice(0, 8);
  return (
    <>
      <Kpis nodes={nodes} overview={overview} />
      <section class="card" aria-label="프로젝트와 워크트리 현황">
        <div class="card-header">
          <div>
            <div class="card-title">온보딩 프로젝트 & 연결 Git Worktree 현황</div>
            <div class="card-sub">행을 누르면 그 체크아웃으로 전환합니다. 기본 조회는 git을 실행하지 않습니다.</div>
          </div>
          <div class="card-actions">
            <span class="card-sub mono">마지막 재스캔 {formatRelative(overview.snapshot.lastReconcileAt)}</span>
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
        <ErrorList errors={errors} projectName={projectName} />
      </section>
    </>
  );
}
