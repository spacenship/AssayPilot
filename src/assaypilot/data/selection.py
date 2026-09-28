"""Primary-only candidate selection shared by fetch preparation and build."""
from __future__ import annotations

import random
from collections.abc import Callable, Iterable
from typing import TypeVar


T = TypeVar("T")


def select_primary_sids(
    rows: Iterable[T],
    *,
    include: str,
    limit: int,
    seed: int,
    sid_getter: Callable[[T], int],
    active_getter: Callable[[T], bool],
    ordering_key: Callable[[T], str],
) -> list[int]:
    """Return deterministic unique SIDs using only primary row information.

    ``ordering_key`` is supplied by the caller so the normalized build path
    and the pre-fetch path use exactly the same ordering and seeded sampling.
    No follow-up rows are accepted by this function.
    """
    eligible = [row for row in rows if include == "primary_tested" or active_getter(row)]
    by_sid: dict[int, T] = {}
    for row in sorted(eligible, key=ordering_key):
        by_sid.setdefault(sid_getter(row), row)
    ordered = list(by_sid.values())
    random.Random(seed).shuffle(ordered)
    return [sid_getter(row) for row in ordered[:limit]]
