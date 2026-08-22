import ApplicationServices
import Foundation

/// Dispatches every wire operation for one accepted connection.
///
/// Each accepted connection gets its own `AgentHandlers` — and, through it, its own
/// `ElementRegistry` — while `executor` is the one process-wide `AXExecutor` shared by every
/// connection, so every live AX call still ends up serialized on a single queue regardless of
/// which connection issued it. `handle(_:)` never lets an error escape uncaught: every op either
/// returns a successful `WireResponse` or converts whatever it threw into a `WireResponse` whose
/// `error` carries a stable wire code, matching the existing Python exception taxonomy in
/// `src/macos_harness/macos.py` / `src/macos_harness/native.py`.
final class AgentHandlers {

  private let executor: AXExecutor
  private let registry = ElementRegistry()
  private let trustCheck: () -> Bool

  /// Snapshot of `systemUptime` the first time any handler on this process computes it,
  /// shared by every connection (it is a type-level constant, not per-instance) so `ping`'s
  /// `uptime_s` reports the agent process's own uptime rather than this connection's age.
  private static let bootUptime = ProcessInfo.processInfo.systemUptime

  /// Mirrors `pyproject.toml`'s `[project].version` at the time this agent was authored;
  /// nothing parses it, it is purely a human-readable `ping` field.
  private static let agentVersion = "0.2.0"

  /// Matches `_AX_ATTRIBUTES` in `macos.py`: the default attribute set for `ax_query`.
  private static let defaultAttributes = [
    "AXRole", "AXSubrole", "AXRoleDescription", "AXTitle", "AXDescription", "AXHelp",
    "AXIdentifier", "AXDOMIdentifier", "AXURL", "AXValue", "AXPlaceholderValue", "AXEnabled",
    "AXFocused", "AXSelected", "AXHidden", "AXPosition", "AXSize", "AXFrame", "AXChildren",
    "AXWindows",
  ]

  /// Matches `_AX_SAFE_ATTRIBUTES` in `macos.py`: the default attribute set for `ax_press`,
  /// which deliberately excludes `AXValue` (cheaper, and press targets rarely need it).
  private static let defaultSafeAttributes = [
    "AXRole", "AXTitle", "AXDescription", "AXPlaceholderValue", "AXHelp", "AXIdentifier",
    "AXDOMIdentifier", "AXEnabled", "AXFocused", "AXSelected", "AXHidden", "AXFrame",
  ]

  /// `executor` defaults to the shared, process-wide queue so plain `AgentHandlers()` (as used
  /// by every test in `HandlerSeamTests`) works with no setup; the server wires the same
  /// default explicitly per accepted connection.
  init(
    executor: AXExecutor = .shared,
    trustCheck: @escaping () -> Bool = { AXIsProcessTrusted() }
  ) {
    self.executor = executor
    self.trustCheck = trustCheck
  }

  /// Handles exactly one request and always returns a response carrying the same `id` —
  /// success or failure, this call never throws and never lets a Swift error propagate to the
  /// caller, so one malformed or unsupported request can never take down the connection.
  func handle(_ request: WireRequest) -> WireResponse {
    do {
      let result = try dispatch(request)
      return WireResponse.success(id: request.id, result: result)
    } catch let error as AgentError {
      return WireResponse.failure(id: request.id, error: error)
    } catch {
      return WireResponse.failure(
        id: request.id,
        error: AgentError(code: "ax.error", message: String(describing: error)))
    }
  }

  // MARK: - Operation routing

  private func dispatch(_ request: WireRequest) throws -> JSONValue {
    switch request.op {
    case "ping":
      return handlePing()
    case "list_apps":
      return try handleListApps()
    case "ax_query":
      try requireTrust()
      return try handleQuery(params: request.params)
    case "ax_press":
      try requireTrust()
      return try handlePress(params: request.params)
    case "ax_element_get":
      try requireTrust()
      return try handleElementGet(params: request.params)
    case "ax_element_set":
      try requireTrust()
      return try handleElementSet(params: request.params)
    case "ax_element_perform":
      try requireTrust()
      return try handleElementPerform(params: request.params)
    default:
      throw AgentError(code: "unsupported_op", message: "Unsupported operation \"\(request.op)\"")
    }
  }

  /// Every AX operation re-checks trust agent-side — client-side trust never implies
  /// agent-side trust — and fails closed before touching any AX API or its params.
  /// `ping`/`list_apps` never call this.
  private func requireTrust() throws {
    guard trustCheck() else {
      throw AgentError(
        code: "permission.accessibility", message: "Accessibility permission is required")
    }
  }

