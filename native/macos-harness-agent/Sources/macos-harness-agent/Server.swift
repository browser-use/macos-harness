import Darwin
import Dispatch
import Foundation

/// Hard cap on concurrently active accepted connections. `acceptOneConnection` reserves one
/// of this many slots before ever dispatching a connection to its own worker; a peer that
/// connects once every slot is taken is closed immediately instead of queued, so an unbounded
/// number of simultaneously open connections can never itself become a resource-exhaustion
/// vector independent of anything any one connection sends.
let maxActiveConnections = 32

/// The agent process's `AF_UNIX` NDJSON server: the concrete transport `Main.swift` drives end
/// to end and the only place in this package that touches a raw socket file descriptor.
///
/// One `Server` owns exactly one listening socket for the process's lifetime. Every accepted
/// connection gets its own `AgentHandlers` (and, through it, its own `ElementRegistry`) while
/// sharing the single process-wide `AXExecutor.shared`, so every AX call from every connection
/// still funnels onto one serial queue regardless of which client issued it — see the doc
/// comment on `AgentHandlers` for why that matters.
///
/// Mirrors the transport `native.py`'s `NativeClient` expects: one NDJSON line in, one line out,
/// no pipelining, with the same framing and size cap on both ends. `agent.py` stops the process
/// with `SIGTERM`, but the wire protocol also provides `shutdown` for other same-UID callers.
final class Server {

  // MARK: - Construction

  private let socketPath: String
  private let executor: AXExecutor

  init(socketPath: String) {
    self.socketPath = socketPath
    self.executor = .shared
  }

  // MARK: - Listener state
  //
  // `listenFD`/`wakeReadFD` are written once by `start()` before the accept-loop task is
  // handed to a queue, then touched only by that one task for the rest of their lifetime.
  // `wakeWriteFD` is written once by `start()` and closed by whichever call wins the `stop()`
  // idempotency race below. No raw descriptor field needs its own lock; only the lifecycle
  // flags genuinely get concurrent access, and those go through `lifecycleLock`.

  private var listenFD: Int32 = -1
  private var wakeReadFD: Int32 = -1
  private var wakeWriteFD: Int32 = -1

  private let lifecycleLock = NSLock()
  private var started = false
  private var isStopping = false
  private var shutdownRequested = false

  private let acceptLoopFinished = DispatchSemaphore(value: 0)
  private let runGate = DispatchSemaphore(value: 0)
  private let connectionLock = NSLock()
  private var activeConnections = 0

  private static let readChunkSize = 64 * 1024

  /// How long a worker's blocking `read(2)` on an accepted socket may wait for the peer to
  /// send anything before it gives up, matching `SO_RCVTIMEO` set on every accepted socket in
  /// `acceptOneConnection()`. Bounds a slow-loris-style connection that never completes a
  /// line; it does not bound how long a connection stays open once it keeps sending, however
  /// slowly, within this interval.
  private static let receiveTimeoutSeconds = 30

  // MARK: - Public lifecycle

