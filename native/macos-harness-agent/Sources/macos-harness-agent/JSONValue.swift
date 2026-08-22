import Foundation

/// A JSON value as it appears anywhere on the agent wire protocol: request
/// `params`, response `result`, nested AX attribute values, and element
/// descriptor fields. Mirrors the shape `MacOS._jsonable` produces on the
/// Python side (`src/macos_harness/macos.py`) — plain scalars, arrays, and
/// string-keyed objects — so AX attribute values round-trip identically
/// across both backends.
enum JSONValue: Equatable {
  case null
  case bool(Bool)
  case number(Double)
  case string(String)
  case array([JSONValue])
  case object([String: JSONValue])
}

// MARK: - Codable

extension JSONValue: Codable {
  /// Tries each wire shape in turn. Order matters only for `Bool` vs.
  /// `Double`: attempting `Bool` first means a JSON `true`/`false` never
  /// gets misread as `1.0`/`0.0`, and a JSON number never gets misread as
  /// a boolean, because `JSONDecoder` enforces the underlying JSON token
  /// type for both and rejects the mismatched case.
  init(from decoder: Decoder) throws {
    let container = try decoder.singleValueContainer()
    if container.decodeNil() {
      self = .null
    } else if let value = try? container.decode(Bool.self) {
      self = .bool(value)
    } else if let value = try? container.decode(Double.self) {
      self = .number(value)
    } else if let value = try? container.decode(String.self) {
      self = .string(value)
    } else if let value = try? container.decode([JSONValue].self) {
      self = .array(value)
    } else if let value = try? container.decode([String: JSONValue].self) {
      self = .object(value)
    } else {
      throw DecodingError.dataCorruptedError(
        in: container, debugDescription: "Unsupported or malformed JSON value")
    }
  }

  func encode(to encoder: Encoder) throws {
    var container = encoder.singleValueContainer()
    switch self {
    case .null:
      try container.encodeNil()
    case .bool(let value):
      try container.encode(value)
    case .number(let value):
      try container.encode(value)
    case .string(let value):
      try container.encode(value)
    case .array(let value):
      try container.encode(value)
    case .object(let value):
      try container.encode(value)
    }
  }
}

// MARK: - Safe conversion helpers

extension JSONValue {
  var isNull: Bool {
    if case .null = self { return true }
    return false
  }

  var boolValue: Bool? {
    if case .bool(let value) = self { return value }
    return nil
  }

  var numberValue: Double? {
    if case .number(let value) = self { return value }
    return nil
  }

  var stringValue: String? {
    if case .string(let value) = self { return value }
    return nil
  }

  var arrayValue: [JSONValue]? {
    if case .array(let value) = self { return value }
    return nil
  }

  var objectValue: [String: JSONValue]? {
    if case .object(let value) = self { return value }
    return nil
  }

  /// `nil` when this value is not `.object` or the key is absent —
  /// callers never need to unwrap `.object` themselves just to look up a
  /// field.
  subscript(key: String) -> JSONValue? {
    objectValue?[key]
  }

  /// `nil` when this value is not `.array` or the index is out of range.
  subscript(index: Int) -> JSONValue? {
    guard let array = arrayValue, array.indices.contains(index) else { return nil }
    return array[index]
  }

  /// The nearest plain Foundation representation of this value (`NSNull`,
  /// `Bool`, `Double`, `String`, `[Any]`, `[String: Any]`), for handing to
  /// Foundation JSON APIs or as a starting point for bridging toward an AX
  /// setter that expects a plain value. One-directional by design: the
  /// reverse conversion (`Any` -> `JSONValue`) is ambiguous for
  /// `NSNumber`-boxed booleans, so callers producing a `JSONValue` from an
  /// AX-derived `Any` should construct the matching case directly instead.
  var foundationValue: Any {
    switch self {
    case .null:
      return NSNull()
    case .bool(let value):
      return value
    case .number(let value):
      return value
    case .string(let value):
      return value
    case .array(let value):
      return value.map { $0.foundationValue }
    case .object(let value):
      return value.mapValues { $0.foundationValue }
    }
  }
}

// MARK: - Literal construction

extension JSONValue: ExpressibleByNilLiteral {
  init(nilLiteral: ()) { self = .null }
}

extension JSONValue: ExpressibleByBooleanLiteral {
  init(booleanLiteral value: Bool) { self = .bool(value) }
}

extension JSONValue: ExpressibleByIntegerLiteral {
  init(integerLiteral value: Int) { self = .number(Double(value)) }
}

extension JSONValue: ExpressibleByFloatLiteral {
  init(floatLiteral value: Double) { self = .number(value) }
}

extension JSONValue: ExpressibleByStringLiteral {
  init(stringLiteral value: String) { self = .string(value) }
}

extension JSONValue: ExpressibleByArrayLiteral {
  init(arrayLiteral elements: JSONValue...) { self = .array(elements) }
}

extension JSONValue: ExpressibleByDictionaryLiteral {
  init(dictionaryLiteral elements: (String, JSONValue)...) {
    self = .object(Dictionary(uniqueKeysWithValues: elements))
  }
}
