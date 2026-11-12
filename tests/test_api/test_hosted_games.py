# ruff: noqa: F811
"""The host rule: ``GET /users/me/hosted-games/{id}`` and the export's bearer proof.

Both ask ``user_service.is_game_host`` and nothing else, so the results page offers
the export to exactly the signed-in callers the export route accepts. The check
answers 204 or 404, never 403, so it is no game-existence oracle.
"""

import asyncio
from collections.abc import Callable, Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api.v1.games import _may_export
from app.core.engine import PulkyEngine
from app.services import user_service
from app.services.state_service import (
    get_state_service,
    new_room_document,
    reset_state_service,
)
from tests.fixtures.mysql import db_session, mysql_url  # noqa: F401
from tests.test_api.test_results import (  # noqa: F401
    NOT_FOUND,
    api,
    db_engine,
    factory,
    guest_id,
    make_room,
    persist,
    play_game,
    sign_in,
)

ANA = "uid-ana-7731"
BOB = "uid-bob-4402"
NOBODY = "uid-nobody-0001"  # signed in, but no users row
UNKNOWN = "0" * 32


def hosted(public_id: str) -> str:
    return f"/api/v1/users/me/hosted-games/{public_id}"


def export(public_id: str) -> str:
    return f"/api/v1/games/id/{public_id}/export?format=json"


# --------------------------------------------------- _may_export, unchanged

GAME_ID = "a" * 32
OTHER_GAME_ID = "b" * 32
CODE = "HST234"
SECRET = "live-secret-HST"
HOST_USER_ID = 7
USERS = {"uid-host": 7, "uid-other": 8}
TOKENS = {
    "token-host": {"uid": "uid-host"},
    "token-other": {"uid": "uid-other"},
    "token-no-row": {"uid": "uid-without-a-row"},
    "token-no-uid": {"email": "x@example.com"},
}


@pytest.fixture()
def live_rooms(memory_backend: Any) -> Iterator[None]:
    reset_state_service()
    yield
    reset_state_service()


def put_live(game_id: str) -> None:
    room = new_room_document(
        room_code=CODE,
        config={},
        host_display_name="Ms. Ruiz",
        host_secret=SECRET,
        created_at="2026-10-12T14:00:00Z",
    )
    room["game_id"] = game_id
    asyncio.run(get_state_service().save_room(CODE, room))


