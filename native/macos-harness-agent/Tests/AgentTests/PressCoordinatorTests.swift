import XCTest

@testable import macos_harness_agent

// MARK: - Assumed production API (ax_press seam, owned by the AX handlers slice)
//
//   struct ElementDescriptor { let handle: Int; let actions: [String] }
//   internal enum PressCoordinator {
//       struct Dependencies {
//           let search: () throws -> [ElementDescriptor]
//           let frontmostPID: () -> pid_t?
//           let performPress: (ElementDescriptor) throws -> Void
//       }
//       static func run(targetPID: pid_t, _ deps: Dependencies) throws -> ElementDescriptor
//   }
//   internal enum AgentError: Error {
//       var code: String { get }   // wire error code, e.g. "element.unknown" / "focus.changed"
//       var message: String { get }
//   }
//
// (Reported by NativeAX mid-implementation: dependencies are plain throwing/non-throwing
// closures rather than protocols, and `run` throws `AgentError` rather than returning a
// `Result`. `ElementDescriptor` is assumed to need only `handle` + `actions` to exercise this
// seam; if its real initializer needs more fields, only the `descriptor(actions:)` factory
// below has to move.)
//
// `PressCoordinator` never activates or raises an app itself; it searches with an effective
// limit of two, fails closed on anything but exactly one AXPress-capable match, and samples
// frontmost state immediately before and after delegating to the injected `performPress`
// closure — reporting focus.changed only when a target that was not already frontmost becomes
// frontmost.

final class PressCoordinatorTests: XCTestCase {

  private let target: pid_t = 500
  private let bystander: pid_t = 600

  private func descriptor(handle: Int = 1, actions: [String] = ["AXPress"]) -> ElementDescriptor {
    ElementDescriptor(handle: handle, actions: actions)
  }

  private final class RecordingPerformer {
    private(set) var invocations: [ElementDescriptor] = []
    var shouldSucceed = true
    func perform(_ descriptor: ElementDescriptor) throws {
      invocations.append(descriptor)
      if !shouldSucceed {
        throw NSError(domain: "PressCoordinatorTests", code: 1)
      }
    }
  }

  private final class ScriptedFrontmost {
    private var values: [pid_t?]
    init(_ values: [pid_t?]) { self.values = values }
    func next() -> pid_t? {
      guard !values.isEmpty else { return nil }
      return values.removeFirst()
    }
  }

  private func assertAgentErrorCode(
    _ expression: @autoclosure () throws -> ElementDescriptor,
    equals expectedCode: String,
    file: StaticString = #filePath,
    line: UInt = #line
  ) {
    XCTAssertThrowsError(try expression(), file: file, line: line) { error in
      guard let agentError = error as? AgentError else {
        return XCTFail("expected an AgentError, got \(error)", file: file, line: line)
      }
      XCTAssertEqual(agentError.code, expectedCode, file: file, line: line)
    }
  }

  func testZeroMatchesFailsClosedWithoutPerforming() {
    let performer = RecordingPerformer()
    let frontmost = ScriptedFrontmost([bystander, bystander])
    let deps = PressCoordinator.Dependencies(
      search: { [] },
      frontmostPID: { frontmost.next() },
      performPress: performer.perform
    )
    assertAgentErrorCode(
      try PressCoordinator.run(targetPID: target, deps), equals: "element.unknown")
    XCTAssertTrue(performer.invocations.isEmpty)
  }

  func testMultipleMatchesFailsClosedWithBadRequestAndCount() {
    let performer = RecordingPerformer()
    let frontmost = ScriptedFrontmost([bystander, bystander])
    let deps = PressCoordinator.Dependencies(
      search: { [self.descriptor(handle: 1), self.descriptor(handle: 2)] },
      frontmostPID: { frontmost.next() },
      performPress: performer.perform
    )
    XCTAssertThrowsError(try PressCoordinator.run(targetPID: target, deps)) { error in
      guard let agentError = error as? AgentError else {
        return XCTFail("expected an AgentError, got \(error)")
      }
      // Ambiguous (>1 match) is the caller's search criteria being too loose, not an
      // unknown element and not worth retrying -- it is always `bad_request`, and the
      // match count stays visible in the message since there is no generic wire "details"
      // field to carry it separately.
      XCTAssertEqual(agentError.code, "bad_request")
      XCTAssertTrue(
        agentError.message.contains("2"),
        "expected the match count in the message, got \(agentError.message)")
    }
    XCTAssertTrue(performer.invocations.isEmpty, "an ambiguous match set must never be pressed")
  }

