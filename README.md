# Monero CCS site

The frontend for the Monero Community Crowdfunding System, built with
[Astro](https://astro.build) to match the new getmonero.org. It builds to static files
with no client-side JavaScript.

## Development

Requires Node 22 or later and pnpm.

`pnpm dev` and `pnpm check` use the example proposals in `example-data/`. `pnpm build`
reads `_data/` and `proposals/` from ccs-back, and fails if they're missing.

| Command         | Action                                      |
| :-------------- | :------------------------------------------ |
| `pnpm install`  | Install dependencies                        |
| `pnpm dev`      | Start the dev server at `localhost:4321`    |
| `pnpm build`    | Build the site from ccs-back's data         |
| `pnpm build:example` | Build the site from `example-data/`    |
| `pnpm check`    | Type check                                  |
| `pnpm lint`     | Lint TypeScript and Astro files             |
| `pnpm lint:css` | Lint CSS                                    |
| `pnpm format`   | Format with Prettier                        |
