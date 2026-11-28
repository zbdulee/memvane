// Checkout state of a sidebar or overview row: label, dot color and default visibility.
// The dot shows the checkout state only; the SLO stays in its own column and badge.

import type {CheckoutViewState, WorktreeNode} from './api/types';
import type {Tone} from './components/Badge';

interface StateInfo {
  tone: Tone;
  label: string;
  title: string;
  /** Hidden until "해제·없는 worktree 보기" is on. */
  dormant: boolean;
}

export const CHECKOUT_STATE: Record<CheckoutViewState, StateInfo> = {
  ready: {tone: 'green', label: '정상', title: '레지스트리 active이고 route 증명이 맞는 체크아웃', dormant: false},
  view_preparing: {tone: 'amber', label: '준비 중', title: '첫 sync 게시를 기다리는 cold view', dormant: false},
  proof_failed: {tone: 'red', label: '증명 실패', title: '경로는 있지만 route 재증명에 실패한 체크아웃', dormant: false},
  path_missing: {tone: 'red', label: '경로 없음', title: '체크아웃 경로가 사라짐(삭제 추정)', dormant: true},
  released: {tone: 'muted', label: '해제됨', title: '온보딩 해제나 worktree 정리로 조회 대상에서 빠짐', dormant: true},
  moved: {tone: 'muted', label: '이동됨', title: '같은 경로에 다른 디렉터리가 들어섬(root inode 변경)', dormant: true},
  conflict: {tone: 'red', label: '충돌', title: '두 설치가 같은 경로를 주장해 어느 쪽도 조회 대상이 아님', dormant: true},
};

export function stateTitle(node: WorktreeNode): string {
  const info = CHECKOUT_STATE[node.viewState];
  return node.proofReason === null ? info.title : `${info.title} · 이유 코드: ${node.proofReason}`;
}

export function isDormant(node: WorktreeNode): boolean {
  return CHECKOUT_STATE[node.viewState].dormant;
}

/** The branch cell: the branch of a live checkout, the state label of a dormant one. */
export function branchText(node: WorktreeNode): string {
  return isDormant(node) ? CHECKOUT_STATE[node.viewState].label : `⎇ ${node.branch}`;
}

export function dotTone(node: WorktreeNode): Tone {
  return CHECKOUT_STATE[node.viewState].tone;
}

/** Number of dormant checkouts: dormant main rows and dormant worktrees. */
export function dormantCount(nodes: readonly WorktreeNode[]): number {
  return nodes.filter(isDormant).length;
}

export function dormantToggleLabel(count: number, shown: boolean): string {
  return shown ? `해제·없는 worktree ${count}개 숨기기` : `해제·없는 worktree ${count}개 보기`;
}
