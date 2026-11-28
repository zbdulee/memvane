import {useEffect, useRef, useState} from 'preact/hooks';
import type {
  DiskUsageItem,
  InstallId,
  LearningResponse,
  PromotionGate,
  ProviderProofState,
  RuntimeResponse,
  SharedWorkerState,
  UiPreferences,
  WakeQueueItem,
  WakeState,
} from '../api/types';
import {Badge, type Tone} from '../components/Badge';
import {formatCount, formatDuration, safeText} from '../text';

/** `invariants` arrives only from the on-demand integrity check. */
export interface LearningInvariants {
  status: string;
  violations?: number;
  rules?: Record<string, number>;
  error?: string;
}

export type LearningView = LearningResponse & {invariants?: LearningInvariants | null};

export interface RuntimePageProps {
  /** Install of the selected checkout; null shows the runtime part only. */
  installId: InstallId | null;
  preferences: UiPreferences | null;
  loadRuntime: () => Promise<RuntimeResponse>;
  loadLearning: (installId: InstallId) => Promise<LearningView>;
  runLearningCheck: (installId: InstallId) => Promise<LearningView>;
  savePreferences: (changes: Partial<Pick<UiPreferences, 'autoOpenOnSessionStart'>>) => Promise<UiPreferences>;
}

type Load<T> = {state: 'loading'} | {state: 'ready'; value: T} | {state: 'failed'; message: string};

function failure(error: unknown): string {
  return error instanceof Error ? safeText(error.message) : '요청에 실패했습니다.';
}

// ---------------------------------------------------------------- wakes

const WAKE_STATE: Record<WakeState, {label: string; tone: Tone}> = {
  running: {label: '실행 중', tone: 'green'},
  waiting: {label: '실행 대기', tone: 'blue'},
  deferred_for_resources: {label: '자원 부족 보류', tone: 'amber'},
};

function untilText(iso: string | null, now: number): string | null {
  if (iso === null) return null;
  const at = Date.parse(iso);
  if (Number.isNaN(at)) return null;
  const seconds = Math.round((at - now) / 1000);
  if (seconds <= 0) return null;
  if (seconds < 60) return `${seconds}초`;
  if (seconds < 3600) return `약 ${Math.round(seconds / 60)}분`;
  return `약 ${Math.round(seconds / 3600)}시간`;
}

export function nextRunText(item: WakeQueueItem, now: number = Date.now()): string {
  if (item.state === 'running') return `${formatCount(item.attempt)}회차 진행 중`;
  const until = untilText(item.dueAt, now);
  if (item.deferrals > 0) {
    return `${until === null ? '곧' : `${until} 뒤`} 재시도 (${formatCount(item.deferrals)}회 미룸)`;
  }
  return until === null ? '곧 시작' : `${until} 뒤 시작 예정`;
}

