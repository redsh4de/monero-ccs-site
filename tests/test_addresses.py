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


@st.composite
def qr_line(draw):
    """One QR line as the backend pushes it: an address, then its code's hash."""
    code = draw(fixed("0123456789abcdef", 64))
    return f"qr {draw(addresses)} sha256={code}"


@given(st.lists(value_line() | qr_line(), max_size=8))
def test_no_unlisted_address_reaches_a_page(lines):
    pushed = "".join(f"{line}\n" for line in lines) + f"updated {NOW}\n"
    values = checks.check_values(pushed.encode(), last=0, now=NOW)
    shown = checks.apply_list(values, listed={LISTED})
    assert set(ADDRESS.findall(shown)) <= {LISTED}


def test_general_funds_qr_line_survives_the_list(tmp_path, capsys):
    fund = "4" + "C" * 94
    line = f"qr {fund} sha256={'a' * 64}\n"
    (tmp_path / "list").write_text(f"{LISTED}\ngeneral-fund {fund}\n")
    (tmp_path / "values").write_text(line)
    assert checks.main(["apply", str(tmp_path / "values"), str(tmp_path / "list")]) == 0
    assert capsys.readouterr().out == line
