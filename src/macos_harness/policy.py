"""Which apps the agent may drive, how risky each one is, and which pages are off limits.

The order is fixed: the policy files first (a deny there wins), then the forbidden
list, then the risk level that decides how the approval prompt reads. By default no
app is asked about and only what protects the Mac itself is forbidden: the agent's
own host and the system's password sheets. `ask_before_first_use` brings the prompt
back, `strict` the terminals.
"""

from __future__ import annotations

import fnmatch
import os
import plistlib
import sys
import tomllib
from pathlib import Path
from urllib.parse import urlsplit

from . import apps, errors, protocol
from .model import AgentSession, AppTarget, Policy

# Forbidden with `strict`: a shell there runs any command a page or a document asks for.
TERMINALS = frozenset(
    {
        "com.apple.Terminal",
        "com.googlecode.iterm2",
        "org.alacritty",
        "dev.warp.Warp-Stable",
        "net.kovidgoyal.kitty",
        "co.zeit.hyper",
        "com.github.wez.wezterm",
        "org.tabby",
        "com.mitchellh.ghostty",
        "com.raphaelamorim.rio",
        "dev.commandline.waveterm",
    }
)
# Driven only after a prompt that carries the warning: passwords and a mirrored phone.
ELEVATED = frozenset(
    {
        "com.apple.ScreenContinuity",
        "com.apple.keychainaccess",
        "com.apple.Passwords",
        "com.1password.1password",
        "com.1password.safari",
        "com.bitwarden.desktop",
        "com.dashlane.dashlanephonefinal",
        "com.lastpass.LastPass",
        "com.nordsec.nordpass",
        "me.proton.pass.electron",
        "me.proton.pass.catalyst",
    }
)
# The system's own password and consent dialogs.
SECURITY = frozenset(
    {
        "com.apple.UserNotificationCenter",
        "com.apple.LocalAuthenticationRemoteService",
        "com.apple.SecurityAgent",
    }
)
WARNING = (
    f"An app that {errors.PRODUCT} drives can show it text written by someone else, and "
    "that text may try to steer it: data could be stolen or lost. "
    f"Watch {errors.PRODUCT} while it works in this app."
)

# Two files with one shape. The first belongs to whoever manages the Mac, the second
# (policy.toml in the state folder) to the person. A missing file is all defaults.
MANAGED_FILE = Path("/Library/Application Support/macos-harness/policy.toml")
DEFAULTS = {
    "enabled": True,
    "ask_before_first_use": False,  # true: the approval prompt before an app's first use
    "strict": False,  # true: terminal apps are forbidden too
    "allow_persistent_approval": True,
    "allow_forbidden_targets": False,  # the switch that turns the forbidden list off
    "default_app_access": "allow",  # "allow" or "deny"
    "bundle_ids": {},  # bundle id -> "allow" or "deny", wins over the default
    "blocked_urls": [],  # shell patterns, matched against the whole URL and the host
}
# How the two files combine: all = one false wins, any = one true wins.
SWITCHES = {
    "enabled": all,
    "ask_before_first_use": any,
    "strict": any,
    "allow_persistent_approval": all,
    "allow_forbidden_targets": any,
}
REFUSALS = {"denied": errors.denied_by_policy, "forbidden": errors.forbidden}

_parsed: dict[Path, tuple[int, dict]] = {}  # file -> (its change time, its content)


def _layer(path: Path) -> dict:
    """One policy file. It is parsed again only when the file changed."""
    try:
        changed = path.stat().st_mtime_ns
    except FileNotFoundError:
        return DEFAULTS
    if _parsed.get(path, (None,))[0] != changed:
        try:
            _parsed[path] = (changed, DEFAULTS | tomllib.loads(path.read_text("utf-8")))
        except (OSError, ValueError) as error:
            # A file we cannot read may hold a deny. Refuse until it is fixed.
            raise errors.policy_unreadable(str(path), error) from None
    return _parsed[path][1]


