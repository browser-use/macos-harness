import AppKit
import ApplicationServices
import Foundation

/// The one process-wide owner of every Accessibility (AX) and `NSWorkspace` call this agent
/// makes. Every public method funnels its actual work through `queue`, a single serial
/// `DispatchQueue`, so no two AX or `NSWorkspace` calls — regardless of which connection or
/// `ElementRegistry` they touch — are ever in flight at the same time, matching the guarantee
/// `Handlers.swift` documents: "every live AX call still ends up serialized on a single queue
/// regardless of which connection issued it." Registry mutations (`register`/`resolve`) happen
/// on that same queue for the same reason `ElementRegistry` itself carries no locking of its own.
///
/// Every caller-facing method assumes `Handlers.swift` has already gated the request on
/// `AXIsProcessTrusted()` (via `requireTrust()`); nothing here re-checks or prompts for
/// Accessibility permission, activates an application, or raises one to the foreground.
///
/// Mirrors the AX-facing half of `MacOS` in `src/macos_harness/macos.py` — attribute reading,
/// `AXUIElementsForSearchPredicate` search with the bounded-traversal fallback, and the plain
/// element get/set/perform primitives. Higher-level app/window/input surface remains Python-only.
/// `Handlers.swift` hands query calls an already-resolved `app_pid`; the explicit
/// `resolveApplication(query:)` path still uses `AppResolver` for selector resolution.
final class AXExecutor {

  static let shared = AXExecutor()

  /// Serializes every ApplicationServices and NSWorkspace call this process makes, plus every
  /// `ElementRegistry` mutation performed while producing their results.
  private static let queue = DispatchQueue(label: "com.macos-harness-agent.ax-executor")

  private init() {}

  // MARK: - App discovery

  func listApps() throws -> [AppInfo] {
    Self.queue.sync { Self.runningApps() }
  }
  /// Resolves a caller-provided app selector against the current running-app snapshot.
  /// The shared serial queue keeps discovery and resolver input from racing with other AX work.
  func resolveApplication(query: String) throws -> AppInfo {
    try Self.queue.sync {
      try AppResolver.resolve(query: query, candidates: Self.runningApps())
    }
  }

  private static func requireTrust() throws {
    guard AXIsProcessTrusted() else {
      throw AgentError(
        code: "permission.accessibility",
        message: "Accessibility permission is required")
    }
  }

  func frontmostApplicationPID() -> pid_t? {
    Self.queue.sync { NSWorkspace.shared.frontmostApplication?.processIdentifier }
  }

  // MARK: - AX query

  func query(
    pid: pid_t,
    searchKey: String,
    text: String?,
    visibleOnly: Bool,
    limit: Int,
    direction: String,
    immediateDescendantsOnly: Bool,
    attributes: [String],
    includeActions: Bool,
    maxNodes: Int,
    messagingTimeout: Double?,
    enhance: Bool,
    registry: ElementRegistry
  ) throws -> [ElementDescriptor] {
    try Self.queue.sync {
      try performQuery(
        pid: pid, searchKey: searchKey, text: text, visibleOnly: visibleOnly, limit: limit,
        direction: direction, immediateDescendantsOnly: immediateDescendantsOnly,
        attributes: attributes, includeActions: includeActions, maxNodes: maxNodes,
        messagingTimeout: messagingTimeout, enhance: enhance, registry: registry)
    }
  }

  // MARK: - AX element primitives

  func get(handle: Int, attributes: [String], registry: ElementRegistry) throws -> [String:
    JSONValue]
  {
    try Self.queue.sync {
      try performGet(handle: handle, attributes: attributes, registry: registry)
    }
  }

  func set(handle: Int, attribute: String, value: JSONValue, registry: ElementRegistry) throws {
    try Self.queue.sync {
      try performSet(handle: handle, attribute: attribute, value: value, registry: registry)
    }
  }

  func perform(handle: Int, action: String, registry: ElementRegistry) throws {
    try Self.queue.sync { try performPerform(handle: handle, action: action, registry: registry) }
  }

  // MARK: - App discovery internals (must only run on `queue`)

