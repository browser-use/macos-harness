"""App policy: the two policy files, the forbidden rule, risk, names, blocked pages."""

from __future__ import annotations

import os
import plistlib
import sys

import fakes_g
import pytest
from fakes_g import agent, app

from macos_harness import errors, policy
from macos_harness.model import AppKind, AppTarget, Policy

BROWSER = app("Surf", "org.example.surf")


@pytest.fixture(autouse=True)
def files(tmp_path, monkeypatch):
    """Both policy files live in a temp folder. `write(layer, text)` fills one."""
    state = tmp_path / "state"
    monkeypatch.setattr(policy, "protocol", fakes_g.FakeProtocol(state))
    monkeypatch.setattr(policy, "MANAGED_FILE", tmp_path / "managed" / "policy.toml")
    monkeypatch.setattr(
        policy, "apps", fakes_g.FakeApps(**{BROWSER.bundle_id: AppKind(http=True)})
    )
    monkeypatch.setattr(policy, "_parsed", {})
    paths = {"managed": policy.MANAGED_FILE, "person": state / "policy.toml"}

    def write(layer: str, text: str) -> None:
        paths[layer].parent.mkdir(parents=True, exist_ok=True)
        paths[layer].write_text(text, "utf-8")
        stamp = paths[layer].stat().st_mtime_ns + 1_000_000  # a change is always seen
        os.utime(paths[layer], ns=(stamp, stamp))

    return write


def refusal(target, who=None) -> str:
    with pytest.raises(errors.HarnessError) as raised:
        policy.decide(who or agent(), target)
    return raised.value.message


def test_app_policy_lookup_returns_three_way_decision_risk(files):
    files("person", '[bundle_ids]\n"com.example.mail" = "deny"\n')
    decisions = {
        target.name: policy.evaluate(agent(), target).decision
        for target in (app("Notes"), app("Mail"), app("Terminal", "com.apple.Terminal"))
    }
    assert decisions == {"Notes": "allowed", "Mail": "denied", "Terminal": "forbidden"}
    assert policy.decide(agent(), app("Notes")) == Policy(
        decision="allowed", risk="low", allow_persistent=True, warning=None
    )


def test_a_deny_in_the_policy_file_wins_over_the_forbidden_list(files):
    files("person", 'default_app_access = "deny"\n')
    assert (
        policy.evaluate(agent(), app("Terminal", "com.apple.Terminal")).decision
        == "denied"
    )


def test_refusal_texts_denied_forbidden_apps(files):
    files("person", '[bundle_ids]\n"com.example.mail" = "deny"\n')
    assert refusal(app("Mail")) == (
        "Your policy file says macOS Harness may not use the app 'com.example.mail'."
    )
    assert refusal(app("iTerm", "com.googlecode.iterm2")) == (
        "For safety, macOS Harness may not use the app 'com.googlecode.iterm2'."
    )


@pytest.mark.parametrize("layer", ["managed", "person"])
def test_app_access_has_host_config_default_list_mac(files, layer):
    """Each file is a default plus per app overrides. A deny in either file wins."""
    files(
        layer,
        'default_app_access = "deny"\n[bundle_ids]\n"com.example.notes" = "allow"\n',
    )
    assert policy.decide(agent(), app("Notes")).decision == "allowed"
    assert "may not use the app 'com.example.mail'" in refusal(app("Mail"))


def test_an_allow_in_one_file_does_not_lift_a_deny_in_the_other(files):
    files("managed", '[bundle_ids]\n"com.example.mail" = "deny"\n')
    files("person", '[bundle_ids]\n"com.example.mail" = "allow"\n')
    assert "Your policy file says" in refusal(app("Mail"))


def test_an_unknown_access_word_denies(files):
    files("person", '[bundle_ids]\n"com.example.mail" = "alow"\n')
    assert "Your policy file says" in refusal(app("Mail"))


@pytest.mark.parametrize("layer", ["managed", "person"])
def test_the_master_switch_disables_everything(files, layer):
    files(layer, "enabled = false\n")
    assert refusal(app("Notes")) == "Your configuration has macOS Harness switched off."


