---
name: ui
description: Use when the user asks to open, close, or configure the MemVane web dashboard — e.g. "memvane 대시보드 열어줘", "모니터링 웹 열어줘", "memvane 위키 브라우저 열어줘", "memvane recall 테스트 화면 열어줘", "memvane 대시보드 닫아줘", "세션 시작할 때 memvane 대시보드 자동으로 열어줘", "자동 열기 꺼줘", "open the memvane dashboard". Starts or reuses one local read-only dashboard server on 127.0.0.1 and opens it in the browser.
---

# UI

Open the MemVane local web dashboard: one window for every onboarded project and its linked worktrees. The server listens on `127.0.0.1` only, serves read-only views, and stops by itself when no dashboard tab has been open for the idle time (30 minutes by default).

## Workflow

1. Resolve the plugin root (the wrapper derives the core path from its own location):

   ```bash
   ROOT="${CLAUDE_PLUGIN_ROOT:-${PLUGIN_ROOT:-$(git rev-parse --show-toplevel)}}"
   ```

2. Run the command that fits the request:

   ```bash
   python3 -B "$ROOT/skills/ui/scripts/ui.py" open --cwd "$PWD"               # "대시보드 열어줘"
   python3 -B "$ROOT/skills/ui/scripts/ui.py" open --cwd "$PWD" --tab wiki    # "위키 브라우저 열어줘"
   python3 -B "$ROOT/skills/ui/scripts/ui.py" open --cwd "$PWD" --tab recall  # "recall 테스트 화면 열어줘"
   python3 -B "$ROOT/skills/ui/scripts/ui.py" stop                            # "대시보드 닫아줘"
   python3 -B "$ROOT/skills/ui/scripts/ui.py" pref --auto-open on             # "세션 시작할 때 자동으로 열어줘"
   python3 -B "$ROOT/skills/ui/scripts/ui.py" pref --auto-open off            # "자동 열기 꺼줘"
   python3 -B "$ROOT/skills/ui/scripts/ui.py" status                          # is it running, which port
   ```

3. Report from the JSON on stdout:
   - `opened: true`: say the dashboard opened and give `url`. The `url` carries no access code; never add one.
   - `opened: false`: give `manualUrl` and say it works once and only for 120 seconds. `reason` says why the browser was not opened (`ssh`, `no_display`, `sandbox`, `headless`, `open_failed`). Over SSH, give the `hint` port-forwarding command. When a host sandbox blocked the start, ask the user to approve running the command outside the sandbox or to run it in their own terminal.
   - `status: error`: report `error` as is.

## Rules

- Do not paste `manualUrl` into files, commits or logs; it is a one-time login link.
- The dashboard only reads. Index, wiki and learning changes stay with the other MemVane skills.
- `pref --auto-open on|off` stores the session-start auto-open preference; it is off by default.