  private static func runningApps() -> [AppInfo] {
    NSWorkspace.shared.runningApplications
      .map(appInfo(from:))
      .filter { !$0.name.isEmpty }
      .sorted { lhs, rhs in
        let lname = lhs.name.lowercased()
        let rname = rhs.name.lowercased()
        return lname == rname ? lhs.pid < rhs.pid : lname < rname
      }
  }

  /// Mirrors `MacOS._app_info`: the display name falls back name -> bundle id -> path -> empty
  /// string, and `bundle_id`/`path` are blanked to `nil` (never an empty string) exactly like
  /// Python's `str(x) if x else None`.
  private static func appInfo(from app: NSRunningApplication) -> AppInfo {
    let bundleID = nonEmpty(app.bundleIdentifier)
    let path = nonEmpty(app.bundleURL?.path)
    let name = firstNonEmpty(app.localizedName, bundleID, path) ?? ""
    return AppInfo(name: name, bundleID: bundleID, pid: app.processIdentifier, path: path)
  }

  private static func nonEmpty(_ value: String?) -> String? {
    guard let value, !value.isEmpty else { return nil }
    return value
  }

  private static func firstNonEmpty(_ values: String?...) -> String? {
    for value in values {
      if let value = nonEmpty(value) { return value }
    }
    return nil
  }

  // MARK: - Search (must only run on `queue`)

  /// Matches `_AX_SEARCH_ROLES` in `macos.py`: the search-key -> role mapping the bounded
  /// fallback uses to filter by role when the optimized predicate search is unavailable.
  private static let searchRoles: [String: String] = {
    let roles = [
      "Button", "CheckBox", "ComboBox", "Image", "Link", "List", "Menu", "MenuItem",
      "RadioButton", "StaticText", "Table", "TextArea", "TextField",
    ]
    return Dictionary(uniqueKeysWithValues: roles.map { ("AX\($0)SearchKey", "AX\($0)") })
  }()

  /// Matches `_AX_NODE_MAPPING` in `macos.py`: raw AX attribute name -> wire field name.
  private static let axNodeMapping: [String: String] = [
    "AXSubrole": "subrole",
    "AXRoleDescription": "role_description",
    "AXTitle": "title",
    "AXDescription": "description",
    "AXHelp": "help",
    "AXIdentifier": "identifier",
    "AXDOMIdentifier": "dom_identifier",
    "AXURL": "url",
    "AXValue": "value",
    "AXPlaceholderValue": "placeholder",
    "AXEnabled": "enabled",
    "AXFocused": "focused",
    "AXSelected": "selected",
    "AXHidden": "hidden",
    "AXPosition": "position",
    "AXSize": "size",
    "AXFrame": "frame",
  ]

  /// Matches `_ACTION_ALIASES` in `macos.py`: a friendly action name -> its AX action constant.
  private static let actionAliases: [String: String] = [
    "press": "AXPress",
    "show menu": "AXShowMenu",
    "confirm": "AXConfirm",
    "cancel": "AXCancel",
    "increment": "AXIncrement",
    "decrement": "AXDecrement",
    "raise": "AXRaise",
  ]

  /// The wire field names the bounded-fallback substring search matches against, in the same
  /// order `_bounded_ax_search` checks them.
  private static let matchFieldOrder = [
    "title", "description", "value", "help", "identifier", "dom_identifier", "placeholder",
  ]

