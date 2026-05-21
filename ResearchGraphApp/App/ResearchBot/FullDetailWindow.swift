//
//  FullDetailWindow.swift
//  ResearchBot
//
//  Comprehensive breakdown of the Phase 4.5 academic_gap_analysis payload.
//  Presented as a full-screen page from GraphView (not a sheet). References
//  render as clickable buttons that push a MarkdownViewer for verification.
//

import SwiftUI

struct FullDetailWindow: View {
    let analysis: AcademicGapAnalysis
    let kbRoot: String?
    let sessionId: String?
    @Bindable var bridge: PythonBridge
    var onClose: () -> Void

    // Pushed source under inspection (nil = list view).
    @State private var inspectedSource: String?

    var body: some View {
        VStack(spacing: 0) {
            pageHeader
            Divider()

            Group {
                if let filename = inspectedSource {
                    MarkdownViewer(
                        filename: filename,
                        kbRoot: kbRoot,
                        onBack: { inspectedSource = nil },
                        showsNavigationChrome: false
                    )
                } else {
                    fullAnalysisScroll
                }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(.background)
        .appTextSelection()
        .onDisappear {
            inspectedSource = nil
        }
    }

    private var pageHeader: some View {
        HStack(spacing: 12) {
            if inspectedSource != nil {
                Button {
                    inspectedSource = nil
                } label: {
                    Label("Back", systemImage: "chevron.left")
                }
                .buttonStyle(.plain)
                .foregroundStyle(.tint)
            }

            Image(systemName: "graduationcap.fill")
                .foregroundStyle(.tint)

            Text(inspectedSource ?? "Full Gap Analysis")
                .font(.headline)
                .lineLimit(1)
                .truncationMode(.middle)

            Spacer()

            if let sid = sessionId, !sid.isEmpty {
                GoogleWorkspaceExportButton(
                    bridge: bridge,
                    sessionId: sid,
                    kbRoot: kbRoot
                )
                .fixedSize()
            }

            Button {
                inspectedSource = nil
                onClose()
            } label: {
                Label("Close", systemImage: "xmark.circle.fill")
            }
            .buttonStyle(.borderedProminent)
            .keyboardShortcut(.cancelAction)
            .help("Return to Knowledge Graph")
        }
        .padding(.horizontal, 20)
        .padding(.vertical, 12)
        .background(.bar)
        .fixedSize(horizontal: false, vertical: true)
    }

    // MARK: - Main breakdown

    private var fullAnalysisScroll: some View {
        ScrollView(.vertical) {
            VStack(alignment: .leading, spacing: 28) {
                summaryHeader

                if let err = analysis.error, !err.isEmpty {
                    errorBanner(err)
                }

                categorySection(
                    title: "Structural Holes",
                    subtitle: "Disconnected communities — opportunities to bridge clusters.",
                    icon: "circle.grid.cross",
                    tint: .purple,
                    count: analysis.structuralHoles.count
                ) {
                    VStack(spacing: 12) {
                        ForEach(analysis.structuralHoles) { hole in
                            StructuralHoleCard(
                                hole: hole,
                                onOpenReference: { inspectedSource = $0 }
                            )
                        }
                    }
                }

                categorySection(
                    title: "High-Degree Limitations",
                    subtitle: "Multi-source validated gaps in the literature.",
                    icon: "exclamationmark.triangle",
                    tint: .orange,
                    count: analysis.highDegreeLimitations.count
                ) {
                    VStack(spacing: 12) {
                        ForEach(analysis.highDegreeLimitations) { limit in
                            LimitationCard(
                                item: limit,
                                onOpenReference: { inspectedSource = $0 }
                            )
                        }
                    }
                }

                categorySection(
                    title: "Orphaned Solutions",
                    subtitle: "Existing solutions undermined by known failure modes.",
                    icon: "puzzlepiece.extension",
                    tint: .teal,
                    count: analysis.orphanedSolutions.count
                ) {
                    VStack(spacing: 12) {
                        ForEach(analysis.orphanedSolutions) { sol in
                            OrphanedSolutionCard(
                                item: sol,
                                onOpenReference: { inspectedSource = $0 }
                            )
                        }
                    }
                }

                if let files = analysis.sourceFiles, !files.isEmpty {
                    sourceIndex(files: files)
                }
            }
            .padding(.horizontal, 32)
            .padding(.vertical, 28)
            .frame(maxWidth: 980, alignment: .leading)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    private var summaryHeader: some View {
        VStack(alignment: .leading, spacing: 10) {
            Label("Executive Summary", systemImage: "graduationcap.fill")
                .font(.title3.weight(.semibold))
                .foregroundStyle(.tint)

            Text(analysis.summary)
                .font(.title3)
                .foregroundStyle(.primary)
                .fixedSize(horizontal: false, vertical: true)

            HStack(spacing: 14) {
                metricChip(label: "Structural Holes", count: analysis.structuralHoles.count, tint: .purple)
                metricChip(label: "Limitations", count: analysis.highDegreeLimitations.count, tint: .orange)
                metricChip(label: "Orphaned Solutions", count: analysis.orphanedSolutions.count, tint: .teal)
            }
            .padding(.top, 4)
        }
        .padding(20)
        .background(.tint.opacity(0.06))
        .clipShape(RoundedRectangle(cornerRadius: 14))
    }

    private func metricChip(label: String, count: Int, tint: Color) -> some View {
        HStack(spacing: 6) {
            Text("\(count)")
                .font(.headline.weight(.bold))
                .monospacedDigit()
                .foregroundStyle(tint)
            Text(label)
                .font(.caption)
                .foregroundStyle(.secondary)
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 6)
        .background(.background.opacity(0.6))
        .clipShape(Capsule())
    }

    @ViewBuilder
    private func categorySection<Cards: View>(
        title: String,
        subtitle: String,
        icon: String,
        tint: Color,
        count: Int,
        @ViewBuilder cards: () -> Cards
    ) -> some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack(spacing: 10) {
                Image(systemName: icon)
                    .foregroundStyle(tint)
                    .font(.title2)
                VStack(alignment: .leading, spacing: 2) {
                    Text(title)
                        .font(.title2.weight(.semibold))
                    Text(subtitle)
                        .font(.subheadline)
                        .foregroundStyle(.secondary)
                }
                Spacer()
                Text("\(count)")
                    .font(.subheadline.weight(.semibold))
                    .monospacedDigit()
                    .padding(.horizontal, 10)
                    .padding(.vertical, 4)
                    .background(tint.opacity(0.15))
                    .clipShape(Capsule())
            }

            if count == 0 {
                Text("No insights in this category for the current run.")
                    .font(.subheadline)
                    .foregroundStyle(.tertiary)
                    .padding(.vertical, 12)
                    .frame(maxWidth: .infinity)
                    .background(.background.opacity(0.4))
                    .clipShape(RoundedRectangle(cornerRadius: 10))
            } else {
                cards()
            }
        }
    }

    private func sourceIndex(files: [String]) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            Label("Indexed Sources", systemImage: "doc.on.doc")
                .font(.title3.weight(.semibold))
                .foregroundStyle(.secondary)

            Text("Every reference cited above maps to one of the \(files.count) documents that fed Phase 4.")
                .font(.caption)
                .foregroundStyle(.tertiary)

            FlowLayout(spacing: 6) {
                ForEach(files, id: \.self) { name in
                    Button {
                        inspectedSource = name
                    } label: {
                        Label(name, systemImage: "doc.text")
                            .font(.caption)
                            .padding(.horizontal, 8)
                            .padding(.vertical, 4)
                            .background(.quaternary.opacity(0.6))
                            .clipShape(Capsule())
                    }
                    .buttonStyle(.plain)
                }
            }
        }
        .padding(16)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(.background.opacity(0.4))
        .clipShape(RoundedRectangle(cornerRadius: 12))
    }

