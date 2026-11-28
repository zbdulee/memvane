import type {TabId} from '../api/types';

const COPY: Record<Exclude<TabId, 'overview'>, {title: string; body: string}> = {
  wiki: {
    title: 'Wiki 카드 브라우저',
    body: '선택한 체크아웃의 위키 카드 목록·상세와 원문 나란히 보기가 이 화면에 들어옵니다.',
  },
  recall: {
    title: 'Recall 인스펙터 & Query Playground',
    body: '최근 recall 타임라인, 단계별 지연, 부작용 없는 dry-run 질의가 이 화면에 들어옵니다.',
  },
  runtime: {
    title: 'Runtime & Learning 모니터',
    body: 'resident 서비스, wake 큐, 학습 승격 지표와 디스크 사용량이 이 화면에 들어옵니다.',
  },
};

export function PlaceholderPage({tab}: {tab: Exclude<TabId, 'overview'>}) {
  const copy = COPY[tab];
  return (
    <section class="card placeholder" aria-label={copy.title}>
      <div class="card-title">{copy.title}</div>
      <p class="card-sub">{copy.body}</p>
      <p class="placeholder-note">이 화면은 아직 준비 중입니다. Overview에서 전체 상태를 먼저 확인할 수 있습니다.</p>
    </section>
  );
}
