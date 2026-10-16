# ruff: noqa: F811
"""Black-box tests for section 19: the user routes (profile, stats, games played)."""

from collections.abc import Callable
from datetime import datetime
from statistics import mean
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.engine import PulkyEngine
from app.services.user_service import upsert_user
from tests.fixtures.mysql import db_session, mysql_url  # noqa: F401
from tests.test_api.test_results import (  # noqa: F401
    NEED_SIGN_IN,
    NO_SIGN_IN,
    NOT_FOUND,
    api,
    assert_no_secrets,
    bare_api,
    db_engine,
    factory,
    guest_id,
    make_room,
    norm,
    persist,
    play_game,
    scalar,
    secrets_of,
    sign_in,
)

USER_KEYS = {"display_name", "email", "photo_url"}
STATS_KEYS = {
    "games_played",
    "wins",
    "total_profit",
    "best_profit",
    "avg_fill_rate",
    "avg_waste_rate",
    "avg_market_share",
}
ITEM_KEYS = {
    "game_id",
    "room_code",
    "finished_at",
    "days_played",
    "duration_days",
    "player_count",
    "ended_early",
    "alias",
    "display_name",
    "rank",
    "total_profit",
}
ANA = "uid-ana-7731"
BOB = "uid-bob-4402"


def when(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def leaderboard_row(engine: PulkyEngine, alias: str) -> dict[str, Any]:
    rows = norm(engine.results())["leaderboard"]
    return next(r for r in rows if r["alias"] == alias)


def user_row(db: Session, uid: str) -> dict[str, Any] | None:
    db.rollback()
    row = (
        db.execute(
            text(
                "SELECT id, display_name, email, photo_url FROM users "
                "WHERE firebase_uid = :u"
            ),
            {"u": uid},
        )
        .mappings()
        .first()
    )
    return dict(row) if row else None


# --------------------------------------------------- the world

ROUTES = [
    ("POST", "/api/v1/users/upsert"),
    ("GET", "/api/v1/users/me"),
    ("GET", "/api/v1/users/me/stats"),
    ("GET", "/api/v1/users/me/games"),
    ("GET", f"/api/v1/users/me/games/{'a' * 32}"),
]


def call(client: TestClient, route: tuple[str, str], **kwargs: Any) -> Any:
    method, url = route
    if method == "POST":
        kwargs.setdefault("json", {})
    return client.request(method, url, **kwargs)


@pytest.fixture(scope="module")
def engines() -> dict[str, PulkyEngine]:
    return {
        "A": play_game(seed=11),
        "B": play_game(seed=5, early=True),
        "C": play_game(seed=3),
        "D": play_game(seed=7),
    }


@pytest.fixture()
def world(
    api: TestClient, factory: Callable[[], Session], engines: dict[str, PulkyEngine]
) -> dict[str, Any]:
    """Ana played A (as P1) and B (as P2) and hosted D; Bob played C only.

    B finished latest but was written first, A finished earlier but was written
    later, so neither insertion order nor start time orders the list correctly.
    """
    rooms = {
        "B": make_room(
            engines["B"],
            code="BBB222",
            identities={"P1": guest_id(31), "P2": ANA},
            started_at="2026-10-11T09:00:00Z",
            finished_at="2026-10-12T13:00:00Z",
        ),
        "A": make_room(
            engines["A"],
            code="AAA222",
            identities={"P1": ANA, "P2": guest_id(32)},
            started_at="2026-10-12T10:00:00Z",
            finished_at="2026-10-12T11:00:00Z",
        ),
        "C": make_room(
            engines["C"],
            code="CCC222",
            identities={"P1": BOB, "P2": guest_id(33)},
            started_at="2026-10-13T10:00:00Z",
            finished_at="2026-10-13T11:00:00Z",
        ),
        "D": make_room(
            engines["D"],
            code="DDD222",
            identities={"P1": guest_id(34), "P2": guest_id(35)},
            host_identity=ANA,
            started_at="2026-10-14T10:00:00Z",
            finished_at="2026-10-14T11:00:00Z",
        ),
    }
    ids = {key: persist(room, factory) for key, room in rooms.items()}
    return {"rooms": rooms, "ids": ids}


def secrets_of_world(world: dict[str, Any]) -> list[str]:
    out = [ANA, BOB]
    for room in world["rooms"].values():
        out += secrets_of(room)
    return out


# --------------------------------------------------- AC 6: auth


@pytest.mark.parametrize("route", ROUTES, ids=[f"{m} {u[-20:]}" for m, u in ROUTES])
@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Token abc"},
        {"Authorization": "Bearer"},
        {"Authorization": "Bearer not-a-valid-token"},
    ],
)
def test_ac6_user_routes_answer_401_without_a_valid_bearer(
    route: tuple[str, str],
    headers: dict[str, str],
    bare_api: TestClient,
    firebase_tokens: dict[str, Any],
) -> None:
    firebase_tokens["token-of-someone"] = {"uid": "someone"}
    res = call(bare_api, route, headers=headers)
    assert res.status_code == 401
    assert res.json() == NEED_SIGN_IN


