"""Black-box tests for section 06 Pipeline (spec 5.6)."""

import pytest

from app.core.pipeline import Pipeline


def _cycle(p: Pipeline[str], item: str) -> str:
    p.push(item)
    return p.pop_front()


def test_spec_worked_examples() -> None:
    p = Pipeline(1, ["s0"])
    p.push("s1")
    assert p.pop_front() == "s0"
    p.push("s2")
    assert p.pop_front() == "s1"
    q = Pipeline(0, [])
    q.push("x")
    assert q.pop_front() == "x"


@pytest.mark.parametrize("length", range(0, 8))
def test_item_returns_exactly_length_cycles_later(length: int) -> None:
    initial = [f"init{i}" for i in range(length)]
    p = Pipeline(length, initial)
    assert p.length == length
    assert len(p) == length
    out = []
    for day in range(1, 3 * length + 6):
        p.push(f"d{day}")
        assert len(p) == length + 1
        out.append(p.pop_front())
        assert len(p) == length
        assert p.length == length
    expected = initial + [f"d{day}" for day in range(1, 3 * length + 6)]
    assert out == expected[: len(out)]
    for day in range(1, len(out) - length + 1):
        assert out[day - 1 + length] == f"d{day}"


def test_length_one_is_delivered_next_day_not_same_day_not_two_days() -> None:
    """Failure mode 6: off-by-one in the pipeline."""
    p = Pipeline(1, ["s0"])
    got = {}
    for day in range(1, 6):
        got[day] = _cycle(p, f"s{day}")
    for day in range(1, 5):
        assert got[day + 1] == f"s{day}"
        assert got[day] != f"s{day}"
    assert got[1] == "s0"


def test_same_day_delivery_with_length_zero() -> None:
    p = Pipeline(0, [])
    for day in range(5):
        assert _cycle(p, f"x{day}") == f"x{day}"
    assert len(p) == 0


def test_items_front_first_and_is_a_copy() -> None:
    p = Pipeline(2, ["a", "b"])
    p.push("c")
    assert p.items() == ["a", "b", "c"]
    snapshot = p.items()
    snapshot.append("zzz")
    snapshot.pop(0)
    assert p.items() == ["a", "b", "c"]
    assert len(p) == 3


def test_pop_front_on_empty_raises_index_error() -> None:
    with pytest.raises(IndexError):
        Pipeline(0, []).pop_front()


@pytest.mark.parametrize(
    ("length", "initial"),
    [(-1, []), (1, []), (0, ["a"]), (2, ["a"]), (2, ["a", "b", "c"])],
)
def test_constructor_rejects_bad_length_or_initial(
    length: int, initial: list[str]
) -> None:
    with pytest.raises(ValueError):
        Pipeline(length, initial)


@pytest.mark.parametrize("length", [0, 1, 3, 7])
def test_payload_round_trip_with_encoder_decoder(length: int) -> None:
    p: Pipeline[int] = Pipeline(length, list(range(length)))
    for v in (100, 101, 102):
        p.push(v)
        p.pop_front()

    def encode(x: int) -> dict:
        return {"q": x * 2}

    def decode(d: dict) -> int:
        return d["q"] // 2

    payload = p.to_payload(encode)
    assert payload == {
        "length": length,
        "items": [{"q": x * 2} for x in p.items()],
    }
    q = Pipeline.from_payload(payload, decode)
    assert q.length == length
    assert q.items() == p.items()
    assert len(q) == length
    assert q.to_payload(encode) == payload
    # behaves identically afterwards
    for v in (7, 8, 9):
        p.push(v)
        q.push(v)
        assert p.pop_front() == q.pop_front()


def test_payload_round_trip_between_push_and_pop() -> None:
    p: Pipeline[str] = Pipeline(2, ["a", "b"])
    p.push("c")
    payload = p.to_payload(lambda x: x)
    assert payload["length"] == 2
    assert len(payload["items"]) == 3
    q = Pipeline.from_payload(payload, lambda x: x)
    assert q.length == 2
    assert len(q) == 3
    assert q.items() == ["a", "b", "c"]
    assert q.pop_front() == "a"


@pytest.mark.parametrize(
    ("length", "items"),
    [(2, ["a"]), (2, ["a", "b", "c", "d"]), (0, ["a", "b"]), (3, [])],
)
def test_from_payload_rejects_wrong_item_count(length: int, items: list[str]) -> None:
    with pytest.raises(ValueError):
        Pipeline.from_payload({"length": length, "items": items}, lambda x: x)
