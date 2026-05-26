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

// MARK: - Google Workspace Export Contract

struct WorkspaceExportResult: Codable {
    let status: String
    let message: String
    let masterDocumentURL: String?
    let topicDocumentURL: String?
    let proposalDocumentURL: String?
    let topicFolderURL: String?
    let sessionId: String?

    enum CodingKeys: String, CodingKey {
        case status, message
        case masterDocumentURL = "master_document_url"
        case topicDocumentURL = "topic_document_url"
        case proposalDocumentURL = "proposal_document_url"
        case topicFolderURL = "topic_folder_url"
        case sessionId = "session_id"
    }
}

// MARK: - Proposal Generation Contract

struct ProposalResult: Codable, Identifiable, Hashable {
    var id: String { proposalId ?? UUID().uuidString }

    let status: String
    let message: String
    let proposalPath: String?
    let sessionId: String?
    let scopedQuery: String?
    let matchedPaperCount: Int?
    let proposalId: String?

    enum CodingKeys: String, CodingKey {
        case status, message
        case proposalPath = "proposal_path"
        case sessionId = "session_id"
        case scopedQuery = "scoped_query"
        case matchedPaperCount = "matched_paper_count"
        case proposalId = "proposal_id"
    }
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

    var isExportingWorkspace = false
    var workspaceExportMessage: String?
    var workspaceExportError: String?
    var masterDocumentURL: String?

    // Proposal generation state
    var isGeneratingProposal = false
    var proposalProgress: String = ""
    var proposalResult: ProposalResult?
    var proposalError: String?
    var isExportingProposal = false
    var proposalExportMessage: String?
    var proposalExportError: String?
    var proposalDocumentURL: String?

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

    // MARK: - Application Support (`.env`, OAuth token)

    static var googleAppSupportDirectory: URL {
        EnvironmentManager.applicationSupportDirectory
    }

    static var googleTokenFileURL: URL {
        googleAppSupportDirectory.appendingPathComponent("token.json")
    }

    /// Paths and OAuth credentials passed to every backend `Process()`.
    nonisolated private static func applyBackendDeploymentPaths(to env: inout [String: String]) {
        env["APP_SUPPORT_DIR"] = EnvironmentManager.applicationSupportDirectory.path

        if let resourcePath = Bundle.main.resourcePath {
            env["APP_BUNDLE_DIR"] = resourcePath
            let bundledCreds = URL(fileURLWithPath: resourcePath)
                .appendingPathComponent("credentials.json")
            if FileManager.default.fileExists(atPath: bundledCreds.path) {
                env["RESEARCHBOT_OAUTH_CREDENTIALS"] = bundledCreds.path
            }
        }
    }

    static var hasGoogleOAuthToken: Bool {
        FileManager.default.fileExists(atPath: googleTokenFileURL.path)
    }

