//
//  PythonBridge.swift
//  ResearchBot
//
//  Observable bridge to the Python backend.
//  - Spawns execute_pipeline.sh via Process() and parses the JSON contract.
//  - Talks to the local VertexProxy (http://localhost:8000) for the
//    interactive Graphify console (`graphify query` / `graphify path`).
//
//  Swift handles ONLY process management + HTTP — no business logic here.
//

import Foundation

// MARK: - Pipeline JSON Contract

/// Decoded payload from the Python backend's JSON envelope.
struct PipelineResult: Codable {
    let status: String
    let message: String
    let graphPath: String?
    let kbRoot: String?
    let phase: String?
    let synthesisPreview: String?
    let academicGapAnalysis: AcademicGapAnalysis?
    let sessionId: String?
    let sessionPath: String?

    enum CodingKeys: String, CodingKey {
        case status, message, phase
        case graphPath = "graph_path"
        case kbRoot = "kb_root"
        case synthesisPreview = "synthesis_preview"
        case academicGapAnalysis = "academic_gap_analysis"
        case sessionId = "session_id"
        case sessionPath = "session_path"
    }
}

// MARK: - Graph Terminal Response

struct GraphConsoleResponse: Codable {
    let ok: Bool
    let stdout: String?
    let error: String?
}

@MainActor
@Observable
final class PythonBridge {

    // MARK: - Published State

    var isRunning = false
    var progress: String = ""
    var errorMessage: String?
    var graphFilePath: String?
    var kbRoot: String?
    var synthesisPreview: String?
    var academicGapAnalysis: AcademicGapAnalysis?
    var sessionId: String?
    var sessionPath: String?

    /// Local FastAPI proxy that hosts the interactive graph endpoints.
    private let proxyBaseURL = URL(string: "http://localhost:8000")!

    // MARK: - Script Resolution

    private var scriptPath: String? {
        if let envPath = ProcessInfo.processInfo.environment["BRIDGE_SCRIPT_PATH"] {
            if FileManager.default.fileExists(atPath: envPath) { return envPath }
        }

        var current = URL(fileURLWithPath: Bundle.main.bundlePath)
        for _ in 0..<10 {
            let candidate = current.appendingPathComponent("execute_pipeline.sh").path
            if FileManager.default.fileExists(atPath: candidate) { return candidate }
            current = current.deletingLastPathComponent()
        }
        return nil
    }

    // MARK: - Pipeline Execution

    func runPipeline(idea: String, url: String = "") {
        guard !isRunning else { return }
        guard let script = scriptPath else {
            errorMessage = "Could not locate execute_pipeline.sh. Check repo structure."
            return
        }

        isRunning = true
        progress = "Initializing pipeline…\n"
        errorMessage = nil
        graphFilePath = nil
        kbRoot = nil
        synthesisPreview = nil
        academicGapAnalysis = nil
        sessionId = nil
        sessionPath = nil

        Task.detached(priority: .userInitiated) { [weak self] in
            await self?.executeProcess(script: script, idea: idea, url: url)
        }
    }

