"""Find, start and list apps.

`resolve` turns what the model wrote into one app. `launch` opens it in the background.
`list_apps` is what is running plus what was used lately. Every look at the system goes
through SYSTEM, so the tests put a fake there.
"""

from __future__ import annotations

import dataclasses
import functools
import os
import plistlib
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from . import errors, windows
from .model import PANEL_SERVICE, AppKind, AppTarget

LAUNCH_LIMIT = 5.0  # seconds a new app has to show a window
LAUNCH_POLL = 0.05
RECENT_DAYS = 14
RECENT_SECONDS = 1_209_600.0  # the same 14 days, cut again against the clock
# Past this cap the list is running apps only.
CATALOG_LIMIT = 2.0
QUERY_LIMIT = 10.0  # the usage query gives up by itself after this long
DATA_VOLUME = "/System/Volumes/Data"
PID_PREFIX = "#pid:"  # the id of a running process that has no bundle

# Opened in the background: nothing is activated, no window comes to the front.
OPEN_OPTIONS = {"activates": False, "allowsRunningApplicationSubstitution": True}
APP_FOLDERS = (
    "/Applications",
    "/Applications/Utilities",
    "~/Applications",
    "/System/Applications",
    "/System/Applications/Utilities",
    "/System/Library/CoreServices",
    "/System/Library/CoreServices/Applications",
    "/System/Volumes/Preboot/Cryptexes/App/System/Applications",
)
USAGE_QUERY = (
    'kMDItemContentType == "com.apple.application-bundle" && kMDItemFSName == "*.app"'
    f" && kMDItemLastUsedDate_Ranking >= $time.today(-{RECENT_DAYS})"
)
USAGE_ATTRIBUTES = (
    "kMDItemPath",
    "kMDItemLastUsedDate",
    "kMDItemLastUsedDate_Ranking",
    "kMDItemUseCount",
    "kMDItemDisplayName",
    "kMDItemCFBundleIdentifier",
)
# What makes an app web-like, read from its Info.plist. No list of bundle ids.
ELECTRON_CLASS = "AtomApplication"
CHROMIUM_CLASS = "BrowserCrApplication"
WEB_SCHEME = "http"


@dataclass(frozen=True)
class Proc:
    """One running process the system knows as an app."""

    pid: int
    path: str | None  # its .app bundle. None for a process with no bundle
    bundle_id: str
    name: str  # display name
    executable: str  # process name


@dataclass(frozen=True)
class Used:
    """One row of the usage index."""

    path: str
    last_used: float | None  # seconds since 1970
    use_count: int | None
    name: str | None
    bundle_id: str | None


def _pooled(method):
    """Run a method that makes many system objects inside its own release pool."""

    @functools.wraps(method)
    def run(*args):
        import objc

        with objc.autorelease_pool():
            return method(*args)

    return run


def _pids() -> list[int]:
    """Every process, from the kernel."""
    import ctypes

    libproc = ctypes.CDLL("/usr/lib/libproc.dylib")
    room = libproc.proc_listallpids(None, 0) + 64
    pids = (ctypes.c_int * room)()
    count = libproc.proc_listallpids(pids, ctypes.sizeof(pids))
    return list(pids[: max(count, 0)])


def _executable(pid: int) -> str:
    """The path of a process's program, from the kernel. Empty when the pid is gone."""
    import ctypes

    libproc = ctypes.CDLL("/usr/lib/libproc.dylib")
    path = ctypes.create_string_buffer(4096)
    length = libproc.proc_pidpath(pid, path, len(path))
    return _clean(path.raw[:length].decode("utf-8", "replace")) if length > 0 else ""


FILE_PANELS = {"save-panel", "open-panel", "GoToWindow"}  # windows of the file panels


def _panel_services() -> dict[int, int]:
    """Every file panel service process -> the app the system holds responsible."""
    import ctypes

    responsible = ctypes.CDLL(None).responsibility_get_pid_responsible_for_pid
    responsible.argtypes, responsible.restype = [ctypes.c_int], ctypes.c_int
    found = [p for p in _pids() if _executable(p).endswith("/" + PANEL_SERVICE)]
    return {service: responsible(service) for service in found}


