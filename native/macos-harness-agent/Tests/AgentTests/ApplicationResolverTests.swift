import XCTest

@testable import macos_harness_agent

// MARK: - Assumed production API (app selector resolution, mirrors macos_harness.macos.MacOS._resolve_app)
//
//   struct AppInfo { let name: String; let bundleID: String?; let pid: pid_t; let path: String? }
//   internal enum AppResolver {
//       static func resolve(query: String, candidates: [AppInfo]) throws -> AppInfo
//   }
//   internal enum AgentError: Error {
//       var code: String { get }      // wire error code, e.g. "app.not_found" / "app.ambiguous"
//       var message: String { get }
//   }
//
// (Reported by NativeAX mid-implementation; if the merged names differ — e.g. `ApplicationResolver`
// instead of `AppResolver`, or a `Result`-returning signature instead of `throws` — only the call
// sites in this file need to move. The ranking rule under test is unambiguous either way.)
//
// Ranking mirrors the Python client 1:1 (src/macos_harness/macos.py `_resolve_app`): an exact
// case-insensitive match against pid/name/bundle id/path beats any merely-substring match,
// ambiguity names are capped at eight, and no candidates at all is app.not_found.

final class ApplicationResolverTests: XCTestCase {

  private func candidate(_ name: String, pid: pid_t, bundleID: String? = nil, path: String? = nil)
    -> AppInfo
  {
    AppInfo(name: name, bundleID: bundleID, pid: pid, path: path)
  }

  private func assertAgentErrorCode(
    _ expression: @autoclosure () throws -> AppInfo,
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

  func testExactNameMatchWinsOverSubstringCandidate() throws {
    let exact = candidate("Safari", pid: 100, bundleID: "com.apple.Safari")
    let substringOnly = candidate(
      "Safari Technology Preview", pid: 200, bundleID: "com.apple.SafariTechnologyPreview")
    let match = try AppResolver.resolve(query: "Safari", candidates: [substringOnly, exact])
    XCTAssertEqual(match.pid, 100)
  }

  func testResolutionIsCaseInsensitive() throws {
    let app = candidate("Notes", pid: 42)
    let match = try AppResolver.resolve(query: "nOtEs", candidates: [app])
    XCTAssertEqual(match.pid, 42)
  }

  func testMatchByPidString() throws {
    let app = candidate("Finder", pid: 777)
    let other = candidate("Notes", pid: 42)
    let match = try AppResolver.resolve(query: "777", candidates: [other, app])
    XCTAssertEqual(match.pid, 777)
  }

  func testMatchByBundleIdentifier() throws {
    let app = candidate("Safari", pid: 100, bundleID: "com.apple.Safari")
    let match = try AppResolver.resolve(query: "com.apple.Safari", candidates: [app])
    XCTAssertEqual(match.pid, 100)
  }

  func testMatchByPath() throws {
    let app = candidate("Notes", pid: 42, path: "/System/Applications/Notes.app")
    let match = try AppResolver.resolve(query: "/System/Applications/Notes.app", candidates: [app])
    XCTAssertEqual(match.pid, 42)
  }

  func testSubstringFallbackWhenNoExactMatchExists() throws {
    let app = candidate("Safari Technology Preview", pid: 200)
    let match = try AppResolver.resolve(query: "technology", candidates: [app])
    XCTAssertEqual(match.pid, 200)
  }

  func testNoCandidatesReportsAppNotFound() {
    assertAgentErrorCode(
      try AppResolver.resolve(query: "AnythingAtAll", candidates: []), equals: "app.not_found")
  }

  func testNoMatchingCandidatesReportsAppNotFound() {
    let apps = [candidate("Notes", pid: 1), candidate("Finder", pid: 2)]
    assertAgentErrorCode(
      try AppResolver.resolve(query: "Xcode", candidates: apps), equals: "app.not_found")
  }

  func testAmbiguousExactMatchesReportAppAmbiguous() {
    let apps = [candidate("Notes", pid: 1), candidate("Notes", pid: 2)]
    assertAgentErrorCode(
      try AppResolver.resolve(query: "Notes", candidates: apps), equals: "app.ambiguous")
  }

  func testAmbiguousMessageListsNamesCappedAtEight() {
    let apps = (1...10).map { candidate("Notes", pid: pid_t($0)) }
    XCTAssertThrowsError(try AppResolver.resolve(query: "Notes", candidates: apps)) { error in
      guard let agentError = error as? AgentError else {
        return XCTFail("expected an AgentError, got \(error)")
      }
      XCTAssertEqual(agentError.code, "app.ambiguous")
      let listedNames = agentError.message.components(separatedBy: ", ")
      XCTAssertLessThanOrEqual(
        listedNames.count, 8, "ambiguity report must cap the candidate list at eight")
    }
  }

  func testExactBucketWinsEvenWhenLargerSubstringBucketExists() throws {
    // Python parity: `matches = exact or candidates` — a single exact hit is never
    // outvoted by a larger pool of merely-substring candidates.
    let exact = candidate("Notes", pid: 1)
    let substringOnly = (2...5).map { candidate("Notes App \($0)", pid: pid_t($0)) }
    let match = try AppResolver.resolve(query: "Notes", candidates: [exact] + substringOnly)
    XCTAssertEqual(match.pid, 1)
  }

  func testAmbiguousSubstringMatchesWhenNoExactMatchExists() {
    let apps = [
      candidate("Safari Technology Preview", pid: 1),
      candidate("Old Safari", pid: 2),
    ]
    assertAgentErrorCode(
      try AppResolver.resolve(query: "safari", candidates: apps), equals: "app.ambiguous")
  }
}
