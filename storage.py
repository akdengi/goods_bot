"""JSON state storage."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any, Dict


DEFAULT_STATE: Dict[str, Any] = {
    "admins": [],
    "threshold": 50.0,
    "tracked": {},
    "last_check": None,
    "last_check_summary": None,
}


class Storage:
    def __init__(self, path: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()

    async def load(self) -> Dict[str, Any]:
        async with self._lock:
            return self._read_sync()

    async def save(self, state: Dict[str, Any]) -> None:
        async with self._lock:
            self._write_sync(state)

    def _read_sync(self) -> Dict[str, Any]:
        if not self.path.exists():
            return json.loads(json.dumps(DEFAULT_STATE))
        try:
            with self.path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return json.loads(json.dumps(DEFAULT_STATE))
            # merge defaults
            for k, v in DEFAULT_STATE.items():
                data.setdefault(k, v if not isinstance(v, (dict, list)) else json.loads(json.dumps(v)))
            if not isinstance(data.get("tracked"), dict):
                data["tracked"] = {}
            if not isinstance(data.get("admins"), list):
                data["admins"] = []
            return data
        except Exception:
            return json.loads(json.dumps(DEFAULT_STATE))

    def _write_sync(self, state: Dict[str, Any]) -> None:
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    async def update(self, updater):
        async with self._lock:
            state = self._read_sync()
            new_state = await updater(state) if asyncio.iscoroutinefunction(updater) else updater(state)
            if new_state is None:
                new_state = state
            self._write_sync(new_state)
            return new_state
