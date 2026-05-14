from pathlib import Path


def find_project_root() -> Path:
    current = Path(__file__).resolve()
    for parent in [current, *current.parents]:
        if (parent / "prometheus").exists() and (parent / "tests").exists():
            return parent
    raise RuntimeError("Could not locate Prometheus project root")


PROJECT_ROOT = find_project_root()
RUNTIME_ROOT = PROJECT_ROOT / "runtime"
REPORTS_DIR = RUNTIME_ROOT / "reports"
WORKSPACE_ROOT = RUNTIME_ROOT / "workspace"
LOGS_DIR = RUNTIME_ROOT / "logs"

JARVIS_STATE_DIR = Path.home() / ".jarvis"


def ensure_runtime_dirs() -> None:
    for path in (RUNTIME_ROOT, REPORTS_DIR, WORKSPACE_ROOT, LOGS_DIR):
        path.mkdir(parents=True, exist_ok=True)