  /// Matches `ax_search`/`_application_element` in `macos.py`: validate `direction`, build the
  /// application root (messaging timeout + optional enhanced UI), then try the optimized
  /// `AXUIElementsForSearchPredicate` parameterized attribute; only on
  /// `kAXErrorParameterizedAttributeUnsupported` does it fall back to a bounded ordinary
  /// traversal. Direction is validated before the root is created (Python validates it after)
  /// so a bad request never touches the target application's AX state.
  private func performQuery(
    pid: pid_t, searchKey: String, text: String?, visibleOnly: Bool, limit: Int, direction: String,
    immediateDescendantsOnly: Bool, attributes: [String], includeActions: Bool, maxNodes: Int,
    messagingTimeout: Double?, enhance: Bool, registry: ElementRegistry
  ) throws -> [ElementDescriptor] {
    try Self.requireTrust()

    let axDirection: String
    switch direction.lowercased() {
    case "next": axDirection = "AXDirectionNext"
    case "previous": axDirection = "AXDirectionPrevious"
    default:
      throw AgentError(
        code: "bad_request", message: "AX search direction must be 'next' or 'previous'")
    }

    let root = try Self.applicationElement(
      pid: pid, messagingTimeout: messagingTimeout, enhance: enhance)

    var predicate: [String: Any] = [
      "AXSearchKey": searchKey,
      "AXVisibleOnly": visibleOnly,
      "AXResultsLimit": limit,
      "AXDirection": axDirection,
      "AXImmediateDescendantsOnly": immediateDescendantsOnly,
    ]
    if let text {
      predicate["AXSearchText"] = text
    }

    var resultRef: CFTypeRef?
    let error = AXUIElementCopyParameterizedAttributeValue(
      root, "AXUIElementsForSearchPredicate" as CFString, predicate as CFDictionary, &resultRef)

    if error == .parameterizedAttributeUnsupported {
      return try Self.boundedSearch(
        root: root, searchKey: searchKey, text: text, visibleOnly: visibleOnly, limit: limit,
        direction: direction, immediateDescendantsOnly: immediateDescendantsOnly,
        attributes: attributes, includeActions: includeActions, maxNodes: maxNodes,
        registry: registry)
    }
    guard error == .success else {
      throw Self.axAgentError("AXUIElementsForSearchPredicate", error)
    }

    guard let values = resultRef as? [AnyObject] else { return [] }
    var matches: [ElementDescriptor] = []
    matches.reserveCapacity(values.count)
    for value in values {
      guard Self.isAXUIElement(value) else { continue }
      let element = value as! AXUIElement
      let handle = registry.register(element)
      matches.append(
        Self.describeElement(
          element, handle: handle, attributes: attributes, includeActions: includeActions))
    }
    return matches
  }

  /// One node visited while walking a small ordinary AX tree in `boundedSearch`, matching what
  /// `_snapshot_tree` records per node in `macos.py`. `matchText` holds the already-stringified
  /// values of the wire fields `_bounded_ax_search` substring-matches against; `element` is
  /// retained so a surviving match can be re-described from scratch exactly like
  /// `_describe_element` does, rather than reusing the (traversal-attribute-reduced) snapshot.
  private struct TraversalNode {
    let handle: Int
    let element: AXUIElement
    let role: String?
    let hidden: Bool
    let matchText: [String]
  }

  /// Searches a small ordinary AX tree when the optimized parameterized predicate is
  /// unsupported, matching `_bounded_ax_search` in `macos.py` one for one: `max_nodes` must be
  /// positive (there is no tree to walk otherwise), a non-positive effective limit yields no
  /// results rather than an error, the root itself is never a candidate, and `previous` walks
  /// the flattened traversal in reverse.
  private static func boundedSearch(
    root: AXUIElement, searchKey: String, text: String?, visibleOnly: Bool, limit: Int,
    direction: String,
    immediateDescendantsOnly: Bool, attributes: [String], includeActions: Bool, maxNodes: Int,
    registry: ElementRegistry
  ) throws -> [ElementDescriptor] {
    guard maxNodes > 0 else {
      throw AgentError(code: "bad_request", message: "AX fallback max_nodes must be positive")
    }
    let resultLimit = limit < 0 ? maxNodes : limit
    guard resultLimit > 0 else { return [] }

    let role = searchRoles[searchKey]
    let needle = text?.lowercased()

    var traversalAttributes = dedup(attributes)
    if !traversalAttributes.contains("AXHidden") {
      traversalAttributes.append("AXHidden")
    }

    var nodes = try snapshotTree(
      root: root,
      maxDepth: immediateDescendantsOnly ? 1 : 25,
      maxNodes: maxNodes,
      includeMenuBar: true,
      attributes: traversalAttributes,
      registry: registry)
    guard !nodes.isEmpty else { return [] }
    nodes.removeFirst()  // the root itself is never a fallback match candidate

    if direction.lowercased() == "previous" {
      nodes.reverse()
    }

    var matches: [ElementDescriptor] = []
    for node in nodes {
      guard role == nil || node.role == role else { continue }
      guard !visibleOnly || !node.hidden else { continue }
      if let needle {
        guard node.matchText.contains(where: { $0.lowercased().contains(needle) }) else { continue }
      }
      matches.append(
        describeElement(
          node.element, handle: node.handle, attributes: attributes, includeActions: includeActions)
      )
      if matches.count >= resultLimit { break }
    }
    return matches
  }

