// @ts-check
import mdx from "@astrojs/mdx";
import { defineConfig } from "astro/config";

// https://astro.build/config
export default defineConfig({
  output: "static",
  trailingSlash: "ignore",
  build: { format: "preserve" },
  integrations: [mdx()],
  markdown: { syntaxHighlight: false },
});
