import XCTest

@testable import macos_harness_agent

// MARK: - Assumed production API (runtime slice)
//
//   final class AgentHandlers {
//       init()
//       func handle(_ request: WireRequest) -> WireResponse
//   }
//
// Every AX op other than ping/list_apps must fail closed with `permission.accessibility` when
// `AXIsProcessTrusted()` is false, which is guaranteed on a CI runner that has never granted
// Accessibility access — making that check deterministically testable without a live app, a
// trusted agent process, or any Accessibility prompt.

final class HandlerSeamTests: XCTestCase {

  func testUnknownOpReturnsUnsupportedOpAndPreservesId() {
    let handlers = AgentHandlers()
    let request = WireRequest(v: 1, id: 101, op: "definitely_not_a_real_op", params: .object([:]))
    let response = handlers.handle(request)
    XCTAssertEqual(response.id, 101)
    XCTAssertFalse(response.ok)
    XCTAssertEqual(response.error?.code, "unsupported_op")
  }

  func testHandlerSurvivesUnknownOpAndAnswersTheNextRequest() {
    // Simulates multiple requests over one connection: an unsupported op must not corrupt
    // or terminate the handler seam that the server drives per-connection.
    let handlers = AgentHandlers()
    let bad = handlers.handle(WireRequest(v: 1, id: 1, op: "bogus", params: .object([:])))
    XCTAssertFalse(bad.ok)
    XCTAssertEqual(bad.error?.code, "unsupported_op")

    let ping = handlers.handle(WireRequest(v: 1, id: 2, op: "ping", params: .object([:])))
    XCTAssertTrue(ping.ok, "the handler must keep answering after an unsupported op")
    XCTAssertEqual(ping.id, 2)
  }

  func testHandlerSurvivesRepeatedUnknownOpsAcrossManyRequests() {
    let handlers = AgentHandlers()
    for requestID in 1...20 {
      let response = handlers.handle(
        WireRequest(v: 1, id: requestID, op: "not_a_real_op", params: .object([:])))
      XCTAssertEqual(response.id, requestID)
      XCTAssertFalse(response.ok)
      XCTAssertEqual(response.error?.code, "unsupported_op")
    }
    let ping = handlers.handle(WireRequest(v: 1, id: 21, op: "ping", params: .object([:])))
    XCTAssertTrue(ping.ok, "the handler must remain usable after a long run of unsupported ops")
  }

  func testAxOpsFailClosedWithoutAccessibilityTrust() {
    let handlers = AgentHandlers(trustCheck: { false })
    let axOps: [(id: Int, op: String, params: JSONValue)] = [
      (10, "ax_query", .object(["app_pid": .number(1), "limit": .number(2)])),
      (11, "ax_press", .object(["app_pid": .number(1), "limit": .number(2)])),
      (
        12, "ax_element_get",
        .object(["handle": .number(1), "attributes": .array([.string("title")])])
      ),
      (
        13, "ax_element_set",
        .object(["handle": .number(1), "attribute": .string("value"), "value": .string("x")])
      ),
      (14, "ax_element_perform", .object(["handle": .number(1), "action": .string("AXPress")])),
    ]
    for (id, op, params) in axOps {
      let response = handlers.handle(WireRequest(v: 1, id: id, op: op, params: params))
      XCTAssertFalse(response.ok, "\(op) must fail without Accessibility trust")
      XCTAssertEqual(
        response.error?.code, "permission.accessibility",
        "\(op) must report permission.accessibility on an untrusted CI runner"
      )
      XCTAssertEqual(response.id, id)
    }
  }

  func testPingNeverRequiresAccessibilityTrust() {
    let handlers = AgentHandlers()
    let response = handlers.handle(WireRequest(v: 1, id: 20, op: "ping", params: .object([:])))
    XCTAssertTrue(response.ok)
    XCTAssertNotEqual(response.error?.code, "permission.accessibility")
  }

  func testPingResultCarriesProtocolMajorOne() throws {
    let handlers = AgentHandlers()
    let response = handlers.handle(WireRequest(v: 1, id: 22, op: "ping", params: .object([:])))
    XCTAssertTrue(response.ok)
    guard case .object(let result)? = response.result else {
      return XCTFail("expected ping's result to decode as a JSON object")
    }
    guard case .number(let protocolVersion)? = result["protocol"] else {
      return XCTFail("expected ping result to carry a numeric protocol field")
    }
    XCTAssertEqual(protocolVersion, 1)
  }

  // MARK: - Request bounds (ax_query / ax_press / ax_element_get / ax_element_set)
  //
  // Every test below runs with `trustCheck: { true }` so a request that survives this file's
  // own bounds validation proceeds into `AXExecutor`, whose *own* trust check always calls
  // the real, non-injectable `AXIsProcessTrusted()` — guaranteed `false` on a CI runner that
  // has never granted Accessibility access (see the file-level doc comment above). That makes
  // `permission.accessibility` proof a request passed this file's bounds gate, and
  // `bad_request` proof it did not, without ever touching real AX state or a live target pid.

  private func axQueryResponse(_ extraParams: [String: JSONValue]) -> WireResponse {
    var params = extraParams
    params["app_pid"] = params["app_pid"] ?? .number(1)
    let handlers = AgentHandlers(trustCheck: { true })
    return handlers.handle(WireRequest(v: 1, id: 1, op: "ax_query", params: .object(params)))
  }

  func testQueryAcceptsMaxNodesAtTheCeiling() {
    let response = axQueryResponse(["max_nodes": .number(5000)])
    XCTAssertNotEqual(
      response.error?.code, "bad_request",
      "max_nodes at the 5000 ceiling must pass bounds validation")
  }

