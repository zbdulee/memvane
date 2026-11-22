#!/usr/bin/env python3
"""Render docs/dev/learning-state.md from the learning state model.

    python3 -B scripts/render_learning_state.py           # write the document
    python3 -B scripts/render_learning_state.py --check   # exit 1 when it differs

The document is generated: edit ``memvane/learning/context_learning/state_model.py``
and render again.
"""
from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / 'docs' / 'dev' / 'learning-state.md'
HEAD = '''# 학습 상태 기계

이 문서는 `memvane/learning/context_learning/state_model.py`에서 만든다. 손으로 고치지 않는다.
모델을 고친 뒤 `scripts/render_learning_state.py`로 다시 만든다. 구조 시험이 둘의 일치를 단정한다.

상태 모델은 순수 데이터다. hook과 worker는 전이를 정하려고 이 데이터를 읽지 않는다. 전이를 쓰는
코드는 표의 "쓰는 곳"에 있고, `memvane/learning/tests/statemachine`의 전이표 시험이 표의 전이마다
실제 쓰는 코드를 돌려 끝 상태를 대조한다. 표에 없는 (상태, 사건)은 상태를 바꾸지 않는다.

`(absent)`는 행이 없는 상태다. 종결 상태는 행이 지워질 때만 떠난다. 표는 제품 경로(default
teacher)만 담는다.
'''


def render():
    from memvane.learning.context_learning import state_model
    lines = [HEAD]
    for machine in state_model.MACHINES:
        lines += ['## %s %s' % (machine.name, machine.title), '',
                  '- 저장 위치: %s' % machine.store, '- lock: %s' % machine.lock,
                  '- 쓰는 주체: %s' % machine.owner,
                  '- 상태: ' + ', '.join('`%s`' % state + (' (종결)' if state in machine.terminal else '')
                                         for state in machine.states),
                  '', '| 상태 | 사건 | 다음 | 쓰는 곳 |', '|---|---|---|---|']
        for state in machine.states:
            for event in machine.events:
                if (state, event) in machine.transitions:
                    lines.append('| `%s` | `%s` | `%s` | `%s` |' % (
                        state, event, machine.transitions[(state, event)],
                        machine.where[event].replace('memvane.learning.context_learning.', '')))
        lines.append('')
    kinds = {'retry': '다시 깨움 요청', 'waiting': '입력 대기', 'done': '적용 완료',
             'closed': '사유와 함께 닫힘', 'held': '멈춤(밖의 변화 필요)'}
    lines += ['## worker pass의 결과 상태', '',
              'pass의 결과는 cycle 단계의 결과다. 모든 결과 상태는 아래 다섯 분류 중 하나다.',
              '`retry`는 worker가 다음 wake를 요청하는 상태이고, `held`는 pass 밖의 무엇이 바뀔 때까지',
              '매 pass 같은 답이 나오는 상태라 처음 그렇게 끝난 pass가 `learning.cycle` `held` 행을 남긴다.', '',
              '| 결과 | 분류 | 이벤트 | pass가 남기는 것 |', '|---|---|---|---|']
    for status, (kind, event, leaves) in state_model.CYCLE_RESULTS.items():
        lines.append('| `%s` | %s(%s) | %s | %s |' % (
            status, kind, kinds[kind], '`%s`' % event if event else '—', leaves))
    lines.append('')
    return '\n'.join(lines)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    sys.path.insert(0, str(ROOT))
    text = render()
    if argv == ['--check']:
        current = TARGET.read_text(encoding='utf8') if TARGET.is_file() else ''
        if current != text:
            print('docs/dev/learning-state.md differs from the state model; render it again',
                  file=sys.stderr)
            return 1
        return 0
    if argv:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    TARGET.write_text(text, encoding='utf8')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
