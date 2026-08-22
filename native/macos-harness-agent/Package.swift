// swift-tools-version: 5.9
// The swift-tools-version declares the minimum version of Swift required to build this package.

import PackageDescription

let package = Package(
    name: "macos-harness-agent",
    platforms: [
        .macOS(.v13)
    ],
    targets: [
        // The agent process itself: NDJSON-over-UDS server, AX execution, and the
        // shared wire protocol. Depends only on Foundation, AppKit,
        // ApplicationServices, and Darwin — no third-party packages.
        //
        // Executable targets are directly `@testable import`able by a sibling test
        // target, so this single target both builds `macos-harness-agent` and backs
        // every test in `AgentTests`.
        .executableTarget(
            name: "macos-harness-agent",
            path: "Sources/macos-harness-agent"
        ),
        .testTarget(
            name: "AgentTests",
            dependencies: [
                .target(name: "macos-harness-agent")
            ],
            path: "Tests/AgentTests"
        ),
    ]
)
