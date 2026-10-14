"""Fixtures and helpers shared by the section 15 socket suites.

Everything here goes through public surfaces: the section 01 fakes, the
section 13 state service, the section 14 room rules and the section 15
handlers themselves.
"""

import json
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

import pytest

from app.core.config_models import GameConfig
from app.services import game_service
from app.services.state_service import get_state_service, reset_state_service
from app.sockets.handlers.connection import connect
from app.sockets.handlers.lobby import (
    join,
    join_waiting,
    leave,
    start_game,
)
from tests.conftest import FakeSocketManager, FirebaseTokens

HOST_NAME = "Ms. Ruiz"

# Seven secret values that appear nowhere else in a game (spec 9.3), plus two
# not-shown values (spec 9.3), all valid under spec 9.1's bounds.
SECRET_PATCH: dict[str, Any] = {
    "random_seed": 918273645,
    "supplier": {
        "capacity_sd_per_player": 4.321,
        "disruption_probability": 0.0371,
        "spot_spread": 0.2468,
    },
    "transport": {"failure_probability": 0.137, "loss_fraction": 0.271},
    "insurance": {"target_margin": 0.123, "reference_shipment": 9871},
    "stock": {"holding_cost": 0.2137},
}
SECRET_VALUES: tuple[Any, ...] = (
    918273645,
    4.321,
    0.0371,
    0.137,
    0.271,
    0.123,
    9871,
)
SECRET_KEY_NAMES: tuple[str, ...] = (
    "random_seed",
    "capacity_sd_per_player",
    "disruption_probability",
    "failure_probability",
    "loss_fraction",
    "target_margin",
    "reference_shipment",
)


def guest(n: int) -> str:
    """A deterministic guest id that matches the handshake's guest pattern."""
    return f"guest_{uuid.UUID(int=n)}"


def norm(payload: Any) -> Any:
    """JSON round trip, so tuples and lists compare as they do on the wire."""
    return json.loads(json.dumps(payload))


def leaf_paths(tree: Any, prefix: str = "") -> set[str]:
    """Dot paths of every non-dict leaf of a nested dict."""
    if isinstance(tree, dict):
        out: set[str] = set()
        for key, value in tree.items():
            out |= leaf_paths(value, f"{prefix}.{key}" if prefix else str(key))
        return out
    return {prefix}


def all_keys(payload: Any) -> Iterator[str]:
    if isinstance(payload, dict):
        for key, value in payload.items():
            yield str(key)
            yield from all_keys(value)
    elif isinstance(payload, (list, tuple)):
        for item in payload:
            yield from all_keys(item)


@dataclass
class Game:
    code: str
    secret: str
    host_sid: str
    sids: dict[str, str] = field(default_factory=dict)  # alias -> sid
    tokens: dict[str, str] = field(default_factory=dict)  # alias -> session token

    def start_data(self) -> dict[str, str]:
        return {"room_id": self.code, "host_secret": self.secret}


