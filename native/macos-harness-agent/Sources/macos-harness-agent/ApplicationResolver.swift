import Foundation

/// Snapshot of a running application, mirroring `MacOS._app_info` in the Python client
/// (`src/macos_harness/macos.py`).
struct AppInfo {
  let name: String
  let bundleID: String?
  let pid: pid_t
  let path: String?
}

/// Resolves an app selector (name, bundle ID, path, or PID, matched case-insensitively) against
/// a list of running-app candidates.
///
/// Mirrors `MacOS._resolve_app` (`src/macos_harness/macos.py:418`) field for field: a
/// case-insensitive exact match against pid/name/bundle id/path always outranks a merely
/// substring match — `matches = exact or candidates` in the Python source — so a single exact
/// hit is never outvoted by a larger substring-only pool, no candidates at all is
/// `app.not_found`, and an ambiguity report lists at most eight candidate names.
enum AppResolver {

  /// Ambiguity reports list at most this many candidate names, matching the Python client.
  private static let maxAmbiguousNames = 8

  static func resolve(query: String, candidates: [AppInfo]) throws -> AppInfo {
    let needle = query.lowercased()

    var exact: [AppInfo] = []
    var substring: [AppInfo] = []

    for app in candidates {
      let values: [String?] = [String(app.pid), app.name, app.bundleID, app.path]
      let lowered = values.compactMap { $0 }.filter { !$0.isEmpty }.map { $0.lowercased() }

      if lowered.contains(needle) {
        exact.append(app)
      } else if lowered.contains(where: { $0.contains(needle) }) {
        substring.append(app)
      }
    }

    let matches = exact.isEmpty ? substring : exact
    guard !matches.isEmpty else {
      throw AgentError(
        code: "app.not_found", message: "No running application matches \"\(query)\"")
    }
    guard matches.count == 1 else {
      let names = matches.prefix(maxAmbiguousNames)
        .map { "\($0.name) (\($0.pid))" }
        .joined(separator: ", ")
      throw AgentError(
        code: "app.ambiguous",
        message: "Application query \"\(query)\" is ambiguous: \(names)")
    }
    return matches[0]
  }
}
