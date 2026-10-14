"""CSV and JSON export of a stored game (spec 13.4, 13.8)."""

import csv
import io
import json
from typing import Any

from app.services.db_service import StoredGame
from app.services.results_service import iso_utc, results_response

_FORMULA_STARTS = ("=", "+", "-", "@", "\t", "\r")
_LEADING = ("game_id", "room_code", "alias", "display_name")
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
                "display_name": game.display_names[alias],
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
            "host_display_name": game.host_display_name,
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
                "display_name": game.display_names[alias],
                "final_rank": players[alias]["rank"],
                "total_profit": players[alias]["total_profit"],
                "final_settlement": players[alias]["final_settlement"],
            }
            for alias in game.records
        ],
        "player_days": _player_day_rows(game),
        "market_days": [day.to_payload() for day in game.market],
    }
