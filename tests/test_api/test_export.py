# ruff: noqa: F811
"""Black-box tests for section 19: the export route and the CSV and JSON builders."""

import csv
import io
import json
from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.engine import PulkyEngine
from app.services.export_service import csv_safe, export_csv, export_json
from tests.fixtures.mysql import db_session, mysql_url  # noqa: F401
from tests.test_api.test_results import (  # noqa: F401
    HOST_SECRET,
    api,
    assert_no_secrets,
    claims_for,
    db_engine,
    drop_live_room,
    factory,
    guest_id,
    live_room_for,
    make_room,
    norm,
    persist,
    play_game,
    put_live_room,
    secrets_of,
    sign_in,
    stored,
)

HOST_FORBIDDEN = {"detail": "Only the host who created this room can export it."}
NOT_FOUND = {"detail": "Results not found."}
COMPACT = (",", ":")
INJECTORS = [
    '=HYPERLINK("x")',
    "+cmd|' /C calc'",
    "-2+3",
    "@chula",
    "\tTab",
    "\rCarriage",
    "Don Agave",
]


# --------------------------------------------------- csv_safe


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("=SUM(A1)", "'=SUM(A1)"),
        ("@chula", "'@chula"),
        ("+1", "'+1"),
        ("-1", "'-1"),
        ("\tx", "'\tx"),
        ("\rx", "'\rx"),
        ('=HYPERLINK("x")', '\'=HYPERLINK("x")'),
        ("Don Agave", "Don Agave"),
        ("a=b", "a=b"),
        ("", ""),
    ],
)
def test_csv_safe_prefixes_text_cells_that_start_with_a_formula_character(
    raw: str, expected: str
) -> None:
    assert csv_safe(raw) == expected


@pytest.mark.parametrize("number", [-12.5, -0.01, -3, 0, 7, 12.5, 0.0])
def test_fm2_csv_safe_leaves_numbers_as_numbers(number: float) -> None:
    out = csv_safe(number)
    assert isinstance(out, str)
    assert not out.startswith("'")
    assert float(out) == number


# --------------------------------------------------- builders


def parse_csv(text: str) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(text, newline="")))


def expected_columns(engine: PulkyEngine) -> list[str]:
    first = engine.records("P1")[0]
    return ["game_id", "room_code", "alias", "display_name", *first.to_payload()]


def by_key(rows: list[dict[str, Any]]) -> dict[tuple[str, int], dict[str, Any]]:
    return {(r["alias"], int(r["day"])): r for r in rows}


def compact(value: object) -> str:
    return json.dumps(value, separators=COMPACT)


@pytest.fixture(scope="module")
def engine() -> PulkyEngine:
    return play_game()


@pytest.fixture()
def game(
    api: TestClient, factory: Callable[[], Session], engine: PulkyEngine
) -> dict[str, Any]:
    """A persisted game hosted by a signed-in user, plus its room document."""
    room = make_room(engine, host_identity="uid-host-1")
    gid = persist(room, factory)
    return {"id": gid, "room": room, "code": room["room_code"]}


def stored_export(db: Session, gid: str) -> tuple[str, dict[str, Any]]:
    g = stored(db, gid)
    return export_csv(g), export_json(g)


# --------------------------------------------------- content (3.3)


@pytest.mark.dbschema
def test_csv_has_one_row_per_player_per_day_in_section_03_order(
    db_session: Session, game: dict[str, Any], engine: PulkyEngine
) -> None:
    text, _ = stored_export(db_session, game["id"])
    header = next(csv.reader(io.StringIO(text, newline="")))
    assert header == expected_columns(engine)
    rows = parse_csv(text)
    days = len(engine.market_history())
    assert len(rows) == len(engine.aliases) * days == 14
    assert set(by_key(rows)) == {
        (a, d) for a in engine.aliases for d in range(1, days + 1)
    }
    for row in rows:
        assert row["game_id"] == game["id"]
        assert row["room_code"] == game["code"]
        assert row["display_name"] == engine.display_names[row["alias"]]


