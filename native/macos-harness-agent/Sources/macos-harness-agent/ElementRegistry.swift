import Foundation

/// Per-connection registry mapping opaque integer handles to retained AX element references
/// (or, in tests, plain `AnyObject` stand-ins).
///
/// Handles are minted from a monotonically increasing counter that is never rewound, so a
/// handle issued before `reset()` can never alias a handle issued afterward even though
/// `reset()` discards every previously registered element and bumps `generation`. Storage is
/// hard-bounded: once the registry holds `maxStoredElements` live registrations, registering
/// another element evicts the oldest surviving one (FIFO) first, so a long-lived or abusive
/// connection cannot grow the registry without bound.
final class ElementRegistry {

  /// Hard cap on live registrations at any one time. Well above the size of any single AX
  /// snapshot or search result set; once reached, the oldest live entry is evicted before a
  /// new one is stored.
  static let maxStoredElements = 50_000

  /// Bumped on every `reset()`; never decreases. Consumers use this to detect that the
  /// element namespace underneath a connection has been invalidated (e.g. after a fresh
  /// `ax_query`/`ax_search`).
  private(set) var generation: Int = 0

  private var nextHandle: Int = 1
  private var elements: [Int: AnyObject] = [:]

  /// FIFO order of live handles, oldest first. `orderHead` marks the first entry that has not
  /// yet been evicted; entries before it are stale and are periodically compacted away so this
  /// array never grows without bound relative to `maxStoredElements`.
  private var insertionOrder: [Int] = []
  private var orderHead: Int = 0

  init() {}

  /// Registers `element`, returning a fresh handle strictly greater than every handle issued
  /// so far (within this registry's lifetime, across resets).
  func register(_ element: AnyObject) -> Int {
    evictOldestIfAtCapacity()

    let handle = nextHandle
    nextHandle += 1

    elements[handle] = element
    insertionOrder.append(handle)
    compactOrderIfWorthwhile()

    return handle
  }

  /// Resolves `handle` to the element it was registered with, or `nil` if the handle is
  /// unknown, was evicted, or predates the most recent `reset()`.
  func resolve(_ handle: Int) -> AnyObject? {
    elements[handle]
  }

  /// Invalidates every currently registered handle and bumps `generation`. The handle counter
  /// itself is left untouched so future handles never alias ones issued before this call.
  func reset() {
    generation += 1
    elements.removeAll()
    insertionOrder.removeAll()
    orderHead = 0
  }

  private func evictOldestIfAtCapacity() {
    while elements.count >= Self.maxStoredElements, orderHead < insertionOrder.count {
      let oldest = insertionOrder[orderHead]
      orderHead += 1
      elements.removeValue(forKey: oldest)
    }
  }

  /// Drops the consumed prefix of `insertionOrder` once it accounts for a meaningful share of
  /// the array, keeping eviction O(1) amortized instead of letting the backing array grow
  /// forever on a long-lived, high-churn connection.
  private func compactOrderIfWorthwhile() {
    guard orderHead >= 4_096, orderHead * 2 >= insertionOrder.count else { return }
    insertionOrder.removeFirst(orderHead)
    orderHead = 0
  }
}
