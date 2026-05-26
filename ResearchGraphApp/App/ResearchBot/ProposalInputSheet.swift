//
//  ProposalInputSheet.swift
//  ResearchBot
//
//  Sheet view for entering a custom project idea and generating a proposal
//  from a selected historical research session.
//

import SwiftUI

struct ProposalInputSheet: View {
    let session: HistorySession
    @Bindable var bridge: PythonBridge
    var onProposalReady: (ProposalResult) -> Void
    @Environment(\.dismiss) private var dismiss

    @State private var projectIdea: String = ""
    @State private var hasError = false

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            // MARK: - Header
            HStack(spacing: 10) {
                Image(systemName: "doc.text.below.ecg")
                    .font(.title2)
                    .foregroundStyle(.tint)
                VStack(alignment: .leading, spacing: 2) {
                    Text("Create Proposal")
                        .font(.title2.weight(.bold))
                    Text("Based on: \(session.topic)")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                }
                Spacer()
                Button("Cancel") { dismiss() }
                    .buttonStyle(.bordered)
            }
            .padding(.horizontal, 24)
            .padding(.vertical, 16)
            .background(.bar)

            Divider()

            ScrollView {
                VStack(alignment: .leading, spacing: 20) {
                    // Session context card
                    sessionContextCard

                    // Project idea input
                    VStack(alignment: .leading, spacing: 8) {
                        Text("Your Project Idea")
                            .font(.title3.weight(.semibold))

                        TextEditor(text: $projectIdea)
                            .font(.body)
                            .scrollContentBackground(.hidden)
                            .frame(minHeight: 140, maxHeight: 200)
                            .padding(12)
                            .background(.ultraThinMaterial)
                            .clipShape(RoundedRectangle(cornerRadius: 12))
                            .overlay(
                                RoundedRectangle(cornerRadius: 12)
                                    .strokeBorder(.quaternary, lineWidth: 1)
                            )

                        Text("Describe your implementation idea. The system will scope it against this session's research domain, match relevant papers (≥ 90% relevance), and synthesize a rigorous academic proposal.")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }

                    // Progress display (while generating)
                    if bridge.isGeneratingProposal {
                        progressSection
                    }

                    // Error display
                    if let error = bridge.proposalError {
                        errorBanner(error)
                    }

                    // Generate button
                    Button {
                        bridge.generateProposal(
                            sessionId: session.id,
                            projectIdea: projectIdea,
                            kbRoot: session.kbRoot
                        )
                    } label: {
                        HStack(spacing: 8) {
                            if bridge.isGeneratingProposal {
                                ProgressView()
                                    .controlSize(.small)
                                Text("Generating…")
                            } else {
                                Image(systemName: "sparkles")
                                Text("Generate Proposal")
                            }
                        }
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, 10)
                    }
                    .buttonStyle(.borderedProminent)
                    .controlSize(.large)
                    .disabled(
                        bridge.isGeneratingProposal
                        || projectIdea.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
                    )
                }
                .padding(24)
            }
        }
        .frame(minWidth: 600, minHeight: 500)
        .frame(maxWidth: 720)
        .onChange(of: bridge.proposalResult) { _, newResult in
            if let result = newResult {
                onProposalReady(result)
                dismiss()
            }
        }
    }

    // MARK: - Session Context Card

    private var sessionContextCard: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 8) {
                Image(systemName: "books.vertical.fill")
                    .foregroundStyle(.tint)
                Text("Source Session")
                    .font(.headline)
            }

            HStack(spacing: 16) {
                metricPill(label: "Topic", value: session.topic)
                metricPill(label: "Date", value: session.formattedDate)
                metricPill(label: "Sources", value: "\(session.urlRefinerCount)")
            }
        }
        .padding(14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(.ultraThinMaterial)
        .clipShape(RoundedRectangle(cornerRadius: 12))
        .overlay(
            RoundedRectangle(cornerRadius: 12)
                .strokeBorder(.quaternary, lineWidth: 1)
        )
    }

    private func metricPill(label: String, value: String) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(label)
                .font(.caption2)
                .foregroundStyle(.tertiary)
            Text(value)
                .font(.caption.weight(.semibold))
                .lineLimit(1)
        }
    }

    // MARK: - Progress Section

    private var progressSection: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Text("Pipeline Progress")
                    .font(.headline)
                Spacer()
                ProgressView()
                    .controlSize(.mini)
            }

            ScrollView {
                Text(bridge.proposalProgress)
                    .font(.system(.caption2, design: .monospaced))
                    .foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, alignment: .topLeading)
            }
            .frame(minHeight: 100, maxHeight: 180)
            .padding(10)
            .background(.ultraThinMaterial)
            .clipShape(RoundedRectangle(cornerRadius: 10))
            .overlay(
                RoundedRectangle(cornerRadius: 10)
                    .strokeBorder(.quaternary, lineWidth: 1)
            )
        }
    }

    // MARK: - Error Banner

    private func errorBanner(_ message: String) -> some View {
        HStack(spacing: 8) {
            Image(systemName: "exclamationmark.triangle.fill")
                .foregroundStyle(.orange)
            Text(message)
                .font(.caption)
                .foregroundStyle(.secondary)
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(.orange.opacity(0.1))
        .clipShape(RoundedRectangle(cornerRadius: 10))
    }
}
