"""pnpm stays pinned to one version, wherever a tool reads it."""

import json
from pathlib import Path


def test_both_pnpm_pins_name_the_same_version():
    package = json.loads(Path("package.json").read_text())
    version = package["devEngines"]["packageManager"]["version"]
    assert package["packageManager"] == f"pnpm@{version}"
