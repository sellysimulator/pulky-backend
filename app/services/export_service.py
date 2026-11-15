"""CSV and JSON export of a stored game (spec 13.4, 13.8).

The files name every pulquería by its alias only, ``P<N>`` by seat (the order it
joined): no display name, the host's included, so a file passed around names no one.
Counterparties in ``peer_trades`` are aliases already. The results screen keeps the
names.

``export_file`` keeps the last ``CACHE_SIZE`` files built, in this process only: a
finished game never changes, so a file never expires, and the cache is empty after a
restart and not shared between instances.
"""

import csv
import io
import json
import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Literal

from app.services.db_service import StoredGame
from app.services.results_service import iso_utc, results_response

ExportFormat = Literal["csv", "json"]

_FORMULA_STARTS = ("=", "+", "-", "@", "\t", "\r")
_LEADING = ("game_id", "room_code", "alias")
_JSON_CELLS = ("peer_buy", "peer_sell", "peer_trades")


def csv_safe(value: object) -> str:
    """A CSV cell: text starting like a formula gets a ``'``, numbers stay numbers."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    text = str(value)
    return "'" + text if text.startswith(_FORMULA_STARTS) else text


def _compact(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"))


def _player_day_rows(game: StoredGame) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for alias, records in game.records.items():
        for record in records:
            row: dict[str, Any] = {
                "game_id": game.game_id,
                "room_code": game.room_code,
                "alias": alias,
            }
            row.update(record.to_payload())
            rows.append(row)
    return rows


def _csv_cell(name: str, value: Any) -> str:
    if name == "conserve_lots":
        return csv_safe(";".join(value))
    if name in _JSON_CELLS:
        return "" if value is None else _compact(value)
    return csv_safe(value)


def export_csv(game: StoredGame) -> str:
    rows = _player_day_rows(game)
    columns = list(rows[0]) if rows else list(_LEADING)
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerow(columns)
    for row in rows:
        writer.writerow([_csv_cell(name, row[name]) for name in columns])
    return out.getvalue()


def export_json(game: StoredGame) -> dict:
    summary = results_response(game)
    players = summary["stats"]["players"]
    return {
        "game": {
            "game_id": game.game_id,
            "room_code": game.room_code,
            "duration_days": game.duration_days,
            "days_played": game.days_played,
            "ended_early": game.ended_early,
            "started_at": iso_utc(game.started_at),
            "finished_at": iso_utc(game.finished_at),
            "config": game.config,
        },
        "participants": [
            {
                "alias": alias,
                "final_rank": players[alias]["rank"],
                "total_profit": players[alias]["total_profit"],
                "final_settlement": players[alias]["final_settlement"],
            }
            for alias in game.records
        ],
        "player_days": _player_day_rows(game),
        "market_days": [day.to_payload() for day in game.market],
    }


# ---- the files, cached


@dataclass(frozen=True)
class ExportFile:
    content: bytes
    media_type: str
    filename: str


CACHE_SIZE = 64
_cache: OrderedDict[tuple[str, str], ExportFile] = OrderedDict()
_cache_lock = threading.Lock()


def build_file(game: StoredGame, fmt: ExportFormat) -> ExportFile:
    name = f"pulky-{game.room_code}-{game.game_id[:8]}.{fmt}"
    if fmt == "csv":
        return ExportFile(
            export_csv(game).encode("utf-8"), "text/csv; charset=utf-8", name
        )
    return ExportFile(json.dumps(export_json(game)).encode(), "application/json", name)


def cached_file(public_id: str, fmt: ExportFormat) -> ExportFile | None:
    """The file built earlier for this game and format, now the most recent."""
    with _cache_lock:
        file = _cache.get((public_id, fmt))
        if file is not None:
            _cache.move_to_end((public_id, fmt))
        return file


def remember_file(public_id: str, fmt: ExportFormat, file: ExportFile) -> None:
    """Keep a built file; past ``CACHE_SIZE`` the least recently used goes."""
    with _cache_lock:
        _cache[(public_id, fmt)] = file
        _cache.move_to_end((public_id, fmt))
        while len(_cache) > CACHE_SIZE:
            _cache.popitem(last=False)


def clear_cache() -> None:
    """Tests only: forget every cached file."""
    with _cache_lock:
        _cache.clear()
