# ruff: noqa: F811
"""Black-box tests for section 19: attaching guest games to an account."""

from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.engine import PulkyEngine
from app.services.claim_service import claim_guest_results
from app.services.stats_service import get_or_create_user
from tests.fixtures.mysql import db_session, mysql_url  # noqa: F401
from tests.test_api.test_results import (  # noqa: F401
    NEED_SIGN_IN,
    NO_SIGN_IN,
    api,
    assert_no_secrets,
    bare_api,
    db_engine,
    factory,
    guest_id,
    make_room,
    persist,
    play_game,
    scalar,
    secrets_of,
    sign_in,
)

NOT_A_GUEST = {"detail": "That is not a guest id."}
CLAIM = "/api/v1/games/claim"


@pytest.fixture(scope="module")
def engine() -> PulkyEngine:
    return play_game()


def two_games_of_one_guest(
    factory: Callable[[], Session], engine: PulkyEngine, guest: str
) -> list[str]:
    """P1 is ``guest`` in two games; P2 is another guest in both."""
    ids = []
    for n, (started, finished) in enumerate(
        [
            ("2026-10-12T14:00:00Z", "2026-10-12T15:00:00Z"),
            ("2026-10-13T14:00:00Z", "2026-10-13T15:00:00Z"),
        ]
    ):
        room = make_room(
            engine,
            code=f"CLM{n}22",
            identities={"P1": guest, "P2": guest_id(500 + n)},
            started_at=started,
            finished_at=finished,
        )
        ids.append(persist(room, factory))
    return ids


