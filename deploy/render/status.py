"""Write and read status.json, the record of what a render used.

Usage:
  status.py write <input-dir> <version> <commit>  print status.json
  status.py mrs <input-dir>                       print the merge request snapshot,
                                                  without the vote counts
  status.py restore <status.json> <input-dir>     write <input-dir>/mr/open.json
                                                  and open.tsv

<input-dir> is the render's input folder, which render.py describes.

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
from inputs import read_merge_requests, read_pairs  # noqa: E402


def canonical(value: object) -> str:
    """Return JSON that is the same for equal values."""
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def field_text(value: object) -> str:
    """Return a field as the render reads it: missing or empty is ""."""
    return str(value or "")


def snapshot(input_dir: Path) -> list[dict]:
    """Return what the render reads of each open merge request, in GitLab's order."""
    added: dict[str, list[str]] = {}
    for number, name in read_pairs(input_dir / "mr" / "open.tsv"):
        added.setdefault(number, []).append(name)
    entries = []
    for mr in read_merge_requests(input_dir / "mr" / "open.json"):
        author = mr.get("author")
        username = author.get("username") if isinstance(author, dict) else ""
        entries.append(
            {
                "iid": mr.get("iid"),
                "title": field_text(mr.get("title")),
                "author": field_text(username),
                "web_url": field_text(mr.get("web_url")),
                "created_at": field_text(mr.get("created_at")),
                "adds": added.get(str(mr.get("iid")), []),
                "upvotes": mr.get("upvotes", 0),
                "downvotes": mr.get("downvotes", 0),
            }
        )
    return entries


def restore(status: Path, input_dir: Path) -> None:
    """Write the merge request input that a snapshot was taken from."""
    entries = json.loads(status.read_text(encoding="utf-8"))["merge_requests"]
    mrs = [
        {
            "iid": mr["iid"],
            "title": mr["title"],
            "author": {"username": mr["author"]},
            "web_url": mr["web_url"],
            "created_at": mr["created_at"],
            "upvotes": mr["upvotes"],
            "downvotes": mr["downvotes"],
        }
        for mr in entries
    ]
    lines = [f"{mr['iid']}\t{name}\n" for mr in entries for name in mr["adds"]]
    (input_dir / "mr").mkdir(parents=True, exist_ok=True)
    (input_dir / "mr" / "open.json").write_text(canonical(mrs), encoding="utf-8")
    (input_dir / "mr" / "open.tsv").write_text("".join(lines), encoding="utf-8")


def main(argv: list[str]) -> int:
    """Run one command, and return the exit status."""
    match argv:
        case ["write", input_dir, version, commit]:
            status = {
                "release": int(version),
                "commit": commit,
                "merge_requests": snapshot(Path(input_dir)),
            }
            print(canonical(status))
        case ["mrs", input_dir]:
            # votes change faster than the web server fetches them, so the
            # monitor compares the rest with GitLab's list
            mrs = snapshot(Path(input_dir))
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