    private func errorBanner(_ message: String) -> some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: "exclamationmark.octagon")
                .foregroundStyle(.orange)
                .font(.title3)
            VStack(alignment: .leading, spacing: 4) {
                Text("Analysis warning")
                    .font(.subheadline.weight(.semibold))
                Text(message)
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            Spacer()
        }
        .padding(14)
        .background(.orange.opacity(0.08))
        .clipShape(RoundedRectangle(cornerRadius: 10))
    }
}

// MARK: - Cards

private struct StructuralHoleCard: View {
    let hole: StructuralHole
    var onOpenReference: (String) -> Void

    var body: some View {
        InsightCard(tint: .purple, icon: "circle.grid.cross", title: hole.title) {
            if !hole.communitiesInvolved.isEmpty {
                TagRow(tags: hole.communitiesInvolved, tint: .purple)
            }
            LabeledBlock(label: "Why it's a hole", text: hole.description)
            LabeledBlock(label: "Bridging opportunity", text: hole.bridgingOpportunity, emphasis: true)
            ReferenceList(refs: hole.references ?? [], onOpen: onOpenReference)
        }
    }
}

private struct LimitationCard: View {
    let item: HighDegreeLimitation
    var onOpenReference: (String) -> Void

    var body: some View {
        InsightCard(tint: .orange, icon: "exclamationmark.triangle", title: item.title) {
            HStack(spacing: 10) {
                if !item.nodeLabels.isEmpty {
                    TagRow(tags: item.nodeLabels, tint: .orange)
                }
                Spacer()
                if let deg = item.degree, deg > 0 {
                    Text("degree \(deg)")
                        .font(.caption.monospacedDigit())
                        .foregroundStyle(.secondary)
                }
            }
            LabeledBlock(label: "Why it's a validated gap", text: item.description)
            if let evidence = item.evidence, !evidence.isEmpty {
                LabeledBlock(label: "Evidence", text: evidence, emphasis: true)
            }
            ReferenceList(refs: item.references ?? [], onOpen: onOpenReference)
        }
    }
}

