// Checkout state of a sidebar or overview row: label, dot color, default visibility
// and whether the per-checkout views can open it. The dot shows the checkout state
// only; the SLO stays in its own column and badge.

import type {CheckoutViewState, ProjectGroup, TargetId, WorktreeNode} from './api/types';
import type {Tone} from './components/Badge';

interface StateInfo {
  tone: Tone;
  label: string;
  title: string;
  /** Hidden until "해제·없는 체크아웃 보기" is on. */
  dormant: boolean;
}

export const CHECKOUT_STATE: Record<CheckoutViewState, StateInfo> = {
  ready: {tone: 'green', label: '정상', title: '레지스트리 active이고 route 증명이 맞는 체크아웃', dormant: false},
  view_preparing: {tone: 'amber', label: '준비 중', title: '첫 sync 게시를 기다리는 cold view', dormant: false},
  proof_failed: {tone: 'red', label: '증명 실패', title: '경로는 있지만 route 재증명에 실패한 체크아웃', dormant: false},
  unreadable: {tone: 'amber', label: '확인 불가', title: '체크아웃 경로를 읽을 수 없음(권한 등)', dormant: false},
  unknown: {tone: 'amber', label: '알 수 없음', title: '대시보드가 모르는 레지스트리 상태', dormant: false},
  path_missing: {tone: 'red', label: '경로 없음', title: '체크아웃 경로가 사라짐(삭제 추정)', dormant: true},
  released: {tone: 'muted', label: '해제됨', title: '온보딩 해제나 worktree 정리로 조회 대상에서 빠짐', dormant: true},
  moved: {tone: 'muted', label: '이동됨', title: '같은 경로에 다른 디렉터리가 들어섬(root inode 변경)', dormant: true},
  conflict: {tone: 'red', label: '충돌', title: '두 설치가 같은 경로를 주장해 어느 쪽도 조회 대상이 아님', dormant: true},
};

function reasonSuffix(node: WorktreeNode): string {
  if (node.stateReason === null) return '';
  return node.viewState === 'unknown' ? ` · 레지스트리 값: ${node.stateReason}` : ` · 이유 코드: ${node.stateReason}`;
}

export function stateTitle(node: WorktreeNode): string {
  return CHECKOUT_STATE[node.viewState].title + reasonSuffix(node);
}

export function isDormant(node: WorktreeNode): boolean {
  return CHECKOUT_STATE[node.viewState].dormant;
}

/** Only these states pass the server's target proof; the others answer 404 or 409. */
export function isServable(node: WorktreeNode): boolean {
  return node.viewState === 'ready' || node.viewState === 'view_preparing';
}

/** Tooltip of a disabled selection or shortcut. */
export function unservableTitle(node: WorktreeNode): string {
  return `조회할 수 없는 체크아웃(상태: ${CHECKOUT_STATE[node.viewState].label})${reasonSuffix(node)}`;
}

/** The branch cell: the branch of a live checkout, the state label of a dormant one. */
export function branchText(node: WorktreeNode): string {
  return isDormant(node) ? CHECKOUT_STATE[node.viewState].label : `⎇ ${node.branch}`;
}

export function dotTone(node: WorktreeNode): Tone {
  return CHECKOUT_STATE[node.viewState].tone;
}

/**
 * The rows of `nodes` that show: every non-dormant row, the selected row, and a dormant
 * main that still has a shown worktree (as its group header); with `showDormant`, all.
 */
export function shownNodes(
  projects: readonly ProjectGroup[],
  nodes: readonly WorktreeNode[],
  showDormant: boolean,
  selected: TargetId | null,
): WorktreeNode[] {
  if (showDormant) return [...nodes];
  const keep = (node: WorktreeNode) => !isDormant(node) || node.targetId === selected;
  const headed = new Set(projects
    .filter(group => group.worktrees.some(node => nodes.includes(node) && keep(node)))
    .map(group => group.main.targetId));
  return nodes.filter(node => keep(node) || (node.kind === 'main' && headed.has(node.targetId)));
}

export function dormantToggleLabel(count: number, shown: boolean): string {
  return shown ? `해제·없는 체크아웃 ${count}개 숨기기` : `해제·없는 체크아웃 ${count}개 보기`;
}

export const DORMANT_TOGGLE_TITLE = '경로 없음·해제됨·이동됨·충돌 상태의 체크아웃';
