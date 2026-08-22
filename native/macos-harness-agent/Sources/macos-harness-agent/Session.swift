import Darwin
import Foundation

/// Owns and serves exactly one already-connected `AF_UNIX` stream socket: the descriptor
/// `Main.swift` hands it from `--fd FD`, already validated as an open, connected `AF_UNIX`
/// `SOCK_STREAM` socket and marked close-on-exec. There is no listener and no `accept(2)` anywhere
/// in this package — the
/// parent process creates both ends of the connection itself with `socketpair(2)` before this
/// executable is even exec'd, and hands this process only its half. The descriptor's mere
/// existence already proves the peer is whoever the parent chose to hand it to, so there is no
/// UID/credential check left to perform the way an `accept(2)`-based server needs one.
///
/// Frames NDJSON in both directions with `LineFramer`, matching the transport `native.py`'s
/// `NativeClient` expects: one line in, one line out, no pipelining, with the same framing and size
/// cap on both ends. The connection is expected to stay open across many sequential requests for as
/// long as the client keeps it open — there is no per-request or per-connection idle timeout — and
/// ends only when the peer disconnects (`read` sees EOF, including the immediate EOF a crashed or
/// exited peer's own descriptor closure delivers), an oversized frame makes the connection
/// unrecoverable, a response fails to write, or this session's `SIGPIPE` guard cannot be armed.
/// Every one of those directly ends this process; `Main.swift` maps the `StopReason` `run()`
/// returns straight onto this process's own exit status.
///
/// `run()` executes entirely on its caller's thread. A single dedicated connection has no
/// listener to keep responsive and nothing else in this process ever competes for `fd`, so there
/// is no accept loop, no per-connection worker dispatch, and none of the locks or semaphores a
/// shared multi-connection daemon would otherwise need to coordinate them.
final class Session {

  private let fd: Int32
  private let executor: AXExecutor

  private static let readChunkSize = 64 * 1024

  /// `executor` defaults to the shared, process-wide `AXExecutor` exactly like `AgentHandlers`'
  /// own default; only a test constructing a `Session` around a fake in-process seam ever
  /// overrides it.
  init(fd: Int32, executor: AXExecutor = .shared) {
    self.fd = fd
    self.executor = executor
  }

  /// Why `run()` returned. `fd` is already closed by the time any case is produced —
  /// `Main.swift` never needs to close it again.
  enum StopReason: Equatable {
    /// `read(2)` reported EOF (a graceful peer close, including the peer's own process
    /// exiting) or a genuine read error — either way the peer is gone and there is nothing
    /// left to flush.
    case peerDisconnected
    /// Arming the per-socket `SIGPIPE` guard (`SO_NOSIGPIPE`) failed before this session ever
    /// read a request. Continuing would leave a later `write(2)` free to raise `SIGPIPE` and
    /// kill the whole process the moment the peer goes away mid-response, so the session ends
    /// immediately instead, before it has read or written anything.
    case socketSetupFailed
    /// A single incomplete line exceeded `maxLineBytes`; `LineFramer` had already discarded
    /// its buffer, so the framing was unrecoverable.
    case frameTooLarge
    /// Writing a response failed — most plausibly the peer went away mid-write.
    case writeFailed
  }

