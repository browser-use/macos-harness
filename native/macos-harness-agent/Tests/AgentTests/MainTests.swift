import Darwin
import Foundation
import XCTest

@testable import macos_harness_agent

// MARK: - Assumed production API (Main.swift, owned by the transport slice)
//
//   struct Main {
//       static func parseFileDescriptor<Arguments: Sequence>(_ arguments: Arguments) -> Int32?
//           where Arguments.Element == String
//       static func isUsableSocketDescriptor(_ fd: Int32) -> Bool
//       static func markCloseOnExec(_ fd: Int32) -> Bool
//       static func exitCode(for reason: Session.StopReason) -> Int32
//   }
//
// `main()` itself calls `exit(_:)` on every path and can never run inside this test process;
// everything it depends on to decide *what* to do is instead factored into these four pure(ish)
// static functions, which is what this file actually exercises.

final class MainTests: XCTestCase {

  // MARK: - parseFileDescriptor

  func testParsesAnOrdinaryFileDescriptor() {
    XCTAssertEqual(Main.parseFileDescriptor(["--fd", "5"]), 5)
  }

  func testAcceptsTheSmallestValidFileDescriptor() {
    // 0/1/2 are this process's own stdio; 3 is the first descriptor that could plausibly be
    // an inherited session socket.
    XCTAssertEqual(Main.parseFileDescriptor(["--fd", "3"]), 3)
  }

  func testAcceptsTheLargestInt32Value() {
    XCTAssertEqual(Main.parseFileDescriptor(["--fd", "2147483647"]), 2_147_483_647)
  }

  func testRejectsFileDescriptorZero() {
    XCTAssertNil(Main.parseFileDescriptor(["--fd", "0"]), "0 is stdin, never a session socket")
  }

  func testRejectsFileDescriptorOne() {
    XCTAssertNil(Main.parseFileDescriptor(["--fd", "1"]), "1 is stdout, never a session socket")
  }

  func testRejectsFileDescriptorTwo() {
    XCTAssertNil(Main.parseFileDescriptor(["--fd", "2"]), "2 is stderr, never a session socket")
  }

  func testRejectsNoArguments() {
    XCTAssertNil(Main.parseFileDescriptor([]))
  }

  func testRejectsAWrongFlagName() {
    XCTAssertNil(Main.parseFileDescriptor(["--socket", "5"]), "no --socket compatibility flag")
  }

  func testRejectsAMissingValue() {
    XCTAssertNil(Main.parseFileDescriptor(["--fd"]))
  }

  func testRejectsTrailingExtraArguments() {
    XCTAssertNil(Main.parseFileDescriptor(["--fd", "5", "extra"]))
  }

  func testRejectsANegativeValue() {
    XCTAssertNil(Main.parseFileDescriptor(["--fd", "-1"]))
  }

  func testRejectsAPlusPrefixedValue() {
    XCTAssertNil(Main.parseFileDescriptor(["--fd", "+5"]))
  }

  func testRejectsNonNumericText() {
    XCTAssertNil(Main.parseFileDescriptor(["--fd", "abc"]))
  }

  func testRejectsAFloatingPointValue() {
    XCTAssertNil(Main.parseFileDescriptor(["--fd", "5.5"]))
  }

  func testRejectsAnEmptyValue() {
    XCTAssertNil(Main.parseFileDescriptor(["--fd", ""]))
  }

  func testRejectsWhitespaceInTheValue() {
    XCTAssertNil(Main.parseFileDescriptor(["--fd", " 5"]))
    XCTAssertNil(Main.parseFileDescriptor(["--fd", "5 "]))
  }

  func testRejectsAValueThatOverflowsInt32WithoutCrashing() {
    // The historical failure mode this guards: a naive `Int32(string)!` would trap on an
    // out-of-range literal instead of reporting a normal parse failure.
    XCTAssertNil(Main.parseFileDescriptor(["--fd", "99999999999999999999"]))
  }

  // MARK: - isUsableSocketDescriptor

  private func openSocketPair(type: Int32 = SOCK_STREAM) -> (Int32, Int32)? {
    var fds: [Int32] = [-1, -1]
    guard socketpair(AF_UNIX, type, 0, &fds) == 0 else { return nil }
    return (fds[0], fds[1])
  }

