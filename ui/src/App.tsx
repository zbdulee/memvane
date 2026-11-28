import {useCallback, useEffect, useMemo, useRef, useState} from 'preact/hooks';
import {
  ApiError,
  checkInstallLearning,
  exchangeLaunchCode,
  getInstallLearning,
  getInstallRecalls,
  getOverview,
  getRuntime,
  getSession,
  getWikiCard,
  getWikiList,
  getWikiSource,
  putPreferences,
  reconcileOverview,
  runQueryPlayground,
} from './api/client';
import {SseCoordinator, type SseStatus} from './api/sse';
import type {OverviewResponse, SessionMeta, UiPreferences} from './api/types';
import {AlertBanner} from './components/AlertBanner';
import {cardHash} from './components/SafeMarkdown';
import {SidebarTree} from './components/SidebarTree';
import {Topbar} from './components/Topbar';
import {OverviewPage} from './pages/OverviewPage';
import {RecallPage} from './pages/RecallPage';
import {RuntimePage} from './pages/RuntimePage';
import {WikiPage} from './pages/WikiPage';
import {isTab, isTargetId, routeHash, useRoute} from './router';
import {alertsInScope, findGroup, findNode} from './scope';
import {safeText} from './text';
import {useTheme} from './theme';

type Boot =
  | {state: 'booting'}
  | {state: 'signed_out'}
  | {state: 'failed'; message: string}
  | {state: 'ready'; session: SessionMeta};

const REFRESH_DEBOUNCE_MS = 500;

function describe(error: unknown): string {
  if (error instanceof ApiError) return `${error.code} (${error.status})`;
  return '서버에 연결하지 못했습니다.';
}

export function App() {
  const [boot, setBoot] = useState<Boot>({state: 'booting'});
  const [theme, toggleTheme] = useTheme();

  useEffect(() => {
    let alive = true;
    void (async () => {
      await exchangeLaunchCode(launch => routeHash({
        tab: isTab(launch.tab) ? launch.tab : 'overview',
        target: isTargetId(launch.target) ? launch.target : null,
      }));
      try {
        const session = await getSession();
        if (alive) setBoot({state: 'ready', session});
      } catch (error) {
        if (!alive) return;
        if (error instanceof ApiError && (error.status === 401 || error.status === 403)) setBoot({state: 'signed_out'});
        else setBoot({state: 'failed', message: describe(error)});
      }
    })();
    return () => {
      alive = false;
    };
  }, []);

  if (boot.state === 'ready') return <Dashboard session={boot.session} theme={theme} onToggleTheme={toggleTheme} />;
  return (
    <div class="boot-screen">
      <div class="card boot-card">
        <div class="brand"><span class="brand-mark" aria-hidden="true">M</span><span>MemVane</span></div>
        {boot.state === 'booting' ? <p class="card-sub">연결 중…</p> : null}
        {boot.state === 'signed_out'
          ? <p class="card-sub">세션이 없거나 만료되었습니다. MemVane UI를 다시 열어 새 링크로 접속하세요.</p>
          : null}
        {boot.state === 'failed' ? <p class="card-sub">대시보드를 열지 못했습니다: {safeText(boot.message)}</p> : null}
      </div>
    </div>
  );
}

