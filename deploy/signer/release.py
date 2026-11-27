#!/usr/bin/python3 -I
"""Build a release from a tag, compare it with other builds, and attest it.

  KEYRING=~/release-signers.gpg deploy/signer/release.py site-vN <your name>

Exit: 0 signed or declined, 1 on any failure.
"""

import hashlib
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

MODULES = Path(__file__).resolve().parents[1] / "modules.py"
exec(compile(MODULES.read_bytes(), MODULES, "exec"), {"__file__": str(MODULES)})
import lib  # noqa: E402

# the signer's environment must change neither the build nor the review
KEPT = """
    HOME PATH TERM GPG_TTY GNUPGHOME XDG_RUNTIME_DIR KEYRING ATTESTATIONS_REF RELEASES
""".split()
# git still reads the signer's config files; these make it ignore them
GIT_SETTINGS = {
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_ATTR_NOSYSTEM": "1",
    "GIT_CONFIG_COUNT": "1",
    "GIT_CONFIG_KEY_0": "core.attributesFile",
    "GIT_CONFIG_VALUE_0": "/dev/null",
}

# pnpm as packageManager names it; Node and GCC from one StageX release
PNPM_VERSION = "11.4.0"
PNPM_URL = f"https://registry.npmjs.org/pnpm/-/pnpm-{PNPM_VERSION}.tgz"
PNPM_SHA512 = (
    "f0febc7e37552ab485494a914241b338e0b3580b93d54ce31f00933015880863"
    "129038a1b4ae4e414a0ee63ac35bf21197e990172c4a68256450b5636310968f"
)
NODE = (
    "docker.io/stagex/pallet-nodejs"
    "@sha256:2a555068fb2e4c9a6fc3bec8fdce265a1ea1206a67ad3dc71083ef7b6f091501"
)
GCC = (
    "docker.io/stagex/core-gcc"
    "@sha256:396679821776a7da0e5806e9ce78fd92113452a0977d90956f62a7caa32520db"
)
# lightningcss loads libgcc_s, which StageX's Node lacks
CONTAINERFILE = f"""\
FROM {GCC} AS gcc
FROM {NODE}
COPY --from=gcc /usr/lib/libgcc_s.so.1 /usr/lib/
"""
PNPM = "/pnpm/package/bin/pnpm.cjs"

# made absolute in main, since gpgv looks a bare name up in ~/.gnupg
KEYRING = Path(os.environ.get("KEYRING") or Path("~/release-signers.gpg").expanduser())
# the remote's branch by its full name, which no tag can take
ATTESTATIONS_REF = os.environ.get("ATTESTATIONS_REF") or "refs/remotes/origin/main"
USAGE = "usage: release.py site-vN <your name, in a-z 0-9 . _ ->"
# a name that is safe as a folder name
BUILDER_NAME = re.compile(r"[a-z0-9_-][a-z0-9._-]*")
CONTROL_BYTE = re.compile(rb"[\x00-\x08\x0b-\x1f\x7f]")


@dataclass(frozen=True)
class Build:
    """One build of a tag: the builder, and the scratch folder it builds in."""

    tag: str
    version: int
    builder: str
    keyring: Path
    work: Path
    epoch: str

    @property
    def previous(self) -> str:
        return f"site-v{self.version - 1}" if self.version > 1 else ""

    @property
    def git(self) -> Path:
        return self.work / "git"

    @property
    def out(self) -> Path:
        return Path(f"release-{self.tag}")


def clean_environment() -> None:
    """Keep only the variables in KEPT, and make git ignore the signer's settings."""
    kept = {name: os.environ[name] for name in KEPT if name in os.environ}
    os.environ.clear()
    os.environ.update(kept, **GIT_SETTINGS)


def cat_v(data: bytes, *options: str) -> bytes:
    """Return the data as cat -v shows it, so no byte can act on a terminal."""
    return lib.run("cat", "-v", *options, input=data, stdout=subprocess.PIPE).stdout


def check_gnu_tar() -> None:
    """Stop unless tar is GNU tar, whose options make the archive byte-identical."""
    tar = subprocess.run(["tar", "--version"], capture_output=True)
    if b"GNU tar" not in tar.stdout:
        raise lib.Stop("needs GNU tar, for a byte-identical archive")


