import Darwin
import Dispatch
import Foundation

/// Executable entry point. Parses `--socket PATH` (exactly what
/// `agent.py`'s `_spawn_process` invokes: `[binary, "--socket", str(socket)]`),
/// starts the shared `Server` on that path, and blocks until something
/// requests it to stop — a `SIGTERM`/`SIGINT` delivered to this process, or
/// (via the server's own wiring of `AgentHandlers`' `onShutdown`) a client's
/// `shutdown` op.
@main
struct Main {
  static func main() {
    guard let socketPath = parseSocketPath(CommandLine.arguments.dropFirst()) else {
      printUsage()
      exit(2)
    }

    let server = Server(socketPath: socketPath)
    installSignalHandlers(for: server)

    do {
      try server.start()
    } catch {
      FileHandle.standardError.write(
        Data("macos-harness-agent: failed to start on \(socketPath): \(error)\n".utf8))
      // `start()` may have partially bound before failing; `stop()` is
      // idempotent and safe to call whether or not it did.
      server.stop()
      exit(1)
    }

    // Whatever unwinds `run()` — a signal or a client-requested shutdown
    // — is expected to have already called `stop()` itself. This is a
    // safety net for any path that returns without having done so;
    // `stop()` tolerates being called more than once.
    defer { server.stop() }
    server.run()
  }

  // MARK: - Argument parsing

  /// Accepts exactly one `--socket PATH` pair. Missing, unknown, or extra
  /// arguments, and an empty path, are all rejected rather than guessed
  /// at, so a misconfigured launch fails loudly instead of silently
  /// binding somewhere unexpected.
  private static func parseSocketPath<Arguments: Sequence>(
    _ arguments: Arguments
  ) -> String? where Arguments.Element == String {
    let args = Array(arguments)
    guard args.count == 2, args[0] == "--socket", !args[1].isEmpty else { return nil }
    return args[1]
  }

  private static func printUsage() {
    FileHandle.standardError.write(Data("usage: macos-harness-agent --socket PATH\n".utf8))
  }

  // MARK: - Signal handling

  /// Dispatch sources for `SIGTERM`/`SIGINT`, kept alive for the process
  /// lifetime; letting them deinitialize would silently stop signal
  /// delivery.
  private static var signalSources: [DispatchSourceSignal] = []

  /// Routes `SIGTERM`/`SIGINT` to `server.stop()` on a dedicated dispatch
  /// queue rather than doing any work inside the actual signal-handler
  /// context. The default disposition is silenced first (`SIG_IGN`) so
  /// delivery reaches the dispatch sources instead of terminating the
  /// process before `stop()` — which unlinks the socket and closes every
  /// connection — has a chance to run.
  private static func installSignalHandlers(for server: Server) {
    signal(SIGTERM, SIG_IGN)
    signal(SIGINT, SIG_IGN)

    let signalQueue = DispatchQueue(label: "macos-harness-agent.signal")
    for sig in [SIGTERM, SIGINT] {
      let source = DispatchSource.makeSignalSource(signal: sig, queue: signalQueue)
      source.setEventHandler { server.stop() }
      source.resume()
      signalSources.append(source)
    }
  }
}
