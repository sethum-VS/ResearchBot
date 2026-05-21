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

    private var researchGraphAppRoot: URL? {
        if let envPath = ProcessInfo.processInfo.environment["BRIDGE_SCRIPT_PATH"] {
            let url = URL(fileURLWithPath: envPath).deletingLastPathComponent()
            if FileManager.default.fileExists(atPath: url.path) { return url }
        }

        var current = URL(fileURLWithPath: Bundle.main.bundlePath)
        for _ in 0..<10 {
            let marker = current.appendingPathComponent("execute_pipeline.sh")
            if FileManager.default.fileExists(atPath: marker.path) { return current }
            current = current.deletingLastPathComponent()
        }
        return nil
    }

    private var scriptPath: String? {
        guard let root = researchGraphAppRoot else { return nil }
        let path = root.appendingPathComponent("execute_pipeline.sh").path
        return FileManager.default.fileExists(atPath: path) ? path : nil
    }

    private var ensureProxyScriptPath: String? {
        guard let root = researchGraphAppRoot else { return nil }
        let path = root.appendingPathComponent("ensure_vertex_proxy.sh").path
        return FileManager.default.fileExists(atPath: path) ? path : nil
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
        guard let base = sessionPath, !base.isEmpty else { return [] }
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

    /// Starts VertexProxy when opening a historical session or after pipeline teardown.
    private func ensureVertexProxyRunning() async -> Result<Void, Error> {
        if await isVertexProxyReachable() {
            return .success(())
        }

        guard let script = ensureProxyScriptPath else {
            return .failure(GraphConsoleError.proxyNotRunning)
        }

        let launched: Result<Void, Error> = await withCheckedContinuation { continuation in
            let process = Process()
            process.executableURL = URL(fileURLWithPath: "/bin/bash")
            process.arguments = [script]

            var env = ProcessInfo.processInfo.environment
            let homeDir = NSHomeDirectory()
            let localBin = "\(homeDir)/.local/bin"
            env["PATH"] = "\(localBin):/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:" + (env["PATH"] ?? "")
            process.environment = env

            let pipe = Pipe()
            process.standardOutput = pipe
            process.standardError = pipe

            process.terminationHandler = { proc in
                if proc.terminationStatus == 0 {
                    continuation.resume(returning: .success(()))
                } else {
                    let detail = String(data: pipe.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8)?
                        .trimmingCharacters(in: .whitespacesAndNewlines)
                    let msg = detail?.isEmpty == false ? detail! : "exit \(proc.terminationStatus)"
                    continuation.resume(returning: .failure(GraphConsoleError.proxyStartFailed(msg)))
                }
            }

            do {
                try process.run()
            } catch {
                continuation.resume(returning: .failure(error))
            }
        }

        switch launched {
        case .failure(let err):
            return .failure(err)
        case .success:
            if await isVertexProxyReachable() {
                return .success(())
            }
            return .failure(GraphConsoleError.proxyNotRunning)
        }
    }

    private func isVertexProxyReachable() async -> Bool {
        var request = URLRequest(url: proxyBaseURL.appendingPathComponent("api/graph/sessions"))
        request.httpMethod = "GET"
        request.timeoutInterval = 3

        do {
            let (_, response) = try await URLSession.shared.data(for: request)
            guard let http = response as? HTTPURLResponse else { return false }
            return (200...299).contains(http.statusCode)
        } catch {
            return false
        }
    }

    private func consoleRequestURL(path: String) -> URL {
        let trimmed = path.hasPrefix("/") ? String(path.dropFirst()) : path
        return proxyBaseURL.appendingPathComponent(trimmed)
    }

    private func postConsole(path: String, body: [String: String]) async -> Result<String, Error> {
        switch await ensureVertexProxyRunning() {
        case .failure(let err):
            return .failure(err)
        case .success:
            break
        }

        var request = URLRequest(url: consoleRequestURL(path: path))
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.timeoutInterval = 300

        do {
            request.httpBody = try JSONSerialization.data(withJSONObject: body)
        } catch {
            return .failure(error)
        }

        do {
            let (data, response) = try await URLSession.shared.data(for: request)
            if let http = response as? HTTPURLResponse, !(200...299).contains(http.statusCode) {
                if let decoded = try? JSONDecoder().decode(GraphConsoleResponse.self, from: data),
                   let err = decoded.error {
                    return .failure(GraphConsoleError.backend(err))
                }
                return .failure(GraphConsoleError.backend("HTTP \(http.statusCode)"))
            }

            let decoded = try JSONDecoder().decode(GraphConsoleResponse.self, from: data)
            if decoded.ok, let stdout = decoded.stdout {
                return .success(stdout)
            }
            return .failure(GraphConsoleError.backend(decoded.error ?? "Unknown backend error"))
        } catch {
            if let urlError = error as? URLError,
               [.cannotConnectToHost, .cannotFindHost, .networkConnectionLost, .timedOut]
                   .contains(urlError.code) {
                return .failure(GraphConsoleError.proxyNotRunning)
            }
            return .failure(error)
        }
    }
}

enum GraphConsoleError: LocalizedError {
    case noActiveSession
    case proxyNotRunning
    case proxyStartFailed(String)
    case backend(String)

    var errorDescription: String? {
        switch self {
        case .noActiveSession:
            return "No active research session. Run a pipeline or open a historical run first."
        case .proxyNotRunning:
            return "VertexProxy is not running on localhost:8000. Run a pipeline once or execute ensure_vertex_proxy.sh from ResearchGraphApp."
        case .proxyStartFailed(let detail):
            return "Failed to start VertexProxy: \(detail)"
        case .backend(let msg):
            return msg
        }
    }
}