def panel_service(pid: int) -> int | None:
    """The process that draws an app's open or save panel and takes its keys."""
    owned = [s for s, owner in _panel_services().items() if owner == pid]
    return owned[0] if owned else None


def _panel_owner_window(pid: int) -> str | None:
    """The identifier of an app's focused window."""
    from . import ax

    try:
        window = ax.get(ax.app(pid), "AXFocusedWindow")
        return None if window is None else ax.get(window, "AXIdentifier")
    except errors.HarnessError:
        return None


def panel_owner(candidates: list[AppTarget]) -> AppTarget:
    """Named by its own id, a file panel's service stands for the app that shows the
    panel: that app is read and its approval holds. (Asked for as itself, it needed
    an approval of its own: the person was asked, the agent waited 110 s.) Only the
    given apps are looked at, newest first: those the agent already reads."""
    for target in reversed(candidates):
        if target.pid and _panel_owner_window(target.pid) in FILE_PANELS:
            return target
    raise errors.no_file_panel()


class _System:
    """The real Mac. AppKit is imported only when a function runs."""

    def __init__(self):
        self._known: dict[int, Proc] = {}
        self._lock = threading.Lock()

    @_pooled
    def running(self) -> list[Proc]:
        """Every process the system knows as an app, asked one pid at a time.

        The pids come from the kernel, so an app that quit is gone at once. What never
        changes for a pid is read once: the first call takes about 60 ms, the ones
        after it about 4 ms. A pid the system does not know as an app is asked again
        every time: an app that is just starting is a plain process for a moment.
        """
        import AppKit

        lookup = AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_
        pids = _pids()
        with self._lock:
            known = {pid: self._known[pid] for pid in pids if pid in self._known}
            for pid in pids:
                app = None if pid in known else lookup(pid)
                if app is not None:
                    known[pid] = self._proc(pid, app)
            self._known = known
            return list(known.values())

    @staticmethod
    def _proc(pid: int, app) -> Proc:
        bundle, executable = app.bundleURL(), app.executableURL()
        # For a bare program the system gives the program file as its "bundle".
        bundled = bundle is not None and os.path.isdir(str(bundle.path()))
        return Proc(
            pid=pid,
            path=_clean(str(bundle.path())) if bundled else None,
            bundle_id=str(app.bundleIdentifier() or ""),
            name=str(app.localizedName() or ""),
            executable=str(executable.lastPathComponent() if executable else ""),
        )

    def runs(self, pid: int, path: str) -> bool:
        """Is this pid still the app at this path? Asked of the kernel, so always true
        to the moment, also for an app the lists above have not caught up with."""
        program = _executable(pid)
        if path.startswith(PID_PREFIX):
            return bool(program) and path == f"{PID_PREFIX}{pid}"
        return any(program.startswith(s + "/") for s in _spellings(path))

    @_pooled
    def regular(self, pid: int) -> bool:
        """Does it show in the Dock? Helpers and overlay processes do not."""
        import AppKit

        lookup = AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_
        app = lookup(pid)
        return app is not None and app.activationPolicy() == 0

    @_pooled
    def bundle_paths(self, bundle_id: str) -> list[str]:
        import AppKit

        workspace = AppKit.NSWorkspace.sharedWorkspace()
        urls = workspace.URLsForApplicationsWithBundleIdentifier_(bundle_id) or []
        return [str(url.path()) for url in urls]

    @_pooled
    def named(self, name: str) -> str | None:
        import AppKit

        path = AppKit.NSWorkspace.sharedWorkspace().fullPathForApplication_(name)
        return str(path) if path else None

    def open(self, path: str) -> int | None:
        import AppKit
        import Foundation

        config = AppKit.NSWorkspaceOpenConfiguration.configuration()
        for option, value in OPEN_OPTIONS.items():
            config.setValue_forKey_(value, option)
        done, opened = threading.Event(), []

        def finished(app, error):
            opened.append(None if app is None else int(app.processIdentifier()))
            done.set()

        url = Foundation.NSURL.fileURLWithPath_(path)
        workspace = AppKit.NSWorkspace.sharedWorkspace()
        workspace.openApplicationAtURL_configuration_completionHandler_(
            url, config, finished
        )
        done.wait(LAUNCH_LIMIT)
        return opened[0] if opened else None

    def front_pids(self) -> list[int]:
        """Owners of the normal windows on screen, front first."""
        return [row["pid"] for row in windows.server_windows() if row["layer"] == 0]

    @_pooled
    def used(self) -> list[Used]:
        import Foundation

        query = Foundation.NSMetadataQuery.alloc().init()
        text = Foundation.NSPredicate.predicateFromMetadataQueryString_(USAGE_QUERY)
        query.setPredicate_(text)
        query.startQuery()
        loop = Foundation.NSRunLoop.currentRunLoop()
        end = time.monotonic() + QUERY_LIMIT
        while query.isGathering() and time.monotonic() < end:
            step = Foundation.NSDate.dateWithTimeIntervalSinceNow_(0.05)
            loop.runMode_beforeDate_(Foundation.NSDefaultRunLoopMode, step)
        query.disableUpdates()
        out = []
        for index in range(query.resultCount()):
            item = query.resultAtIndex_(index)
            values = [item.valueForAttribute_(name) for name in USAGE_ATTRIBUTES]
            path, used, day, count, name, bundle_id = values
            used = used if used is not None else day
            out.append(
                Used(
                    path=_clean(str(path)),
                    last_used=None if used is None else used.timeIntervalSince1970(),
                    use_count=None if count is None else int(count),
                    name=None if name is None else str(name),
                    bundle_id=None if bundle_id is None else str(bundle_id),
                )
            )
        query.stopQuery()
        return out


