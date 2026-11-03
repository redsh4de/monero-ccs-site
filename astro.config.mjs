// @ts-check
import mdx from "@astrojs/mdx";
import { defineConfig } from "astro/config";

// https://astro.build/config
export default defineConfig({
  output: "static",
  trailingSlash: "ignore",
  build: { format: "preserve", inlineStylesheets: "never" },
  integrations: [mdx()],
  // one stylesheet for every page, and no data: URLs, which a CSP would have to allow
  vite: { build: { assetsInlineLimit: 0, cssCodeSplit: false } },
  markdown: { syntaxHighlight: false },
});
