# Threat model

**Two attacks can change what visitors see, with no alert: a `markdown-it-py` bug, and
a web server singling out visitors.**

---

## What checks each input

`checks.py` makes every decision to accept or refuse pushed or signed bytes. It reads
each input once, with a size cap, decides on those bytes, and refuses anything
unexpected.

| Input                                    | From                              | Arrives by                       | Checked by                                 | Lands in                                 |
| ---------------------------------------- | --------------------------------- | -------------------------------- | ------------------------------------------ | ---------------------------------------- |
| Values                                   | ccs-back, on the backend          | `values.py`, then the SSH push   | `checks.py values`, then `checks.py apply` | `/var/lib/ccs/state/values`              |
| The address list                         | The address signer                | By hand, into `/etc/ccs/`        | `checks.py addresses`                      | `values`, `general-fund` and `qr`        |
| Proposal texts and their merge history   | GitLab: `ccs-proposals`           | `fetch.sh`, with `git fetch`     | TLS; the render escapes the text           | `/var/lib/ccs/fetch/trees/`              |
| Open merge requests                      | GitLab's merge request API        | `fetch.sh`, with `mrs.py`        | TLS; the render escapes the text           | `/var/lib/ccs/fetch/mr-*/`               |
| A release: templates, render and scripts | GitHub, signed by two maintainers | `install.sh`, run by an operator | `checks.py release`                        | `/usr/local/lib/ccs/releases/`           |
| The live site's `status.json`            | The web server                    | The monitor, over Tor            | `checks.py status` and `snapshot`          | The monitor's run folder                 |

The push key can only replace `/var/lib/ccs/in/values`, and only from the backend's
address.

Some code only extracts data. `export_tree` in `lib.sh` reads the merge history, and
`mrs.py` reads GitLab's JSON. `status.py restore` rebuilds the monitor's merge request
input from `status.json`, and `fetch.sh` reads the `gitlab` line. Their output feeds
only the render, which escapes everything it shows, or the fetch schedule.

---

## Threats