@pytest.mark.parametrize(
    ("live", "host_user_id", "secret", "authorization", "allowed"),
    [
        # the live room's secret, for this game only
        (GAME_ID, HOST_USER_ID, SECRET, None, True),
        (GAME_ID, None, SECRET, None, True),
        (GAME_ID, HOST_USER_ID, "wrong", None, False),
        (GAME_ID, HOST_USER_ID, "", None, False),
        (OTHER_GAME_ID, HOST_USER_ID, SECRET, None, False),
        (None, HOST_USER_ID, SECRET, None, False),
        (None, HOST_USER_ID, None, None, False),
        # the host's bearer, live room or not
        (None, HOST_USER_ID, None, "Bearer token-host", True),
        (GAME_ID, HOST_USER_ID, None, "Bearer token-host", True),
        (GAME_ID, HOST_USER_ID, "wrong", "bearer token-host", True),
        # anyone else's bearer, or no usable bearer
        (None, HOST_USER_ID, None, "Bearer token-other", False),
        (None, HOST_USER_ID, None, "Bearer token-no-row", False),
        (None, HOST_USER_ID, None, "Bearer token-no-uid", False),
        (None, HOST_USER_ID, None, "Bearer not-a-valid-token", False),
        (None, HOST_USER_ID, None, "Token token-host", False),
        (None, HOST_USER_ID, None, "Bearer", False),
        (None, HOST_USER_ID, None, "Bearer token-host extra", False),
        (None, HOST_USER_ID, None, "", False),
        # a game whose host was a guest has no host user: no bearer exports it
        (None, None, None, "Bearer token-host", False),
    ],
)
def test_may_export_accepts_and_refuses_as_before(
    live: str | None,
    host_user_id: int | None,
    secret: str | None,
    authorization: str | None,
    allowed: bool,
    live_rooms: None,
    firebase_tokens: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    firebase_tokens.update(TOKENS)
    monkeypatch.setattr(
        user_service,
        "find_user",
        lambda db, uid: SimpleNamespace(id=USERS[uid]) if uid in USERS else None,
    )
    if live is not None:
        put_live(live)
    game: Any = SimpleNamespace(
        public_id=GAME_ID, room_code=CODE, host_user_id=host_user_id
    )
    db: Any = None  # find_user is the only database read, and it is replaced
    assert asyncio.run(_may_export(db, game, secret, authorization)) is allowed


# --------------------------------------------------- the route (MySQL)


@pytest.fixture(scope="module")
def engine() -> PulkyEngine:
    return play_game()


@pytest.fixture()
def world(
    api: TestClient, factory: Callable[[], Session], engine: PulkyEngine
) -> dict[str, str]:
    """Ana hosted H and played G, whose host was a guest; Bob hosted O."""
    rooms = {
        "H": make_room(engine, code="HHH222", host_identity=ANA),
        "G": make_room(
            engine,
            code="GGG222",
            identities={"P1": ANA, "P2": guest_id(41)},
            started_at="2026-10-13T10:00:00Z",
            finished_at="2026-10-13T11:00:00Z",
        ),
        "O": make_room(
            engine,
            code="BBB222",
            host_identity=BOB,
            started_at="2026-10-14T10:00:00Z",
            finished_at="2026-10-14T11:00:00Z",
        ),
    }
    return {key: persist(room, factory) for key, room in rooms.items()}


@pytest.mark.dbschema
def test_the_host_gets_204_with_no_body(
    api: TestClient, world: dict[str, str], firebase_tokens: dict[str, Any]
) -> None:
    res = api.get(hosted(world["H"]), headers=sign_in(firebase_tokens, ANA))
    assert res.status_code == 204
    assert res.content == b""
    bob = api.get(hosted(world["O"]), headers=sign_in(firebase_tokens, BOB))
    assert bob.status_code == 204


@pytest.mark.dbschema
def test_anyone_else_gets_the_404_of_an_unknown_id(
    api: TestClient, world: dict[str, str], firebase_tokens: dict[str, Any]
) -> None:
    ana = sign_in(firebase_tokens, ANA)
    unknown = api.get(hosted(UNKNOWN), headers=ana)
    assert unknown.status_code == 404
    assert unknown.json() == NOT_FOUND
    answers = [
        api.get(hosted(world["H"]), headers=sign_in(firebase_tokens, BOB)),
        api.get(hosted(world["H"]), headers=sign_in(firebase_tokens, NOBODY)),
        api.get(hosted(world["O"]), headers=ana),  # hosted by Bob
        api.get(hosted(world["G"]), headers=ana),  # played, not hosted
    ]
    for res in answers:
        assert res.status_code == 404
        assert res.text == unknown.text
        assert res.headers["content-type"] == unknown.headers["content-type"]
        assert res.headers.get("content-length") == unknown.headers.get(
            "content-length"
        )


@pytest.mark.dbschema
def test_the_check_answers_yes_exactly_when_the_bearer_exports(
    api: TestClient, world: dict[str, str], firebase_tokens: dict[str, Any]
) -> None:
    yes = set()
    for uid in (ANA, BOB, NOBODY):
        bearer = sign_in(firebase_tokens, uid)
        for key, public_id in world.items():
            check = api.get(hosted(public_id), headers=bearer).status_code
            download = api.get(export(public_id), headers=bearer).status_code
            assert (check, download) in ((204, 200), (404, 403)), (uid, key)
            if check == 204:
                yes.add((uid, key))
    assert yes == {(ANA, "H"), (BOB, "O")}


@pytest.mark.dbschema
def test_both_routes_follow_is_game_host_and_nothing_else(
    api: TestClient,
    world: dict[str, str],
    firebase_tokens: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked: list[tuple[str, str]] = []
    answer = {"value": True}

    def rule(db: Session, uid: str, game: Any) -> bool:
        asked.append((uid, game.public_id))
        return answer["value"]

    monkeypatch.setattr(user_service, "is_game_host", rule)
    bob, ana = sign_in(firebase_tokens, BOB), sign_in(firebase_tokens, ANA)
    # the rule says Bob hosted H: both routes let him through
    assert api.get(hosted(world["H"]), headers=bob).status_code == 204
    assert api.get(export(world["H"]), headers=bob).status_code == 200
    # the rule says Ana did not: both routes refuse her
    answer["value"] = False
    assert api.get(hosted(world["H"]), headers=ana).status_code == 404
    assert api.get(export(world["H"]), headers=ana).status_code == 403
    assert asked == [(BOB, world["H"])] * 2 + [(ANA, world["H"])] * 2
