"""
Action Verification — observational checks that confirm a tool action actually worked.

Strictly observational: does not mutate state, does not retry, does not call tools.
Takes the execution_result dict and world_snapshot and checks for evidence of success.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class VerificationResult:
    verified: bool
    confidence: float        # 0.0–1.0
    summary: str             # human-readable one-liner
    evidence: list[str]      # observations that support the conclusion
    retry_recommended: bool  # True if verification failed and the action is retryable


def verify_action_result(
    tool_name: str,
    expected_outcome: str,
    execution_result: dict[str, Any],
    world_snapshot: dict[str, Any] | None = None,
) -> VerificationResult:
    """
    Check whether a tool action succeeded.

    Parameters
    ----------
    tool_name:         ACTION_ENUM name of the tool that ran
    expected_outcome:  human-readable description of success
    execution_result:  dict from ToolResult — must have "ok" (bool) and "data" or "message"
    world_snapshot:    optional live machine state from build_world_snapshot()

    Returns VerificationResult. Never raises.
    """
    snap = world_snapshot or {}

    try:
        ok = bool(execution_result.get("ok", False))
        message = str(execution_result.get("message", "") or "")
        data = execution_result.get("data") or {}

        verifier = _VERIFIERS.get(tool_name, _generic_verifier)
        return verifier(ok, message, data, snap, expected_outcome)

    except Exception as exc:
        return VerificationResult(
            verified=False,
            confidence=0.0,
            summary=f"Verification error: {exc}",
            evidence=[],
            retry_recommended=False,
        )


# ── Tool-specific verifiers ───────────────────────────────────────────────────

def _generic_verifier(
    ok: bool, message: str, data: Any, snap: dict, expected: str
) -> VerificationResult:
    if ok:
        return VerificationResult(
            verified=True, confidence=0.75,
            summary=f"Action reported success: {message[:80]}",
            evidence=[f"ok=True", message[:120]] if message else ["ok=True"],
            retry_recommended=False,
        )
    return VerificationResult(
        verified=False, confidence=0.85,
        summary=f"Action reported failure: {message[:80]}",
        evidence=[f"ok=False", message[:120]] if message else ["ok=False"],
        retry_recommended=True,
    )


def _verify_open_app(
    ok: bool, message: str, data: Any, snap: dict, expected: str
) -> VerificationResult:
    evidence: list[str] = []
    app_name = str(data.get("app", "") or "").lower() if isinstance(data, dict) else ""

    if ok:
        evidence.append(f"ToolResult ok=True: {message[:80]}")

    windows: list = snap.get("open_windows") or []
    window_titles = " ".join(str(w) for w in windows).lower()

    if app_name and app_name in window_titles:
        evidence.append(f"'{app_name}' found in open windows")
        return VerificationResult(
            verified=True, confidence=0.95,
            summary=f"App '{app_name}' confirmed open",
            evidence=evidence,
            retry_recommended=False,
        )

    if ok and not snap:
        return VerificationResult(
            verified=True, confidence=0.70,
            summary=f"App launch reported success (no snapshot to cross-check)",
            evidence=evidence,
            retry_recommended=False,
        )

    if ok:
        return VerificationResult(
            verified=True, confidence=0.65,
            summary=f"App launched but not yet visible in window list",
            evidence=evidence + [f"Windows checked: {window_titles[:120]}"],
            retry_recommended=False,
        )

    return VerificationResult(
        verified=False, confidence=0.85,
        summary=f"App launch failed",
        evidence=evidence + [f"ok=False"],
        retry_recommended=True,
    )


def _verify_write_file(
    ok: bool, message: str, data: Any, snap: dict, expected: str
) -> VerificationResult:
    evidence: list[str] = []
    path_str = str(data.get("path", "") or "") if isinstance(data, dict) else ""

    if ok:
        evidence.append(f"ToolResult ok=True")

    if path_str:
        p = Path(path_str)
        if p.exists():
            size = p.stat().st_size
            evidence.append(f"File exists: {path_str} ({size} bytes)")
            return VerificationResult(
                verified=True, confidence=0.98,
                summary=f"File written and confirmed on disk: {p.name}",
                evidence=evidence,
                retry_recommended=False,
            )
        evidence.append(f"File NOT found on disk: {path_str}")

    if ok and not path_str:
        return VerificationResult(
            verified=True, confidence=0.70,
            summary="Write reported success (path not returned for disk check)",
            evidence=evidence,
            retry_recommended=False,
        )

    return VerificationResult(
        verified=False, confidence=0.90,
        summary="File write failed or file not found on disk",
        evidence=evidence,
        retry_recommended=True,
    )


def _verify_screenshot(
    ok: bool, message: str, data: Any, snap: dict, expected: str
) -> VerificationResult:
    evidence: list[str] = []
    path_str = str(data.get("path", "") or "") if isinstance(data, dict) else ""

    if ok:
        evidence.append("ToolResult ok=True")

    if path_str:
        p = Path(path_str)
        if p.exists() and p.stat().st_size > 1000:
            evidence.append(f"Screenshot file exists: {path_str} ({p.stat().st_size} bytes)")
            return VerificationResult(
                verified=True, confidence=0.97,
                summary=f"Screenshot confirmed on disk: {p.name}",
                evidence=evidence,
                retry_recommended=False,
            )

    if ok:
        return VerificationResult(
            verified=True, confidence=0.75,
            summary="Screenshot reported success",
            evidence=evidence,
            retry_recommended=False,
        )

    return VerificationResult(
        verified=False, confidence=0.85,
        summary="Screenshot failed",
        evidence=["ok=False", message[:80]],
        retry_recommended=True,
    )


def _verify_tell_time(
    ok: bool, message: str, data: Any, snap: dict, expected: str
) -> VerificationResult:
    if ok and message:
        return VerificationResult(
            verified=True, confidence=0.99,
            summary=f"Time returned: {message[:40]}",
            evidence=[f"ok=True", f"message: {message[:60]}"],
            retry_recommended=False,
        )
    return VerificationResult(
        verified=False, confidence=0.90,
        summary="tell_time returned no result",
        evidence=["ok=False or empty message"],
        retry_recommended=False,
    )


def _verify_run_shell(
    ok: bool, message: str, data: Any, snap: dict, expected: str
) -> VerificationResult:
    evidence: list[str] = [f"exit_ok={ok}"]
    output = str(data.get("output", "") or "") if isinstance(data, dict) else str(data or "")
    if output:
        evidence.append(f"stdout: {output[:200]}")

    if ok:
        return VerificationResult(
            verified=True, confidence=0.88,
            summary="Shell command exited 0",
            evidence=evidence,
            retry_recommended=False,
        )

    error_hint = output[:120] or message[:120]
    return VerificationResult(
        verified=False, confidence=0.90,
        summary=f"Shell command failed: {error_hint[:80]}",
        evidence=evidence,
        retry_recommended=True,
    )


def _verify_run_python(
    ok: bool, message: str, data: Any, snap: dict, expected: str
) -> VerificationResult:
    return _verify_run_shell(ok, message, data, snap, expected)


def _verify_git_commit(
    ok: bool, message: str, data: Any, snap: dict, expected: str
) -> VerificationResult:
    evidence: list[str] = [f"ok={ok}"]
    output = str(data.get("output", "") or "") if isinstance(data, dict) else str(data or "")

    commit_hash = None
    for line in output.split("\n"):
        if "master" in line or "main" in line or len(line.strip()) == 7:
            commit_hash = line.strip()
            break

    if ok:
        evidence.append(f"Commit output: {output[:120]}")
        if commit_hash:
            evidence.append(f"Commit hash found: {commit_hash}")
        return VerificationResult(
            verified=True, confidence=0.95,
            summary=f"Commit created successfully",
            evidence=evidence,
            retry_recommended=False,
        )

    return VerificationResult(
        verified=False, confidence=0.92,
        summary=f"Commit failed: {message[:60]}",
        evidence=evidence + [message[:120]],
        retry_recommended=False,  # commits should not auto-retry
    )


def _verify_git_status(
    ok: bool, message: str, data: Any, snap: dict, expected: str
) -> VerificationResult:
    output = str(data.get("output", "") or "") if isinstance(data, dict) else str(data or "")
    if ok:
        return VerificationResult(
            verified=True, confidence=0.97,
            summary="Git status returned successfully",
            evidence=[f"ok=True", f"output: {output[:120]}"],
            retry_recommended=False,
        )
    return VerificationResult(
        verified=False, confidence=0.88,
        summary="Git status failed",
        evidence=[f"ok=False", message[:80]],
        retry_recommended=True,
    )


def _verify_list_files(
    ok: bool, message: str, data: Any, snap: dict, expected: str
) -> VerificationResult:
    files: list = []
    if isinstance(data, dict):
        files = data.get("files") or data.get("items") or []
    elif isinstance(data, list):
        files = data

    if ok and files:
        return VerificationResult(
            verified=True, confidence=0.96,
            summary=f"Listed {len(files)} files",
            evidence=[f"ok=True", f"file count: {len(files)}"],
            retry_recommended=False,
        )
    if ok:
        return VerificationResult(
            verified=True, confidence=0.80,
            summary="list_files succeeded (empty or unparsed result)",
            evidence=["ok=True"],
            retry_recommended=False,
        )
    return VerificationResult(
        verified=False, confidence=0.88,
        summary="list_files failed",
        evidence=["ok=False", message[:80]],
        retry_recommended=True,
    )


def _verify_read_file(
    ok: bool, message: str, data: Any, snap: dict, expected: str
) -> VerificationResult:
    content = str(data.get("content", "") or "") if isinstance(data, dict) else str(data or "")
    if ok and content:
        return VerificationResult(
            verified=True, confidence=0.97,
            summary=f"File read: {len(content)} chars",
            evidence=[f"ok=True", f"content length: {len(content)}"],
            retry_recommended=False,
        )
    if ok:
        return VerificationResult(
            verified=True, confidence=0.72,
            summary="read_file ok but content empty or unstructured",
            evidence=["ok=True", f"raw: {message[:80]}"],
            retry_recommended=False,
        )
    return VerificationResult(
        verified=False, confidence=0.90,
        summary=f"read_file failed: {message[:60]}",
        evidence=["ok=False", message[:80]],
        retry_recommended=True,
    )


def _verify_mission_update(
    ok: bool, message: str, data: Any, snap: dict, expected: str
) -> VerificationResult:
    if ok:
        return VerificationResult(
            verified=True, confidence=0.92,
            summary="Mission state updated",
            evidence=["ok=True", message[:80]],
            retry_recommended=False,
        )
    return VerificationResult(
        verified=False, confidence=0.85,
        summary="Mission state update failed",
        evidence=["ok=False", message[:80]],
        retry_recommended=True,
    )


def _verify_web_search(
    ok: bool, message: str, data: Any, snap: dict, expected: str
) -> VerificationResult:
    summary = str(data.get("summary", "") or "") if isinstance(data, dict) else ""
    if ok and summary:
        return VerificationResult(
            verified=True, confidence=0.90,
            summary=f"Web search returned result ({len(summary)} chars)",
            evidence=["ok=True", f"summary length: {len(summary)}"],
            retry_recommended=False,
        )
    if ok:
        return VerificationResult(
            verified=True, confidence=0.65,
            summary="Web search ok but no summary returned",
            evidence=["ok=True"],
            retry_recommended=False,
        )
    return VerificationResult(
        verified=False, confidence=0.80,
        summary="Web search failed",
        evidence=["ok=False", message[:80]],
        retry_recommended=True,
    )


def _verify_ha_script(
    ok: bool, message: str, data: Any, snap: dict, expected: str
) -> VerificationResult:
    if ok:
        return VerificationResult(
            verified=True, confidence=0.88,
            summary="Home Assistant script executed (HTTP success)",
            evidence=["ok=True", message[:80]],
            retry_recommended=False,
        )
    return VerificationResult(
        verified=False, confidence=0.90,
        summary=f"HA script failed: {message[:60]}",
        evidence=["ok=False", message[:80]],
        retry_recommended=True,
    )


def _verify_show_logs(
    ok: bool, message: str, data: Any, snap: dict, expected: str
) -> VerificationResult:
    entries: list = []
    if isinstance(data, dict):
        entries = data.get("entries") or data.get("lines") or []
    elif isinstance(data, list):
        entries = data

    if ok:
        return VerificationResult(
            verified=True, confidence=0.95,
            summary=f"Logs returned ({len(entries)} entries)",
            evidence=["ok=True", f"entry count: {len(entries)}"],
            retry_recommended=False,
        )
    return VerificationResult(
        verified=False, confidence=0.80,
        summary="show_logs failed",
        evidence=["ok=False", message[:80]],
        retry_recommended=True,
    )


def _verify_background_task(
    ok: bool, message: str, data: Any, snap: dict, expected: str
) -> VerificationResult:
    task_id = str(data.get("task_id", "") or "") if isinstance(data, dict) else ""
    if ok and task_id:
        return VerificationResult(
            verified=True, confidence=0.93,
            summary=f"Background task submitted: {task_id}",
            evidence=["ok=True", f"task_id={task_id}"],
            retry_recommended=False,
        )
    if ok:
        return VerificationResult(
            verified=True, confidence=0.72,
            summary="Background task submitted (no task_id returned)",
            evidence=["ok=True"],
            retry_recommended=False,
        )
    return VerificationResult(
        verified=False, confidence=0.88,
        summary="Background task submission failed",
        evidence=["ok=False", message[:80]],
        retry_recommended=True,
    )


# ── Verifier dispatch table ───────────────────────────────────────────────────

_VERIFIERS = {
    "open_app":          _verify_open_app,
    "close_app":         _generic_verifier,
    "open_code_folder":  _verify_open_app,
    "write_file":        _verify_write_file,
    "screenshot":        _verify_screenshot,
    "tell_time":         _verify_tell_time,
    "run_shell":         _verify_run_shell,
    "run_python":        _verify_run_python,
    "git_commit":        _verify_git_commit,
    "git_status":        _verify_git_status,
    "git_diff":          _verify_git_status,
    "list_files":        _verify_list_files,
    "read_file":         _verify_read_file,
    "web_search":        _verify_web_search,
    "run_ha_script":     _verify_ha_script,
    "show_logs":         _verify_show_logs,
    "background_task":   _verify_background_task,
    "get_mission_status": _verify_mission_update,
    "set_mission":       _verify_mission_update,
    "add_subtask":       _verify_mission_update,
    "complete_subtask":  _verify_mission_update,
    "run_diagnostics":   _generic_verifier,
    "search_codebase":   _verify_show_logs,
    "list_windows":      _verify_show_logs,
    "system_status":     _generic_verifier,
    "query_vault":       _verify_web_search,
    "start_coding_task": _verify_background_task,
    "start_build":       _verify_background_task,
}