  /// Creates the parent directory (mode 0700) and the listening socket (mode 0600), then hands
  /// the accept loop to a background queue and returns — `start()` never blocks waiting for a
  /// connection. Throws and leaves no partial state behind (every socket/file this call itself
  /// created is closed or removed again) on any failure.
  func start() throws {
    lifecycleLock.lock()
    defer { lifecycleLock.unlock() }

    guard !started else { throw StartFailure.alreadyStarted }
    guard !socketPath.isEmpty else {
      throw StartFailure.invalidSocketPath("socket path must not be empty")
    }

    try ensureParentDirectory(for: socketPath)
    try removeStaleSocketFileIfPresent(at: socketPath)

    let fd = socket(AF_UNIX, SOCK_STREAM, 0)
    guard fd >= 0 else { throw StartFailure.socketCreationFailed(errno: errno) }

    let existingFlags = fcntl(fd, F_GETFL, 0)
    guard existingFlags >= 0, fcntl(fd, F_SETFL, existingFlags | O_NONBLOCK) >= 0 else {
      let failure = StartFailure.socketCreationFailed(errno: errno)
      Darwin.close(fd)
      throw failure
    }

    let address: sockaddr_un
    do {
      address = try makeSocketAddress(path: socketPath)
    } catch {
      Darwin.close(fd)
      throw error
    }
    let bindResult = withUnsafePointer(to: address) { addressPointer -> Int32 in
      addressPointer.withMemoryRebound(to: sockaddr.self, capacity: 1) { sockaddrPointer in
        Darwin.bind(fd, sockaddrPointer, socklen_t(MemoryLayout<sockaddr_un>.size))
      }
    }
    guard bindResult == 0 else {
      let failure = StartFailure.bindFailed(errno: errno)
      Darwin.close(fd)
      throw failure
    }

    guard chmod(socketPath, 0o600) == 0 else {
      let failure = StartFailure.chmodFailed(errno: errno)
      Darwin.close(fd)
      unlink(socketPath)
      throw failure
    }

    guard listen(fd, SOMAXCONN) == 0 else {
      let failure = StartFailure.listenFailed(errno: errno)
      Darwin.close(fd)
      unlink(socketPath)
      throw failure
    }

    var pipeFDs: [Int32] = [-1, -1]
    guard pipe(&pipeFDs) == 0 else {
      let failure = StartFailure.pipeCreationFailed(errno: errno)
      Darwin.close(fd)
      unlink(socketPath)
      throw failure
    }

    listenFD = fd
    wakeReadFD = pipeFDs[0]
    wakeWriteFD = pipeFDs[1]
    started = true

    DispatchQueue.global(qos: .utility).async { [weak self] in
      self?.acceptLoop()
    }
  }

  /// Blocks the executable's main thread until `stop()` completes.
  func run() {
    runGate.wait()
  }

  /// Idempotent shutdown: safe to call from a signal-handling queue, from `Main.swift`'s normal
  /// control flow, or after an in-band `shutdown` response has been flushed. Only the first call
  /// does anything; every later call returns immediately.
  ///
  /// Stops accepting new connections, waits for the accept loop to exit, and removes the socket
  /// file. Live workers finish their current response cooperatively; a process-level signal then
  /// lets the process close any remaining descriptors during exit.
  func stop() {
    guard markStoppingIfNeeded() else { return }
    wakeAcceptLoop()
    acceptLoopFinished.wait()
    unlink(socketPath)
    runGate.signal()
  }

  // MARK: - Startup helpers

  /// Creates every missing path component up to the socket's parent directory at mode 0700,
  /// then (re)pins that mode even when the directory already existed — mirrors
  /// `_ensure_state_dir` in `agent.py`, which does the same unconditionally on every lifecycle
  /// call regardless of which process created the directory first.
  private func ensureParentDirectory(for socketPath: String) throws {
    let parent = (socketPath as NSString).deletingLastPathComponent
    guard !parent.isEmpty else { return }

    let fileManager = FileManager.default
    var isDirectory: ObjCBool = false
    let exists = fileManager.fileExists(atPath: parent, isDirectory: &isDirectory)
    if exists && !isDirectory.boolValue {
      throw StartFailure.invalidSocketPath(
        "parent of \(socketPath) exists and is not a directory: \(parent)")
    }
    if !exists {
      do {
        try fileManager.createDirectory(
          atPath: parent, withIntermediateDirectories: true,
          attributes: [.posixPermissions: 0o700])
      } catch {
        throw StartFailure.stateDirectoryFailed(path: parent, reason: "\(error)")
      }
    }
    do {
      try fileManager.setAttributes([.posixPermissions: 0o700], ofItemAtPath: parent)
    } catch {
      throw StartFailure.stateDirectoryFailed(path: parent, reason: "\(error)")
    }
  }

