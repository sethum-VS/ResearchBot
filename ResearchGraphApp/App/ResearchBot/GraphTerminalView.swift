//
//  GraphTerminalView.swift
//  ResearchBot
//
//  Interactive Graph Explorer Console.
//
//  Sits adjacent to the main graph view and exposes Graphify's native
//  `query` and `path` CLI tools to the user. All requests target the
//  local FastAPI proxy on :8000 (`/api/graph/query`, `/api/graph/path`)
//  via PythonBridge — Swift never spawns subprocesses for these calls.
//

import SwiftUI

struct GraphTerminalView: View {
    @Bindable var bridge: PythonBridge

    @State private var freeformQuery: String = ""
    @State private var sourceNode: String = ""
    @State private var targetNode: String = ""

    @State private var transcript: [TerminalEntry] = []
    @State private var isRunning: Bool = false
    @State private var liveError: String?

    private let coreGapsMacro =
        "What are the most commonly cited limitations or future work recommendations in the scraped academic papers?"
    private let intersectionMacro =
        "How does the societal problem intersect with the limitations of current technologies?"

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            header

            transcriptView
                .frame(maxWidth: .infinity, maxHeight: .infinity)
                .layoutPriority(1)

            Divider()

            macroBar

            Divider()

            pathFinderForm

            Divider()

            customQueryBar

            if let err = liveError {
                Text(err)
                    .font(.caption)
                    .foregroundStyle(.red)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(14)
        .frame(minWidth: 320, maxWidth: .infinity, maxHeight: .infinity)
        .background(.background)
        .appTextSelection()
    }

    // MARK: - Header

    private var header: some View {
        HStack(spacing: 8) {
            Image(systemName: "terminal.fill")
                .foregroundStyle(.tint)
            Text("Graph Explorer")
                .font(.headline)
            Spacer()
            if isRunning {
                ProgressView().controlSize(.mini)
            }
            if !transcript.isEmpty {
                Button {
                    transcript.removeAll()
                } label: {
                    Image(systemName: "trash")
                }
                .buttonStyle(.borderless)
                .help("Clear transcript")
            }
        }
    }

    // MARK: - Transcript

