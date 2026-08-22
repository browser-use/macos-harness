# Changelog

All notable changes to macOS Harness are documented here. This project
follows [Semantic Versioning](https://semver.org/).

## [0.2.0] - 2026-08-22

### Added

- `mac.do`: a receipted, verified operations surface for mutations,
  recommended over the raw primitives for anything that changes state.
  `press`, `set`, `toggle`, `run`, and `key` mutate; `recall` looks up a
  past receipt by its idempotency token without dispatching anything. Every
  call returns an immutable, JSON-safe `Receipt` -- `outcome` (`planned`,
  `done`, `already`, `failed`), `acted` (`no`, `yes`, `unknown`), the
  backend and executor that actually ran it, the normalized request and
  target, whether anything changed, whether a postcondition verified the
  effect, duration_s, and a structured error on failure. `present`/`gone`
  postconditions (the same call shape as `ax.wait`/`ax.wait_gone`) confirm
  an operation's real effect, and default to the operation's own scope
  when left unscoped -- except `run`, which has no scope of its own and
  requires an explicitly scoped postcondition. `press`/`run`/`key`
  additionally take a nonempty `once` token for at-most-once dispatch
  within one live `MacOS` instance. Its in-memory ledger is not shared
  with a new instance or process. A repeat call with the same token
  replays the recorded receipt instead of dispatching again, and an
  interrupted or still in-flight attempt fails closed with a failed,
  `acted="unknown"` receipt instead of risking a second dispatch.
  `dry_run=True` validates, resolves, and (for `run`) compiles a script
  without ever dispatching it or touching the idempotency ledger.
- A machine-readable error taxonomy: `errors.ErrorCode` (nine wire codes
  shared by the native agent protocol and `mac.do`) and
  `MacOSError.code`/`.details`/`.to_json()`.
- `macos-harness --version`.
- A `py.typed` marker (PEP 561): the package now ships type information in
  both the wheel and the sdist.
- Python 3.13 and 3.14 support, alongside the existing 3.11/3.12.
- Background-safe cross-process AX controls: `ax.query_all`, `ax.wait`,
  `ax.wait_gone`, and `ax.press` act across every running application (or a
  named subset), not just one, for background AutoFill sheets and system
  popovers outside the app already targeted.
- A persistent Swift Accessibility agent (opt-in native backend) that can
  take over a fixed, narrow set of Accessibility calls for process
  isolation; `python` remains the default and the latency-recommended
  choice. Each `MacOS(backend="native"/"auto")` instance that dispatches a
  routed call launches its own private child process over an inherited,
  validated UNIX-domain socket pair that only that process holds either
  end of, with the handshake bound to that child's own PID -- there is no
  shared daemon, no well-known socket path, and no pidfile. PyPI wheels
  bundle an ad-hoc-signed universal2 (arm64 + x86_64) build of the agent,
  so a cold launch costs about 240 ms on an M4 Pro instead of the ~1,125 ms
  a from-source SwiftPM build takes; most installs never build it
  themselves.

### Changed

- Telemetry is now opt-in. A fresh install sends nothing until
  `macos-harness telemetry enable`; `DO_NOT_TRACK`, `MACOS_HARNESS_TELEMETRY`,
  and `ANONYMIZED_TELEMETRY` remain fail-closed kill switches that always
  win, but no environment variable can turn telemetry on. The documented
  payload, storage path, and endpoint are now complete and accurate,
  including `python_version`, which was previously sent but undocumented.
- The package version is now defined in exactly one place
  (`src/macos_harness/_version.py`) and read directly by
  `macos_harness.__version__`, the CLI, telemetry, and Hatchling's dynamic
  version config -- nothing derives it separately through
  `importlib.metadata`.

### Security

- A telemetry endpoint override (`MACOS_HARNESS_POSTHOG_HOST`) is honored
  only when it is an HTTPS URL; any other scheme is ignored in favor of the
  built-in default, so no environment variable can silently redirect
  telemetry to an unencrypted or unintended endpoint.
- The publish workflow now triggers only on a published GitHub release, no
  longer on arbitrary `workflow_dispatch`; asserts the release tag matches
  the package's own version before doing any build work; runs Ruff, the
  non-native Python test suite, and the Swift test suite as release gates;
  builds the universal2 native agent for real (not just `swift build`); and
  reaches the trusted-publish step only through pinned, SHA-verified
  GitHub Actions.

## [0.1.2] - 2026-08-17

### Fixed

- Render the project banner correctly on PyPI.

## [0.1.1] - 2026-08-17

### Fixed

- Publish the PyPI package from a Linux runner.
- Animate the README hero image.
- Use a repository-relative path for the banner image.

## [0.1.0] - 2026-08-15

### Added

- Initial release: six raw primitives (`see`, `key`, `type`, `click`, `ax`,
  `script`) for controlling a Mac from one persistent Python process, plus
  Browser Harness integration and local filesystem/subprocess access.