  // MARK: - ping / list_apps

  private func handlePing() -> JSONValue {
    .object([
      "protocol": .number(Double(protocolVersion)),
      "agent_version": .string(Self.agentVersion),
      "pid": .number(Double(ProcessInfo.processInfo.processIdentifier)),
      "trusted": .bool(trustCheck()),
      "uptime_s": .number(ProcessInfo.processInfo.systemUptime - Self.bootUptime),
    ])
  }

  private func handleListApps() throws -> JSONValue {
    let apps = try executor.listApps()
    return .object(["apps": .array(apps.map { Self.encode(app: $0) })])
  }

  /// Mirrors `MacOS._app_info` in `macos.py` field for field, including the `bundle_id`/`path`
  /// keys always being present (as JSON `null` when absent) rather than omitted.
  private static func encode(app: AppInfo) -> JSONValue {
    .object([
      "name": .string(app.name),
      "bundle_id": app.bundleID.map(JSONValue.string) ?? .null,
      "pid": .number(Double(app.pid)),
      "path": app.path.map(JSONValue.string) ?? .null,
    ])
  }

  // MARK: - AX query / press

  private func handleQuery(params: JSONValue) throws -> JSONValue {
    let appPid = try Self.requiredPID(params, "app_pid")
    let searchKey = Self.string(params, "search_key") ?? "AXAnyTypeSearchKey"
    let text = try Self.boundedText(params)
    let visibleOnly = Self.bool(params, "visible_only", default: false)
    let direction = Self.string(params, "direction") ?? "next"
    let immediateDescendantsOnly = Self.bool(params, "immediate_descendants_only", default: false)
    let attributes = try Self.boundedAttributes(params, default: Self.defaultAttributes)
    let includeActions = Self.bool(params, "include_actions", default: true)
    let maxNodes = try Self.boundedMaxNodes(params, default: 500)
    let limit = try Self.boundedLimit(params, effectiveMaxNodes: maxNodes)
    let resetElements = Self.bool(params, "reset_elements", default: true)
    let messagingTimeout = try Self.boundedMessagingTimeout(params)
    let enhance = Self.bool(params, "enhance", default: true)

    // "Query resets registry when requested": honor the caller's reset_elements, unlike
    // ax_press below which always resets regardless of what the wire params say.
    if resetElements {
      registry.reset()
    }

    let matches = try executor.query(
      pid: appPid,
      searchKey: searchKey,
      text: text,
      visibleOnly: visibleOnly,
      limit: limit,
      direction: direction,
      immediateDescendantsOnly: immediateDescendantsOnly,
      attributes: attributes,
      includeActions: includeActions,
      maxNodes: maxNodes,
      messagingTimeout: messagingTimeout,
      enhance: enhance,
      registry: registry)
    return .object(["matches": .array(matches.map { $0.wireValue })])
  }

  /// Builds the `PressCoordinator` seam around `AXExecutor` and delegates to it. Ignores
  /// whatever `limit`/`include_actions`/`reset_elements` the wire params might claim: a
  /// single-shot press always resets the registry first and always searches with an effective
  /// limit of two, matching `MacOS._native_press` in `macos.py` — the agent enforces this
  /// itself rather than trusting a client to have sent the right values.
  private func handlePress(params: JSONValue) throws -> JSONValue {
    let targetPID = try Self.requiredPID(params, "app_pid")
    let searchKey = Self.string(params, "search_key") ?? "AXAnyTypeSearchKey"
    let text = try Self.boundedText(params)
    let visibleOnly = Self.bool(params, "visible_only", default: true)
    let direction = Self.string(params, "direction") ?? "next"
    let immediateDescendantsOnly = Self.bool(params, "immediate_descendants_only", default: false)
    let attributes = try Self.boundedAttributes(params, default: Self.defaultSafeAttributes)
    let maxNodes = try Self.boundedMaxNodes(params, default: 500)
    let messagingTimeout = try Self.boundedMessagingTimeout(params)
    let enhance = Self.bool(params, "enhance", default: true)

    registry.reset()

    // Bind to locals so the closures below capture plain values instead of `self`.
    let executor = self.executor
    let registry = self.registry
    let deps = PressCoordinator.Dependencies(
      search: {
        try executor.query(
          pid: targetPID,
          searchKey: searchKey,
          text: text,
          visibleOnly: visibleOnly,
          limit: 2,
          direction: direction,
          immediateDescendantsOnly: immediateDescendantsOnly,
          attributes: attributes,
          includeActions: true,
          maxNodes: maxNodes,
          messagingTimeout: messagingTimeout,
          enhance: enhance,
          registry: registry)
      },
      frontmostPID: { executor.frontmostApplicationPID() },
      performPress: { match in
        try executor.perform(handle: match.handle, action: "AXPress", registry: registry)
      })

    let match = try PressCoordinator.run(targetPID: targetPID, deps)
    return .object(["match": match.wireValue])
  }