    private var transcriptView: some View {
        ScrollViewReader { proxy in
            ScrollView(.vertical, showsIndicators: true) {
                LazyVStack(alignment: .leading, spacing: 10) {
                    if transcript.isEmpty {
                        emptyTranscript
                    } else {
                        ForEach(transcript) { entry in
                            transcriptRow(entry)
                                .id(entry.id)
                        }
                    }
                }
                .padding(12)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .background(Color.black.opacity(0.85))
            .clipShape(RoundedRectangle(cornerRadius: 10))
            .onChange(of: transcript.count) { _, _ in
                if let last = transcript.last?.id {
                    withAnimation(.easeOut(duration: 0.2)) {
                        proxy.scrollTo(last, anchor: .bottom)
                    }
                }
            }
        }
    }

    private var emptyTranscript: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("$ graphify ready")
                .font(.system(.callout, design: .monospaced))
                .foregroundStyle(.green)
            Text("Use a macro, find a contribution path, or type a custom question below.")
                .font(.system(.caption, design: .monospaced))
                .foregroundStyle(.gray)
        }
    }

    private func transcriptRow(_ entry: TerminalEntry) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 6) {
                Text(entry.kind.prompt)
                    .font(.system(.callout, design: .monospaced).weight(.bold))
                    .foregroundStyle(entry.kind.promptColor)
                Text(entry.command)
                    .font(.system(.callout, design: .monospaced))
                    .foregroundStyle(.white)
                    .lineLimit(3)
                    .truncationMode(.middle)
            }

            Text(entry.output)
                .font(.system(.caption, design: .monospaced))
                .foregroundStyle(entry.isError ? .red : .green.opacity(0.92))
                .fixedSize(horizontal: false, vertical: true)
                .frame(maxWidth: .infinity, alignment: .leading)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    // MARK: - Macro Buttons

    private var macroBar: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("MACROS")
                .font(.caption2.weight(.bold))
                .foregroundStyle(.secondary)

            HStack(spacing: 8) {
                Button {
                    submitQuery(coreGapsMacro, label: "Extract Core Gaps")
                } label: {
                    Label("Extract Core Gaps", systemImage: "sparkles")
                }
                .buttonStyle(.bordered)
                .disabled(isRunning || bridge.sessionId == nil)

                Button {
                    submitQuery(intersectionMacro, label: "Problem Intersection")
                } label: {
                    Label("Problem Intersection", systemImage: "circle.grid.cross")
                }
                .buttonStyle(.bordered)
                .disabled(isRunning || bridge.sessionId == nil)
            }
        }
    }

    // MARK: - Find Contribution Path

    private var pathFinderForm: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("FIND CONTRIBUTION PATH")
                .font(.caption2.weight(.bold))
                .foregroundStyle(.secondary)

            HStack(spacing: 8) {
                TextField("Source Node", text: $sourceNode)
                    .textFieldStyle(.roundedBorder)
                    .onSubmit { submitPath() }

                Image(systemName: "arrow.right")
                    .foregroundStyle(.secondary)

                TextField("Target Node", text: $targetNode)
                    .textFieldStyle(.roundedBorder)
                    .onSubmit { submitPath() }

                Button {
                    submitPath()
                } label: {
                    Image(systemName: "arrow.triangle.branch")
                }
                .buttonStyle(.borderedProminent)
                .disabled(
                    isRunning ||
                    bridge.sessionId == nil ||
                    sourceNode.trimmingCharacters(in: .whitespaces).isEmpty ||
                    targetNode.trimmingCharacters(in: .whitespaces).isEmpty
                )
                .help("graphify path")
            }
        }
    }

    // MARK: - Free-form Query

    private var customQueryBar: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("CUSTOM QUERY")
                .font(.caption2.weight(.bold))
                .foregroundStyle(.secondary)

            HStack(spacing: 8) {
                TextField(
                    "Ask anything about the graph…",
                    text: $freeformQuery,
                    axis: .vertical
                )
                .lineLimit(1...3)
                .textFieldStyle(.roundedBorder)
                .onSubmit { submitFreeform() }

                Button {
                    submitFreeform()
                } label: {
                    Image(systemName: "paperplane.fill")
                }
                .buttonStyle(.borderedProminent)
                .disabled(
                    isRunning ||
                    bridge.sessionId == nil ||
                    freeformQuery.trimmingCharacters(in: .whitespaces).isEmpty
                )
                .help("graphify query")
            }

            if bridge.sessionId == nil {
                Text("No active session. Open a run from the Archive or start a new pipeline.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
    }

    // MARK: - Submission Helpers

    private func submitFreeform() {
        let q = freeformQuery.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !q.isEmpty else { return }
        submitQuery(q, label: "custom")
        freeformQuery = ""
    }

    private func submitQuery(_ question: String, label: String) {
        let displayCommand = "graphify query \"\(question)\""
        runConsoleAction(prompt: .query, command: displayCommand) {
            await bridge.runGraphQuery(question: question)
        }
    }

    private func submitPath() {
        let s = sourceNode.trimmingCharacters(in: .whitespacesAndNewlines)
        let t = targetNode.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !s.isEmpty, !t.isEmpty else { return }

        let displayCommand = "graphify path \"\(s)\" \"\(t)\""
        runConsoleAction(prompt: .path, command: displayCommand) {
            await bridge.runGraphPath(source: s, target: t)
        }
    }

    private func runConsoleAction(
        prompt: TerminalEntry.Kind,
        command: String,
        action: @escaping () async -> Result<String, Error>
    ) {
        liveError = nil
        isRunning = true

        let pendingEntry = TerminalEntry(
            kind: prompt,
            command: command,
            output: "running…",
            isError: false
        )
        transcript.append(pendingEntry)
        let pendingID = pendingEntry.id

        Task {
            let result = await action()
            await MainActor.run {
                if let idx = transcript.firstIndex(where: { $0.id == pendingID }) {
                    switch result {
                    case .success(let stdout):
                        transcript[idx] = TerminalEntry(
                            id: pendingID,
                            kind: prompt,
                            command: command,
                            output: stdout.isEmpty ? "(no output)" : stdout,
                            isError: false
                        )
                    case .failure(let err):
                        let message = err.localizedDescription
                        transcript[idx] = TerminalEntry(
                            id: pendingID,
                            kind: prompt,
                            command: command,
                            output: message,
                            isError: true
                        )
                        liveError = message
                    }
                }
                isRunning = false
            }
        }
    }
}

// MARK: - Transcript Model

struct TerminalEntry: Identifiable, Equatable {
    enum Kind {
        case query, path

        var prompt: String {
            switch self {
            case .query: return "Q›"
            case .path:  return "P›"
            }
        }

        var promptColor: Color {
            switch self {
            case .query: return .cyan
            case .path:  return .yellow
            }
        }
    }

    let id: UUID
    let kind: Kind
    let command: String
    let output: String
    let isError: Bool

    init(
        id: UUID = UUID(),
        kind: Kind,
        command: String,
        output: String,
        isError: Bool
    ) {
        self.id = id
        self.kind = kind
        self.command = command
        self.output = output
        self.isError = isError
    }
}
