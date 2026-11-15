# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 필수 문서

@docs/dev/contexts/working-rules.md

작업 시작 시 [공통 개발 규칙](docs/dev/contexts/working-rules.md)을 반드시 읽는다. 해당 작업에 적용되는 아래 context 문서도 수정 전에 직접 읽는다. 링크 자체가 문서를 자동으로 불러오지는 않는다.

| 작업 경로·주제 | 읽을 문서 |
|---|---|
| `memvane/<domain>/` | 해당 도메인의 `README.md` |
| `hooks/`, `hermes_adapter/` | [host](memvane/host/README.md), [route](memvane/route/README.md) |
| 설치와 provider | [install](memvane/install/README.md), [provider](memvane/provider/README.md) |
| `skills/` | [skills](docs/dev/contexts/skills.md) |
| manifest·릴리스 | [release](docs/dev/contexts/release.md) |

위에 없는 경로도 공통 규칙을 따르고 관련 context와 그 문서가 가리키는 개발 문서를 찾아 읽는다.