@pytest.mark.parametrize("route", ROUTES, ids=[f"{m} {u[-20:]}" for m, u in ROUTES])
def test_ac6_user_routes_answer_503_when_firebase_is_unconfigured(
    route: tuple[str, str], bare_api: TestClient
) -> None:
    res = call(bare_api, route, headers={"Authorization": "Bearer anything"})
    assert res.status_code == 503
    assert res.json() == NO_SIGN_IN


# --------------------------------------------------- AC 6: upsert and me


@pytest.mark.dbschema
def test_upsert_creates_the_row_from_the_verified_token(
    api: TestClient, db_session: Session, firebase_tokens: dict[str, Any]
) -> None:
    bearer = sign_in(
        firebase_tokens,
        ANA,
        email="ana@example.com",
        picture="https://photos.example.com/ana.png",
        name="Ana Token",
    )
    res = api.post(
        "/api/v1/users/upsert", json={"display_name": "Casa Maguey"}, headers=bearer
    )
    assert res.status_code == 200
    assert res.json() == {
        "display_name": "Casa Maguey",
        "email": "ana@example.com",
        "photo_url": "https://photos.example.com/ana.png",
    }
    row = user_row(db_session, ANA)
    assert row is not None
    assert row["display_name"] == "Casa Maguey"
    assert row["email"] == "ana@example.com"
    assert row["photo_url"] == "https://photos.example.com/ana.png"


@pytest.mark.dbschema
def test_upsert_without_a_body_name_takes_the_token_name_then_player(
    api: TestClient, firebase_tokens: dict[str, Any]
) -> None:
    named = sign_in(firebase_tokens, ANA, email="ana@example.com", name="Ana Token")
    res = api.post("/api/v1/users/upsert", json={}, headers=named)
    assert res.status_code == 200
    assert res.json()["display_name"] == "Ana Token"
    nameless = sign_in(firebase_tokens, BOB, email="bob@example.com")
    res = api.post("/api/v1/users/upsert", json={}, headers=nameless)
    assert res.status_code == 200
    assert res.json() == {
        "display_name": "Player",
        "email": "bob@example.com",
        "photo_url": None,
    }


@pytest.mark.dbschema
def test_upsert_twice_updates_one_row(
    api: TestClient, db_session: Session, firebase_tokens: dict[str, Any]
) -> None:
    bearer = sign_in(firebase_tokens, ANA, email="ana@example.com", name="Ana")
    api.post("/api/v1/users/upsert", json={"display_name": "Primero"}, headers=bearer)
    firebase_tokens[bearer["Authorization"].split()[1]] = {
        "uid": ANA,
        "sub": ANA,
        "user_id": ANA,
        "email": "ana.new@example.com",
        "picture": "https://photos.example.com/new.png",
    }
    res = api.post(
        "/api/v1/users/upsert", json={"display_name": "Segundo"}, headers=bearer
    )
    assert res.status_code == 200
    assert res.json() == {
        "display_name": "Segundo",
        "email": "ana.new@example.com",
        "photo_url": "https://photos.example.com/new.png",
    }
    assert scalar(db_session, "SELECT COUNT(*) FROM users") == 1


@pytest.mark.dbschema
def test_upsert_display_name_goes_through_the_sanitiser(
    api: TestClient, firebase_tokens: dict[str, Any]
) -> None:
    bearer = sign_in(firebase_tokens, ANA)
    res = api.post(
        "/api/v1/users/upsert", json={"display_name": "x" * 80}, headers=bearer
    )
    assert res.status_code == 200
    name = res.json()["display_name"]
    assert isinstance(name, str) and 1 <= len(name) <= 24


