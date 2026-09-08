from __future__ import annotations

import json
import os
import time
from pathlib import Path

from .domain import AccountState


class StateStore:
    def __init__(self, path: Path, starting_capital: float):
        self.path = path
        self.starting_capital = starting_capital

    def load(self) -> AccountState:
        if not self.path.exists():
            return AccountState(
                starting_capital=self.starting_capital,
                cash=self.starting_capital,
            )
        with self.path.open("r", encoding="utf-8") as handle:
            state = AccountState.from_dict(json.load(handle))
        if abs(state.starting_capital - self.starting_capital) > 1e-9:
            raise ValueError(
                "Existing state uses a different starting capital; choose a new "
                "PREDICTION_AGENT_STATE_FILE"
            )
        return state

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
