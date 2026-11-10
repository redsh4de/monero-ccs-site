"""Everything the pages show, built from the input."""

from dataclasses import dataclass
from datetime import datetime
from urllib.parse import quote

from inputs import Inputs, Wallet
from proposals import GITLAB, STAGES, Proposal, shown_date
from safety import redact

HOME_CARDS = 4


@dataclass(frozen=True)
class Idea:
    title: str
    author: str
    url: str
    date: str
    upvotes: int
    downvotes: int
    created: str  # As GitLab sent it, for sorting

    # Read by the card template, like a proposal's fields.
    stage = "idea"
    amount = ""
    listed = False


@dataclass(frozen=True)
class Site:
    proposals: list[Proposal]  # Every proposal file, newest first
    ideas: list[Idea]
    general_fund: str  # Its address, or ""

    @property
    def listed(self) -> list[Proposal]:
        return [proposal for proposal in self.proposals if proposal.listed]

    @property
    def stage_pages(self) -> dict[str, list[Proposal] | list[Idea]]:
        """Each stage page's path, also its template's name, mapped to its cards."""
        pages = {"ideas": self.ideas}
        for name, stage in STAGES.items():
            pages[stage.page] = [p for p in self.listed if p.stage == name]
        return pages

    @property
    def counts(self) -> dict[str, int]:
        return {page: len(cards) for page, cards in self.stage_pages.items()}

    @property
    def home(self) -> dict:
        funding = [proposal for proposal in self.listed if proposal.funding]
        return {
            "needs_funding": [p for p in funding if p.open][:HOME_CARDS],
            "funding_count": len(funding),
            "see_all": len(funding) > HOME_CARDS,
        }

    @property
    def projects(self) -> list[dict]:
        return [proposal.project() for proposal in self.listed]

    @property
    def addresses_shown(self) -> set[str]:
        """The addresses that need a QR code."""
        shown = {proposal.address for proposal in self.proposals if proposal.open}
        return (shown | {self.general_fund}) - {""}


def build(inputs: Inputs) -> Site:
    proposals = [
        Proposal.from_file(
            slug,
            source,
            inputs.wallets.get(quote(slug, safe=""), Wallet()),
            inputs.merged.get(f"{slug}.md"),
        )
        for slug, source in inputs.proposals.items()
    ]
    return Site(
        proposals=sorted(proposals, key=newest_first),
        ideas=open_ideas(inputs.merge_requests, set(inputs.proposals)),
        general_fund=inputs.general_fund,
    )


def newest_first(proposal: Proposal) -> tuple[int, int, str]:
    """Sort key: dated proposals newest first, then the rest by name."""
    if proposal.day is None:
        return (1, 0, proposal.slug)
    return (0, -proposal.day.toordinal(), proposal.slug)


def open_ideas(merge_requests: list[dict], slugs: set[str]) -> list[Idea]:
    """Return the ideas among the open merge requests, newest first.

    `slugs` holds the proposals already in the repo. If two merge requests add
    the same file, the first in GitLab's list wins.
    """
    ideas, taken = [], set()
    for mr in merge_requests:
        if len(mr["adds"]) != 1:
            continue
        name = mr["adds"][0]
        slug = name.removesuffix(".md")
        if not is_new_proposal(name) or slug in slugs or name in taken:
            continue
        try:
            created = datetime.fromisoformat(mr["created_at"])
        except ValueError:
            continue
        taken.add(name)
        ideas.append(
            Idea(
                title=redact(mr["title"], ignore_spaces=True),
                author=redact(mr["author"], ignore_spaces=True),
                url=mr["web_url"] if mr["web_url"].startswith(GITLAB) else "",
                date=shown_date(created),
                upvotes=mr["upvotes"],
                downvotes=mr["downvotes"],
                created=mr["created_at"],
            )
        )
    return sorted(ideas, key=lambda idea: idea.created, reverse=True)


def is_new_proposal(name: str) -> bool:
    return name.endswith(".md") and len(name) > 3 and "/" not in name
