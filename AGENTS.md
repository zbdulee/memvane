# AGENTS.md

This file provides guidance to Codex and other coding agents when working with code in this repository.

## 필수 문서

작업 시작 시 [공통 개발 규칙](docs/dev/contexts/working-rules.md)을 반드시 읽는다. 해당 작업에 적용되는 아래 context 문서도 수정 전에 직접 읽는다. 링크 자체가 문서를 자동으로 불러오지는 않는다.

| 작업 경로·주제 | 읽을 문서 |
|---|---|
| `core/laya_checkout_view*`, `laya_blob_*`, `laya_view_*`, journal reader | [view-store](docs/dev/contexts/view-store.md) |
| `core/laya_shadow_*`, `laya_product_sync_worker*` | [migration-sync](docs/dev/contexts/migration-sync.md), [view-store](docs/dev/contexts/view-store.md) |
| `core/laya_*upgrade*`, bundle, 설치 증명 | [install-upgrade](docs/dev/contexts/install-upgrade.md), [installation-naming](docs/dev/contexts/installation-naming.md) |
| `hooks/`, `hermes_adapter/`, host response·route | [host-route](docs/dev/contexts/host-route.md) |
| `core/config.py`, `resolve_paths.py` | [settings-paths](docs/dev/contexts/settings-paths.md) |
| `core/wiki_compile*`, `wiki_extract*`, `extractors/` | [wiki-compile](docs/dev/contexts/wiki-compile.md), [wiki-verify](docs/dev/contexts/wiki-verify.md), [wiki-dedup](docs/dev/contexts/wiki-dedup.md) |
| `core/wiki_verify*`, `wiki_lifecycle*` | [wiki-verify](docs/dev/contexts/wiki-verify.md) |
| `core/wiki_dedup*` | [wiki-dedup](docs/dev/contexts/wiki-dedup.md) |
| `core/runtime_*`, `core/laya_*service*`, `core/context_learning/` | [installation-naming](docs/dev/contexts/installation-naming.md), [migration-sync](docs/dev/contexts/migration-sync.md)와 관련 개발 문서 |
| `core/memvane_registry.py`, `core/laya_external_edits.py` | [host-route](docs/dev/contexts/host-route.md)와 연결된 전용 개발 문서 |
| `skills/` | [skills](docs/dev/contexts/skills.md)와 관련 context |
| 설치·cleanup·이름·경로 | [installation-naming](docs/dev/contexts/installation-naming.md) |
| manifest·릴리스 | [release](docs/dev/contexts/release.md) |

위에 없는 경로도 공통 규칙을 따르고 관련 context와 그 문서가 가리키는 개발 문서를 찾아 읽는다.
