"""Notes about single apps. One is printed in front of the first state of its app."""

from __future__ import annotations

from pathlib import Path

from .model import AgentSession

BUILT_IN = Path(__file__).with_name("instructions")
TAG = "app_notes"
NEVER = frozenset({"com.apple.iwork.numbers"})  # the file ships and is never printed
# Every browser gets the one note in browser.md. Lower case; each entry also covers the
# ids that start with it (beta, nightly and test builds).
BROWSER_NOTE = "browser"
BROWSERS = (
    "ai.perplexity.comet",
    "app.floorp.floorp",
    "app.zen-browser.zen",
    "com.apple.safari",
    "com.brave.browser",
    "com.google.chrome",
    "com.microsoft.edgemac",
    "com.operasoftware.opera",
    "com.vivaldi.vivaldi",
    "company.thebrowser.browser",
    "company.thebrowser.dia",
    "io.gitlab.librewolf-community.librewolf",
    "net.imput.helium",
    "net.librewolf.librewolf",
    "net.waterfox.",
    "org.chromium.chromium",
    "org.mozilla.firefox",
    "org.mozilla.nightly",
)


def personal_dir() -> Path:
    """Where the person puts notes of their own: one `<bundle id>.md` per app."""
    from .protocol import state_dir

    return state_dir() / "instructions"


def note(bundle_id: str) -> str | None:
    """The note for one app. A personal file replaces the built-in one completely."""
    key = bundle_id.lower()
    names = [key, BROWSER_NOTE] if key.startswith(BROWSERS) else [key]
    for folder in (personal_dir(), BUILT_IN):
        files = {path.stem.lower(): path for path in folder.glob("*.md")}
        for name in names:
            if name in files:
                try:
                    return files[name].read_text(encoding="utf-8").strip() or None
                except (OSError, UnicodeDecodeError):
                    return None  # a note that cannot be read never fails a state read
    return None


def prefix(agent: AgentSession, bundle_id: str) -> str | None:
    """The block to print before a state, once per agent session and app."""
    key = bundle_id.lower()
    if key in NEVER or key in agent.instructed or (text := note(key)) is None:
        return None
    agent.instructed.add(key)
    return f"<{TAG}>\n{text}\n</{TAG}>\n"