@pytest.mark.dbschema
def test_csv_cells_follow_the_record_field_by_field(
    db_session: Session, game: dict[str, Any], engine: PulkyEngine
) -> None:
    text, _ = stored_export(db_session, game["id"])
    rows = by_key(parse_csv(text))
    negative_seen = False
    for alias in engine.aliases:
        for rec in engine.records(alias):
            row = rows[(alias, rec.day)]
            payload = rec.to_payload()
            for name, value in payload.items():
                cell = row[name]
                if name in ("peer_buy", "peer_sell"):
                    expected = "" if value is None else compact(value)
                    assert cell == expected, name
                elif name == "peer_trades":
                    assert cell == compact(value), name
                elif name == "conserve_lots":
                    assert cell == ";".join(value), name
                elif isinstance(value, bool):
                    continue  # the spelling of a flag is not in the documents
                elif isinstance(value, str):
                    assert cell == value, name
                else:
                    assert not cell.startswith("'"), (name, cell)
                    assert float(cell) == pytest.approx(value, abs=1e-6), name
                    negative_seen = negative_seen or value < 0
    assert negative_seen  # FM2 is exercised by this game, not vacuous


@pytest.mark.dbschema
def test_fm8_peer_trades_survive_in_the_csv_and_the_json_export(
    db_session: Session, game: dict[str, Any], engine: PulkyEngine
) -> None:
    text, data = stored_export(db_session, game["id"])
    csv_rows = by_key(parse_csv(text))
    json_rows = by_key(data["player_days"])
    traded = 0
    for alias in engine.aliases:
        for rec in engine.records(alias):
            expected = [t.to_payload() for t in rec.peer_trades]
            traded += len(expected)
            assert json.loads(csv_rows[(alias, rec.day)]["peer_trades"]) == expected
            assert norm(json_rows[(alias, rec.day)]["peer_trades"]) == expected
            jb, js = (
                json_rows[(alias, rec.day)]["peer_buy"],
                json_rows[(alias, rec.day)]["peer_sell"],
            )
            for got, want in ((jb, rec.peer_buy), (js, rec.peer_sell)):
                if want is None:
                    assert got is None
                else:
                    assert isinstance(got, dict) and norm(got) == want.to_payload()
    assert traded >= 2  # a buy and a sell


@pytest.mark.dbschema
def test_worked_example_day_without_limits_has_empty_cells_and_empty_list(
    db_session: Session, game: dict[str, Any]
) -> None:
    text, _ = stored_export(db_session, game["id"])
    row = by_key(parse_csv(text))[("P2", 1)]
    assert row["peer_buy"] == "" and row["peer_sell"] == ""
    assert row["peer_trades"] == "[]"


@pytest.mark.dbschema
def test_json_export_structure(
    db_session: Session, game: dict[str, Any], engine: PulkyEngine
) -> None:
    _, data = stored_export(db_session, game["id"])
    assert set(data) == {"game", "participants", "player_days", "market_days"}
    assert norm(data["game"]["config"]) == game["room"]["config"]
    assert {p["alias"] for p in data["participants"]} == set(engine.aliases)
    leaderboard = {r["alias"]: r for r in norm(engine.results())["leaderboard"]}
    settlements = engine.settlements()
    for p in data["participants"]:
        assert set(p) == {
            "alias",
            "display_name",
            "final_rank",
            "total_profit",
            "final_settlement",
        }
        row = leaderboard[p["alias"]]
        assert p["display_name"] == row["display_name"]
        assert p["final_rank"] == row["rank"]
        assert p["total_profit"] == pytest.approx(row["total_profit"], abs=1e-6)
        assert norm(p["final_settlement"]) == norm(settlements[p["alias"]].to_payload())
    assert len(data["player_days"]) == 14
    assert norm(data["market_days"]) == norm(
        [m.to_payload() for m in engine.market_history()]
    )
    # JSON rows carry the same columns as the CSV
    assert set(data["player_days"][0]) == set(expected_columns(engine))


@pytest.mark.dbschema
def test_ac3_the_builders_carry_no_identity(
    db_session: Session, game: dict[str, Any]
) -> None:
    text, data = stored_export(db_session, game["id"])
    secrets = secrets_of(game["room"])
    assert_no_secrets(text, secrets)
    assert_no_secrets(data, secrets)
    header = text.splitlines()[0]
    for name in ("identity", "user_id", "email", "uid", "token", "secret"):
        assert name not in header
    assert "email" not in json.dumps(data)