def participant_rows(db: Session, guest: str | None = None) -> list[dict[str, Any]]:
    db.rollback()
    rows = (
        db.execute(
            text(
                "SELECT game_id, alias, user_id, guest_identity FROM participants "
                "ORDER BY game_id, alias"
            )
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]


def user_id_of(db: Session, uid: str) -> int | None:
    return scalar(db, "SELECT id FROM users WHERE firebase_uid = :u", u=uid)


# --------------------------------------------------- 3.5


@pytest.mark.dbschema
def test_ac5_claim_attaches_every_guest_row_and_rebuilds_stats(
    api: TestClient,
    factory: Callable[[], Session],
    db_session: Session,
    firebase_tokens: dict[str, Any],
    engine: PulkyEngine,
) -> None:
    guest = guest_id(1)
    two_games_of_one_guest(factory, engine, guest)
    bearer = sign_in(firebase_tokens, "uid-claimer", name="Claimer")
    res = api.post(CLAIM, json={"guest_identity": guest}, headers=bearer)
    assert res.status_code == 200
    assert res.json() == {"claimed": 2}
    uid_pk = user_id_of(db_session, "uid-claimer")
    assert uid_pk is not None  # the user was created from the verified uid
    rows = participant_rows(db_session)
    p1 = [r for r in rows if r["alias"] == "P1"]
    p2 = [r for r in rows if r["alias"] == "P2"]
    assert [r["user_id"] for r in p1] == [uid_pk, uid_pk]
    assert [r["guest_identity"] for r in p1] == [None, None]
    # the other guest is untouched
    assert all(r["user_id"] is None and r["guest_identity"] for r in p2)
    # stats were rebuilt for the claimer
    db_session.rollback()
    played, wins = db_session.execute(
        text("SELECT games_played, wins FROM user_stats WHERE user_id = :u"),
        {"u": uid_pk},
    ).one()
    leaderboard = engine.results()["leaderboard"]
    p1_rank = next(r["rank"] for r in leaderboard if r["alias"] == "P1")
    assert played == 2
    assert wins == (2 if p1_rank == 1 else 0)
    # and the profile now lists both
    games = api.get("/api/v1/users/me/games", headers=bearer).json()["games"]
    assert len(games) == 2


@pytest.mark.dbschema
def test_ac5_claim_is_idempotent(
    api: TestClient,
    factory: Callable[[], Session],
    db_session: Session,
    firebase_tokens: dict[str, Any],
    engine: PulkyEngine,
) -> None:
    guest = guest_id(1)
    two_games_of_one_guest(factory, engine, guest)
    bearer = sign_in(firebase_tokens, "uid-claimer")
    assert api.post(CLAIM, json={"guest_identity": guest}, headers=bearer).json() == {
        "claimed": 2
    }
    before = participant_rows(db_session)
    stats_before = scalar(db_session, "SELECT games_played FROM user_stats")
    again = api.post(CLAIM, json={"guest_identity": guest}, headers=bearer)
    assert again.status_code == 200 and again.json() == {"claimed": 0}
    assert participant_rows(db_session) == before
    assert scalar(db_session, "SELECT games_played FROM user_stats") == stats_before
    assert scalar(db_session, "SELECT COUNT(*) FROM users") == 1


@pytest.mark.dbschema
def test_claiming_nothing_is_a_valid_answer(
    api: TestClient, firebase_tokens: dict[str, Any]
) -> None:
    bearer = sign_in(firebase_tokens, "uid-claimer")
    res = api.post(CLAIM, json={"guest_identity": guest_id(77)}, headers=bearer)
    assert res.status_code == 200 and res.json() == {"claimed": 0}


@pytest.mark.dbschema
def test_fm5_a_row_that_already_has_a_user_is_never_touched(
    api: TestClient,
    factory: Callable[[], Session],
    db_session: Session,
    firebase_tokens: dict[str, Any],
    engine: PulkyEngine,
) -> None:
    guest = guest_id(1)
    two_games_of_one_guest(factory, engine, guest)
    owner = get_or_create_user(db_session, "uid-owner", "Owner")
    db_session.commit()
    owner_pk = owner.id
    # State no public call produces: one of the two rows belongs to an account
    # and still carries the guest id (the claim must not move it).
    db_session.execute(
        text(
            "UPDATE participants SET user_id = :u WHERE alias = 'P1' AND game_id = "
            "(SELECT MIN(id) FROM games)"
        ),
        {"u": owner_pk},
    )
    db_session.commit()
    bearer = sign_in(firebase_tokens, "uid-thief")
    res = api.post(CLAIM, json={"guest_identity": guest}, headers=bearer)
    assert res.status_code == 200 and res.json() == {"claimed": 1}
    thief = user_id_of(db_session, "uid-thief")
    rows = [r for r in participant_rows(db_session) if r["alias"] == "P1"]
    owned = next(r for r in rows if r["user_id"] == owner_pk)
    assert owned["guest_identity"] == guest  # untouched in every column
    other = next(r for r in rows if r is not owned)
    assert other["user_id"] == thief and other["guest_identity"] is None


@pytest.mark.dbschema
def test_second_account_cannot_claim_what_the_first_already_claimed(
    api: TestClient,
    factory: Callable[[], Session],
    db_session: Session,
    firebase_tokens: dict[str, Any],
    engine: PulkyEngine,
) -> None:
    guest = guest_id(1)
    two_games_of_one_guest(factory, engine, guest)
    first = sign_in(firebase_tokens, "uid-first")
    second = sign_in(firebase_tokens, "uid-second")
    assert api.post(CLAIM, json={"guest_identity": guest}, headers=first).json() == {
        "claimed": 2
    }
    assert api.post(CLAIM, json={"guest_identity": guest}, headers=second).json() == {
        "claimed": 0
    }
    first_pk = user_id_of(db_session, "uid-first")
    assert {
        r["user_id"] for r in participant_rows(db_session) if r["alias"] == "P1"
    } == {first_pk}


@pytest.mark.dbschema
def test_fm4_the_uid_comes_from_the_token_never_from_the_request(
    api: TestClient,
    factory: Callable[[], Session],
    db_session: Session,
    firebase_tokens: dict[str, Any],
    engine: PulkyEngine,
) -> None:
    guest = guest_id(1)
    two_games_of_one_guest(factory, engine, guest)
    get_or_create_user(db_session, "uid-victim", "Victim")
    db_session.commit()
    victim_pk = user_id_of(db_session, "uid-victim")
    bearer = sign_in(firebase_tokens, "uid-caller")
    res = api.post(
        CLAIM + f"?uid=uid-victim&firebase_uid=uid-victim&user_id={victim_pk}",
        json={
            "guest_identity": guest,
            "uid": "uid-victim",
            "firebase_uid": "uid-victim",
            "user_id": victim_pk,
        },
        headers=bearer,
    )
    assert res.status_code == 200 and res.json() == {"claimed": 2}
    caller = user_id_of(db_session, "uid-caller")
    assert caller is not None and caller != victim_pk
    assert {
        r["user_id"] for r in participant_rows(db_session) if r["alias"] == "P1"
    } == {caller}
    assert (
        scalar(
            db_session,
            "SELECT COUNT(*) FROM user_stats WHERE user_id = :u",
            u=victim_pk,
        )
        == 0
    )


@pytest.mark.parametrize(
    "bad",
    [
        "u1",
        "",
        "uid-abcdefghijklmnopqrstuvwx",
        "guest_",
        "guest_1234",
        "guest_" + "A" * 36,  # uppercase is not a uuid4 string
        "guest_" + "a" * 35,
        "guest_" + "a" * 37,
        "Guest_" + "a" * 36,
        " guest_" + "a" * 36,
        "guest_" + "g" * 36,
    ],
)
@pytest.mark.dbschema
def test_a_value_that_is_not_a_guest_id_is_400(
    bad: str, api: TestClient, firebase_tokens: dict[str, Any]
) -> None:
    bearer = sign_in(firebase_tokens, "uid-claimer")
    res = api.post(CLAIM, json={"guest_identity": bad}, headers=bearer)
    assert res.status_code == 400
    assert res.json() == NOT_A_GUEST


@pytest.mark.dbschema
def test_a_guest_id_in_the_documented_shape_is_accepted(
    api: TestClient, firebase_tokens: dict[str, Any]
) -> None:
    bearer = sign_in(firebase_tokens, "uid-claimer")
    for good in (guest_id(1), "guest_" + "0123456789abcdef" * 2 + "0123"):
        res = api.post(CLAIM, json={"guest_identity": good}, headers=bearer)
        assert res.status_code == 200, good


@pytest.mark.dbschema
def test_a_refused_claim_changes_nothing(
    api: TestClient,
    factory: Callable[[], Session],
    db_session: Session,
    firebase_tokens: dict[str, Any],
    engine: PulkyEngine,
) -> None:
    two_games_of_one_guest(factory, engine, guest_id(1))
    before = participant_rows(db_session)
    bearer = sign_in(firebase_tokens, "uid-claimer")
    assert (
        api.post(CLAIM, json={"guest_identity": "u1"}, headers=bearer).status_code
        == 400
    )
    assert participant_rows(db_session) == before


@pytest.mark.dbschema
def test_the_service_counts_rows_claimed(
    api: TestClient,
    factory: Callable[[], Session],
    db_session: Session,
    engine: PulkyEngine,
) -> None:
    guest = guest_id(1)
    two_games_of_one_guest(factory, engine, guest)
    assert claim_guest_results(db_session, "uid-svc", guest) == 2
    db_session.commit()
    assert claim_guest_results(db_session, "uid-svc", guest) == 0


@pytest.mark.dbschema
def test_ac3_a_claim_answer_carries_nothing_but_the_count(
    api: TestClient,
    factory: Callable[[], Session],
    firebase_tokens: dict[str, Any],
    engine: PulkyEngine,
) -> None:
    guest = guest_id(1)
    ids = {"P1": guest, "P2": guest_id(2)}
    room = make_room(engine, identities=ids)
    persist(room, factory)
    bearer = sign_in(firebase_tokens, "uid-claimer")
    res = api.post(CLAIM, json={"guest_identity": guest}, headers=bearer)
    assert res.json() == {"claimed": 1}
    assert_no_secrets(res.text, [*secrets_of(room), "uid-claimer"])
    assert_no_secrets(dict(res.headers), [*secrets_of(room), "uid-claimer"])


# --------------------------------------------------- auth (AC 6)


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Token abc"},
        {"Authorization": "Bearer"},
        {"Authorization": "Bearer not-a-valid-token"},
    ],
)
def test_claim_without_a_valid_bearer_is_401(
    headers: dict[str, str],
    bare_api: TestClient,
    firebase_tokens: dict[str, Any],
) -> None:
    firebase_tokens["token-of-someone"] = {"uid": "someone"}
    res = bare_api.post(CLAIM, json={"guest_identity": guest_id(1)}, headers=headers)
    assert res.status_code == 401 and res.json() == NEED_SIGN_IN


def test_claim_is_503_when_firebase_is_unconfigured(bare_api: TestClient) -> None:
    res = bare_api.post(
        CLAIM,
        json={"guest_identity": guest_id(1)},
        headers={"Authorization": "Bearer anything"},
    )
    assert res.status_code == 503 and res.json() == NO_SIGN_IN
