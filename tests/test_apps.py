"""apps.py against a fake system and small app bundles written into a temp folder."""

from __future__ import annotations

import os
import plistlib
import sys
import threading
import types

import pytest
from fakes import Clock
from fakes_f import FakeSystem, ScriptedClock, background_app, make_app, proc

from macos_harness import apps, errors
from macos_harness.model import AppKind, AppTarget

NOW = 1_800_000_000.0
DAY = 86400.0


@pytest.fixture
def system(monkeypatch, tmp_path):
    fake = FakeSystem()
    fake.folder = tmp_path / "Applications"
    fake.folder.mkdir()
    monkeypatch.setattr(apps, "SYSTEM", fake)
    monkeypatch.setattr(apps, "APP_FOLDERS", (str(fake.folder),))
    monkeypatch.setattr(apps, "_CATALOG", {"rows": None, "thread": None})
    monkeypatch.setattr(apps, "time", Clock(NOW))
    apps._kind.cache_clear()
    return fake


def install(system, name, bundle_id, folder=None, **info):
    path = make_app(folder or system.folder, name, bundle_id, **info)
    system.by_bundle_id.setdefault(bundle_id, []).append(path)
    return path


def used(path, days_ago=0.0, count=1, name=None, bundle_id=None):
    last = NOW - days_ago * DAY
    name = name or os.path.basename(path).removesuffix(".app")
    return apps.Used(path, last, count, name, bundle_id)


# --- resolve --------------------------------------------------------------------------


def test_app_named_display_name_full_app_path_unambiguous(system):
    """A display name, a full path and a bundle id name the same app."""
    path = install(system, "TextEdit", "com.apple.TextEdit")
    want = AppTarget(path, "com.apple.TextEdit", "TextEdit", None)
    for identifier in ("TextEdit", path, "com.apple.TextEdit", path + "/"):
        assert apps.resolve(identifier) == want


def test_the_display_name_of_a_bundle_comes_from_its_info(system):
    path = install(system, "Code", "dev.editor", CFBundleDisplayName="Visual Editor")
    assert apps.resolve(path).name == "Visual Editor"
    other = install(system, "Short", "dev.short", CFBundleName="Shorter")
    assert apps.resolve(other).name == "Shorter"


def test_ambiguous_bundle_id_error_names_candidates_fix(system, tmp_path):
    """Two installed copies and none running: the error names both paths."""
    one = install(system, "Browser", "dev.browser")
    two = install(system, "Browser", "dev.browser", tmp_path / "Beta")
    with pytest.raises(errors.HarnessError) as raised:
        apps.resolve("dev.browser")
    assert raised.value.code == 133
    assert raised.value.message == (
        f"More than one app has the bundle id 'dev.browser': "
        f"{one}, {two}. Name the app, or give its full path."
    )
    assert apps.resolve(two).path == two  # the path still works


def test_resolution_prefers_running_instance(system, tmp_path):
    """Of two installed copies the running one wins, by id, name and path."""
    install(system, "Browser", "dev.browser")
    beta = install(system, "Browser", "dev.browser", tmp_path / "Beta")
    system.procs = [proc(41, "Browser", beta, "dev.browser")]
    want = AppTarget(beta, "dev.browser", "Browser", 41)
    assert apps.resolve("dev.browser") == want
    assert apps.resolve("Browser") == want  # the running app before the installed one
    assert apps.resolve(beta) == want


def test_two_running_copies_of_one_bundle_id_are_ambiguous(system, tmp_path):
    one = install(system, "Browser", "dev.browser")
    two = install(system, "Browser", "dev.browser", tmp_path / "Beta")
    system.procs = [proc(41, "Browser", one, "dev.browser")]
    system.procs += [proc(42, "Browser", two, "dev.browser")]
    with pytest.raises(
        errors.HarnessError, match="More than one app has the bundle id"
    ):
        apps.resolve("dev.browser")
    system.procs[1] = proc(42, "Browser", one, "dev.browser")  # two of the same copy
    assert apps.resolve("dev.browser").pid == 41


