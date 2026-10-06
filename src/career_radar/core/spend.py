"""Per-call spend ledger for paid model calls.

Every transport that talks to a paid API writes one row per call: model,
provider, tokens, the dollar cost the provider reported, and latency. The
ledger lives in the same SQLite file as the postings (``spend_log`` table), or
in any SQLite file you hand it (the eval command keeps its own).
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS spend_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT NOT NULL,
    provider      TEXT NOT NULL,
    model         TEXT NOT NULL,
    call_type     TEXT NOT NULL,
    key           TEXT,
    input_tokens  INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cost_usd      REAL DEFAULT 0.0,
    latency_ms    REAL DEFAULT 0.0,
    generation_id TEXT
)
"""


class Ledger:
    """Append-only spend ledger over a SQLite connection (or no-op if None)."""

    def __init__(self, conn: sqlite3.Connection | None):
        self.conn = conn
        if conn is not None:
            conn.execute(_SCHEMA)
            conn.commit()

    @classmethod
    def at(cls, path: str | Path) -> "Ledger":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        return cls(conn)

    def record(self, *, provider: str, model: str, call_type: str = "decide",
               key: str | None = None, input_tokens: int = 0, output_tokens: int = 0,
               cost_usd: float = 0.0, latency_ms: float = 0.0,
               generation_id: str | None = None) -> None:
        if self.conn is None:
            return
        try:
            self.conn.execute(
                """INSERT INTO spend_log (ts, provider, model, call_type, key, input_tokens,
                   output_tokens, cost_usd, latency_ms, generation_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (datetime.now(timezone.utc).isoformat(), provider, model, call_type, key,
                 int(input_tokens or 0), int(output_tokens or 0), float(cost_usd or 0.0),
                 float(latency_ms or 0.0), generation_id))
            self.conn.commit()
        except sqlite3.Error as e:  # the ledger must never break scoring
            logger.warning("spend ledger write failed: %s", e)

    def summary(self, since: str | None = None) -> list[dict]:
        """Totals per provider/model, optionally since an ISO date."""
        if self.conn is None:
            return []
        where, args = ("WHERE ts >= ?", (since,)) if since else ("", ())
        rows = self.conn.execute(
            f"""SELECT provider, model, COUNT(*) AS calls,
                       SUM(input_tokens) AS input_tokens, SUM(output_tokens) AS output_tokens,
                       SUM(cost_usd) AS cost_usd, AVG(latency_ms) AS mean_latency_ms
                FROM spend_log {where} GROUP BY provider, model ORDER BY cost_usd DESC""",
            args).fetchall()
        return [dict(zip(("provider", "model", "calls", "input_tokens", "output_tokens",
                          "cost_usd", "mean_latency_ms"), tuple(r))) for r in rows]