def test_a_policy_file_that_cannot_be_parsed_refuses_every_app(files):
    files("person", "enabled = \n")
    text = refusal(app("Notes"))
    assert text.startswith("macOS Harness cannot read its policy file '")
    assert text.endswith(
        "Ask the user to fix or remove the file, then call this tool again."
    )
    files("person", "enabled = true\n")
    assert policy.decide(agent(), app("Notes")).decision == "allowed"


def test_organization_policy_fetched_host_cached(files, monkeypatch):
    """A file is parsed once and again only after it changed."""
    files("person", '[bundle_ids]\n"com.example.mail" = "deny"\n')
    parses = []
    real = policy.tomllib.loads
    monkeypatch.setattr(
        policy.tomllib, "loads", lambda text: parses.append(1) or real(text)
    )
    for _ in range(3):
        assert "Your policy file says" in refusal(app("Mail"))
    assert len(parses) == 1
    files("person", "")
    assert policy.decide(agent(), app("Mail")).decision == "allowed"
    assert len(parses) == 2


def test_listing_apps_skips_policy_wrapper(files):
    """Nothing is checked or remembered outside decide: a caller that never calls it,
    like the app list, is not held up by a deny or a broken file."""
    files("person", "enabled = \n")
    assert policy._parsed == {}
    assert policy.display_name(app("Nowhere")) == "Nowhere"


def test_forbidden_11_terminal_apps():
    assert len(policy.TERMINALS) == 11
    for bundle_id in policy.TERMINALS:
        assert policy.evaluate(agent(), app("T", bundle_id)).decision == "forbidden"
        assert policy.evaluate(agent(), app("T", bundle_id)).risk == "high"


def test_a_process_that_gets_events_for_an_app_passes_the_lists_too(files):
    """Checked before an event is posted to a process that is not the app itself."""
    policy.check_recipient("com.apple.appkit.xpc.openAndSavePanelService")
    with pytest.raises(errors.HarnessError, match="security process: unknown process"):
        policy.check_recipient("")  # a process we cannot name gets nothing
    with pytest.raises(errors.HarnessError, match="system security process"):
        policy.check_recipient("com.apple.SecurityAgent")
    with pytest.raises(errors.HarnessError, match="For safety,"):
        policy.check_recipient("com.apple.Terminal")
    files("person", '[bundle_ids]\n"com.example.vault" = "deny"\n')
    with pytest.raises(errors.HarnessError, match="Your policy file says"):
        policy.check_recipient("com.example.vault")


def test_forbidden_an_agent_cannot_drive_its_own_host_app(tmp_path):
    """An agent may not drive the app it runs in: every bundle on its parent chain."""
    host = app("Agent Desk", "com.example.agentdesk")
    caller = agent(hosts=frozenset({host.path}))
    assert "may not use the app 'com.example.agentdesk'" in refusal(host, caller)
    assert policy.decide(agent("other"), host).decision == "allowed"
    # The same bundle reached through a link is still the host.
    real = tmp_path / "Real.app"
    real.mkdir()
    (tmp_path / "Link.app").symlink_to(real)
    linked = AppTarget(str(tmp_path / "Link.app"), "com.example.link", "Link", 9)
    assert (
        policy.evaluate(agent(hosts=frozenset({str(real)})), linked).decision
        == "forbidden"
    )


def test_forbidden_the_helper_itself():
    """No list of product ids. The helper itself is forbidden: by pid and by bundle."""
    own_pid = app("Helper", "org.example.helper", pid=os.getpid())
    assert policy.evaluate(agent(), own_pid).decision == "forbidden"
    bundle = sys.executable.split("/bin/")[0]
    inside = AppTarget(bundle, "org.example.runtime", "Runtime", 7)
    assert policy.evaluate(agent(), inside).decision == "forbidden"
    assert len(policy.TERMINALS | policy.ELEVATED | policy.SECURITY) == 25


def test_forbidden_3_system_security_processes_their_own_action(files):
    assert len(policy.SECURITY) == 3
    target = app("SecurityAgent", "com.apple.SecurityAgent")
    assert "For safety," in refusal(target)
    files("person", "allow_forbidden_targets = true\n")
    assert refusal(target) == (
        "macOS Harness error 100: This tool may not act on a system "
        "security process: com.apple.SecurityAgent"
    )


