import XCTest

@testable import macos_harness_agent

// MARK: - Assumed production API (per-connection element registry)
//
//   final class ElementRegistry {
//       init()
//       var generation: Int { get }
//       func register(_ element: AnyObject) -> Int
//       func resolve(_ handle: Int) -> AnyObject?
//       func reset()
//   }
//
// `AXUIElement` is a `CFTypeRef`, which bridges to `AnyObject` on Darwin, so plain `NSObject`
// instances stand in for real AX elements here without touching Accessibility at all — the
// registry's own bookkeeping (handle allocation, generation tracking, reset) is agnostic to
// what is actually stored.

final class ElementRegistryTests: XCTestCase {

  func testHandlesAreMonotonicAndDistinct() {
    let registry = ElementRegistry()
    let a = registry.register(NSObject())
    let b = registry.register(NSObject())
    let c = registry.register(NSObject())
    XCTAssertNotEqual(a, b)
    XCTAssertNotEqual(b, c)
    XCTAssertNotEqual(a, c)
    XCTAssertLessThan(a, b)
    XCTAssertLessThan(b, c)
  }

  func testResolveReturnsTheRegisteredIdentity() {
    let registry = ElementRegistry()
    let element = NSObject()
    let handle = registry.register(element)
    XCTAssertTrue(registry.resolve(handle) === element)
  }

  func testResolveDistinguishesDistinctRegistrations() {
    let registry = ElementRegistry()
    let first = NSObject()
    let second = NSObject()
    let firstHandle = registry.register(first)
    let secondHandle = registry.register(second)
    XCTAssertTrue(registry.resolve(firstHandle) === first)
    XCTAssertTrue(registry.resolve(secondHandle) === second)
    XCTAssertFalse(registry.resolve(firstHandle) === second)
  }

  func testResolveUnknownHandleReturnsNil() {
    let registry = ElementRegistry()
    let handle = registry.register(NSObject())
    XCTAssertNil(registry.resolve(handle + 1_000_000))
  }

  func testResetBumpsGenerationAndInvalidatesPriorHandles() {
    let registry = ElementRegistry()
    let before = registry.generation
    let stale = registry.register(NSObject())
    registry.reset()
    XCTAssertEqual(registry.generation, before + 1)
    XCTAssertNil(registry.resolve(stale), "a handle issued before reset must not resolve afterward")
  }

  func testHandlesNeverAliasAcrossAReset() {
    let registry = ElementRegistry()
    var issued = Set<Int>()
    for _ in 0..<5 {
      issued.insert(registry.register(NSObject()))
    }
    registry.reset()
    for _ in 0..<5 {
      let handle = registry.register(NSObject())
      XCTAssertFalse(
        issued.contains(handle), "a post-reset handle reused a pre-reset handle number")
      issued.insert(handle)
    }
  }

  func testMultipleResetsKeepAdvancingGenerationMonotonically() {
    let registry = ElementRegistry()
    let start = registry.generation
    registry.reset()
    registry.reset()
    registry.reset()
    XCTAssertEqual(registry.generation, start + 3)
  }

  func testResetOnAnEmptyRegistryIsHarmless() {
    let registry = ElementRegistry()
    let start = registry.generation
    registry.reset()
    XCTAssertEqual(registry.generation, start + 1)
    let handle = registry.register(NSObject())
    XCTAssertNotNil(registry.resolve(handle))
  }

  func testManyRegistrationsRemainDistinctWithoutCrashing() {
    let registry = ElementRegistry()
    var seen = Set<Int>()
    for _ in 0..<10_000 {
      let handle = registry.register(NSObject())
      XCTAssertFalse(seen.contains(handle))
      seen.insert(handle)
    }
    XCTAssertEqual(seen.count, 10_000)
  }
}
