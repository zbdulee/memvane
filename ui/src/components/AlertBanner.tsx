import {useState} from 'preact/hooks';
import type {AlertBannerItem, AlertSeverity, SnapshotMeta} from '../api/types';
import type {SseStatus} from '../api/sse';
import {formatDuration, safeText} from '../text';
import {Badge, type Tone} from './Badge';

const TONE: Record<AlertSeverity, Tone> = {info: 'blue', warn: 'amber', error: 'red'};
const SEVERITY_RANK: Record<AlertSeverity, number> = {info: 0, warn: 1, error: 2};

const SNAPSHOT_NOTE: Partial<Record<SnapshotMeta['state'], {tone: Tone; text: string}>> = {
  stale: {tone: 'amber', text: '등록 정보가 오래되었습니다. 상태 재스캔으로 갱신할 수 있습니다.'},
  absent: {tone: 'amber', text: '등록 정보가 없습니다. 온보딩된 프로젝트가 아직 없을 수 있습니다.'},
  busy: {tone: 'amber', text: '다른 작업이 등록 정보를 갱신하는 중입니다. 잠시 뒤 다시 읽습니다.'},
  recovery_required: {tone: 'red', text: '등록 정보 복구가 필요합니다.'},
};

/** Alert codes the snapshot note already explains, keyed by the snapshot state that covers them. */
const COVERED_BY_SNAPSHOT: Partial<Record<SnapshotMeta['state'], string>> = {
  stale: 'registry_stale',
  absent: 'registry_absent',
  busy: 'registry_busy',
  recovery_required: 'registry_recovery_required',
};

const CODE_TEXT: Record<string, string> = {
  laya_vector_selected_provider_preparing: '임베딩 provider를 준비하는 중이라 일부 recall이 키워드 검색만 사용했습니다.',
  laya_vector_selected_provider_start_failed: '임베딩 provider를 시작하지 못해 recall이 키워드 검색으로 대체되었습니다.',
  needs_extractor: '문서 추출기가 없어 일부 원문을 색인하지 못했습니다.',
  candidate_storage_full: '학습 후보 저장 공간이 가득 차 새 사례를 보관하지 못했습니다.',
  selected_recall_settings_changed: 'recall 설정이 바뀌어 선택된 색인을 다시 봉인해야 합니다.',
  reader_settings_resealing: 'reader 설정을 다시 봉인하는 중입니다.',
  worker_attestation_unavailable: '임베딩 워커 증명을 확인할 수 없습니다.',
  attestation_invalid: '임베딩 provider 증명이 맞지 않아 자동 재증명을 기다립니다.',
  view_preparing: '연결 worktree의 위키 view가 첫 게시를 기다리는 중입니다.',
  registry_stale: '등록 정보가 오래되었습니다.',
  registry_incomplete: '등록 정보 일부를 읽지 못했습니다.',
};

export function describeAlertCode(code: string): string | null {
  const known = CODE_TEXT[code];
  if (known !== undefined) return known;
  if (code.startsWith('laya_vector_')) return '벡터 검색 경로에 문제가 있어 일부 recall이 축소된 경로로 동작했습니다.';
  return null;
}

interface AlertGroup {
  code: string;
  severity: AlertSeverity;
  items: AlertBannerItem[];
}

function groupAlerts(alerts: readonly AlertBannerItem[]): AlertGroup[] {
  const groups = new Map<string, AlertGroup>();
  for (const alert of alerts) {
    const group = groups.get(alert.code);
    if (group === undefined) {
      groups.set(alert.code, {code: alert.code, severity: alert.severity, items: [alert]});
    } else {
      group.items.push(alert);
      if (SEVERITY_RANK[alert.severity] > SEVERITY_RANK[group.severity]) group.severity = alert.severity;
    }
  }
  return [...groups.values()];
}

function scopeText(alert: AlertBannerItem): string {
  switch (alert.scope.kind) {
    case 'global':
      return '전체';
    case 'install':
      return alert.scope.installId.slice(0, 8);
    case 'target':
      return alert.scope.targetId.slice(0, 8);
  }
}