  // MARK: - AX element primitives

  private func handleElementGet(params: JSONValue) throws -> JSONValue {
    let handle = try Self.requiredInt(params, "handle")
    let attributes = try Self.boundedAttributes(params, default: [])
    let values = try executor.get(handle: handle, attributes: attributes, registry: registry)
    return .object(["attributes": .object(values)])
  }

  private func handleElementSet(params: JSONValue) throws -> JSONValue {
    let handle = try Self.requiredInt(params, "handle")
    let attribute = try Self.requiredAttributeName(params, "attribute")
    let value = params["value"] ?? .null
    try executor.set(handle: handle, attribute: attribute, value: value, registry: registry)
    return .object([:])
  }

  private func handleElementPerform(params: JSONValue) throws -> JSONValue {
    let handle = try Self.requiredInt(params, "handle")
    let action = try Self.requiredString(params, "action")
    try executor.perform(handle: handle, action: action, registry: registry)
    return .object([:])
  }

  // MARK: - Wire parameter decoding
  //
  // Every helper reads from the request's `params` (itself a `JSONValue`, normally `.object`)
  // via its `subscript(key:)` and the `*Value` accessors from `JSONValue.swift`. Numeric
  // fields never go through a bare `Int(Double)`; they go through `Int(exactly:)`/
  // `pid_t(exactly:)` so a malformed or hostile wire number fails closed with `bad_request`
  // instead of crashing the process.
  //
  // The `bounded*` helpers below additionally enforce this agent's request/output ceilings —
  // independent of whatever bound the Python client applies on its own side, since a hostile
  // or buggy client can talk to this socket directly. Each one rejects a *present* value that
  // violates its ceiling with `bad_request` rather than silently clamping or dropping it, so a
  // caller never mistakes a silently narrowed request for the one it actually sent. An absent
  // key or an explicit JSON `null` both mean "use the default," exactly like every other wire
  // parameter accessor in this file already treats them — `isPresent` is what distinguishes
  // that case from a present-but-wrong-shaped value.

  private static let maxNodesCeiling = 5_000
  private static let limitCeiling = 1_000
  private static let attributesCountCeiling = 64
  private static let attributeNameByteCeiling = 256
  private static let textByteCeiling = 1024 * 1024
  private static let messagingTimeoutRange = 0.01...6.0

  private static func requiredString(_ params: JSONValue, _ key: String) throws -> String {
    guard let value = params[key]?.stringValue else {
      throw AgentError(code: "bad_request", message: "Missing or invalid \"\(key)\" parameter")
    }
    return value
  }

  private static func requiredInt(_ params: JSONValue, _ key: String) throws -> Int {
    guard let raw = params[key]?.numberValue, let value = Int(exactly: raw) else {
      throw AgentError(code: "bad_request", message: "Missing or invalid \"\(key)\" parameter")
    }
    return value
  }

  private static func requiredPID(_ params: JSONValue, _ key: String) throws -> pid_t {
    guard let raw = params[key]?.numberValue, let value = pid_t(exactly: raw) else {
      throw AgentError(code: "bad_request", message: "Missing or invalid \"\(key)\" parameter")
    }
    return value
  }

  /// A single wire-supplied AX attribute name (currently just `ax_element_set`'s `attribute`
  /// field): non-empty per `requiredString`, and bounded by the same
  /// `attributeNameByteCeiling` every entry of an `attributes` array is bounded by below.
  private static func requiredAttributeName(_ params: JSONValue, _ key: String) throws -> String {
    let name = try requiredString(params, key)
    guard name.utf8.count <= attributeNameByteCeiling else {
      throw AgentError(
        code: "bad_request",
        message: "\"\(key)\" must not exceed \(attributeNameByteCeiling) UTF-8 bytes")
    }
    return name
  }

  private static func string(_ params: JSONValue, _ key: String) -> String? {
    params[key]?.stringValue
  }

  private static func bool(_ params: JSONValue, _ key: String, default defaultValue: Bool) -> Bool {
    params[key]?.boolValue ?? defaultValue
  }

