//
//  GapAnalysisPanel.swift
//  ResearchBot
//
//  Concise summary panel alongside the WKWebView. Surfaces the executive
//  summary + gap counts only; the deep breakdown lives in FullDetailWindow.
//

import SwiftUI

// MARK: - Decoded Models

struct AcademicGapAnalysis: Codable, Sendable {
    let summary: String
    let structuralHoles: [StructuralHole]
    let highDegreeLimitations: [HighDegreeLimitation]
    let orphanedSolutions: [OrphanedSolution]
    let sourceFiles: [String]?
    let error: String?

    enum CodingKeys: String, CodingKey {
        case summary, error
        case structuralHoles = "structural_holes"
        case highDegreeLimitations = "high_degree_limitations"
        case orphanedSolutions = "orphaned_solutions"
        case sourceFiles = "source_files"
    }

    var totalGapCount: Int {
        structuralHoles.count + highDegreeLimitations.count + orphanedSolutions.count
    }
}

struct StructuralHole: Codable, Identifiable, Sendable, Hashable {
    var id: String { title }
    let title: String
    let communitiesInvolved: [String]
    let description: String
    let bridgingOpportunity: String
    let references: [String]?

    enum CodingKeys: String, CodingKey {
        case title, description, references
        case communitiesInvolved = "communities_involved"
        case bridgingOpportunity = "bridging_opportunity"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        title = try c.decode(String.self, forKey: .title)
        communitiesInvolved = try c.decodeIfPresent([String].self, forKey: .communitiesInvolved) ?? []
        description = try c.decodeIfPresent(String.self, forKey: .description) ?? ""
        bridgingOpportunity = try c.decodeIfPresent(String.self, forKey: .bridgingOpportunity) ?? ""
        references = try c.decodeIfPresent([String].self, forKey: .references)
    }
}

struct HighDegreeLimitation: Codable, Identifiable, Sendable, Hashable {
    var id: String { title }
    let title: String
    let nodeLabels: [String]
    let degree: Int?
    let description: String
    let evidence: String?
    let references: [String]?

    enum CodingKeys: String, CodingKey {
        case title, description, degree, evidence, references
        case nodeLabels = "node_labels"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        title = try c.decode(String.self, forKey: .title)
        nodeLabels = try c.decodeIfPresent([String].self, forKey: .nodeLabels) ?? []
        if let d = try? c.decode(Int.self, forKey: .degree) {
            degree = d
        } else if let d = try? c.decode(Double.self, forKey: .degree) {
            degree = Int(d)
        } else {
            degree = nil
        }
        description = try c.decodeIfPresent(String.self, forKey: .description) ?? ""
        evidence = try c.decodeIfPresent(String.self, forKey: .evidence)
        references = try c.decodeIfPresent([String].self, forKey: .references)
    }
}

struct OrphanedSolution: Codable, Identifiable, Sendable, Hashable {
    var id: String { title }
    let title: String
    let failureConditions: [String]
    let description: String
    let technicalContribution: String
    let references: [String]?

    enum CodingKeys: String, CodingKey {
        case title, description, references
        case failureConditions = "failure_conditions"
        case technicalContribution = "technical_contribution"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        title = try c.decode(String.self, forKey: .title)
        failureConditions = try c.decodeIfPresent([String].self, forKey: .failureConditions) ?? []
        description = try c.decodeIfPresent(String.self, forKey: .description) ?? ""
        technicalContribution = try c.decodeIfPresent(String.self, forKey: .technicalContribution) ?? ""
        references = try c.decodeIfPresent([String].self, forKey: .references)
    }
}

// MARK: - Concise Panel

struct GapAnalysisPanel: View {
    let analysis: AcademicGapAnalysis
    @Binding var isCollapsed: Bool
    var onOpenFullAnalysis: () -> Void