def check_plain_files(build: Build) -> None:
    """Stop when the tag holds anything but plain files with plain names.

    A submodule, a .gitattributes or an odd name can hide a change from review.
    """
    gittree = [lib.DEPLOY / "gittree.py", "plain", build.git, build.tag]
    try:
        odd = lib.run(*gittree, stdout=subprocess.PIPE).stdout.rstrip(b"\n")
    except lib.FAILED as error:
        raise lib.Stop(f"can't list {build.tag}") from error
    if odd:
        files = cat_v(odd).decode()
        raise lib.Stop(f"more than plain files, so {build.tag} isn't built: {files}")


def check_pnpm_pin(tag: str) -> None:
    """Stop unless the tag's package.json names the pnpm pinned here."""
    show = ["git", "show", f"{tag}:package.json"]
    package = subprocess.run(show, stdout=subprocess.PIPE).stdout
    if f'"packageManager": "pnpm@{PNPM_VERSION}"'.encode() not in package:
        raise lib.Stop(f"the packageManager of {tag} isn't pnpm@{PNPM_VERSION}")


def fetch_pnpm(work: Path) -> None:
    """Download pnpm, check it against its pinned hash, and unpack it in work/pnpm."""
    tgz = work / "pnpm.tgz"
    lib.run("curl", "--disable", "-fsS", "-o", tgz, PNPM_URL)
    if hashlib.sha512(tgz.read_bytes()).hexdigest() != PNPM_SHA512:
        raise lib.Stop("pnpm doesn't match its hash")
    lib.run("tar", "-xzf", tgz, "-C", work / "pnpm")


def get_previous_release(build: Build) -> Path:
    """Download and verify release N-1 into work/old, and return its scripts.

    A tag's ancestry can skip releases or lack their tags. Release 1 has none, so
    it compares with empty folders, and only its own scripts may build it.
    """
    if build.version == 1:
        for name in ["deploy", "dist"]:
            (build.work / "old/files" / name).mkdir(parents=True)
        return build.work / "tag/deploy"
    tag, old = build.previous, build.work / "old"
    commit = ["git", "rev-parse", "-q", "--verify", f"{tag}^{{commit}}"]
    if not lib.succeeds(*commit, stdout=subprocess.DEVNULL):
        raise lib.Stop(f"there is no tag {tag} here to compare the lockfile with")
    try:
        lib.get_release(tag, old)
    except lib.FAILED as error:
        raise lib.Stop(f"can't download {tag}") from error
    files = old / "files"
    if not lib.accepts("release", old, build.keyring, build.version - 1, files):
        raise lib.Stop(f"{tag} doesn't verify against {build.keyring}")
    return files / "deploy"


def build_image(work: Path) -> str:
    """Build the image to build the site in, and return its ID.

    By its ID, no one can swap the image under a name meanwhile.
    """
    podman = ["podman", "build", "-q", "-f", "-", work / "image"]
    try:
        return lib.output(*podman, input=CONTAINERFILE).strip()
    except lib.FAILED as error:
        raise lib.Stop("can't build the image to build in") from error