function Dashboard({session, theme, onToggleTheme}: {session: SessionMeta; theme: 'dark' | 'light'; onToggleTheme: () => void}) {
  const route = useRoute();
  const [overview, setOverview] = useState<OverviewResponse | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [reconciling, setReconciling] = useState(false);
  const [sse, setSse] = useState<SseStatus>('connecting');
  const [idle, setIdle] = useState<number | null>(session.idleSecondsRemaining);
  const [preferences, setPreferences] = useState<UiPreferences>(session.preferences);
  const [eventsRefreshToken, setEventsRefreshToken] = useState(0);
  const [runtimeRefreshToken, setRuntimeRefreshToken] = useState(0);
  const refreshTimer = useRef<number | null>(null);

  const refresh = useCallback(async () => {
    try {
      setOverview(await getOverview());
      setLoadError(null);
    } catch (error) {
      setLoadError(describe(error));
    }
  }, []);

  const scheduleRefresh = useCallback(() => {
    if (refreshTimer.current !== null) window.clearTimeout(refreshTimer.current);
    refreshTimer.current = window.setTimeout(() => {
      refreshTimer.current = null;
      void refresh();
    }, REFRESH_DEBOUNCE_MS);
  }, [refresh]);

  useEffect(() => {
    void refresh();
    const coordinator = new SseCoordinator();
    const offStatus = coordinator.onStatus(setSse);
    const offEvent = coordinator.onEvent(event => {
      switch (event.type) {
        case 'heartbeat':
          setIdle(event.data.idleSecondsRemaining);
          break;
        case 'events_updated':
          scheduleRefresh();
          setEventsRefreshToken(n => n + 1);
          break;
        case 'runtime_updated':
          scheduleRefresh();
          setRuntimeRefreshToken(n => n + 1);
          break;
        case 'session_started':
          scheduleRefresh();
          break;
        case 'server_draining':
          break;
      }
    });
    coordinator.start();
    return () => {
      offStatus();
      offEvent();
      coordinator.dispose();
      if (refreshTimer.current !== null) window.clearTimeout(refreshTimer.current);
    };
  }, [refresh, scheduleRefresh]);

  useEffect(() => {
    const timer = window.setInterval(() => setIdle(value => (value === null ? null : Math.max(0, value - 1))), 1000);
    return () => window.clearInterval(timer);
  }, []);

  const reconcile = async () => {
    setReconciling(true);
    try {
      setOverview(await reconcileOverview());
      setLoadError(null);
    } catch (error) {
      setLoadError(describe(error));
    } finally {
      setReconciling(false);
    }
  };

  const projects = overview?.projects ?? [];
  const selected = useMemo(() => findNode(projects, route.target), [projects, route.target]);
  const projectName = selected === null ? null : findGroup(projects, selected.installId)?.main.name ?? null;
  const alerts = overview === null ? [] : alertsInScope(overview, selected);

  const loadRuntime = useCallback(() => {
    void runtimeRefreshToken;
    return getRuntime();
  }, [runtimeRefreshToken]);

  const savePreferences = useCallback(async (changes: Partial<Pick<UiPreferences, 'autoOpenOnSessionStart'>>) => {
    const updated = await putPreferences(changes);
    setPreferences(updated);
    return updated;
  }, []);

  const openWikiCard = useCallback((path: string) => {
    if (selected === null) return;
    const rel = path.startsWith('.memvane/wiki/') ? path.slice('.memvane/wiki/'.length) : path;
    const hash = cardHash(selected.targetId, rel);
    if (hash !== null) window.location.hash = hash;
  }, [selected]);

  return (
    <div class="app-shell">
      <Topbar
        tab={route.tab}
        selected={selected}
        projectName={projectName}
        session={session}
        sse={sse}
        idleSecondsRemaining={idle}
        theme={theme}
        onToggleTheme={onToggleTheme}
      />
      <SidebarTree projects={projects} tab={route.tab} selected={selected?.targetId ?? null} />
      <main class="main-wrap">
        <div class="main-scroll">
          <AlertBanner alerts={alerts} snapshot={overview?.snapshot ?? null} sse={sse} />
          {loadError !== null
            ? <div class="inline-error" role="alert">현황을 불러오지 못했습니다: {safeText(loadError)}</div>
            : null}
          {route.target !== null && overview !== null && selected === null
            ? <div class="inline-error" role="alert">선택한 대상을 찾지 못했습니다. 전체 현황을 표시합니다.</div>
            : null}
          {route.tab === 'overview'
            ? overview === null
              ? <div class="empty-note">{loadError === null ? '현황을 불러오는 중…' : ''}</div>
              : <OverviewPage overview={overview} selected={selected} tab={route.tab} reconciling={reconciling} onReconcile={() => void reconcile()} />
            : null}
          {route.tab === 'wiki'
            ? <WikiPage selected={selected} loadList={getWikiList} loadCard={getWikiCard} loadSource={getWikiSource} />
            : null}
          {route.tab === 'recall'
            ? <RecallPage selected={selected} loadRecalls={getInstallRecalls} runPlayground={runQueryPlayground} onOpenCard={openWikiCard} refreshToken={eventsRefreshToken} />
            : null}
          {route.tab === 'runtime'
            ? <RuntimePage installId={selected?.installId ?? null} preferences={preferences} loadRuntime={loadRuntime} loadLearning={getInstallLearning} runLearningCheck={checkInstallLearning} savePreferences={savePreferences} />
            : null}
        </div>
      </main>
    </div>
  );
}
