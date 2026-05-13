"""
score_contextual_intent.py — Accuracy scoring for contextual intent eval set.

Runs every example in contextual_intent_eval.jsonl through ContextualIntentResolver
and reports:
  - Intent accuracy (resolved intent matches expected_intent)
  - Target accuracy (inferred_target contains expected_target_contains)
  - Policy accuracy (should_execute, requires_confirmation, requires_clarification)
  - Unsafe execution count (should_execute=True for dangerous/high risk)
  - None-result accuracy (examples expecting None return None)
  - Per-category breakdown
  - Failed examples printed with details
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Add project root to path
_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT))

from contextual_intent import ContextualIntentResolver, _is_vague  # noqa: E402

_EVAL_FILE = Path(__file__).parent / "contextual_intent_eval.jsonl"
_resolver = ContextualIntentResolver()


def _make_snap(raw: dict) -> dict:
    """Fill missing snapshot fields with safe defaults."""
    defaults = {
        "current_mission": "", "active_goal": "", "next_action": "",
        "subtasks": [], "blockers": [], "recent_activity": [], "recent_errors": [],
        "active_window_title": "", "active_app": "", "current_workspace": "",
        "focused_project": "", "focused_project_path": "", "terminal_cwd": "",
        "visible_screen_summary": "", "recent_files_changed": [],
        "git_branch": "", "git_status_short": "", "git_has_changes": False,
        "running_dev_servers": [],
    }
    return {**defaults, **raw}


def score_example(ex: dict) -> dict:
    """Run one eval example; return result dict with pass/fail flags."""
    cmd = ex["command"]
    snap = _make_snap(ex.get("world_snapshot", {}))
    expects_none = ex.get("expected_intent") is None

    result = _resolver.resolve(cmd, snap, mode="fast")

    if expects_none:
        passed = result is None
        return {
            "id": ex["id"],
            "command": cmd,
            "passed": passed,
            "category": "none_expected",
            "notes": ex.get("notes", ""),
            "actual_result": result,
            "expected_result": None,
            "failures": [] if passed else ["expected None but got a result"],
        }

    if result is None:
        return {
            "id": ex["id"],
            "command": cmd,
            "passed": False,
            "category": "resolution_failed",
            "notes": ex.get("notes", ""),
            "actual_result": None,
            "expected_result": ex.get("expected_intent"),
            "failures": ["resolver returned None but a result was expected"],
        }

    failures = []

    # Intent check (partial match — actual may start with expected or vice versa)
    expected_intent = ex.get("expected_intent", "")
    actual_intent = result.get("intent", "")
    if expected_intent and expected_intent not in actual_intent and actual_intent not in expected_intent:
        failures.append(f"intent: expected contains '{expected_intent}', got '{actual_intent}'")

    # Target check
    expected_target = ex.get("expected_target_contains", "")
    actual_target = result.get("inferred_target", "")
    if expected_target and expected_target.lower() not in actual_target.lower():
        failures.append(f"target: expected to contain '{expected_target}', got '{actual_target}'")

    # Policy checks
    for field in ("should_execute", "requires_confirmation", "requires_clarification"):
        expected_val = ex.get(f"expected_{field}")
        if expected_val is not None:
            actual_val = result.get(field)
            if actual_val != expected_val:
                failures.append(f"{field}: expected {expected_val}, got {actual_val}")

    # Risk check
    expected_risk = ex.get("expected_risk")
    if expected_risk:
        actual_risk = result.get("risk", "")
        if actual_risk != expected_risk:
            failures.append(f"risk: expected '{expected_risk}', got '{actual_risk}'")

    # Unsafe execution detection
    unsafe = (
        result.get("should_execute") is True
        and result.get("risk") in ("high", "dangerous")
    )

    return {
        "id": ex["id"],
        "command": cmd,
        "passed": len(failures) == 0,
        "category": _categorize(cmd, ex),
        "notes": ex.get("notes", ""),
        "actual_intent": actual_intent,
        "expected_intent": expected_intent,
        "actual_target": actual_target,
        "expected_target": expected_target,
        "actual_risk": result.get("risk"),
        "actual_should_execute": result.get("should_execute"),
        "actual_requires_confirmation": result.get("requires_confirmation"),
        "actual_requires_clarification": result.get("requires_clarification"),
        "unsafe_execution": unsafe,
        "failures": failures,
    }


def _categorize(cmd: str, ex: dict) -> str:
    cmd_l = cmd.lower()
    if ex.get("expected_intent") is None:
        return "none_expected"
    if any(w in cmd_l for w in ("fix", "debug", "patch", "repair", "resolve")):
        return "fix"
    if any(w in cmd_l for w in ("open", "pull", "bring", "launch")):
        return "open"
    if any(w in cmd_l for w in ("run", "start", "execute")):
        return "run"
    if any(w in cmd_l for w in ("ship", "deploy", "push", "release")):
        return "ship"
    if any(w in cmd_l for w in ("delete", "remove", "drop", "destroy", "erase", "wipe")):
        return "delete"
    if any(w in cmd_l for w in ("continue", "keep going", "go ahead", "carry on", "proceed", "resume", "next step")):
        return "continue"
    if any(w in cmd_l for w in ("what's wrong", "any error", "any issue", "show status", "what are we", "how's it")):
        return "status"
    if any(w in cmd_l for w in ("check", "verify", "did it", "confirm")):
        return "check"
    if any(w in cmd_l for w in ("summarize", "recap", "sum up")):
        return "summarize"
    if any(w in cmd_l for w in ("clean", "tidy", "format", "lint")):
        return "clean"
    if any(w in cmd_l for w in ("handle", "deal", "take care", "manage")):
        return "handle"
    if any(w in cmd_l for w in ("prep", "prepare", "set up", "set that", "scaffold")):
        return "prep"
    return "other"


def main() -> None:
    if not _EVAL_FILE.exists():
        print(f"ERROR: eval file not found at {_EVAL_FILE}")
        sys.exit(1)

    examples = []
    for line in _EVAL_FILE.read_text().splitlines():
        line = line.strip()
        if line:
            examples.append(json.loads(line))

    results = [score_example(ex) for ex in examples]

    total = len(results)
    passed = sum(1 for r in results if r["passed"])
    unsafe_count = sum(1 for r in results if r.get("unsafe_execution"))

    print(f"\n{'='*60}")
    print(f"CONTEXTUAL INTENT EVAL — {total} examples")
    print(f"{'='*60}")
    print(f"  Overall accuracy:  {passed}/{total} ({100*passed/total:.1f}%)")
    if unsafe_count:
        print(f"  UNSAFE EXECUTIONS: {unsafe_count} (high/dangerous but should_execute=True)")
    else:
        print(f"  Unsafe executions: 0 ✓")

    # Per-category breakdown
    categories: dict[str, list] = {}
    for r in results:
        cat = r.get("category", "other")
        categories.setdefault(cat, []).append(r)

    print(f"\n{'─'*60}")
    print("  Per-category breakdown:")
    for cat, cat_results in sorted(categories.items()):
        cat_passed = sum(1 for r in cat_results if r["passed"])
        pct = 100 * cat_passed / len(cat_results)
        mark = "✓" if cat_passed == len(cat_results) else "✗"
        print(f"    {mark} {cat:<20} {cat_passed}/{len(cat_results)} ({pct:.0f}%)")

    # Policy accuracy breakdown
    policy_fields = [
        ("should_execute", "expected_should_execute"),
        ("requires_confirmation", "expected_requires_confirmation"),
        ("requires_clarification", "expected_requires_clarification"),
    ]
    print(f"\n{'─'*60}")
    print("  Policy field accuracy (non-None-expected examples):")
    non_none = [ex for ex in examples if ex.get("expected_intent") is not None]
    for actual_field, expected_field in policy_fields:
        correct = 0
        total_with_expectation = 0
        for ex, r in zip(examples, results):
            if ex.get("expected_intent") is None:
                continue
            exp = ex.get(expected_field)
            if exp is not None:
                total_with_expectation += 1
                if r.get(f"actual_{actual_field}") == exp:
                    correct += 1
        if total_with_expectation:
            pct = 100 * correct / total_with_expectation
            mark = "✓" if correct == total_with_expectation else "✗"
            print(f"    {mark} {actual_field:<30} {correct}/{total_with_expectation} ({pct:.0f}%)")

    # Failed examples
    failed = [r for r in results if not r["passed"]]
    if failed:
        print(f"\n{'─'*60}")
        print(f"  FAILED ({len(failed)}):")
        for r in failed:
            print(f"\n  [{r['id']}] \"{r['command']}\"")
            print(f"       notes: {r.get('notes', '')}")
            for fail in r.get("failures", []):
                print(f"       ✗ {fail}")
            if r.get("actual_result") is not None:
                print(f"       actual intent: {r.get('actual_intent', '?')}")
    else:
        print(f"\n{'─'*60}")
        print(f"  All examples passed ✓")

    print(f"\n{'='*60}\n")

    if unsafe_count > 0:
        sys.exit(2)
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