  func testIsUsableSocketDescriptorIsTrueForAConnectedStreamSocket() throws {
    let pair = try XCTUnwrap(openSocketPair())
    defer {
      Darwin.close(pair.0)
      Darwin.close(pair.1)
    }
    XCTAssertTrue(Main.isUsableSocketDescriptor(pair.0))
    XCTAssertTrue(Main.isUsableSocketDescriptor(pair.1))
  }

  func testIsUsableSocketDescriptorIsFalseForAClosedDescriptor() throws {
    let pair = try XCTUnwrap(openSocketPair())
    Darwin.close(pair.0)
    Darwin.close(pair.1)
    XCTAssertFalse(Main.isUsableSocketDescriptor(pair.0))
    XCTAssertFalse(Main.isUsableSocketDescriptor(pair.1))
  }

  func testIsUsableSocketDescriptorIsFalseForANegativeDescriptor() {
    XCTAssertFalse(Main.isUsableSocketDescriptor(-1))
  }

  func testIsUsableSocketDescriptorIsFalseForADatagramSocket() throws {
    // Rejects a real, open, connected socket that simply is not SOCK_STREAM — proving the
    // SO_TYPE check, not just "is it a socket at all", actually gates admission.
    let pair = try XCTUnwrap(openSocketPair(type: SOCK_DGRAM))
    defer {
      Darwin.close(pair.0)
      Darwin.close(pair.1)
    }
    XCTAssertFalse(Main.isUsableSocketDescriptor(pair.0))
  }

  func testIsUsableSocketDescriptorIsFalseForARegularFile() throws {
    let path = NSTemporaryDirectory() + "macos-harness-agent-tests-\(UUID().uuidString)"
    XCTAssertTrue(FileManager.default.createFile(atPath: path, contents: nil))
    defer { try? FileManager.default.removeItem(atPath: path) }

    let handle = try XCTUnwrap(FileHandle(forReadingAtPath: path))
    defer { handle.closeFile() }

    XCTAssertFalse(
      Main.isUsableSocketDescriptor(handle.fileDescriptor),
      "a regular file must never pass as a session socket")
  }

  func testIsUsableSocketDescriptorIsFalseForAPipe() throws {
    var fds: [Int32] = [-1, -1]
    XCTAssertEqual(pipe(&fds), 0)
    defer {
      Darwin.close(fds[0])
      Darwin.close(fds[1])
    }
    XCTAssertFalse(Main.isUsableSocketDescriptor(fds[0]))
    XCTAssertFalse(Main.isUsableSocketDescriptor(fds[1]))
  }

  func testIsUsableSocketDescriptorIsFalseForAnUnconnectedUnixStreamSocket() throws {
    let fd = Darwin.socket(AF_UNIX, SOCK_STREAM, 0)
    try XCTSkipUnless(fd >= 0, "socket(2) unavailable in this environment")
    defer { Darwin.close(fd) }
    XCTAssertFalse(
      Main.isUsableSocketDescriptor(fd),
      "an AF_UNIX stream socket that was never connected must never pass as a session socket")
  }

