from __future__ import annotations

import numpy as np


class WakeWordDetector:
    """
    Safe no-op wake word detector.

    This keeps the rest of Jarvis production-stable even when a local wake-word
    engine is not installed or configured. Jarvis stays fully usable through PTT,
    and the visual layer can still show an "armed" standby state.
    """

    def __init__(self) -> None:
        self.is_ready = False
        self.error = "Wake word engine disabled. Using push-to-talk only."

    def process(self, chunk: np.ndarray) -> bool:
        return False

    def close(self) -> None:
        return None
