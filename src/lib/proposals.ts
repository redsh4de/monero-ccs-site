import { readFile } from "node:fs/promises";
import { join } from "node:path";

export type ProposalStatus =
  | "idea"
  | "funding-required"
  | "work-in-progress"
  | "completed";

export type Proposal = {
  id: string;
  status: ProposalStatus;
  title: string;
  author: string;
  date: string;
  url: string;
  gitlabUrl: string;
  amount: string;
  amountFunded: string;
  contributions: number;
  percentage: number;
  milestonesCompleted: number;
  address: string;
  qrCode: string;
};

interface BackendEntry {
  name: string;
  author?: string;
  date?: string;
  amount?: number | string;
  contributions?: number;
  percentage?: number;
  "amount-funded"?: number | string;
  "milestones-completed"?: number;
  "gitlab-url"?: string;
  "local-url"?: string;
  "donate-address"?: string;
  "donate-qr-code"?: string;
}

interface BackendStage {
  stage: string;
  proposals: BackendEntry[];
}

const STAGES: [string, ProposalStatus][] = [
  ["Ideas", "idea"],
  ["Funding Required", "funding-required"],
  ["Work in Progress", "work-in-progress"],
  ["Completed Proposals", "completed"],
];

const ADDRESS = /^[48][1-9A-HJ-NP-Za-km-z]{94}$/;
const QR_CODE = /^data:image\/png;base64,\s?[A-Za-z0-9+/=]+$/;

export async function getProposals(): Promise<Proposal[]> {
  const root = process.env.CCS_DATA_ROOT ?? process.cwd();
  const stages = [
    ...(await readStages(join(root, "_data", "proposals.json"))),
    ...(await readStages(join(root, "_data", "completed-proposals.json"))),
  ];

  return STAGES.flatMap(([name, status]) => {
    const stage = stages.find((item) => item.stage === name);
    if (!stage) throw new Error(`ccs-back data has no "${name}" stage`);
    return stage.proposals.map((entry) => toProposal(entry, status));
  });
}

async function readStages(path: string): Promise<BackendStage[]> {
  return JSON.parse(await readFile(path, "utf8"));
}

function slugOf(entry: BackendEntry): string {
  const localUrl = clean(entry["local-url"]);
  return localUrl.match(/^\/proposals\/(.+)\.html$/)?.[1] ?? "";
}

function toProposal(entry: BackendEntry, status: ProposalStatus): Proposal {
  const slug = slugOf(entry);
  const title = clean(entry.name);

  const link = clean(entry["gitlab-url"]);
  const gitlabUrl = link.startsWith("https://") ? link : "";

  const address = entry["donate-address"] ?? "";
  const qrCode = entry["donate-qr-code"] ?? "";
  const donatable = status === "funding-required" && ADDRESS.test(address);
  if (status === "funding-required" && !donatable) {
    console.warn(`No valid donation address for "${title}"`);
  }

  return {
    id: slug || gitlabUrl || title,
    status,
    title,
    author: clean(entry.author),
    date: clean(entry.date),
    url: slug ? `/proposals/${slug}.html` : gitlabUrl,
    gitlabUrl,
    amount: clean(entry.amount),
    amountFunded: clean(entry["amount-funded"]),
    contributions: entry.contributions ?? 0,
    percentage: entry.percentage ?? 0,
    milestonesCompleted: entry["milestones-completed"] ?? 0,
    address: donatable ? address : "",
    qrCode: donatable && QR_CODE.test(qrCode) ? qrCode : "",
  };
}

// ccs-back escapes its text and astro would escape it again. bidi controls
// are dropped so a title can't reorder text next to an address
function clean(value: unknown): string {
  if (typeof value !== "string" && typeof value !== "number") return "";
  return String(value)
    .replaceAll("&lt;", "<")
    .replaceAll("&gt;", ">")
    .replaceAll("&quot;", '"')
    .replaceAll("&#039;", "'")
    .replaceAll("&#39;", "'")
    .replaceAll("&amp;", "&")
    .replace(/\p{Bidi_Control}/gu, "");
}
