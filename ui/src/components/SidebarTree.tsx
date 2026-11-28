import {useMemo, useState} from 'preact/hooks';
import type {ProjectGroup, TabId, TargetId, WorktreeNode} from '../api/types';
import {
  CHECKOUT_STATE,
  branchText,
  dormantCount,
  dormantToggleLabel,
  dotTone,
  isDormant,
  stateTitle,
} from '../checkoutState';
import {navigate} from '../router';
import {allNodes} from '../scope';
import {safeText} from '../text';
import {Badge, Dot, type Tone} from './Badge';

interface Props {
  projects: readonly ProjectGroup[];
  tab: TabId;
  selected: TargetId | null;
  /** True until the first overview answer arrives; distinguishes "loading" from "no projects". */
  loading?: boolean;
}

function matches(node: WorktreeNode, query: string): boolean {
  if (query === '') return true;
  const hay = `${node.name}\n${node.branch}\n${node.displayRoot}`.toLowerCase();
  return hay.includes(query);
}

/** Worktree badge: `linked` for a live view, the state label otherwise. */
function viewBadge(node: WorktreeNode): {tone: Tone; label: string} | null {
  const info = CHECKOUT_STATE[node.viewState];
  if (node.kind === 'main') return node.viewState === 'ready' ? null : {tone: info.tone, label: info.label};
  return {tone: info.tone, label: node.viewState === 'ready' ? 'linked' : `linked · ${info.label}`};
}

function TreeNode(props: {node: WorktreeNode; active: boolean; tab: TabId; toggle?: {open: boolean; onToggle: () => void}}) {
  const {node, active, tab, toggle} = props;
  const select = () => navigate({tab, target: node.targetId});
  const view = viewBadge(node);
  return (
    <div
      class={`tree-item${node.kind === 'worktree' ? ' tree-wt' : ''}${active ? ' active' : ''}`}
      role="treeitem"
      aria-selected={active}
      tabIndex={0}
      title={safeText(node.displayRoot)}
      onClick={select}
      onKeyDown={event => {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault();
          select();
        }
      }}
    >
      <div class="tree-item-main">
        {toggle !== undefined
          ? (
            <button
              type="button"
              class="tree-toggle"
              aria-label={toggle.open ? '접기' : '펼치기'}
              aria-expanded={toggle.open}
              onClick={event => {
                event.stopPropagation();
                toggle.onToggle();
              }}
            >
              {toggle.open ? '▾' : '▸'}
            </button>
          )
          : null}
        <Dot tone={dotTone(node)} />
        <div class="tree-item-text">
          <span class="tree-name">{safeText(node.name)}</span>
          <span class={`tree-branch${isDormant(node) ? '' : ' mono'}`}>{safeText(branchText(node))}</span>
        </div>
      </div>
      {view !== null ? <Badge tone={view.tone} title={stateTitle(node)}>{view.label}</Badge> : null}
    </div>
  );
}

export function SidebarTree({projects, tab, selected, loading = false}: Props) {
  const [query, setQuery] = useState('');
  const [collapsed, setCollapsed] = useState<ReadonlySet<string>>(new Set());
  const [showDormant, setShowDormant] = useState(false);
  const needle = query.trim().toLowerCase();
  const hidden = useMemo(() => dormantCount(allNodes(projects)), [projects]);

  const visible = useMemo(() => projects.flatMap(group => {
    // Only active checkouts show by default; a dormant main without a live worktree hides its group.
    const live = showDormant ? group.worktrees : group.worktrees.filter(node => !isDormant(node));
    if (!showDormant && isDormant(group.main) && live.length === 0) return [];
    if (needle === '') return [{group, worktrees: live}];
    const mainHit = matches(group.main, needle);
    const worktrees = mainHit ? live : live.filter(node => matches(node, needle));
    return mainHit || worktrees.length > 0 ? [{group, worktrees}] : [];
  }), [projects, needle, showDormant]);

  const toggle = (installId: string) => {
    const next = new Set(collapsed);
    if (next.has(installId)) next.delete(installId);
    else next.add(installId);
    setCollapsed(next);
  };

  return (
    <aside class="sidebar" aria-label="프로젝트·워크트리">
      <div class="sidebar-search">
        <input
          type="search"
          placeholder="프로젝트 / 워크트리 / 브랜치 검색"
          aria-label="프로젝트, 워크트리, 브랜치 검색"
          value={query}
          onInput={event => setQuery(event.currentTarget.value)}
        />
      </div>
      <div class="sidebar-scroll" role="tree">
        <div
          class={`tree-item${selected === null ? ' active' : ''}`}
          role="treeitem"
          aria-selected={selected === null}
          tabIndex={0}
          onClick={() => navigate({tab, target: null})}
          onKeyDown={event => {
            if (event.key === 'Enter' || event.key === ' ') {
              event.preventDefault();
              navigate({tab, target: null});
            }
          }}
        >
          <div class="tree-item-main">
            <Dot tone="blue" />
            <div class="tree-item-text">
              <span class="tree-name">All Projects</span>
              <span class="tree-branch">전체 현황</span>
            </div>
          </div>
        </div>
        <div class="nav-section-label">
          <span>온보딩 프로젝트</span>
          <span class="mono">{loading ? '…' : projects.filter(group => !isDormant(group.main) || group.worktrees.some(node => !isDormant(node))).length}</span>
        </div>
        {loading
          ? <div class="sidebar-empty" role="status">프로젝트 목록을 불러오는 중…</div>
          : visible.length === 0
          ? <div class="sidebar-empty">{needle === '' ? '온보딩된 프로젝트가 없습니다.' : '검색 결과가 없습니다.'}</div>
          : visible.map(({group, worktrees}) => {
            const open = needle !== '' || !collapsed.has(group.installId);
            return (
              <div key={group.installId} role="group">
                <TreeNode
                  node={group.main}
                  active={selected === group.main.targetId}
                  tab={tab}
                  {...(worktrees.length > 0 ? {toggle: {open, onToggle: () => toggle(group.installId)}} : {})}
                />
                {open && worktrees.length > 0
                  ? (
                    <div class="tree-children">
                      {worktrees.map(node => (
                        <TreeNode key={node.targetId} node={node} active={selected === node.targetId} tab={tab} />
                      ))}
                    </div>
                  )
                  : null}
              </div>
            );
          })}
        {!loading && hidden > 0
          ? (
            <button
              type="button"
              class="btn btn-sm sidebar-dormant"
              aria-pressed={showDormant}
              title="경로 없음·해제됨·이동됨·충돌 상태의 체크아웃"
              onClick={() => setShowDormant(!showDormant)}
            >
              {dormantToggleLabel(hidden, showDormant)}
            </button>
          )
          : null}
      </div>
    </aside>
  );
}
