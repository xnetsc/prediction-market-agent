from __future__ import annotations

import json
import os
import time
from pathlib import Path

from .domain import AccountState


class StateStore:
    def __init__(self, path: Path, starting_capital: float | None):
        self.path = path
        self.starting_capital = starting_capital

    def load(self) -> AccountState:
        if not self.path.exists():
            return AccountState(
                starting_capital=self.starting_capital or 0.0,
                cash=self.starting_capital or 0.0,
            )
        # The figure a new book opens at is what the platform reports it can spend. For a book that
        # already exists that figure is history: the platform's balance moves with every deposit,
        # trade and settlement. Refusing to open a book whose opening figure differs from today's
        # balance - a check left from when capital was typed into configuration - would have kept
        # the robot from starting again after its first trade.
        with self.path.open("r", encoding="utf-8") as handle:
            return AccountState.from_dict(json.load(handle))

    def save(self, state: AccountState) -> None:
        state.updated_at = int(time.time() * 1000)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(
                state.to_dict(), handle, ensure_ascii=False, indent=2, sort_keys=True
            )
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.path)
