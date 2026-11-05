"""Whatever the backend pushes, a page shows only addresses from the signed list."""

import re

from hypothesis import given
from hypothesis import strategies as st

import checks

ADDRESS = re.compile(r"[48][1-9A-HJ-NP-Za-km-z]{94}")
LISTED = "8" + "A" * 94
UNLISTED = "4" + "B" * 94
NOW = 1_800_000_000
any_address = st.from_regex(ADDRESS, fullmatch=True)


@st.composite
def value_line(draw):
    """One line as the backend pushes it: id, amount raised, contributions, address."""
    proposal = draw(st.from_regex(r"[0-9a-f]{16}", fullmatch=True))
    raised = draw(st.from_regex(r"[0-9]{1,12}(\.[0-9]{1,12})?", fullmatch=True))
    contributions = draw(st.integers(0, 999_999_999))
    address = draw(st.sampled_from([LISTED, UNLISTED, "-"]) | any_address)
    return f"{proposal} {raised} {contributions} {address}"


@given(st.lists(value_line(), max_size=8))
def test_no_unlisted_address_reaches_a_page(lines):
    pushed = "".join(f"{line}\n" for line in lines) + f"updated {NOW}\n"
    values = checks.check_values(pushed.encode(), last=0, now=NOW)
    shown = checks.apply_list(values, listed={LISTED})
    assert set(ADDRESS.findall(shown)) <= {LISTED}
