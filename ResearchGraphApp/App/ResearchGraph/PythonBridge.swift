//
//  PythonBridge.swift
//  ResearchBot
//
//  ObservableObject that executes execute_pipeline.sh via Process().
//  Captures stdout, parses the JSON bridging contract, and exposes
//  typed state for SwiftUI consumption.
//
//  Swift handles ONLY process management — no business logic here.
//

import Foundation

/// Decoded payload from the Python backend's JSON contract.
struct PipelineResult: Codable {
    let status: String
    let message: String
    let graphPath: String?
    let phase: String?
    let synthesisPreview: String?

    enum CodingKeys: String, CodingKey {
        case status, message, phase
        case graphPath = "graph_path"
        case synthesisPreview = "synthesis_preview"
    }
}

@Observable
final class PythonBridge {

    // MARK: - Published State

    var isRunning = false
    var progress: String = ""
    var errorMessage: String?
    var graphFilePath: String?
    var synthesisPreview: String?

    // MARK: - Script Resolution

    /// Locates execute_pipeline.sh by walking up from the app bundle.
    private var scriptPath: String? {
        // 1. Environment variable override (useful in Xcode scheme)
        if let envPath = ProcessInfo.processInfo.environment["BRIDGE_SCRIPT_PATH"] {
            if FileManager.default.fileExists(atPath: envPath) { return envPath }
        }

        // 2. Walk up from the bundle to find the repo root
        var current = URL(fileURLWithPath: Bundle.main.bundlePath)
        for _ in 0..<10 {
            let candidate = current.appendingPathComponent("execute_pipeline.sh").path
            if FileManager.default.fileExists(atPath: candidate) { return candidate }
            current = current.deletingLastPathComponent()
        }
        return nil
    }

    // MARK: - Execution

    /// Runs the full pipeline asynchronously.
    func runPipeline(idea: String) {
        guard !isRunning else { return }
        guard let script = scriptPath else {
            errorMessage = "Could not locate execute_pipeline.sh. Check repo structure."
            return
        }

        isRunning = true
        progress = "Initializing pipeline…\n"
        errorMessage = nil
        graphFilePath = nil
        synthesisPreview = nil

        Task.detached(priority: .userInitiated) { [weak self] in
            await self?.executeProcess(script: script, idea: idea)
        }
    }

    private func executeProcess(script: String, idea: String) async {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/bin/bash")
        process.arguments = [script, "--idea", idea]

        // Inherit the user's shell environment for GCP ADC, PATH, etc.
        var env = ProcessInfo.processInfo.environment
        env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:" + (env["PATH"] ?? "")
        process.environment = env

        let stdoutPipe = Pipe()
        let stderrPipe = Pipe()
        process.standardOutput = stdoutPipe
        process.standardError = stderrPipe

        // Stream stdout lines for live progress
        stdoutPipe.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            guard !data.isEmpty, let line = String(data: data, encoding: .utf8) else { return }
            Task { @MainActor in
                self?.progress += line
            }
        }

        do {
            try process.run()
            process.waitUntilExit()
        } catch {
            await MainActor.run {
                self.errorMessage = "Failed to launch process: \(error.localizedDescription)"
                self.isRunning = false
            }
            return
        }

        stdoutPipe.fileHandleForReading.readabilityHandler = nil

        let fullOutput = progress

        await MainActor.run {
            parseOutput(fullOutput, exitCode: process.terminationStatus)
            isRunning = false
        }
    }

    /// Extract the JSON object from stdout using the PIPELINE_RESULT markers.
    private func parseOutput(_ raw: String, exitCode: Int32) {
        let startMarker = "---PIPELINE_RESULT_START---"
        let endMarker = "---PIPELINE_RESULT_END---"

        guard let startRange = raw.range(of: startMarker),
              let endRange = raw.range(of: endMarker),
              startRange.upperBound < endRange.lowerBound else {
            if exitCode != 0 {
                errorMessage = "Pipeline failed (exit \(exitCode)). Result markers not found."
            }
            return
        }

        let jsonString = String(raw[startRange.upperBound..<endRange.lowerBound]).trimmingCharacters(in: .whitespacesAndNewlines)
        guard let jsonData = jsonString.data(using: .utf8) else {
            errorMessage = "Failed to convert result string to data."
            return
        }

        do {
            let result = try JSONDecoder().decode(PipelineResult.self, from: jsonData)

            if result.status == "error" {
                errorMessage = result.message
            } else {
                graphFilePath = result.graphPath
                synthesisPreview = result.synthesisPreview
                errorMessage = nil
            }
        } catch {
            errorMessage = "JSON decode error: \(error.localizedDescription)\n\nRaw JSON was:\n\(jsonString)"
        }
    }
}