def test_the_file_panel_service_stands_for_an_app_the_agent_reads(monkeypatch):
    """Named as itself, the panel service needed an approval of its own:
    the person was asked and the agent waited 110 s. It is the panel of an
    app the agent reads: that app is the target. No other app is ever looked at."""
    notes = AppTarget("/A/Notes.app", "dev.notes", "Notes", 40)
    numbers = AppTarget("/A/Numbers.app", "com.apple.iWork.Numbers", "Numbers", 50)
    shown = {50: "save-panel", 77: "save-panel"}  # 77: an app the agent never read
    asked = []
    monkeypatch.setattr(
        apps, "_panel_owner_window", lambda pid: asked.append(pid) or shown.get(pid)
    )
    assert apps.panel_owner([notes, numbers]) == numbers  # the newest first
    assert asked == [50]
    shown[50] = None
    with pytest.raises(errors.HarnessError, match="Invalid app"):
        apps.panel_owner([notes, numbers])
    assert 77 not in asked  # unrelated apps are never read
    asked.clear()
    with pytest.raises(errors.HarnessError, match="Invalid app"):
        apps.panel_owner([AppTarget("/A/Off.app", "dev.off", "Off", None)])
    assert asked == []  # not running: nothing to read


def test_a_catalyst_app_is_told_by_its_own_app_element(system, monkeypatch):
    """_AXExhibitsMacCatalystBehavior is read from the app element."""
    from macos_harness import ax

    path = install(system, "Pass", "me.proton.pass.catalyst")
    asked = []
    monkeypatch.setattr(ax, "app", lambda pid: pid)
    monkeypatch.setattr(
        ax, "get", lambda el, name: asked.append((el, name)) or el == 70
    )
    apps._catalyst.cache_clear()
    assert apps.kind(AppTarget(path, "me.proton.pass.catalyst", "Pass", 70)).catalyst
    assert apps.kind(AppTarget(path, "me.proton.pass.catalyst", "Pass", 70)).catalyst
    assert not apps.kind(
        AppTarget(path, "me.proton.pass.catalyst", "Pass", 71)
    ).catalyst
    assert not apps.kind(
        AppTarget(path, "me.proton.pass.catalyst", "Pass", None)
    ).catalyst
    assert asked == [(70, apps.CATALYST), (71, apps.CATALYST)]  # once per process
    apps._catalyst.cache_clear()


def test_after_approval_app_argument_replaced_full_app_path(system):
    """Whatever was typed, the target is the full path, and errors name it."""
    path = install(system, "Safari", "com.apple.Safari")
    target = apps.resolve("Safari")
    assert target.path == path
    with pytest.raises(errors.HarnessError) as raised:
        apps.running(target)
    assert raised.value.message == f"No running app matches {path}"


def test_path_bare_executable_app_bundle_rejected(system, tmp_path):
    """A path that is not an app bundle is an invalid app."""
    tool = tmp_path / "tool"
    tool.write_text("#!/bin/sh\n")
    folder = tmp_path / "Folder.app"
    folder.mkdir()  # the right ending, and no app inside
    for identifier in (str(tool), str(folder), str(tmp_path), "/no/such/Thing.app"):
        with pytest.raises(errors.HarnessError) as raised:
            apps.resolve(identifier)
        assert raised.value.message == f"Invalid app: {identifier}"
        assert (raised.value.code, raised.value.name) == (130, "no_such_app")


@pytest.mark.parametrize("identifier", ["", "   ", "NoSuchApp-7f3a", "no.such.id"])
def test_nothing_found_is_an_invalid_app(system, identifier):
    install(system, "TextEdit", "com.apple.TextEdit")
    with pytest.raises(errors.HarnessError) as raised:
        apps.resolve(identifier)
    assert raised.value.message == f"Invalid app: {identifier}"


def test_process_name_accepted_app_identifier(system):
    """The process name works, after display names."""
    browser = install(system, "Night Browser", "dev.night")
    system.procs = [
        proc(51, "Night Browser", browser, "dev.night", executable="nightly")
    ]
    assert apps.resolve("nightly").pid == 51
    install(system, "nightly", "dev.other")  # an installed app of that very name
    assert apps.resolve("nightly").bundle_id == "dev.other"


