export const stages = [
  {
    status: "idea",
    slug: "ideas",
    icon: "lightbulb",
    color: "var(--status-idea-color)",
    label: "Ideas",
    short: "Ideas",
    description:
      "Anyone with an idea for a feature, task, or service can pitch it for discussion.",
  },
  {
    status: "funding-required",
    slug: "funding-required",
    icon: "hand-coins",
    color: "var(--status-funding-color)",
    label: "Funding Required",
    short: "Funding",
    description: "Once approved, the idea moves to community fundraising.",
  },
  {
    status: "work-in-progress",
    slug: "work-in-progress",
    icon: "cog",
    color: "var(--status-wip-color)",
    label: "Work in Progress",
    short: "Progress",
    description:
      "Funded ideas get picked up, and their progress can be monitored.",
  },
  {
    status: "completed",
    slug: "completed-proposals",
    icon: "package",
    color: "var(--status-completed-color)",
    label: "Completed",
    short: "Completed",
    description:
      "The work is finished once all milestones are met and all funds are paid out.",
  },
] as const;

export type ProposalStatus = (typeof stages)[number]["status"];