  /// Depth-first, cycle-guarded walk mirroring `_snapshot_tree` in `macos.py`: bounded by
  /// `maxDepth` and `maxNodes`, descending into `AXChildren` (or, at the root only, `AXWindows`
  /// when there are no children), and — unless `includeMenuBar` — never visiting an
  /// `AXMenuBar` element or its descendants. Every visited node is registered in `registry`
  /// regardless of whether it later survives `boundedSearch`'s filtering, exactly like
  /// `_remember_element` runs unconditionally inside `_snapshot_tree`'s own `visit`.
  /// `AXChildren`/`AXWindows` are fetched only to steer this traversal; they are never mapped
  /// attributes, so `mappedFields`/`extraFields` never surface them on the resulting nodes.
  private static func snapshotTree(
    root: AXUIElement, maxDepth: Int, maxNodes: Int, includeMenuBar: Bool, attributes: [String],
    registry: ElementRegistry
  ) throws -> [TraversalNode] {
    var requestedNames = dedup(attributes)
    for name in ["AXRole", "AXChildren", "AXWindows"] where !requestedNames.contains(name) {
      requestedNames.append(name)
    }

    var nodes: [TraversalNode] = []
    var seenHashes = Set<UInt>()

    func visit(_ element: AXUIElement, depth: Int) throws {
      guard depth <= maxDepth, nodes.count < maxNodes else { return }
      let identity = CFHash(element)
      guard seenHashes.insert(identity).inserted else { return }

      let raw = copyAttributes(element, requestedNames)
      let role = raw["AXRole"].map(jsonable)?.stringValue
      if role == "AXMenuBar" && !includeMenuBar { return }

      let handle = registry.register(element)
      let fields = mappedFields(from: raw)
      let hidden = fields["hidden"]?.boolValue ?? false
      let matchText = matchFieldOrder.compactMap { fields[$0].map(pythonLikeString) }
      nodes.append(
        TraversalNode(
          handle: handle, element: element, role: role, hidden: hidden, matchText: matchText))

      let childrenToVisit: [AnyObject]?
      if let children = raw["AXChildren"] as? [AnyObject], !children.isEmpty {
        childrenToVisit = children
      } else if depth == 0 {
        childrenToVisit = raw["AXWindows"] as? [AnyObject]
      } else {
        childrenToVisit = nil
      }
      if let childrenToVisit {
        for child in childrenToVisit {
          guard isAXUIElement(child) else { continue }
          let childElement = child as! AXUIElement
          try visit(childElement, depth: depth + 1)
        }
      }
    }

    try visit(root, depth: 0)
    return nodes
  }

  /// Sets up the application-root AX element exactly like `_application_element` in
  /// `macos.py`: an optional per-element messaging timeout, then — unless `enhance` is false —
  /// reads `AXEnhancedUserInterface` and, only if it is not already enabled, enables it and
  /// waits 50ms for the target app to settle. Never activates or raises the target application;
  /// `AXUIElementCreateApplication` performs no messaging of its own and cannot fail.
  private static func applicationElement(
    pid: pid_t, messagingTimeout: Double?, enhance: Bool
  ) throws -> AXUIElement {
    let root = AXUIElementCreateApplication(pid)
    if let messagingTimeout {
      let error = AXUIElementSetMessagingTimeout(root, Float(messagingTimeout))
      guard error == .success else {
        throw axAgentError("Set AX messaging timeout", error)
      }
    }
    guard enhance else { return root }

    var enhancedRef: CFTypeRef?
    let readError = AXUIElementCopyAttributeValue(
      root, "AXEnhancedUserInterface" as CFString, &enhancedRef)
    let alreadyEnhanced = enhancedRef.flatMap { $0 as? Bool } ?? false
    if readError == .success, !alreadyEnhanced {
      // Best-effort, exactly like the Python reference: the result of enabling the
      // enhanced-UI attribute is intentionally not checked.
      _ = AXUIElementSetAttributeValue(
        root, "AXEnhancedUserInterface" as CFString, true as CFTypeRef)
      Thread.sleep(forTimeInterval: 0.05)
    }
    return root
  }