def test_a_name_known_only_to_the_system_lookup_resolves(system, tmp_path):
    path = make_app(tmp_path / "Elsewhere", "Tool", "dev.tool")
    system.by_name["Tool"] = path
    assert apps.resolve("Tool") == AppTarget(path, "dev.tool", "Tool", None)


def test_ignoring_case_and_the_app_ending_only_after_the_exact_steps(system):
    """For small models: tried only when the exact steps found nothing."""
    path = install(system, "TextEdit", "com.apple.TextEdit")
    for identifier in ("textedit", "TEXTEDIT", "TextEdit.app", "textedit.APP"):
        assert apps.resolve(identifier).path == path
    system.procs = [proc(5, "textedit", "/X/lower.app", "dev.lowercase")]
    assert apps.resolve("textedit").pid == 5  # an exact name is never overruled
    assert apps.resolve("TextEdit").path == path
    system.procs = [proc(6, "TextEdit.app", "/X/dotted.app", "dev.dotted")]
    assert apps.resolve("TextEdit.app").pid == 6  # also a name that ends in .app


def test_ignoring_case_also_finds_a_running_app_and_a_process(system):
    system.procs = [
        proc(61, "Zed", "/Applications/Zed.app", "dev.zed", executable="zed-bin")
    ]
    assert apps.resolve("zed").pid == apps.resolve("ZED-BIN").pid == 61


def test_path_normalisation_placeholder_id(system):
    """The data volume prefix is not part of an app's path."""
    assert (
        apps._clean("/System/Volumes/Data/Applications/A.app") == "/Applications/A.app"
    )
    assert (
        apps._clean("/System/Volumes/Database/A.app")
        == "/System/Volumes/Database/A.app"
    )
    assert apps._clean("/Applications/A.app/") == "/Applications/A.app"
    path = install(system, "TextEdit", "com.apple.TextEdit")
    system.procs = [proc(71, "TextEdit", path, "com.apple.TextEdit")]
    assert apps.resolve(apps.DATA_VOLUME + path) == apps.resolve(path)
    assert apps.resolve(path).pid == 71


def test_pid_ids(system):
    """A process with no bundle is named by its pid, in the list and back."""
    system.procs = [
        proc(321, "Raw Tool", None),
        proc(322, "Bundled", "/Apps/B.app", "dev.b"),
    ]
    target = apps.resolve("#pid:321")
    # Its id is the same text, so an approval or a policy line names this one program.
    assert target == AppTarget("#pid:321", "#pid:321", "Raw Tool", 321)
    assert apps.running(target) == target
    assert apps.kind(target) == AppKind()
    # A bundled process keeps its path, so the rules about paths still see it.
    assert apps.resolve("#pid:322") == AppTarget("/Apps/B.app", "dev.b", "Bundled", 322)
    for identifier in ("#pid:999", "#pid:", "#pid:abc"):
        with pytest.raises(errors.HarnessError, match="Invalid app"):
            apps.resolve(identifier)
    system.procs = []
    with pytest.raises(errors.HarnessError, match="No running app matches #pid:321"):
        apps.running(target)


# --- running --------------------------------------------------------------------------


def test_a_running_app_s_target_carries_its_pid(system):
    """The target of a running app carries its pid, whichever way it is named."""
    path = install(system, "Notes", "dev.notes")
    system.procs = [proc(81, "Notes", path, "dev.notes")]
    assert {apps.resolve(i).pid for i in ("Notes", path, "dev.notes", "#pid:81")} == {
        81
    }


def test_an_app_that_quit_has_its_own_error(system):
    """An app that quit is found out at the next call, with its own text."""
    path = install(system, "Notes", "dev.notes")
    system.procs = [proc(81, "Notes", path, "dev.notes")]
    target = apps.resolve("Notes")
    system.procs = []
    with pytest.raises(errors.HarnessError) as raised:
        apps.running(target)
    assert (raised.value.code, raised.value.name) == (
        131,
        "app_not_running",
    )
    assert raised.value.message == f"No running app matches {path}"


