//
//  HistoryView.swift
//  ResearchBot
//
//  Historical Run Registry — the application's initial dashboard.
//
//  Enumerates every prior pipeline run by inspecting
//  `research_knowledge_base/runs/session_*` on disk, surfaces metadata
//  (timestamp, original keyword, _URLRefiner doc count) and lets the user
//  programmatically swap the SwiftUI app state into a previously archived
//  graph + gap analysis without re-running the backend.
//

import SwiftUI

// MARK: - Historical Session Model

struct HistorySession: Identifiable, Hashable {
    let id: String                    // session_<TIMESTAMP>_<slug>
    let absolutePath: String
    let kbRoot: String
    let createdAt: Date
    let topic: String                 // primary_keyword if known
    let urlRefinerCount: Int
    let graphHTMLPath: String?
    let proposalCount: Int

    var hasGraph: Bool { graphHTMLPath != nil }
    var hasProposals: Bool { proposalCount > 0 }

    /// Pretty-printed timestamp for the card header.
    var formattedDate: String {
        let formatter = DateFormatter()
        formatter.dateStyle = .medium
        formatter.timeStyle = .short
        return formatter.string(from: createdAt)
    }
}

// MARK: - History View

struct HistoryView: View {
    @Bindable var bridge: PythonBridge
    var onNewRun: () -> Void
    var onOpenSession: (HistorySession) -> Void

    @State private var sessions: [HistorySession] = []
    @State private var isLoading = false
    @State private var loadError: String?
    @State private var proposalSheetSession: HistorySession?
    @State private var reviewingProposal: ProposalResult?
    @State private var proposalHistorySession: HistorySession?