  /// Serves this connection to completion: reads, frames, dispatches, and answers requests
  /// sequentially until one of `StopReason`'s cases applies, then closes `fd` exactly once and
  /// returns. Never throws — every failure this loop can hit ends the session, never the
  /// process by itself; `Main.swift` decides the process's own fate from the reason returned.
  @discardableResult
  func run() -> StopReason {
    defer { Darwin.close(fd) }

    // Darwin-specific: a write() after the peer has already gone away raises SIGPIPE by
    // default, which would kill the whole process. Scope the fix to this one socket rather
    // than ignoring SIGPIPE process-wide. Required, not best-effort: a session that cannot
    // arm this guard ends immediately, before it has read or written anything, rather than
    // leaving a later write(2) free to raise SIGPIPE and kill the whole process.
    var disableSigPipe: Int32 = 1
    guard
      setsockopt(
        fd, SOL_SOCKET, SO_NOSIGPIPE, &disableSigPipe, socklen_t(MemoryLayout<Int32>.size)) == 0
    else {
      return .socketSetupFailed
    }

    let handlers = AgentHandlers(executor: executor)

    var framer = LineFramer()
    var buffer = [UInt8](repeating: 0, count: Self.readChunkSize)

    while true {
      let bytesRead = buffer.withUnsafeMutableBytes { rawBuffer -> Int in
        var result: Int
        repeat {
          result = Darwin.read(fd, rawBuffer.baseAddress, rawBuffer.count)
        } while result < 0 && errno == EINTR
        return result
      }
      if bytesRead <= 0 {
        return .peerDisconnected
      }

      let lines: [Data]
      do {
        lines = try framer.feed(Data(buffer[0..<bytesRead]))
      } catch {
        return .frameTooLarge
      }

      for line in lines {
        let response = process(line: line, using: handlers)
        guard let payload = Self.encodeResponse(response), writeAll(payload) else {
          return .writeFailed
        }
      }
    }
  }

  /// Decodes exactly one NDJSON frame and dispatches it. A structural decode failure never
  /// reaches `AgentHandlers` — `ProtocolCodec` has already turned it into a `bad_request`
  /// `WireResponse` — so both paths converge on the same response shape and the session
  /// survives either one.
  private func process(line: Data, using handlers: AgentHandlers) -> WireResponse {
    switch ProtocolCodec.decode(line) {
    case .success(let request):
      return handlers.handle(request)
    case .failure(let errorResponse):
      return errorResponse
    }
  }

  /// NDJSON-encodes one response: compact JSON plus the trailing newline `LineFramer` and
  /// `NativeClient` both frame on. A response whose encoded frame (including the newline) would
  /// exceed `maxLineBytes` — an oversized AX query result, most plausibly — is replaced with a
  /// small `ax.error` envelope instead of ever writing a frame the peer's own
  /// `LineFramer`-equivalent cannot read back. The same fallback also covers the (effectively
  /// unreachable in practice, since every field a `WireResponse` can carry is either a plain
  /// scalar or already-validated wire JSON) case where `response` itself fails to encode at all,
  /// rather than silently dropping the connection as a bare `nil` would.
  ///
  /// Static and internal rather than private so `SessionTests` can exercise the size guard
  /// directly, without a live socket.
  static func encodeResponse(_ response: WireResponse) -> Data? {
    if let payload = encodeFrame(response), payload.count <= maxLineBytes {
      return payload
    }
    return encodeFrame(
      WireResponse.failure(
        id: response.id,
        error: AgentError(
          code: "ax.error",
          message: "Response exceeded the \(maxLineBytes)-byte wire frame limit")))
  }

  private static func encodeFrame(_ response: WireResponse) -> Data? {
    guard var payload = try? JSONEncoder().encode(response) else { return nil }
    payload.append(UInt8(ascii: "\n"))
    return payload
  }

  /// Writes every byte of `data` to `fd`, looping across partial writes and `EINTR` the way a
  /// blocking stream socket sometimes demands. Returns `false` on any unrecoverable error
  /// (including a peer that has already gone away) instead of throwing.
  private func writeAll(_ data: Data) -> Bool {
    data.withUnsafeBytes { (rawBuffer: UnsafeRawBufferPointer) -> Bool in
      guard let base = rawBuffer.baseAddress else { return true }
      var remaining = rawBuffer.count
      var offset = 0
      while remaining > 0 {
        let written = Darwin.write(fd, base + offset, remaining)
        if written < 0 {
          if errno == EINTR { continue }
          return false
        }
        if written == 0 { return false }
        offset += written
        remaining -= written
      }
      return true
    }
  }
}
