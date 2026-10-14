"""Seeded random streams (spec §10): the one place a generator is built."""

import random


def stream(seed: int, day: int, purpose: str) -> random.Random:
    return random.Random(f"{seed}:{day}:{purpose}")
