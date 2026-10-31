import { defineCollection } from "astro:content";
import { z } from "astro/zod";

import { getProposals, type Proposal } from "@/lib/proposals";

const proposals = defineCollection({
  loader: getProposals,
  schema: z.custom<Proposal>(),
});

export const collections = { proposals };
