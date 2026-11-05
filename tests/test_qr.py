"""A donation QR code is drawn only if it reads back as its own address."""

import pytest

import qr

ADDRESS = "8" + "A" * 94
OTHER = "4" + "B" * 94


def test_code_is_drawn_for_its_address(tmp_path):
    qr.write(ADDRESS, tmp_path / "qr.png")
    assert (tmp_path / "qr.png").read_bytes().startswith(b"\x89PNG")


def test_planted_qrencode_stops_the_render(tmp_path, monkeypatch):
    planted = tmp_path / "bin" / "qrencode"
    planted.parent.mkdir()
    # draws another address's code, whatever it is asked for
    planted.write_text(f'#!/bin/sh\nexec /usr/bin/qrencode -t ASCII "monero:{OTHER}"\n')
    planted.chmod(0o755)
    monkeypatch.setenv("PATH", f"{planted.parent}:/usr/bin:/bin")
    with pytest.raises(ValueError, match="doesn't read back"):
        qr.write(ADDRESS, tmp_path / "qr.png")
    assert not (tmp_path / "qr.png").exists()
