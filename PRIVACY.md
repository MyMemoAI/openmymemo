# Privacy Policy

Last updated: 2026-10-19

OpenMyMemo is open-source software published by MyMemo. It keeps your AI memory in a folder on your own computer. This policy covers the OpenMyMemo command-line tool, its MCP server, its review panel, the Mac app and the plugins and extensions in this repository. OpenMyMemo Cloud, an optional MyMemo service, is covered by the section of that name below and by MyMemo's own privacy policy.

## In short

- Your memory stays on your computer. MyMemo does not receive it, and OpenMyMemo has no account, telemetry, analytics, crash reporting or update check.
- AI apps you connect read your memory and send what they read to their own model provider, under that app's privacy policy. The `health` and `finance` topics are hidden from every app unless you allow them.
- Nothing leaves your computer through OpenMyMemo unless you sign in to OpenMyMemo Cloud and link this computer to a MyMemo account, or turn on the OpenMyMemo Assistant with a model that isn't on your computer.

## What OpenMyMemo stores on your computer

Everything below is stored only on your computer. OpenMyMemo never uploads it.

- **The memory folder** (`~/OpenMyMemo` by default, or wherever you choose) holds:
  - your memories in Markdown;
  - their full history, including earlier versions and private memories;
  - pending and decided changes proposed by AI apps, with the reason and quote the app gave;
  - the list of connected apps and their permissions. Connector tokens are stored only as a hash.
  - an activity log of what each app did: when it read your memory, which memories it read, the text of its searches, its proposals and their outcome, and imports.