def test_user_defaults_key_turns_forbidden_list_off(files):
    terminal = app("Terminal", "com.apple.Terminal")
    host = app("Agent Desk")
    caller = agent(hosts=frozenset({host.path}))
    assert policy.evaluate(caller, terminal).decision == "forbidden"
    files("person", "allow_forbidden_targets = true\n")
    assert policy.decide(caller, terminal).risk == "high"
    assert policy.decide(caller, host).decision == "allowed"


def test_elevated_risk_allowed_warning_blocked_password_managers_iphone(files):
    """Three ways to be high risk: a terminal, a password or mirroring app, a browser."""
    files("person", "allow_forbidden_targets = true\n")
    assert len(policy.ELEVATED) == 11
    high = [app("T", "org.alacritty"), app("K", "com.apple.keychainaccess"), BROWSER]
    assert [policy.decide(agent(), target).risk for target in high] == ["high"] * 3
    assert policy.decide(agent(), app("Notes")).risk == "low"
    for bundle_id in policy.ELEVATED:
        assert policy.evaluate(agent(), app("E", bundle_id)).decision == "allowed"


def test_warning_subtitle_elevated_risk_apps():
    assert policy.decide(agent(), app("Passwords", "com.apple.Passwords")).warning == (
        "An app that macOS Harness drives can show it text written by someone else, "
        "and that text may try to steer it: data could be stolen or lost. Watch macOS "
        "Harness while it works in this app."
    )
    assert policy.decide(agent(), app("Notes")).warning is None


@pytest.mark.parametrize("layer", ["managed", "person"])
def test_persistent_approval_can_be_switched_off(files, layer):
    files(layer, "allow_persistent_approval = false\n")
    assert policy.decide(agent(), app("Notes")).allow_persistent is False


@pytest.mark.parametrize(
    "info, expected",
    [
        ({"CFBundleDisplayName": "Shown Name", "CFBundleName": "Short"}, "Shown Name"),
        ({"CFBundleName": "Short"}, "Short"),
        ({}, "File Name"),
        (None, "File Name"),  # no Info.plist at all
    ],
)
def test_display_name_fallbacks(tmp_path, info, expected):
    bundle = tmp_path / "File Name.app"
    (bundle / "Contents").mkdir(parents=True)
    if info is not None:
        (bundle / "Contents" / "Info.plist").write_bytes(plistlib.dumps(info))
    assert policy.display_name(AppTarget(str(bundle), "org.example.x", "x")) == expected


def test_disallowed_browser_url_ends_session_even_if_person(files):
    files(
        "person", 'blocked_urls = ["*.bank.example", "https://intranet.example/hr/*"]\n'
    )
    blocked = [
        "https://www.bank.example/login",
        "HTTPS://WWW.BANK.EXAMPLE/",
        "https://intranet.example/hr/salaries",
    ]
    for url in blocked:
        with pytest.raises(errors.HarnessError) as raised:
            policy.check_url(url)
        assert raised.value.name == "page_off_limits" and raised.value.cancelled
        assert raised.value.message == (
            "macOS Harness may not be used on the page that is open in the browser, "
            "so this session is over. Stop here and say why in your final message. "
            "For macOS Harness the page stays off limits even when the user opens it "
            "themselves."
        )
    for url in ("https://bank.example.org/", "https://intranet.example/news", "", None):
        policy.check_url(url)


def test_url_blocklist_timed_cache_filled_backend_account_id(files):
    """The blocked list comes from the two files on disk and from nowhere else."""
    files("managed", 'blocked_urls = ["managed.example"]\n')
    files("person", 'blocked_urls = ["person.example"]\n')
    assert policy.load()["blocked_urls"] == ["managed.example", "person.example"]
    for host in ("managed.example", "person.example"):
        with pytest.raises(errors.HarnessError):
            policy.check_url(f"https://{host}/")
    files("person", "")
    policy.check_url("https://person.example/")
