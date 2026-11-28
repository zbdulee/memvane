import type {SessionMeta, TabId, WorktreeNode} from '../api/types';
import type {SseStatus} from '../api/sse';
import {navigate, TABS} from '../router';
import {formatDuration, safeText} from '../text';
import type {Theme} from '../theme';
import {Dot, type Tone} from './Badge';

interface Props {
  tab: TabId;
  selected: WorktreeNode | null;
  projectName: string | null;
  session: SessionMeta | null;
  sse: SseStatus;
  idleSecondsRemaining: number | null;
  theme: Theme;
  onToggleTheme: () => void;
}

const SSE_LABEL: Record<SseStatus, {tone: Tone; label: string; title: string}> = {
  connecting: {tone: 'muted', label: 'Connecting', title: '실시간 스트림 연결 중'},
  leader: {tone: 'green', label: 'Leader', title: '이 탭이 실시간 스트림을 유지하고 다른 탭에 전달합니다'},
  follower: {tone: 'blue', label: 'Follower', title: '다른 탭이 유지하는 실시간 스트림을 받습니다'},
  reconnecting: {tone: 'amber', label: 'Reconnecting', title: '실시간 스트림 재연결 중'},
  draining: {tone: 'red', label: 'Draining', title: '서버가 종료 중입니다. 자동 재연결을 멈췄습니다'},
  rate_limited: {tone: 'red', label: 'Stream limit', title: '동시 스트림 한도를 넘어 자동 재연결을 멈췄습니다'},
  unauthorized: {tone: 'red', label: 'Signed out', title: '세션이 만료되었습니다. 새 링크로 다시 여세요'},
};

export function Topbar(props: Props) {
  const {tab, selected, projectName, session, sse, idleSecondsRemaining, theme, onToggleTheme} = props;
  const sseLabel = SSE_LABEL[sse];
  return (
    <header class="topbar">
      <div class="topbar-left">
        <div class="brand">
          <span class="brand-mark" aria-hidden="true">M</span>
          <span>MemVane</span>
        </div>
        <nav class="breadcrumb" aria-label="선택한 대상">
          {selected === null
            ? <strong>All Projects</strong>
            : (
              <>
                {selected.kind === 'worktree' && projectName !== null
                  ? <><span>{safeText(projectName)}</span><span aria-hidden="true">›</span></>
                  : null}
                <strong>{safeText(selected.name)}</strong>
                <span class="badge badge-muted">{selected.kind === 'main' ? 'main' : 'worktree'}</span>
                <span class="badge badge-muted mono">⎇ {safeText(selected.branch)}</span>
              </>
            )}
        </nav>
        <nav class="tab-nav" aria-label="화면">
          {TABS.map(item => (
            <button
              key={item.id}
              type="button"
              class={`tab-btn${item.id === tab ? ' active' : ''}`}
              aria-current={item.id === tab ? 'page' : undefined}
              onClick={() => navigate({tab: item.id, target: selected?.targetId ?? null})}
            >
              {item.label}
            </button>
          ))}
        </nav>
      </div>
      <div class="topbar-right">
        <span class="pill" title={sseLabel.title}><Dot tone={sseLabel.tone} />SSE {sseLabel.label}</span>
        {session !== null ? <span class="pill mono" title="서버 번들 버전">v{safeText(session.bundleVersion)}</span> : null}
        {session !== null && !session.isInstalledPluginCache
          ? <span class="pill pill-warn" title="설치된 플러그인 캐시가 아닌 개발 체크아웃에서 실행 중">Read-Only (Dev Checkout)</span>
          : null}
        {idleSecondsRemaining !== null
          ? <span class="pill mono" title="모든 창이 닫힌 뒤 서버가 스스로 종료하기까지 남은 시간">유휴 {formatDuration(idleSecondsRemaining)}</span>
          : null}
        <button type="button" class="btn btn-sm" onClick={onToggleTheme} aria-label="테마 전환">
          {theme === 'dark' ? '라이트 테마' : '다크 테마'}
        </button>
      </div>
    </header>
  );
}