# --------------------------------------------------- injection (4)


@pytest.mark.dbschema
def test_fm1_formula_injection_in_a_pulqueria_name(
    api: TestClient, factory: Callable[[], Session], db_session: Session
) -> None:
    eng = play_game(players=7, names=INJECTORS)
    gid = persist(make_room(eng, code="INJ777"), factory)
    text, _ = stored_export(db_session, gid)
    names = {r["alias"]: r["display_name"] for r in parse_csv(text)}
    assert names["P1"] == '\'=HYPERLINK("x")'
    assert names["P2"] == "'+cmd|' /C calc'"
    assert names["P3"] == "'-2+3"
    assert names["P4"] == "'@chula"
    assert names["P5"] == "'\tTab"
    assert names["P6"] == "'\rCarriage"
    assert names["P7"] == "Don Agave"
    for row in parse_csv(text):
        assert row["display_name"][:1] not in ("=", "+", "-", "@", "\t", "\r")
    # and through the route, as the host of the live room
    room = make_room(eng, code="INJ777")
    put_live_room(live_room_for(gid, room, secret="live-secret-INJ"))
    res = api.get(
        "/api/v1/games/INJ777/export?format=csv",
        headers={"X-Host-Secret": "live-secret-INJ"},
    )
    assert res.status_code == 200
    assert {r["alias"]: r["display_name"] for r in parse_csv(res.text)} == names


# --------------------------------------------------- access (3.2)


def url(code: str, fmt: str = "csv") -> str:
    return f"/api/v1/games/{code}/export?format={fmt}"


@pytest.mark.dbschema
def test_valid_secret_of_the_live_room_exports_both_formats(
    api: TestClient, game: dict[str, Any], engine: PulkyEngine
) -> None:
    put_live_room(live_room_for(game["id"], game["room"], secret="live-secret-A"))
    headers = {"X-Host-Secret": "live-secret-A"}
    res = api.get(url(game["code"]), headers=headers)
    assert res.status_code == 200
    disposition = res.headers["content-disposition"]
    assert disposition.startswith("attachment")
    assert f"pulky-{game['code']}-{game['id'][:8]}.csv" in disposition
    assert len(parse_csv(res.text)) == 14
    res = api.get(url(game["code"], "json"), headers=headers)
    assert res.status_code == 200
    assert res.headers["content-disposition"].startswith("attachment")
    assert f"pulky-{game['code']}-{game['id'][:8]}.json" in (
        res.headers["content-disposition"]
    )
    assert set(res.json()) == {"game", "participants", "player_days", "market_days"}
    assert len(res.json()["player_days"]) == 14


@pytest.mark.dbschema
def test_wrong_secret_and_missing_header_are_403_with_the_summary_copy(
    api: TestClient, game: dict[str, Any]
) -> None:
    put_live_room(live_room_for(game["id"], game["room"], secret="live-secret-A"))
    wrong = api.get(url(game["code"]), headers={"X-Host-Secret": "wrong"})
    assert wrong.status_code == 403 and wrong.json() == HOST_FORBIDDEN
    missing = api.get(url(game["code"]))
    assert missing.status_code == 403 and missing.json() == HOST_FORBIDDEN


@pytest.mark.dbschema
def test_host_bearer_exports_with_a_live_room_and_after_the_room_expired(
    api: TestClient, game: dict[str, Any], firebase_tokens: dict[str, Any]
) -> None:
    bearer = sign_in(firebase_tokens, "uid-host-1")
    # the room is still live, and the bearer carries no secret
    put_live_room(live_room_for(game["id"], game["room"], secret="live-secret-A"))
    assert api.get(url(game["code"]), headers=bearer).status_code == 200
    # the room has expired (24 h): the host's account still exports
    drop_live_room(game["code"])
    res = api.get(url(game["code"], "json"), headers=bearer)
    assert res.status_code == 200
    assert len(res.json()["player_days"]) == 14