  /// Builds one wire match, matching `_describe_element` in `macos.py`: re-reads `AXRole` plus
  /// exactly the caller's requested `attributes` straight from `element` (never reused from a
  /// traversal snapshot, which may have been taken with a different, traversal-only attribute
  /// set), maps the recognized subset onto wire field names, carries any other requested
  /// attribute under `attributes`, and includes `actions` only when asked and non-empty.
  private static func describeElement(
    _ element: AXUIElement, handle: Int, attributes: [String], includeActions: Bool
  ) -> ElementDescriptor {
    var requestedNames = ["AXRole"]
    for name in dedup(attributes) where name != "AXRole" {
      requestedNames.append(name)
    }
    let raw = copyAttributes(element, requestedNames)
    let role = raw["AXRole"].map(jsonable)?.stringValue
    let fields = mappedFields(from: raw)
    let extra = extraFields(from: raw)
    let actions = includeActions ? actionNames(element) : []
    return ElementDescriptor(
      handle: handle, actions: actions, role: role, fields: fields, attributes: extra)
  }

  // MARK: - Element primitive internals (must only run on `queue`)

  /// Matches `get_attributes`/`_copy_attributes` in `macos.py`: every requested name is
  /// represented in the result, `.null` where AX reports no value, regardless of how many
  /// times the caller listed the same name.
  private func performGet(
    handle: Int, attributes: [String], registry: ElementRegistry
  ) throws -> [String: JSONValue] {
    try Self.requireTrust()

    let element = try Self.resolveElement(handle, registry: registry)
    let names = Self.dedup(attributes)
    guard !names.isEmpty else { return [:] }
    let raw = Self.copyAttributes(element, names)
    var result: [String: JSONValue] = [:]
    for name in names {
      result[name] = raw[name].map(Self.jsonable) ?? .null
    }
    return result
  }

  /// Matches `MacOS.set`/`set_value` in `macos.py`.
  private func performSet(
    handle: Int, attribute: String, value: JSONValue, registry: ElementRegistry
  ) throws {
    try Self.requireTrust()

    let element = try Self.resolveElement(handle, registry: registry)
    let error = AXUIElementSetAttributeValue(
      element, attribute as CFString, value.foundationValue as CFTypeRef)
    guard error == .success else {
      throw Self.axAgentError("Set \(attribute) on element \(handle)", error)
    }
  }

  /// Matches `perform_action` in `macos.py`: resolves `action` through `actionAliases`
  /// case-insensitively (falling back to `action` verbatim for an already-raw AX action name),
  /// rejects it up front if the element does not currently expose it, then performs it.
  private func performPerform(handle: Int, action: String, registry: ElementRegistry) throws {
    try Self.requireTrust()
    let element = try Self.resolveElement(handle, registry: registry)
    let normalized = Self.actionAliases[action.lowercased()] ?? action
    let available = Self.actionNames(element)
    guard available.contains(normalized) else {
      throw AgentError(
        code: "element.unknown",
        message: "Element \(handle) does not expose \(normalized); available actions: \(available)")
    }
    let error = AXUIElementPerformAction(element, normalized as CFString)
    guard error == .success else {
      throw Self.axAgentError("Perform \(normalized) on element \(handle)", error)
    }
  }

  /// Matches `MacOS._element`: an unknown or already-evicted handle, or one that (defensively)
  /// did not resolve to a real AX element, fails closed with `element.unknown` rather than
  /// crashing.
  private static func resolveElement(_ handle: Int, registry: ElementRegistry) throws -> AXUIElement
  {
    guard let object = registry.resolve(handle) else {
      throw AgentError(
        code: "element.unknown",
        message: "Unknown element handle \(handle); take a fresh query first")
    }
    guard isAXUIElement(object) else {
      throw AgentError(
        code: "element.unknown", message: "Element handle \(handle) is not an AX element")
    }
    let element = object as! AXUIElement
    return element
  }

  // MARK: - Attribute reading (must only run on `queue`)

