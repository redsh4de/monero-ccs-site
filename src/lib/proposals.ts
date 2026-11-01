import { readdir, readFile } from "node:fs/promises";
import { join } from "node:path";
import { parse } from "yaml";

import { type ProposalStatus, stages } from "@/lib/stages";

export interface Milestone {
  name: string;
  funds: string;
  done: string;
  finished: boolean;
}

export interface Payout {
  date: string;
  amount: string;
}

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
  fullyFunded: boolean;
  contributions: number;
  percentage: number;
  milestonesCompleted: number;
  address: string;
  milestones: Milestone[];
  payouts: Payout[];
  body: string;
};

interface BackendEntry {
  name?: string;
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
}

interface BackendStage {
  stage: string;
  proposals: BackendEntry[];
}

type ProposalFile = Pick<
  Proposal,
  | "id"
  | "title"
  | "author"
  | "date"
  | "amount"
  | "milestones"
  | "payouts"
  | "body"
>;

const ADDRESS = /^[48][1-9A-HJ-NP-Za-km-z]{94}$/;

export async function getProposals(): Promise<Proposal[]> {
  const root = process.env.CCS_DATA_ROOT ?? process.cwd();
  const backendStages = [
    ...(await readStages(join(root, "_data", "proposals.json"))),
    ...(await readStages(join(root, "_data", "completed-proposals.json"))),
  ];
  const files = await readProposalFiles(join(root, "proposals"));

  const listed = stages.flatMap(({ backendName, status }) => {
    const stage = backendStages.find((item) => item.stage === backendName);
    if (!stage) throw new Error(`ccs-back data has no "${backendName}" stage`);
    return stage.proposals.map((entry) =>
      toProposal(entry, status, files.get(slugOf(entry))),
    );
  });

  // every file gets a page, but only ccs-back's entries get listed
  const ids = new Set(listed.map((proposal) => proposal.id));
  const unlisted = [...files.values()]
    .filter((file) => !ids.has(file.id))
    .map((file) => toProposal({}, "unlisted", file));

  return [...listed, ...unlisted];
}

async function readStages(path: string): Promise<BackendStage[]> {
  return JSON.parse(await readFile(path, "utf8"));
}

async function readProposalFiles(
  folder: string,
): Promise<Map<string, ProposalFile>> {
  const names = (await readdir(folder)).filter((name) => name.endsWith(".md"));
  const files = await Promise.all(
    names.map(async (name) =>
      parseProposalFile(name, await readFile(join(folder, name), "utf8")),
    ),
  );
  return new Map(files.map((file) => [file.id, file]));
}

function parseProposalFile(filename: string, source: string): ProposalFile {
  const id = filename.replace(/\.md$/, "");
  const [, frontMatter = "", body = ""] =
    source.match(/^\uFEFF?---\r?\n([\s\S]*?)\r?\n---\r?\n?([\s\S]*)$/) ?? [];
  const front = parseFrontMatter(frontMatter);
  const title = clean(front.title);
  if (!title) console.warn(`No readable front matter in ${filename}`);

  return {
    id,
    title: title || id,
    author: clean(front.author),
    date: clean(front.date),
    amount: clean(front.amount),
    milestones: list(front.milestones).map((milestone) => ({
      name: clean(milestone.name),
      funds: clean(milestone.funds),
      done: clean(milestone.done),
      finished: /^(finished|completed|done)$/i.test(clean(milestone.status)),
    })),
    payouts: list(front.payouts)
      .map((payout) => ({
        date: clean(payout.date),
        amount: clean(payout.amount),
      }))
      .filter((payout) => payout.date || payout.amount),
    body,
  };
}

// failsafe keeps values as written, otherwise 0xfffc becomes 65532
function parseFrontMatter(yaml: string): Record<string, unknown> {
  try {
    return parse(yaml, { schema: "failsafe" }) ?? {};
  } catch {
    return {};
  }
}

function slugOf(entry: BackendEntry): string {
  const localUrl = clean(entry["local-url"]);
  return localUrl.match(/^\/proposals\/(.+)\.html$/)?.[1] ?? "";
}

function toProposal(
  entry: BackendEntry,
  status: ProposalStatus,
  file?: ProposalFile,
): Proposal {
  const slug = slugOf(entry) || file?.id || "";
  const title = clean(entry.name) || file?.title || "";

  const link = clean(entry["gitlab-url"]);
  const gitlabUrl = link.startsWith("https://") ? link : "";

  const amount = clean(entry.amount) || file?.amount || "";
  const amountFunded = clean(entry["amount-funded"]);
  const fullyFunded =
    status === "funding-required" &&
    Number(amount) > 0 &&
    Number(amountFunded) >= Number(amount);

  const address = entry["donate-address"] ?? "";
  const needsFunding = status === "funding-required" && !fullyFunded;
  const acceptsDonations = needsFunding && ADDRESS.test(address);
  if (needsFunding && !acceptsDonations) {
    console.warn(`No valid donation address for "${title}"`);
  }
  if (status !== "idea" && !file) console.warn(`No file for "${title}"`);
  if (!slug && !gitlabUrl) console.warn(`No link for "${title}"`);

  return {
    id: slug || gitlabUrl || title,
    status,
    title,
    // the file wins, ccs-back has 0xfffc as 65532
    author: file?.author || clean(entry.author),
    date: clean(entry.date) || file?.date || "",
    url: slug ? `/proposals/${slug}.html` : gitlabUrl,
    gitlabUrl,
    amount,
    amountFunded,
    fullyFunded,
    contributions: entry.contributions ?? 0,
    percentage: entry.percentage ?? 0,
    milestonesCompleted: entry["milestones-completed"] ?? 0,
    address: acceptsDonations ? address : "",
    milestones: file?.milestones ?? [],
    payouts: file?.payouts ?? [],
    body: file?.body ?? "",
  };
}

export function contributionsText(count: number): string {
  return `${count} ${count === 1 ? "contribution" : "contributions"}`;
}

function list(value: unknown): Record<string, unknown>[] {
  return Array.isArray(value) ? value : [];
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
