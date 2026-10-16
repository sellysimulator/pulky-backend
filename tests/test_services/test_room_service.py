"""Black-box tests for the shared room rules (section 14)."""

import copy
from typing import Any

import pytest

from app.core import config_models
from app.core.config_models import MSG_CONFIG_LOCKED, GameConfig
from app.core.defaults import DEFAULT_CONFIG
from app.core.limits import MAX_DISPLAY_NAME, MIN_PLAYERS_TO_START
from app.services import room_service
from app.services.room_service import (
    START_ALREADY,
    START_NEED_PLAYERS,
    STATUS_FINISHED,
    STATUS_FULL,
    STATUS_NOT_FOUND,
    STATUS_STARTED,
    ConfigLocked,
    Emit,
    apply_config_update,
    can_start,
    check_host_identity,
    check_host_secret,
    config_locked,
    find_alias_for_identity,
    find_alias_for_reconnect,
    lobby_payload,
    room_status,
    sanitise_display_name,
    seats_taken,
    send,
)
from app.services.state_service import new_room_document
from tests.conftest import FakeSocketManager, contains_key, contains_value

CODE = "ABC234"
HOST_SID = "sid-host-0001"
FALLBACK = "Pulquería P2"


def make_room(
    players: int = 0,
    state: str = "lobby",
    *,
    host_secret: str = "host-secret-xyz",
    config_patch: dict[str, Any] | None = None,
    connected: bool = True,
) -> dict:
    config = copy.deepcopy(DEFAULT_CONFIG)
    if config_patch:
        config = GameConfig.from_host_input(config_patch).to_dict()
    room = new_room_document(
        room_code=CODE,
        config=config,
        host_display_name="Ms. Ruiz",
        host_secret=host_secret,
        created_at="2026-10-12T14:03:22Z",
    )
    room["state"] = state
    for n in range(1, players + 1):
        alias = f"P{n}"
        room["participants"][alias] = {
            "alias": alias,
            "identity": f"identity-secret-{n}",
            "session_token": f"token-secret-{n}",
            "display_name": f"Pulquería {alias}",
            "connected": connected,
        }
    return room


def next_seq(room: dict) -> int:
    room["seq"] += 1
    return int(room["seq"])


# ---------------------------------------------------------------- host secret


def test_check_host_secret_examples() -> None:
    assert check_host_secret({"host_secret": "abc"}, {"host_secret": "abc"}) is True
    assert check_host_secret({"host_secret": "abc"}, {"host_secret": "abd"}) is False
    assert check_host_secret({"host_secret": "abc"}, None) is False
    assert check_host_secret({"host_secret": "abc"}, {}) is False
    assert check_host_secret({"host_secret": "abc"}, {"host_secret": ""}) is False


@pytest.mark.parametrize(
    "room",
    [{"host_secret": ""}, {"host_secret": None}, {}],
    ids=["empty", "none", "missing"],
)
@pytest.mark.parametrize(
    "data",
    [None, {}, {"host_secret": ""}, {"host_secret": None}],
    ids=["no-data", "no-key", "empty", "none"],
)
def test_check_host_secret_empty_stored_secret_never_authorises(
    room: dict, data: dict | None
) -> None:
    assert check_host_secret(room, data) is False


def test_check_host_identity() -> None:
    room = {"host_identity": "uid-1"}
    assert check_host_identity(room, "uid-1") is True
    assert check_host_identity(room, "uid-2") is False
    assert check_host_identity(room, None) is False
    assert check_host_identity(room, "") is False


@pytest.mark.parametrize("stored", [None, "", "missing"])
def test_check_host_identity_empty_stored_never_matches(stored: str | None) -> None:
    room: dict[str, Any] = {} if stored == "missing" else {"host_identity": stored}
    assert check_host_identity(room, None) is False
    assert check_host_identity(room, "") is False
    assert check_host_identity(room, "uid-1") is False


