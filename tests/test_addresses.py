"""Whatever the backend pushes, a page shows only addresses from the signed list."""

import re

from hypothesis import given
from hypothesis import strategies as st

import checks

BASE58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
ADDRESS = re.compile(rf"[48][{BASE58}]{{94}}")
LISTED = "8" + "A" * 94
UNLISTED = "4" + "B" * 94
NOW = 1_800_000_000


def fixed(alphabet, size):
    return st.text(alphabet, min_size=size, max_size=size)


any_address = st.builds(str.__add__, st.sampled_from("48"), fixed(BASE58, 94))
addresses = st.sampled_from([LISTED, UNLISTED]) | any_address


@st.composite
def value_line(draw):
    """One proposal's line as the backend pushes it."""
    name = draw(st.text("abc%2B-", min_size=1, max_size=40))
    whole, fraction = draw(st.integers(0, 10**12 - 1)), draw(st.integers(0, 10**12 - 1))
    raised = draw(st.sampled_from([f"{whole}", f"{whole}.{fraction}"]))
    contributions = draw(st.integers(0, 999_999_999))
    address = draw(addresses | st.just("-"))
    return (
        f"proposal {name} raised={raised} contributions={contributions}"
        f" address={address}"
    )


@given(st.lists(value_line(), max_size=8))
def test_no_unlisted_address_reaches_a_page(lines):
    pushed = "".join(f"{line}\n" for line in lines) + f"updated {NOW}\n"
    values = checks.check_values(pushed.encode(), last=0, now=NOW)
    shown = checks.apply_list(values, {LISTED})
    assert set(ADDRESS.findall(shown)) <= {LISTED}


FIRST, BOTH, LAST = ("8" + c * 94 for c in "CDE")
OLD_LIST, NEW_LIST = {FIRST, BOTH}, {BOTH, LAST}
NAMES = ["x", "y"]
pair = st.tuples(st.sampled_from(NAMES), st.sampled_from([FIRST, BOTH, LAST, "-"]))


@given(st.lists(st.lists(pair, max_size=3), max_size=6), st.integers(0, 6))
def test_a_shown_address_stays_with_its_proposal(pushes, new_list_from):
    """A push is accepted only when every address shown has one proposal, and
    every proposal has at most one address that the list still holds."""
    pins, shown = {}, set()
    for i, push in enumerate(pushes):
        listed = OLD_LIST if i < new_list_from else NEW_LIST
        lines = [f"proposal {n} raised=0 contributions=0 address={a}" for n, a in push]
        values = checks.check_values(
            "".join(f"{line}\n" for line in [*lines, f"updated {NOW}"]).encode(),
            last=0,
            now=NOW,
        )
        rows = [row.split(" ") for row in values.splitlines()[:-1]]
        pairs = {(r[1], r[-1].removeprefix("address=")) for r in rows}
        seen = shown | {(n, a) for n, a in pairs if a in listed}
        allowed = len({a for _, a in seen}) == len(seen) and all(
            len({a for m, a in seen if m == n and a in listed}) <= 1 for n in NAMES
        )
        try:
            pins = checks.keep_pins(values, listed, pins)
        except checks.Reject:
            assert not allowed
            continue
        assert allowed
        shown = seen