  func testQueryRejectsMaxNodesAboveTheCeiling() {
    let response = axQueryResponse(["max_nodes": .number(5001)])
    XCTAssertEqual(response.error?.code, "bad_request")
  }

  func testQueryRejectsNonPositiveMaxNodes() {
    let outOfRangeMaxNodes: [Double] = [0, -1]
    for value in outOfRangeMaxNodes {
      let response = axQueryResponse(["max_nodes": .number(value)])
      XCTAssertEqual(response.error?.code, "bad_request", "max_nodes \(value) must be rejected")
    }
  }

  func testQueryAcceptsLimitAtTheCeiling() {
    let response = axQueryResponse(["limit": .number(1000)])
    XCTAssertNotEqual(response.error?.code, "bad_request")
  }

  func testQueryRejectsLimitAboveTheCeiling() {
    let response = axQueryResponse(["limit": .number(1001)])
    XCTAssertEqual(response.error?.code, "bad_request")
  }

  func testQueryAcceptsNegativeLimitMappedToMaxNodes() {
    let response = axQueryResponse(["limit": .number(-1), "max_nodes": .number(2000)])
    XCTAssertNotEqual(
      response.error?.code, "bad_request",
      "a negative limit maps to max_nodes rather than being rejected")
  }

  func testQueryAcceptsZeroLimit() {
    // Matches AXExecutor.boundedSearch's own semantics: limit 0 is a valid "return nothing"
    // request, not an error.
    let response = axQueryResponse(["limit": .number(0)])
    XCTAssertNotEqual(response.error?.code, "bad_request")
  }

  func testQueryRejectsOversizedAttributesArray() {
    let tooMany = (0..<65).map { JSONValue.string("AXAttr\($0)") }
    let response = axQueryResponse(["attributes": .array(tooMany)])
    XCTAssertEqual(response.error?.code, "bad_request")
  }

  func testQueryAcceptsAttributesArrayAtTheCountCeiling() {
    let exactlyMax = (0..<64).map { JSONValue.string("AXAttr\($0)") }
    let response = axQueryResponse(["attributes": .array(exactlyMax)])
    XCTAssertNotEqual(response.error?.code, "bad_request")
  }

  func testQueryRejectsNonStringAttributesEntry() {
    let response = axQueryResponse(["attributes": .array([.string("AXTitle"), .number(1)])])
    XCTAssertEqual(response.error?.code, "bad_request")
  }

  func testQueryRejectsOversizedAttributeName() {
    let overlong = String(repeating: "x", count: 257)
    let response = axQueryResponse(["attributes": .array([.string(overlong)])])
    XCTAssertEqual(response.error?.code, "bad_request")
  }

  func testQueryAcceptsAttributeNameAtTheByteCeiling() {
    let exactlyMax = String(repeating: "x", count: 256)
    let response = axQueryResponse(["attributes": .array([.string(exactlyMax)])])
    XCTAssertNotEqual(response.error?.code, "bad_request")
  }

  func testQueryRejectsOversizedText() {
    let overlong = String(repeating: "x", count: 1024 * 1024 + 1)
    let response = axQueryResponse(["text": .string(overlong)])
    XCTAssertEqual(response.error?.code, "bad_request")
  }

  func testQueryAcceptsTextAtTheByteCeiling() {
    let exactlyMax = String(repeating: "x", count: 1024 * 1024)
    let response = axQueryResponse(["text": .string(exactlyMax)])
    XCTAssertNotEqual(response.error?.code, "bad_request")
  }

  func testQueryRejectsOutOfRangeMessagingTimeout() {
    let outOfRangeTimeouts: [Double] = [0, 0.009, 6.001, -1]
    for value in outOfRangeTimeouts {
      let response = axQueryResponse(["messaging_timeout": .number(value)])
      XCTAssertEqual(
        response.error?.code, "bad_request", "messaging_timeout \(value) must be rejected")
    }
  }

  func testQueryRejectsNonFiniteMessagingTimeout() {
    for value in [Double.nan, .infinity, -Double.infinity] {
      let response = axQueryResponse(["messaging_timeout": .number(value)])
      XCTAssertEqual(response.error?.code, "bad_request")
    }
  }

  func testQueryAcceptsMessagingTimeoutWithinRange() {
    let response = axQueryResponse(["messaging_timeout": .number(3.0)])
    XCTAssertNotEqual(response.error?.code, "bad_request")
  }

  func testElementGetRejectsOversizedAttributesArray() {
    let handlers = AgentHandlers(trustCheck: { true })
    let tooMany = (0..<65).map { JSONValue.string("AXAttr\($0)") }
    let response = handlers.handle(
      WireRequest(
        v: 1, id: 1, op: "ax_element_get",
        params: .object(["handle": .number(1), "attributes": .array(tooMany)])))
    XCTAssertEqual(response.error?.code, "bad_request")
  }

  func testElementSetRejectsOversizedAttributeName() {
    let handlers = AgentHandlers(trustCheck: { true })
    let overlong = String(repeating: "x", count: 257)
    let response = handlers.handle(
      WireRequest(
        v: 1, id: 1, op: "ax_element_set",
        params: .object([
          "handle": .number(1), "attribute": .string(overlong), "value": .string("x"),
        ])))
    XCTAssertEqual(response.error?.code, "bad_request")
  }

  func testElementSetAcceptsAttributeNameAtTheByteCeiling() {
    let handlers = AgentHandlers(trustCheck: { true })
    let exactlyMax = String(repeating: "x", count: 256)
    let response = handlers.handle(
      WireRequest(
        v: 1, id: 1, op: "ax_element_set",
        params: .object([
          "handle": .number(1), "attribute": .string(exactlyMax), "value": .string("x"),
        ])))
    XCTAssertNotEqual(response.error?.code, "bad_request")
  }
}
