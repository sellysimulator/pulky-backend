# ruff: noqa: F811
"""Black-box tests for section 19: the export route and the CSV and JSON builders."""

import csv
import io
import json
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core import abuse
from app.core.engine import PulkyEngine
from app.services import db_service, export_service
from app.services.db_service import StoredGame
from app.services.export_service import csv_safe, export_csv, export_json
from app.services.results_service import iso_utc, results_response
from app.services.state_service import reset_state_service
from tests.conftest import contains_key
from tests.fixtures.postgres import db_session, pg_url  # noqa: F401
from tests.test_api.test_results import (  # noqa: F401
    api,
    assert_no_secrets,
    bare_api,
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
    return ["game_id", "room_code", "alias", *first.to_payload()]


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
        assert "display_name" not in row


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
            "final_rank",
            "total_profit",
            "final_settlement",
        }
        row = leaderboard[p["alias"]]
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
def test_fm1_a_formula_in_a_pulqueria_name_never_reaches_the_file(
    api: TestClient, factory: Callable[[], Session], db_session: Session
) -> None:
    """Names are not exported at all (P<N> only), so no name can inject a formula."""
    eng = play_game(players=7, names=INJECTORS)
    gid = persist(make_room(eng, code="XNJ777"), factory)
    text, data = stored_export(db_session, gid)
    res = api.get(url(gid))
    assert res.status_code == 200 and res.text == text
    for name in INJECTORS:
        assert name not in text and name not in json.dumps(data)
    rows = parse_csv(text)
    assert {r["alias"] for r in rows} == {f"P{i}" for i in range(1, 8)}
    for row in rows:
        assert all(not cell.startswith(("=", "+", "@")) for cell in row.values())


# --------------------------------------------------- access (3.2)


def url(public_id: str, fmt: str = "csv") -> str:
    return f"/api/v1/games/id/{public_id}/export?format={fmt}"


@pytest.mark.dbschema
def test_anyone_exports_both_formats_with_no_secret_and_no_sign_in(
    api: TestClient, game: dict[str, Any]
) -> None:
    """Open by public id, like the results: a guest host has no account to fall back
    on once the room is gone, so nothing binds the export to the host."""
    drop_live_room(game["code"])
    res = api.get(url(game["id"]))
    assert res.status_code == 200
    disposition = res.headers["content-disposition"]
    assert disposition.startswith("attachment")
    assert f"pulky-{game['code']}-{game['id'][:8]}.csv" in disposition
    assert len(parse_csv(res.text)) == 14
    res = api.get(url(game["id"], "json"))
    assert res.status_code == 200
    assert res.headers["content-disposition"].startswith("attachment")
    assert f"pulky-{game['code']}-{game['id'][:8]}.json" in (
        res.headers["content-disposition"]
    )
    assert set(res.json()) == {"game", "participants", "player_days", "market_days"}
    assert len(res.json()["player_days"]) == 14


@pytest.mark.dbschema
def test_any_header_or_bearer_is_ignored(
    api: TestClient,
    factory: Callable[[], Session],
    game: dict[str, Any],
    firebase_tokens: dict[str, Any],
    engine: PulkyEngine,
) -> None:
    put_live_room(live_room_for(game["id"], game["room"], secret="live-secret-A"))
    plain = api.get(url(game["id"])).text
    for headers in (
        {"X-Host-Secret": "wrong"},
        {"X-Host-Secret": "live-secret-A"},
        sign_in(firebase_tokens, "uid-someone-else"),
        {"Authorization": "Bearer not-a-valid-token"},
    ):
        res = api.get(url(game["id"]), headers=headers)
        assert res.status_code == 200 and res.text == plain
    # a game whose host was a guest exports like any other
    guest_hosted = persist(
        make_room(
            engine,
            code="GST234",
            started_at="2026-10-14T09:00:00Z",
            finished_at="2026-10-14T10:00:00Z",
        ),
        factory,
    )
    assert api.get(url(guest_hosted)).status_code == 200


