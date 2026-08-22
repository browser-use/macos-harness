import Darwin
import Dispatch
import Foundation
import XCTest

@testable import macos_harness_agent

// MARK: - Assumed production API (Session.swift, owned by the transport slice)
//
//   final class Session {
//       init(fd: Int32, executor: AXExecutor = .shared)
//       enum StopReason: Equatable {
//           case peerDisconnected, socketSetupFailed, frameTooLarge, writeFailed
//       }
//       @discardableResult func run() -> StopReason
//       static func encodeResponse(_ response: WireResponse) -> Data?
//   }
//
// Every test that needs a live connection drives a real `Session` against one end of a real
// `socketpair(2)` — the same shape of descriptor `Main.swift` adopts on `--fd` — keeping the
// other end in this file as a minimal synchronous NDJSON test client. This is the only file in
// the suite that opens a live socket; response-size bounds that do not need one stay covered
// directly against `Session.encodeResponse`, without ever constructing a live connection.

final class SessionTests: XCTestCase {

  // MARK: - Fixture

  /// Two ends of one connected `AF_UNIX` stream with no filesystem entry — indistinguishable,
  /// from `Session`'s point of view, from the pair the real launcher creates with
  /// `socketpair(2)` before handing this executable its half on `--fd`.
  private func makeSocketPair() throws -> (parent: Int32, child: Int32) {
    var fds: [Int32] = [-1, -1]
    let result = socketpair(AF_UNIX, SOCK_STREAM, 0, &fds)
    try XCTSkipUnless(result == 0, "socketpair(2) unavailable in this environment")
    return (fds[0], fds[1])
  }

  /// Starts `session.run()` on a background queue and returns a closure that blocks — with a
  /// generous timeout, so a stuck session fails the test instead of hanging the suite — for the
  /// `StopReason` it eventually returns. `nil` means the timeout fired first.
  private func runInBackground(_ session: Session) -> () -> Session.StopReason? {
    let done = DispatchSemaphore(value: 0)
    var reason: Session.StopReason?
    DispatchQueue.global(qos: .userInitiated).async {
      reason = session.run()
      done.signal()
    }
    return {
      _ = done.wait(timeout: .now() + 5)
      return reason
    }
  }

  @discardableResult
  private func sendRaw(_ fd: Int32, _ data: Data) -> Bool {
    data.withUnsafeBytes { (rawBuffer: UnsafeRawBufferPointer) -> Bool in
      guard let base = rawBuffer.baseAddress else { return true }
      var remaining = rawBuffer.count
      var offset = 0
      while remaining > 0 {
        var written: Int
        repeat {
          written = Darwin.write(fd, base + offset, remaining)
        } while written < 0 && errno == EINTR
        if written <= 0 { return false }
        offset += written
        remaining -= written
      }
      return true
    }
  }

  @discardableResult
  private func sendLine(_ fd: Int32, _ text: String) -> Bool {
    sendRaw(fd, Data((text + "\n").utf8))
  }

  private func requestLine(id: Int, op: String, params: String = "{}") -> String {
    "{\"v\":1,\"id\":\(id),\"op\":\"\(op)\",\"params\":\(params)}"
  }

  /// Reads exactly one newline-terminated line one byte at a time — slower than chunked
  /// reading, but never at risk of reading past a line boundary and losing bytes a later call
  /// would need, which matters here because several tests deliberately have many responses
  /// already sitting in the kernel buffer by the time they start reading. Returns `nil` on EOF,
  /// a read error, or the bounded receive timeout firing.
  private func recvLine(_ fd: Int32, timeoutSeconds: Int = 5) -> String? {
    var timeout = timeval(tv_sec: timeoutSeconds, tv_usec: 0)
    _ = setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &timeout, socklen_t(MemoryLayout<timeval>.size))