    nonisolated private func executeProcess(script: String, idea: String, url: String) async {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/bin/bash")
        var args = [script, "--idea", idea]
        let trimmedURL = url.trimmingCharacters(in: .whitespacesAndNewlines)
        if !trimmedURL.isEmpty {
            args += ["--url", trimmedURL]
        }
        process.arguments = args

        var env = ProcessInfo.processInfo.environment
        let homeDir = NSHomeDirectory()
        let localBin = "\(homeDir)/.local/bin"
        env["PATH"] = "\(localBin):/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:" + (env["PATH"] ?? "")
        process.environment = env

        let stdoutPipe = Pipe()
        process.standardOutput = stdoutPipe
        process.standardError = stdoutPipe

        stdoutPipe.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            guard !data.isEmpty, let line = String(data: data, encoding: .utf8) else { return }
            let bridge = self
            Task { @MainActor in
                bridge?.progress += line
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
        let fullOutput = await MainActor.run { self.progress }

        await MainActor.run {
            parseOutput(fullOutput, exitCode: process.terminationStatus)
            isRunning = false
        }
    }

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

        let jsonString = String(raw[startRange.upperBound..<endRange.lowerBound])
            .trimmingCharacters(in: .whitespacesAndNewlines)
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
                kbRoot = result.kbRoot
                synthesisPreview = result.synthesisPreview
                academicGapAnalysis = result.academicGapAnalysis
                sessionId = result.sessionId
                sessionPath = result.sessionPath
                errorMessage = nil
            }
        } catch {
            errorMessage = "JSON decode error: \(error.localizedDescription)\n\nRaw JSON was:\n\(jsonString)"
        }
    }

    // MARK: - Run-Scoped File Discovery

    /// List `.md` files inside a sub-folder of the currently active run.
    ///
    /// Resolves against `sessionPath` (preferred — every run is isolated under
    /// `runs/session_<TIMESTAMP>_<slug>/`) and falls back to `kbRoot` only if
    /// no session has been loaded yet. Returns an empty array when the
    /// sub-folder doesn't exist or no run is active. Results are sorted by
    /// filename (case-insensitive) for stable UI ordering.
    func listMarkdownFiles(in subfolder: String) -> [URL] {
        let base = sessionPath ?? kbRoot
        guard let base, !base.isEmpty else { return [] }
        return PythonBridge.listMarkdownFiles(in: subfolder, under: base)
    }

    /// Pure helper — scans `<root>/<subfolder>` and returns its `.md` files.
    /// Kept `static` so SwiftUI views and detached tasks can call it without
    /// touching the observable state.
    static func listMarkdownFiles(in subfolder: String, under root: String) -> [URL] {
        let folder = URL(fileURLWithPath: root).appendingPathComponent(subfolder)
        let fm = FileManager.default

        var isDir: ObjCBool = false
        guard fm.fileExists(atPath: folder.path, isDirectory: &isDir), isDir.boolValue else {
            return []
        }

        let entries: [URL]
        do {
            entries = try fm.contentsOfDirectory(
                at: folder,
                includingPropertiesForKeys: [.isRegularFileKey, .fileSizeKey],
                options: [.skipsHiddenFiles, .skipsPackageDescendants]
            )
        } catch {
            return []
        }

        return entries
            .filter { $0.pathExtension.lowercased() == "md" }
            .sorted { a, b in
                a.lastPathComponent.localizedCaseInsensitiveCompare(b.lastPathComponent) == .orderedAscending
            }
    }

    // MARK: - Historical Session Loading

    /// Load a previously-recorded session into the live observable state.
    /// Reads `graph.html` and the persisted `academic_gap_analysis.json`
    /// from `runs/session_<id>/` and populates `graphFilePath`,
    /// `academicGapAnalysis`, `sessionId`, and `sessionPath`.
    func loadHistoricalSession(_ session: HistorySession) {
        graphFilePath = session.graphHTMLPath
        kbRoot = session.kbRoot
        sessionId = session.id
        sessionPath = session.absolutePath
        synthesisPreview = nil
        errorMessage = nil

        let gapURL = URL(fileURLWithPath: session.absolutePath)
            .appendingPathComponent("academic_gap_analysis.json")

        if let data = try? Data(contentsOf: gapURL),
           let decoded = try? JSONDecoder().decode(AcademicGapAnalysis.self, from: data) {
            academicGapAnalysis = decoded
        } else {
            academicGapAnalysis = nil
        }
    }

    // MARK: - Interactive Graphify Console

    /// POST /api/graph/query — `graphify query <session> "<question>"`.
    func runGraphQuery(question: String) async -> Result<String, Error> {
        guard let sid = sessionId, !sid.isEmpty else {
            return .failure(GraphConsoleError.noActiveSession)
        }
        return await postConsole(
            path: "/api/graph/query",
            body: ["session_id": sid, "question": question]
        )
    }

    /// POST /api/graph/path — `graphify path <session> "<source>" "<target>"`.
    func runGraphPath(source: String, target: String) async -> Result<String, Error> {
        guard let sid = sessionId, !sid.isEmpty else {
            return .failure(GraphConsoleError.noActiveSession)
        }
        return await postConsole(
            path: "/api/graph/path",
            body: ["session_id": sid, "source": source, "target": target]
        )
    }

    private func postConsole(path: String, body: [String: String]) async -> Result<String, Error> {
        var request = URLRequest(url: proxyBaseURL.appendingPathComponent(path))
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.timeoutInterval = 300

        do {
            request.httpBody = try JSONSerialization.data(withJSONObject: body)
        } catch {
            return .failure(error)
        }

        do {
            let (data, _) = try await URLSession.shared.data(for: request)
            let decoded = try JSONDecoder().decode(GraphConsoleResponse.self, from: data)
            if decoded.ok, let stdout = decoded.stdout {
                return .success(stdout)
            }
            return .failure(GraphConsoleError.backend(decoded.error ?? "Unknown backend error"))
        } catch {
            return .failure(error)
        }
    }
}

enum GraphConsoleError: LocalizedError {
    case noActiveSession
    case backend(String)

    var errorDescription: String? {
        switch self {
        case .noActiveSession:
            return "No active research session. Run a pipeline or open a historical run first."
        case .backend(let msg):
            return msg
        }
    }
}
