# OpenMyMemo Cloud protocol fixtures

Shared, byte-identical copies live in two repositories:

- mymemo-service: `tests/fixtures/openmymemo_cloud/`
- openmymemo: `tests/fixtures/cloud/`

They pin the wire format between the service and the computer: bridge frames (`bridge/`), linking (`link/`) and a
real HPKE test vector for the offline queue (`hpke_vector.json`). The prose authority is mymemo-service
`docs/design-notes/openmymemo-cloud-bridge-2026-10-12.md` §3–§5; the product authority is openmymemo
`docs/product-design.md` §16. Each side's tests load these files: the service's fake computer and the computer's
fake service are built from them, so a change here is a protocol change and must land in both repositories.

Everything secret-looking here is test-only: the X25519 key is derived from a fixed string, and the `omd_` and
`omc_` values are placeholders. Never use them outside tests.