    private var oauthCredentialsPath: String? {
        if let bundled = Bundle.main.url(forResource: "credentials", withExtension: "json") {
            return bundled.path
        }
        var current = URL(fileURLWithPath: Bundle.main.bundlePath)
        for _ in 0..<10 {
            let nested = current.appendingPathComponent("App/ResearchBot/credentials.json")
            if FileManager.default.fileExists(atPath: nested.path) {
                return nested.path
            }
            let flat = current.appendingPathComponent("credentials.json")
            if FileManager.default.fileExists(atPath: flat.path) {
                return flat.path
            }
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
        Self.applyBackendDeploymentPaths(to: &env)
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

    // MARK: - Google Workspace Export

    func exportToWorkspace(sessionId: String, kbRoot: String?) {
        guard !isExportingWorkspace else { return }
        guard !sessionId.isEmpty else {
            workspaceExportError = "No session selected for export."
            return
        }
        guard let script = scriptPath else {
            workspaceExportError = "Could not locate execute_pipeline.sh."
            return
        }

        isExportingWorkspace = true
        workspaceExportError = nil
        workspaceExportMessage = nil
        masterDocumentURL = nil

        let credsPath = oauthCredentialsPath
        let kb = kbRoot ?? ""

        Task.detached(priority: .userInitiated) { [weak self] in
            await self?.executeWorkspaceExport(
                script: script,
                sessionId: sessionId,
                kbRoot: kb,
                credentialsPath: credsPath
            )
        }
    }

    nonisolated private func executeWorkspaceExport(
        script: String,
        sessionId: String,
        kbRoot: String,
        credentialsPath: String?
    ) async {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/bin/bash")
        var args = [
            script,
            "--command", "export_to_workspace",
            "--session-id", sessionId,
        ]
        if !kbRoot.isEmpty {
            args += ["--kb-root", kbRoot]
        }
        process.arguments = args

        var env = ProcessInfo.processInfo.environment
        let homeDir = NSHomeDirectory()
        env["PATH"] = "\(homeDir)/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:" + (env["PATH"] ?? "")
        Self.applyBackendDeploymentPaths(to: &env)
        if let credentialsPath {
            env["RESEARCHBOT_OAUTH_CREDENTIALS"] = credentialsPath
        }
        process.environment = env

        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = pipe

        // Drain stdout/stderr while the child runs. Reading only after waitUntilExit()
        // can deadlock when the buffer fills (same pattern as executeProcess).
        var capturedOutput = ""
        let outputLock = NSLock()

        pipe.fileHandleForReading.readabilityHandler = { handle in
            let data = handle.availableData
            guard !data.isEmpty, let chunk = String(data: data, encoding: .utf8) else { return }
            outputLock.lock()
            capturedOutput += chunk
            outputLock.unlock()
        }

        do {
            try process.run()
            process.waitUntilExit()
        } catch {
            pipe.fileHandleForReading.readabilityHandler = nil
            await MainActor.run {
                self.workspaceExportError = "Failed to launch export: \(error.localizedDescription)"
                self.isExportingWorkspace = false
            }
            return
        }

        pipe.fileHandleForReading.readabilityHandler = nil
        let trailing = pipe.fileHandleForReading.readDataToEndOfFile()
        if !trailing.isEmpty, let tail = String(data: trailing, encoding: .utf8) {
            outputLock.lock()
            capturedOutput += tail
            outputLock.unlock()
        }

        outputLock.lock()
        let output = capturedOutput
        outputLock.unlock()

        await MainActor.run {
            parseWorkspaceExportOutput(output, exitCode: process.terminationStatus)
            isExportingWorkspace = false
        }
    }

    private func parseWorkspaceExportOutput(_ raw: String, exitCode: Int32) {
        let startMarker = "---WORKSPACE_EXPORT_RESULT_START---"
        let endMarker = "---WORKSPACE_EXPORT_RESULT_END---"

        guard let startRange = raw.range(of: startMarker),
              let endRange = raw.range(of: endMarker),
              startRange.upperBound < endRange.lowerBound else {
            workspaceExportError = exitCode != 0
                ? "Workspace export failed (exit \(exitCode)). Result markers not found."
                : "Workspace export finished but no result payload was returned."
            return
        }

        let jsonString = String(raw[startRange.upperBound..<endRange.lowerBound])
            .trimmingCharacters(in: .whitespacesAndNewlines)
        guard let jsonData = jsonString.data(using: .utf8) else {
            workspaceExportError = "Failed to decode export response."
            return
        }

        do {
            let result = try JSONDecoder().decode(WorkspaceExportResult.self, from: jsonData)
            if result.status == "error" {
                workspaceExportError = result.message
                masterDocumentURL = nil
            } else {
                workspaceExportError = nil
                workspaceExportMessage = result.message
                masterDocumentURL = result.masterDocumentURL
            }
        } catch {
            workspaceExportError = "JSON decode error: \(error.localizedDescription)"
        }
    }

    // MARK: - Proposal Generation

    func generateProposal(sessionId sid: String, projectIdea: String, kbRoot kb: String?) {
        guard !isGeneratingProposal else { return }
        guard !sid.isEmpty else {
            proposalError = "No session selected for proposal generation."
            return
        }
        guard !projectIdea.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            proposalError = "Project idea cannot be empty."
            return
        }
        guard let script = scriptPath else {
            proposalError = "Could not locate execute_pipeline.sh."
            return
        }

        isGeneratingProposal = true
        proposalProgress = "Initializing proposal pipeline…\n"
        proposalError = nil
        proposalResult = nil
        proposalDocumentURL = nil

        let kbRootValue = kb ?? ""
        let ideaValue = projectIdea

        Task.detached(priority: .userInitiated) { [weak self] in
            await self?.executeProposalGeneration(
                script: script,
                sessionId: sid,
                projectIdea: ideaValue,
                kbRoot: kbRootValue
            )
        }
    }

    nonisolated private func executeProposalGeneration(
        script: String,
        sessionId: String,
        projectIdea: String,
        kbRoot: String
    ) async {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/bin/bash")
        var args = [
            script,
            "--command", "generate_proposal",
            "--session-id", sessionId,
            "--project-idea", projectIdea,
        ]
        if !kbRoot.isEmpty {
            args += ["--kb-root", kbRoot]
        }
        process.arguments = args

        var env = ProcessInfo.processInfo.environment
        let homeDir = NSHomeDirectory()
        env["PATH"] = "\(homeDir)/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:" + (env["PATH"] ?? "")
        Self.applyBackendDeploymentPaths(to: &env)
        process.environment = env

        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = pipe

        // Stream progress lines to the UI while the process runs.
        pipe.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            guard !data.isEmpty, let chunk = String(data: data, encoding: .utf8) else { return }
            let bridge = self
            Task { @MainActor in
                bridge?.proposalProgress += chunk
            }
        }

        do {
            try process.run()
            process.waitUntilExit()
        } catch {
            pipe.fileHandleForReading.readabilityHandler = nil
            await MainActor.run {
                self.proposalError = "Failed to launch proposal process: \(error.localizedDescription)"
                self.isGeneratingProposal = false
            }
            return
        }

        pipe.fileHandleForReading.readabilityHandler = nil
        let trailing = pipe.fileHandleForReading.readDataToEndOfFile()

        let fullOutput = await MainActor.run { () -> String in
            if !trailing.isEmpty, let tail = String(data: trailing, encoding: .utf8) {
                self.proposalProgress += tail
            }
            return self.proposalProgress
        }

        await MainActor.run {
            parseProposalOutput(fullOutput, exitCode: process.terminationStatus)
            isGeneratingProposal = false
        }
    }

