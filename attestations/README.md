# Attestations

Each folder `site-vN/<builder>/` holds one builder's attestation of release N: their
`SHA256SUMS`, and their signature over it, `SHA256SUMS.asc`. `deploy/signer/release.py`
writes them. [RELEASING.md](../deploy/docs/RELEASING.md#attestations) says how to add one and
how to check them.
