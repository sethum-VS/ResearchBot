// swift-tools-version: 5.10
import PackageDescription

let package = Package(
    name: "ResearchGraph",
    platforms: [
        .macOS(.v14)
    ],
    products: [
        .executable(name: "ResearchGraph", targets: ["ResearchGraph"])
    ],
    targets: [
        .executableTarget(
            name: "ResearchGraph",
            path: "ResearchGraph"
        )
    ]
)
