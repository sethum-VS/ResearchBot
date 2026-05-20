//
//  MarkdownViewer.swift
//  ResearchBot
//
//  Native Markdown reader for any .md file inside research_knowledge_base.
//  Resolves filenames against kb_root delivered by the Python bridge, then
//  renders blocks with AttributedString markdown parsing.
//

import SwiftUI
import AppKit

struct MarkdownViewer: View {
    let filename: String
    let kbRoot: String?
    var onBack: () -> Void

    @State private var loadState: LoadState = .loading

    enum LoadState {
        case loading
        case loaded(resolvedPath: URL, blocks: [MarkdownBlock])
        case failed(message: String)
    }

    var body: some View {
        VStack(spacing: 0) {
            toolbar
            Divider()
            content
        }
        .background(.background)
        .task(id: filename) {
            await load()
        }
    }

    // MARK: - Toolbar

    private var toolbar: some View {
        HStack(spacing: 12) {
            Button {
                onBack()
            } label: {
                Label("Back", systemImage: "chevron.left")
                    .labelStyle(.titleAndIcon)
            }
            .buttonStyle(.plain)
            .foregroundStyle(.tint)
            .keyboardShortcut(.cancelAction)

            Divider()
                .frame(height: 16)

            Image(systemName: "doc.text")
                .foregroundStyle(.secondary)
            Text(filename)
                .font(.system(.subheadline, design: .monospaced))
                .lineLimit(1)
                .truncationMode(.middle)

            Spacer()

            if case .loaded(let url, _) = loadState {
                Button {
                    NSWorkspace.shared.activateFileViewerSelecting([url])
                } label: {
                    Label("Reveal", systemImage: "folder")
                }
                .buttonStyle(.plain)
                .help("Reveal in Finder")
            }
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 10)
        .background(.bar)
    }

    // MARK: - Content

    @ViewBuilder
    private var content: some View {
        switch loadState {
        case .loading:
            VStack(spacing: 10) {
                ProgressView()
                Text("Loading \(filename)…")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)

        case .failed(let message):
            VStack(spacing: 12) {
                Image(systemName: "exclamationmark.triangle")
                    .font(.largeTitle)
                    .foregroundStyle(.orange)
                Text("Couldn't open source document")
                    .font(.headline)
                Text(message)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .multilineTextAlignment(.center)
                    .padding(.horizontal, 32)
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)

        case .loaded(_, let blocks):
            ScrollView {
                VStack(alignment: .leading, spacing: 10) {
                    ForEach(blocks) { block in
                        MarkdownBlockView(block: block)
                    }
                }
                .padding(24)
                .frame(maxWidth: 820, alignment: .leading)
                .frame(maxWidth: .infinity, alignment: .center)
            }
            .textSelection(.enabled)
        }
    }

    // MARK: - Loading

    @MainActor
    private func load() async {
        loadState = .loading

        let resolved: URL?
        if let root = kbRoot {
            resolved = MarkdownViewer.resolveFile(named: filename, under: URL(fileURLWithPath: root))
        } else {
            resolved = nil
        }

        guard let url = resolved else {
            loadState = .failed(message: "File '\(filename)' was not found inside the knowledge base.")
            return
        }

        do {
            let raw = try String(contentsOf: url, encoding: .utf8)
            let blocks = MarkdownParser.parse(raw)
            loadState = .loaded(resolvedPath: url, blocks: blocks)
        } catch {
            loadState = .failed(message: "Failed to read file: \(error.localizedDescription)")
        }
    }

    /// Recursively search *root* for a file whose name matches *named*.
    static func resolveFile(named: String, under root: URL) -> URL? {
        let target = (named as NSString).lastPathComponent
        let fm = FileManager.default

        let direct = root.appendingPathComponent(target)
        if fm.fileExists(atPath: direct.path) { return direct }

        guard let enumerator = fm.enumerator(
            at: root,
            includingPropertiesForKeys: [.isRegularFileKey],
            options: [.skipsHiddenFiles]
        ) else { return nil }

        for case let candidate as URL in enumerator {
            if candidate.lastPathComponent == target {
                return candidate
            }
        }
        return nil
    }
}

// MARK: - Markdown rendering

struct MarkdownBlock: Identifiable {
    let id = UUID()
    let kind: Kind
    let text: String

    enum Kind {
        case heading(level: Int)
        case paragraph
        case bullet
        case codeBlock
        case quote
        case rule
    }
}