SYSTEM = _System()


# --- paths and bundles ----------------------------------------------------------------


def _clean(path: str) -> str:
    path = os.path.normpath(path)
    return path[len(DATA_VOLUME) :] if path.startswith(DATA_VOLUME + "/") else path


def _info(path: str) -> dict | None:
    """The app's Info.plist. None when the path is not an app bundle."""
    for inner in ("Contents/Info.plist", "WrappedBundle/Info.plist", "Info.plist"):
        try:
            with open(os.path.join(path, inner), "rb") as file:
                info = plistlib.load(file)
        except (OSError, ValueError, plistlib.InvalidFileException):
            continue
        return info if isinstance(info, dict) else None
    return None


def _stem(path: str) -> str:
    return os.path.basename(path).removesuffix(".app")


def _spellings(path: str) -> set[str]:
    return {path, os.path.realpath(path)}


def _proc_target(proc: Proc) -> AppTarget:
    path = proc.path or f"{PID_PREFIX}{proc.pid}"
    # With no bundle id the path stands in, so an approval never covers two programs.
    return AppTarget(
        path, proc.bundle_id or path, proc.name or proc.executable, proc.pid
    )


def _target(path: str, procs: list[Proc]) -> AppTarget:
    """The app at a path. A running instance of it wins."""
    spellings = _spellings(path)
    live = next((p for p in procs if p.path in spellings), None)
    if live:
        return _proc_target(live)
    info = _info(path) or {}
    name = info.get("CFBundleDisplayName") or info.get("CFBundleName") or _stem(path)
    return AppTarget(path, info.get("CFBundleIdentifier") or "", name, None)


# --- resolve --------------------------------------------------------------------------


def _by_pid(identifier: str, procs: list[Proc]) -> AppTarget | None:
    pid = identifier.removeprefix(PID_PREFIX)
    if pid == identifier or not pid.isdigit():
        return None
    live = next((p for p in procs if p.pid == int(pid)), None)
    return _proc_target(live) if live else None


def _by_path(identifier: str, procs: list[Proc]) -> AppTarget | None:
    if not identifier.startswith(("/", "~")):
        return None
    path = _clean(os.path.expanduser(identifier))
    if not path.endswith(".app") or _info(path) is None:
        raise errors.invalid_app(identifier)  # a bare executable, a folder, nothing
    return _target(path, procs)


def _by_bundle_id(identifier: str, procs: list[Proc]) -> AppTarget | None:
    live = [p.path for p in procs if p.path and p.bundle_id == identifier]
    paths = live or [_clean(path) for path in SYSTEM.bundle_paths(identifier)]
    paths = list(dict.fromkeys(paths))
    if len(paths) > 1:
        raise errors.ambiguous_app(identifier, paths)
    return _target(paths[0], procs) if paths else None