function retryText(alert: AlertBannerItem) {
  return alert.retryInSeconds !== null
    ? <span class="muted mono">자동 재시도 {formatDuration(alert.retryInSeconds)} 뒤</span>
    : null;
}

function GroupRow({group, open, onToggle}: {group: AlertGroup; open: boolean; onToggle: () => void}) {
  const first = group.items[0];
  if (first === undefined) return null;
  const description = describeAlertCode(group.code);
  const many = group.items.length > 1;
  return (
    <div class="alert-group">
      <div class="alert-row">
        <Badge tone={TONE[group.severity]} title={safeText(group.code)}>
          {description === null ? safeText(group.code) : group.severity === 'error' ? '오류' : group.severity === 'warn' ? '경고' : '안내'}
        </Badge>
        <span>{description ?? safeText(first.message)}</span>
        {description !== null ? <span class="alert-code mono" title="원시 코드">{safeText(group.code)}</span> : null}
        {many ? <Badge tone="muted" mono>×{group.items.length}</Badge> : retryText(first)}
        {many
          ? (
            <button type="button" class="btn btn-sm" aria-expanded={open} onClick={onToggle}>
              {open ? '접기' : '펼치기'}
            </button>
          )
          : null}
      </div>
      {many && open
        ? (
          <ul class="alert-items">
            {group.items.map((alert, index) => (
              <li key={`${scopeText(alert)}-${index}`}>
                <span class="mono muted">{safeText(scopeText(alert))}</span>
                <span>{safeText(alert.message)}</span>
                {retryText(alert)}
              </li>
            ))}
          </ul>
        )
        : null}
    </div>
  );
}

interface Props {
  alerts: readonly AlertBannerItem[];
  snapshot: SnapshotMeta | null;
  sse: SseStatus;
  onReconcile?: () => void;
  reconciling?: boolean;
}

export function AlertBanner({alerts, snapshot, sse, onReconcile, reconciling = false}: Props) {
  const [open, setOpen] = useState<ReadonlySet<string>>(new Set());
  const snapshotNote = snapshot === null ? undefined : SNAPSHOT_NOTE[snapshot.state];
  const covered = snapshot === null ? undefined : COVERED_BY_SNAPSHOT[snapshot.state];
  const visible = covered === undefined ? alerts : alerts.filter(alert => alert.code !== covered);
  const groups = groupAlerts(visible);
  const draining = sse === 'draining';
  if (groups.length === 0 && snapshotNote === undefined && !draining && snapshot?.truncated !== true) return null;
  const worst = visible.some(alert => alert.severity === 'error') || draining || snapshot?.state === 'recovery_required'
    ? 'error'
    : 'warn';
  const toggle = (code: string) => {
    const next = new Set(open);
    if (next.has(code)) next.delete(code);
    else next.add(code);
    setOpen(next);
  };
  const canRescan = onReconcile !== undefined && (snapshot?.state === 'stale' || snapshot?.state === 'absent');
  return (
    <section class={`alert-banner alert-${worst}`} aria-label="경고">
      {draining
        ? <div class="alert-row"><Badge tone="red" title="server_draining">서버 종료 중</Badge><span>서버가 종료 중입니다. 다시 열려면 MemVane UI를 새로 실행하세요.</span></div>
        : null}
      {snapshotNote !== undefined
        ? (
          <div class="alert-row">
            <Badge tone={snapshotNote.tone} title={snapshot?.state ?? ''}>등록 정보</Badge>
            <span>{snapshotNote.text}</span>
            {canRescan
              ? (
                <button type="button" class="btn btn-sm" onClick={onReconcile} disabled={reconciling}>
                  {reconciling ? '재스캔 중…' : '상태 재스캔'}
                </button>
              )
              : null}
          </div>
        )
        : null}
      {snapshot?.truncated === true
        ? <div class="alert-row"><Badge tone="amber" title="truncated">잘림</Badge><span>체크아웃이 {snapshot.maxRows}행 상한에서 잘렸습니다.</span></div>
        : null}
      {groups.map(group => (
        <GroupRow key={group.code} group={group} open={open.has(group.code)} onToggle={() => toggle(group.code)} />
      ))}
    </section>
  );
}