# ------------------------------------------------------------ alias lookups


def test_find_alias_for_reconnect_matches_token_and_identity() -> None:
    room = make_room(3)
    assert find_alias_for_reconnect(room, "token-secret-2", "identity-secret-2") == "P2"


def test_reconnect_token_replay_with_another_identity_is_refused() -> None:
    room = make_room(3)
    assert find_alias_for_reconnect(room, "token-secret-2", "identity-secret-3") is None
    assert find_alias_for_reconnect(room, "token-secret-2", "someone-else") is None
    assert find_alias_for_reconnect(room, "token-secret-2", None) is None


def test_reconnect_wrong_token_is_refused() -> None:
    room = make_room(2)
    assert find_alias_for_reconnect(room, "nope", "identity-secret-1") is None


def test_reconnect_never_matches_on_empty_values() -> None:
    room = make_room(2)
    room["participants"]["P1"]["session_token"] = ""
    room["participants"]["P1"]["identity"] = ""
    for token, identity in [("", ""), (None, None), ("", "identity-secret-2")]:
        assert find_alias_for_reconnect(room, token, identity) is None


def test_find_alias_for_identity() -> None:
    room = make_room(3)
    assert find_alias_for_identity(room, "identity-secret-3") == "P3"
    assert find_alias_for_identity(room, "stranger") is None
    assert find_alias_for_identity(room, None) is None
    assert find_alias_for_identity(room, "") is None


def test_find_alias_for_identity_never_matches_empty_stored_identity() -> None:
    room = make_room(2)
    room["participants"]["P1"]["identity"] = ""
    assert find_alias_for_identity(room, "") is None
    assert find_alias_for_identity(room, None) is None


# ------------------------------------------------------------ display names


def test_sanitise_display_name_examples() -> None:
    assert sanitise_display_name("  La\x07 Chula  ", FALLBACK) == "La Chula"
    assert sanitise_display_name("x" * 40, FALLBACK) == "x" * 24
    assert sanitise_display_name(None, FALLBACK) == FALLBACK
    assert MAX_DISPLAY_NAME == 24


@pytest.mark.parametrize("raw", [None, 12, 1.5, True, ["a"], {"a": 1}, b"bytes"])
def test_sanitise_display_name_non_strings_become_fallback(raw: object) -> None:
    assert sanitise_display_name(raw, FALLBACK) == FALLBACK


@pytest.mark.parametrize("raw", ["", "   ", "\x00\x07", " \x1b "])
def test_sanitise_display_name_empty_result_becomes_fallback(raw: str) -> None:
    assert sanitise_display_name(raw, FALLBACK) == FALLBACK


def test_sanitise_display_name_strips_control_characters() -> None:
    assert sanitise_display_name("a\x00b\x1fc\x7fd", FALLBACK) == "abcd"
    assert sanitise_display_name("a​b", FALLBACK) == "ab"  # format char (Cf)


def test_sanitise_display_name_collapses_whitespace() -> None:
    assert sanitise_display_name("La     Chula   Rosa", FALLBACK) == "La Chula Rosa"


def test_sanitise_display_name_keeps_accents_and_exact_length() -> None:
    assert sanitise_display_name("Pulquería Ñandú", FALLBACK) == "Pulquería Ñandú"
    assert sanitise_display_name("ñ" * 24, FALLBACK) == "ñ" * 24
    assert sanitise_display_name("ñ" * 25, FALLBACK) == "ñ" * 24


# -------------------------------------------------------------------- seats


def test_seats_taken_counts_participants_only() -> None:
    assert seats_taken(make_room(0)) == 0
    room = make_room(3, connected=False)
    room["host_identity"] = "host-uid"
    room["host_sid"] = HOST_SID
    assert seats_taken(room) == 3


