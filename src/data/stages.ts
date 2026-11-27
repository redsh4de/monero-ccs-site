export const stages = [
  {
    name: "idea",
    slug: "ideas",
    icon: "lightbulb",
    label: "Ideas",
    short: "Ideas",
    description:
      "Anyone with an idea for a feature, task, or service can pitch it for discussion.",
  },
  {
    name: "funding-required",
    slug: "funding-required",
    icon: "hand-coins",
    label: "Funding Required",
    short: "Funding",
    description: "Once approved, the idea moves to community fundraising.",
  },
  {
    name: "work-in-progress",
    slug: "work-in-progress",
    icon: "cog",
    label: "Work in Progress",
    short: "Progress",
    description:
      "Funded ideas get picked up, and their progress can be monitored.",
  },
  {
    name: "completed",
    slug: "completed-proposals",
    icon: "package",
    label: "Completed",
    short: "Completed",
    description:
      "The work is finished once all milestones are met and all funds are paid out.",
  },
] as const;

export type StageName = (typeof stages)[number]["name"];