  /// Removes a pre-existing filesystem entry at `path` only when it is itself a UNIX-domain
  /// socket left behind by a killed agent — `bind()` fails with `EADDRINUSE` against any
  /// existing path, stale or not. Refuses to touch anything else (a regular file, directory,
  /// symlink, or device someone put at this path) since the socket path is caller-supplied
  /// configuration and never trusted blindly.
  private func removeStaleSocketFileIfPresent(at path: String) throws {
    var info = stat()
    guard lstat(path, &info) == 0 else {
      if errno == ENOENT { return }
      throw StartFailure.staleSocketNotRemovable(path: path, reason: posixErrorDescription())
    }

    let typeMask = mode_t(truncatingIfNeeded: S_IFMT)
    let expectedType = mode_t(truncatingIfNeeded: S_IFSOCK)
    guard info.st_mode & typeMask == expectedType else {
      throw StartFailure.staleSocketNotRemovable(
        path: path, reason: "existing entry is not a socket; refusing to remove it")
    }
    guard unlink(path) == 0 || errno == ENOENT else {
      throw StartFailure.staleSocketNotRemovable(path: path, reason: posixErrorDescription())
    }
  }

  /// Builds the `sockaddr_un` for `bind()`, rejecting any path too long to fit `sun_path` (104
  /// bytes on Darwin, including the terminating NUL) rather than silently truncating it — a
  /// truncated path would bind a different, wrong socket file with no indication to the caller.
  private func makeSocketAddress(path: String) throws -> sockaddr_un {
    let pathBytes = Array(path.utf8)
    var address = sockaddr_un()
    let capacity = MemoryLayout.size(ofValue: address.sun_path)
    guard pathBytes.count < capacity else {
      throw StartFailure.invalidSocketPath(
        "socket path is \(pathBytes.count) bytes, over the \(capacity - 1)-byte AF_UNIX limit: \(path)"
      )
    }

    address.sun_family = sa_family_t(truncatingIfNeeded: AF_UNIX)
    address.sun_len = UInt8(MemoryLayout<sockaddr_un>.size)
    withUnsafeMutableBytes(of: &address.sun_path) { rawBuffer in
      let buffer = rawBuffer.bindMemory(to: Int8.self)
      for index in 0..<capacity { buffer[index] = 0 }
      for (index, byte) in pathBytes.enumerated() { buffer[index] = Int8(bitPattern: byte) }
    }
    return address
  }

  // MARK: - Shutdown helpers

  /// Flips `isStopping` exactly once and reports whether *this* call was the one that flipped
  /// it — the single idempotency gate every other part of shutdown is built on.
  private func markStoppingIfNeeded() -> Bool {
    lifecycleLock.lock()
    defer { lifecycleLock.unlock() }
    guard started, !isStopping else { return false }
    isStopping = true
    return true
  }

  private func stoppingRequested() -> Bool {
    lifecycleLock.lock()
    defer { lifecycleLock.unlock() }
    return isStopping
  }
  private func requestShutdown() {
    lifecycleLock.lock()
    shutdownRequested = true
    lifecycleLock.unlock()
  }

  private func shutdownWasRequested() -> Bool {
    lifecycleLock.lock()
    defer { lifecycleLock.unlock() }
    return shutdownRequested
  }

  /// Wakes `acceptLoop()` out of its indefinite `poll()` by writing one byte to the private
  /// wake pipe. Always called after `isStopping` is already `true`, so the accept loop can
  /// treat "wake pipe readable" alone as sufficient reason to exit without re-checking the flag.
  private func wakeAcceptLoop() {
    guard wakeWriteFD >= 0 else { return }
    var wakeByte: UInt8 = 1
    _ = Darwin.write(wakeWriteFD, &wakeByte, 1)
    Darwin.close(wakeWriteFD)
    wakeWriteFD = -1
  }

  // MARK: - Accept loop

