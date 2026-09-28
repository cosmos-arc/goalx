"""回放取数的共享行读取器（review-20260928 票 05：drift/pool 两 replay 同源）。"""

from __future__ import annotations

import sqlite3


def odds_triple(row: sqlite3.Row, prefix: str) -> tuple[float, float, float] | None:
    """行 → (h, d, a) 三向赔率；任一缺/退化（≤1）整组不可用返回 None。"""
    raw = (row[f"{prefix}_home"], row[f"{prefix}_draw"], row[f"{prefix}_away"])
    odds: list[float] = []
    for value in raw:
        if value is None or float(value) <= 1.0:
            return None
        odds.append(float(value))
    home, draw, away = odds
    return home, draw, away