def test_the_pid_is_refreshed_per_call(system):
    """The pid is refreshed per call: a restart is seen, the driven copy kept."""
    path = install(system, "Notes", "dev.notes")
    system.procs = [proc(81, "Notes", path, "dev.notes")]
    target = apps.resolve("Notes")
    system.procs = [proc(95, "Notes", path, "dev.notes")]  # quit and started again
    assert apps.running(target).pid == 95
    system.procs = [
        proc(90, "Notes", path, "dev.notes"),
        proc(81, "Notes", path, "dev.notes"),
    ]
    assert apps.running(target).pid == 81  # two copies: the one already driven
    assert apps.running(target).path == path


def test_a_running_app_matches_through_a_link_to_its_bundle(system, tmp_path):
    real = install(system, "Safari", "com.apple.Safari", tmp_path / "Cryptex")
    os.symlink(real, system.folder / "Safari.app")
    system.procs = [proc(88, "Safari", os.path.realpath(real), "com.apple.Safari")]
    target = apps.resolve(str(system.folder / "Safari.app"))
    assert (target.pid, target.path) == (88, os.path.realpath(real))


# --- launch ---------------------------------------------------------------------------


class Launch:
    """A fake clock and a window that shows up at a planned moment."""

    def __init__(self, monkeypatch, system, shows_after=None):
        self.clock, self.asked, self.shows_after = ScriptedClock(NOW), [], shows_after
        monkeypatch.setattr(apps, "time", self.clock)
        monkeypatch.setattr(apps, "windows", types.SimpleNamespace(key_window=self.key))
        self.path = install(system, "Dictionary", "com.apple.Dictionary")
        system.starts = proc(700, "Dictionary", self.path, "com.apple.Dictionary")
        self.target = apps.resolve("Dictionary")

    def key(self, target):
        self.asked.append(target)
        if self.shows_after is None or self.clock.elapsed < self.shows_after:
            raise errors.no_windows()
        return "window"


def test_state_read_launches_app_background_if_running(system, monkeypatch):
    """An app that is not running is opened, and comes back with its pid."""
    launch = Launch(monkeypatch, system, shows_after=0)
    assert launch.target.pid is None
    target = apps.launch(launch.target)
    assert system.opened == [launch.path]
    assert target == AppTarget(launch.path, "com.apple.Dictionary", "Dictionary", 700)
    assert launch.asked == [target] and launch.clock.sleeps == []


def test_launch_if_needed_name_url(system):
    """Opened without activating; an instance that already runs may answer."""
    assert apps.OPEN_OPTIONS == {
        "activates": False,
        "allowsRunningApplicationSubstitution": True,
    }
    appkit = pytest.importorskip("AppKit")
    config = appkit.NSWorkspaceOpenConfiguration.configuration()
    assert config.activates() is True  # the system's own default
    for option, value in apps.OPEN_OPTIONS.items():
        config.setValue_forKey_(value, option)
    assert config.activates() is False
    assert config.allowsRunningApplicationSubstitution() is True
    assert config.hides() is False  # in the background, not hidden


def test_launch_waits_wait_launch_finish_primary_window_polling(system, monkeypatch):
    """After the open, look for the first window every 0.05 s."""
    launch = Launch(monkeypatch, system, shows_after=0.98)
    target = apps.launch(launch.target)
    assert target.pid == 700
    assert set(launch.clock.sleeps) == {apps.LAUNCH_POLL} == {0.05}
    assert launch.clock.elapsed == pytest.approx(1.0)
    assert "settle" not in vars(apps)  # the caller settles, with the tree reader


def test_launch_readiness_wait_has_budget_about_5_s(system, monkeypatch):
    """No window in 5.0 s is the bare name The app opened no window in time."""
    launch = Launch(monkeypatch, system)
    with pytest.raises(errors.HarnessError) as raised:
        apps.launch(launch.target)
    assert (
        raised.value.message
        == "macOS Harness error 100: The app opened no window in time"
    )
    assert apps.LAUNCH_LIMIT == 5.0
    assert 5.0 <= launch.clock.elapsed <= 5.05 + 1e-6


