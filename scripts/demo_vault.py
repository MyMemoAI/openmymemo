"""Create a demo vault: imported memories, two AI apps, a digest of their changes and a few that wait.

    uv run python scripts/demo_vault.py /tmp/omm-demo
    uv run openmymemo --vault /tmp/omm-demo serve

The vault keeps the default settings (AI changes apply right away and are listed for review), so it shows:

* in the digest: two additions and a self-correction from Claude Desktop, and an addition from Cursor,
  which the user lets write directly (``write: auto``);
* waiting for the user: Claude Desktop's update of a memory the user imported (protected) and an
  addition to the private Health topic.
"""

import sys
from collections import Counter

from openmymemo import transfer
from openmymemo.connectors import Connectors
from openmymemo.proposals import Proposals
from openmymemo.vault import Vault

CHATGPT_PASTE = """Saved memories
Prefers concise answers that lead with the conclusion, then the reasoning.
Delete
Is the CTO of MyMemo; the stack is FastAPI, Vue 3 and Swift.
Delete
Is learning Japanese and wants to watch Japanese dramas without subtitles next year.
Delete
Has a five-year-old daughter.
Delete
Uses a MacBook Pro with VS Code as the main editor.
Delete
Loves hard science fiction; favorite book is The Three-Body Problem.
Delete
Goes running every morning at 6.
"""


def main(path: str) -> None:
    v = Vault.create(path)
    transfer.commit(v, transfer.plan(v, transfer.parse("chatgpt", CHATGPT_PASTE)), "chatgpt")
    suggested = [(r["memory_id"], r["topic"]) for r in transfer.sort_plan(v) if r["topic"]]
    transfer.sort_commit(v, suggested[:-2])  # the user sorted most of the import; two are still in From ChatGPT
    v.add("how-to-work-with-me", "Don't write long auto-generated summaries in PR descriptions.")
    project = v.add("projects", "Building OpenMyMemo, a local, open-source AI memory folder.",
                    source="connector:claude-desktop", actor="connector:claude-desktop")
    v.add("health", "Allergic to penicillin.")
    v.add("values", "Believes personal memory is the user's own asset, not something a company should control.")

    cons = Connectors(v)
    claude = cons.ensure("claude-desktop", "Claude Desktop")
    cursor, _ = cons.create("Cursor", "http", write="auto")
    props = Proposals(v)
    cto = next(m for m in v.memories() if "CTO" in m.content)
    results = [
        # Applied at once, listed in the digest.
        props.propose(claude, "add", content="Is evaluating a move of the iOS app from UIKit to SwiftUI.",
                      topic="projects", reason="Came up while discussing the iOS stack",
                      evidence="We're thinking about moving the iOS app to SwiftUI next year"),
        props.propose(claude, "add", content="Wants code reviews to point out missing tests first.",
                      topic="how-to-work-with-me", reason="Stated a review preference",
                      evidence="When you review my code, tell me about missing tests before anything else"),
        props.propose(claude, "update", memory_id=project.id,  # Claude Desktop wrote this memory, so it may fix it
                      content="Building OpenMyMemo, a local, open-source AI memory folder with a review digest.",
                      reason="The project gained a digest", evidence="We just shipped the review digest"),
        props.propose(cursor, "add", content="Uses pnpm for all JavaScript projects.", topic="tools"),
        # Waits: the user imported this memory, so it is theirs.
        props.propose(claude, "update", memory_id=cto.id,
                      content="Is the CTO of MyMemo; the stack is FastAPI, Vue 3 and Swift. Also runs infrastructure.",
                      reason="Added a responsibility", evidence="I run the infrastructure too"),
        # Waits: Health is a private topic.
        props.propose(claude, "add", content="Allergic to peanuts.", topic="health",
                      reason="Mentioned while picking a restaurant", evidence="I'm allergic to peanuts, keep that in mind"),
    ]
    v.log_activity("claude-desktop", "context", memory_ids=[m.id for m in v.memories()][:9], chars=1200)
    v.log_activity("cursor", "search", query="pnpm", memory_ids=[])

    holds = Counter(r["hold"] for r in results if r["status"] == "pending")
    waiting = ", ".join(f"{n} {hold}" for hold, n in sorted(holds.items()))
    print(f"Demo vault at {v.root}: {len(v.memories())} memories, {len(props.unreviewed())} unreviewed changes, "
          f"{props.pending_count()} waiting for you ({waiting})")


if __name__ == "__main__":
    main(sys.argv[1])
