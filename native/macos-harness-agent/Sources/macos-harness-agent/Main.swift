import Darwin
import Foundation

/// Executable entry point. Parses `--fd FD` — the single inherited, already-connected
/// `AF_UNIX` stream socket descriptor this process is launched with — validates it strictly,
/// and serves it to completion with one `Session`.
///
/// There is no listener and no `accept(2)` anywhere in this executable: the parent process creates
/// both ends of the connection itself with `socketpair(2)` before this executable is even exec'd,
/// and hands this process only its half. This process's entire job is to serve that one connection
/// until the peer disconnects, an oversized frame makes the connection unrecoverable, a response
/// fails to write, or the connection's `SIGPIPE` guard cannot be armed — every one of those
/// directly ends this process; see `Session.StopReason` and `exitCode(for:)`.
///
/// A `SIGTERM`/`SIGINT` is left at its default disposition (terminate the process) rather than
/// trapped: there is no listening socket file, pidfile, or other filesystem state left to
/// unwind first, so the OS's own termination is already a clean exit.
@main
struct Main {
  static func main() {
    guard let fd = parseFileDescriptor(CommandLine.arguments.dropFirst()) else {
      printUsage()
      exit(2)
    }
    guard isUsableSocketDescriptor(fd) else {
      FileHandle.standardError.write(
        Data("macos-harness-agent: fd \(fd) is not a usable AF_UNIX stream socket\n".utf8))
      exit(1)
    }
    guard markCloseOnExec(fd) else {
      FileHandle.standardError.write(
        Data("macos-harness-agent: could not mark fd \(fd) close-on-exec\n".utf8))
      exit(1)
    }

    exit(exitCode(for: Session(fd: fd).run()))
  }

  // MARK: - Argument parsing

  /// Accepts exactly one `--fd FD` pair, where `FD` is a base-10 integer literal (bare digits
  /// only — no sign, no fractional part, no surrounding whitespace) strictly greater than `2`:
  /// descriptors `0`–`2` are this process's own stdio and can never legitimately be the
  /// inherited session socket, so they are rejected the same as any other malformed value
  /// rather than silently accepted. Missing, unknown, or extra arguments are rejected too, so a
  /// misconfigured launch fails loudly instead of silently reading from the wrong descriptor.
  ///
  /// Every step here fails to `nil` instead of trapping: `Int32.init?(_:)` fails closed rather
  /// than crashing on a value too large to fit, so no adversarial or malformed `FD` argument can
  /// ever bring the process down before it even reaches a socket call. Internal rather than
  /// private so `MainTests` can exercise it directly.
  static func parseFileDescriptor<Arguments: Sequence>(
    _ arguments: Arguments
  ) -> Int32? where Arguments.Element == String {
    let args = Array(arguments)
    guard args.count == 2, args[0] == "--fd" else { return nil }

    let text = args[1]
    let isAllDigits =
      !text.isEmpty
      && text.utf8.allSatisfy { byte in (UInt8(ascii: "0")...UInt8(ascii: "9")).contains(byte) }
    guard isAllDigits, let value = Int32(text), value > 2 else { return nil }
    return value
  }

  /// True when `fd` is safe to adopt as this process's one session: an open descriptor
  /// (`F_GETFD`) that `fstat(2)` reports as an actual socket — never a regular file, pipe, or
  /// terminal passed by mistake — whose `SO_TYPE` is `SOCK_STREAM`, the only kind
  /// `LineFramer`'s byte-stream framing can make sense of, and whose own bound address and
  /// connected peer's address are both `AF_UNIX` (`getsockname(2)`/`getpeername(2)`). The
  /// `getpeername(2)` check does double duty: it also fails closed on a listening or otherwise
  /// never-connected socket, since only an actually connected socket has any peer to report.
  /// Every check fails closed on any syscall error instead of trapping. Internal rather than
  /// private so `MainTests` can exercise it directly, including every rejection path.
  static func isUsableSocketDescriptor(_ fd: Int32) -> Bool {
    guard fcntl(fd, F_GETFD, 0) >= 0 else { return false }

    var status = stat()
    guard fstat(fd, &status) == 0 else { return false }
    let typeMask = mode_t(truncatingIfNeeded: S_IFMT)
    let socketType = mode_t(truncatingIfNeeded: S_IFSOCK)
    guard status.st_mode & typeMask == socketType else { return false }

    var wireType: Int32 = 0
    var wireTypeLength = socklen_t(MemoryLayout<Int32>.size)
    guard getsockopt(fd, SOL_SOCKET, SO_TYPE, &wireType, &wireTypeLength) == 0 else {
      return false
    }
    guard wireType == SOCK_STREAM else { return false }

    guard addressFamily(of: fd, using: { getsockname(fd, $0, $1) }) == sa_family_t(AF_UNIX) else {
      return false
    }
    guard addressFamily(of: fd, using: { getpeername(fd, $0, $1) }) == sa_family_t(AF_UNIX) else {
      return false
    }
    return true
  }

  /// Reads a POSIX address family out of `fd` via `probe` — `getsockname(2)` for `fd`'s own
  /// bound address, `getpeername(2)` for its connected peer's — decoded from a
  /// `sockaddr_storage` large enough to hold any address family this process could ever be
  /// handed. Fails closed to `nil` on any syscall error, most notably the `ENOTCONN`
  /// `getpeername(2)` itself raises for a listening or never-connected socket: the caller
  /// above uses that to also require an actually connected peer, not merely the right socket
  /// type. Private: only `isUsableSocketDescriptor` needs this, so `MainTests` exercises it
  /// exclusively through that entry point.
  private static func addressFamily(
    of fd: Int32,
    using probe: (UnsafeMutablePointer<sockaddr>?, UnsafeMutablePointer<socklen_t>?) -> Int32
  ) -> sa_family_t? {
    var storage = sockaddr_storage()
    var length = socklen_t(MemoryLayout<sockaddr_storage>.size)
    let result = withUnsafeMutablePointer(to: &storage) { rawStorage -> Int32 in
      rawStorage.withMemoryRebound(to: sockaddr.self, capacity: 1) { sockaddrPointer in
        probe(sockaddrPointer, &length)
      }
    }
    guard result == 0 else { return nil }
    return storage.ss_family
  }

  /// Marks `fd` close-on-exec once this process has decided to adopt it: this process never
  /// spawns children of its own today, but a descriptor this sensitive should never be left
  /// inheritable by accident should that change. Internal rather than private so `MainTests`
  /// can verify the flag is actually set, not merely that this call reports success.
  static func markCloseOnExec(_ fd: Int32) -> Bool {
    fcntl(fd, F_SETFD, FD_CLOEXEC) >= 0
  }

  /// Maps how a session ended to this process's own exit status: a clean peer disconnect exits `0`;
  /// an oversized frame, a failed write, or a `SIGPIPE`-guard setup failure exits `1`, so whatever
  /// supervises this process — the parent that created the connection in the first place — can tell
  /// an ordinary close from one it should be surprised by. Internal rather than private so
  /// `MainTests` can verify the mapping without ever calling `exit(_:)` itself.
  static func exitCode(for reason: Session.StopReason) -> Int32 {
    switch reason {
    case .peerDisconnected:
      return 0
    case .socketSetupFailed, .frameTooLarge, .writeFailed:
      return 1
    }
  }

  private static func printUsage() {
    FileHandle.standardError.write(Data("usage: macos-harness-agent --fd FD\n".utf8))
  }
}