def load() -> dict:
    """The policy in force: the combined switches, every blocked URL, and both layers."""
    layers = [_layer(MANAGED_FILE), _layer(protocol.state_dir() / "policy.toml")]
    switches = {
        name: join(layer[name] for layer in layers) for name, join in SWITCHES.items()
    }
    urls = [pattern for layer in layers for pattern in layer["blocked_urls"]]
    return {**switches, "blocked_urls": urls, "layers": layers}


def _is_host(agent: AgentSession, target: AppTarget) -> bool:
    """An agent may not drive the app it runs in, nor this helper."""
    path = os.path.realpath(target.path)
    own = target.pid == os.getpid() or sys.executable.startswith(path + os.sep)
    return own or path in {os.path.realpath(host) for host in agent.hosts}


def evaluate(agent: AgentSession, target: AppTarget) -> Policy:
    """The three-way decision and the risk, with nothing raised for a refusal."""
    config = load()
    if not config["enabled"]:
        raise errors.disabled()
    bundle = target.bundle_id
    denied = any(
        layer["bundle_ids"].get(bundle, layer["default_app_access"]) != "allow"
        for layer in config["layers"]
    )
    guarded = SECURITY | (TERMINALS if config["strict"] else frozenset())
    forbidden = not config["allow_forbidden_targets"] and (
        bundle in guarded or _is_host(agent, target)
    )
    # Every app that opens http links is a browser, and a page can carry instructions.
    high = bundle in TERMINALS | ELEVATED or apps.kind(target).http
    return Policy(
        decision="denied" if denied else "forbidden" if forbidden else "allowed",
        risk="high" if high else "low",
        allow_persistent=config["allow_persistent_approval"],
        warning=WARNING if high else None,
        ask=config["ask_before_first_use"],
    )


def decide(agent: AgentSession, target: AppTarget) -> Policy:
    """The policy for an app the agent may drive. Raises the refusal text for the rest."""
    policy = evaluate(agent, target)
    if policy.decision != "allowed":
        raise REFUSALS[policy.decision](target.bundle_id)
    if target.bundle_id in SECURITY:  # also when the forbidden list is switched off
        raise errors.security_process(target.bundle_id)
    return policy


def check_recipient(bundle_id: str) -> None:
    """Before an event is posted to a process that is not the app itself (it draws
    part of the app's window: a file panel, a system password sheet): never to one on
    the forbidden lists, never to one a policy file names without allowing it."""
    if bundle_id in SECURITY or not bundle_id:  # one we cannot name gets nothing
        raise errors.security_process(bundle_id or "unknown process")
    config = load()
    if (
        bundle_id in TERMINALS
        and config["strict"]
        and not config["allow_forbidden_targets"]
    ):
        raise errors.forbidden(bundle_id)
    if any(
        layer["bundle_ids"].get(bundle_id, "allow") != "allow"
        for layer in config["layers"]
    ):
        raise errors.denied_by_policy(bundle_id)


def display_name(target: AppTarget) -> str:
    """The name the approval prompt shows for an app."""
    try:
        with open(os.path.join(target.path, "Contents", "Info.plist"), "rb") as file:
            info = plistlib.load(file)
    except (OSError, ValueError):
        info = {}
    return (
        info.get("CFBundleDisplayName")
        or info.get("CFBundleName")
        or Path(target.path).stem
    )


def watches_urls() -> bool:
    """True when some page is on the blocked list at all."""
    return bool(load()["blocked_urls"])


def check_url(url: str | None) -> None:
    """Refuse a browser page on the blocked list, whoever opened it."""
    if not url:
        return
    candidates = (url.lower(), (urlsplit(url).hostname or "").lower())
    for pattern in load()["blocked_urls"]:
        if any(fnmatch.fnmatchcase(text, pattern.lower()) for text in candidates):
            raise errors.blocked_url()