private enum MarkdownParser {
    static func parse(_ raw: String) -> [MarkdownBlock] {
        var blocks: [MarkdownBlock] = []
        let lines = raw.components(separatedBy: "\n")

        var i = 0
        var paragraphBuffer: [String] = []

        func flushParagraph() {
            let joined = paragraphBuffer.joined(separator: " ").trimmingCharacters(in: .whitespaces)
            if !joined.isEmpty {
                blocks.append(MarkdownBlock(kind: .paragraph, text: joined))
            }
            paragraphBuffer.removeAll()
        }

        while i < lines.count {
            let line = lines[i]
            let trimmed = line.trimmingCharacters(in: .whitespaces)

            if trimmed.hasPrefix("```") {
                flushParagraph()
                var code: [String] = []
                i += 1
                while i < lines.count && !lines[i].trimmingCharacters(in: .whitespaces).hasPrefix("```") {
                    code.append(lines[i])
                    i += 1
                }
                blocks.append(MarkdownBlock(kind: .codeBlock, text: code.joined(separator: "\n")))
                i += 1
                continue
            }

            if trimmed.isEmpty {
                flushParagraph()
                i += 1
                continue
            }

            if trimmed == "---" || trimmed == "***" {
                flushParagraph()
                blocks.append(MarkdownBlock(kind: .rule, text: ""))
                i += 1
                continue
            }

            if trimmed.hasPrefix("#") {
                flushParagraph()
                var level = 0
                var idx = trimmed.startIndex
                while idx < trimmed.endIndex, trimmed[idx] == "#", level < 6 {
                    level += 1
                    idx = trimmed.index(after: idx)
                }
                let title = trimmed[idx...].trimmingCharacters(in: .whitespaces)
                blocks.append(MarkdownBlock(kind: .heading(level: level), text: title))
                i += 1
                continue
            }

            if trimmed.hasPrefix("- ") || trimmed.hasPrefix("* ") || trimmed.hasPrefix("+ ") {
                flushParagraph()
                let content = String(trimmed.dropFirst(2))
                blocks.append(MarkdownBlock(kind: .bullet, text: content))
                i += 1
                continue
            }

            if trimmed.hasPrefix("> ") {
                flushParagraph()
                blocks.append(MarkdownBlock(kind: .quote, text: String(trimmed.dropFirst(2))))
                i += 1
                continue
            }

            paragraphBuffer.append(trimmed)
            i += 1
        }

        flushParagraph()
        return blocks
    }
}

private struct MarkdownBlockView: View {
    let block: MarkdownBlock

    var body: some View {
        switch block.kind {
        case .heading(let level):
            Text(block.text)
                .font(headingFont(for: level))
                .fontWeight(.semibold)
                .padding(.top, level <= 2 ? 8 : 4)
                .padding(.bottom, 2)
                .frame(maxWidth: .infinity, alignment: .leading)

        case .paragraph:
            Text(attributed(block.text))
                .font(.body)
                .fixedSize(horizontal: false, vertical: true)
                .frame(maxWidth: .infinity, alignment: .leading)

        case .bullet:
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Text("•")
                    .foregroundStyle(.secondary)
                Text(attributed(block.text))
                    .font(.body)
                    .fixedSize(horizontal: false, vertical: true)
            }
            .frame(maxWidth: .infinity, alignment: .leading)

        case .codeBlock:
            ScrollView(.horizontal, showsIndicators: false) {
                Text(block.text)
                    .font(.system(.callout, design: .monospaced))
                    .padding(12)
                    .frame(minWidth: 0, alignment: .leading)
            }
            .background(.quaternary.opacity(0.4))
            .clipShape(RoundedRectangle(cornerRadius: 8))

        case .quote:
            HStack(spacing: 10) {
                Rectangle()
                    .fill(.tint)
                    .frame(width: 3)
                Text(attributed(block.text))
                    .font(.body.italic())
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            .frame(maxWidth: .infinity, alignment: .leading)

        case .rule:
            Divider()
                .padding(.vertical, 6)
        }
    }

    private func headingFont(for level: Int) -> Font {
        switch level {
        case 1: return .system(.largeTitle, design: .default)
        case 2: return .system(.title, design: .default)
        case 3: return .system(.title2, design: .default)
        case 4: return .system(.title3, design: .default)
        default: return .system(.headline, design: .default)
        }
    }

    private func attributed(_ text: String) -> AttributedString {
        if let parsed = try? AttributedString(
            markdown: text,
            options: .init(interpretedSyntax: .inlineOnlyPreservingWhitespace)
        ) {
            return parsed
        }
        return AttributedString(text)
    }
}