@pytest.mark.dbschema
def test_other_bearer_and_guest_hosted_game_are_403(
    api: TestClient,
    factory: Callable[[], Session],
    game: dict[str, Any],
    firebase_tokens: dict[str, Any],
    engine: PulkyEngine,
) -> None:
    other = sign_in(firebase_tokens, "uid-someone-else")
    res = api.get(url(game["code"]), headers=other)
    assert res.status_code == 403 and res.json() == HOST_FORBIDDEN
    # a game whose host was a guest has no host user: no bearer exports it
    guest_hosted = make_room(
        engine,
        code="GST234",
        started_at="2026-10-14T09:00:00Z",
        finished_at="2026-10-14T10:00:00Z",
    )
    persist(guest_hosted, factory)
    res = api.get(url("GST234"), headers=sign_in(firebase_tokens, "uid-host-1"))
    assert res.status_code == 403 and res.json() == HOST_FORBIDDEN


@pytest.mark.dbschema
def test_fm6_the_secret_of_a_different_live_room_does_not_export(
    api: TestClient,
    game: dict[str, Any],
    factory: Callable[[], Session],
    engine: PulkyEngine,
) -> None:
    # another live room, with its own valid secret, for a different game
    elsewhere = persist(
        make_room(
            engine,
            code="OTH222",
            started_at="2026-10-14T09:00:00Z",
            finished_at="2026-10-14T10:00:00Z",
        ),
        factory,
    )
    put_live_room(
        live_room_for(elsewhere, game["room"], secret="secret-of-OTH222", code="OTH222")
    )
    res = api.get(url(game["code"]), headers={"X-Host-Secret": "secret-of-OTH222"})
    assert res.status_code == 403 and res.json() == HOST_FORBIDDEN
    # sanity: that secret does export its own game
    own = api.get(url("OTH222"), headers={"X-Host-Secret": "secret-of-OTH222"})
    assert own.status_code == 200


@pytest.mark.dbschema
def test_fm6_a_reused_code_live_room_for_another_game_does_not_export_this_one(
    api: TestClient,
    game: dict[str, Any],
    factory: Callable[[], Session],
    engine: PulkyEngine,
) -> None:
    """Same room code, valid secret, but the live room is a *different* game."""
    newer = persist(
        make_room(
            engine,
            started_at="2026-10-14T09:00:00Z",
            finished_at="2026-10-14T10:00:00Z",
        ),
        factory,
    )
    assert newer != game["id"]
    put_live_room(live_room_for(newer, game["room"], secret="live-secret-NEW"))
    # the export route is keyed by code, so it serves the latest game for it,
    # and the live room (game_id == that latest game) is its host
    ok = api.get(url(game["code"]), headers={"X-Host-Secret": "live-secret-NEW"})
    assert ok.status_code == 200
    assert f"-{newer[:8]}." in ok.headers["content-disposition"]
    # a live room whose game_id is neither: valid secret, still refused
    put_live_room(live_room_for("f" * 32, game["room"], secret="live-secret-NEW"))
    bad = api.get(url(game["code"]), headers={"X-Host-Secret": "live-secret-NEW"})
    assert bad.status_code == 403 and bad.json() == HOST_FORBIDDEN


@pytest.mark.dbschema
def test_unknown_code_is_404_and_another_format_is_422(
    api: TestClient, game: dict[str, Any]
) -> None:
    put_live_room(live_room_for(game["id"], game["room"], secret="live-secret-A"))
    headers = {"X-Host-Secret": "live-secret-A"}
    missing = api.get(url("NOPE99"), headers=headers)
    assert missing.status_code == 404 and missing.json() == NOT_FOUND
    assert api.get(url(game["code"], "xml"), headers=headers).status_code == 422
    assert api.get(url(game["code"], "xlsx"), headers=headers).status_code == 422


@pytest.mark.dbschema
def test_ac3_exported_files_carry_no_identity(
    api: TestClient, game: dict[str, Any]
) -> None:
    put_live_room(live_room_for(game["id"], game["room"], secret="live-secret-A"))
    headers = {"X-Host-Secret": "live-secret-A"}
    secrets = [*secrets_of(game["room"]), "live-secret-A"]
    for fmt in ("csv", "json"):
        res = api.get(url(game["code"], fmt), headers=headers)
        assert res.status_code == 200
        assert_no_secrets(res.text, secrets)
        assert_no_secrets(dict(res.headers), secrets)
    assert guest_id(1) not in api.get(url(game["code"]), headers=headers).text