def test_can_start_examples() -> None:
    assert MIN_PLAYERS_TO_START == 2
    assert can_start(make_room(1)) == (False, "Waiting for at least 2 players.")
    assert can_start(make_room(0)) == (False, START_NEED_PLAYERS)
    assert can_start(make_room(2)) == (True, None)
    assert can_start(make_room(2, "running")) == (
        False,
        "The game has already started.",
    )
    assert START_ALREADY == "The game has already started."


def test_can_start_states() -> None:
    assert can_start(make_room(3, "configuring")) == (True, None)
    assert can_start(make_room(3, "finished")) == (False, START_ALREADY)
    assert can_start(make_room(0, "running")) == (False, START_ALREADY)
    assert can_start(make_room(1, "finished")) == (False, START_ALREADY)


def test_can_start_counts_disconnected_participants() -> None:
    assert can_start(make_room(2, connected=False)) == (True, None)


# ------------------------------------------------------------ lobby payload


def test_lobby_payload_shape() -> None:
    room = make_room(2)
    room["participants"]["P2"]["connected"] = False
    payload = lobby_payload(room, 7)
    assert payload == {
        "seq": 7,
        "state": "lobby",
        "participants": [
            {"alias": "P1", "display_name": "Pulquería P1", "connected": True},
            {"alias": "P2", "display_name": "Pulquería P2", "connected": False},
        ],
        "seats_total": 4,
        "config_locked": False,
        "can_start": True,
        "start_blocked_reason": None,
    }


def test_lobby_payload_blocked_and_locked() -> None:
    payload = lobby_payload(make_room(1), 1)
    assert payload["can_start"] is False
    assert payload["start_blocked_reason"] == START_NEED_PLAYERS
    running = lobby_payload(make_room(3, "running"), 2)
    assert running["config_locked"] is True
    assert running["state"] == "running"
    assert running["start_blocked_reason"] == START_ALREADY


def test_lobby_payload_seats_total_follows_config() -> None:
    room = make_room(2, config_patch={"max_players": 9})
    assert lobby_payload(room, 1)["seats_total"] == 9


def test_lobby_payload_orders_by_alias_number() -> None:
    room = make_room(0)
    for n in (10, 2, 1, 11, 3):
        alias = f"P{n}"
        room["participants"][alias] = {
            "alias": alias,
            "identity": f"identity-secret-{n}",
            "session_token": f"token-secret-{n}",
            "display_name": f"Name {n}",
            "connected": True,
        }
    aliases = [p["alias"] for p in lobby_payload(room, 1)["participants"]]
    assert aliases == ["P1", "P2", "P3", "P10", "P11"]


def test_lobby_payload_carries_no_credential() -> None:
    room = make_room(3)
    room["host_identity"] = "host-identity-secret"
    room["host_sid"] = HOST_SID
    payload = lobby_payload(room, 4)
    for key in ("identity", "session_token", "host_secret", "host_identity"):
        assert not contains_key(payload, key)
    for n in (1, 2, 3):
        assert not contains_value(payload, f"identity-secret-{n}")
        assert not contains_value(payload, f"token-secret-{n}")
    assert not contains_value(payload, "host-secret-xyz")
    assert not contains_value(payload, "host-identity-secret")
    assert "host-secret-xyz" not in repr(payload)
    assert "identity-secret" not in repr(payload)
    assert "token-secret" not in repr(payload)


# --------------------------------------------------------------- room_status


def test_room_status_missing_room() -> None:
    assert room_status(None) == {
        "ok": False,
        "reason": "No room with that code.",
        "state": None,
        "host_display_name": None,
        "seats_taken": 0,
        "seats_total": 0,
    }
    assert STATUS_NOT_FOUND == "No room with that code."


@pytest.mark.parametrize("state", ["lobby", "configuring"])
def test_room_status_open_room(state: str) -> None:
    status = room_status(make_room(2, state))
    assert status == {
        "ok": True,
        "state": state,
        "host_display_name": "Ms. Ruiz",
        "seats_taken": 2,
        "seats_total": 4,
    }
    assert "reason" not in status


