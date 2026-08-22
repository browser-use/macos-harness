import Foundation

/// Resolves and presses exactly one AX target on behalf of the `ax_press` wire operation.
///
/// `PressCoordinator` never activates or raises an application itself — the injected `search`
/// closure is expected to already be scoped to the target process and bounded to the effective
/// press search limit of two, matching `MacOS._native_press`'s hardcoded `limit: 2` in
/// `macos.py`: just enough to prove ambiguity without paying for an unbounded search. This
/// coordinator's own job is narrower — fail closed on anything but exactly one AXPress-capable
/// match, then sample frontmost state immediately before and immediately after delegating to
/// the injected `performPress` closure, reporting `focus.changed` only when the target process
/// was not already frontmost immediately before the press but became frontmost immediately
/// after it. That mirrors `MacOS._guard_focus` in `macos.py` one for one: the press has already
/// happened by the time focus is judged, so a background target that steals focus is reported
/// as an error even though its action already ran.
enum PressCoordinator {

  struct Dependencies {
    /// Finds the candidate elements to press. Expected to already be scoped to the target
    /// process and bounded to an effective limit of two.
    let search: () throws -> [ElementDescriptor]
    /// Samples the current frontmost application's pid, or `nil` if none.
    let frontmostPID: () -> pid_t?
    /// Performs the actual `AXPress` action on the resolved element.
    let performPress: (ElementDescriptor) throws -> Void
  }

  /// Throws `element.unknown` when the search finds no match at all, `bad_request` when it
  /// finds more than one (ambiguous — the caller's `search_key`/`text` must narrow further),
  /// or `unsupported_op` when the single match it does settle on does not expose `AXPress` --
  /// in all three cases before `performPress` is ever invoked, so a caller retrying only
  /// `element.unknown` (see `MacOS._native_press` in `macos.py`) never wastes its deadline
  /// retrying an ambiguous or non-pressable target that another search will never resolve
  /// any differently. Once the press is attempted, any error `performPress` throws propagates
  /// unchanged: a failed press is never reported as a success. A successful press that made a
  /// background target frontmost throws a `focus.changed` `AgentError` despite the press
  /// itself already having happened.
  static func run(targetPID: pid_t, _ deps: Dependencies) throws -> ElementDescriptor {
    let matches = try deps.search()
    guard !matches.isEmpty else {
      throw AgentError(
        code: "element.unknown",
        message:
          "AX press expected exactly one match for pid \(targetPID), found 0"
      )
    }
    guard matches.count == 1 else {
      throw AgentError(
        code: "bad_request",
        message:
          "AX press expected exactly one match for pid \(targetPID), found "
          + "\(matches.count) (ambiguous); narrow search_key/text to a unique target"
      )
    }
    let match = matches[0]
    guard match.actions.contains("AXPress") else {
      throw AgentError(
        code: "unsupported_op",
        message:
          "AX press target does not expose AXPress; available actions: \(match.actions)"
      )
    }

    let wasFrontmost = deps.frontmostPID() == targetPID
    try deps.performPress(match)
    let isFrontmost = deps.frontmostPID() == targetPID

    if !wasFrontmost && isFrontmost {
      throw AgentError(
        code: "focus.changed",
        message: "Process \(targetPID) became frontmost during AX press; stopped"
      )
    }
    return match
  }
}