  /// Runs on its own background queue for the server's whole lifetime. Blocks in `poll()` on
  /// the listening socket and the private wake pipe simultaneously, so it costs nothing while
  /// idle yet reacts immediately to either a pending connection or `stop()` — never a fixed
  /// polling interval, and never a blocking `accept()` that a concurrent `close()` from another
  /// thread would have to interrupt racily. Only this task ever touches `listenFD`/`wakeReadFD`,
  /// including closing them, which is why no lock guards those two fields.
  private func acceptLoop() {
    defer {
      if listenFD >= 0 {
        Darwin.close(listenFD)
        listenFD = -1
      }
      if wakeReadFD >= 0 {
        Darwin.close(wakeReadFD)
        wakeReadFD = -1
      }
      acceptLoopFinished.signal()
    }

    let pollInFlag = Int16(truncatingIfNeeded: POLLIN)
    let fatalFlags = Int16(truncatingIfNeeded: POLLERR | POLLHUP | POLLNVAL)

    while true {
      var pollFDs = [
        pollfd(fd: listenFD, events: pollInFlag, revents: 0),
        pollfd(fd: wakeReadFD, events: pollInFlag, revents: 0),
      ]
      let ready = poll(&pollFDs, nfds_t(pollFDs.count), -1)
      if ready < 0 {
        if errno == EINTR { continue }
        log(op: "accept", id: -1, durationMs: 0, error: "poll")
        return
      }

      // stop() only ever writes to the wake pipe after it has already flipped
      // `isStopping`, so observing readability here is itself sufficient to exit.
      if pollFDs[1].revents & pollInFlag != 0 {
        return
      }
      if pollFDs[0].revents & fatalFlags != 0 {
        log(op: "accept", id: -1, durationMs: 0, error: "listener")
        return
      }
      if pollFDs[0].revents & pollInFlag != 0 {
        acceptOneConnection()
      }
    }
  }

  // MARK: - Connection admission control

  /// Reserves one of `maxActiveConnections` concurrent-connection slots without blocking.
  func tryAcquireConnectionSlot() -> Bool {
    connectionLock.lock()
    defer { connectionLock.unlock() }
    guard activeConnections < maxActiveConnections else { return false }
    activeConnections += 1
    return true
  }

  /// Releases one previously acquired connection slot.
  func releaseConnectionSlot() {
    connectionLock.lock()
    if activeConnections > 0 {
      activeConnections -= 1
    }
    connectionLock.unlock()
  }

  /// Accepts exactly one pending connection (if any remains after a spurious wakeup), rejects
  /// it immediately unless its peer's effective UID matches this process's own, and otherwise
  /// hands it to a fresh worker so the accept loop is never blocked by a connection's own I/O.
  private func acceptOneConnection() {
    var clientFD: Int32 = -1
    repeat {
      clientFD = Darwin.accept(listenFD, nil, nil)
    } while clientFD < 0 && errno == EINTR

    guard clientFD >= 0 else {
      if errno != EWOULDBLOCK && errno != EAGAIN {
        log(op: "accept", id: -1, durationMs: 0, error: "accept")
      }
      return
    }

    // The listening socket is nonblocking; accepted sockets inherit that flag on macOS.
    // Worker connections use blocking reads, so clear O_NONBLOCK before handing one off.
    let clientFlags = fcntl(clientFD, F_GETFL, 0)
    guard clientFlags >= 0, fcntl(clientFD, F_SETFL, clientFlags & ~O_NONBLOCK) >= 0 else {
      log(op: "accept", id: -1, durationMs: 0, error: "fcntl")
      Darwin.close(clientFD)
      return
    }

    // Darwin-specific: a write() after the peer has already gone away raises SIGPIPE by
    // default, which would kill the whole process. Scope the fix to this one socket instead
    // of globally ignoring SIGPIPE for the process.
    var disableSigPipe: Int32 = 1
    _ = setsockopt(
      clientFD, SOL_SOCKET, SO_NOSIGPIPE, &disableSigPipe, socklen_t(MemoryLayout<Int32>.size))

    // Bounds how long a worker's blocking read may wait on a connected but silent peer — see
    // `receiveTimeoutSeconds`. Unlike the SIGPIPE mitigation just above (whose failure only
    // reintroduces a pre-existing, unrelated risk), a failure here would leave this one
    // connection's worker able to block indefinitely — exactly the DoS this option exists to
    // prevent — so it is fatal to this connection rather than best-effort.
    var receiveTimeout = timeval(tv_sec: Self.receiveTimeoutSeconds, tv_usec: 0)
    guard
      setsockopt(
        clientFD, SOL_SOCKET, SO_RCVTIMEO, &receiveTimeout,
        socklen_t(MemoryLayout<timeval>.size)) == 0
    else {
      log(op: "accept", id: -1, durationMs: 0, error: "setsockopt")
      Darwin.close(clientFD)
      return
    }

    var peerUID: uid_t = 0
    var peerGID: gid_t = 0
    guard getpeereid(clientFD, &peerUID, &peerGID) == 0, peerUID == geteuid() else {
      log(op: "accept", id: -1, durationMs: 0, error: "permission.peer_uid")
      Darwin.close(clientFD)
      return
    }

    // Reject outright once `maxActiveConnections` workers are already live rather than queue
    // behind them: an unbounded number of concurrently open connections is itself a resource
    // exhaustion vector independent of anything any single connection ever sends.
    guard tryAcquireConnectionSlot() else {
      log(op: "accept", id: -1, durationMs: 0, error: "connection_limit")
      Darwin.close(clientFD)
      return
    }

    DispatchQueue.global(qos: .userInitiated).async { [weak self] in
      guard let self else {
        Darwin.close(clientFD)
        return
      }
      defer { self.releaseConnectionSlot() }
      self.serveConnection(clientFD)
    }
  }

