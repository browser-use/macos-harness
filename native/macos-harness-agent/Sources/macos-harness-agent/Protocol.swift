import Foundation

/// Major protocol version this agent speaks. Echoed in `ping`'s result under
/// the `protocol` key; the Python client's `PROTOCOL_VERSION` constant must match it.
let protocolVersion = 1

/// Wire-level request envelope: `{"v":1,"id":<int>,"op":<str>,"params":{...}}`.
struct WireRequest: Codable {
  let v: Int
  let id: Int
  let op: String
  let params: JSONValue
}

/// Wire-level error payload embedded in a failing `WireResponse`:
/// `{"code":<str>,"message":<str>,"ax_error":<int?>}`. `axError` is omitted
/// entirely on encode when absent, and decodes as `nil` when the key is
/// missing.
struct WireError: Codable {
  let code: String
  let message: String
  let axError: Int?

  enum CodingKeys: String, CodingKey {
    case code
    case message
    case axError = "ax_error"
  }

  init(code: String, message: String, axError: Int? = nil) {
    self.code = code
    self.message = message
    self.axError = axError
  }

  /// Wire-shape projection of a thrown `AgentError`.
  init(_ error: AgentError) {
    self.init(code: error.code, message: error.message, axError: error.axError)
  }
}

/// Wire-level response envelope: `{"id":<int>,"ok":<bool>,"result":{...}}` on
/// success or `{"id":<int>,"ok":false,"error":{...}}` on failure. `result`
/// and `error` are mutually exclusive and the absent one is always omitted
/// on encode (never emitted as `null`).
///
/// Conforms to `Error` solely so it can stand as the `Failure` type of
/// `Result<WireRequest, WireResponse>` in `ProtocolCodec.decode` — a
/// `WireResponse` built by that path is data returned to the caller, never
/// thrown.
struct WireResponse: Codable, Error {
  let id: Int
  let ok: Bool
  let result: JSONValue?
  let error: WireError?

  init(id: Int, ok: Bool, result: JSONValue? = nil, error: WireError? = nil) {
    self.id = id
    self.ok = ok
    self.result = result
    self.error = error
  }

  static func success(id: Int, result: JSONValue) -> WireResponse {
    WireResponse(id: id, ok: true, result: result, error: nil)
  }

  static func failure(id: Int, error: AgentError) -> WireResponse {
    WireResponse(id: id, ok: false, result: nil, error: WireError(error))
  }
}

/// An operation handler's internal failure, mapped 1:1 onto a `WireError` by
/// `WireError.init(_:)`/`WireResponse.failure(id:error:)` wherever a thrown
/// `AgentError` becomes a response.
///
/// Recognized wire codes: `permission.accessibility`, `app.not_found`,
/// `app.ambiguous`, `focus.changed`, `ax.error`, `element.unknown`,
/// `timeout`, `bad_request`, `unsupported_op`. `axError` carries the raw
/// `AXError` integer for `ax.error`; every other code leaves it `nil`.
struct AgentError: Error {
  let code: String
  let message: String
  let axError: Int?

  init(code: String, message: String, axError: Int? = nil) {
    self.code = code
    self.message = message
    self.axError = axError
  }
}

/// Decodes a single NDJSON request line. On structural failure — invalid
/// JSON, a non-object top level, or an object missing/mistyping `v`/`id`/
/// `op`/`params` — recovers a numeric `id` from the raw payload where one is
/// present, so the caller can still echo it back on the `bad_request`
/// response instead of losing correlation with the request.
enum ProtocolCodec {
  static func decode(_ payload: Data) -> Result<WireRequest, WireResponse> {
    let decoder = JSONDecoder()
    if let request = try? decoder.decode(WireRequest.self, from: payload) {
      return .success(request)
    }

    let recoveredID: Int
    let message: String
    switch try? decoder.decode(JSONValue.self, from: payload) {
    case .object(let fields):
      if case .number(let rawID)? = fields["id"] {
        recoveredID = Int(exactly: rawID) ?? 0
      } else {
        recoveredID = 0
      }
      message =
        "Request object is missing a required field (v, id, op, params) or has the wrong type"
    case .some:
      recoveredID = 0
      message = "Request must be a JSON object"
    case .none:
      recoveredID = 0
      message = "Request body is not valid JSON"
    }

    return .failure(
      WireResponse(
        id: recoveredID, ok: false, result: nil,
        error: WireError(code: "bad_request", message: message)))
  }
}

/// Shared description of an AX element minted by an agent-side search,
/// addressed henceforth by `handle`. Mirrors the node shape
/// `MacOS._describe_element` produces in `macos.py`, with `element_index`
/// renamed to `handle` on the agent wire.
///
/// `fields` carries the already wire-keyed, already-`_jsonable`-reduced
/// mapped AX attributes (`subrole`, `role_description`, `title`,
/// `description`, `help`, `identifier`, `dom_identifier`, `url`, `value`,
/// `placeholder`, `enabled`, `focused`, `selected`, `hidden`, `position`,
/// `size`, `frame` — one entry per attribute actually present and non-empty
/// on the element, mirroring `_AX_NODE_MAPPING`). `attributes` carries any
/// extra, non-mapped attributes a caller explicitly requested. Producers
/// must not place `handle`, `role`, `attributes`, or `actions` inside
/// `fields`; `wireValue` always wins those four keys regardless.
struct ElementDescriptor: Equatable {
  var handle: Int
  var actions: [String]
  var role: String?
  var fields: [String: JSONValue]
  var attributes: [String: JSONValue]

  init(
    handle: Int,
    actions: [String] = [],
    role: String? = nil,
    fields: [String: JSONValue] = [:],
    attributes: [String: JSONValue] = [:]
  ) {
    self.handle = handle
    self.actions = actions
    self.role = role
    self.fields = fields
    self.attributes = attributes
  }

  /// The wire-shape JSON object: `handle`, then `role`, the mapped AX
  /// fields, optional `attributes`, optional `actions` — every
  /// absent/empty value omitted, matching `_describe_element` parity.
  var wireValue: JSONValue {
    var object: [String: JSONValue] = [:]
    for (key, value) in fields where value != .null {
      object[key] = value
    }
    object["handle"] = .number(Double(handle))
    if let role = role, !role.isEmpty {
      object["role"] = .string(role)
    }
    if !attributes.isEmpty {
      object["attributes"] = .object(attributes)
    }
    if !actions.isEmpty {
      object["actions"] = .array(actions.map(JSONValue.string))
    }
    return .object(object)
  }
}
