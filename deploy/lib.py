"""Steps the deploy scripts share."""

import glob
import hashlib
import io
import os
import re
import shutil
import signal
import subprocess
import sys
from collections.abc import Callable, Mapping
from pathlib import Path

DEPLOY = Path(__file__).resolve().parent
# defaults for ccs.getmonero.org, which /etc/ccs/config overrides
CCS_DIR = Path(os.environ.get("CCS_DIR", "/var/lib/ccs"))
GITLAB_API = os.environ.get(
    "GITLAB_API",
    "https://repo.getmonero.org/api/v4/projects/monero-project%2Fccs-proposals",
)
PROPOSALS_GIT = os.environ.get(
    "PROPOSALS_GIT", "https://repo.getmonero.org/monero-project/ccs-proposals.git"
)
RELEASES = os.environ.get(
    "RELEASES", "https://github.com/redsh4de/monero-ccs-site/releases"
)
SITE = os.environ.get("SITE", "https://ccs.getmonero.org")
RELEASE_SIGNERS = Path("/etc/ccs/release-signers.gpg")
ADDRESS_SIGNERS = Path("/etc/ccs/address-signers.gpg")


class Stop(Exception):
    """A failure that ends the script with its message and exit 1."""


# what a failed step raises: a command that fails, a file it cannot read or
# write, or a Stop
FAILED = (OSError, subprocess.SubprocessError, Stop)


def log(message: str) -> None:
    """Write the message to stderr, for the journal."""
    print(message, file=sys.stderr, flush=True)


def log_failed(error: BaseException) -> None:
    """Log a failed step in one line, after the script's name."""
    log(f"{Path(sys.argv[0]).name}: {error}")


def main(steps: Callable[[], int | None]) -> None:
    """Run the script's steps; a failed step logs one line and exits 1."""
    # a stop from systemd raises, so finally blocks run as on any other exit
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(1))
    try:
        sys.exit(steps() or 0)
    except FAILED as error:
        log_failed(error)
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)


def line_buffered() -> None:
    """Write each line of stdout at once, in order with the commands' output."""
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(line_buffering=True)


def run(*command: object, **options) -> subprocess.CompletedProcess:
    """Run a command, and raise CalledProcessError when it fails."""
    return subprocess.run(list(map(str, command)), check=True, **options)


def succeeds(*command: object, **options) -> bool:
    """Run a command, and return whether it exited 0."""
    return subprocess.run(list(map(str, command)), **options).returncode == 0


def output(*command: object, **options) -> str:
    """Run a command, and return its output as text."""
    return run(*command, stdout=subprocess.PIPE, text=True, **options).stdout


def checks(*arguments: object, **options) -> subprocess.CompletedProcess:
    """Run checks.py, and return its result whether it accepts or refuses."""
    return subprocess.run(list(map(str, [DEPLOY / "checks.py", *arguments])), **options)


def accepts(*arguments: object, **options) -> bool:
    """Run checks.py, and return whether it accepts."""
    return checks(*arguments, **options).returncode == 0


def read(path: Path) -> str:
    """Return a small state file's text without its last newline, or "" without one."""
    try:
        return path.read_text().removesuffix("\n")
    except OSError:
        return ""


def write(path: Path, text: str) -> None:
    path.write_text(f"{text}\n")


def has_data(path: Path) -> bool:
    """Return whether the file exists and is not empty."""
    return path.exists() and path.stat().st_size > 0


def remove(path: Path) -> None:
    """Delete a file, link or folder, and do nothing when it is missing."""
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def rename_synced(source: Path, target: Path) -> None:
    """Rename source to target once it is on disk, as later runs trust the name."""
    run("sync", "-f", source)
    source.rename(target)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def release_tag(tag: str) -> int | None:
    """Return N for a tag site-vN with no leading zero, and None for any other."""
    match = re.fullmatch(r"site-v([1-9][0-9]*)", tag)
    return int(match[1]) if match else None


def get_release(tag: str, folder: Path) -> None:
    """Download the release's three files into folder."""
    limits = "--max-time 600 --max-filesize 200000000".split()
    for name in ["SHA256SUMS", "SHA256SUMS.asc", f"ccs-{tag}.tar"]:
        url = f"{RELEASES}/download/{tag}/{name}"
        run("curl", "--disable", "-fsS", "-L", *limits, "-o", folder / name, url)