    private func parseProposalOutput(_ raw: String, exitCode: Int32) {
        let startMarker = "---PROPOSAL_RESULT_START---"
        let endMarker = "---PROPOSAL_RESULT_END---"

        guard let startRange = raw.range(of: startMarker),
              let endRange = raw.range(of: endMarker),
              startRange.upperBound < endRange.lowerBound else {
            proposalError = exitCode != 0
                ? "Proposal generation failed (exit \(exitCode)). Result markers not found."
                : "Proposal generation finished but no result payload was returned."
            return
        }

        let jsonString = String(raw[startRange.upperBound..<endRange.lowerBound])
            .trimmingCharacters(in: .whitespacesAndNewlines)
        guard let jsonData = jsonString.data(using: .utf8) else {
            proposalError = "Failed to decode proposal response."
            return
        }

        do {
            let result = try JSONDecoder().decode(ProposalResult.self, from: jsonData)
            if result.status == "error" {
                proposalError = result.message
                proposalResult = nil
            } else {
                proposalError = nil
                proposalResult = result
                proposalDocumentURL = nil
            }
        } catch {
            proposalError = "JSON decode error: \(error.localizedDescription)"
        }
    }

    // MARK: - Proposal Export to Google Workspace

    func exportProposalToWorkspace(sessionId sid: String, proposalPath: String, kbRoot kb: String?) {
        guard !isExportingProposal else { return }
        guard !sid.isEmpty, !proposalPath.isEmpty else {
            proposalExportError = "Missing session or proposal path."
            return
        }
        guard let script = scriptPath else {
            proposalExportError = "Could not locate execute_pipeline.sh."
            return
        }

        isExportingProposal = true
        proposalExportError = nil
        proposalExportMessage = nil

        let kbRootValue = kb ?? ""
        let credsPath = oauthCredentialsPath

        Task.detached(priority: .userInitiated) { [weak self] in
            await self?.executeProposalExport(
                script: script,
                sessionId: sid,
                proposalPath: proposalPath,
                kbRoot: kbRootValue,
                credentialsPath: credsPath
            )
        }
    }

