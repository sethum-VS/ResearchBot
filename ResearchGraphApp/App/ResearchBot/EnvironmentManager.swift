//
//  EnvironmentManager.swift
//  ResearchBot
//
//  Manages user-supplied API keys in Application Support for public .dmg releases.
//

import Foundation

enum EnvironmentManager {

    static let appSupportFolderName = "AutonomousResearchGraph"

    /// `~/Library/Application Support/AutonomousResearchGraph/`
    static var applicationSupportDirectory: URL {
        let base = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first!
        return base.appendingPathComponent(appSupportFolderName, isDirectory: true)
    }

    static var envFileURL: URL {
        applicationSupportDirectory.appendingPathComponent(".env")
    }

    static func checkEnvironmentExists() -> Bool {
        FileManager.default.fileExists(atPath: envFileURL.path)
    }

    /// Writes `KEY="VALUE"` lines to Application Support `.env`.
    static func saveEnvironment(keys: [String: String]) throws {
        let fm = FileManager.default
        try fm.createDirectory(
            at: applicationSupportDirectory,
            withIntermediateDirectories: true
        )

        let lines = keys
            .sorted { $0.key.localizedCaseInsensitiveCompare($1.key) == .orderedAscending }
            .map { key, value in
                let escaped = value
                    .replacingOccurrences(of: "\\", with: "\\\\")
                    .replacingOccurrences(of: "\"", with: "\\\"")
                return "\(key)=\"\(escaped)\""
            }

        let content = lines.joined(separator: "\n") + "\n"
        try content.write(to: envFileURL, atomically: true, encoding: .utf8)
    }
}