    var body: some View {
        VStack(spacing: 0) {
            panelHeader
            Divider()

            if isCollapsed {
                collapsedHint
            } else {
                VStack(alignment: .leading, spacing: 18) {
                    summarySection

                    if let err = analysis.error, !err.isEmpty {
                        errorBanner(err)
                    }

                    metricsGrid

                    Spacer(minLength: 0)

                    Button {
                        onOpenFullAnalysis()
                    } label: {
                        HStack(spacing: 8) {
                            Image(systemName: "doc.text.magnifyingglass")
                            Text("View Full Analysis")
                            Spacer()
                            Image(systemName: "arrow.up.right.square")
                                .font(.caption)
                                .opacity(0.7)
                        }
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, 10)
                    }
                    .buttonStyle(.borderedProminent)
                    .controlSize(.large)
                    .tint(.accentColor)
                    .disabled(analysis.totalGapCount == 0 && analysis.error == nil)
                }
                .padding(16)
            }
        }
        .frame(minWidth: isCollapsed ? 44 : 280, idealWidth: 320, maxWidth: 420)
        .background(.regularMaterial)
    }

    // MARK: Subviews

    private var panelHeader: some View {
        HStack(spacing: 8) {
            if !isCollapsed {
                Image(systemName: "graduationcap")
                    .foregroundStyle(.tint)
                Text("FYP Gap Analysis")
                    .font(.headline)
                Spacer()
            }
            Button {
                withAnimation(.easeInOut(duration: 0.2)) {
                    isCollapsed.toggle()
                }
            } label: {
                Image(systemName: isCollapsed ? "sidebar.right" : "sidebar.left")
            }
            .buttonStyle(.plain)
            .help(isCollapsed ? "Expand gap analysis panel" : "Collapse panel")
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 10)
    }

    private var collapsedHint: some View {
        VStack {
            Image(systemName: "graduationcap")
                .font(.title3)
                .foregroundStyle(.tint)
            Spacer()
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .padding(.vertical, 16)
    }

    private var summarySection: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Executive Summary")
                .font(.subheadline.weight(.semibold))
                .foregroundStyle(.secondary)
            Text(analysis.summary)
                .font(.body)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    private var metricsGrid: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Gaps Detected")
                .font(.subheadline.weight(.semibold))
                .foregroundStyle(.secondary)

            VStack(spacing: 8) {
                metricRow(
                    label: "Structural Holes",
                    count: analysis.structuralHoles.count,
                    icon: "circle.grid.cross",
                    tint: .purple
                )
                metricRow(
                    label: "High-Degree Limitations",
                    count: analysis.highDegreeLimitations.count,
                    icon: "exclamationmark.triangle",
                    tint: .orange
                )
                metricRow(
                    label: "Orphaned Solutions",
                    count: analysis.orphanedSolutions.count,
                    icon: "puzzlepiece.extension",
                    tint: .teal
                )
            }

            if let files = analysis.sourceFiles, !files.isEmpty {
                Text("\(files.count) source document\(files.count == 1 ? "" : "s") indexed")
                    .font(.caption)
                    .foregroundStyle(.tertiary)
                    .padding(.top, 4)
            }
        }
    }

    private func metricRow(label: String, count: Int, icon: String, tint: Color) -> some View {
        HStack(spacing: 10) {
            Image(systemName: icon)
                .foregroundStyle(tint)
                .frame(width: 18)
            Text(label)
                .font(.subheadline)
            Spacer()
            Text("\(count)")
                .font(.subheadline.weight(.semibold))
                .monospacedDigit()
                .padding(.horizontal, 8)
                .padding(.vertical, 2)
                .background(tint.opacity(0.15))
                .clipShape(Capsule())
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 8)
        .background(.background.opacity(0.5))
        .clipShape(RoundedRectangle(cornerRadius: 8))
        .overlay(
            RoundedRectangle(cornerRadius: 8)
                .strokeBorder(.quaternary, lineWidth: 1)
        )
    }

    private func errorBanner(_ message: String) -> some View {
        HStack(alignment: .top, spacing: 8) {
            Image(systemName: "exclamationmark.circle")
                .foregroundStyle(.orange)
            Text(message)
                .font(.caption)
                .foregroundStyle(.secondary)
        }
        .padding(10)
        .background(.orange.opacity(0.08))
        .clipShape(RoundedRectangle(cornerRadius: 8))
    }
}