  // MARK: - Connection handling

  /// Owns one accepted connection end to end: a fresh `AgentHandlers` (and, through it, a
  /// fresh `ElementRegistry`) against the shared `AXExecutor`, framed NDJSON in both
  /// directions, and sequential request/response pairs matching `NativeClient`'s "one line in,
  /// one line out, no pipelining" contract. A malformed request or an unsupported op yields a
  /// `bad_request`/`unsupported_op` response and keeps the connection open; an oversized frame
  /// is unrecoverable (`LineFramer` has already discarded its buffer) and ends the connection.
  /// Closes its own socket exactly once, from exactly this thread, on every exit path.
  private func serveConnection(_ fd: Int32) {
    defer { Darwin.close(fd) }

    let handlers = AgentHandlers(
      executor: executor,
      onShutdown: { [weak self] in self?.requestShutdown() })

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
        return  // EOF or read error: peer is gone, nothing left to flush
      }

      let lines: [Data]
      do {
        lines = try framer.feed(Data(buffer[0..<bytesRead]))
      } catch {
        return  // oversized frame; framer already discarded its buffer, unrecoverable
      }

      for line in lines {
        let response = process(line: line, using: handlers)
        guard let payload = encodeResponse(response), writeAll(payload, to: fd) else {
          return
        }
        if shutdownWasRequested() {
          stop()
          return
        }

      }

