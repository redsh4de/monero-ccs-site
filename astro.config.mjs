// @ts-check
import mdx from "@astrojs/mdx";
import { defineConfig, passthroughImageService } from "astro/config";

export default defineConfig({
  build: { format: "preserve", inlineStylesheets: "never" },
  integrations: [mdx()],
  image: { service: passthroughImageService() },
  // one stylesheet for every page, and no data: URLs, which a CSP would have to allow
  vite: { build: { assetsInlineLimit: 0, cssCodeSplit: false } },
  markdown: { syntaxHighlight: false },
});