def run_node(
    build: Build, image: str, folder: str, *arguments: str, online: bool = False
) -> None:
    """Run Node in the image on work/<folder>, with no network unless online."""
    network = [] if online else ["--network=none"]
    # --timeout, as Node, the container's first process, ignores a plain stop;
    # cat -v, as any byte from the tag could be a terminal escape
    work = build.work
    podman = ["podman", "run", "--rm", "--timeout=1800", *network]
    podman += ["-e", "HOME=/tmp", "-e", f"SOURCE_DATE_EPOCH={build.epoch}"]
    podman += ["-e", "ASTRO_TELEMETRY_DISABLED=1"]
    podman += ["-v", f"{work}/{folder}:/src:Z", "-v", f"{work}/store:/store:Z"]
    podman += ["-v", f"{work}/pnpm:/pnpm:ro,Z", "-w", "/src"]
    podman += ["--entrypoint", "node", image, *arguments]
    node = subprocess.Popen(podman, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    lib.piped(node, "cat", "-v")


def install_packages(build: Build, image: str) -> None:
    """Fetch the packages the tag's lockfile names, then install them offline."""
    lock = build.work / "lock/pnpm-lock.yaml"
    # online, pnpm sees only the lockfile, so no code of the tag has the network
    shutil.copyfile(build.work / "src/pnpm-lock.yaml", lock)
    if not lib.accepts("lockfile", lock):
        raise lib.Stop(
            f"the lockfile takes a package from elsewhere, so {build.tag} isn't signed"
        )
    # the policies are set here, so no tag can loosen them
    fetch = [
        "fetch",
        "--prod",
        "--store-dir=/store",
        "--config.minimumReleaseAge=20160",
        "--config.trustPolicy=no-downgrade",
        "--config.blockExoticSubdeps=true",
    ]
    run_node(build, image, "lock", PNPM, *fetch, online=True)
    # the offline install trusts the fetch's policy check
    install = [
        "install",
        "--offline",
        "--frozen-lockfile",
        "--trust-lockfile",
        "--prod",
        "--ignore-scripts",
        "--ignore-pnpmfile",
        "--config.strictStorePkgContentCheck=true",
        "--store-dir=/store",
    ]
    run_node(build, image, "src", PNPM, *install)


def check_control_bytes(deploy: Path) -> None:
    """Stop on a control byte in deploy/, which could hide a change from review."""
    files = sorted(path for path in deploy.rglob("*") if path.is_file())
    bad = "\n".join(str(f) for f in files if CONTROL_BYTE.search(f.read_bytes()))
    if bad:
        raise lib.Stop(
            f"a control byte in a text file, so this build isn't signed: {bad}"
        )


def build_site(build: Build, image: str) -> None:
    """Build the site from the tag's source into work/src, and check the build."""
    src = build.work / "src"
    lib.untar(lib.git_archive(build.git, build.tag), src)
    install_packages(build, image)
    run_node(build, image, "src", "node_modules/astro/bin/astro.mjs", "build")
    # deploy/ runs as root, so it comes from the tag, out of the build's reach
    shutil.rmtree(src / "deploy")
    lib.untar(lib.git_archive(build.git, build.tag, "deploy"), src)
    check_control_bytes(src / "deploy")
    hosts = src / "deploy/site-hosts"
    if not lib.accepts("build", src / "dist", hosts, build.work / "tag"):
        raise lib.Stop("the build holds what a release may not, so it isn't signed")


def pack_release(build: Build) -> None:
    """Write the archive and SHA256SUMS into a new folder, release-<tag>.

    One already there, even from the tag, stops the run.
    """
    try:
        build.out.mkdir()
    except FileExistsError as error:
        raise lib.Stop(f"{build.out} already exists; move it away first") from error
    archive = f"ccs-{build.tag}.tar"
    tar = ["tar", "--sort=name", f"--mtime=@{build.epoch}", "--owner=0", "--group=0"]
    tar += ["--numeric-owner", "--mode=u=rwX,go=rX", "--format=gnu"]
    src = build.work / "src"
    lib.run(*tar, "-cf", build.out / archive, "-C", src, "dist", "deploy")
    sums = f"{lib.sha256((build.out / archive).read_bytes())}  {archive}\n"
    (build.out / "SHA256SUMS").write_text(sums)
    print(sums, end="")


def get_attestations(build: Build) -> Path:
    """Copy each builder's attestation of the tag from ATTESTATIONS_REF.

    Return the folder that holds them, a folder for each builder.
    """
    folder = build.work / "attestations"
    folder.mkdir()
    path = f"attestations/{build.tag}"
    tree = f"{ATTESTATIONS_REF}:{path}"
    if lib.succeeds("git", "cat-file", "-e", tree, stderr=subprocess.DEVNULL):
        lib.check_tree_size(build.git, tree, 1000, 1000000)
        archive = lib.git_archive(build.git, ATTESTATIONS_REF, path)
        lib.piped(archive, "tar", "-x", "-C", folder, "--strip-components=2")
    return folder


def is_plain_attestation(folder: Path) -> bool:
    """Return whether the folder holds SHA256SUMS and SHA256SUMS.asc as plain files.

    Anyone can add an attestation, so a link, which can point anywhere, fails.
    """
    files = [folder / "SHA256SUMS", folder / "SHA256SUMS.asc"]
    plain = all(file.is_file() and not file.is_symlink() for file in files)
    return plain and not folder.is_symlink()


def is_release_signer(keyring: Path, folder: Path) -> bool:
    """Return whether a key in the keyring signed the attestation in the folder."""
    gpgv = ["gpgv", "--keyring", keyring, folder / "SHA256SUMS.asc"]
    quiet = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    return lib.succeeds(*gpgv, folder / "SHA256SUMS", **quiet)


def compare_attestations(build: Build, attestations: Path) -> bytes:
    """Compare this build with each attestation.

    Stop when a release signer's differs. Return the matching release signers'
    signatures.
    """
    ours = (build.out / "SHA256SUMS").read_bytes()
    trusted = b""
    for folder in sorted(attestations.iterdir()):
        builder = folder.name
        if not BUILDER_NAME.fullmatch(builder) or not is_plain_attestation(folder):
            continue
        same = (folder / "SHA256SUMS").read_bytes() == ours
        if is_release_signer(build.keyring, folder):
            if not same:
                raise lib.Stop(f"this build differs from {builder}'s; don't sign it")
            trusted += (folder / "SHA256SUMS.asc").read_bytes()
            print(f"The same bytes as {builder}, a release signer.")
        elif same:
            print(f"The same bytes as {builder}.")
        else:
            lib.log(f"{builder}, who isn't a release signer, built different bytes")
    return trusted


def show_diff(work: Path, *options: str) -> bytes:
    """Return git's diff of two paths in work, with each hunk line indented.

    So no line can pass for a file's header.
    """
    git = ["git", "-C", str(work), "diff", "--no-index", "--no-color", "--no-ext-diff"]
    # git diff exits 1 when the paths differ
    diff = subprocess.run([*git, "--no-textconv", *options], stdout=subprocess.PIPE)
    shown, hunk = [], False
    # split at \n only: splitlines also splits at \r, which would let a line of a
    # file start a fake "diff --git" header
    for line in io.BytesIO(diff.stdout):
        if line.startswith(b"diff --git "):
            hunk = False
        shown.append(b"  " + line if hunk else line)
        if line.startswith(b"@@ "):
            hunk = True
    return b"".join(shown)


def show_changes(build: Build) -> None:
    """Show in less what changed since the release before."""
    work = build.work
    lib.run("tar", "-xf", build.out / f"ccs-{build.tag}.tar", "-C", work / "new")
    print(f"Changes since {build.previous or 'nothing'}:")
    # the lockfile first, so a reviewer sees where each package comes from
    with open(work / "old/pnpm-lock.yaml", "wb") as lock:
        if build.previous:
            lib.run("git", "show", f"{build.previous}:pnpm-lock.yaml", stdout=lock)
    review = [
        show_diff(work, "old/pnpm-lock.yaml", "tag/pnpm-lock.yaml"),
        show_diff(work, "--patch-with-stat", "old/files/deploy", "new/deploy"),
        # a word to a line, so a planted declaration shows in minified CSS;
        # -M10% pairs a renamed stylesheet with the old one
        show_diff(
            work,
            "--patch-with-stat",
            "-M10%",
            "--word-diff=porcelain",
            "--word-diff-regex=[^;{}[:space:]]+|[;{}]",
            "old/files/dist",
            "new/dist",
        ),
    ]
    # cat -vET shows control bytes, tabs and line ends, so nothing hides
    shown = cat_v(b"".join(review), "-ET")
    # a signer may quit less early; the run still goes on to the question
    less = {**os.environ, "LESS": "", "LESSSECURE": "1"}
    subprocess.run(["less"], input=shown, env=less)


def sign_release(build: Build, trusted: bytes) -> int:
    """Ask the builder, then sign SHA256SUMS and write the attestation."""
    sums = build.out / "SHA256SUMS"
    print(f"Sign {sums}? [y/N] ", end="", flush=True)
    if sys.stdin.buffer.readline().strip(b" \t\n") not in (b"y", b"Y"):
        print("Not signed.")
        return 0
    mine = build.work / "mine.asc"
    lib.run("gpg", "--detach-sign", "--armor", "-o", mine, sums)
    quiet = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if not lib.succeeds("gpg", "--verify", mine, sums, **quiet):
        raise lib.Stop("your signature doesn't verify with your own key")
    folder = build.out / "attestations" / build.tag / build.builder
    folder.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(sums, folder / "SHA256SUMS")
    shutil.copyfile(mine, folder / "SHA256SUMS.asc")
    if not is_release_signer(build.keyring, folder):
        lib.log(
            f"your key isn't in {build.keyring}, so this attests the build, "
            "but doesn't count towards the two release signers"
        )
    under = f"attestations/{build.tag}/{build.builder}/"
    print(f"Your attestation is in {folder}/. Add it to the repository under {under}.")
    join_signatures(build, trusted + mine.read_bytes())
    return 0


def join_signatures(build: Build, signatures: bytes) -> None:
    """Join the release signers' signatures into SHA256SUMS.asc.

    It keeps the file only when it passes the check that every install makes.
    """
    joined = build.out / "SHA256SUMS.asc"
    joined.write_bytes(signatures)
    verify = build.work / "verify"
    if lib.accepts("release", build.out, build.keyring, build.version, verify):
        print(
            f"Create the GitHub release {build.tag} with ccs-{build.tag}.tar, "
            f"SHA256SUMS and SHA256SUMS.asc, from {build.out}/."
        )
    else:
        joined.unlink()
        print(
            "Not ready to publish: the release needs two release signers' "
            "attestations."
        )


def open_work(tag: str, version: int, builder: str, keyring: Path, work: Path) -> Build:
    """Make the scratch folders, and a git repository that later git commands read.

    It has only the refs this reads, so no setting or attribute of the checkout
    counts.
    """
    git = work / "git"
    lib.run("git", "init", "-q", "--bare", git)
    with open(git / "objects/info/alternates", "wb") as alternates:
        objects = ["--path-format=absolute", "--git-path", "objects"]
        lib.run("git", "rev-parse", *objects, stdout=alternates)
    # no replace ref of the checkout
    each_ref = ["git", "for-each-ref", "--format=create %(refname) %(objectname)"]
    refs = lib.run(*each_ref, "refs/tags", ATTESTATIONS_REF, stdout=subprocess.PIPE)
    lib.run("git", f"--git-dir={git}", "update-ref", "--stdin", input=refs.stdout)
    os.environ["GIT_DIR"] = str(git)
    epoch = lib.output("git", "log", "-1", "--format=%ct", tag).strip()
    for name in ["src", "lock", "store", "tag", "pnpm", "image", "new", "old"]:
        (work / name).mkdir()
    return Build(tag, version, builder, keyring, work, epoch)


def take_tag(build: Build) -> None:
    """Refuse a tag of more than plain files, and copy its files to work/tag.

    There, out of the build's reach, checks.py build reads them.
    """
    lib.check_tree_size(build.git, build.tag, 10000, 100000000)
    check_plain_files(build)
    lib.untar(lib.git_archive(build.git, build.tag), build.work / "tag")


def hand_over(build: Build, scripts: Path) -> int:
    """Run the scripts that may build this release, and return their exit status."""
    # nothing has signed release 1's scripts, so only the ones read here run
    if build.version == 1:
        raise lib.Stop(f"run release 1 from a checkout of {build.tag} itself")
    print(f"Running the scripts of {build.previous}.")
    script = scripts / "signer/release.py"
    command = [sys.executable, "-I", str(script), build.tag, build.builder]
    return subprocess.run(command).returncode


def main() -> int:
    clean_environment()
    lib.line_buffered()
    tag, builder = (sys.argv[1:] + ["", ""])[:2]
    version = lib.release_tag(tag)
    if version is None or not BUILDER_NAME.fullmatch(builder):
        raise lib.Stop(USAGE)
    check_gnu_tar()
    if not KEYRING.exists():
        raise lib.Stop("set KEYRING to the release signers' keyring")
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as work:
        build = open_work(tag, version, builder, KEYRING.resolve(), Path(work))
        take_tag(build)
        scripts = get_previous_release(build)
        # only release N-1's scripts, which two signers read, build release N
        diff = ["diff", "-rq", "--", lib.DEPLOY, scripts]
        if not lib.succeeds(*diff, stdout=subprocess.DEVNULL):
            return hand_over(build, scripts)

        check_pnpm_pin(tag)
        fetch_pnpm(build.work)
        image = build_image(build.work)
        build_site(build, image)
        pack_release(build)

        trusted = compare_attestations(build, get_attestations(build))
        show_changes(build)
        return sign_release(build, trusted)


if __name__ == "__main__":
    lib.main(main)
