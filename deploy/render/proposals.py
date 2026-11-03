"""Proposals, and ccs-back's rules for them.

ccs-back takes a proposal's stage from its layout, lists it only when its front
matter is complete, and counts the milestones marked done. This module applies
the same rules to the files in the public repo.
"""

import math
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Self
from urllib.parse import quote

import yaml

from inputs import Wallet
from safety import php_escape, redact

GITLAB = "https://repo.getmonero.org/"
REPO = f"{GITLAB}monero-project/ccs-proposals"


@dataclass(frozen=True)
class Stage:
    name: str
    page: str  # Path of the stage's page
    label: str
    summary: str  # Heading of the summary card
    state: str  # As the projects JSON names it


STAGES = {
    stage.name: stage
    for stage in (
        Stage(
            "funding-required",
            "funding-required",
            "Funding Required",
            "Contribute",
            "FUNDING-REQUIRED",
        ),
        Stage(
            "work-in-progress",
            "work-in-progress",
            "Work in Progress",
            "Progress",
            "WORK-IN-PROGRESS",
        ),
        Stage("completed", "completed-proposals", "Completed", "Funded", "COMPLETED"),
    )
}
LAYOUTS = {"fr": "funding-required", "wip": "work-in-progress", "cp": "completed"}

# ccs-back lists a proposal only when all of these are filled in.
REQUIRED = ("amount", "author", "date", "layout", "milestones", "title")
NOT_DONE = {"", "false", "no", "null", "~", "0"}

LEADING_NUMBER = re.compile(r"\s*[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?")
ORDINAL = re.compile(r"(\d)(st|nd|rd|th)\b")
# The date formats proposals use, all of which ccs-back's strtotime reads.
DATE_FORMS = ("%B %d %Y", "%d %B %Y", "%b %d %Y", "%d %b %Y", "%m/%d/%Y", "%B %Y")


@dataclass(frozen=True)
class Milestone:
    name: str
    funds: str
    done: str
    finished: bool


@dataclass(frozen=True)
class Payout:
    date: str
    amount: str


@dataclass(frozen=True)
class Proposal:
    slug: str
    stage: str  # A key of STAGES, or "unlisted"
    title: str
    author: str
    day: datetime | None  # For sorting
    date: str
    goal: float
    amount: str
    raised: str
    contributions: int
    address: str
    milestones: list[Milestone]
    payouts: list[Payout]
    merge_request: str | None
    markdown: str
    body: str = ""  # Rendered Markdown, set by render.py

    @classmethod
    def from_file(
        cls, slug: str, source: str, wallet: Wallet, merge_request: str | None
    ) -> Self:
        front, markdown = split_front_matter(source)
        stage = LAYOUTS.get(field(front, "layout"), "unlisted")
        if not all(present(front.get(key)) for key in REQUIRED):
            stage = "unlisted"
        day = parse_date(field(front, "date"))
        goal = number(field(front, "amount"))
        return cls(
            slug=slug,
            stage=stage,
            title=redact(field(front, "title")) or redact(slug),
            author=redact(field(front, "author")),
            day=day,
            date=shown_date(day) if day else redact(field(front, "date")),
            goal=goal,
            amount=shown_number(goal) or redact(field(front, "amount")),
            raised=wallet.raised,
            contributions=int(number(wallet.contributions)),
            address=wallet.address,
            milestones=[milestone(item) for item in maps(front.get("milestones"))],
            payouts=[
                p for p in map(payout, maps(front.get("payouts"))) if p.date or p.amount
            ],
            merge_request=merge_request,
            markdown=markdown,
        )

    @property
    def listed(self) -> bool:
        return self.stage in STAGES

    @property
    def url(self) -> str:
        return f"/proposals/{quote(self.slug)}.html"

    @property
    def file(self) -> str:
        return f"proposals/{self.slug}.html"

    @property
    def gitlab_url(self) -> str:
        """The merge request that added the file, or else the file's history."""
        if self.merge_request:
            return f"{REPO}/-/merge_requests/{self.merge_request}"
        return f"{REPO}/-/commits/master/{quote(self.slug)}.md"

    @property
    def back_href(self) -> str:
        return f"/{STAGES[self.stage].page}/" if self.listed else "/"

    @property
    def back_label(self) -> str:
        return STAGES[self.stage].label if self.listed else "Home"

    @property
    def milestones_done(self) -> int:
        return sum(m.done.lower() not in NOT_DONE for m in self.milestones)

    @property
    def milestones_left(self) -> int:
        return len(self.milestones) - self.milestones_done

    @property
    def summary_heading(self) -> str:
        if self.fully_funded:
            return "Fully funded"
        return STAGES[self.stage].summary if self.listed else ""

    @property
    def funding(self) -> bool:
        return self.stage == "funding-required"

    @property
    def in_progress(self) -> bool:
        return self.stage == "work-in-progress"

    @property
    def completed(self) -> bool:
        return self.stage == "completed"

    @property
    def percent(self) -> int:
        """Percent funded, rounded half up like ccs-back's percentage_funded."""
        if not self.goal:
            return 0
        # Capped before rounding, so a tiny goal can't overflow.
        return int(min(100.0, number(self.raised) * 100 / self.goal) + 0.5)

    @property
    def fully_funded(self) -> bool:
        return self.funding and self.goal > 0 and number(self.raised) >= self.goal

    @property
    def open(self) -> bool:
        """Whether the proposal takes donations."""
        return self.funding and not self.fully_funded and bool(self.address)

    @property
    def address_missing(self) -> bool:
        return self.funding and not self.fully_funded and not self.address

    def project(self) -> dict:
        """Return the row ccs-back's /index.php/projects gives for this proposal.

        As there, text is HTML-escaped and a missing address is null.
        """
        return {
            "address": self.address or None,
            "author": php_escape(self.author),
            "contributions": self.contributions,
            "date": php_escape(self.date),
            "percentage_funded": self.percent,
            "raised_amount": number(self.raised),
            "state": STAGES[self.stage].state,
            "target_amount": round(self.goal, 2),
            "title": php_escape(self.title),
        }


