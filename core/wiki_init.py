"""Project wiki directories shared by direct initialization and recommended opt-in.

The wiki holds only cards: this module creates the wiki root and the type
directories, never root files. Card-writing rules ship in the bundle
(`core/wiki_card_rules`), the card catalog is the memvane-db index, and the
activity log lives in the install state root (`wiki_activity_log`).

Both callers run this function in their own process. A caller may already hold
wiki_mutation_lock.lock(target); the nested acquisition is thread-reentrant.
"""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile

import config as qmd_config
import wiki_mutation_lock


def init_wiki(target, preset="default", *, quiet=False, initial_config=None):
    target = Path(target).resolve()
    settings_dir = target / ".auto-context"
    settings = settings_dir / "settings.json"
    with wiki_mutation_lock.lock(target):

        def ensure_settings_dir() -> None:
            if settings_dir.exists():
                if settings_dir.is_symlink() or not settings_dir.is_dir():
                    print(f"[qmd] unsafe .auto-context path: {settings_dir}", file=sys.stderr)
                    sys.exit(1)
            else:
                settings_dir.mkdir(parents=True, exist_ok=False)
            try:
                resolved = settings_dir.resolve()
                resolved.relative_to(target)
            except (OSError, ValueError):
                print(f"[qmd] unsafe .auto-context path: {settings_dir}", file=sys.stderr)
                sys.exit(1)
            if resolved != settings_dir:
                print(f"[qmd] unsafe .auto-context path: {settings_dir}", file=sys.stderr)
                sys.exit(1)

        if settings.is_symlink():
            print(f"[qmd] unsafe settings.json path: {settings}", file=sys.stderr)
            sys.exit(1)
        if initial_config is not None:
            if settings.exists() or not isinstance(initial_config, dict):
                raise ValueError("recommended_settings_conflict")
            config = copy.deepcopy(initial_config)
        elif settings.exists():
            try:
                config = json.loads(settings.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                print(f"[qmd] invalid settings.json preserved: {settings}: {exc}", file=sys.stderr)
                sys.exit(1)
            if not isinstance(config, dict):
                print(f"[qmd] invalid settings.json preserved: {settings}: expected object", file=sys.stderr)
                sys.exit(1)
        else:
            config = {}

        ensure_settings_dir()
        wiki = settings_dir / "wiki"

        def ensure_project_dir(path: Path, label: str) -> None:
            if path.exists():
                if path.is_symlink() or not path.is_dir():
                    print(f"[qmd] unsafe {label} path: {path}", file=sys.stderr)
                    sys.exit(1)
            else:
                path.mkdir(parents=False, exist_ok=False)
            try:
                resolved = path.resolve()
                resolved.relative_to(target)
            except (OSError, ValueError):
                print(f"[qmd] unsafe {label} path: {path}", file=sys.stderr)
                sys.exit(1)
            if resolved != path:
                print(f"[qmd] unsafe {label} path: {path}", file=sys.stderr)
                sys.exit(1)

        # scaffold는 **자동 compile이 실제로 채울 수 있는** 타입 디렉터리만 만든다.
        # extractors/lib.ALLOWED_TYPES(프롬프트가 모델에게 제시하는 집합)가 그 목록이고
        # 현재 concept/entity/decision/comparison 4종이다. `sessions`·`queries`는 그 집합에
        # 없어 자동으로는 영영 비어 있었다(라이브 ai-proxy 실측 0건/0건, service-engineering은
        # 두 디렉터리가 아예 없이 정상 동작). 미리 만들 이유가 없는 이유는 두 가지다 —
        # (1) wiki_compile이 카드를 쓰기 전에 `target.parent.mkdir(parents=True)` 하므로
        #     수동 wiki-compile로 session 카드를 쓰면 그때 생긴다(기능은 그대로다),
        # (2) 빈 디렉터리는 "여기에 뭔가 쌓여야 하는데 안 쌓인다"로 읽혀 오진을 부른다.
        # 즉 여기서 지운 것은 **미리 만드는 것**이지 타입 지원이 아니다 —
        # wiki_compile.ALLOWED_TYPES/TYPE_DIRS의 session·query 항목은 그대로 둔다.
        # 목록이 프롬프트와 갈리지 않는지는 test/update.test.mjs가 코드에서 유도해 단정한다.
        base_dirs = ["concepts", "entities", "decisions", "comparisons"]
        # novel 전용 디렉터리(characters·world·timeline·plot·style)는 compile 워커가 novel
        # 카드 규칙을 고르는 보조 표식이다(주 표식은 루트의 `<숫자>_Manuscript/`,
        # `wiki_compile_worker.card_rules_preset`).
        novel_dirs = ["characters", "world", "timeline", "plot", "style", "discarded", "decisions", "sessions"]
        dir_names = novel_dirs if preset == "novel" else base_dirs
        dirs = [wiki] + [wiki / name for name in dir_names]
        for path in dirs:
            ensure_project_dir(path, "wiki")

        # minimal settings는 wiki collection·compile을 플러그인이 유도한다. 디렉터리만
        # 만들고 settings는 바이트 그대로 둔다(키를 더하면 legacy 파일이 된다).
        if initial_config is None and qmd_config.is_minimal_settings(config):
            if not quiet:
                print(f"[qmd] wiki ready: {wiki}")
            return

        def slug(name: str) -> str:
            cleaned = "".join(ch.lower() if ch.isalnum() else "-" for ch in name).strip("-")
            while "--" in cleaned:
                cleaned = cleaned.replace("--", "-")
            return cleaned or "project"

        wiki_collection = f"{slug(target.name)}-wiki"
        collections = config.get("collections") if isinstance(config.get("collections"), list) else []
        collections = [item for item in collections if isinstance(item, str)]
        if wiki_collection not in collections:
            collections.append(wiki_collection)
        config["collections"] = collections

        collection_paths = config.get("collectionPaths") if isinstance(config.get("collectionPaths"), dict) else {}
        collection_paths = {key: value for key, value in collection_paths.items() if isinstance(key, str) and isinstance(value, str)}
        collection_paths[wiki_collection] = ".auto-context/wiki"
        config["collectionPaths"] = collection_paths

        collection_roles = config.get("collectionRoles") if isinstance(config.get("collectionRoles"), dict) else {}
        collection_roles = {key: value for key, value in collection_roles.items() if isinstance(key, str) and isinstance(value, str)}
        for collection in collections:
            collection_roles.setdefault(collection, "raw")
        collection_roles[wiki_collection] = "wiki"
        config["collectionRoles"] = collection_roles
        # recallStrategy "hierarchical"·wikiPath ".auto-context/wiki"는 DEFAULT_CONFIG 기본값과
        # 같으므로 쓰지 않는다(생성기 delta-only). recallStrategy는 예전에 **대입**이라 기존 값을
        # 강제로 덮었으므로, 안 쓰는 것만으로는 부족하고 키를 지워야 같은 결과가 된다
        # ("키 없음 → 기본값 hierarchical"). wikiPath는 setdefault라 지우면 사용자 커스텀 경로를
        # 파괴하므로 **줄만 없앤다**(없으면 기본값, 있으면 그대로).
        config.pop("recallStrategy", None)
        if preset == "novel":
            compile_config = config.get("compile") if isinstance(config.get("compile"), dict) else {}
            # 기본값과 다른 키만 채운다(생성기 delta-only). 활성화는 `mode` 한 값이 담당한다 —
            # `enabled`·`autoWrite`는 스키마에서 사라졌으므로 쓰면 정규화에서 무시되는 죽은 키다.
            compile_config.setdefault("mode", "auto-wiki")
            # post_session_summary는 host가 compact session summary를 hook에 넘겨줄 때만
            # 자동으로 발화할 수 있고 그런 host가 아직 없다(수동 skills/wiki-compile 경로의
            # 라벨로만 소비된다). 자동 수집을 실제로 담당하는 트리거는 post_tool_source이므로
            # 반드시 포함시킨다 — 없으면 세션 노트를 채워도 카드가 생기지 않는다.
            compile_config.setdefault(
                "triggers", ["post_tool_source", "manual", "explicit_user_approval", "post_session_summary"]
            )
            config["compile"] = compile_config
        if "indexing" not in config:
            config["indexing"] = True

        fd, tmp = tempfile.mkstemp(dir=str(settings_dir), prefix="settings.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(config, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            os.replace(tmp, settings)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

        if not quiet:
            print(f"[qmd] wiki ready: {wiki}")

if __name__ == "__main__":
    init_wiki(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "default")
