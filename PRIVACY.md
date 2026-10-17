# Privacy Policy

Last updated: 2026-10-15

OpenMyMemo is open-source software published by MyMemo. It keeps your AI memory in a folder on your own computer. This policy covers the OpenMyMemo command-line tool, its MCP server, its review panel, the Mac app and the plugins and extensions in this repository. MyMemo Cloud, an optional MyMemo service, is covered by the section of that name below and by MyMemo's own privacy policy.

## In short

- Your memory stays on your computer. MyMemo does not receive it, and OpenMyMemo has no account, telemetry, analytics, crash reporting or update check.
- AI apps you connect read your memory and send what they read to their own model provider, under that app's privacy policy. The `health` and `finance` topics are hidden from every app unless you allow them.
- Nothing leaves your computer through OpenMyMemo unless you turn on MyMemo Cloud and link this computer to a MyMemo account.

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
- **MyMemo Cloud, if you link this computer:** `cloud.json` and `settings.json` in OpenMyMemo's per-user application folder. Only you can read them. They hold:
  - the service address, a device id and token, and the private key that opens queued changes;
  - which folder is linked;
  - your MyMemo display name, masked email and plan.

  The activity log also records which account the computer was linked to.

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
- **MyMemo Cloud**, only after you turn it on and link this computer (see below).

## MyMemo Cloud (optional)

MyMemo Cloud lets ChatGPT and claude.ai on the web use your memory through your MyMemo account. It is hidden until you turn on "Show MyMemo Cloud" in Settings and does nothing until you sign in and link this computer. You can choose another compatible service instead of MyMemo's own (`https://api.mymemo.ai`).

When this computer is linked:

- **Linking.** Your browser opens the service's sign-in page with this computer's name. OpenMyMemo then sends the service:
  - a one-time sign-in code;
  - a public key, whose private key never leaves your computer;
  - OpenMyMemo's version.

  The service returns a device token, your display name, your masked email and your plan.
- **While OpenMyMemo runs**, it keeps a connection to the service and sends the list of OpenMyMemo's tools.
  - When a web app calls a tool, the service relays the call, and OpenMyMemo answers it on your computer. Your memory therefore passes through the service in transit: the memories the web app reads, and the changes it proposes.
  - Web apps get their own connectors, with the same defaults as other apps: private topics excluded, every change a proposal.
- **Offline queue.** On plans that include it, changes a web app proposes while your computer is offline wait at the service. They are encrypted to this computer's key, so the service can't read them, and they are deleted once your computer has received them.
- **Unlinking** deletes the link on your computer and asks the service to delete the device.

What the MyMemo service stores and how long it keeps it is governed by [MyMemo's privacy policy](https://mymemo.ai/privacy-policy/). If you choose another service, its operator's policy applies.

## Your choices

- Read, edit, export or delete your memories at any time. They are plain files in a folder you own.
- Choose for each app which topics it may read and whether it may write. Remove an app's access, or turn approval on so that every change from an app that proposes (the default) waits for you; an app you let write directly still applies its changes at once, except in private topics.
- Disconnect an app, which removes OpenMyMemo from its settings. Unlink MyMemo Cloud, or leave it hidden.
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