def _by_name(name: str, procs: list[Proc], same: Callable) -> AppTarget | None:
    """A running app's display name, an installed app's, then a process name."""
    if named := [p for p in procs if same(p.name, name)]:
        return _proc_target(_docked_first(named))
    for folder in APP_FOLDERS:
        folder = os.path.expanduser(folder)
        try:
            entries = sorted(os.listdir(folder))
        except OSError:
            continue
        for entry in entries:
            if entry.endswith(".app") and same(_stem(entry), name):
                return _target(os.path.join(folder, entry), procs)
    if path := SYSTEM.named(name):
        return _target(_clean(path), procs)
    named = [p for p in procs if same(p.executable, name)]
    return _proc_target(_docked_first(named)) if named else None


def _docked_first(procs: list[Proc]) -> Proc:
    """Of several processes with one name, an app in the Dock before a helper."""
    return min(procs, key=lambda p: not SYSTEM.regular(p.pid))


def _exactly(a: str, b: str) -> bool:
    return a == b


def _ignoring_case(a: str, b: str) -> bool:
    return a.casefold() == b.casefold()


def resolve(identifier: str) -> AppTarget:
    """One app from a path, a bundle id, a display name or a process name."""
    if not identifier.strip():
        raise errors.invalid_app(identifier)
    procs = SYSTEM.running()
    found = (
        _by_pid(identifier, procs)
        or _by_path(identifier, procs)
        or _by_bundle_id(identifier, procs)
    )
    # Tried only when nothing above matched: the name ignoring case, then a name that
    # ends in .app without the ending.
    ends_in_app = identifier.casefold().endswith(".app") and "/" not in identifier
    names = [identifier] + [identifier[:-4]] * ends_in_app
    tries = [(name, same) for name in names for same in (_exactly, _ignoring_case)]
    for name, same in tries:
        found = found or _by_name(name, procs, same)
    if not found:
        raise errors.invalid_app(identifier)
    return found


# --- running, launching ---------------------------------------------------------------


def running(target: AppTarget) -> AppTarget:
    """The target with the pid it has now. An app that quit is not running."""
    if target.pid and SYSTEM.runs(target.pid, target.path):
        return target
    paths = _spellings(target.path)
    live = [
        p
        for p in SYSTEM.running()
        if p.path in paths or target.path == f"{PID_PREFIX}{p.pid}"
    ]
    if not live:
        raise errors.app_not_running(target.path)
    return replace(target, pid=live[0].pid)


def launch(target: AppTarget, abandoned=lambda: False) -> AppTarget:
    """Open the app in the background and wait for its first window. No settle here."""
    pid = SYSTEM.open(target.path)
    if not pid:
        raise errors.failed_to_launch()
    target = replace(target, pid=pid)
    end = time.monotonic() + LAUNCH_LIMIT
    while True:
        try:
            windows.key_window(target)
            return target
        except errors.HarnessError:
            if time.monotonic() >= end:
                raise errors.timeout_reached() from None
        if abandoned():  # nobody waits for this call any more
            raise errors.request_timed_out()
        time.sleep(LAUNCH_POLL)


@functools.lru_cache(maxsize=256)
def _kind(path: str) -> AppKind:
    info = _info(path) or {}
    types = info.get("CFBundleURLTypes")
    schemes = [
        scheme
        for entry in (types if isinstance(types, list) else [])
        if isinstance(entry, dict)
        for scheme in entry.get("CFBundleURLSchemes") or []
    ]
    principal = info.get("NSPrincipalClass")
    return AppKind(
        electron=principal == ELECTRON_CLASS,
        chromium=principal == CHROMIUM_CLASS,
        http=WEB_SCHEME in schemes,
    )


CATALYST = "_AXExhibitsMacCatalystBehavior"  # the app element says so itself


@functools.lru_cache(maxsize=256)
def _catalyst(pid: int) -> bool:
    from . import ax

    try:
        return ax.get(ax.app(pid), CATALYST) is True
    except errors.HarnessError:
        return False


BUNDLES = (".app", ".xpc", ".appex", ".bundle")  # folders that carry a bundle id


