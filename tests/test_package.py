"""pnpm and its policies stay pinned, wherever a tool reads them."""

import json
from pathlib import Path

import yaml


def test_both_pnpm_pins_name_the_same_version():
    package = json.loads(Path("package.json").read_text())
    version = package["devEngines"]["packageManager"]["version"]
    assert package["packageManager"] == f"pnpm@{version}"


def test_release_py_pins_the_supply_chain_policies_of_pnpm_workspace_yaml():
    workspace = yaml.safe_load(Path("pnpm-workspace.yaml").read_text())
    release = Path("deploy/signer/release.py").read_text()
    for key in ("minimumReleaseAge", "trustPolicy", "blockExoticSubdeps"):
        assert f"--config.{key}={str(workspace[key]).lower()}" in release
