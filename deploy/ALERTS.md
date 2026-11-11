# Alerts

Each alert message ends with its alert name in parentheses, such as `(status)` or
`(page-<hash>)`. Look the name up below.

## Web server alerts

Look first in `journalctl -u ccs-fetch -u ccs-update` on the web server, then in
`journalctl -u ccs-values -u ccs-push` on the backend. These alerts repeat every 50
minutes.

| Alert          | Fires when                                                       | Usual causes                                                                                                                                                |
| -------------- | ---------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `stale-values` | No new values for 10 minutes. Each fetch checks, at least hourly | "In the future": clock skew. A traceback: a render bug, so go back one release. A refused key: see [Keys and certificates](README.md#keys-and-certificates). "Moves off its address" or "shows the address of": the backend changed a proposal's address, so check the backend for a compromise |
| `fetch`        | Fetches from GitLab fail for 50 minutes                          | GitLab down or stalling. An error about objects or a lock: [repair the web server](README.md#repair-the-web-server)                                         |

## Monitor alerts

Look first in `journalctl -u ccs-monitor` on the backend, then on the web server as
above. These alerts fire after 10 minutes, unless the row says otherwise, and repeat
daily.

| Alert                            | Fires when                                                                                  | Usual causes                                                                                                                               |
| -------------------------------- | ------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------ |
| `key-expiry`                     | A release or address signer's key expires within 30 days, or has                            | [Extend that signer's key](RELEASING.md#extend-a-signer-key)                                                                               |
| `status`                         | `status.json` is missing or malformed                                                       | Tor down, an expired certificate, or the web server down                                                                                   |
| `release`                        | The site's release does not download or verify                                              | GitHub unreachable over Tor, or an expired signer key                                                                                      |
| `old-release`                    | The site runs an older release, which differs outside the backend's scripts and docs        | The backend upgraded before the web server                                                                                                 |
| `commit`                         | The site's commit is not in ccs-back's checkout                                             | Content from somewhere other than GitLab                                                                                                   |
| `stall`                          | After 1 hour: the site's commit is behind ccs-back's                                        | The web server's fetch failing: see `fetch`                                                                                                |
| `snapshot`                       | `status.json`'s merge requests are malformed                                                | A site that is not the web server's render                                                                                                 |
| `merge-requests`                 | After 75 minutes: the site's merge requests differ from GitLab's, or GitLab does not answer | GitLab unreachable over Tor, or the site's merge requests stale                                                                            |
| `addresses`                      | The address list does not verify, or a proposal taking donations shows no address           | An expired address signer key (`gpg --show-keys /etc/ccs/address-signers.gpg`), a list that ran out, or `values.py` leaving a proposal out |
| `unlisted`                       | A page of the render shows an address the list lacks, in its text, a link or an attribute   | A bug in the render or a template. The web server keeps its site as it was: go back one release                                            |
| `page-<hash>`, one for each page | The page differs from the expected render                                                   | Many pages: the list, the release or the render packages differ between the machines. One page: a damaged cache file, or a compromise      |
| `monitor`                        | The monitor crashed                                                                         | A bug, or a full disk on the backend                                                                                                       |

To see `status.json` as the monitor does, run on the backend:

```sh
curl -x socks5h://localhost/run/tor/socks -sSI https://ccs.getmonero.org/status.json
```

When nothing arrives, run
`journalctl -u ccs-monitor -u ccs-fetch | grep 'alert not sent'` on both machines. Usual
causes: the channel refuses posts, Tor is down, or `/etc/ccs/alert_url` is empty.

---

## How alerts work

An alert goes out only when its check still fails at the end of its hold.

- It goes out again after each repeat. When the check passes, one "resolved" message
  with the same name follows.
- The hold counts from the first failing run since the last pass. The monitor runs every
  15 to 25 minutes, so a 10-minute hold alerts on its second failing run.
- When `status`, `release`, `old-release`, `commit` or `snapshot` fails, the run stops
  there. Later checks neither fire nor resolve until it passes.
- Each run sends all its messages in one post, and to the journal. If the post fails,
  the next run sends them again.
- Each failing check has one file in `/var/lib/ccs/fetch/alerts/` on the web server, or
  `/var/lib/private/ccs-monitor/alerts/` on the backend. It holds
  `<since> <sent> <message>`, with Unix times.

To pause alerts during known work, empty `/etc/ccs/alert_url`. Alerts that come due
meanwhile go out once you restore it.