def _bundle_of(program: str) -> str:
    """The id of the innermost bundle a program lies in. "" when it lies in none."""
    for folder in Path(program).parents:
        for info in (
            ("Contents/Info.plist", "Info.plist") if folder.suffix in BUNDLES else ()
        ):
            try:
                found = plistlib.loads((folder / info).read_bytes()).get(
                    "CFBundleIdentifier"
                )
            except (OSError, ValueError, plistlib.InvalidFileException):
                continue
            if found:
                return str(found)
    return ""


def bundle_id(pid: int) -> str:
    """The bundle id of a running process. A service that draws into another app's
    window is no app to the system: it has the id of the bundle its program lies in.
    "" for a process in no bundle, or one that is gone."""
    import AppKit

    app = AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
    return str((app and app.bundleIdentifier()) or _bundle_of(_executable(pid)))


def kind(target: AppTarget) -> AppKind:
    """What the bundle says, and, for a running app, whether it is an iPad app on the
    Mac: the app element is asked for CATALYST (once per process)."""
    found = _kind(target.path)
    if target.pid and _catalyst(target.pid):
        return dataclasses.replace(found, catalyst=True)
    return found


# --- the app list ---------------------------------------------------------------------


def frontmost(
    hosts: frozenset[str] = frozenset(), procs: list[Proc] | None = None
) -> AppTarget | None:
    """The app in front. Never this process, and never the caller's own host app."""
    procs = SYSTEM.running() if procs is None else procs
    by_pid = {p.pid: p for p in procs}
    for pid in dict.fromkeys(SYSTEM.front_pids()):  # owners of windows, front first
        proc = by_pid.get(pid)
        ours = pid == os.getpid() or (proc and proc.path in hosts)
        if proc and not ours and SYSTEM.regular(pid):
            return _proc_target(proc)
    return None


_CATALOG: dict = {"rows": None, "thread": None}
_CATALOG_LOCK = threading.Lock()


def _load_catalog() -> None:
    SYSTEM.running()  # the first listing of processes is the slow one
    _CATALOG["rows"] = SYSTEM.used()


def prewarm() -> None:
    """Start the usage query now, so the first app list does not wait for it."""
    with _CATALOG_LOCK:
        thread = _CATALOG["thread"]
        if thread is None or not thread.is_alive():
            thread = threading.Thread(target=_load_catalog, daemon=True)
            _CATALOG["thread"] = thread
            thread.start()


def _catalog() -> list[Used]:
    """The usage rows of the last query, while a new one runs for the next call."""
    prewarm()
    if _CATALOG["rows"] is None:
        _CATALOG["thread"].join(CATALOG_LIMIT)
    return _CATALOG["rows"] or []


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _row(name: str, ident: str, is_running: bool, used: Used | None) -> dict:
    row = {"displayName": name, "id": ident, "isRunning": is_running}
    if used:
        row["lastUsedDate"] = _iso(used.last_used)
        if used.use_count is not None:
            row["useCount"] = used.use_count
    return row


def _rank(in_front: bool, is_running: bool, used: Used | None) -> tuple:
    """In front, then running, then by day last used and by use count."""
    day = used.last_used // 86400 if used else 0
    return (
        not in_front,
        not is_running,
        used is None,
        -day,
        -(used and used.use_count or 0),
    )


def list_apps(hosts: frozenset[str] = frozenset()) -> list[dict]:
    """Running apps that show in the Dock, plus apps used in the last 14 days."""
    cut = time.time() - RECENT_SECONDS
    recent = [u for u in _catalog() if u.last_used is not None and u.last_used >= cut]
    used = {spelling: u for u in recent for spelling in _spellings(u.path)}
    procs = SYSTEM.running()
    front = frontmost(hosts, procs)
    ranked = []
    for proc in procs:
        if proc.pid == os.getpid() or not SYSTEM.regular(proc.pid):
            continue
        usage = used.get(proc.path)
        recent = [u for u in recent if u is not usage]
        ident = proc.bundle_id or f"{PID_PREFIX}{proc.pid}"
        row = _row(proc.name or proc.bundle_id, ident, True, usage)
        ranked.append((_rank(bool(front and front.pid == proc.pid), True, usage), row))
    for usage in recent:
        name = usage.name or _stem(usage.path)
        row = _row(name, usage.bundle_id or name or "unknown", False, usage)
        ranked.append((_rank(False, False, usage), row))
    ranked.sort(key=lambda pair: pair[0])
    return [row for _, row in ranked]
