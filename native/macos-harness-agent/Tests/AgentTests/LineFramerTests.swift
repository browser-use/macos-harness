import XCTest

@testable import macos_harness_agent

// MARK: - Assumed production API (Protocol.swift / Server.swift, owned by the Foundation slice)
//
// A global `maxLineBytes` constant (8 * 1024 * 1024) bounds a single NDJSON frame, measured
// on the complete line including its trailing newline. `LineFramer` incrementally splits
// arbitrary byte chunks (as read off a socket) into complete newline-terminated frames,
// buffering partial lines across calls and failing closed instead of growing without bound
// when a peer never sends a newline.
//
//   struct LineFramer {
//       enum FramerError: Error, Equatable { case lineTooLong }
//       mutating func feed(_ data: Data) throws -> [Data]   // complete lines, newline stripped
//   }
//
// If the real type/method names differ, only this file needs to move during reconciliation.

final class LineFramerTests: XCTestCase {

  func testMaxLineBytesMatchesWireContract() {
    XCTAssertEqual(maxLineBytes, 8 * 1024 * 1024)
  }

  func testSingleChunkContainingOneLine() throws {
    var framer = LineFramer()
    let lines = try framer.feed(
      Data(#"{"v":1,"id":1,"op":"ping","params":{}}"#.utf8) + Data("\n".utf8))
    XCTAssertEqual(lines.count, 1)
    XCTAssertEqual(
      String(decoding: lines[0], as: UTF8.self), #"{"v":1,"id":1,"op":"ping","params":{}}"#)
  }

  func testLineSplitAcrossMultipleFeeds() throws {
    var framer = LineFramer()
    let first = try framer.feed(Data(#"{"v":1,"id":2,"#.utf8))
    XCTAssertTrue(first.isEmpty, "an incomplete line must not be surfaced yet")
    let second = try framer.feed(Data(#""op":"ping","params":{}}"#.utf8) + Data("\n".utf8))
    XCTAssertEqual(second.count, 1)
    XCTAssertEqual(
      String(decoding: second[0], as: UTF8.self), #"{"v":1,"id":2,"op":"ping","params":{}}"#)
  }

  func testMultipleLinesInOneChunk() throws {
    var framer = LineFramer()
    let payload = Data("line-one\n".utf8) + Data("line-two\n".utf8)
    let lines = try framer.feed(payload)
    XCTAssertEqual(lines.map { String(decoding: $0, as: UTF8.self) }, ["line-one", "line-two"])
  }

  func testTrailingPartialLineIsHeldUntilItsNewlineArrives() throws {
    var framer = LineFramer()
    let lines = try framer.feed(Data("complete\npartial-tail".utf8))
    XCTAssertEqual(lines.map { String(decoding: $0, as: UTF8.self) }, ["complete"])
    let rest = try framer.feed(Data("\n".utf8))
    XCTAssertEqual(rest.map { String(decoding: $0, as: UTF8.self) }, ["partial-tail"])
  }

  func testOversizedLineWithoutNewlineIsRejected() {
    var framer = LineFramer()
    let oversized = Data(repeating: 0x78, count: maxLineBytes + 1)  // no trailing newline
    XCTAssertThrowsError(try framer.feed(oversized)) { error in
      XCTAssertEqual(error as? LineFramer.FramerError, .lineTooLong)
    }
  }

  func testLineExactlyAtCapWithNewlineIsAccepted() throws {
    var framer = LineFramer()
    // maxLineBytes is measured on the complete line including the newline.
    let payloadBytes = maxLineBytes - 1
    let line = Data(repeating: 0x78, count: payloadBytes) + Data("\n".utf8)
    let lines = try framer.feed(line)
    XCTAssertEqual(lines.count, 1)
    XCTAssertEqual(lines[0].count, payloadBytes)
  }
}
