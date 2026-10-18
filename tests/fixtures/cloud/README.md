# OpenMyMemo Cloud protocol fixtures

These files pin the wire format between the MyMemo service and the computer: bridge frames (`bridge/`), linking
(`link/`), a real HPKE test vector for the offline queue (`hpke_vector.json`), and the OpenMyMemo Assistant's
sign-in and hosted model endpoint (`assistant/`, product-design.md §17.8: tokens, `/models` and error answers). The product description is
`docs/product-design.md` §16 in the openmymemo repository. The MyMemo service keeps a byte-identical copy and
tests against it, so a change here is a protocol change and must land on both sides; the computer's tests build
their fake service (`tests/cloud_fake.py`) from these files.

Everything secret-looking here is test-only: the X25519 key is derived from a fixed string, and the `omd_`, `oma_`, `omr_` and
`omc_` values are placeholders. Never use them outside tests.
