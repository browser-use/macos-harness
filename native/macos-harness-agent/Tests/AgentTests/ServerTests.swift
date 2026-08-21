import XCTest

@testable import macos_harness_agent

// MARK: - Assumed production API (Server.swift, owned by the transport slice)
//
//   let maxActiveConnections: Int   // top-level, mirrors `maxLineBytes` in LineFramer.swift
//   final class Server {
//       init(socketPath: String)
//       func tryAcquireConnectionSlot() -> Bool
//       func releaseConnectionSlot()
//       func encodeResponse(_ response: WireResponse) -> Data?
//   }
//
// `tryAcquireConnectionSlot()`/`releaseConnectionSlot()` and `encodeResponse(_:)` are internal
// rather than private specifically so this file can exercise the connection-cap semaphore and
// the outbound response-size guard directly, without opening a real socket, dispatching a real
// accepted connection, or touching a live AX target. Constructing a bare `Server` has no side
// effects — only `start()` binds a socket or touches the filesystem — so every test below is a
// pure, deterministic, fast unit test.
//
// What this file does *not* cover: the few lines inside `acceptOneConnection()` that actually
// call `accept(2)`/`setsockopt(SO_RCVTIMEO)`/`Darwin.close` around these seams are exercised
// only by a live socket end to end, which is out of scope for a unit test target.

final class ServerTests: XCTestCase {

  private func freshServer() -> Server {
    Server(socketPath: "/tmp/macos-harness-agent-tests-unused.sock")
  }

  // MARK: - Connection admission control

  func testTryAcquireConnectionSlotSucceedsUpToTheCap() {
    let server = freshServer()
    var acquired = 0
    while server.tryAcquireConnectionSlot() {
      acquired += 1
      if acquired > maxActiveConnections {
        break  // guard against an infinite loop if the cap were somehow not enforced
      }
    }
    XCTAssertEqual(
      acquired, maxActiveConnections,
      "exactly maxActiveConnections slots must be acquirable before the cap engages")
  }

  func testTryAcquireConnectionSlotFailsOnceAtTheCap() {
    let server = freshServer()
    for _ in 0..<maxActiveConnections {
      XCTAssertTrue(server.tryAcquireConnectionSlot())
    }
    XCTAssertFalse(
      server.tryAcquireConnectionSlot(),
      "a connection beyond maxActiveConnections must be rejected, not queued")
  }

  func testReleaseConnectionSlotFreesCapacityForReuse() {
    let server = freshServer()
    for _ in 0..<maxActiveConnections {
      XCTAssertTrue(server.tryAcquireConnectionSlot())
    }
    XCTAssertFalse(server.tryAcquireConnectionSlot())

    server.releaseConnectionSlot()
    XCTAssertTrue(
      server.tryAcquireConnectionSlot(),
      "releasing one slot must free exactly one slot of capacity for the next connection")
    XCTAssertFalse(server.tryAcquireConnectionSlot())
  }

  func testReleaseConnectionSlotIsExactlyOnePermitPerCall() {
    let server = freshServer()
    for _ in 0..<maxActiveConnections {
      XCTAssertTrue(server.tryAcquireConnectionSlot())
    }
    server.releaseConnectionSlot()
    server.releaseConnectionSlot()
    XCTAssertTrue(server.tryAcquireConnectionSlot())
    XCTAssertTrue(server.tryAcquireConnectionSlot())
    XCTAssertFalse(
      server.tryAcquireConnectionSlot(), "two releases must free exactly two slots, not more")
  }

  // MARK: - Outbound response-size guard

  func testEncodeResponseKeepsAWellSizedSuccessResponseAsIs() throws {
    let server = freshServer()
    let response = WireResponse.success(id: 7, result: .object(["ok": .bool(true)]))
    let encoded = try XCTUnwrap(server.encodeResponse(response))
    let decoded = try JSONDecoder().decode(WireResponse.self, from: encoded)
    XCTAssertEqual(decoded.id, 7)
    XCTAssertTrue(decoded.ok)
  }

  func testEncodeResponseEndsWithExactlyOneTrailingNewline() throws {
    let server = freshServer()
    let response = WireResponse.success(id: 1, result: .object([:]))
    let encoded = try XCTUnwrap(server.encodeResponse(response))
    XCTAssertEqual(encoded.last, UInt8(ascii: "\n"))
    XCTAssertEqual(encoded.filter { $0 == UInt8(ascii: "\n") }.count, 1)
  }

  func testEncodeResponseReplacesOversizedSuccessWithBoundedAxError() throws {
    let server = freshServer()
    // A single JSON string field comfortably over `maxLineBytes` on its own guarantees the
    // encoded frame exceeds the cap regardless of whatever else the envelope carries.
    let oversizedText = String(repeating: "x", count: maxLineBytes + 4096)
    let response = WireResponse.success(
      id: 42, result: .object(["matches": .string(oversizedText)]))

    let encoded = try XCTUnwrap(server.encodeResponse(response))
    XCTAssertLessThanOrEqual(
      encoded.count, maxLineBytes, "the replacement envelope itself must respect maxLineBytes")

    let decoded = try JSONDecoder().decode(WireResponse.self, from: encoded)
    XCTAssertEqual(decoded.id, 42, "the replacement envelope must still echo the original id")
    XCTAssertFalse(decoded.ok)
    XCTAssertEqual(decoded.error?.code, "ax.error")
  }

  func testEncodeResponseReplacementNeverLeaksTheOversizedPayload() throws {
    let server = freshServer()
    let secretMarker = "super-secret-payload-marker"
    let oversizedText = secretMarker + String(repeating: "x", count: maxLineBytes + 4096)
    let response = WireResponse.success(
      id: 1, result: .object(["matches": .string(oversizedText)]))

    let encoded = try XCTUnwrap(server.encodeResponse(response))
    let text = String(decoding: encoded, as: UTF8.self)
    XCTAssertFalse(
      text.contains(secretMarker),
      "a replaced oversized response must not still contain fragments of the original payload")
  }
}