    var line = [UInt8]()
    while true {
      var byte: UInt8 = 0
      var bytesRead: Int
      repeat {
        bytesRead = Darwin.read(fd, &byte, 1)
      } while bytesRead < 0 && errno == EINTR
      if bytesRead <= 0 { return nil }
      if byte == UInt8(ascii: "\n") {
        return String(decoding: line, as: UTF8.self)
      }
      line.append(byte)
    }
  }

  /// True when the next single byte read reports EOF — used to confirm a session actually
  /// closed its own end of the pair rather than merely finishing its background thread.
  private func expectEOF(_ fd: Int32, timeoutSeconds: Int = 5) -> Bool {
    var timeout = timeval(tv_sec: timeoutSeconds, tv_usec: 0)
    _ = setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &timeout, socklen_t(MemoryLayout<timeval>.size))
    var byte: UInt8 = 0
    var bytesRead: Int
    repeat {
      bytesRead = Darwin.read(fd, &byte, 1)
    } while bytesRead < 0 && errno == EINTR
    return bytesRead == 0
  }

  private func decodeResponse(_ data: Data) throws -> WireResponse {
    try JSONDecoder().decode(WireResponse.self, from: data)
  }

  /// Reads one response line and decodes it in a single call, failing the test with a clear
  /// message the instant either step comes up empty, rather than leaving every call site to
  /// unwrap `recvLine`'s result itself.
  private func recvResponse(_ fd: Int32, timeoutSeconds: Int = 5) throws -> WireResponse {
    let line = try XCTUnwrap(
      recvLine(fd, timeoutSeconds: timeoutSeconds), "no response line received")
    return try decodeResponse(Data(line.utf8))
  }

  private func numberField(_ result: JSONValue?, _ key: String) -> Double? {
    guard case .object(let fields)? = result, case .number(let value)? = fields[key] else {
      return nil
    }
    return value
  }

  // MARK: - PID-bearing ping

  func testPingReportsTheLiveChildProcessId() throws {
    let (parent, child) = try makeSocketPair()
    defer { Darwin.close(parent) }
    let waitForStop = runInBackground(Session(fd: child))

    sendLine(parent, requestLine(id: 1, op: "ping"))
    let response = try recvResponse(parent)
    XCTAssertTrue(response.ok)
    XCTAssertEqual(response.id, 1)
    XCTAssertEqual(
      numberField(response.result, "pid"), Double(ProcessInfo.processInfo.processIdentifier),
      "ping's pid field must be this very process's own pid — Session runs in-process on a "
        + "background queue in this test, so the two are the same process")

    Darwin.shutdown(parent, SHUT_WR)
    XCTAssertEqual(waitForStop(), .peerDisconnected)
  }

  // MARK: - EOF

  func testSessionStopsCleanlyOnPeerEOF() throws {
    let (parent, child) = try makeSocketPair()
    defer { Darwin.close(parent) }
    let waitForStop = runInBackground(Session(fd: child))

    sendLine(parent, requestLine(id: 1, op: "ping"))
    _ = try recvResponse(parent)

    Darwin.shutdown(parent, SHUT_WR)  // "no more requests" — the peer half of an EOF
    XCTAssertEqual(waitForStop(), .peerDisconnected)
    XCTAssertTrue(
      expectEOF(parent), "the session must close its own end of the pair once it stops")
  }

  // MARK: - Socket setup failure

  func testSessionEndsImmediatelyWhenTheSigpipeGuardCannotBeArmed() throws {
    var fds: [Int32] = [-1, -1]
    XCTAssertEqual(pipe(&fds), 0)
    let (readEnd, writeEnd) = (fds[0], fds[1])
    defer { Darwin.close(readEnd) }

    // A pipe write end is not a socket, so arming SO_NOSIGPIPE on it must fail — exercising
    // the fail-closed path any socket whose SO_NOSIGPIPE setup fails would take, without
    // needing to fabricate that failure on an actual socket. `Session.run()` itself closes
    // `writeEnd` via its own `defer`.
    XCTAssertEqual(Session(fd: writeEnd).run(), .socketSetupFailed)
  }

  // MARK: - Sequential requests

  func testSessionAnswersManyRequestsSequentiallyInOrder() throws {
    let (parent, child) = try makeSocketPair()
    defer { Darwin.close(parent) }
    let waitForStop = runInBackground(Session(fd: child))

    let requestCount = 25
    var batch = Data()
    for id in 1...requestCount {
      batch += Data((requestLine(id: id, op: "ping") + "\n").utf8)
    }
    sendRaw(parent, batch)  // every request written in one shot, before reading any response

    for expectedID in 1...requestCount {
      let response = try recvResponse(parent)
      XCTAssertTrue(response.ok)
      XCTAssertEqual(
        response.id, expectedID, "responses must come back strictly in request order")
    }

    Darwin.shutdown(parent, SHUT_WR)
    XCTAssertEqual(waitForStop(), .peerDisconnected)
  }

  func testUnsupportedOpDoesNotEndTheSessionAndLaterRequestsStillAnswer() throws {
    let (parent, child) = try makeSocketPair()
    defer { Darwin.close(parent) }
    let waitForStop = runInBackground(Session(fd: child))

    sendLine(parent, requestLine(id: 1, op: "definitely_not_a_real_op"))
    let bad = try recvResponse(parent)
    XCTAssertFalse(bad.ok)
    XCTAssertEqual(bad.error?.code, "unsupported_op")

    sendLine(parent, requestLine(id: 2, op: "ping"))
    let good = try recvResponse(parent)
    XCTAssertTrue(good.ok, "an unsupported op must not corrupt or end the session")
    XCTAssertEqual(good.id, 2)

    Darwin.shutdown(parent, SHUT_WR)
    XCTAssertEqual(waitForStop(), .peerDisconnected)
  }

  // MARK: - Malformed ids

  func testSessionSurvivesAMalformedIdAndAnswersTheNextRequest() throws {
    let (parent, child) = try makeSocketPair()
    defer { Darwin.close(parent) }
    let waitForStop = runInBackground(Session(fd: child))

    // An id too large to represent exactly as an Int; ProtocolCodec must recover id 0 rather
    // than trap or desync the connection — see ProtocolCodecTests for the unit-level regression
    // this mirrors end to end.
    sendLine(parent, #"{"id":1e300}"#)
    let badResponse = try recvResponse(parent)
    XCTAssertFalse(badResponse.ok)
    XCTAssertEqual(badResponse.error?.code, "bad_request")
    XCTAssertEqual(badResponse.id, 0, "an unrepresentable id must recover to 0, never trap")

    sendLine(parent, requestLine(id: 99, op: "ping"))
    let goodResponse = try recvResponse(parent)
    XCTAssertTrue(goodResponse.ok, "the session must keep answering after a malformed id")
    XCTAssertEqual(goodResponse.id, 99)

    Darwin.shutdown(parent, SHUT_WR)
    XCTAssertEqual(waitForStop(), .peerDisconnected)
  }

  // MARK: - Bounds

  func testOversizedFrameEndsTheSessionWithoutAResponse() throws {
    let (parent, child) = try makeSocketPair()
    defer { Darwin.close(parent) }
    let waitForStop = runInBackground(Session(fd: child))

    let oversized = Data(repeating: UInt8(ascii: "x"), count: maxLineBytes + 1)  // no newline
    sendRaw(parent, oversized)

    XCTAssertEqual(waitForStop(), .frameTooLarge)
    XCTAssertTrue(
      expectEOF(parent), "an oversized frame must end the connection, not just the one request")
  }

  // MARK: - Outbound response-size guard (no live socket needed)

  func testEncodeResponseKeepsAWellSizedSuccessResponseAsIs() throws {
    let response = WireResponse.success(id: 7, result: .object(["ok": .bool(true)]))
    let encoded = try XCTUnwrap(Session.encodeResponse(response))
    let decoded = try decodeResponse(encoded)
    XCTAssertEqual(decoded.id, 7)
    XCTAssertTrue(decoded.ok)
  }

  func testEncodeResponseEndsWithExactlyOneTrailingNewline() throws {
    let response = WireResponse.success(id: 1, result: .object([:]))
    let encoded = try XCTUnwrap(Session.encodeResponse(response))
    XCTAssertEqual(encoded.last, UInt8(ascii: "\n"))
    XCTAssertEqual(encoded.filter { $0 == UInt8(ascii: "\n") }.count, 1)
  }

  func testEncodeResponseReplacesOversizedSuccessWithBoundedAxError() throws {
    // A single JSON string field comfortably over `maxLineBytes` on its own guarantees the
    // encoded frame exceeds the cap regardless of whatever else the envelope carries.
    let oversizedText = String(repeating: "x", count: maxLineBytes + 4096)
    let response = WireResponse.success(
      id: 42, result: .object(["matches": .string(oversizedText)]))

    let encoded = try XCTUnwrap(Session.encodeResponse(response))
    XCTAssertLessThanOrEqual(
      encoded.count, maxLineBytes, "the replacement envelope itself must respect maxLineBytes")

    let decoded = try decodeResponse(encoded)
    XCTAssertEqual(decoded.id, 42, "the replacement envelope must still echo the original id")
    XCTAssertFalse(decoded.ok)
    XCTAssertEqual(decoded.error?.code, "ax.error")
  }

  func testEncodeResponseReplacementNeverLeaksTheOversizedPayload() throws {
    let secretMarker = "super-secret-payload-marker"
    let oversizedText = secretMarker + String(repeating: "x", count: maxLineBytes + 4096)
    let response = WireResponse.success(
      id: 1, result: .object(["matches": .string(oversizedText)]))

    let encoded = try XCTUnwrap(Session.encodeResponse(response))
    let text = String(decoding: encoded, as: UTF8.self)
    XCTAssertFalse(
      text.contains(secretMarker),
      "a replaced oversized response must not still contain fragments of the original payload")
  }
}
