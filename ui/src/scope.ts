// Which checkouts, installs and alerts a sidebar selection covers.

import type {
  AlertBannerItem,
  InstallId,
  OverviewResponse,
  ProjectGroup,
  TargetId,
  WorktreeNode,
} from './api/types';

export function allNodes(projects: readonly ProjectGroup[]): WorktreeNode[] {
  return projects.flatMap(group => [group.main, ...group.worktrees]);
}

export function findNode(projects: readonly ProjectGroup[], target: TargetId | null): WorktreeNode | null {
  if (target === null) return null;
  return allNodes(projects).find(node => node.targetId === target) ?? null;
}

export function findGroup(projects: readonly ProjectGroup[], installId: InstallId): ProjectGroup | null {
  return projects.find(group => group.installId === installId) ?? null;
}

/** All checkouts for "All Projects", a main checkout with its worktrees, or one worktree. */
export function nodesInScope(projects: readonly ProjectGroup[], selected: WorktreeNode | null): WorktreeNode[] {
  if (selected === null) return allNodes(projects);
  if (selected.kind === 'worktree') return [selected];
  const group = findGroup(projects, selected.installId);
  return group === null ? [selected] : [group.main, ...group.worktrees];
}

export function alertsInScope(overview: OverviewResponse, selected: WorktreeNode | null): AlertBannerItem[] {
  if (selected === null) return overview.alerts;
  const targets = new Set(nodesInScope(overview.projects, selected).map(node => node.targetId));
  return overview.alerts.filter(alert => {
    switch (alert.scope.kind) {
      case 'global':
        return true;
      case 'install':
        return alert.scope.installId === selected.installId;
      case 'target':
        return targets.has(alert.scope.targetId);
    }
  });
}
