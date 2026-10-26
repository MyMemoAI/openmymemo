# MyMemo Memory sync protocol fixtures

These files pin the wire format of MyMemo Memory sync (openmymemo `docs/product-design.md` §20; MyMemo service
`docs/design-notes/member-memory-2026-10-26.md` §13 and `docs/product-specs/member-memory-api.md` §6). The MyMemo
service keeps a byte-identical copy in `tests/fixtures/openmymemo_cloud/sync/`, and a test on each side checks the
digest of this folder, so a change here is a protocol change and must land on both sides.

Sync runs over the OpenMyMemo Cloud bridge (`openmymemo-bridge.v1`). It is on only when the person turned it on in
OpenMyMemo (a consent separate from linking) and MyMemo accepts it for the account. The folder is the source of
truth: MyMemo keeps a copy that follows it. Every frame is a JSON text frame with `"v": 1`.

## Turning it on

- `hello.features` contains `"sync"` only while sync is on in OpenMyMemo. MyMemo ignores it unless the account's
  MyMemo Memory is on and MyMemo's switch for sync is on.
- `sync_hello.json` (computer → MyMemo), right after `welcome`: `sync_id` is a random 32-hex id made each time the
  person turns sync on, so a new consent is never mistaken for the old one (MyMemo turns sync off per `sync_id`).
- `sync_status.json` (MyMemo → computer), the answer, and again whenever the answer changes:
  - `accepted`; when false, `reason` is `unavailable` (MyMemo's switch is off), `memory_off` (MyMemo Memory is off
    or not included in the account's plan: `sync_status_refused.json`), `off` (sync was turned off for this
    `sync_id`, on MyMemo's memory page or by `sync_off`: `sync_status_off.json`; the computer turns sync off and says
    so), or `invalid` (a malformed `sync_hello`).
  - `merge`: `needed` for this `sync_id`'s first sync (§ First merge), else `done`.
  - `seq`: the last `sync_push` MyMemo applied; `cursor`: the last `sync_ops` seq the computer acknowledged.
  - `cloud.memories`: with `merge: needed`, how many memories MyMemo will send down for the merge.

## MyMemo → computer: `sync_ops`

`sync_ops.json` and `sync_ops_merge.json`. MyMemo sends, in order, what it has for the folder after the computer's
cursor: `cursor` is the last op's `seq`, `more` whether MyMemo had more waiting when it sent the frame. A frame is at
most about 512 KB. Ops are sent again (same `seq`) until a `sync_push` acknowledges them with its `cursor`, so filing
must be idempotent. Each op has a `seq` and a `from`:

- `from: "app"`: a proposal by one of MyMemo's AI features or a web app, which MyMemo has already applied to its
  copy if its rules allowed (provisionally). The computer files it through `proposals.py` as `connector.id`
  (always `cloud-…`: `cloud-askmemo`, `cloud-agent`, a web app's `cloud-<slug>`, `cloud-mymemo` for a MyMemo edit
  the folder overrode) with `idempotency_key` = `proposal_id` and `created_at`. `new_memory_id`, on an `add`, is
  the `c-` id MyMemo gave the memory; the folder keeps it when the add applies (proposal field `new_memory_id`). `hold: "review"` asks the computer to make it wait for the person even where its rules would apply it.
- `from: "user"`: the person's own edit on MyMemo's memory page, applied as `actor: user`. `edit_id` names it.
  - `op: "put"`: the memory as the person left it (`memory`: `content`, `topic`, `section`, `kind`, `until`,
    `private`, `pinned`, `status`). `base_version` is the version the edit was made on, `null` for a new memory
    (created with `memory_id`). If the folder's memory is already like that: `applied`. Else if its version isn't
    `base_version`: `conflict`, and the folder's version stays.
  - `op: "forget"`: the person deleted the memory for good in MyMemo; the folder archives it and never sends it
    again while it stays archived.
  - `op: "topic"`: a topic created or changed (`slug`, `title`, `description`, `group`, `private`).
  - `op: "settings"`: `require_approval`.
  - `op: "decide"`: `accept`, `reject` or `undo` of a proposal MyMemo sent earlier (`proposal_id`; `content` and
    `topic` for an edited accept).
