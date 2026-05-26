//
//  ProposalReviewView.swift
//  ResearchBot
//
//  Full-screen view for reviewing a generated proposal.
//  Renders the proposal Markdown in a styled, scrollable read-only view
//  with toolbar actions for exporting to Google Docs or regenerating.
//

import SwiftUI

struct ProposalReviewView: View {
    let proposalResult: ProposalResult
    @Bindable var bridge: PythonBridge
    var onBack: () -> Void

    @State private var proposalContent: String = ""
    @State private var loadError: String?
    @State private var showExportConfirmation = false

    var body: some View {
        VStack(spacing: 0) {
            headerBar

            Divider()

            if let error = loadError {
                errorView(error)
            } else if proposalContent.isEmpty {
                ProgressView("Loading proposal…")
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else {
                proposalBody
            }
        }
        .frame(minWidth: 720, minHeight: 560)
        .appTextSelection()
        .onAppear { loadProposalContent() }
        .alert("Export to Google Docs", isPresented: $showExportConfirmation) {
            Button("Export") {
                exportToWorkspace()
            }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("This will create a Google Doc in your Drive and update the Master Tracking Document.")
        }
    }

    // MARK: - Header

    private var headerBar: some View {
        HStack(spacing: 10) {
            Button {
                onBack()
            } label: {
                Label("Back", systemImage: "chevron.left")
            }
            .buttonStyle(.plain)
            .foregroundStyle(.tint)

            Spacer()

            VStack(spacing: 2) {
                Text("Proposal Review")
                    .font(.headline)
                if let count = proposalResult.matchedPaperCount {
                    Text("\(count) matched papers")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }

            Spacer()

            // Export status indicators
            if bridge.isExportingProposal {
                ProgressView()
                    .controlSize(.small)
            }
            if let msg = bridge.proposalExportMessage {
                Label(msg, systemImage: "checkmark.circle.fill")
                    .font(.caption)
                    .foregroundStyle(.green)
            }
            if let err = bridge.proposalExportError {
                Label(err, systemImage: "xmark.circle.fill")
                    .font(.caption)
                    .foregroundStyle(.red)
                    .lineLimit(1)
            }

            if let docURLString = bridge.proposalDocumentURL, let url = URL(string: docURLString) {
                Button {
                    NSWorkspace.shared.open(url)
                } label: {
                    Label("Open the link", systemImage: "arrow.up.right.square")
                }
                .buttonStyle(.borderedProminent)
            } else {
                Button {
                    showExportConfirmation = true
                } label: {
                    Label("Export to Google Docs", systemImage: "arrow.up.doc")
                }
                .buttonStyle(.borderedProminent)
                .disabled(bridge.isExportingProposal || proposalContent.isEmpty)
            }
        }
        .padding(.horizontal, 20)
        .padding(.vertical, 12)
        .background(.bar)
    }

    // MARK: - Proposal Body

    private var proposalBody: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                // Metadata strip
                if let query = proposalResult.scopedQuery {
                    metadataStrip(query: query)
                }

                // Rendered Markdown content
                Text(LocalizedStringKey(proposalContent))
                    .font(.body)
                    .textSelection(.enabled)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(20)
                    .background(.ultraThinMaterial)
                    .clipShape(RoundedRectangle(cornerRadius: 12))
                    .overlay(
                        RoundedRectangle(cornerRadius: 12)
                            .strokeBorder(.quaternary, lineWidth: 1)
                    )
            }
            .padding(24)
        }
    }

    private func metadataStrip(query: String) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 8) {
                Image(systemName: "scope")
                    .foregroundStyle(.tint)
                Text("Scoped Query")
                    .font(.caption.weight(.bold))
                    .foregroundStyle(.secondary)
            }
            Text(query)
                .font(.callout)
                .foregroundStyle(.primary)
                .italic()
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.accentColor.opacity(0.08))
        .clipShape(RoundedRectangle(cornerRadius: 10))
    }

    // MARK: - Error

    private func errorView(_ message: String) -> some View {
        VStack(spacing: 12) {
            Image(systemName: "exclamationmark.triangle.fill")
                .font(.title)
                .foregroundStyle(.orange)
            Text(message)
                .multilineTextAlignment(.center)
            Button("Go Back", action: onBack)
                .buttonStyle(.bordered)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    // MARK: - Logic

    private func loadProposalContent() {
        guard let path = proposalResult.proposalPath else {
            loadError = "No proposal file path in result."
            return
        }
        let url = URL(fileURLWithPath: path)
        do {
            proposalContent = try String(contentsOf: url, encoding: .utf8)
        } catch {
            loadError = "Failed to read proposal: \(error.localizedDescription)"
        }
    }

    private func exportToWorkspace() {
        guard let sid = proposalResult.sessionId,
              let path = proposalResult.proposalPath else {
            bridge.proposalExportError = "Missing session or proposal path."
            return
        }
        bridge.exportProposalToWorkspace(
            sessionId: sid,
            proposalPath: path,
            kbRoot: bridge.kbRoot
        )
    }
}