    var body: some View {
        VStack(spacing: 0) {
            headerBar

            if isLoading {
                ProgressView("Loading runs…")
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else if let err = loadError {
                errorBanner(err)
            } else if sessions.isEmpty {
                emptyState
            } else {
                runMatrix
            }
        }
        .frame(minWidth: 720, minHeight: 560)
        .appTextSelection()
        .onAppear { reloadSessions() }
        .sheet(item: $proposalSheetSession) { session in
            ProposalInputSheet(
                session: session,
                bridge: bridge,
                onProposalReady: { result in
                    reviewingProposal = result
                }
            )
        }
        .sheet(item: $reviewingProposal) { result in
            ProposalReviewView(
                proposalResult: result,
                bridge: bridge,
                onBack: {
                    reviewingProposal = nil
                    reloadSessions()
                }
            )
        }
        .sheet(item: $proposalHistorySession) { session in
            ProposalHistorySheet(
                session: session,
                bridge: bridge,
                onReview: { result in
                    proposalHistorySession = nil
                    reviewingProposal = result
                },
                onDismiss: {
                    proposalHistorySession = nil
                }
            )
        }
    }

    // MARK: - Header

    private var headerBar: some View {
        HStack(spacing: 10) {
            Image(systemName: "books.vertical.fill")
                .font(.title2)
                .foregroundStyle(.tint)
            VStack(alignment: .leading, spacing: 2) {
                Text("Research Archive")
                    .font(.title2.weight(.bold))
                Text("\(sessions.count) historical session\(sessions.count == 1 ? "" : "s")")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            Spacer()
            Button {
                reloadSessions()
            } label: {
                Label("Refresh", systemImage: "arrow.clockwise")
            }
            .buttonStyle(.bordered)

            Button {
                onNewRun()
            } label: {
                Label("New Research", systemImage: "sparkle.magnifyingglass")
            }
            .buttonStyle(.borderedProminent)
        }
        .padding(.horizontal, 24)
        .padding(.vertical, 16)
        .background(.bar)
    }

    // MARK: - States

    private var emptyState: some View {
        VStack(spacing: 16) {
            Image(systemName: "tray")
                .font(.system(size: 48))
                .foregroundStyle(.secondary)
            Text("No archived runs yet")
                .font(.title3.weight(.semibold))
            Text("Press New Research to seed your first knowledge graph.")
                .font(.callout)
                .foregroundStyle(.secondary)
            Button("Run Research", action: onNewRun)
                .buttonStyle(.borderedProminent)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .padding(40)
    }

    private func errorBanner(_ message: String) -> some View {
        VStack(spacing: 12) {
            Image(systemName: "exclamationmark.triangle.fill")
                .font(.title)
                .foregroundStyle(.orange)
            Text(message)
                .multilineTextAlignment(.center)
                .padding(.horizontal, 24)
            Button("Retry") { reloadSessions() }
                .buttonStyle(.bordered)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    // MARK: - Card Matrix

    private var runMatrix: some View {
        ScrollView {
            LazyVGrid(
                columns: [GridItem(.adaptive(minimum: 280, maximum: 360), spacing: 16)],
                spacing: 16
            ) {
                ForEach(sessions) { session in
                    HistoryCard(
                        session: session,
                        bridge: bridge,
                        onOpen: {
                            bridge.loadHistoricalSession(session)
                            onOpenSession(session)
                        },
                        onCreateProposal: {
                            proposalSheetSession = session
                        },
                        onViewProposals: {
                            proposalHistorySession = session
                        }
                    )
                }
            }
            .padding(20)
        }
    }

    // MARK: - Disk Enumeration

    private func reloadSessions() {
        isLoading = true
        loadError = nil
        Task.detached(priority: .userInitiated) {
            let scan = HistoryView.scanRuns()
            await MainActor.run {
                self.sessions = scan.sessions
                self.loadError = scan.error
                self.isLoading = false
            }
        }
    }

    nonisolated private static func scanRuns() -> (sessions: [HistorySession], error: String?) {
        guard let kbRoot = locateKBRoot() else {
            return ([], "Could not locate research_knowledge_base/. Check repo structure.")
        }

        let runsRoot = kbRoot.appendingPathComponent("runs")
        let fm = FileManager.default

        guard fm.fileExists(atPath: runsRoot.path) else {
            return ([], nil)
        }

        let children: [URL]
        do {
            children = try fm.contentsOfDirectory(
                at: runsRoot,
                includingPropertiesForKeys: [.creationDateKey, .isDirectoryKey],
                options: [.skipsHiddenFiles]
            )
        } catch {
            return ([], "Failed to read runs directory: \(error.localizedDescription)")
        }

        let sessions: [HistorySession] = children.compactMap { url in
            var isDir: ObjCBool = false
            guard fm.fileExists(atPath: url.path, isDirectory: &isDir), isDir.boolValue else {
                return nil
            }
            guard url.lastPathComponent.hasPrefix("session_") else { return nil }
            return buildSession(at: url, kbRoot: kbRoot)
        }
        .sorted { $0.createdAt > $1.createdAt }

        return (sessions, nil)
    }

    nonisolated private static func buildSession(at url: URL, kbRoot: URL) -> HistorySession {
        let fm = FileManager.default
        let id = url.lastPathComponent

        // Parse timestamp segment between `session_` and the optional slug.
        // Format: session_YYYYMMDDTHHMMSSZ[_slug]
        let raw = String(id.dropFirst("session_".count))
        let timestampSegment = raw.split(separator: "_").first.map(String.init) ?? raw
        let date = parseTimestamp(timestampSegment)
            ?? ((try? url.resourceValues(forKeys: [.creationDateKey]))?.creationDate ?? Date())

        // Try to read session_manifest.json for nicer metadata.
        var topic: String = ""
        var urlRefinerCount = 0

        let manifestURL = url.appendingPathComponent("session_manifest.json")
        if let data = try? Data(contentsOf: manifestURL),
           let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
            topic = (json["primary_keyword"] as? String) ?? (json["topic"] as? String) ?? ""
            urlRefinerCount = (json["url_refiner_count"] as? Int) ?? 0
        }

        // Fallback / cross-check: scan agent_scrapes for *_URLRefiner*.md files.
        if urlRefinerCount == 0 {
            let scrapeDir = url.appendingPathComponent("agent_scrapes")
            if let entries = try? fm.contentsOfDirectory(atPath: scrapeDir.path) {
                urlRefinerCount = entries.filter {
                    $0.lowercased().contains("_urlrefiner")
                }.count
            }
        }

        if topic.isEmpty {
            // Last resort: derive a topic from the slug portion of the dir name.
            let parts = raw.split(separator: "_", maxSplits: 1, omittingEmptySubsequences: true)
            if parts.count == 2 {
                topic = String(parts[1]).replacingOccurrences(of: "_", with: " ")
            } else {
                topic = "Untitled run"
            }
        }

        let graphURL = url
            .appendingPathComponent("graphify-out")
            .appendingPathComponent("graph.html")
        let graphPath = fm.fileExists(atPath: graphURL.path) ? graphURL.path : nil

        // Count proposals in proposals/ subdirectory
        let proposalsDir = url.appendingPathComponent("proposals")
        var proposalCount = 0
        if let proposalEntries = try? fm.contentsOfDirectory(atPath: proposalsDir.path) {
            proposalCount = proposalEntries.filter {
                $0.hasSuffix(".md") && $0.hasPrefix("proposal_")
            }.count
        }

        return HistorySession(
            id: id,
            absolutePath: url.path,
            kbRoot: kbRoot.path,
            createdAt: date,
            topic: topic,
            urlRefinerCount: urlRefinerCount,
            graphHTMLPath: graphPath,
            proposalCount: proposalCount
        )
    }

    nonisolated private static func parseTimestamp(_ s: String) -> Date? {
        let formatter = DateFormatter()
        formatter.dateFormat = "yyyyMMdd'T'HHmmss'Z'"
        formatter.timeZone = TimeZone(identifier: "UTC")
        return formatter.date(from: s)
    }

    nonisolated private static func locateKBRoot() -> URL? {
        if let env = ProcessInfo.processInfo.environment["RESEARCHBOT_KB_ROOT"] {
            let url = URL(fileURLWithPath: env)
            if FileManager.default.fileExists(atPath: url.path) { return url }
        }

        var current = URL(fileURLWithPath: Bundle.main.bundlePath)
        for _ in 0..<10 {
            let candidate = current.appendingPathComponent("research_knowledge_base")
            if FileManager.default.fileExists(atPath: candidate.path) {
                return candidate
            }
            current = current.deletingLastPathComponent()
        }
        return nil
    }
}

// MARK: - History Card

private struct HistoryCard: View {
    let session: HistorySession
    @Bindable var bridge: PythonBridge
    var onOpen: () -> Void
    var onCreateProposal: () -> Void
    var onViewProposals: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 8) {
                Image(systemName: session.hasGraph ? "point.3.connected.trianglepath.dotted" : "doc.text.magnifyingglass")
                    .font(.title3)
                    .foregroundStyle(session.hasGraph ? Color.accentColor : .secondary)
                Text(session.formattedDate)
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(.secondary)
                Spacer()
                if !session.hasGraph {
                    Text("NO GRAPH")
                        .font(.caption2.weight(.bold))
                        .foregroundStyle(.orange)
                        .padding(.horizontal, 6)
                        .padding(.vertical, 2)
                        .background(.orange.opacity(0.15))
                        .clipShape(Capsule())
                }
            }

            Text(session.topic)
                .font(.headline)
                .lineLimit(2)
                .multilineTextAlignment(.leading)
                .frame(maxWidth: .infinity, alignment: .leading)

            Spacer(minLength: 4)

            HStack(spacing: 12) {
                metricChip(
                    icon: "link",
                    value: "\(session.urlRefinerCount)",
                    label: "URLRefiners"
                )
                Spacer()
                Image(systemName: "chevron.right")
                    .font(.caption.weight(.bold))
                    .foregroundStyle(.tertiary)
            }

            Text(session.id)
                .font(.system(.caption2, design: .monospaced))
                .foregroundStyle(.tertiary)
                .lineLimit(1)
                .truncationMode(.middle)

            HStack(spacing: 8) {
                GoogleWorkspaceExportButton(
                    bridge: bridge,
                    sessionId: session.id,
                    kbRoot: session.kbRoot,
                    prominent: false
                )
                .font(.caption)
                .controlSize(.small)

                Button {
                    onCreateProposal()
                } label: {
                    Label("Create Proposal", systemImage: "doc.text.below.ecg")
                }
                .buttonStyle(.bordered)
                .font(.caption)
                .controlSize(.small)

                if session.hasProposals {
                    Button {
                        onViewProposals()
                    } label: {
                        Text("\(session.proposalCount) Proposals")
                            .font(.caption2.weight(.bold))
                    }
                    .buttonStyle(.borderedProminent)
                    .tint(.blue)
                    .controlSize(.small)
                    .clipShape(Capsule())
                }
            }
        }
        .padding(16)
        .frame(maxWidth: .infinity, minHeight: 160, alignment: .topLeading)
        .background(.ultraThinMaterial)
        .clipShape(RoundedRectangle(cornerRadius: 14))
        .overlay(
            RoundedRectangle(cornerRadius: 14)
                .strokeBorder(.quaternary, lineWidth: 1)
        )
        .contentShape(Rectangle())
        .onTapGesture(perform: onOpen)
    }

    private func metricChip(icon: String, value: String, label: String) -> some View {
        HStack(spacing: 6) {
            Image(systemName: icon)
                .font(.caption)
            Text(value)
                .font(.caption.weight(.bold))
            Text(label)
                .font(.caption)
                .foregroundStyle(.secondary)
        }
        .padding(.horizontal, 8)
        .padding(.vertical, 4)
        .background(Color.accentColor.opacity(0.12))
        .clipShape(Capsule())
    }
}
