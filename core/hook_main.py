"""Shared fail-safe entrypoint wrapper for hook scripts.

Hooks MUST always exit 0 — a policy "deny"/block decision is expressed as
JSON on stdout, never as a non-zero exit. Any uncaught exception reaching
the process top level turns into a non-zero exit, which the host
(Claude Code / Codex / Hermes / Antigravity) surfaces to the user as a
noisy "hook (failed): exited with code 1".

This is the single fail-open boundary for every Python hook entrypoint:
run `main()`, and on ANY exception return 0 so the hook degrades to a
silent no-op instead of a visible failure. The exception is best-effort
recorded to QMD_RECALL_LOG (file only, never stdout, so it can't pollute
the model context) when that env var is set — preserving the documented
diagnosis path (see CLAUDE.md "빈 출력은 정상 동작일 수 있다").

Rationale for centralizing here rather than per-site try/except: several
entrypoints (recall/posttool/gate/index/compile) call the same config and
filesystem helpers with no guard of their own, and a sandboxed runtime
(e.g. Codex seatbelt workspace-write) can deny filesystem access outside
its allowed roots and raise where an unsandboxed shell would not. Guarding
each call site is easy to miss on the next edit; one wrapper is not.
"""
from __future__ import annotations

import os
import sys
import traceback
import hook_budget


def run(main) -> int:
    """Run main() and ALWAYS return 0.

    main()'s return value is intentionally discarded: every qmd hook main()
    returns 0 on success, and the invariant is that the hook process exits 0
    regardless of what main() returns or raises. We coerce EVERYTHING to 0 --
    a non-zero/negative int return, or ANY exception including a SystemExit
    raised deep in a call tree or a KeyboardInterrupt -- so the hook can never
    surface as "hook (failed): exited with code N" to the host. A deny/block
    decision must be expressed as JSON on stdout, never via exit code. The
    exception (if any) is best-effort logged to QMD_RECALL_LOG.
    """
    try:
        main()
    except hook_budget.HookDeadlineExceeded:
        try:
            path = os.environ.get('QMD_RECALL_LOG')
            if path:
                with open(path, 'a', encoding='utf-8') as handle:
                    handle.write('qmd_hook_deadline_exceeded\n')
        except BaseException:
            pass
    except BaseException:  # noqa: BLE001 — hooks must never crash the host
        try:
            _log_exception()
        except BaseException:  # noqa: BLE001 — logging must not resurrect a failure
            pass
    finally:
        if not os.environ.get('QMD_SANDBOX') and '--sandbox' not in sys.argv:
            try:
                from runtime_work import hook_wake
                hook_wake()
            except Exception:
                pass  # Wake failure must not break host hooks or drop the queue.
    return 0


def _log_exception() -> None:
    path = os.environ.get("QMD_RECALL_LOG")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write("qmd_hook_uncaught_exception\n")
            traceback.print_exc(file=handle)
    except Exception:  # noqa: BLE001 — logging must not itself raise
        pass
