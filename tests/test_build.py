"""A build can't carry a font or image that isn't byte for byte a file of the tag."""

import pytest

import checks

LOGO = b"\x89PNG the logo as committed"


@pytest.fixture
def tag(tmp_path):
    """The tag's own files, with the logo in src/assets."""
    folder = tmp_path / "tag"
    (folder / "src/assets").mkdir(parents=True)
    (folder / "src/assets/logo.png").write_bytes(LOGO)
    return folder


def build(tmp_path, logo):
    """Return a built site holding one page and the logo, with no hosts listed."""
    dist = tmp_path / "dist"
    (dist / "_astro").mkdir(parents=True)
    (dist / "index.html").write_text("<p>the site</p>")
    (dist / "_astro/logo.Bx1Kq3.png").write_bytes(logo)
    (tmp_path / "hosts").write_text("")
    return dist


def test_build_of_the_tags_own_images_passes(tmp_path, tag):
    dist = build(tmp_path, LOGO)
    checks.check_build(dist, tmp_path / "hosts", tag)


def test_planted_image_is_refused(tmp_path, tag):
    dist = build(tmp_path, b"\x89PNG an image of another address")
    with pytest.raises(checks.Reject, match="logo.Bx1Kq3.png isn't byte for byte"):
        checks.check_build(dist, tmp_path / "hosts", tag)