  /// True when `key` is present in `params` and was not sent as an explicit JSON `null`.
  private static func isPresent(_ params: JSONValue, _ key: String) -> Bool {
    guard let value = params[key] else { return false }
    return !value.isNull
  }

  /// `max_nodes`: absent (or `null`) defaults to `defaultValue`; present must be a positive
  /// integer no greater than `maxNodesCeiling`.
  private static func boundedMaxNodes(
    _ params: JSONValue, default defaultValue: Int
  ) throws -> Int {
    guard isPresent(params, "max_nodes") else { return defaultValue }
    guard let raw = params["max_nodes"]?.numberValue, let value = Int(exactly: raw),
      (1...maxNodesCeiling).contains(value)
    else {
      throw AgentError(
        code: "bad_request",
        message: "\"max_nodes\" must be an integer in 1...\(maxNodesCeiling)")
    }
    return value
  }

  /// `limit`: absent (or `null`) maps to `effectiveMaxNodes`, matching how a negative value
  /// maps below — an unbounded/"unlimited" request is capped at the traversal's own
  /// `max_nodes` ceiling rather than left truly unlimited. A present value must be an integer
  /// no greater than `limitCeiling`; zero is a valid "return nothing" request rather than an
  /// error, matching `AXExecutor.boundedSearch`'s own `resultLimit > 0 else { return [] }`.
  private static func boundedLimit(_ params: JSONValue, effectiveMaxNodes: Int) throws -> Int {
    guard isPresent(params, "limit") else { return effectiveMaxNodes }
    guard let raw = params["limit"]?.numberValue, let value = Int(exactly: raw) else {
      throw AgentError(code: "bad_request", message: "\"limit\" must be an integer")
    }
    guard value <= limitCeiling else {
      throw AgentError(code: "bad_request", message: "\"limit\" must not exceed \(limitCeiling)")
    }
    return value < 0 ? effectiveMaxNodes : value
  }

  /// `attributes`: absent (or `null`) defaults to `defaultValue`; present must be an array of
  /// strings, no more than `attributesCountCeiling` of them, each no longer than
  /// `attributeNameByteCeiling` UTF-8 bytes. Unlike the pre-hardening `stringArray` helper this
  /// replaces, a present-but-malformed entry is a `bad_request`, never silently dropped.
  private static func boundedAttributes(
    _ params: JSONValue, default defaultValue: [String]
  ) throws -> [String] {
    guard isPresent(params, "attributes") else { return defaultValue }
    guard let array = params["attributes"]?.arrayValue else {
      throw AgentError(code: "bad_request", message: "\"attributes\" must be an array of strings")
    }
    guard array.count <= attributesCountCeiling else {
      throw AgentError(
        code: "bad_request",
        message: "\"attributes\" must not exceed \(attributesCountCeiling) entries")
    }
    var names: [String] = []
    names.reserveCapacity(array.count)
    for element in array {
      guard let name = element.stringValue else {
        throw AgentError(code: "bad_request", message: "\"attributes\" entries must be strings")
      }
      guard name.utf8.count <= attributeNameByteCeiling else {
        throw AgentError(
          code: "bad_request",
          message:
            "\"attributes\" entries must not exceed \(attributeNameByteCeiling) UTF-8 bytes")
      }
      names.append(name)
    }
    return names
  }

  /// `text`: absent (or `null`) means no text filter at all; present must be a string no
  /// longer than `textByteCeiling` UTF-8 bytes.
  private static func boundedText(_ params: JSONValue) throws -> String? {
    guard isPresent(params, "text") else { return nil }
    guard let text = params["text"]?.stringValue else {
      throw AgentError(code: "bad_request", message: "\"text\" must be a string")
    }
    guard text.utf8.count <= textByteCeiling else {
      throw AgentError(
        code: "bad_request", message: "\"text\" must not exceed \(textByteCeiling) UTF-8 bytes")
    }
    return text
  }

  /// `messaging_timeout`: absent (or `null`) leaves the per-element messaging timeout at its
  /// AX-API default; present must be a finite number within `messagingTimeoutRange`.
  private static func boundedMessagingTimeout(_ params: JSONValue) throws -> Double? {
    guard isPresent(params, "messaging_timeout") else { return nil }
    guard let value = params["messaging_timeout"]?.numberValue, value.isFinite,
      messagingTimeoutRange.contains(value)
    else {
      throw AgentError(
        code: "bad_request",
        message:
          "\"messaging_timeout\" must be a finite number in "
          + "\(messagingTimeoutRange.lowerBound)...\(messagingTimeoutRange.upperBound)")
    }
    return value
  }
}
