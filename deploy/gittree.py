#!/usr/bin/python3 -I
r"""Read a git tree or its history for the deploy scripts, a record at a time.

Usage:
  gittree.py size <git-dir> <tree> <entries> <bytes>
  gittree.py plain <git-dir> <tree>
  gittree.py merges <git-dir> <commit>

size: refuses a tree with more entries, folders included, or more bytes.
plain: prints each entry that is not a plain file with a plain name.
merges: prints "<merge request>\t<file>" for each root .md file that a merge
on the first-parent history added, oldest first.

Exit: 0 done, 1 refused or git failed, 2 usage.
"""

import re
import subprocess
import sys
from collections.abc import Iterator

PLAIN_MODES = {b"100644", b"100755"}
PLAIN_NAME = re.compile(rb"[][A-Za-z0-9._/-]+")
MERGE = re.compile(rb"See merge request monero-project/ccs-proposals!([0-9]+)")
# the bytes git quotes in a name; merged.tsv leaves such names out
QUOTED = re.compile(rb'[\x00-\x1f\x7f"\\]')


def records(argv: list[str | bytes]) -> Iterator[bytes]:
    """Yield the NUL-ended records a command writes, then fail if it failed.

    A reader that stops early closes the pipe, which stops the command.
    """
    with subprocess.Popen(argv, stdout=subprocess.PIPE) as command:
        assert command.stdout is not None
        rest = b""
        while chunk := command.stdout.read(65536):
            *done, rest = (rest + chunk).split(b"\0")
            yield from done
        if command.wait() != 0:
            raise OSError(f"git exited {command.returncode}")


def size(git_dir: str, tree: str, entries: int, cap: int) -> bool:
    """Return True if the tree holds at most `entries` entries and `cap` bytes.

    It stops at the first entry past a cap, as a few objects can list without end.
    """
    count = total = 0
    for record in records(
        ["git", "-C", git_dir, "ls-tree", "-r", "-t", "-l", "-z", tree]
    ):
        length = record.partition(b"\t")[0].split()[3]
        count += 1
        total += 0 if length == b"-" else int(length)
        if count > entries or total > cap:
            return False
    return True


def odd_entries(git_dir: str, tree: str) -> Iterator[bytes]:
    """Yield each entry that a review would not show as it is, as git lists it.

    That is a submodule, a link or any other entry but a plain file, a name of
    more than plain letters, or a .gitattributes, which changes what a checkout
    shows.
    """
    for record in records(["git", "-C", git_dir, "ls-tree", "-r", "-z", tree]):
        meta, _, name = record.partition(b"\t")
        if (
            meta.split(b" ")[0] not in PLAIN_MODES
            or not PLAIN_NAME.fullmatch(name)
            or name.rpartition(b"/")[2] == b".gitattributes"
        ):
            yield record


def merges(git_dir: str, commit: str) -> Iterator[bytes]:
    """Yield the merge request and the name of each root .md file a merge added.

    Only the tip's own root files are read, so no file an old commit added
    costs anything. A file added again maps to its newest merge.
    """
    names = set()
    for record in records(["git", "-C", git_dir, "ls-tree", "-z", commit]):
        meta, _, name = record.partition(b"\t")
        mode, kind, _ = meta.split(b" ")
        if mode.startswith(b"100") and kind == b"blob" and name.endswith(b".md"):
            if not QUOTED.search(name):
                names.add(name)
    if not names:
        return
    log = ["timeout", "300", "git", "-C", git_dir, "log", "-z", "--reverse"]
    log += ["--first-parent", "--diff-filter=A", "--name-only"]
    log += ["--format=%H%x00%B", commit, "--"]
    # each commit is its hash, its message, then the names it added, the
    # first after a newline; no name of a root .md file looks like a hash
    merge, message_next = None, False
    for token in records([*log, *(b":(literal)" + name for name in names)]):
        name = token.removeprefix(b"\n")
        if message_next:
            found = [m[1] for m in map(MERGE.fullmatch, token.split(b"\n")) if m]
            merge, message_next = (found[-1] if found else None), False
        elif name in names:
            if merge:
                yield merge + b"\t" + name
        else:
            message_next = True


def main(argv: list[str]) -> int:
    out = sys.stdout.buffer
    try:
        match argv:
            case ["size", git_dir, tree, entries, cap]:
                return 0 if size(git_dir, tree, int(entries), int(cap)) else 1
            case ["plain", git_dir, tree]:
                for record in odd_entries(git_dir, tree):
                    out.write(record + b"\n")
                return 0
            case ["merges", git_dir, commit]:
                for line in merges(git_dir, commit):
                    out.write(line + b"\n")
                return 0
    except (OSError, ValueError) as error:
        print(f"gittree.py: {error}", file=sys.stderr)
        return 1
    print(__doc__, file=sys.stderr, end="")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