def verify_list(path: Path) -> None:
    """Write the signed address list to path, or an empty list if it fails.

    An empty list closes every donation box, so no unsigned address shows.
    """
    with open(path, "w") as file:
        signed = "/etc/ccs/addresses.txt"
        if not accepts("addresses", ADDRESS_SIGNERS, signed, stdout=file):
            file.truncate(0)


def render_values(values: Path, in_dir: Path, verified: Path) -> str:
    """Write the render's values, addresses and site into in_dir.

    Return their hash, without the values' time, which every push changes.
    """
    shutil.copyfile(verified, in_dir / "addresses")
    write(in_dir / "site", SITE)
    with open(in_dir / "values", "w") as file:
        checks("apply", values, verified, stdout=file, check=True)
    lines = "".join(
        line
        for name in ["values", "addresses", "site"]
        for line in (in_dir / name).read_text().splitlines(keepends=True)
        if not line.startswith("updated ")
    )
    return sha256(lines.encode())


def swap(target: Path, link: Path) -> None:
    """Point the link at the target in one rename, so it never goes missing."""
    temporary = Path(f"{link}.tmp")
    temporary.unlink(missing_ok=True)
    temporary.symlink_to(target)
    temporary.replace(link)


def keep(prefix: str, first: Path | str, second: Path | str) -> None:
    """Delete every path that starts with prefix, except first and second."""
    for path in glob.glob(glob.escape(prefix) + "*"):
        if path not in (str(first), str(second)):
            remove(Path(path))


def credential(name: str, env: Mapping[str, str] = os.environ) -> str:
    """Return the text of the systemd credential name, or ""."""
    folder = env.get("CREDENTIALS_DIRECTORY", "/nonexistent")
    return read(Path(folder, name)).strip()


def request_url(name: str, *curl: str, env: Mapping[str, str] = os.environ) -> bool:
    """Request the URL held in the credential name; False without one.

    The URL reaches curl on stdin, so ps never shows it.
    """
    url = credential(name, env)
    if not url:
        return False
    command = ["curl", "--disable", "-fsS", "-m", "20", "-K", "-", *curl]
    quiet = {"stdout": subprocess.DEVNULL, "text": True, "env": env}
    return succeeds(*command, input=f"url = {url}\n", **quiet)


def check_tree_size(git_dir: Path, tree: str, entries: int, size: int) -> None:
    """Raise Stop when the tree holds more entries, folders included, or bytes."""
    if not succeeds(DEPLOY / "gittree.py", "size", git_dir, tree, entries, size):
        raise Stop(
            f"{tree} can't be listed, or holds more than {entries} entries "
            f"or {size} bytes"
        )


def git_archive(git_dir: Path, tree: str, *paths: str) -> subprocess.Popen:
    """Start git archive of the tree, its tar on the process's stdout.

    Attributes come from an empty tree, so no .gitattributes can change a file.
    """
    git = ["git", "-C", str(git_dir)]
    none = output(*git, "hash-object", "-t", "tree", "/dev/null").strip()
    archive = [*git, f"--attr-source={none}", "archive", "--worktree-attributes"]
    return subprocess.Popen([*archive, tree, *paths], stdout=subprocess.PIPE)


def piped(first: subprocess.Popen, *command: object) -> None:
    """Run the command on the output of first, and fail when either fails."""
    run(*command, stdin=first.stdout)
    if first.stdout:
        first.stdout.close()
    if first.wait() != 0:
        raise subprocess.CalledProcessError(first.returncode, first.args)


def untar(archive: subprocess.Popen, folder: Path) -> None:
    """Unpack the archive's tar into folder, and fail when either side fails."""
    piped(archive, "tar", "-x", "-C", folder, "--no-same-owner")


def export_tree(git_dir: Path, commit: str, folder: Path) -> None:
    """Write the commit's files, and the merge request behind each, into folder.

    Its caps, 2000 files and 4 MB, are about 6 and 3 times the proposals of 2026.
    """
    check_tree_size(git_dir, commit, 2000, 4000000)
    (folder / "proposals").mkdir(parents=True, exist_ok=True)
    (folder / "mr").mkdir(parents=True, exist_ok=True)
    untar(git_archive(git_dir, commit), folder / "proposals")
    with open(folder / "mr/merged.tsv", "w") as merged:
        run(DEPLOY / "gittree.py", "merges", git_dir, commit, stdout=merged)
