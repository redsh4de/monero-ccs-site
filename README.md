# Monero CCS site

The frontend for the Monero Community Crowdfunding System, built with
[Astro](https://astro.build) to match the new getmonero.org. It builds to static files
with no client-side JavaScript.

## Development

Requires Node 22 or later, pnpm and podman. Run `pnpm render:image` once, then
`pnpm preview` to see the site with the sample data in `example-data/`.

| Command             | Action                                             |
| :------------------ | :------------------------------------------------- |
| `pnpm install`      | Install dependencies                               |
| `pnpm render:image` | Build the render container (once)                  |
| `pnpm dev`          | Start the dev server, with the data pages unfilled |
| `pnpm build`        | Build the static pages and templates               |
| `pnpm render`       | Render `example-data/` into `dist/`                |
| `pnpm preview`      | Build, render and serve the site                   |
| `pnpm check`        | Type check                                         |
| `pnpm lint`         | Lint TypeScript and Astro files                    |
| `pnpm lint:css`     | Lint CSS                                           |
| `pnpm format`       | Format with Prettier                               |

[DEPLOYMENT.md](DEPLOYMENT.md) explains how the live site fills in its data.
