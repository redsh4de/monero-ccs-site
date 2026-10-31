export const stages = [
  {
    status: "idea",
    backendName: "Ideas",
    slug: "ideas",
    icon: "lightbulb",
    color: "var(--status-idea-color)",
    label: "Ideas",
    short: "Ideas",
    summary: "",
    step: "Pitch",
    description:
      "Anyone with an idea for a feature, task, or service can pitch it for discussion.",
  },
  {
    status: "funding-required",
    backendName: "Funding Required",
    slug: "funding-required",
    icon: "hand-coins",
    color: "var(--status-funding-color)",
    label: "Funding Required",
    short: "Funding",
    summary: "Contribute",
    step: "Fund",
    description: "Once approved, the idea moves to community fundraising.",
  },
  {
    status: "work-in-progress",
    backendName: "Work in Progress",
    slug: "work-in-progress",
    icon: "cog",
    color: "var(--status-wip-color)",
    label: "Work in Progress",
    short: "Progress",
    summary: "Progress",
    step: "Work",
    description:
      "Funded ideas get picked up, and their progress can be monitored.",
  },
  {
    status: "completed",
    backendName: "Completed Proposals",
    slug: "completed-proposals",
    icon: "package",
    color: "var(--status-completed-color)",
    label: "Completed",
    short: "Completed",
    summary: "Funded",
    step: "Complete",
    description:
      "The work is finished once all milestones are met and all funds are paid out.",
  },
] as const;

export type ProposalStatus = (typeof stages)[number]["status"] | "unlisted";
