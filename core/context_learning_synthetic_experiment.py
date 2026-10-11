"""Internal reservation entrypoint for reviewed synthetic CLI launchers."""
import json
from pathlib import Path
import sys

from context_learning.synthetic_experiment import evaluate_final, recover_q5_capture, snapshot
from context_learning.synthetic_launcher import (launch_one, repair_planning_world,
                                                 reconcile_planning_world,
                                                 reconcile_continuation)


def main(args):
    try:
        if len(args) == 2 and args[0] == "status":
            result = snapshot(Path(args[1]))
        elif len(args) == 2 and args[0] == "final-evaluate":
            result = evaluate_final(Path(args[1]))
        elif len(args) == 3 and args[0] == "recover-q5-capture":
            result = recover_q5_capture(Path(args[1]), Path(args[2]))
        elif len(args) == 3 and args[0] == "check-q5-capture-recovery":
            result = recover_q5_capture(Path(args[1]), Path(args[2]),
                                        validate_only=True)
        elif len(args) == 3 and args[0] == "repair-world":
            result = repair_planning_world(Path(args[1]), Path(args[2]))
        elif len(args) == 3 and args[0] == "reconcile-world-repair":
            result = reconcile_planning_world(Path(args[1]), Path(args[2]))
        elif len(args) == 4 and args[0] == "reconcile-continuation":
            result = reconcile_continuation(Path(args[1]), Path(args[2]), int(args[3]))
        elif len(args) == 6 and args[0] == "launch":
            result = launch_one(Path(args[1]), Path(args[2]), args[3],
                                int(args[4]), int(args[5]))
        elif len(args) == 6 and args[0] == "recover-query":
            result = launch_one(Path(args[1]), Path(args[2]), "query",
                                int(args[3]), int(args[4]), recovery=int(args[5]))
        elif len(args) == 7 and args[0] == "recover":
            result = launch_one(Path(args[1]), Path(args[2]), args[3],
                                int(args[4]), int(args[5]), recovery=int(args[6]))
        else:
            return 2
    except (OSError, ValueError) as exc:
        result = {"status": "failed", "reason": str(exc)[:80]}
    print(json.dumps(result, sort_keys=True), flush=True)
    return 1 if result.get("status") in ("failed", "already_reserved") else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