function WakeTable({wakes}: {wakes: readonly WakeQueueItem[]}) {
  const now = Date.now();
  return (
    <section class="card">
      <div class="card-header">
        <div>
          <div class="card-title">백그라운드 작업 대기열</div>
          <p class="card-sub">색인·위키·학습 작업이 지금 무엇을 하고 언제 다시 도는지 보여 줍니다.</p>
        </div>
      </div>
      {wakes.length === 0 ? (
        <div class="empty-note">대기 중인 작업이 없습니다.</div>
      ) : (
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>작업 종류</th>
                <th>대상 프로젝트</th>
                <th>현재 상태</th>
                <th>다음 실행 / 재시도</th>
              </tr>
            </thead>
            <tbody>
              {wakes.map((item, index) => (
                <tr key={`${item.kind}:${item.installId ?? ''}:${index}`}>
                  <td>{safeText(item.label)}</td>
                  <td>{item.projectName === null ? <span class="muted">—</span> : safeText(item.projectName)}</td>
                  <td><Badge tone={WAKE_STATE[item.state].tone}>{WAKE_STATE[item.state].label}</Badge></td>
                  <td>{nextRunText(item, now)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <p class="card-sub">
        실행 중: 지금 처리하는 작업 · 실행 대기: 예정 시각이 되면 시작 · 자원 부족 보류: CPU·메모리 여유가 생길 때까지 미룸
      </p>
    </section>
  );
}

// ---------------------------------------------------------------- resident

const PROOF_STATE: Record<ProviderProofState['state'], {label: string; tone: Tone}> = {
  proved: {label: '증명됨', tone: 'green'},
  attestation_invalid: {label: '증명 실패 (자동 재증명 대기)', tone: 'amber'},
  unavailable: {label: '사용 불가', tone: 'muted'},
};

function ResidentCard(props: {
  workers: readonly SharedWorkerState[];
  proofs: readonly ProviderProofState[];
  installId: InstallId | null;
}) {
  const proofs = props.installId === null ? props.proofs : props.proofs.filter(p => p.installId === props.installId);
  return (
    <section class="card">
      <div class="card-header">
        <div>
          <div class="card-title">임베딩 런타임</div>
          <p class="card-sub">공유 임베딩 워커와 설치별 provider 증명 상태를 따로 보여 줍니다.</p>
        </div>
      </div>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>공유 워커 소켓</th>
              <th>상태</th>
              <th>PID</th>
              <th>번들 버전</th>
            </tr>
          </thead>
          <tbody>
            {props.workers.length === 0 ? (
              <tr><td colSpan={4} class="muted">기록된 공유 워커가 없습니다.</td></tr>
            ) : (
              props.workers.map(worker => (
                <tr key={worker.socket}>
                  <td class="mono">{safeText(worker.socket)}</td>
                  <td>{worker.running ? <Badge tone="green">실행 중</Badge> : <Badge tone="muted">멈춤</Badge>}</td>
                  <td>{worker.pid === null ? '—' : String(worker.pid)}</td>
                  <td>{worker.bundleVersion === null ? '—' : safeText(worker.bundleVersion)}</td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>설치</th>
              <th>provider 증명</th>
              <th>재증명까지</th>
            </tr>
          </thead>
          <tbody>
            {proofs.length === 0 ? (
              <tr><td colSpan={3} class="muted">증명된 설치가 없습니다.</td></tr>
            ) : (
              proofs.map(proof => (
                <tr key={proof.installId}>
                  <td class="mono">{safeText(proof.installId.slice(0, 12))}</td>
                  <td><Badge tone={PROOF_STATE[proof.state].tone}>{PROOF_STATE[proof.state].label}</Badge></td>
                  <td>{proof.reproofInSeconds === null ? '—' : formatDuration(proof.reproofInSeconds)}</td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </section>
  );
}

// ---------------------------------------------------------------- disk

const BYTE_UNITS = ['B', 'KB', 'MB', 'GB', 'TB'] as const;

export function formatBytes(bytes: number): string {
  let value = Math.max(0, bytes);
  let unit = 0;
  while (value >= 1024 && unit < BYTE_UNITS.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${unit === 0 ? value : value.toFixed(1)} ${BYTE_UNITS[unit]}`;
}

function DiskCard(props: {
  disk: readonly DiskUsageItem[];
  preferences: UiPreferences | null;
  savePreferences: RuntimePageProps['savePreferences'];
}) {
  const [autoOpen, setAutoOpen] = useState<boolean | null>(props.preferences?.autoOpenOnSessionStart ?? null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    setAutoOpen(props.preferences?.autoOpenOnSessionStart ?? null);
  }, [props.preferences?.autoOpenOnSessionStart]);

  const toggle = (next: boolean) => {
    setSaving(true);
    setError(null);
    props.savePreferences({autoOpenOnSessionStart: next}).then(
      saved => {
        setAutoOpen(saved.autoOpenOnSessionStart);
        setSaving(false);
      },
      (reason: unknown) => {
        setError(failure(reason));
        setSaving(false);
      },
    );
  };

  return (
    <section class="card">
      <div class="card-header">
        <div>
          <div class="card-title">디스크 사용량</div>
          <p class="card-sub">MemVane가 이 계정에 둔 상태와 런타임의 크기입니다. 링크는 따라가지 않습니다.</p>
        </div>
      </div>
      {props.disk.length === 0 ? (
        <div class="empty-note">집계할 디렉터리가 없습니다.</div>
      ) : (
        <div class="table-wrap">
          <table>
            <tbody>
              {props.disk.map(item => (
                <tr key={item.label}>
                  <td>{safeText(item.label)}</td>
                  <td class="cell-action">{formatBytes(item.bytes)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <div class="card-header">
        <label>
          <input
            type="checkbox"
            checked={autoOpen === true}
            disabled={autoOpen === null || saving}
            onChange={event => toggle((event.currentTarget as HTMLInputElement).checked)}
          />{' '}
          세션 시작 시 대시보드 자동으로 열기 <span class="muted mono">autoOpenOnSessionStart</span>
        </label>
        {saving ? <span class="muted">저장 중…</span> : null}
      </div>
      <p class="card-sub">
        온보딩된 프로젝트의 세션이 시작될 때 대시보드가 닫혀 있으면 한 창만 엽니다. 쿨다운
        {props.preferences === null ? '' : ` ${formatCount(props.preferences.autoOpenCooldownMinutes)}분`} 안에는 다시 열지 않습니다.
      </p>
      {error === null ? null : <div class="inline-error">{error}</div>}
    </section>
  );
}

// ---------------------------------------------------------------- learning

const GATE_TEXT: Record<PromotionGate['code'], {title: string; meaning: string}> = {
  candidateTrain: {title: '학습용 편집 사례', meaning: '후보 모델 학습에 쓰는 Teacher 정답 사례'},
  candidateEval: {title: '검증용 편집 사례', meaning: '학습에 쓰지 않고 정확도 평가에만 쓰는 홀드아웃 사례'},
  candidateEvalNonEmpty: {title: '카드 선택 정답 사례', meaning: '관련 카드 주입이 정답인 검증 사례'},
  candidateEvalEmpty: {title: '비선택 정답 사례', meaning: '카드를 넣지 않는 것이 정답인 검증 사례(과주입 방지)'},
};

function percent(value: number | null): string {
  return value === null ? '—' : `${(value * 100).toFixed(1)}%`;
}

function GateRow({gate}: {gate: PromotionGate}) {
  const text = GATE_TEXT[gate.code];
  const done = gate.current >= gate.required;
  return (
    <div class="kpi-box">
      <div class="kpi-label">
        <span>{text.title}</span>
        <span class="mono">{gate.code}</span>
      </div>
      <div class="kpi-value">
        {formatCount(gate.current)}<span class="kpi-unit"> / {formatCount(gate.required)}</span>
      </div>
      <progress max={gate.required} value={Math.min(gate.current, gate.required)} aria-label={text.title} />
      <div class="kpi-foot">
        {done ? <Badge tone="green">충족</Badge> : null}
        <span>{text.meaning}</span>
      </div>
    </div>
  );
}

function Invariants({value}: {value: LearningInvariants | null}) {
  if (value === null) return <div class="inline-error">무결성 점검 결과를 읽지 못했습니다.</div>;
  if (value.status !== 'checked') {
    return <div class="empty-note">무결성 점검: {safeText(value.status)}{value.error === undefined ? '' : ` (${safeText(value.error)})`}</div>;
  }
  const rules = Object.entries(value.rules ?? {}).filter(([, count]) => count > 0);
  return (
    <div class="kpi-foot">
      {value.violations === 0 ? (
        <Badge tone="green">위반 0건</Badge>
      ) : (
        <Badge tone="red">위반 {formatCount(value.violations ?? 0)}건</Badge>
      )}
      {rules.map(([rule, count]) => (
        <span key={rule} class="mono">{safeText(rule)}: {formatCount(count)}</span>
      ))}
    </div>
  );
}

function LearningCard(props: {installId: InstallId} & Pick<RuntimePageProps, 'loadLearning' | 'runLearningCheck'>) {
  const [learning, setLearning] = useState<Load<LearningView>>({state: 'loading'});
  const [checking, setChecking] = useState(false);
  const [checkError, setCheckError] = useState<string | null>(null);
  const ticketRef = useRef(0);
  const {installId, loadLearning, runLearningCheck} = props;

  useEffect(() => {
    const ticket = ++ticketRef.current;
    setLearning({state: 'loading'});
    setChecking(false);
    setCheckError(null);
    loadLearning(installId).then(
      value => ticket === ticketRef.current && setLearning({state: 'ready', value}),
      (reason: unknown) => ticket === ticketRef.current && setLearning({state: 'failed', message: failure(reason)}),
    );
    return () => {
      ticketRef.current += 1;
    };
  }, [installId, loadLearning]);

  const check = () => {
    const ticket = ++ticketRef.current;
    setChecking(true);
    setCheckError(null);
    runLearningCheck(installId).then(
      value => {
        if (ticket !== ticketRef.current) return;
        setLearning({state: 'ready', value});
        setChecking(false);
      },
      (reason: unknown) => {
        if (ticket !== ticketRef.current) return;
        setCheckError(failure(reason));
        setChecking(false);
      },
    );
  };

  return (
    <section class="card">
      <div class="card-header">
        <div>
          <div class="card-title">학습(Selector) 모니터</div>
          <p class="card-sub">
            새 Selector 후보 모델을 평가·자동 승격하기 위한 4가지 최소 데이터 조건입니다. 모두 충족되면 기존 모델과 비교해 더
            나을 때만 자동 적용합니다.
          </p>
        </div>
        <div class="card-actions">
          <button type="button" class="btn btn-sm" disabled={checking || learning.state !== 'ready'} onClick={check}>
            {checking ? '점검 중…' : '무결성 점검 실행'}
          </button>
        </div>
      </div>
      {learning.state === 'loading' ? <div class="empty-note">불러오는 중…</div> : null}
      {learning.state === 'failed' ? <div class="inline-error">{learning.message}</div> : null}
      {learning.state === 'ready' ? (
        <>
          <div class="kpi-grid">
            {learning.value.gates.map(gate => <GateRow key={gate.code} gate={gate} />)}
          </div>
          <div class="kpi-foot">
            <span>
              활성 체크포인트{' '}
              {learning.value.checkpoint === null ? '없음 (기본 순위)' : <span class="mono">{safeText(learning.value.checkpoint)}</span>}
            </span>
            <span>
              사례 {formatCount(learning.value.caseRows)} / {formatCount(learning.value.caseRowsCap)} 순환 보관
            </span>
          </div>
          <div class="kpi-foot">
            <span>정확 일치율 <span class="mono">exactRate</span> {percent(learning.value.metrics.exactRate)}</span>
            <span>카드 F1 <span class="mono">microF1</span> {percent(learning.value.metrics.microF1)}</span>
            <span>기본 순위 기준선 <span class="mono">rankExactRate</span> {percent(learning.value.metrics.rankExactRate)}</span>
          </div>
          {learning.value.teacher.length === 0 ? null : (
            <div class="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Teacher 호스트</th>
                    <th>대기 중 라벨링</th>
                    <th>대기 사유</th>
                  </tr>
                </thead>
                <tbody>
                  {learning.value.teacher.map(row => (
                    <tr key={row.host}>
                      <td>{row.host}</td>
                      <td>{formatCount(row.callsToday)}</td>
                      <td>{row.waitingReason === null ? '—' : <span class="mono">{safeText(row.waitingReason)}</span>}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {learning.value.invariants === undefined ? null : <Invariants value={learning.value.invariants} />}
        </>
      ) : null}
      {checkError === null ? null : <div class="inline-error">{checkError}</div>}
    </section>
  );
}

// ---------------------------------------------------------------- page

export function RuntimePage(props: RuntimePageProps) {
  const [runtime, setRuntime] = useState<Load<RuntimeResponse>>({state: 'loading'});
  const {loadRuntime} = props;

  useEffect(() => {
    let live = true;
    loadRuntime().then(
      value => live && setRuntime({state: 'ready', value}),
      (reason: unknown) => live && setRuntime({state: 'failed', message: failure(reason)}),
    );
    return () => {
      live = false;
    };
  }, [loadRuntime]);

  return (
    <>
      {runtime.state === 'loading' ? <section class="card"><div class="empty-note">런타임 상태를 불러오는 중…</div></section> : null}
      {runtime.state === 'failed' ? <div class="inline-error">{runtime.message}</div> : null}
      {runtime.state === 'ready' ? (
        <>
          <WakeTable wakes={runtime.value.wakes} />
          <ResidentCard workers={runtime.value.sharedWorkers} proofs={runtime.value.proofs} installId={props.installId} />
        </>
      ) : null}
      {props.installId === null ? (
        <section class="card"><div class="empty-note">학습 모니터는 사이드바에서 프로젝트를 고르면 보입니다.</div></section>
      ) : (
        <LearningCard installId={props.installId} loadLearning={props.loadLearning} runLearningCheck={props.runLearningCheck} />
      )}
      <DiskCard
        disk={runtime.state === 'ready' ? runtime.value.disk : []}
        preferences={props.preferences}
        savePreferences={props.savePreferences}
      />
    </>
  );
}