def test_a_window_error_other_than_no_window_also_keeps_waiting(system, monkeypatch):
    launch = Launch(monkeypatch, system)

    def not_ready(target):
        if launch.clock.elapsed < 0.3:
            raise errors.accessibility("AXError.cannotComplete")
        return "window"

    monkeypatch.setattr(apps.windows, "key_window", not_ready)
    assert apps.launch(launch.target).pid == 700


def test_an_app_that_does_not_open(system, monkeypatch):
    launch = Launch(monkeypatch, system)
    system.starts = None
    with pytest.raises(errors.HarnessError, match="100: The app could not be started"):
        apps.launch(launch.target)
    assert launch.asked == []


# --- the kind of app ------------------------------------------------------------------


def schemes(*names):
    return [{"CFBundleURLName": "Web", "CFBundleURLSchemes": list(names)}]


def test_app_classification_feeds_pipeline_electron_catalyst_web_shaped(system):
    """The principal class and the http scheme decide. Catalyst is not known."""
    cases = {
        "Chat": ({"NSPrincipalClass": "AtomApplication"}, AppKind(electron=True)),
        "Shell": ({"NSPrincipalClass": "BrowserCrApplication"}, AppKind(chromium=True)),
        "Fox": ({"CFBundleURLTypes": schemes("https", "http")}, AppKind(http=True)),
        "Mail": ({"CFBundleURLTypes": schemes("mailto", "https")}, AppKind()),
        "Plain": ({"NSPrincipalClass": "NSApplication"}, AppKind()),
        "Odd": ({"CFBundleURLTypes": "http"}, AppKind()),
    }
    for name, (info, want) in cases.items():
        target = apps.resolve(install(system, name, f"dev.{name.lower()}", **info))
        assert apps.kind(target) == want
        assert apps.kind(target).web_like == (name in {"Chat", "Shell", "Fox"})
    assert apps.kind(AppTarget("/no/such/Thing.app", "", "Thing", None)) == AppKind()


def test_browser_classification_lists_general_chromium_firefox_family(system):
    """A browser is told by what its bundle declares, never by a list of ids."""
    web = {"CFBundleURLTypes": schemes("http", "https", "file")}
    chromium = install(
        system, "Any", "dev.never.seen", NSPrincipalClass="BrowserCrApplication", **web
    )
    gecko = install(
        system, "Gecko", "dev.also.new", NSPrincipalClass="GeckoNSApplication", **web
    )
    assert apps.kind(apps.resolve(chromium)) == AppKind(chromium=True, http=True)
    assert apps.kind(apps.resolve(gecko)) == AppKind(http=True)
    ids = [
        v for v in vars(apps).values() if isinstance(v, (set, frozenset, list, tuple))
    ]
    assert not [v for v in ids if any("com." in str(item) for item in v)]


# --- the app list ---------------------------------------------------------------------


def test_app_catalog_comes_spotlight_query_last_used_date(system):
    """Regular running apps, plus apps used in the last 14 days, cut again."""
    assert apps.USAGE_QUERY == (
        'kMDItemContentType == "com.apple.application-bundle" && kMDItemFSName == "*.app"'
        " && kMDItemLastUsedDate_Ranking >= $time.today(-14)"
    )
    assert apps.RECENT_SECONDS == 14 * DAY == 1_209_600
    system.procs = [
        proc(11, "Mail", "/Applications/Mail.app", "dev.mail"),
        proc(12, "python", "/Frameworks/Python.app", "org.python", regular=False),
        proc(13, "Helper", None, regular=False),
    ]
    system.usage = [
        used("/Applications/Edge.app", days_ago=14, bundle_id="dev.edge"),
        apps.Used("/Applications/Old.app", NOW - 14 * DAY - 1, 9, "Old", "dev.old"),
        apps.Used("/Applications/Never.app", None, 9, "Never", "dev.never"),
    ]
    assert [row["id"] for row in apps.list_apps()] == ["dev.mail", "dev.edge"]


