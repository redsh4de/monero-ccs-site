# Monero CCS site

The frontend for the Monero Community Crowdfunding System, built with
[Astro](https://astro.build) to match the new getmonero.org. It builds to static files
with no client-side JavaScript, and nginx serves those files. Node only builds them, and
runs on neither server.

## Development

Requires Node 22 or later, pnpm 11.4 or later, and podman. Run `pnpm preview` to see
the site with the sample data in `example-data/`. Every pnpm command runs the
dependencies' code as you, so read "Maintainer setup" in
[deploy/RELEASING.md](deploy/RELEASING.md#maintainer-setup) first.

| Command        | Action                                                                   |
| :------------- | :----------------------------------------------------------------------- |
| `pnpm install` | Install dependencies                                                     |
| `pnpm dev`     | Start a local live-reload server, with the data pages unfilled           |
| `pnpm build`   | Build the static pages and templates                                     |
| `pnpm render`  | Render `example-data/` into `dist/`, in the render container             |
| `pnpm test`    | Lint the deploy's Python and shell, then run `tests/`, in a container    |
| `pnpm preview` | Build, render, and serve `dist/` as plain files on http://localhost:4321 |
| `pnpm check`   | Type check, lint, and check the formatting                               |
| `pnpm format`  | Format with Prettier                                                     |

[deploy/README.md](deploy/README.md) explains how to run the live site, and
[deploy/ARCHITECTURE.md](deploy/ARCHITECTURE.md) how it works.
