import Foundation

/// Hard cap on a single NDJSON frame, measured on the complete line
/// including its trailing newline. Matches the Python client's `MAX_LINE_BYTES`
/// constant, so both ends enforce the same request and response boundary.
let maxLineBytes = 8 * 1024 * 1024

/// Incrementally splits arbitrary byte chunks — as read off a socket — into
/// complete newline-terminated NDJSON frames, buffering any trailing
/// partial line across calls.
///
/// Fails closed instead of growing without bound when a peer never sends a
/// newline. Once an incomplete line exceeds `maxLineBytes`, `feed` throws and
/// clears the buffer. The caller must close the connection because the framer
/// cannot resynchronize in the middle of an oversized line.
struct LineFramer {
  enum FramerError: Error, Equatable {
    case lineTooLong
  }

  private var buffer = Data()

  /// Appends `data` to the internal buffer and returns every complete
  /// line it can now extract, in order, with the trailing newline
  /// stripped. Returns an empty array when no line has completed yet.
  mutating func feed(_ data: Data) throws -> [Data] {
    buffer.append(data)

    var lines: [Data] = []
    while let newlineIndex = buffer.firstIndex(of: UInt8(ascii: "\n")) {
      let frameEnd = buffer.index(after: newlineIndex)
      let frameLength = buffer.distance(from: buffer.startIndex, to: frameEnd)
      if frameLength > maxLineBytes {
        buffer.removeAll()
        throw FramerError.lineTooLong
      }
      lines.append(Data(buffer[buffer.startIndex..<newlineIndex]))
      buffer.removeSubrange(buffer.startIndex..<frameEnd)
    }

    if buffer.count > maxLineBytes {
      buffer.removeAll()
      throw FramerError.lineTooLong
    }

    return lines
  }
}
