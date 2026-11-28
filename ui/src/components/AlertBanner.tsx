import type {AlertBannerItem, AlertSeverity, SnapshotMeta} from '../api/types';
import type {SseStatus} from '../api/sse';
import {formatDuration, safeText} from '../text';
import {Badge, type Tone} from './Badge';

const TONE: Record<AlertSeverity, Tone> = {info: 'blue', warn: 'amber', error: 'red'};

const SNAPSHOT_NOTE: Partial<Record<SnapshotMeta['state'], {tone: Tone; text: string}>> = {
  stale: {tone: 'amber', text: '등록 정보가 오래되었습니다. 상태 재스캔으로 갱신할 수 있습니다.'},
  absent: {tone: 'amber', text: '등록 정보가 없습니다. 온보딩된 프로젝트가 아직 없을 수 있습니다.'},
  busy: {tone: 'amber', text: '다른 작업이 등록 정보를 갱신하는 중입니다. 잠시 뒤 다시 읽습니다.'},
  recovery_required: {tone: 'red', text: '등록 정보 복구가 필요합니다.'},
};

interface Props {
  alerts: readonly AlertBannerItem[];
  snapshot: SnapshotMeta | null;
  sse: SseStatus;
}

export function AlertBanner({alerts, snapshot, sse}: Props) {
  const snapshotNote = snapshot === null ? undefined : SNAPSHOT_NOTE[snapshot.state];
  const draining = sse === 'draining';
  if (alerts.length === 0 && snapshotNote === undefined && !draining && snapshot?.truncated !== true) return null;
  const worst = alerts.some(alert => alert.severity === 'error') || draining || snapshot?.state === 'recovery_required'
    ? 'error'
    : 'warn';
  return (
    <section class={`alert-banner alert-${worst}`} aria-label="경고">
      {draining
        ? <div class="alert-row"><Badge tone="red">server_draining</Badge><span>서버가 종료 중입니다. 다시 열려면 MemVane UI를 새로 실행하세요.</span></div>
        : null}
      {snapshotNote !== undefined
        ? <div class="alert-row"><Badge tone={snapshotNote.tone} mono>{snapshot?.state}</Badge><span>{snapshotNote.text}</span></div>
        : null}
      {snapshot?.truncated === true
        ? <div class="alert-row"><Badge tone="amber" mono>truncated</Badge><span>체크아웃이 {snapshot.maxRows}행 상한에서 잘렸습니다.</span></div>
        : null}
      {alerts.map((alert, index) => (
        <div class="alert-row" key={`${alert.code}-${index}`}>
          <Badge tone={TONE[alert.severity]} mono>{safeText(alert.code)}</Badge>
          <span>{safeText(alert.message)}</span>
          {alert.retryInSeconds !== null
            ? <span class="muted mono">자동 재시도 {formatDuration(alert.retryInSeconds)} 뒤</span>
            : null}
        </div>
      ))}
    </section>
  );
}
