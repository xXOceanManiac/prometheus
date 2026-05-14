from pathlib import Path

from prometheus.infra.paths import WORKSPACE_ROOT


def resolve_workspace_path(raw_path: str | None) -> Path:
    if not raw_path:
        raise ValueError("Missing path")
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        candidate = WORKSPACE_ROOT / candidate
    resolved = candidate.resolve()
    root = WORKSPACE_ROOT.resolve()
    if resolved != root and root not in resolved.parents:
        raise PermissionError(f"Path outside workspace is not allowed: {resolved}")
    return resolved


def ensure_workspace_root() -> Path:
    WORKSPACE_ROOT.mkdir(parents=True, exist_ok=True)
    return WORKSPACE_ROOT