- `from: "merge"`: a memory MyMemo had before this computer synced (§ First merge).

## Computer → MyMemo: `sync_push`, and `sync_ack`

`sync_push_merge.json` and `sync_push.json`. One push is in flight at a time and each is at most about 512 KB; MyMemo
answers `sync_ack` with the same `seq` (`sync_ack.json`) once it has applied it, listing any memory it would not
store in `skipped` (`memory_secret`: MyMemo's never-save list; `memory_limit_reached`; `invalid`:
`sync_ack_skipped.json`). A push whose `seq` MyMemo already applied is acknowledged again without effect. Fields,
all optional except `seq`:

- `cursor`: the last `sync_ops` seq filed. MyMemo deletes what it had waiting up to there.
- `settings`, `groups` (all of them, in sidebar order), `topics` (the changed ones): the folder's frontmatter. A
  private topic the person didn't pick is sent as its slug, group and `private: true` only (`title` and
  `description` null). A deleted topic is `{"slug", "deleted": true}`.
- `ops`, in order. Each is a version of one memory, as in `history.jsonl`, plus its state:
  - `create`, `update`, `move`, `archive`, `restore`: the memory at `version`, with `content`, `topic`, `section`,
    `kind`, `until`, `private`, `pinned`, `status` (`active` or `archived`), `actor` (who wrote this version), `source`, `at`, `created_at` and
    `proposal_id` (MyMemo's id when it came from MyMemo, otherwise the folder's, or null).
  - `"past": true`: an earlier version (only `content`, `topic`, `actor`, `at`, `proposal_id`), for MyMemo's history
    only; the memory's state comes from its last op in the push.
  - `set`: the memory's state at its current version without a new version (a review date, pinned, kind), and the
    folder's answer when MyMemo's copy has to follow it (after a conflict, or a proposal that didn't apply here).
  - `tombstone`: the memory left the sync (it went private, or into a private topic that isn't picked). MyMemo
    deletes it and the text of all its versions.
  - MyMemo applies an op unless its `version` is older than the last one the folder sent for that memory; the
    folder's state then replaces MyMemo's, including what MyMemo applied provisionally.
- `outcomes`: what became of each op from MyMemo, by `seq`:
  - `proposal_id` (from `app`, and a `decide`): the proposal's status here: `applied`, `pending` (with `hold`),
    `accepted`, `edited_accepted`, `rejected`, `expired`, `undone`, or, when nothing was stored, `duplicate` or
    `refused` (`code`, e.g. `revoked`, `read_only`, `topic_blocked`). `memory_id` is the memory it changed here.
    When the person decides a waiting proposal here later, the new status goes up with `seq: null`.
  - `merge_id` (from `merge`): as for a proposal; `memory_id` is the folder's memory (the same `c-` id when it
    applied, an existing memory for a duplicate).
  - `edit_id` only (from `user`): `applied`, `conflict`, `missing` (no such memory here) or `refused` (`code`).
  - MyMemo follows each outcome: it keeps what applied, and removes what it applied provisionally that the folder
    didn't take.
- `merge: "done"` and `known`, on the last push of a first merge: every memory id the folder syncs, so MyMemo drops
  its copies of memories the folder no longer has.

## First merge

With `merge: needed`, MyMemo sends every memory it has that didn't come from this folder as `from: "merge"` ops
(`sync_ops_merge.json`). The computer files nothing yet: the panel shows a preview (MyMemo's count, how many are
already here or similar, how many are new, and how many of the folder's memories will go up). When the person
starts syncing, the computer files each one as a proposal from `cloud-mymemo` with `idempotency_key`
`merge:<memory_id>` and `new_memory_id` = `memory_id`, then pushes every memory it syncs, the last push with
`merge: "done"` and `known` (`sync_push_merge.json`). Cancelling turns sync off; nothing was sent.

## Turning it off

`sync_off.json` (computer → MyMemo): sync was turned off here; with `delete_copy`, MyMemo deletes every memory that
came from the folder and their versions. MyMemo answers `sync_status` with `accepted: false` and `reason: "off"`
(`sync_status_off.json`). The same `sync_status` arrives when the person turned sync off on MyMemo's memory page.

Everything here is test data: the ids, names and memories are made up.