@pytest.mark.dbschema
def test_me_creates_the_row_when_missing_and_keeps_an_upserted_name(
    api: TestClient, db_session: Session, firebase_tokens: dict[str, Any]
) -> None:
    bearer = sign_in(firebase_tokens, ANA, email="ana@example.com", name="Ana Token")
    assert user_row(db_session, ANA) is None
    first = api.get("/api/v1/users/me", headers=bearer)
    assert first.status_code == 200
    assert set(first.json()) == USER_KEYS
    assert first.json()["email"] == "ana@example.com"
    assert user_row(db_session, ANA) is not None
    api.post(
        "/api/v1/users/upsert", json={"display_name": "Casa Maguey"}, headers=bearer
    )
    again = api.get("/api/v1/users/me", headers=bearer)
    assert again.json()["display_name"] == "Casa Maguey"
    assert scalar(db_session, "SELECT COUNT(*) FROM users") == 1


@pytest.mark.dbschema
def test_fm4_the_uid_comes_from_the_token_never_from_body_or_query(
    api: TestClient, db_session: Session, firebase_tokens: dict[str, Any]
) -> None:
    sign_in(firebase_tokens, BOB, email="bob@example.com", name="Bob")
    bob_bearer = {"Authorization": f"Bearer token-of-{BOB}"}
    api.post(
        "/api/v1/users/upsert", json={"display_name": "Bob Bar"}, headers=bob_bearer
    )
    bob_before = user_row(db_session, BOB)
    assert bob_before is not None
    ana = sign_in(firebase_tokens, ANA, email="ana@example.com", name="Ana")
    spoof = f"?uid={BOB}&firebase_uid={BOB}&user_id={bob_before['id']}"
    res = api.post(
        "/api/v1/users/upsert" + spoof,
        json={
            "display_name": "Ana Bar",
            "uid": BOB,
            "firebase_uid": BOB,
            "user_id": bob_before["id"],
            "email": "evil@example.com",
        },
        headers=ana,
    )
    assert res.status_code == 200
    assert res.json()["display_name"] == "Ana Bar"
    assert res.json()["email"] == "ana@example.com"
    assert user_row(db_session, BOB) == bob_before  # not a column of it changed
    ana_row = user_row(db_session, ANA)
    assert ana_row is not None and ana_row["display_name"] == "Ana Bar"
    me = api.get("/api/v1/users/me" + spoof, headers=ana)
    assert me.json()["email"] == "ana@example.com"
    assert me.json()["display_name"] == "Ana Bar"


@pytest.mark.dbschema
def test_the_upsert_service_follows_the_same_rules(db_session: Session) -> None:
    user = upsert_user(
        db_session,
        {"uid": ANA, "sub": ANA, "user_id": ANA, "email": "ana@example.com"},
        None,
    )
    db_session.commit()
    assert user.display_name == "Player"
    assert user.email == "ana@example.com"
    again = upsert_user(
        db_session, {"uid": ANA, "sub": ANA, "user_id": ANA, "name": "Ana"}, "Doña Ana"
    )
    db_session.commit()
    assert again.id == user.id
    assert again.display_name == "Doña Ana"
    assert scalar(db_session, "SELECT COUNT(*) FROM users") == 1


# --------------------------------------------------- stats


@pytest.mark.dbschema
def test_stats_for_a_user_without_games_are_zeros_and_nulls(
    api: TestClient, firebase_tokens: dict[str, Any]
) -> None:
    bearer = sign_in(firebase_tokens, ANA)
    res = api.get("/api/v1/users/me/stats", headers=bearer)
    assert res.status_code == 200
    body = res.json()
    assert set(body) == STATS_KEYS
    assert body["games_played"] == 0 and body["wins"] == 0
    assert body["total_profit"] == 0
    assert body["best_profit"] in (None, 0)
    for key in ("avg_fill_rate", "avg_waste_rate", "avg_market_share"):
        assert body[key] is None
    assert api.get("/api/v1/users/me/games", headers=bearer).json() == {"games": []}


