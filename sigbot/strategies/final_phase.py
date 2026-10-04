"""
sigbot.strategies.final_phase
-----------------------------
Dynamic tournament end-game policy optimizing for Top-3 finish.

Phases:
- Phase A (Start -> Oct 20): Capital foundation via low-variance riskless arbitrage.
- Phase B (Oct 20 -> Nov 1): Researched directional edge allocation.
- Phase C (Nov 1 -> Nov 4 close):
  * If in / near top 3: minimize variance, lock hedges, protect podium position.
  * If far behind: only declared, size-capped variance bet configured in config.yaml
    (final_gamble: {enabled: false, max_fraction: ...}). Default disabled.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

log = logging.getLogger("sigbot.final_phase")

PHASE_B_START = datetime(2026, 10, 20, 0, 0, tzinfo=timezone.utc)
PHASE_C_START = datetime(2026, 11, 1, 0, 0, tzinfo=timezone.utc)


@dataclass
class FinalPhasePolicy:
    current_rank: int | None = None
    target_rank: int = 3
    final_gamble_enabled: bool = False
    final_gamble_max_fraction: float = 0.20

    def current_phase(self) -> str:
        now = datetime.now(timezone.utc)
        if now < PHASE_B_START:
            return "Phase A (Capital Base / Riskless Arb)"
        elif now < PHASE_C_START:
            return "Phase B (Add Researched Directional)"
        else:
            return "Phase C (Endgame Convexity / Lock)"

    def should_reduce_variance(self) -> bool:
        """True if in Phase C and within striking distance of podium."""
        now = datetime.now(timezone.utc)
        if now >= PHASE_C_START and self.current_rank is not None and self.current_rank <= self.target_rank + 2:
            return True
        return False

    def can_gamble(self) -> bool:
        """Only allowed if explicitly pre-declared and enabled by operator."""
        now = datetime.now(timezone.utc)
        if now >= PHASE_C_START and self.final_gamble_enabled:
            if self.current_rank is None or self.current_rank > self.target_rank:
                return True
        return False