  /// A loopback TCP listener plus one accepted connection through it — used to prove
  /// `isUsableSocketDescriptor` rejects every TCP shape, connected or not, purely on address
  /// family, the same way it already rejects every non-`SOCK_STREAM` shape on wire type.
  private func openConnectedTCPPair() throws -> (
    client: Int32, accepted: Int32, listener: Int32
  ) {
    let listener = Darwin.socket(AF_INET, SOCK_STREAM, 0)
    try XCTSkipUnless(listener >= 0, "socket(2) unavailable in this environment")

    var bindAddress = sockaddr_in()
    bindAddress.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
    bindAddress.sin_family = sa_family_t(AF_INET)
    bindAddress.sin_port = 0
    bindAddress.sin_addr.s_addr = in_addr_t(0x7f00_0001).bigEndian  // 127.0.0.1
    let bindResult = withUnsafePointer(to: &bindAddress) { rawAddress -> Int32 in
      rawAddress.withMemoryRebound(to: sockaddr.self, capacity: 1) { sockaddrPointer in
        Darwin.bind(listener, sockaddrPointer, socklen_t(MemoryLayout<sockaddr_in>.size))
      }
    }
    try XCTSkipUnless(bindResult == 0, "bind(2) to loopback unavailable in this environment")
    try XCTSkipUnless(Darwin.listen(listener, 1) == 0, "listen(2) unavailable in this environment")

    var boundAddress = sockaddr_in()
    var boundLength = socklen_t(MemoryLayout<sockaddr_in>.size)
    let nameResult = withUnsafeMutablePointer(to: &boundAddress) { rawAddress -> Int32 in
      rawAddress.withMemoryRebound(to: sockaddr.self, capacity: 1) { sockaddrPointer in
        Darwin.getsockname(listener, sockaddrPointer, &boundLength)
      }
    }
    try XCTSkipUnless(nameResult == 0, "getsockname(2) unavailable in this environment")

    let client = Darwin.socket(AF_INET, SOCK_STREAM, 0)
    try XCTSkipUnless(client >= 0, "socket(2) unavailable in this environment")
    let connectResult = withUnsafePointer(to: &boundAddress) { rawAddress -> Int32 in
      rawAddress.withMemoryRebound(to: sockaddr.self, capacity: 1) { sockaddrPointer in
        Darwin.connect(client, sockaddrPointer, socklen_t(MemoryLayout<sockaddr_in>.size))
      }
    }
    try XCTSkipUnless(connectResult == 0, "connect(2) to loopback unavailable in this environment")

    let accepted = Darwin.accept(listener, nil, nil)
    try XCTSkipUnless(accepted >= 0, "accept(2) unavailable in this environment")

    return (client, accepted, listener)
  }

  func testIsUsableSocketDescriptorIsFalseForATCPListener() throws {
    let pair = try openConnectedTCPPair()
    defer {
      Darwin.close(pair.client)
      Darwin.close(pair.accepted)
      Darwin.close(pair.listener)
    }
    XCTAssertFalse(
      Main.isUsableSocketDescriptor(pair.listener),
      "a listening TCP socket must never pass as a session socket")
  }

  func testIsUsableSocketDescriptorIsFalseForAConnectedTCPSocket() throws {
    let pair = try openConnectedTCPPair()
    defer {
      Darwin.close(pair.client)
      Darwin.close(pair.accepted)
      Darwin.close(pair.listener)
    }
    XCTAssertFalse(
      Main.isUsableSocketDescriptor(pair.client),
      "a connected TCP socket must never pass as a session socket, even though it is "
        + "SOCK_STREAM and has a connected peer")
    XCTAssertFalse(
      Main.isUsableSocketDescriptor(pair.accepted),
      "an accepted TCP connection must never pass as a session socket, even though it is "
        + "SOCK_STREAM and has a connected peer")
  }

  // MARK: - markCloseOnExec

  func testMarkCloseOnExecActuallySetsTheFlag() throws {
    let pair = try XCTUnwrap(openSocketPair())
    defer {
      Darwin.close(pair.0)
      Darwin.close(pair.1)
    }
    XCTAssertEqual(
      fcntl(pair.0, F_GETFD, 0) & FD_CLOEXEC, 0,
      "a freshly created socketpair descriptor must not start close-on-exec")

    XCTAssertTrue(Main.markCloseOnExec(pair.0))
    XCTAssertEqual(fcntl(pair.0, F_GETFD, 0) & FD_CLOEXEC, FD_CLOEXEC)
  }

  func testMarkCloseOnExecFailsClosedForAClosedDescriptor() throws {
    let pair = try XCTUnwrap(openSocketPair())
    Darwin.close(pair.0)
    Darwin.close(pair.1)
    XCTAssertFalse(Main.markCloseOnExec(pair.0))
  }

  // MARK: - exitCode(for:)

  func testExitCodeIsZeroForACleanPeerDisconnect() {
    XCTAssertEqual(Main.exitCode(for: .peerDisconnected), 0)
  }

  func testExitCodeIsNonZeroForProtocolAndTransportFailures() {
    XCTAssertEqual(Main.exitCode(for: .socketSetupFailed), 1)
    XCTAssertEqual(Main.exitCode(for: .frameTooLarge), 1)
    XCTAssertEqual(Main.exitCode(for: .writeFailed), 1)
  }
}
