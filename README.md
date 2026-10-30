# Monero CCS site

The frontend for the Monero Community Crowdfunding System, built with
[Astro](https://astro.build) to match the new getmonero.org. It builds to static files
with no client-side JavaScript.

## Development

Requires Node 22 or later and pnpm.

| Command         | Action                                      |
| :-------------- | :------------------------------------------ |
| `pnpm install`  | Install dependencies                        |
| `pnpm dev`      | Start the dev server at `localhost:4321`    |
| `pnpm build`    | Build the site to `./dist/`                 |
| `pnpm check`    | Type check                                  |
| `pnpm lint`     | Lint TypeScript and Astro files             |
| `pnpm lint:css` | Lint CSS                                    |
| `pnpm format`   | Format with Prettier                        |
