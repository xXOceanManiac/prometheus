"""
main.py — Prometheus entry point.

systemd (prometheus.service) runs this file. The runtime lives in
prometheus/core/main.py; everything here must stay a thin launcher.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from prometheus.core.main import amain

if __name__ == "__main__":
    asyncio.run(amain())