    nonisolated private func executeProposalExport(
        script: String,
        sessionId: String,
        proposalPath: String,
        kbRoot: String,
        credentialsPath: String?
    ) async {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/bin/bash")
        var args = [
            script,
            "--command", "export_proposal_to_workspace",
            "--session-id", sessionId,
            "--proposal-path", proposalPath,
        ]
        if !kbRoot.isEmpty {
            args += ["--kb-root", kbRoot]
        }
        process.arguments = args

        var env = ProcessInfo.processInfo.environment
        let homeDir = NSHomeDirectory()
        env["PATH"] = "\(homeDir)/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:" + (env["PATH"] ?? "")
        Self.applyBackendDeploymentPaths(to: &env)
        if let credentialsPath {
            env["RESEARCHBOT_OAUTH_CREDENTIALS"] = credentialsPath
        }
        process.environment = env

        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = pipe

        var capturedOutput = ""
        let outputLock = NSLock()

        pipe.fileHandleForReading.readabilityHandler = { handle in
            let data = handle.availableData
            guard !data.isEmpty, let chunk = String(data: data, encoding: .utf8) else { return }
            outputLock.lock()
            capturedOutput += chunk
            outputLock.unlock()
        }

        do {
            try process.run()
            process.waitUntilExit()
        } catch {
            pipe.fileHandleForReading.readabilityHandler = nil
            await MainActor.run {
                self.proposalExportError = "Failed to launch export: \(error.localizedDescription)"
                self.isExportingProposal = false
            }
            return
        }

        pipe.fileHandleForReading.readabilityHandler = nil
        let trailing = pipe.fileHandleForReading.readDataToEndOfFile()
        if !trailing.isEmpty, let tail = String(data: trailing, encoding: .utf8) {
            outputLock.lock()
            capturedOutput += tail
            outputLock.unlock()
        }

        outputLock.lock()
        let output = capturedOutput
        outputLock.unlock()

        await MainActor.run {
            parseProposalExportOutput(output, exitCode: process.terminationStatus)
            isExportingProposal = false
        }
    }

    private func parseProposalExportOutput(_ raw: String, exitCode: Int32) {
        let startMarker = "---WORKSPACE_EXPORT_RESULT_START---"
        let endMarker = "---WORKSPACE_EXPORT_RESULT_END---"

        guard let startRange = raw.range(of: startMarker),
              let endRange = raw.range(of: endMarker),
              startRange.upperBound < endRange.lowerBound else {
            proposalExportError = exitCode != 0
                ? "Proposal export failed (exit \(exitCode))."
                : "Export finished but no result payload was returned."
            return
        }

        let jsonString = String(raw[startRange.upperBound..<endRange.lowerBound])
            .trimmingCharacters(in: .whitespacesAndNewlines)
        guard let jsonData = jsonString.data(using: .utf8) else {
            proposalExportError = "Failed to decode export response."
            return
        }

        do {
            let result = try JSONDecoder().decode(WorkspaceExportResult.self, from: jsonData)
            if result.status == "error" {
                proposalExportError = result.message
                proposalDocumentURL = nil
            } else {
                proposalExportError = nil
                proposalExportMessage = result.message
                proposalDocumentURL = result.proposalDocumentURL ?? result.topicDocumentURL
            }
        } catch {
            proposalExportError = "JSON decode error: \(error.localizedDescription)"
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
            PythonBridge.applyBackendDeploymentPaths(to: &env)
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
