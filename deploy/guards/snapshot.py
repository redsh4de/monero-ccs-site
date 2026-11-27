"""The status.json that a web server sends the monitor."""

import json
import re

from guards.common import Reject

STATUS_CAP = 50_000_000

COMMIT = re.compile(r"[0-9a-f]{40}")
GITLAB_URL = re.compile(r"https://[!-~]{1,500}")
GITLAB_TIME = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,9})?(Z|[+-][0-9]{2}:[0-9]{2})"
)
VOTES = ("upvotes", "downvotes")
SNAPSHOT_FIELDS = {"iid", "title", "author", "web_url", "created_at", "adds", *VOTES}
MAX_MERGE_REQUESTS = 1_000  # mrs.py reads at most 1,000
MAX_TEXT = 1_000  # GitLab allows 255 characters for a title or a username


def check_status(data: bytes) -> str:
    """Return status.json's release and commit, each checked."""
    try:
        status = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise Reject("status: not JSON") from None
    if not isinstance(status, dict):
        raise Reject("status: not an object")
    release, commit = status.get("release"), status.get("commit")
    if (
        type(release) is not int
        or not 1 <= release <= 999_999
        or not isinstance(commit, str)
        or not COMMIT.fullmatch(commit)
    ):
        raise Reject("status: a field is missing or malformed")
    return f"{release} {commit}"


def blank_or_matches(pattern: re.Pattern[str], value: object) -> bool:
    return isinstance(value, str) and (value == "" or bool(pattern.fullmatch(value)))


def check_snapshot(data: bytes) -> None:
    """Refuse a merge request snapshot that GitLab itself could not have sent.

    Anyone can give a merge request any title, so only a text's length is checked.
    """
    try:
        snapshot = json.loads(data.decode("utf-8")).get("merge_requests")
    except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
        raise Reject("snapshot: not a JSON object") from None
    if not isinstance(snapshot, list) or len(snapshot) > MAX_MERGE_REQUESTS:
        raise Reject("snapshot: not a list of at most 1,000 merge requests")
    for mr in snapshot:
        check_merge_request(mr)


def check_merge_request(mr: dict) -> None:
    """Refuse a merge request in a form GitLab never sends."""
    if not isinstance(mr, dict) or set(mr) != SNAPSHOT_FIELDS:
        raise Reject("snapshot: a merge request has other fields")
    if type(mr["iid"]) is not int or not 1 <= mr["iid"] <= 999_999_999:
        raise Reject("snapshot: a merge request number is malformed")
    if not all(type(mr[k]) is int and 0 <= mr[k] <= 999_999_999 for k in VOTES):
        raise Reject("snapshot: a vote count is malformed")
    adds = mr["adds"]
    if not isinstance(adds, list) or len(adds) > MAX_MERGE_REQUESTS:
        raise Reject("snapshot: a merge request's files are malformed")
    texts = [mr["title"], mr["author"], *adds]
    if not all(isinstance(text, str) and len(text) <= MAX_TEXT for text in texts):
        raise Reject("snapshot: a text field is too long, or not text")
    if not blank_or_matches(GITLAB_URL, mr["web_url"]):
        raise Reject("snapshot: a merge request address is malformed")
    if not blank_or_matches(GITLAB_TIME, mr["created_at"]):
        raise Reject("snapshot: a merge request time is malformed")
