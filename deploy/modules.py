"""Load the deploy's modules from their own source, which the signers read.

An import would take a folder or a cached .pyc of the same name first. So each
script runs this file instead, and then imports the modules by name.
"""

import sys
from pathlib import Path
from types import ModuleType

DEPLOY = Path(__file__).resolve().parent
# each comes before its importers
RUNS = {
    "safety": "render/safety.py",
    "qr": "render/qr.py",
    "guards": "guards/__init__.py",
    "guards.common": "guards/common.py",
    "guards.style": "guards/style.py",
    "guards.values": "guards/values.py",
    "guards.snapshot": "guards/snapshot.py",
    "guards.build": "guards/build.py",
    "guards.lockfile": "guards/lockfile.py",
    "guards.site": "guards/site.py",
    "guards.keys": "guards/keys.py",
    "guards.release": "guards/release.py",
    "lib": "lib.py",
    "alerts": "alerts.py",
}


def load(name: str, file: str) -> None:
    """Run one of RUNS as the module that the others import by its name."""
    module = sys.modules[name] = ModuleType(name)
    module.__file__ = str(DEPLOY / file)
    parent, _, child = name.rpartition(".")
    if parent:
        setattr(sys.modules[parent], child, module)
    code = compile((DEPLOY / file).read_bytes(), module.__file__, "exec")
    exec(code, vars(module))


sys.dont_write_bytecode = True
for name, file in RUNS.items():
    load(name, file)
