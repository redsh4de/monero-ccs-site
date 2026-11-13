"""Write and read status.json, the record of what a render used.

Usage:
  status.py write <input-dir> <version> <commit>  print status.json
  status.py mrs <input-dir>                       print the merge request snapshot,
                                                  without the vote counts
  status.py restore <status.json> <input-dir>     write <input-dir>/mr/open.json

<input-dir> is the render's input folder, which render/README.md describes.

The web server writes status.json after each render. The monitor checks each
part against its own sources, then rebuilds the same input to render again.

Exit: 0 done, 2 usage; a malformed status.json raises.
"""

import json
import sys
from pathlib import Path

# python3 -I leaves this folder off the import path, so the render's own
# modules are found from here
sys.path.insert(0, str(Path(__file__).resolve().parent))
from inputs import read_merge_requests  # noqa: E402


def canonical(value: object) -> str:
    """Return JSON that is the same for equal values."""
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def restore(status: Path, input_dir: Path) -> None:
    """Write the merge request input that a snapshot was taken from."""
    entries = json.loads(status.read_text(encoding="utf-8"))["merge_requests"]
    (input_dir / "mr").mkdir(parents=True, exist_ok=True)
    (input_dir / "mr" / "open.json").write_text(canonical(entries), encoding="utf-8")


def main(argv: list[str]) -> int:
    match argv:
        case ["write", input_dir, version, commit]:
            status = {
                "release": int(version),
                "commit": commit,
                "merge_requests": read_merge_requests(Path(input_dir) / "mr"),
            }
            print(canonical(status))
        case ["mrs", input_dir]:
            # votes change faster than the web server fetches them, so the
            # monitor compares the rest with GitLab's list
            mrs = read_merge_requests(Path(input_dir) / "mr")
            for mr in mrs:
                del mr["upvotes"], mr["downvotes"]
            print(canonical(mrs))
        case ["restore", status, input_dir]:
            restore(Path(status), Path(input_dir))
        case _:
            print(__doc__, file=sys.stderr, end="")
            return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
