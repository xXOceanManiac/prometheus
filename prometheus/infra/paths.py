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

# Ecosystem — sibling projects live alongside Prometheus_Main
PROMETHEUS_ECOSYSTEM_ROOT = PROJECT_ROOT.parent
LUMEN_ROOT = PROMETHEUS_ECOSYSTEM_ROOT / "Lumen"
LUMEN_OUTBOX_DIR = LUMEN_ROOT / "runtime" / "outbox"
LUMEN_ACCEPTED_DIR = LUMEN_ROOT / "runtime" / "accepted"
LUMEN_REJECTED_DIR = LUMEN_ROOT / "runtime" / "rejected"
LUMEN_ARCHIVE_DIR = LUMEN_ROOT / "runtime" / "archive"
PENDING_LUMEN_DIR = RUNTIME_ROOT / "pending" / "lumen_calendar"
REVIEWED_LUMEN_DIR = RUNTIME_ROOT / "reviewed" / "lumen_calendar"
SECRETS_DIR = RUNTIME_ROOT / "secrets"


def ensure_runtime_dirs() -> None:
    for path in (RUNTIME_ROOT, REPORTS_DIR, WORKSPACE_ROOT, LOGS_DIR):
        path.mkdir(parents=True, exist_ok=True)


def ensure_lumen_ingestion_dirs() -> None:
    for path in (LUMEN_ACCEPTED_DIR, LUMEN_REJECTED_DIR, LUMEN_ARCHIVE_DIR, PENDING_LUMEN_DIR):
        path.mkdir(parents=True, exist_ok=True)


def ensure_lumen_router_dirs() -> None:
    for path in (REVIEWED_LUMEN_DIR,):
        path.mkdir(parents=True, exist_ok=True)