- **Backups of other apps' settings:** before it first edits an AI app's MCP configuration file to connect it, OpenMyMemo keeps a copy next to that file (`<file>.openmymemo-backup`). The copy is readable by you only, whatever the original file allows (a copy made by a version before 2026-10-15 took the original's permissions; OpenMyMemo makes it private the next time it changes that file, and you can delete it yourself), but it contains whatever that file held, which may include other secrets.
- **Other apps' memory files, when you import them:** the Import page's Files tab lists them, for example `CLAUDE.md`, `AGENTS.md`, `GEMINI.md`, Cursor rules or `memory.md`, in the folder you choose (your home folder by default). It reads a file only when you open its preview. What you import is copied into your memory folder; the originals are left as they are. Nothing is uploaded.
- **The Mac app** keeps these under `~/Library`:
  - its settings: your memory folder's location, the appearance and notifications switches;
  - the Python it runs on and a download cache;
  - a log of its background server, which never records your memories.

  When an AI app changes something, the Mac app can show a macOS notification that includes the text of the change. You can turn notifications off in Settings.
- **OpenMyMemo Cloud:** once you change its settings, `settings.json` in OpenMyMemo's per-user application folder holds whether it is listed and which service it uses. If you link this computer, `cloud.json` next to it holds:
  - the service address, a device id and token, and the private key that opens queued changes;
  - which folder is linked;
  - your MyMemo display name, masked email and plan.

  Only you can read these files. The activity log also records which account the computer was linked to.

- **Search by meaning on this computer, if you turn it on:** `semantic.json` (on or off, the model you chose, and which models' terms you accepted), the downloaded model under `models/`, and, if OpenMyMemo had to install it, ONNX Runtime under `semantic-runtime/`, all next to `cloud.json`. In your memory folder, `.openmymemo/index/` holds a number vector for each memory: a cache that keeps itself out of git and is deleted when you turn the switch off.
- **The OpenMyMemo Assistant, if you turn it on:** `assistant.json`, next to `cloud.json`, readable by you only. It holds your choice of model and endpoint, its switches and limits, this month's usage counts, and which apps' sessions you allowed it to read. With session reading on, `session-state.json` next to it records, per session, how many messages were already read and when (no text), for 30 days. Your API key is kept in the macOS Keychain when possible, otherwise in that file; it is never put in your memory folder. The activity log records each request the Assistant makes: what it was for, the model, the endpoint's host name, token counts, how long it took, which memories it was sent and whether it worked. It never records the text sent or received.

## What AI apps receive

When you connect an AI app, it can read memories in the topics you allow and propose changes. By default, additions apply right away and show up for you to review and undo, and changes to memories you wrote yourself wait for you. If you let an app write directly (its write mode "auto"), its changes apply at once, including changes to memories you wrote and while approval is on. Anything in a private topic always waits for you, whatever an app's write mode.

What an app reads, it sends to its own model provider as part of your conversation. That provider's privacy policy then applies, not this one. You decide per app which topics it may read; private topics are excluded by default. You can see exactly what an app receives on the "What AI sees" page or with `openmymemo context --connector <id>`.

## Network connections

OpenMyMemo's server listens only on `127.0.0.1`, so other devices cannot reach it. OpenMyMemo connects to the internet only in these cases:

- **Installing and first start.** `uv`, the Claude Desktop extension and the Mac app's first start download Python and OpenMyMemo's open-source libraries:
  - Python from python-build-standalone releases on GitHub;
  - the libraries from PyPI (pypi.org and files.pythonhosted.org).

  These are ordinary download requests. Like any `uv` download, they carry `uv`'s usual client information, such as its version and your operating system. No memory is sent.
- **Links you click.** The panel and the Mac app open documentation and download pages in your browser. Install links open the AI app they belong to directly (for example `cursor://` or `vscode:`), never a website, because they carry the command that starts OpenMyMemo and with it the path of your memory folder.
- **OpenMyMemo Cloud**, only after you sign in and link this computer (see below).
- **The OpenMyMemo Assistant**, only after you turn it on with a model that isn't on your computer (see below). With MyMemo's hosted model, signing in and refreshing the sign-in also talk to the MyMemo service.
- **Search by meaning on this computer**, only when you turn it on or switch models: it downloads the model you chose from Hugging Face (huggingface.co and the storage it redirects to), at a fixed address, and checks each file against a SHA-256 checksum in OpenMyMemo's code, deleting a file that doesn't match. If ONNX Runtime isn't installed, it is downloaded from PyPI the same way OpenMyMemo's other libraries are. These are ordinary downloads: no memory and nothing about you is sent, beyond what any download carries (your IP address, and the client's name). Searching with it happens entirely on your computer.

## OpenMyMemo Cloud (optional)

OpenMyMemo Cloud lets ChatGPT and claude.ai on the web use your memory through your MyMemo account, on MyMemo Pro, Ultra or Lifetime. It is a relay, not hosting: your memory stays on your computer. It is listed on the Connectors page, which sends nothing anywhere, and you can hide it by turning off "Show OpenMyMemo Cloud" in Settings. It does nothing until you sign in and link this computer. You can choose another compatible service instead of MyMemo's own (`https://api.mymemo.ai`).

When this computer is linked:

- **Linking.** Your browser opens the service's sign-in page with this computer's name. OpenMyMemo then sends the service:
  - a one-time sign-in code;
  - a public key, whose private key never leaves your computer;
  - OpenMyMemo's version.

  The service returns a device token, your display name, your masked email and your plan.
- **While OpenMyMemo runs**, it keeps a connection to the service and sends the list of OpenMyMemo's tools.
  - When a web app calls a tool, the service relays the call, and OpenMyMemo answers it on your computer. Your memory therefore passes through the service in transit: the memories the web app reads, and the changes it proposes.
  - Web apps get their own connectors, with the same defaults as other apps: private topics excluded, every change a proposal.
- **While your computer is offline**, web apps can't read or change your memory, and nothing waits at the service: the offline queue isn't offered yet. If it is offered later, on plans that include it, changes a web app proposes while your computer is offline will wait at the service, encrypted to this computer's key so the service can't read them, and be deleted once your computer has received them.
- **Unlinking** deletes the link on your computer and asks the service to delete the device.

What the MyMemo service stores and how long it keeps it is governed by [MyMemo's privacy policy](https://mymemo.ai/privacy-policy/). If you choose another service, its operator's policy applies.

## OpenMyMemo Assistant (optional)

The OpenMyMemo Assistant uses a model to help with imports, sorting and search, to check what AI apps propose, to read chat exports you import, and, if you allow it per app, to read Claude Code and Codex sessions when they end. It is off until you turn it on in Settings → Assistant or with `openmymemo assistant set`. What it sends depends on the model you choose:

| Your choice | Where memory text goes |
| --- | --- |
| Off (the default) | Nowhere |
| On this computer (Ollama, LM Studio, llama.cpp, vLLM, …) | Only to the model server on your computer. OpenMyMemo checks that its address is local and never follows a redirect elsewhere. |
| Your own API key (Anthropic, or any OpenAI-compatible service) | To that provider, under its terms and privacy policy. |
| MyMemo's hosted model | To MyMemo's service, which passes it to Anthropic's Claude models to answer. See "MyMemo's hosted model" below. |

- **What is sent:** for each request, the text a feature works on, with the names of your topics (never private ones):
  - imports and sorting: what you paste or the file you import, or the imported memories being sorted;
  - search: the search itself (to add related words), and the memories found for it (to put the best first);
  - checking a change an AI app proposes: that change, and up to 8 of your memories that look most like it (chosen on your computer by word overlap, among those both the app and the Assistant may read);
  - chat exports and sessions: your messages and the start of each reply, in parts of about 12,000 characters, newest first. A conversation or session that contains any part of a private memory is left out, and anything that looks like an API key or token is masked. For a session, also what the app already saved during it, and at most about 48,000 characters of the newest part of the session.
- **Embeddings:** if you set an embeddings model at the endpoint, the text of each memory it may read is sent once to get its vector (and again when the memory changes), and each search to get the search's.
- **Transcripts and chat exports** are read on your computer when you import them or a session ends, and never stored or copied by OpenMyMemo. What is kept is only what you or the review rules keep: the suggested memories, each with your quoted words and the date. Session reading is off by default and asked per app; apart from it and embeddings, nothing is sent in the background.
- **Never sent to a model outside your computer:** private topics (`health` and `finance` by default) and memories marked private. OpenMyMemo refuses a request that contains any part of them, however it is worded or formatted, and that includes archived memories, earlier versions and proposed changes, whatever a feature asks. Memories in topics you have kept the Assistant from reading are treated the same way, and topics it can't read aren't even named to the model. A model on your computer gets only the private topics you pick in its settings.
- **Its access:** the Assistant appears on the Connectors page as "OpenMyMemo Assistant". You can limit which topics it reads, make it read-only or remove its access, as for any app. Anything it changes is a proposal you can review and undo, and it never writes into a private topic.
- **Your key** stays on your computer (see above) and is sent only to the endpoint you chose.

### MyMemo's hosted model

MyMemo's hosted model isn't offered yet: Settings → Assistant doesn't list it. Once MyMemo opens its plan, you will be able to choose it in Settings → Assistant, or with `openmymemo assistant signin`. It is a MyMemo service with its own monthly plan, the OpenMyMemo Assistant plan. Nothing is sent until you choose it and sign in.

- **Signing in.** If this computer is linked to OpenMyMemo Cloud, OpenMyMemo trades the link's device token for a sign-in that can only reach the model, with no browser. Otherwise your browser opens MyMemo's sign-in page with this computer's name. The service returns an access token and a refresh token. They are kept on your computer like an API key (in the Keychain on a Mac, otherwise in `assistant.json`), never in your memory folder, and sent only to that service.
- **For each request,** OpenMyMemo sends the service the text a feature works on (as above) and the feature's name, such as "import" or "sort". The service passes the text to Anthropic's Claude models to answer and returns the answer. Private topics are never sent, as for any model outside your computer.
- **What MyMemo keeps.** The service doesn't keep the text or the answer after answering. It keeps metadata only: your account, the feature, the model, token counts, the time and whether it worked, which it uses for your plan's usage and limits. It shows OpenMyMemo your plan and this period's usage, and OpenMyMemo shows them in Settings → Assistant.
- **Anthropic** processes the text to answer it, under its terms as MyMemo's provider.
- **Signing out** asks the service to revoke the sign-in and deletes both tokens from your computer. OpenMyMemo Cloud, if linked, stays linked.

How MyMemo handles your account and its metadata is governed by [MyMemo's privacy policy](https://mymemo.ai/privacy-policy/).

## Your choices

- Read, edit, export or delete your memories at any time. They are plain files in a folder you own.
- Choose for each app which topics it may read and whether it may write. Remove an app's access, or turn approval on so that every change from an app that proposes (the default) waits for you; an app you let write directly still applies its changes at once, except in private topics.
- Disconnect an app, which removes OpenMyMemo from its settings. Unlink OpenMyMemo Cloud, or hide it in Settings. Turn the OpenMyMemo Assistant off, or limit what it is used for.
- To remove everything:
  1. Delete the memory folder.
  2. Delete OpenMyMemo's application folders (`~/Library/Application Support/ai.mymemo.openmymemo`, `~/Library/Application Support/OpenMyMemo` and `~/Library/Caches/ai.mymemo.openmymemo` on a Mac).
  3. Delete the app or run `uv tool uninstall openmymemo`.

## Children

OpenMyMemo is not directed at children and does not knowingly collect anything from anyone. It has no account and collects nothing.

## Changes

Changes to this policy are published in this file, with a new date at the top. The repository's history shows every earlier version.

## Contact

Questions about this policy: open an issue at https://github.com/MyMemoAI/openmymemo/issues or write to leo@mymemo.ai. Report security problems privately as described in [SECURITY.md](./SECURITY.md).
