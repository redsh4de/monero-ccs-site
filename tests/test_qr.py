"""A donation QR code is drawn only as the backend read it back."""

import sys
from dataclasses import replace
from pathlib import Path

import pytest

import inputs
import qr
import render

ADDRESS = "8" + "A" * 94
OTHER = "4" + "B" * 94


def plant_qrencode(tmp_path, monkeypatch):
    """Put a qrencode first on PATH that draws another address's code."""
    planted = tmp_path / "bin" / "qrencode"
    planted.parent.mkdir()
    planted.write_text(f'#!/bin/sh\nexec /usr/bin/qrencode -t ASCII "monero:{OTHER}"\n')
    planted.chmod(0o755)
    monkeypatch.setenv("PATH", f"{planted.parent}:/usr/bin:/bin")


def test_planted_qrencode_fails_the_backends_read_back(tmp_path, monkeypatch):
    plant_qrencode(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="doesn't read back"):
        qr.read_back(ADDRESS)


def test_planted_qrencode_on_the_web_server_draws_nothing(tmp_path, monkeypatch):
    pushed = qr.read_back(ADDRESS)
    plant_qrencode(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="isn't the one the backend read"):
        qr.write(ADDRESS, tmp_path / "qr.png", pushed)
    assert not (tmp_path / "qr.png").exists()


def test_drawing_needs_no_decoder(tmp_path, monkeypatch):
    pushed = qr.read_back(ADDRESS)
    # as on the web server, which has no zbar
    monkeypatch.setitem(sys.modules, "zbar", None)
    qr.write(ADDRESS, tmp_path / "qr.png", pushed)
    assert (tmp_path / "qr.png").read_bytes().startswith(b"\x89PNG")


def test_box_whose_code_wasnt_read_back_stays_closed():
    given = inputs.read(Path("example-data"))
    assert render.build_site(given).addresses_shown
    unread = replace(given, qr_hashes={})
    assert render.build_site(unread).addresses_shown == set()