      // Every response already owed to this client has been flushed above; only now is
      // it safe for a shutdown noticed mid-loop to end the connection.
      if stoppingRequested() {
        return
      }
    }
  }

  /// Decodes exactly one NDJSON frame and dispatches it, timing the round trip for logging. A
  /// structural decode failure never reaches `AgentHandlers` — `ProtocolCodec` has already
  /// turned it into a `bad_request` `WireResponse` — so both paths converge on the same
  /// log/response shape and the connection survives either one.
  private func process(line: Data, using handlers: AgentHandlers) -> WireResponse {
    let start = DispatchTime.now()
    let op: String
    let response: WireResponse
    switch ProtocolCodec.decode(line) {
    case .success(let request):
      op = request.op
      response = handlers.handle(request)
    case .failure(let errorResponse):
      op = "decode_error"
      response = errorResponse
    }
    let durationMs =
      Double(DispatchTime.now().uptimeNanoseconds &- start.uptimeNanoseconds) / 1_000_000
    log(op: op, id: response.id, durationMs: durationMs, error: response.error?.code)
    return response
  }

  /// NDJSON-encodes one response: compact JSON plus the trailing newline `LineFramer` and
  /// `NativeClient` both frame on. A response whose encoded frame (including the newline)
  /// would exceed `maxLineBytes` — an oversized AX query result, most plausibly — is replaced
  /// with a small `ax.error` envelope instead of ever writing a frame the peer's own
  /// `LineFramer`-equivalent cannot read back. The same fallback also covers the
  /// (effectively unreachable in practice, since every field a `WireResponse` can carry is
  /// either a plain scalar or already-validated wire JSON) case where `response` itself fails
  /// to encode at all, rather than silently dropping the connection as a bare `nil` would.
  ///
  /// Internal rather than private so `ServerTests` can exercise the size guard directly,
  /// without a live socket.
  func encodeResponse(_ response: WireResponse) -> Data? {
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

  private func encodeFrame(_ response: WireResponse) -> Data? {
    guard var payload = try? JSONEncoder().encode(response) else { return nil }
    payload.append(UInt8(ascii: "\n"))
    return payload
  }

  /// Writes every byte of `data` to `fd`, looping across partial writes and `EINTR` the way a
  /// blocking stream socket sometimes demands. Returns `false` on any unrecoverable error
  /// (including a peer that has already gone away) instead of throwing — a failed write here
  /// just means "drop this connection," never a process-level failure.
  private func writeAll(_ data: Data, to fd: Int32) -> Bool {
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

  // MARK: - Logging

  /// The agent's entire logging surface: one concise line per request (`op`, `id`,
  /// `duration_ms`, and `error` when present) plus the same shape for accept-loop-level
  /// failures (`id: -1`). Written to stderr, which `agent.py`'s `_spawn_process` redirects into
  /// `agent.log` alongside stdout.
  private func log(op: String, id: Int, durationMs: Double, error: String?) {
    var line =
      "macos-harness-agent op=\(op) id=\(id) duration_ms=\(String(format: "%.2f", durationMs))"
    if let error {
      line += " error=\(error)"
    }
    line += "\n"
    FileHandle.standardError.write(Data(line.utf8))
  }

  private func posixErrorDescription(_ code: Int32 = errno) -> String {
    String(cString: strerror(code))
  }
}

// MARK: - Startup failures

extension Server {
  /// Every way `start()` can fail to stand up the listener. Always fatal to that one call —
  /// each throwing path in `start()` closes/removes whatever it had already created before
  /// rethrowing, so a caller never has to guess whether partial state was left behind.
  enum StartFailure: Error, CustomStringConvertible {
    case alreadyStarted
    case invalidSocketPath(String)
    case staleSocketNotRemovable(path: String, reason: String)
    case stateDirectoryFailed(path: String, reason: String)
    case socketCreationFailed(errno: Int32)
    case bindFailed(errno: Int32)
    case chmodFailed(errno: Int32)
    case listenFailed(errno: Int32)
    case pipeCreationFailed(errno: Int32)

    var description: String {
      switch self {
      case .alreadyStarted:
        return "server has already been started"
      case .invalidSocketPath(let detail):
        return "invalid socket path: \(detail)"
      case .staleSocketNotRemovable(let path, let reason):
        return "refusing to remove existing path \(path): \(reason)"
      case .stateDirectoryFailed(let path, let reason):
        return "could not prepare state directory \(path): \(reason)"
      case .socketCreationFailed(let code):
        return "socket setup failed: \(String(cString: strerror(code)))"
      case .bindFailed(let code):
        return "bind(2) failed: \(String(cString: strerror(code)))"
      case .chmodFailed(let code):
        return "chmod(2) failed: \(String(cString: strerror(code)))"
      case .listenFailed(let code):
        return "listen(2) failed: \(String(cString: strerror(code)))"
      case .pipeCreationFailed(let code):
        return "pipe(2) failed: \(String(cString: strerror(code)))"
      }
    }
  }
}