  func testMissingAXPressActionFailsClosedWithUnsupportedOp() {
    let performer = RecordingPerformer()
    let frontmost = ScriptedFrontmost([bystander, bystander])
    let deps = PressCoordinator.Dependencies(
      search: { [self.descriptor(actions: ["AXShowMenu"])] },
      frontmostPID: { frontmost.next() },
      performPress: performer.perform
    )
    // A single, unambiguous match that simply cannot be pressed is `unsupported_op`, not
    // `element.unknown`: the element was found just fine, it just has no AXPress action.
    assertAgentErrorCode(
      try PressCoordinator.run(targetPID: target, deps), equals: "unsupported_op")
    XCTAssertTrue(performer.invocations.isEmpty, "an element without AXPress must not be pressed")
  }

  func testSingleMatchNotFrontmostAndStaysNotFrontmostSucceeds() throws {
    let performer = RecordingPerformer()
    let frontmost = ScriptedFrontmost([bystander, bystander])
    let deps = PressCoordinator.Dependencies(
      search: { [self.descriptor(handle: 7)] },
      frontmostPID: { frontmost.next() },
      performPress: performer.perform
    )
    let match = try PressCoordinator.run(targetPID: target, deps)
    XCTAssertEqual(match.handle, 7)
    XCTAssertEqual(performer.invocations.count, 1)
  }

  func testTargetBecomingFrontmostAfterPressReportsFocusChanged() {
    let performer = RecordingPerformer()
    // Was not frontmost before the press, becomes frontmost immediately after it.
    let frontmost = ScriptedFrontmost([bystander, target])
    let deps = PressCoordinator.Dependencies(
      search: { [self.descriptor()] },
      frontmostPID: { frontmost.next() },
      performPress: performer.perform
    )
    assertAgentErrorCode(try PressCoordinator.run(targetPID: target, deps), equals: "focus.changed")
    XCTAssertEqual(
      performer.invocations.count, 1, "the press must still be attempted before the focus check")
  }

  func testTargetAlreadyFrontmostStayingFrontmostSucceeds() throws {
    let performer = RecordingPerformer()
    // Already frontmost before the press, and remains frontmost after it.
    let frontmost = ScriptedFrontmost([target, target])
    let deps = PressCoordinator.Dependencies(
      search: { [self.descriptor()] },
      frontmostPID: { frontmost.next() },
      performPress: performer.perform
    )
    let match = try PressCoordinator.run(targetPID: target, deps)
    XCTAssertEqual(match.handle, 1)
  }

  func testUnrelatedFrontmostChurnDoesNotReportFocusChanged() throws {
    // Frontmost moves between two other apps around the press; the target itself never
    // becomes frontmost, so this must not be reported as focus.changed.
    let performer = RecordingPerformer()
    let otherApp: pid_t = 700
    let frontmost = ScriptedFrontmost([bystander, otherApp])
    let deps = PressCoordinator.Dependencies(
      search: { [self.descriptor()] },
      frontmostPID: { frontmost.next() },
      performPress: performer.perform
    )
    let match = try PressCoordinator.run(targetPID: target, deps)
    XCTAssertEqual(match.handle, 1)
  }

  func testFailedPerformIsNeverReportedAsSuccess() {
    let performer = RecordingPerformer()
    performer.shouldSucceed = false
    let frontmost = ScriptedFrontmost([bystander, bystander])
    let deps = PressCoordinator.Dependencies(
      search: { [self.descriptor()] },
      frontmostPID: { frontmost.next() },
      performPress: performer.perform
    )
    XCTAssertThrowsError(try PressCoordinator.run(targetPID: target, deps))
    XCTAssertEqual(performer.invocations.count, 1)
  }
}
