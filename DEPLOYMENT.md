# Deployment

Astro builds the site design as templates. On the web server, the render fills these
templates with the proposals and their values. Node does not run on the web server or
the backend.

TODO: Add the release, web server and backend sections when the deploy scripts are made.

## Glossary

- **Backend:** The server that runs ccs-back. ccs-back watches the donation wallets. The
  backend sends the values to the web server.
- **Data pages:** The pages that show proposal data: the home page, the stage pages
  (Ideas, Funding Required, Work in Progress and Completed), the proposal pages, the
  donate page and `/index.php/projects`.
- **Render:** The Python script in `deploy/render/`. It fills the templates and writes
  the data pages.
- **Signed address list:** The donation addresses that a Core Team member signs. The web
  server shows only addresses on this list.
- **Templates:** The Jinja files that `pnpm build` writes to `dist/templates/`.
- **Values:** The amount raised, the number of contributions and the donation address of
  each proposal.

## Render

`deploy/render/render.py` writes the data pages. It reads an input folder and the
templates from `pnpm build`.

```sh
python3 deploy/render/render.py <in> <templates> <out> [<cache>]
```

The cache folder is optional. It makes later renders faster.

The render uses Python 3 and these Debian 13 packages: `python3-yaml`,
`python3-markdown-it`, `python3-mdit-py-plugins`, `python3-jinja2` and `qrencode`.
`pnpm render:image` builds a container image with the same packages, from
`deploy/Containerfile.render`.

### Input folder

| Path             | Contents                                    | Source                   |
| :--------------- | :------------------------------------------ | :----------------------- |
| `proposals/*.md` | The proposal files                          | ccs-proposals repository |
| `mr/open.json`   | The open merge requests                     | GitLab API               |
| `mr/open.tsv`    | The files that open merge requests add      | GitLab API               |
| `mr/merged.tsv`  | The merge request that added each file      | ccs-proposals history    |
| `values`         | Amounts raised, contributions and addresses | Backend                  |
| `general-fund`   | The General Fund address                    | Signed address list      |

`example-data/` is a sample input folder. `pnpm render` renders it into `dist/`.

TODO: Explain how `fetch.sh` writes `proposals/` and `mr/`, how the backend sends `values`,
and how `update.sh` writes `general-fund`.

TODO: Add a preview with live data, when `fetch.sh` can write a local input folder.