def test_list_apps_item_shape_drops_path_frontmost(system):
    """The row, its keys in order, and what stands in for a missing value."""
    system.procs = [
        proc(11, "Mail", "/Applications/Mail.app", "dev.mail"),
        proc(12, "", "/Applications/Nameless.app", "dev.nameless"),
        proc(13, "Raw Tool", None),
    ]
    system.front = [11]
    system.usage = [
        used("/Applications/Mail.app", days_ago=0.5, count=4716),
        apps.Used("/Applications/Map.app", NOW - DAY, None, None, None),
    ]
    assert apps.list_apps() == [
        {
            "displayName": "Mail",
            "id": "dev.mail",
            "isRunning": True,
            "lastUsedDate": "2027-01-14T20:00:00Z",
            "useCount": 4716,
        },
        {"displayName": "dev.nameless", "id": "dev.nameless", "isRunning": True},
        {"displayName": "Raw Tool", "id": "#pid:13", "isRunning": True},
        {
            "displayName": "Map",
            "id": "Map",
            "isRunning": False,
            "lastUsedDate": "2027-01-14T08:00:00Z",
        },
    ]


def test_the_list_order(system):
    """In front first, then running, then by the day last used and the use count."""
    system.procs = [
        proc(1, "Idle", "/A/Idle.app", "idle"),
        proc(2, "Busy", "/A/Busy.app", "busy"),
        proc(3, "Front", "/A/Front.app", "front"),
        proc(4, "Often", "/A/Often.app", "often"),
        proc(5, "Yesterday", "/A/Yesterday.app", "yesterday"),
    ]
    system.front = [3]
    system.usage = [
        used("/A/Busy.app", days_ago=0.2, count=5),
        used("/A/Often.app", days_ago=0.3, count=900),
        used("/A/Yesterday.app", days_ago=1.2, count=5000),
        used("/A/Closed.app", days_ago=0.1, count=2, bundle_id="closed"),
        used("/A/Shut.app", days_ago=0.25, count=70, bundle_id="shut"),
        used("/A/Past.app", days_ago=3, count=999, bundle_id="past"),
    ]
    order = [row["id"] for row in apps.list_apps()]
    assert order == [
        "front",
        "often",
        "busy",
        "yesterday",
        "idle",
        "shut",
        "closed",
        "past",
    ]


def test_no_fixed_list_of_own_bundle_ids(system):
    """No fixed list of our own ids. This process is simply never in the list."""
    system.procs = [
        proc(os.getpid(), "Harness", "/A/Harness.app", "dev.harness"),
        proc(2, "Mail", "/A/Mail.app", "dev.mail"),
    ]
    assert [row["id"] for row in apps.list_apps()] == ["dev.mail"]


def test_frontmost_app_tracking_skips_the_helper_itself(system):
    """The app in front is never this process and never the caller's host."""
    system.procs = [
        proc(os.getpid(), "Harness", "/A/Harness.app", "dev.harness"),
        proc(2, "Terminal", "/A/Terminal.app", "dev.terminal"),
        proc(3, "Overlay", "/A/Overlay.app", "dev.overlay", regular=False),
        proc(4, "Mail", "/A/Mail.app", "dev.mail"),
    ]
    system.front = [os.getpid(), 3, 2, 2, 4]
    assert apps.frontmost().pid == 2
    assert apps.frontmost(frozenset({"/A/Terminal.app"})).pid == 4
    system.front = [3]
    assert apps.frontmost() is None
    system.front = [2, 4]
    system.usage = [used("/A/Mail.app"), used("/A/Terminal.app")]
    hosted = apps.list_apps(frozenset({"/A/Terminal.app"}))
    assert [row["id"] for row in hosted][:2] == ["dev.mail", "dev.terminal"]