  /// Reads `names` from `element` in as few round trips as possible, matching
  /// `_copy_attributes` in `macos.py`: tries the batch API first and falls back to one
  /// `AXUIElementCopyAttributeValue` call per name if the batch call itself fails outright. A
  /// name AX reports as unavailable — including a batched slot that decodes as the
  /// `kAXValueAXErrorType` per-item error sentinel — is simply absent from the result; callers
  /// that must represent every requested name (`performGet`) fill the gaps with `.null`
  /// themselves.
  private static func copyAttributes(_ element: AXUIElement, _ names: [String]) -> [String:
    AnyObject]
  {
    guard !names.isEmpty else { return [:] }

    var valuesRef: CFArray?
    let batchError = AXUIElementCopyMultipleAttributeValues(
      element, names as CFArray, [], &valuesRef)
    if batchError == .success, let values = valuesRef as? [AnyObject], values.count == names.count {
      var result: [String: AnyObject] = [:]
      for (name, value) in zip(names, values) where !isErrorSentinel(value) {
        result[name] = value
      }
      return result
    }

    var result: [String: AnyObject] = [:]
    for name in names {
      if let value = copyAttribute(element, name) {
        result[name] = value
      }
    }
    return result
  }

  private static func copyAttribute(_ element: AXUIElement, _ name: String) -> AnyObject? {
    var value: CFTypeRef?
    let error = AXUIElementCopyAttributeValue(element, name as CFString, &value)
    return error == .success ? value : nil
  }

  private static func actionNames(_ element: AXUIElement) -> [String] {
    var namesRef: CFArray?
    let error = AXUIElementCopyActionNames(element, &namesRef)
    guard error == .success, let names = namesRef as? [String] else { return [] }
    return names
  }

  /// Projects `raw` through `axNodeMapping`, matching the wire-field half of
  /// `_describe_element`/`_snapshot_tree`'s node construction: only the recognized attributes
  /// that were actually requested (present in `raw`) and non-empty after `jsonable` survive.
  private static func mappedFields(from raw: [String: AnyObject]) -> [String: JSONValue] {
    var fields: [String: JSONValue] = [:]
    for (source, target) in axNodeMapping {
      guard let rawValue = raw[source] else { continue }
      let value = jsonable(rawValue)
      guard !isEmptyJSON(value) else { continue }
      fields[target] = value
    }
    return fields
  }

  /// The non-mapped, explicitly-requested attributes, matching `_describe_element`'s `extra`
  /// dict: every key in `raw` other than `AXRole` and whatever `axNodeMapping` already claimed,
  /// filtered the same way `mappedFields` is.
  private static func extraFields(from raw: [String: AnyObject]) -> [String: JSONValue] {
    var extra: [String: JSONValue] = [:]
    for (name, rawValue) in raw where axNodeMapping[name] == nil && name != "AXRole" {
      let value = jsonable(rawValue)
      guard !isEmptyJSON(value) else { continue }
      extra[name] = value
    }
    return extra
  }

  private static func dedup(_ values: [String]) -> [String] {
    var seen = Set<String>()
    var result: [String] = []
    for value in values where seen.insert(value).inserted {
      result.append(value)
    }
    return result
  }

  /// Matches the Python fields-filter `value not in (None, "", [], {})`: `false`/`0` are never
  /// empty, only an absent value, empty string, empty array, or empty object are.
  private static func isEmptyJSON(_ value: JSONValue) -> Bool {
    switch value {
    case .null: return true
    case .string(let text): return text.isEmpty
    case .array(let items): return items.isEmpty
    case .object(let fields): return fields.isEmpty
    case .bool, .number: return false
    }
  }

  // MARK: - AX value <-> JSONValue conversion (must only run on `queue`)

  /// Converts one raw AX-returned value into wire JSON, matching `_jsonable` in `macos.py`:
  /// booleans and numbers pass through as JSON scalars, strings and decoded byte data become
  /// JSON strings, arrays drop any embedded AX elements before converting their remaining
  /// members, dictionaries convert key-for-key, and a wrapped `AXValue` (point/size/rect/range)
  /// becomes the matching small JSON object. Anything else — including a lone `AXUIElement`,
  /// which callers never hand this directly except embedded in an array — falls back to a
  /// readable description, mirroring Python's final `str(value)`.
  private static func jsonable(_ value: AnyObject) -> JSONValue {
    if value is NSNull { return .null }
    if let flag = value as? Bool { return .bool(flag) }
    if let number = value as? NSNumber { return .number(number.doubleValue) }
    if let text = value as? String { return .string(text) }
    if let data = value as? Data { return .string(String(decoding: data, as: UTF8.self)) }
    if let array = value as? [AnyObject] {
      return .array(array.filter { !isAXUIElement($0) }.map(jsonable))
    }
    if let dictionary = value as? [AnyHashable: AnyObject] {
      var object: [String: JSONValue] = [:]
      for (key, item) in dictionary {
        object[String(describing: key)] = jsonable(item)
      }
      return .object(object)
    }
    if let axValue = axValueCast(value) {
      return jsonableAXValue(axValue)
    }
    return .string(String(describing: value))
  }