@pytest.mark.dbschema
def test_stats_aggregate_the_games_the_caller_played(
    api: TestClient,
    world: dict[str, Any],
    engines: dict[str, PulkyEngine],
    firebase_tokens: dict[str, Any],
) -> None:
    bearer = sign_in(firebase_tokens, ANA)
    body = api.get("/api/v1/users/me/stats", headers=bearer).json()
    mine = [
        (leaderboard_row(engines["A"], "P1"), engines["A"], "P1"),
        (leaderboard_row(engines["B"], "P2"), engines["B"], "P2"),
    ]
    profits = [r["total_profit"] for r, _, _ in mine]
    assert set(body) == STATS_KEYS
    assert body["games_played"] == 2  # hosting D does not count
    assert body["wins"] == sum(1 for r, _, _ in mine if r["rank"] == 1)
    assert body["total_profit"] == pytest.approx(sum(profits), abs=0.01)
    assert body["best_profit"] == pytest.approx(max(profits), abs=0.01)
    fills = [r["fill_rate"] for r, _, _ in mine if r["fill_rate"] is not None]
    wastes = [r["waste_rate"] for r, _, _ in mine if r["waste_rate"] is not None]
    shares = [
        norm(e.results())["stats"]["players"][a]["overall_market_share"]
        for _, e, a in mine
    ]
    assert fills and body["avg_fill_rate"] == pytest.approx(mean(fills), abs=1e-6)
    if wastes:
        assert body["avg_waste_rate"] == pytest.approx(mean(wastes), abs=1e-6)
    else:
        assert body["avg_waste_rate"] is None
    assert body["avg_market_share"] == pytest.approx(mean(shares), abs=1e-6)


# --------------------------------------------------- games list and detail (AC 7)


@pytest.mark.dbschema
def test_ac7_the_games_list_holds_only_played_games_newest_first(
    api: TestClient,
    world: dict[str, Any],
    engines: dict[str, PulkyEngine],
    firebase_tokens: dict[str, Any],
) -> None:
    bearer = sign_in(firebase_tokens, ANA)
    res = api.get("/api/v1/users/me/games", headers=bearer)
    assert res.status_code == 200
    games = res.json()["games"]
    ids = world["ids"]
    assert [g["game_id"] for g in games] == [ids["B"], ids["A"]]
    for item, key, alias in zip(games, ("B", "A"), ("P2", "P1"), strict=True):
        engine, room = engines[key], world["rooms"][key]
        row = leaderboard_row(engine, alias)
        assert set(item) == ITEM_KEYS
        assert item["room_code"] == room["room_code"]
        assert when(item["finished_at"]) == when(room["finished_at"])
        assert item["days_played"] == len(engine.market_history())
        assert item["duration_days"] == room["config"]["duration_days"]
        assert item["player_count"] == len(engine.aliases)
        assert item["ended_early"] is engine.ended_early
        assert item["alias"] == alias
        assert item["display_name"] == engine.display_names[alias]
        assert item["rank"] == row["rank"]
        assert item["total_profit"] == pytest.approx(row["total_profit"], abs=0.01)
    assert games[0]["ended_early"] is True and games[1]["ended_early"] is False
    # another player sees only theirs
    bob = api.get("/api/v1/users/me/games", headers=sign_in(firebase_tokens, BOB))
    assert [g["game_id"] for g in bob.json()["games"]] == [ids["C"]]


@pytest.mark.dbschema
def test_ac7_detail_returns_the_results_plus_my_alias(
    api: TestClient, world: dict[str, Any], firebase_tokens: dict[str, Any]
) -> None:
    bearer = sign_in(firebase_tokens, ANA)
    ids = world["ids"]
    for key, alias in (("A", "P1"), ("B", "P2")):
        res = api.get(f"/api/v1/users/me/games/{ids[key]}", headers=bearer)
        assert res.status_code == 200
        public = api.get(f"/api/v1/games/id/{ids[key]}/results").json()
        assert res.json() == {**public, "my_alias": alias}


@pytest.mark.dbschema
def test_fm3_a_game_the_caller_did_not_play_answers_like_a_random_id(
    api: TestClient, world: dict[str, Any], firebase_tokens: dict[str, Any]
) -> None:
    bearer = sign_in(firebase_tokens, ANA)
    ids = world["ids"]
    random_id = api.get(f"/api/v1/users/me/games/{'0' * 32}", headers=bearer)
    assert random_id.status_code == 404
    assert random_id.json() == NOT_FOUND
    # C was played by someone else, D was only hosted by the caller
    for key in ("C", "D"):
        real = api.get(f"/api/v1/users/me/games/{ids[key]}", headers=bearer)
        assert real.status_code == 404
        assert real.json() == NOT_FOUND
        assert real.text == random_id.text
        assert real.headers["content-type"] == random_id.headers["content-type"]
        assert real.headers.get("content-length") == random_id.headers.get(
            "content-length"
        )
    # the public results of that game still exist, so only the profile hides it
    assert api.get(f"/api/v1/games/id/{ids['C']}/results").status_code == 200