def split_front_matter(source: str) -> tuple[dict, str]:
    """Split a proposal into its front matter and body.

    Front matter that doesn't parse counts as none.
    """
    lines = source.removeprefix(chr(0xFEFF)).replace("\r\n", "\n").split("\n")
    if lines[0] != "---" or "---" not in lines[1:]:
        return {}, source
    end = lines.index("---", 1)
    try:
        # BaseLoader keeps every value a string, so 0xfffc doesn't become 65532.
        front = yaml.load("\n".join(lines[1:end]), Loader=yaml.BaseLoader)
    except (yaml.YAMLError, RecursionError):  # Deep nesting raises RecursionError
        front = None
    return front if isinstance(front, dict) else {}, "\n".join(lines[end + 1 :])


def field(front: dict, key: str) -> str:
    """Return a front matter value as text; lists and maps count as empty."""
    value = front.get(key)
    return value.strip() if isinstance(value, str) else ""


def present(value: object) -> bool:
    """Mirror PHP's !empty(), as ccs-back uses it: "" and "0" count as empty."""
    return bool(value) and value != "0"


def maps(value: object) -> list[dict]:
    """Return the maps in a front matter list, such as the milestones."""
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def milestone(item: dict) -> Milestone:
    return Milestone(
        name=redact(field(item, "name")),
        funds=redact(field(item, "funds")),
        done=redact(field(item, "done")),
        finished=re.fullmatch(r"finished|completed|done", field(item, "status"), re.I)
        is not None,
    )


def payout(item: dict) -> Payout:
    return Payout(
        date=redact(field(item, "date")), amount=redact(field(item, "amount"))
    )


def number(text: str) -> float:
    """Read the leading number like PHP's floatval, so "97.06 XMR" is 97.06."""
    match = LEADING_NUMBER.match(text.replace(",", "."))
    value = float(match.group()) if match else 0.0
    return value if math.isfinite(value) and value > 0 else 0.0


def shown_number(value: float) -> str:
    """Format a number the way the old site did: 97.06, 25, 9.9."""
    return f"{value:.2f}".rstrip("0").rstrip(".") if value else ""


def parse_date(text: str) -> datetime | None:
    cleaned = " ".join(ORDINAL.sub(r"\1", text.replace(",", " ")).split())
    for form in DATE_FORMS:
        try:
            return datetime.strptime(cleaned, form)
        except ValueError:
            continue
    return None


def shown_date(day: datetime) -> str:
    """Format a date the way ccs-back does: October 1, 2026."""
    return f"{day:%B} {day.day}, {day.year}"
