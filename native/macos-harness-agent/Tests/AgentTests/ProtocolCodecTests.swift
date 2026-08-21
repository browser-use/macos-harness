import XCTest

@testable import macos_harness_agent

// MARK: - Assumed production API (Protocol.swift, owned by the Foundation slice)
//
//   struct WireRequest: Codable { let v: Int; let id: Int; let op: String; let params: JSONValue }
//   struct WireError: Codable {
//       let code: String; let message: String; let axError: Int?
//       enum CodingKeys: String, CodingKey { case code, message; case axError = "ax_error" }
//   }
//   struct WireResponse: Codable { let id: Int; let ok: Bool; let result: JSONValue?; let error: WireError? }
//   enum JSONValue: Codable {
//       case null, bool(Bool), number(Double), string(String)
//       case array([JSONValue]), object([String: JSONValue])
//   }
//   enum ProtocolCodec {
//       static func decode(_ payload: Data) -> Result<WireRequest, WireResponse>
//   }
//
// These tests assert wire-level JSON shape (keys/values), not Swift source text, so they stay
// meaningful even if the concrete property names above are renamed during integration.

final class ProtocolCodecTests: XCTestCase {

  // MARK: Request decoding

  func testWireRequestDecodesFullEnvelope() throws {
    let json = Data(#"{"v":1,"id":7,"op":"ping","params":{}}"#.utf8)
    let request = try JSONDecoder().decode(WireRequest.self, from: json)
    XCTAssertEqual(request.v, 1)
    XCTAssertEqual(request.id, 7)
    XCTAssertEqual(request.op, "ping")
    guard case .object(let params) = request.params else {
      return XCTFail("expected params to decode as a JSON object")
    }
    XCTAssertTrue(params.isEmpty)
  }

  func testWireRequestDecodesNestedParams() throws {
    let json = Data(
      #"{"v":1,"id":8,"op":"ax_query","params":{"app_pid":123,"limit":2,"visible_only":true,"attributes":["title","role"]}}"#
        .utf8)
    let request = try JSONDecoder().decode(WireRequest.self, from: json)
    guard case .object(let params) = request.params else {
      return XCTFail("expected params to decode as a JSON object")
    }
    guard case .number(let pid)? = params["app_pid"] else {
      return XCTFail("expected app_pid to decode as a JSON number")
    }
    XCTAssertEqual(pid, 123)
    guard case .array(let attrs)? = params["attributes"] else {
      return XCTFail("expected attributes to decode as a JSON array")
    }
    XCTAssertEqual(attrs.count, 2)
  }

  // MARK: Response encoding round trips (wire-shape assertions, not source text)

  func testSuccessResponseOmitsErrorKeyOnEncode() throws {
    let response = WireResponse(id: 3, ok: true, result: .object(["apps": .array([])]), error: nil)
    let data = try JSONEncoder().encode(response)
    let object = try XCTUnwrap(try JSONSerialization.jsonObject(with: data) as? [String: Any])
    XCTAssertEqual(object["id"] as? Int, 3)
    XCTAssertEqual(object["ok"] as? Bool, true)
    XCTAssertNotNil(object["result"])
    XCTAssertNil(object["error"], "ok:true envelopes must not carry an error key")
  }

  func testFailureResponseOmitsResultKeyOnEncode() throws {
    let error = WireError(code: "app.not_found", message: "no match", axError: nil)
    let response = WireResponse(id: 9, ok: false, result: nil, error: error)
    let data = try JSONEncoder().encode(response)
    let object = try XCTUnwrap(try JSONSerialization.jsonObject(with: data) as? [String: Any])
    XCTAssertEqual(object["id"] as? Int, 9)
    XCTAssertEqual(object["ok"] as? Bool, false)
    XCTAssertNil(object["result"], "ok:false envelopes must not carry a result key")
    let wireError = try XCTUnwrap(object["error"] as? [String: Any])
    XCTAssertEqual(wireError["code"] as? String, "app.not_found")
    XCTAssertNil(wireError["ax_error"], "ax_error must be omitted when absent")
  }

  func testAxErrorSurvivesEncodeDecodeUnderItsWireKey() throws {
    let error = WireError(code: "ax.error", message: "kAXErrorFailure", axError: -25200)
    let response = WireResponse(id: 12, ok: false, result: nil, error: error)
    let data = try JSONEncoder().encode(response)
    let object = try XCTUnwrap(try JSONSerialization.jsonObject(with: data) as? [String: Any])
    let wireError = try XCTUnwrap(object["error"] as? [String: Any])
    XCTAssertEqual(
      wireError["ax_error"] as? Int, -25200, "ax_error must round trip under its literal wire key")

    let decoded = try JSONDecoder().decode(WireResponse.self, from: data)
    XCTAssertEqual(decoded.error?.axError, -25200)
    XCTAssertEqual(decoded.id, 12)
  }

  func testWireRequestRoundTripsThroughEncodeDecode() throws {
    let request = WireRequest(v: 1, id: 44, op: "list_apps", params: .object([:]))
    let data = try JSONEncoder().encode(request)
    let decoded = try JSONDecoder().decode(WireRequest.self, from: data)
    XCTAssertEqual(decoded.v, request.v)
    XCTAssertEqual(decoded.id, request.id)
    XCTAssertEqual(decoded.op, request.op)
  }

  // MARK: Malformed line decoding (bad_request, id recovered where possible)

  func testDecodeRecoversIdFromStructurallyIncompleteRequest() {
    let payload = Data(#"{"id":42}"#.utf8)
    switch ProtocolCodec.decode(payload) {
    case .success:
      XCTFail("a request missing v/op/params must not decode successfully")
    case .failure(let response):
      XCTAssertEqual(
        response.id, 42, "a recoverable id must be preserved on the bad_request envelope")
      XCTAssertFalse(response.ok)
      XCTAssertEqual(response.error?.code, "bad_request")
    }
  }

  func testDecodeRejectsNonObjectJSON() {
    let payload = Data(#"[1,2,3]"#.utf8)
    switch ProtocolCodec.decode(payload) {
    case .success:
      XCTFail("a JSON array is not a valid request envelope")
    case .failure(let response):
      XCTAssertFalse(response.ok)
      XCTAssertEqual(response.error?.code, "bad_request")
    }
  }

  func testDecodeRejectsGarbageBytesWithoutCrashing() {
    let payload = Data("not json at all".utf8)
    switch ProtocolCodec.decode(payload) {
    case .success:
      XCTFail("garbage bytes are not a valid request envelope")
    case .failure(let response):
      XCTAssertFalse(response.ok)
      XCTAssertEqual(response.error?.code, "bad_request")
    }
  }

  func testDecodeAcceptsWellFormedRequest() {
    let payload = Data(#"{"v":1,"id":5,"op":"ping","params":{}}"#.utf8)
    switch ProtocolCodec.decode(payload) {
    case .success(let request):
      XCTAssertEqual(request.id, 5)
      XCTAssertEqual(request.op, "ping")
    case .failure(let response):
      XCTFail("well-formed request rejected: \(response.error?.message ?? "<nil>")")
    }
  }

  // MARK: Malformed id recovery (protocol-DoS regression: Int(Double) must never trap)
  //
  // `Int(someDouble)` traps at runtime when `someDouble` cannot be represented as an `Int` —
  // a single line like `{"id":1e300}` used to crash the whole agent process, taking down
  // every other connection with it. `ProtocolCodec.decode` must recover a safe `id` of `0`
  // for any id it cannot represent exactly, never trap, and never wrap/truncate to a
  // plausible-looking but wrong value.

  func testDecodeRecoversFromAstronomicallyLargeIdWithoutCrashing() {
    let payload = Data(#"{"id":1e300}"#.utf8)
    switch ProtocolCodec.decode(payload) {
    case .success:
      XCTFail("a request missing v/op/params must not decode successfully")
    case .failure(let response):
      XCTAssertEqual(
        response.id, 0,
        "an id too large to represent as Int must fall back to 0, not crash or wrap")
      XCTAssertFalse(response.ok)
      XCTAssertEqual(response.error?.code, "bad_request")
    }
  }

  func testDecodeRecoversFromNegativeAstronomicallyLargeIdWithoutCrashing() {
    let payload = Data(#"{"id":-1e300}"#.utf8)
    switch ProtocolCodec.decode(payload) {
    case .success:
      XCTFail("a request missing v/op/params must not decode successfully")
    case .failure(let response):
      XCTAssertEqual(response.id, 0)
      XCTAssertEqual(response.error?.code, "bad_request")
    }
  }

  func testDecodeRecoversFromNonIntegralIdWithoutSilentTruncation() {
    // 4.5 fits comfortably within Int's range, so a bare `Int(4.5)` would silently truncate
    // to 4 instead of trapping — still wrong, since 4 was never the id the caller sent.
    let payload = Data(#"{"id":4.5}"#.utf8)
    switch ProtocolCodec.decode(payload) {
    case .success:
      XCTFail("a request missing v/op/params must not decode successfully")
    case .failure(let response):
      XCTAssertEqual(response.id, 0, "a non-integral id must fall back to 0, not truncate")
      XCTAssertEqual(response.error?.code, "bad_request")
    }
  }

  func testDecoderSurvivesAnAstronomicallyLargeIdAndAnswersTheNextRequest() {
    // The regression this guards against was a process-level crash: proving the decoder is
    // still usable for an unrelated, well-formed request right after the malformed one is
    // the whole point, not just that this one call returns instead of trapping.
    let poison = Data(#"{"id":1e300}"#.utf8)
    guard case .failure = ProtocolCodec.decode(poison) else {
      return XCTFail("a request missing v/op/params must not decode successfully")
    }

    let wellFormed = Data(#"{"v":1,"id":9,"op":"ping","params":{}}"#.utf8)
    switch ProtocolCodec.decode(wellFormed) {
    case .success(let request):
      XCTAssertEqual(request.id, 9)
      XCTAssertEqual(request.op, "ping")
    case .failure(let response):
      XCTFail("well-formed request rejected: \(response.error?.message ?? "<nil>")")
    }
  }
}