@pytest.mark.dbschema
def test_fm4_spoofed_uid_in_the_query_changes_nothing(
    api: TestClient, world: dict[str, Any], firebase_tokens: dict[str, Any]
) -> None:
    sign_in(firebase_tokens, BOB)
    nobody = sign_in(firebase_tokens, "uid-nobody-0001")
    spoof = f"?uid={BOB}&firebase_uid={BOB}"
    stats = api.get("/api/v1/users/me/stats" + spoof, headers=nobody).json()
    assert stats["games_played"] == 0
    games = api.get("/api/v1/users/me/games" + spoof, headers=nobody).json()
    assert games == {"games": []}
    c_id = world["ids"]["C"]
    res = api.get(f"/api/v1/users/me/games/{c_id}" + spoof, headers=nobody)
    assert res.status_code == 404


# --------------------------------------------------- AC 3: no leaks


@pytest.mark.dbschema
def test_ac3_no_user_route_carries_an_identity_or_a_credential(
    api: TestClient, world: dict[str, Any], firebase_tokens: dict[str, Any]
) -> None:
    bearer = sign_in(firebase_tokens, ANA, email="ana@example.com", name="Ana")
    ids = world["ids"]
    secrets = secrets_of_world(world)
    urls = [
        ("GET", "/api/v1/users/me"),
        ("GET", "/api/v1/users/me/stats"),
        ("GET", "/api/v1/users/me/games"),
        ("GET", f"/api/v1/users/me/games/{ids['A']}"),
        ("GET", f"/api/v1/users/me/games/{ids['B']}"),
        ("POST", "/api/v1/users/upsert"),
    ]
    for route in urls:
        res = call(api, route, headers=bearer)
        assert res.status_code == 200, route
        assert_no_secrets(res.text, secrets)
        assert_no_secrets(dict(res.headers), secrets)
        assert "uid-ana" not in res.text


# --------------------------------------------------- AC 8: queries


class Counter:
    def __init__(self, engine: Any) -> None:
        from sqlalchemy import event

        self.engine = engine
        self.statements: list[str] = []
        self._event = event
        event.listen(engine, "before_cursor_execute", self._on)

    def _on(self, conn: Any, cursor: Any, statement: Any, *args: Any) -> None:
        self.statements.append(str(statement))

    def reset(self) -> None:
        self.statements.clear()

    def stop(self) -> None:
        self._event.remove(self.engine, "before_cursor_execute", self._on)


@pytest.mark.dbschema
@pytest.mark.parametrize("path", ["/api/v1/users/me/games", "/api/v1/users/me/stats"])
def test_ac8_query_count_does_not_grow_with_the_number_of_games(
    path: str,
    api: TestClient,
    factory: Callable[[], Session],
    db_engine: Any,
    engines: dict[str, PulkyEngine],
    firebase_tokens: dict[str, Any],
) -> None:
    bearer = sign_in(firebase_tokens, ANA)

    def add_games(count: int, start: int) -> None:
        for n in range(start, start + count):
            persist(
                make_room(
                    engines["A"],
                    code=f"Q{n:02d}222",
                    identities={"P1": ANA, "P2": guest_id(900 + n)},
                    started_at=f"2026-11-{n:02d}T10:00:00Z",
                    finished_at=f"2026-11-{n:02d}T11:00:00Z",
                ),
                factory,
            )

    add_games(1, 1)
    api.get(path, headers=bearer)  # warm up: the first call may create the user
    counter = Counter(db_engine)
    try:
        counter.reset()
        small = api.get(path, headers=bearer)
        with_one = len(counter.statements)
        add_games(8, 2)
        counter.reset()
        large = api.get(path, headers=bearer)
        with_nine = len(counter.statements)
    finally:
        counter.stop()
    assert small.status_code == large.status_code == 200
    if path.endswith("games"):
        assert len(small.json()["games"]) == 1
        assert len(large.json()["games"]) == 9
    else:
        assert large.json()["games_played"] == 9
    assert with_one > 0
    assert with_nine <= with_one, (with_one, with_nine)
