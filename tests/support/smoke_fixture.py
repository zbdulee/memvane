"""The synthetic project of the post-release smoke (docs/release.md, Q1 and Q2).

Built on the operator's machine from this checkout and shipped as a tree: the
smoke machine only unpacks it and runs git. Every raw document has one
verified wiki card written through the card api (``tests/support/project.py``),
so onboarding indexes the cards as they are and no wiki compile runs. A raw
file without a card would start one, so there is none (no README).

``build(out)`` writes the tree and returns the plan: the cards (path, file
sha256, token) and the prompts P0..P4 with the sha256 an event row records and
the ASCII mark a short prompt shows on the screen (the long P2 has none).
``delta(out, plan)`` applies the RS-SYNC change in place: one document and
card added, one changed, one deleted. All content is invented.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import secrets

SCHEMA = 'memvane-smoke-fixture-v1'
NAME = 'smoke-fixture'
GITIGNORE = '.memvane/route.json\n.memvane/selection.json\n.memvane/wiki/.memvane-root\n'
# (slug, title, one invented fact). The token of each card is added at build time.
TOPICS = (
    ('orchard-irrigation', '과수원 관개 순환', '과수원 관개는 동쪽 구역부터 4시간 간격으로 돌린다.'),
    ('lighthouse-lamp', '등대 램프 교대 주기', '등대 램프는 900시간마다 교대하고 예비 램프는 북쪽 창고에 둔다.'),
    ('bakery-proofing', '제빵 반죽 발효 온도', '통밀 반죽은 26도에서 70분 발효한다.'),
    ('tram-schedule', '가상 노면전차 배차표', '가상 노면전차 7번 노선은 평일 아침 12분 간격으로 배차한다.'),
    ('beekeeping-log', '양봉 점검 기록 규칙', '벌통 점검은 맑은 날 오전 10시 이후에 한다.'),
    ('library-shelving', '도서관 서가 배치 규칙', '참고 도서는 서가 A열, 지역 연감은 서가 F열에 둔다.'),
    ('kite-festival', '연날리기 축제 안전 수칙', '연줄은 전선에서 50미터 이상 떨어진 곳에서만 푼다.'),
    ('glacier-survey', '빙하 측량 장비 목록', '빙하 측량 팀은 수신기 두 대와 눈금 막대 여섯 개를 가져간다.'),
    ('pottery-kiln', '도자기 가마 소성 곡선', '초벌 소성은 시간당 100도씩 올려 900도에서 멈춘다.'),
    ('chess-club', '체스 동호회 대국 규정', '동호회 정기전은 25분에 수당 10초 증가 방식이다.'),
    ('aquarium-water', '수족관 수질 점검표', '열대어 수조의 질산염은 20ppm 아래로 유지한다.'),
    ('bicycle-repair', '자전거 정비 순서', '정비는 타이어 공기압, 브레이크 패드, 체인 윤활 순서로 한다.'),
    ('weather-balloon', '기상 관측 풍선 발사 절차', '관측 풍선은 발사 30분 전에 라디오존데를 켠다.'),
    ('herb-garden', '허브 정원 파종 달력', '바질은 5월 둘째 주, 고수는 9월 첫째 주에 파종한다.'),
    ('choir-rehearsal', '합창단 연습 진행 순서', '연습은 호흡, 발성, 파트 연습, 전체 합창 순서다.'),
    ('telescope-care', '망원경 보관 수칙', '반사 망원경은 경통을 수평으로 눕혀 보관한다.'),
    ('canoe-route', '카누 하천 코스 안내', '가상 하천 B코스는 보 두 곳에서 배를 들어 옮긴다.'),
    ('cheese-aging', '치즈 숙성실 관리', '경성 치즈는 12도, 습도 85%에서 숙성한다.'),
    ('puppet-theater', '인형극 무대 설치 순서', '인형극 무대는 바닥 틀, 뒷막, 조명 바 순서로 세운다.'),
    ('mushroom-foraging', '버섯 채집 기록 방식', '채집한 버섯은 위치, 기주 나무, 갓 지름을 기록한다.'),
)
ADDED = ('sundial-garden', '해시계 정원 관리', '해시계 정원의 눈금판은 춘분마다 방위를 다시 맞춘다.')
NO_TOOLS = '도구는 쓰지 말고 답만 해 줘. '
# Roles of the cards a run targets; each prompt of one session names a different card.
ROLES = ('short', 'long', 'edit', 'changed', 'deleted')
LONG_PROMPT_CHARS = 12000


def prompt_sha(text):
    """The ``prompt.sha256`` an event row records for ``text``."""
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _document(title, fact, token):
    return '# %s\n\n%s\n\n확인 토큰: %s\n' % (title, fact, token)


def _write(project, slug, title, fact, token):
    """One raw document and its verified card; the card's plan entry."""
    note = project.write_note('docs/%s.md' % slug, _document(title, fact, token))
    path = project.write_card('concepts/%s.md' % slug, title=title,
                              body='\n%s\n\n확인 토큰: %s\n' % (fact, token), sources=[note])
    return {'slug': slug, 'token': token, 'source': 'docs/%s.md' % slug,
            'file': path.relative_to(project.root).as_posix(),
            'path': '%s/concepts/%s.md' % (project.wiki_name, slug),
            'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}


def _question(*tokens):
    names = ', '.join(tokens)
    return NO_TOOLS + '확인 토큰 %s이 적힌 문서는 무엇에 관한 문서야?' % names


def _long_prompt(token):
    """About 12,000 characters of invented filler, then the question."""
    line = '이 문단은 길이를 채우기 위한 가상의 회의록 한 줄이고 어떤 문서의 내용과도 관련이 없다. '
    filler = line * (LONG_PROMPT_CHARS // len(line) + 1)
    return filler[:LONG_PROMPT_CHARS] + ' ' + _question(token)


def _prompts(roles, run_id):
    """P0..P3. Every text carries a token of this run, so its hash and its mark on
    the screen belong to this run and to no earlier one on the same account."""
    edit = roles['edit']
    marks = {'P0': 'SMK%sP0' % run_id, 'P1': roles['short']['token'], 'P3': 'SMK%sP3' % run_id}
    paragraph = '추가 점검 메모 %s: 이 문단은 smoke 편집 확인을 위한 가상의 문장이다.' % marks['P3']
    texts = {
        'P0': '%s 실행: 이 프로젝트 MemVane 온보딩해 줘. 계획을 검토한 뒤 그대로 적용하고 활성화까지 해 줘.'
              % marks['P0'],
        'P1': _question(roles['short']['token']),
        'P2': _long_prompt(roles['long']['token']),
        'P3': 'Edit 도구로 %s 파일 맨 끝에 다음 문단을 그대로 추가해 줘. 다른 파일은 건드리지 마. 문단: %s'
              % (edit['source'], paragraph),
    }
    # One line each: a multiplexer paste may turn a line feed into a carriage
    # return, and the event row must record the hash of exactly this text.
    if any('\n' in text or '\r' in text for text in texts.values()):
        raise ValueError('a smoke prompt is one line')
    return {name: {'text': text, 'sha256': prompt_sha(text), 'chars': len(text),
                   'mark': marks.get(name)}
            for name, text in texts.items()}


def build(out, *, cards=len(TOPICS), learning=False, run_id=None):
    """Write the fixture tree under the new directory ``out``; returns the plan."""
    from tests.support.project import Project
    if not len(ROLES) <= cards <= len(TOPICS):
        raise ValueError('cards must be between %d and %d' % (len(ROLES), len(TOPICS)))
    out = Path(out)
    out.mkdir(parents=True)
    run_id = run_id or secrets.token_hex(3)
    project = Project(out, NAME, [])
    (out / '.memvane').mkdir()
    project.settings_path.write_text(json.dumps(
        {'name': NAME, 'contextLearning': {'capture': bool(learning), 'autoCycle': bool(learning)}},
        ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    (out / '.gitignore').write_text(GITIGNORE, encoding='utf-8')
    listed = [_write(project, slug, title, fact, 'SMK%s%02d' % (run_id.upper(), index))
              for index, (slug, title, fact) in enumerate(TOPICS[:cards])]
    roles = dict(zip(ROLES, listed))
    return {'schema': SCHEMA, 'runId': run_id, 'name': NAME, 'learning': bool(learning),
            'cards': listed, 'roles': {role: card['slug'] for role, card in roles.items()},
            'prompts': _prompts(roles, run_id.upper()), 'delta': None}


def delta(out, plan):
    """Apply the RS-SYNC change under ``out`` and add P4 to ``plan`` (returned)."""
    from tests.support.project import Project
    if plan.get('delta') is not None:
        raise ValueError('the delta is already applied')
    out = Path(out)
    project = Project(out, plan['name'], [])
    by_slug = {card['slug']: card for card in plan['cards']}
    changed, deleted = by_slug[plan['roles']['changed']], by_slug[plan['roles']['deleted']]
    run_id = plan['runId'].upper()
    slug, title, fact = ADDED
    added = _write(project, slug, title, fact, 'SMK%sAD' % run_id)
    topic = next(item for item in TOPICS if item[0] == changed['slug'])
    after = _write(project, topic[0], topic[1], topic[2] + ' 개정판에서 한 줄이 더해졌다.',
                   'SMK%sCH' % run_id)
    for name in (deleted['source'], deleted['file']):
        (out / name).unlink()
    text = _question(added['token'], after['token'], deleted['token'])
    if '\n' in text or '\r' in text:
        raise ValueError('a smoke prompt is one line')
    plan = dict(plan)
    plan['delta'] = {'added': added, 'changed': {'before': changed, 'after': after},
                     'deleted': deleted}
    plan['prompts'] = dict(plan['prompts'], P4={'text': text, 'sha256': prompt_sha(text),
                                                'chars': len(text), 'mark': added['token']})
    return plan