@pytest.mark.dbschema
def test_unknown_game_id_is_404_and_another_format_is_422(
    api: TestClient, game: dict[str, Any]
) -> None:
    missing = api.get(url("0" * 32))
    assert missing.status_code == 404 and missing.json() == NOT_FOUND
    assert api.get(url(game["id"], "xml")).status_code == 422
    assert api.get(url(game["id"], "xlsx")).status_code == 422


@pytest.mark.dbschema
def test_ac3_exported_files_carry_no_identity(
    api: TestClient, game: dict[str, Any]
) -> None:
    put_live_room(live_room_for(game["id"], game["room"], secret="live-secret-A"))
    secrets = [*secrets_of(game["room"]), "live-secret-A"]
    for fmt in ("csv", "json"):
        res = api.get(url(game["id"], fmt))
        assert res.status_code == 200
        assert_no_secrets(res.text, secrets)
        assert_no_secrets(dict(res.headers), secrets)
    assert guest_id(1) not in api.get(url(game["id"])).text


# --------------------------------------------------- without a database
# The route and the builders on a game held in memory: the open access, the 404,
# the 429 and the privacy check run in every suite, Docker or not.


@pytest.mark.parametrize(
    "value",
    [
        datetime(2026, 10, 12, 14, tzinfo=UTC),
        datetime(2026, 10, 12, 14, tzinfo=ZoneInfo("UTC")),  # what psycopg returns
        datetime(2026, 10, 12, 8, tzinfo=timezone(timedelta(hours=-6))),
        datetime(2026, 10, 12, 14),  # naive (SQLite): taken to be UTC
    ],
)
def test_iso_utc_writes_every_instant_in_utc_with_z(value: datetime) -> None:
    assert iso_utc(value) == "2026-10-12T14:00:00Z"


UID = "uid-firebase-ana-5150"
EMAIL = "ana.ruiz.host@example.com"
USER_ID = 987_654_321
LIVE_SECRET = "live-host-secret-QQ77"
FORBIDDEN_KEYS = (
    "uid",
    "firebase_uid",
    "email",
    "user_id",
    "identity",
    "guest_identity",
    "host_identity",
    "session_token",
    "host_secret",
)


def memory_game(engine: PulkyEngine, room: dict[str, Any]) -> StoredGame:
    """``room``'s game as ``load_stored_game`` returns it, identities included."""
    identity = {a: p["identity"] for a, p in room["participants"].items()}
    return StoredGame(
        game_id=room["game_id"],
        room_code=room["room_code"],
        host_display_name=room["host_display_name"],
        duration_days=engine.config.duration_days,
        days_played=len(engine.market_history()),
        ended_early=False,
        started_at=datetime(2026, 10, 12, 14, tzinfo=UTC),
        finished_at=datetime(2026, 10, 12, 15, tzinfo=UTC),
        seed=engine.seed,
        config=engine.config.to_dict(),
        records={a: engine.records(a) for a in engine.aliases},
        market=engine.market_history(),
        settlements=engine.settlements(),
        display_names=engine.display_names,
        user_ids={a: USER_ID if identity[a] == UID else None for a in identity},
        guest_identities={
            a: None if identity[a] == UID else identity[a] for a in identity
        },
    )


@pytest.fixture()
def held(
    engine: PulkyEngine,
    monkeypatch: pytest.MonkeyPatch,
    memory_backend: Any,
) -> Iterator[dict[str, Any]]:
    """A finished game the routes find without a database; its room is still live, with
    a signed-in host (``UID``) who also played as P1 and a guest as P2."""
    room = make_room(
        engine,
        code="MEM234",
        identities={"P1": UID, "P2": guest_id(77)},
        host_identity=UID,
        game_id="c" * 32,
    )
    stored_game = memory_game(engine, room)
    reads: list[str] = []

    def find(db: Any, pid: str) -> Any:
        reads.append(pid)
        return SimpleNamespace(id=1) if pid == room["game_id"] else None

    def load(db: Any, pk: int) -> StoredGame:
        reads.append(f"load {pk}")
        return stored_game

    monkeypatch.setattr(db_service, "find_game_by_public_id", find)
    monkeypatch.setattr(db_service, "load_stored_game", load)
    reset_state_service()
    put_live_room(live_room_for(room["game_id"], room, secret=LIVE_SECRET))
    yield {"id": room["game_id"], "room": room, "stored": stored_game, "reads": reads}
    reset_state_service()


