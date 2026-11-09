# The render

`render.py` fills the templates with an input folder, and writes the pages that show
proposal data. These are the home, stage, proposal and donate pages,
`/index.php/projects`, and the RSS feed of Funding Required, `/feed.xml`.

```sh
python3 deploy/render/render.py <input-dir> <templates-dir> <out-dir> [<cache-dir>]
```

The cache folder is optional, and speeds up later renders. The input folder holds:

| Path             | Contents                                                    | Source                   |
| :--------------- | :---------------------------------------------------------- | :----------------------- |
| `proposals/*.md` | The proposal files                                          | ccs-proposals repository |
| `mr/open.json`   | The open merge requests                                     | GitLab API               |
| `mr/open.tsv`    | The files that open merge requests add                      | GitLab API               |
| `mr/merged.tsv`  | The merge request that added each file                      | ccs-proposals history    |
| `values`         | Amounts raised, contributions, addresses and QR code hashes | Backend                  |
| `general-fund`   | The General Fund address                                    | Address list             |
| `site`           | The address the site is served at, for the feed's links     | `SITE` in the config     |

The render needs Python 3 and the Debian 13 packages in `deploy/Containerfile.render`.
`pnpm render` runs the render in that image, so a preview uses production's versions.
