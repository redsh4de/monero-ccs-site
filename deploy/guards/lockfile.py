"""pnpm's lockfile, which must take every package from the registry."""

import re
from collections.abc import Iterator

from guards.common import Reject, lines, read_capped


# the sections, and each entry's fields, that pnpm writes in a lockfile
LOCK_SECTIONS = set(
    "lockfileVersion settings overrides importers packages snapshots"
    " ignoredOptionalDependencies".split()
)
LOCK_FIELDS = {
    "packages": set(
        "cpu deprecated engines hasBin libc os peerDependencies peerDependenciesMeta"
        " resolution".split()
    ),
    "snapshots": set(
        "dependencies optional optionalDependencies transitivePeerDependencies".split()
    ),
}
SEMVER = r"[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?"
PACKAGE = rf"(?:@[a-z0-9][a-z0-9._~-]*/)?[a-z0-9][a-z0-9._~-]*@{SEMVER}"
PEERS = r"(?:\(.*\))?"
LOCK_ROW = re.compile(r"( *)(?:- (.+)|('[^']*'|[^']+?):(?: (.+))?)")


def lock_scalar(text: str, flow: bool = False) -> str:
    """Return a scalar pnpm writes, single-quoted or plain, or raise ValueError."""
    if re.fullmatch("'[^']*'", text):
        return text[1:-1]
    syntax = r"[,:\[\]{}]| $" if flow else r": | #|[ :]$"
    if re.match(r"$|[-?:,\[\]{}#&*!|>'\"%@`< ]", text) or re.search(syntax, text):
        raise ValueError(text)
    return text


def lock_value(text: str | None) -> object:
    """Return a row's value: None, a flat flow map or list, or a scalar."""
    if text is None or text in ("{}", "[]"):
        return {"{}": {}, "[]": []}.get(text)
    items = text[1:-1].split(", ")
    if text[0] + text[-1] == "[]":
        return [lock_scalar(item, True) for item in items]
    if text[0] + text[-1] != "{}":
        return lock_scalar(text)
    pairs = (item.split(": ") for item in items)
    flat = {lock_scalar(key, True): lock_scalar(value, True) for key, value in pairs}
    if len(flat) < len(items):
        raise ValueError(text)
    return flat


def lock_block(rows: list, start: int, indent: int) -> tuple[object, int]:
    """Return the map or list whose rows begin at start, and the row after it."""
    tree: dict | list = [] if rows[start][2] is None else {}
    i = start
    while i < len(rows) and rows[i][1] == indent:
        n, _, key, value = rows[i]
        i += 1
        if i < len(rows) and rows[i][1] > indent:
            if value is not None:
                raise ValueError(rows[i][0])
            value, i = lock_block(rows, i, indent + 2)
        if isinstance(tree, list) and key is None:
            tree.append(value)
        elif isinstance(tree, dict) and key is not None and key not in tree:
            tree[key] = value
        else:
            raise ValueError(n)
    return tree, i


def as_map(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def lock_strays(tree: dict) -> Iterator[str]:
    """Yield each key or version that names anything but a registry package."""
    yield from (key for key in tree if key not in LOCK_SECTIONS)
    for key, entry in as_map(tree.get("packages")).items():
        resolution = as_map(as_map(entry).get("resolution"))
        if not (
            re.fullmatch(PACKAGE, key)
            and set(as_map(entry)) <= LOCK_FIELDS["packages"]
            and list(resolution) == ["integrity"]
            and re.fullmatch(r"sha512-[A-Za-z0-9+/]+=*", str(resolution["integrity"]))
        ):
            yield key
    for key, entry in as_map(tree.get("snapshots")).items():
        if not re.fullmatch(PACKAGE + PEERS, key):
            yield key
        elif not set(as_map(entry)) <= LOCK_FIELDS["snapshots"]:
            yield key
    for importer in as_map(tree.get("importers")).values():
        for group in as_map(importer).values():
            for dependency in as_map(group).values():
                version = str(as_map(dependency).get("version"))
                if not re.fullmatch(SEMVER + PEERS, version):
                    yield version


def check_lockfile(path: str) -> None:
    """Refuse a pnpm lockfile that takes a package from elsewhere than the registry.

    pnpm's supply-chain policies judge a package only by its name and version.
    The file is read only as the YAML pnpm writes, so no parser reads it otherwise.
    """
    rows = lines(read_capped(path, 10_000_000, "lockfile"), "lockfile")
    for document in lock_documents(rows):
        stray = next(lock_strays(as_map(lock_tree(document))), None)
        if stray is not None:
            raise Reject(
                f"lockfile: {stray[:80]} does not come from the registry by name and"
                " version"
            )


def lock_documents(rows: list[str]) -> list[list]:
    """Return each document's rows, as number, indent, key and value."""
    documents: list[list] = []
    for n, row in enumerate(rows, 1):
        try:
            if row == "---":
                documents.append([])
            elif row:
                documents[-1].append((n, *lock_row(row)))
        except (ValueError, IndexError):
            raise Reject(f"lockfile: row {n} is not YAML as pnpm writes it") from None
    return documents


def lock_row(row: str) -> tuple[int, str | None, object]:
    """Return a row's indent, key and value.

    Raise ValueError for a row that pnpm never writes.
    """
    match = LOCK_ROW.fullmatch(row)
    if not match or row[0] == "'" or not row.isprintable() or len(match[1]) % 2:
        raise ValueError(row)
    _, item, key, value = match.groups()
    if item:
        return len(match[1]), None, lock_scalar(item)
    return len(match[1]), lock_scalar(key), lock_value(value)


def lock_tree(document: list) -> object:
    """Return a document's tree, or refuse a row that does not fit it."""
    try:
        tree, end = lock_block(document, 0, 0) if document else ({}, 0)
        if end < len(document):
            raise ValueError(document[end][0])
    except ValueError as error:
        raise Reject(f"lockfile: row {error} is not YAML as pnpm writes it") from None
    return tree