def forbidden_values(room: dict[str, Any]) -> list[str]:
    names = [p["display_name"] for p in room["participants"].values()]
    return [
        *secrets_of(room),
        LIVE_SECRET,
        UID,
        EMAIL,
        str(USER_ID),
        guest_id(77),
        *names,
        room["host_display_name"],
    ]


def test_privacy_the_builders_carry_only_what_the_results_show(
    held: dict[str, Any],
) -> None:
    text = export_csv(held["stored"])
    data = export_json(held["stored"])
    blob = json.dumps(data)
    for value in forbidden_values(held["room"]):
        assert value not in text, value
        assert value not in blob, value
    header = next(csv.reader(io.StringIO(text, newline="")))
    for key in FORBIDDEN_KEYS:
        assert not contains_key(data, key), key
        assert key not in header, key
    # the files name every pulquería by its alias only, P<N> by seat
    shown = results_response(held["stored"])
    assert [p["alias"] for p in data["participants"]] == ["P1", "P2"]
    assert {p["alias"] for p in data["participants"]} == {
        r["alias"] for r in shown["leaderboard"]
    }
    assert data["game"]["config"] == shown["config"]
    for key in ("display_name", "host_display_name"):
        assert not contains_key(data, key), key
        assert key not in header, key


def test_privacy_the_route_carries_no_identity_of_the_caller_or_the_room(
    bare_api: TestClient, held: dict[str, Any], firebase_tokens: dict[str, Any]
) -> None:
    bearer = sign_in(firebase_tokens, UID, email=EMAIL)
    values = forbidden_values(held["room"])
    for fmt in ("csv", "json"):
        for headers in ({}, bearer, {"X-Host-Secret": LIVE_SECRET}):
            res = bare_api.get(url(held["id"], fmt), headers=headers)
            assert res.status_code == 200
            for value in values:
                assert value not in res.text, value
                assert value not in json.dumps(dict(res.headers)), value


def test_the_route_is_open_and_answers_404_for_an_unknown_id(
    bare_api: TestClient, held: dict[str, Any]
) -> None:
    res = bare_api.get(url(held["id"]))
    assert res.status_code == 200
    assert res.text == export_csv(held["stored"])
    assert res.headers["content-disposition"] == (
        f'attachment; filename="pulky-MEM234-{held["id"][:8]}.csv"'
    )
    res = bare_api.get(url(held["id"], "json"))
    assert res.status_code == 200
    assert res.json() == norm(export_json(held["stored"]))
    missing = bare_api.get(url("0" * 32))
    assert missing.status_code == 404 and missing.json() == NOT_FOUND


def test_the_route_answers_429_past_100_an_hour_per_ip_across_formats(
    bare_api: TestClient, held: dict[str, Any]
) -> None:
    origin = {"Origin": "https://pulkysim.web.app"}
    limit = abuse.ROUTE_LIMITS[abuse.EXPORT_ROUTE]
    for i in range(limit):
        fmt = "csv" if i % 2 else "json"
        assert bare_api.get(url(held["id"], fmt), headers=origin).status_code == 200
    for fmt in ("csv", "json"):
        refused = bare_api.get(url(held["id"], fmt), headers=origin)
        assert refused.status_code == 429
        assert refused.json() == {"detail": abuse.TOO_MANY_REQUESTS}
        assert refused.headers["retry-after"] == "3600"
        assert refused.headers["access-control-allow-origin"] == origin["Origin"]
    # an unknown id is charged too: guessing ids costs the same budget
    assert bare_api.get(url("0" * 32)).status_code == 429
    # the results stay open to that IP
    assert bare_api.get(f"/api/v1/games/id/{held['id']}/results").status_code == 200