def test_room_status_running_and_finished() -> None:
    running = room_status(make_room(2, "running"))
    assert running["ok"] is False
    assert running["reason"] == STATUS_STARTED == "This game has already started."
    assert running["state"] == "running"
    finished = room_status(make_room(2, "finished"))
    assert finished["ok"] is False
    assert finished["reason"] == STATUS_FINISHED == "This game has finished."
    assert finished["state"] == "finished"


@pytest.mark.parametrize("state", ["lobby", "configuring"])
def test_room_status_full_room(state: str) -> None:
    status = room_status(make_room(4, state))
    assert status["ok"] is False
    assert status["reason"] == STATUS_FULL == "This room is full."
    assert status["seats_taken"] == 4
    assert status["seats_total"] == 4


def test_room_status_full_counts_disconnected_participants() -> None:
    status = room_status(make_room(4, connected=False))
    assert status["ok"] is False
    assert status["reason"] == STATUS_FULL


def test_room_status_host_holds_no_seat() -> None:
    room = make_room(3)
    room["host_sid"] = HOST_SID
    room["host_identity"] = "host-uid"
    status = room_status(room)
    assert status["ok"] is True
    assert status["seats_taken"] == 3


def test_room_status_never_carries_the_secret() -> None:
    status = room_status(make_room(2))
    assert not contains_value(status, "host-secret-xyz")
    assert not contains_key(status, "host_secret")


# ------------------------------------------------------------ config_locked


@pytest.mark.parametrize(
    ("state", "locked"),
    [("lobby", False), ("configuring", False), ("running", True), ("finished", True)],
)
def test_config_locked(state: str, locked: bool) -> None:
    assert config_locked(make_room(2, state)) is locked


# ------------------------------------------------------ apply_config_update


def test_apply_config_update_emits_in_order() -> None:
    room = make_room(2)
    room["host_sid"] = HOST_SID
    room["seq"] = 10
    patch = {"demand": {"retail_price": 33}, "max_players": 6}
    emits = apply_config_update(room, patch, next_seq)

    full = GameConfig.from_host_input(patch).to_dict()
    assert room["config"] == full
    assert room["state"] == "configuring"
    assert room["seq"] == 12

    assert len(emits) == 3
    host, shared, lobby = emits
    assert host == Emit(HOST_SID, "config_updated", {"seq": 11, "config": full})
    assert host.skip_sid is None
    assert shared.target == CODE
    assert shared.event == "config_updated"
    assert shared.skip_sid == HOST_SID
    assert shared.data == {
        "seq": 11,
        "config": config_models.config_public(GameConfig.from_dict(full)),
    }
    assert lobby.target == CODE
    assert lobby.event == "lobby_update"
    assert lobby.skip_sid is None
    assert lobby.data == lobby_payload(room, 12)
    assert lobby.data["state"] == "configuring"
    assert lobby.data["seats_total"] == 6


def test_room_config_emit_holds_exactly_the_five_shown_keys() -> None:
    room = make_room(2)
    room["host_sid"] = HOST_SID
    emits = apply_config_update(room, {"demand": {"retail_price": 40}}, next_seq)
    public = emits[1].data["config"]
    assert public == {
        "demand": {"retail_price": 40.0},
        "stock": {
            "conservation_extra_fresh_days": 2,
            "conservation_cost_per_unit": 1.0,
        },
        "peer_market": {"enabled": True},
        "transport": {"unit_cost": 1.0},
    }
    for key in (
        "random_seed",
        "capacity_sd_per_player",
        "disruption_probability",
        "failure_probability",
        "loss_fraction",
        "target_margin",
        "reference_shipment",
        "max_players",
        "base_price",
        "host_secret",
    ):
        assert not contains_key(emits[1].data, key)
    assert "host-secret-xyz" not in repr(emits[1].data)
    # the host's version is the full one
    assert contains_key(emits[0].data, "random_seed")
    assert contains_key(emits[0].data, "failure_probability")


