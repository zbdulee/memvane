import {useMemo, useState} from 'preact/hooks';
import type {LinkedWorktreeNode, ProjectGroup, TabId, TargetId, WorktreeNode} from '../api/types';
import {navigate} from '../router';
import {safeText} from '../text';
import {Badge, Dot, type Tone} from './Badge';

interface Props {
  projects: readonly ProjectGroup[];
  tab: TabId;
  selected: TargetId | null;
}

const VIEW_BADGE: Record<LinkedWorktreeNode['viewState'], {tone: Tone; label: string; title: string}> = {
  ready: {tone: 'green', label: 'linked', title: '연결 worktree의 자체 위키 view가 준비됨'},
  view_preparing: {tone: 'amber', label: 'linked · 준비 중', title: '첫 sync 게시를 기다리는 cold view'},
  absent: {tone: 'muted', label: 'linked · 없음', title: '이 worktree의 view가 아직 없음'},
  unproved: {tone: 'red', label: 'linked · 미증명', title: 'route 재증명에 실패한 worktree'},
};

function matches(node: WorktreeNode, query: string): boolean {
  if (query === '') return true;
  const hay = `${node.name}\n${node.branch}\n${node.displayRoot}`.toLowerCase();
  return hay.includes(query);
}

function nodeTone(node: WorktreeNode): Tone {
  if (node.kind === 'worktree' && node.viewState !== 'ready') return VIEW_BADGE[node.viewState].tone;
  return node.slo === 'over' ? 'red' : 'green';
}

function TreeNode(props: {node: WorktreeNode; active: boolean; tab: TabId; toggle?: {open: boolean; onToggle: () => void}}) {
  const {node, active, tab, toggle} = props;
  const select = () => navigate({tab, target: node.targetId});
  const view = node.kind === 'worktree' ? VIEW_BADGE[node.viewState] : null;
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
          : <Dot tone={nodeTone(node)} />}
        <div class="tree-item-text">
          <span class="tree-name">{safeText(node.name)}</span>
          <span class="tree-branch mono">⎇ {safeText(node.branch)}</span>
        </div>
      </div>
      {view !== null ? <Badge tone={view.tone} title={view.title}>{view.label}</Badge> : null}
    </div>
  );
}

export function SidebarTree({projects, tab, selected}: Props) {
  const [query, setQuery] = useState('');
  const [collapsed, setCollapsed] = useState<ReadonlySet<string>>(new Set());
  const needle = query.trim().toLowerCase();

  const visible = useMemo(() => projects.flatMap(group => {
    if (needle === '') return [{group, worktrees: group.worktrees}];
    const mainHit = matches(group.main, needle);
    const worktrees = mainHit ? group.worktrees : group.worktrees.filter(node => matches(node, needle));
    return mainHit || worktrees.length > 0 ? [{group, worktrees}] : [];
  }), [projects, needle]);

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
          <span class="mono">{projects.length}</span>
        </div>
        {visible.length === 0
          ? <div class="sidebar-empty">{needle === '' ? '온보딩된 프로젝트가 없습니다.' : '검색 결과가 없습니다.'}</div>
          : visible.map(({group, worktrees}) => {
            const open = needle !== '' || !collapsed.has(group.installId);
            return (
              <div key={group.installId} role="group">
                <TreeNode
                  node={group.main}
                  active={selected === group.main.targetId}
                  tab={tab}
                  {...(group.worktrees.length > 0 ? {toggle: {open, onToggle: () => toggle(group.installId)}} : {})}
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
      </div>
    </aside>
  );
}
