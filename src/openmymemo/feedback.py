"""Send feedback (product-design.md §21): a GitHub issue form or an email, with this computer's OpenMyMemo version
and system filled in. Nothing is sent from here: both open where the person reads what they send before they send
it, and nothing from the memory folder is ever added."""

from __future__ import annotations

import platform
import sys
from urllib.parse import quote, urlencode

from . import __version__

ISSUE_FORM = "https://github.com/MyMemoAI/openmymemo/issues/new"
EMAIL = "leo@mymemo.ai"  # the contact in PRIVACY.md


def system() -> str:
    """The operating system as people name it, with the processor: "macOS 15.4 (arm64)", "Windows 11 (AMD64)"."""
    machine = platform.machine()
    if sys.platform == "darwin":
        name = f"macOS {platform.mac_ver()[0]}".rstrip()
    elif sys.platform == "win32":
        release = platform.release()
        build = platform.version().rsplit(".", 1)[-1]
        if release == "10" and build.isdigit() and int(build) >= 22000:
            release = "11"  # Windows 11 still says 10 here on older Pythons; its builds start at 22000
        name = f"Windows {release}".rstrip()
    else:
        try:
            name = platform.freedesktop_os_release().get("PRETTY_NAME") or ""
        except OSError:
            name = ""
        name = name or f"{platform.system()} {platform.release()}".strip()
    return f"{name} ({machine})" if machine else name


def app_name() -> str | None:
    """What the desktop app is called on this system, for "How you use OpenMyMemo" (there's none for Linux)."""
    return {"darwin": "Mac app", "win32": "Windows app"}.get(sys.platform)


def links(*, app: bool = False) -> dict:
    """Where to send feedback: the issue form (public) and an email (private), each with the version and system
    filled in, and ``app`` saying it came from the Mac or Windows app."""
    shell = app_name() if app else None
    about = f"OpenMyMemo {__version__} · {system()}" + (f" · {shell}" if shell else "")
    issue = f"{ISSUE_FORM}?" + urlencode({"template": "feedback.yml", "version": __version__, "os": system()},
                                         quote_via=quote)
    email = f"mailto:{EMAIL}?" + "&".join(
        f"{k}={quote(v, safe='')}" for k, v in (("subject", "OpenMyMemo feedback"), ("body", f"\n\n—\n{about}\n"))
    )
    return {"issue_url": issue, "email": EMAIL, "email_url": email, "about": about,
            "version": __version__, "system": system()}
