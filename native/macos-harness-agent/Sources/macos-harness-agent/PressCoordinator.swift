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

  /// Presses the single AXPress-capable element `deps.search()` reports for `targetPID`.
  ///
  /// Throws an `element.unknown` `AgentError` when the search does not settle on exactly one
  /// match, or when that lone match does not expose `AXPress` — in both cases before
  /// `performPress` is ever invoked. Once the press is attempted, any error `performPress`
  /// throws propagates unchanged: a failed press is never reported as a success. A successful
  /// press that made a background target frontmost throws a `focus.changed` `AgentError`
  /// despite the press itself already having happened.
  static func run(targetPID: pid_t, _ deps: Dependencies) throws -> ElementDescriptor {
    let matches = try deps.search()
    guard matches.count == 1 else {
      throw AgentError(
        code: "element.unknown",
        message:
          "AX press expected exactly one match for pid \(targetPID), found \(matches.count)"
      )
    }
    let match = matches[0]
    guard match.actions.contains("AXPress") else {
      throw AgentError(
        code: "element.unknown",
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