def test_apply_config_update_without_host_sid_has_no_host_emit() -> None:
    room = make_room(2)
    emits = apply_config_update(room, {"starting_cash": 100}, next_seq)
    assert [(e.target, e.event, e.skip_sid) for e in emits] == [
        (CODE, "config_updated", None),
        (CODE, "lobby_update", None),
    ]


def test_apply_config_update_takes_exactly_two_seqs() -> None:
    room = make_room(2)
    room["host_sid"] = HOST_SID
    calls: list[int] = []

    def counting(r: dict) -> int:
        calls.append(1)
        return next_seq(r)

    apply_config_update(room, {"starting_cash": 100}, counting)
    assert len(calls) == 2


def test_apply_config_update_merges_onto_current_config() -> None:
    room = make_room(2, config_patch={"duration_days": 20})
    apply_config_update(room, {"starting_cash": 123}, next_seq)
    assert room["config"]["duration_days"] == 20
    assert room["config"]["starting_cash"] == 123


def test_apply_config_update_keeps_configuring_state() -> None:
    room = make_room(2, "configuring")
    apply_config_update(room, {"starting_cash": 100}, next_seq)
    assert room["state"] == "configuring"


def test_apply_config_update_invalid_patch_leaves_room_untouched() -> None:
    room = make_room(2)
    room["host_sid"] = HOST_SID
    before = copy.deepcopy(room)
    with pytest.raises(config_models.ConfigError) as info:
        apply_config_update(room, {"stock": {"max_days": 1}}, next_seq)
    assert info.value.field == "stock.max_days"
    assert info.value.message == "Must be more than the fresh days (1)."
    assert room == before


@pytest.mark.parametrize("state", ["running", "finished"])
def test_apply_config_update_locked_room_is_refused(state: str) -> None:
    room = make_room(3, state)
    before = copy.deepcopy(room)
    with pytest.raises(ConfigLocked) as info:
        apply_config_update(room, {"starting_cash": 1}, next_seq)
    assert str(info.value) == MSG_CONFIG_LOCKED
    assert room == before


def test_locked_check_wins_over_validation() -> None:
    room = make_room(3, "running")
    with pytest.raises(ConfigLocked):
        apply_config_update(room, {"bogus": 1}, next_seq)


def test_config_locked_is_an_exception_not_a_config_error() -> None:
    assert issubclass(ConfigLocked, Exception)
    assert not issubclass(ConfigLocked, config_models.ConfigError)


# --------------------------------------------------------------------- send


def test_emit_is_a_frozen_dataclass() -> None:
    emit = Emit("sid-1", "x", {"a": 1})
    assert emit.skip_sid is None
    with pytest.raises(AttributeError):
        emit.event = "y"  # type: ignore[misc]


@pytest.mark.asyncio
async def test_send_routes_sid_and_room_emits(
    fake_socket_manager: FakeSocketManager,
) -> None:
    emits = [
        Emit(HOST_SID, "config_updated", {"seq": 1, "config": {"a": 1}}),
        Emit(CODE, "config_updated", {"seq": 1, "config": {}}, skip_sid=HOST_SID),
        Emit(CODE, "lobby_update", {"seq": 2}),
    ]
    await send(emits)
    assert fake_socket_manager.records == [
        (HOST_SID, "config_updated", {"seq": 1, "config": {"a": 1}}, None),
        (CODE, "config_updated", {"seq": 1, "config": {}}, HOST_SID),
        (CODE, "lobby_update", {"seq": 2}, None),
    ]


@pytest.mark.asyncio
async def test_send_nothing_is_a_no_op(
    fake_socket_manager: FakeSocketManager,
) -> None:
    await send([])
    assert fake_socket_manager.records == []


def test_module_exposes_the_documented_names() -> None:
    for name in ("check_host_secret", "apply_config_update", "send", "Emit"):
        assert hasattr(room_service, name)