DISTINCT_NAMES = ["Zyxwv Quokka 4417", "Mxyzptlk Pulqueria 0042", "=Jabberwock 9"]


def test_p_n_anonymization_no_display_name_reaches_either_format(
    bare_api: TestClient, monkeypatch: pytest.MonkeyPatch, memory_backend: Any
) -> None:
    """Players keep their names on screen; the files name them P1, P2, P3 by seat,
    the same P<N> in every row and in every peer-trade counterparty."""
    eng = play_game(players=3, names=DISTINCT_NAMES)
    room = make_room(eng, code="ANN234", host_name="Profesora Xochiquetzal 77")
    game = memory_game(eng, room)
    assert set(results_response(game)["stats"]["players"]) == {"P1", "P2", "P3"}
    monkeypatch.setattr(
        db_service, "find_game_by_public_id", lambda db, pid: SimpleNamespace(id=1)
    )
    monkeypatch.setattr(db_service, "load_stored_game", lambda db, pk: game)
    hidden = [*DISTINCT_NAMES, "Profesora Xochiquetzal 77", "Zyxwv", "Mxyzptlk"]
    for fmt in ("csv", "json"):
        res = bare_api.get(url(room["game_id"], fmt))
        assert res.status_code == 200
        for name in hidden:
            assert name not in res.text, (fmt, name)
    data = export_json(game)
    aliases = {"P1", "P2", "P3"}
    assert {r["alias"] for r in data["player_days"]} == aliases
    assert {p["alias"] for p in data["participants"]} == aliases
    counterparties = {
        t["counterparty"] for r in data["player_days"] for t in r["peer_trades"]
    }
    assert counterparties and counterparties <= aliases


# --------------------------------------------------- the cache


def test_a_second_request_is_served_from_the_cache_with_no_database_read(
    bare_api: TestClient, held: dict[str, Any]
) -> None:
    first = bare_api.get(url(held["id"]))
    assert first.status_code == 200
    assert held["reads"] == [held["id"], "load 1"]
    second = bare_api.get(url(held["id"]))
    assert second.status_code == 200
    assert second.content == first.content
    assert second.headers["content-disposition"] == first.headers["content-disposition"]
    assert held["reads"] == [held["id"], "load 1"]  # no database read
    # the other format is its own entry
    assert bare_api.get(url(held["id"], "json")).status_code == 200
    assert len(held["reads"]) == 4
    bare_api.get(url(held["id"], "json"))
    assert len(held["reads"]) == 4


def test_only_a_200_is_cached_and_every_request_still_counts_against_the_limit(
    bare_api: TestClient, held: dict[str, Any]
) -> None:
    unknown = "0" * 32
    assert bare_api.get(url(unknown)).status_code == 404
    assert bare_api.get(url(unknown)).status_code == 404
    assert held["reads"] == [unknown, unknown]  # a 404 is never cached
    limit = abuse.ROUTE_LIMITS[abuse.EXPORT_ROUTE]
    for _ in range(limit - 2):
        assert bare_api.get(url(held["id"])).status_code == 200
    assert bare_api.get(url(held["id"])).status_code == 429  # cached, still counted


def test_the_cache_keeps_the_64_most_recently_used_files(
    held: dict[str, Any],
) -> None:
    size = export_service.CACHE_SIZE
    assert size == 64
    file = export_service.build_file(held["stored"], "csv")
    for i in range(size):
        export_service.remember_file(f"{i:032x}", "csv", file)
    assert export_service.cached_file(f"{0:032x}", "csv") is file  # now most recent
    export_service.remember_file("f" * 32, "csv", file)  # one past the size
    assert export_service.cached_file(f"{1:032x}", "csv") is None  # least recent went
    assert export_service.cached_file(f"{0:032x}", "csv") is file
    assert export_service.cached_file("f" * 32, "csv") is file