def test_app_catalog_prewarmed_loaded_timeout_exclusions(system, monkeypatch):
    """The query starts early; when it is late the list is running apps only."""
    assert apps.CATALOG_LIMIT == 2.0
    monkeypatch.setattr(apps, "CATALOG_LIMIT", 0.05)
    release, calls = threading.Event(), []

    def slow():
        calls.append(1)
        release.wait(5)
        return [used("/A/Recent.app", bundle_id="dev.recent")]

    system.used = slow
    system.procs = [proc(2, "Mail", "/A/Mail.app", "dev.mail")]
    apps.prewarm()
    apps.prewarm()  # one query at a time
    assert [row["id"] for row in apps.list_apps()] == ["dev.mail"]
    assert calls == [1]
    release.set()
    apps._CATALOG["thread"].join(5)
    assert [row["id"] for row in apps.list_apps()] == ["dev.mail", "dev.recent"]
    apps._CATALOG["thread"].join(5)
    assert len(calls) == 2  # each list starts the query for the next one


@pytest.mark.skipif(sys.platform != "darwin", reason="asks the macOS kernel")
def test_the_kernel_says_which_process_runs_which_program():
    """What the running check rests on: fresh also where the app lists lag behind."""
    me = os.getpid()
    assert me in apps._pids() and 0 < len(apps._pids()) < 100_000
    assert apps._executable(me) and os.path.isabs(apps._executable(me))
    assert apps._executable(2**30) == ""
    real = apps._System()
    assert real.runs(me, f"#pid:{me}") and not real.runs(me, f"#pid:{me + 1}")
    folder = os.path.dirname(apps._executable(me))
    assert real.runs(me, folder) and not real.runs(me, folder + "/nowhere")
    assert not real.runs(2**30, folder)


# --- live: reads the real Mac, starts only Dictionary ---------------------------------


@pytest.mark.live
def test_live_resolve_reads_the_real_mac():
    path = "/System/Applications/Dictionary.app"
    for identifier in (
        "Dictionary",
        "com.apple.Dictionary",
        path,
        "dictionary",
        "Dictionary.app",
    ):
        target = apps.resolve(identifier)
        assert (target.path, target.bundle_id) == (path, "com.apple.Dictionary")
    assert apps.kind(apps.resolve("Dictionary")) == AppKind()
    with pytest.raises(errors.HarnessError, match="Invalid app: /bin/ls"):
        apps.resolve("/bin/ls")
    rows = apps.list_apps()
    assert rows and all(row["isRunning"] for row in rows[:1])
    assert all(
        set(row) <= {"displayName", "id", "isRunning", "lastUsedDate", "useCount"}
        for row in rows
    )


@pytest.mark.live
def test_live_background_launch_does_not_change_the_front_app():
    appkit = pytest.importorskip("AppKit")
    from macos_harness import windows

    workspace = appkit.NSWorkspace.sharedWorkspace()
    front = workspace.frontmostApplication().processIdentifier()
    with background_app() as target:
        key = windows.key_window(target)
        assert key.pid == target.pid and key.frame[2] > 0 and key.window_id
        assert windows.server_window(key.window_id)["pid"] == target.pid
        assert windows.ref(key.element).window_id == key.window_id
        inside = (key.frame[0] + key.frame[2] / 2, key.frame[1] + key.frame[3] / 2)
        assert windows.at(target, inside).window_id == key.window_id
        assert workspace.frontmostApplication().processIdentifier() == front
        assert apps.frontmost().pid != target.pid
        listed = {row["id"]: row for row in apps.list_apps()}
        assert listed[target.bundle_id]["isRunning"] is True
        assert apps.resolve(target.path) == apps.resolve(target.bundle_id) == target
        assert apps.running(target) == target


def test_a_service_that_is_no_app_has_the_id_of_the_bundle_its_program_lies_in(
    tmp_path,
):
    """The process that draws a password sheet or a file panel inside an app's window
    is not listed as an app: it had no id, and a process with no id passed the lists."""
    service = tmp_path / "Some.framework/XPCServices/Agent.xpc"
    (service / "Contents/MacOS").mkdir(parents=True)
    (service / "Contents/Info.plist").write_bytes(
        plistlib.dumps({"CFBundleIdentifier": "com.example.agent"})
    )
    assert apps._bundle_of(str(service / "Contents/MacOS/Agent")) == "com.example.agent"
    assert apps._bundle_of(str(tmp_path / "bin/tool")) == ""