  private static func jsonableAXValue(_ axValue: AXValue) -> JSONValue {
    switch AXValueGetType(axValue) {
    case .cgPoint:
      var point = CGPoint.zero
      guard AXValueGetValue(axValue, .cgPoint, &point) else {
        return .string(String(describing: axValue))
      }
      return .object(["x": .number(Double(point.x)), "y": .number(Double(point.y))])
    case .cgSize:
      var size = CGSize.zero
      guard AXValueGetValue(axValue, .cgSize, &size) else {
        return .string(String(describing: axValue))
      }
      return .object(["width": .number(Double(size.width)), "height": .number(Double(size.height))])
    case .cgRect:
      var rect = CGRect.zero
      guard AXValueGetValue(axValue, .cgRect, &rect) else {
        return .string(String(describing: axValue))
      }
      return .object([
        "x": .number(Double(rect.origin.x)),
        "y": .number(Double(rect.origin.y)),
        "width": .number(Double(rect.size.width)),
        "height": .number(Double(rect.size.height)),
      ])
    case .cfRange:
      var range = CFRange(location: 0, length: 0)
      guard AXValueGetValue(axValue, .cfRange, &range) else {
        return .string(String(describing: axValue))
      }
      return .object([
        "location": .number(Double(range.location)), "length": .number(Double(range.length)),
      ])
    default:
      return .string(String(describing: axValue))
    }
  }

  /// Best-effort `str(value)` used only for the bounded-fallback substring text match
  /// (`_bounded_ax_search`'s `needle in str(value).casefold()`), where a mapped field is
  /// occasionally non-string (for example a numeric or boolean `AXValue`). Scalars mirror
  /// Python's `str()` formatting, including capitalized `True`/`False`; a mapped field never
  /// legitimately nests an array or object in practice, so that shape falls back to a plain
  /// description rather than Python's exact list/dict repr syntax.
  private static func pythonLikeString(_ value: JSONValue) -> String {
    switch value {
    case .null: return "None"
    case .bool(let flag): return flag ? "True" : "False"
    case .string(let text): return text
    case .number(let number):
      if let integral = Int64(exactly: number) {
        return String(integral)
      }
      return String(number)
    case .array, .object:
      return String(describing: value)
    }
  }

  private static func axValueCast(_ value: AnyObject) -> AXValue? {
    guard CFGetTypeID(value) == AXValueGetTypeID() else { return nil }
    return (value as! AXValue)
  }

  private static func isErrorSentinel(_ value: AnyObject) -> Bool {
    guard let axValue = axValueCast(value) else { return false }
    return AXValueGetType(axValue) == .axError
  }

  private static func isAXUIElement(_ value: AnyObject) -> Bool {
    CFGetTypeID(value) == AXUIElementGetTypeID()
  }

  // MARK: - AX error mapping

  /// Maps a non-success `AXError` onto the agent's wire error vocabulary:
  /// `kAXErrorCannotComplete` — messaging failed, or the target app is busy, unresponsive, or
  /// hit the messaging timeout — becomes `timeout`; every other failure becomes `ax.error`
  /// carrying the raw `AXError` code. `_ax_error` in `macos.py` always raises a plain
  /// `MacOSError` wrapping the code; the native agent has the richer two-code wire vocabulary
  /// `Protocol.swift` already documents, so this splits across it instead.
  private static func axAgentError(_ operation: String, _ error: AXError) -> AgentError {
    let code = error == .cannotComplete ? "timeout" : "ax.error"
    return AgentError(
      code: code, message: "\(operation) failed with AXError \(error.rawValue)",
      axError: Int(error.rawValue))
  }
}