class World:
    """The recorded socket manager plus the helpers every socket test needs."""

    def __init__(self, sm: FakeSocketManager, tokens: FirebaseTokens) -> None:
        self.sm = sm
        self.tokens = tokens

    # ------------------------------------------------------------- recording
    def mark(self) -> int:
        return len(self.sm.records)

    def sent(
        self, target: str, event: str | None = None, since: int = 0
    ) -> list[dict[str, Any]]:
        """Payloads recorded for ``target`` (a sid or a room code), in order."""
        return [
            r[2]
            for r in self.sm.records[since:]
            if r[0] == target and (event is None or r[1] == event)
        ]

    def reaching(
        self, sid: str, code: str, since: int = 0
    ) -> list[tuple[str, dict[str, Any]]]:
        """(event, data) of every emit that reaches ``sid``, in order."""
        return [
            (r[1], r[2])
            for r in self.sm.records[since:]
            if r[0] == sid or (r[0] == code and r[3] != sid)
        ]

    def reaching_events(self, sid: str, code: str, since: int = 0) -> list[str]:
        return [e for e, _ in self.reaching(sid, code, since)]

    def last(self, target: str, event: str) -> dict[str, Any]:
        found = self.sent(target, event)
        assert found, f"no {event} was sent to {target}"
        return found[-1]

    # ------------------------------------------------------------------ rooms
    async def create_room(self, patch: dict[str, Any] | None = None) -> str:
        config = GameConfig.from_host_input(patch or {}).to_dict()
        svc = get_state_service()
        room = await svc.create_room(config, HOST_NAME)
        code = str(room["room_code"])
        if not await svc.room_exists(code):
            await svc.save_room(code, room)
        return code

    async def room(self, code: str) -> dict[str, Any]:
        room = await get_state_service().get_room(code)
        assert room is not None
        return room

    async def mutate(self, code: str, change: Callable[[dict[str, Any]], None]) -> None:
        svc = get_state_service()
        async with svc.lock(code):
            room = await svc.get_room(code)
            assert room is not None
            change(room)
            await svc.save_room(code, room)

    async def finish_early(self, code: str, game_id: str | None = None) -> None:
        """Reach a finished room through the engine's public end_early()."""
        svc = get_state_service()
        async with svc.lock(code):
            room = await svc.get_room(code)
            assert room is not None
            engine = game_service.load_engine(room)
            assert engine is not None
            engine.end_early()
            game_service.store_engine(room, engine)
            room["state"] = "finished"
            room["finished_at"] = "2026-10-12T15:00:00Z"
            room["game_id"] = game_id
            await svc.save_room(code, room)

    # ---------------------------------------------------------------- sockets
    async def connect(self, sid: str, who: int | str | None = None) -> str:
        """Handshake ``sid``. int -> guest id, str -> Firebase uid, None -> no auth."""
        auth: dict[str, str] | None = None
        if isinstance(who, int):
            auth = {"guestId": guest(who)}
        elif isinstance(who, str):
            self.tokens[f"token-{who}"] = {"uid": who}
            auth = {"idToken": f"token-{who}"}
        result = await connect(sid, {}, auth)
        assert result is not False
        return self.sm.sid_to_identity[sid]

    async def claim_host(
        self,
        code: str,
        sid: str = "host-1",
        who: int | str | None = 900,
        *,
        with_secret: bool = True,
    ) -> None:
        await self.connect(sid, who)
        data: dict[str, Any] = {"room_id": code}
        if with_secret:
            data["host_secret"] = (await self.room(code))["host_secret"]
        await join_waiting(sid, data)

    async def join(
        self,
        code: str,
        sid: str,
        who: int | str | None,
        *,
        name: str | None = None,
        token: str | None = None,
    ) -> list[dict[str, Any]]:
        """Handshake and join; returns what the sid received, in order."""
        await self.connect(sid, who)
        data: dict[str, Any] = {"room_id": code}
        if name is not None:
            data["display_name"] = name
        if token is not None:
            data["session_token"] = token
        start = self.mark()
        await join(sid, data)
        return self.sent(sid, since=start)

    async def seat(
        self,
        code: str,
        sid: str,
        who: int | str,
        *,
        name: str | None = None,
        token: str | None = None,
    ) -> dict[str, Any]:
        """Join and return the ``joined`` payload (fails when not seated)."""
        await self.join(code, sid, who, name=name, token=token)
        return self.last(sid, "joined")

    async def leave(self, code: str, sid: str) -> None:
        await leave(sid, {"room_id": code})

    async def running_game(
        self, patch: dict[str, Any] | None = None, players: int = 2
    ) -> Game:
        code = await self.create_room(patch)
        secret = str((await self.room(code))["host_secret"])
        game = Game(code=code, secret=secret, host_sid="host-1")
        await self.claim_host(code, game.host_sid)
        for n in range(1, players + 1):
            sid = f"p{n}"
            joined = await self.seat(code, sid, n)
            game.sids[joined["alias"]] = sid
            game.tokens[joined["alias"]] = joined["session_token"]
        await start_game(game.host_sid, game.start_data())
        assert (await self.room(code))["state"] == "running"
        return game


@pytest.fixture
def world(
    fake_socket_manager: FakeSocketManager,
    memory_backend: Any,
    firebase_tokens: FirebaseTokens,
) -> Iterator[World]:
    reset_state_service()
    yield World(fake_socket_manager, firebase_tokens)
    reset_state_service()