private struct OrphanedSolutionCard: View {
    let item: OrphanedSolution
    var onOpenReference: (String) -> Void

    var body: some View {
        InsightCard(tint: .teal, icon: "puzzlepiece.extension", title: item.title) {
            if !item.failureConditions.isEmpty {
                VStack(alignment: .leading, spacing: 4) {
                    Text("Failure conditions")
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(.secondary)
                    ForEach(item.failureConditions, id: \.self) { cond in
                        Label(cond, systemImage: "xmark.circle")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }
            }
            LabeledBlock(label: "Where it falls short", text: item.description)
            LabeledBlock(label: "Technical contribution", text: item.technicalContribution, emphasis: true)
            ReferenceList(refs: item.references ?? [], onOpen: onOpenReference)
        }
    }
}

// MARK: - Shared sub-components

private struct InsightCard<Content: View>: View {
    let tint: Color
    let icon: String
    let title: String
    @ViewBuilder let content: () -> Content

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 10) {
                Image(systemName: icon)
                    .foregroundStyle(tint)
                    .font(.title3)
                Text(title)
                    .font(.headline)
                Spacer()
            }
            content()
        }
        .padding(18)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(.background.opacity(0.6))
        .clipShape(RoundedRectangle(cornerRadius: 12))
        .overlay(
            RoundedRectangle(cornerRadius: 12)
                .strokeBorder(tint.opacity(0.35), lineWidth: 1)
        )
    }
}

private struct LabeledBlock: View {
    let label: String
    let text: String
    var emphasis: Bool = false

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(label)
                .font(.caption.weight(.semibold))
                .foregroundStyle(.secondary)
            Text(text)
                .font(.subheadline)
                .fontWeight(emphasis ? .medium : .regular)
                .foregroundStyle(emphasis ? .primary : .secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
    }
}

private struct TagRow: View {
    let tags: [String]
    let tint: Color

    var body: some View {
        FlowLayout(spacing: 6) {
            ForEach(tags, id: \.self) { tag in
                Text(tag)
                    .font(.caption2)
                    .padding(.horizontal, 8)
                    .padding(.vertical, 4)
                    .background(tint.opacity(0.15))
                    .clipShape(Capsule())
            }
        }
    }
}

private struct ReferenceList: View {
    let refs: [String]
    var onOpen: (String) -> Void

    var body: some View {
        if refs.isEmpty {
            EmptyView()
        } else {
            VStack(alignment: .leading, spacing: 6) {
                Text("References")
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(.secondary)
                FlowLayout(spacing: 6) {
                    ForEach(refs, id: \.self) { name in
                        Button {
                            onOpen(name)
                        } label: {
                            HStack(spacing: 4) {
                                Image(systemName: "doc.text")
                                    .font(.caption2)
                                Text(name)
                                    .font(.caption.monospaced())
                                    .lineLimit(1)
                                    .truncationMode(.middle)
                            }
                            .padding(.horizontal, 8)
                            .padding(.vertical, 4)
                            .background(.tint.opacity(0.12))
                            .clipShape(Capsule())
                            .overlay(
                                Capsule().strokeBorder(.tint.opacity(0.4), lineWidth: 1)
                            )
                        }
                        .buttonStyle(.plain)
                        .help("Open \(name)")
                    }
                }
            }
        }
    }
}

// MARK: - Flow layout (shared)

struct FlowLayout: Layout {
    var spacing: CGFloat = 8

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        arrange(proposal: proposal, subviews: subviews).size
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        let result = arrange(proposal: proposal, subviews: subviews)
        for (index, position) in result.positions.enumerated() {
            subviews[index].place(
                at: CGPoint(x: bounds.minX + position.x, y: bounds.minY + position.y),
                proposal: .unspecified
            )
        }
    }

    private func arrange(proposal: ProposedViewSize, subviews: Subviews) -> (size: CGSize, positions: [CGPoint]) {
        let maxWidth = proposal.width ?? .infinity
        var positions: [CGPoint] = []
        var x: CGFloat = 0
        var y: CGFloat = 0
        var rowHeight: CGFloat = 0
        var maxX: CGFloat = 0

        for subview in subviews {
            let size = subview.sizeThatFits(.unspecified)
            if x + size.width > maxWidth, x > 0 {
                x = 0
                y += rowHeight + spacing
                rowHeight = 0
            }
            positions.append(CGPoint(x: x, y: y))
            rowHeight = max(rowHeight, size.height)
            x += size.width + spacing
            maxX = max(maxX, x)
        }
        return (CGSize(width: maxX, height: y + rowHeight), positions)
    }
}