| Attacker controls                                     | Tries to                                                   | Outcome                                                                                                                                                                                                                                | Stopped or seen by                               |
| ----------------------------------------------------- | ---------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------ |
| The backend                                           | Show their own address                                     | Refused: not on the address list, so the box shows none, and the monitor alerts                                                                                                                                                        | `checks.py apply`, the `addresses` alert         |
| The backend                                           | Inject markup through a value                              | Refused: each line must match a whole pattern                                                                                                                                                                                          | `checks.py values`                               |
| The backend                                           | Send a cut-off or doubled file                             | Refused: the file must end with one newer `updated` line. `checks.py` drops every line of a repeated proposal                                                                                                                          | `checks.py values`                               |
| The backend                                           | Change text, stage, percent or which box shows             | Not possible: the web server works all of it out                                                                                                                                                                                       | The render                                       |
| The backend                                           | Give one proposal's address to another                     | Refused: each address stays pinned to the first proposal that shows it. The site keeps its last good values                                                                                                               | `checks.py values`, the `stale-values` alert     |
| The backend                                           | Pick a new proposal's first address                        | Possible, but only with a listed address that no proposal showed before. Every listed address pays the CCS wallet, and the backend keeps its books anyway                                                                                                             | Out of scope                                     |
| The backend                                           | Stop sending                                               | The values go stale, and the web server alerts                                                                                                                                                                                         | The `stale-values` alert in `fetch.sh`           |
| The backend                                           | Lie in the `gitlab` line                                   | Extra fetches, or content up to an hour late                                                                                                                                                                                           | `fetch.sh`, which fetches at least hourly        |
| One signer's key or machine                           | Ship a release                                             | Refused: a release needs two distinct signers                                                                                                                                                                                          | `checks.py release`                              |
| Anyone adding an attestation                          | Count as a release signer                                  | Not possible: only the keys in `release-signers.gpg` count, and any other is skipped                                                                                                                                                   | `checks.py release`                              |
| A revoked or expired signer key                       | Count as a signer                                          | Refused: only a good signature counts                                                                                                                                                                                                  | `checks.py release`, `checks.py addresses`       |
| The release host or its account                       | Swap files                                                 | Refused: the hash is in the signed file                                                                                                                                                                                                | `checks.py release`                              |
| The repository, through a stolen token or a bad merge | Change the scripts that build and show a release           | Both signers read the scripts' diff before they run them, and `release.sh` runs only the tag's                                                                                                                                         | The signers, `release.sh`                        |
| An old release or list                                | Roll back                                                  | Refused: the version must rise, and lists arrive only by hand. The monitor alerts on a site release older than the backend's, unless they differ only in backend scripts                                                               | `install.sh`, `checks.py release`, `old-release` |
| A dependency, in both signers' builds                 | Plant something in templates, CSS, fonts or images         | Text shows in the diff both signers read, and a control byte in it stops the build; templates run in Jinja's sandbox; a script, a link to an unlisted host, address-like text, or a font or image not in the tag stops the build       | `release.sh`, `checks.py build`, the render      |
| Anyone opening a merge request                        | Put a script, markup, a link or an address in a title      | Escaped, never read as Markdown, and any address replaced with `[address removed]`                                                                                                                                                     | The render                                       |
| A merged proposal                                     | Add raw HTML, a `javascript:` link, an image or an address | The render keeps only `details`, `summary` and `sup`, and replaces any block with an address, or the whole text for one split across blocks, by `[address removed]`                                                                    | The render                                       |
| `qrencode`, through a bug or a planted change         | Show a QR code for another address                         | Refused: `address-list.py` reads each code back with zbar, an unrelated decoder, from the bits the servers draw, before the signer signs. The servers run no encoder                                                                   | `address-list.py`                                |
| A bug in `markdown-it-py`                             | Emit unexpected HTML                                       | Contained, not detected: the CSP allows no script, and the monitor renders the same HTML                                                                                                                                               | nginx                                            |
| A bug in nginx                                        | Take over the web server                                   | Contained: nginx runs in its own sandbox                                                                                                                                                                                               | `web/nginx-sandbox.conf`                         |
| Anyone on the internet                                | Guess the web server's SSH password                        | Refused: `install.sh` stops while sshd's config files let a password alone log in, so reload ssh after a change; `Match` blocks stay the operator's                                                                                    | `install.sh`                                     |
| A bug in the render or a template                     | Show an address the list lacks                             | Refused: before the switch, `checks.py site` scans every rendered file, links and attributes included; the monitor scans its own render too                                                                                            | `update.sh`, the `unlisted` alert                |
| A stale cache entry                                   | Serve an outdated proposal                                 | Detected: the monitor renders without the cache                                                                                                                                                                                        | `monitor.sh`                                     |
| The network to GitLab                                 | Change fetched content                                     | Refused: `git` and `curl` verify TLS                                                                                                                                                                                                   | `fetch.sh`                                       |
| The web server                                        | Serve anything                                             | Possible for visitors it can single out, the monitor included; otherwise the monitor's compare detects it                                                                                                                              | `monitor.sh`                                     |
| The web server                                        | Show wrong vote counts on the Ideas page                   | Not detected: the monitor compares the merge requests with GitLab's without their vote counts, which change faster than the site fetches them                                                                                          | `status.py mrs`                                  |

**The web server can tell the monitor apart from a visitor.** The monitor fetches
`status.json` and the pages over two new Tor circuits each run, and asks for each page
as Tor Browser does. But curl's TLS still differs from a browser's.

The alert URL reaches only the two units that send alerts, as a systemd credential, and
`install.sh`, which posts each install. The render never sees it.

The monitor renders the merge requests that the web server sends. `checks.py snapshot`
first limits them to what GitLab itself could send, so a web server can feed the monitor
no more than any GitLab user can. The monitor's unit also runs with
`PrivateNetwork=yes`: it reaches only Tor's socket, so the wallet's RPC stays out of
reach. Of the backend's files, it sees only ccs-back's storage folder and the values, so
even an exploit of it cannot read the wallet's files.
