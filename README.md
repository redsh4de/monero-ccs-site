# Monero CCS site

The frontend for the Monero Community Crowdfunding System, built with
[Astro](https://astro.build) to match the new getmonero.org. It builds to static files
with no client-side JavaScript, and nginx serves those files. Node only builds them, and
runs on neither server.

## Development

Requires Node 22 or later, pnpm 11.4 or later, and podman. Run `pnpm dev` to see
the site with the sample data in `example-data/`. Every pnpm command runs the
dependencies' code as you, so read "Maintainer setup" in
[deploy/RELEASING.md](deploy/RELEASING.md#maintainer-setup) first.

| Command            | Action                                                                      |
| :----------------- | :-------------------------------------------------------------------------- |
| `pnpm install`     | Install dependencies                                                        |
| `pnpm dev`         | Serve the site on http://localhost:4321, and rebuild it on each change      |
| `pnpm build`       | Build the static pages and templates                                        |
| `pnpm render`      | Render `example-data/` into `dist/`, in the render container                |
| `pnpm check`       | Type check, lint, and check the formatting                                  |
| `pnpm test`        | Lint the deploy's Python and shell, then run `tests/`, in a container       |
| `pnpm test:deploy` | Build, then run the deploy scripts' tests in `tests/deploy/`, in containers |
| `pnpm format`      | Format with Prettier                                                        |

`pnpm render` fills the pages that `pnpm build` made, so build first. CI and the release
build use Node 26.8.1.

The pages in `src/pages/templates/` are Jinja templates. Astro builds them with their
`[[ ]]` and `[% %]` marks left in, and `deploy/render/` fills them with the proposal data.
`dev/` holds the scripts behind `pnpm dev` and `pnpm render`.

[tests/deploy/README.md](tests/deploy/README.md) explains the deploy tests.
[deploy/README.md](deploy/README.md) explains how to run the live site, and
[deploy/ARCHITECTURE.md](deploy/ARCHITECTURE.md) how it works.
