//
//  DocumentBrowserView.swift
//  ResearchBot
//
//  Run-scoped Markdown corpus browser.
//
//  Lists every `.md` file inside a single sub-folder of the active session
//  (e.g. `agent_scrapes/` or `processed_summaries/`) and pushes the existing
//  `MarkdownViewer` when the user selects a row. UI-only — file discovery
//  happens in `PythonBridge.listMarkdownFiles(in:)` so this view stays free
//  of business logic.
//

import SwiftUI
import AppKit

struct DocumentBrowserView: View {
    /// Heading shown in the toolbar (e.g. "Refined Academic Sources").
    let title: String

    /// `.md` URLs already discovered for this run (Task 1 output).
    let files: [URL]

    /// Knowledge-base root used by `MarkdownViewer` to resolve filenames.
    let kbRoot: String?

    /// Session path — used for the "Reveal in Finder" affordance and as the
    /// preferred resolution root for the markdown viewer when present.
    let sessionPath: String?

    /// Dismiss callback — close the sheet from the Done button.
    var onClose: () -> Void

    /// Currently inspected file (nil = list view).
    @State private var selectedFile: URL?

    var body: some View {
        NavigationStack {
            ZStack {
                if let file = selectedFile {
                    MarkdownViewer(
                        filename: file.lastPathComponent,
                        kbRoot: sessionPath ?? kbRoot,
                        onBack: { selectedFile = nil }
                    )
                    .transition(.move(edge: .trailing).combined(with: .opacity))
                } else {
                    fileList
                        .transition(.move(edge: .leading).combined(with: .opacity))
                }
            }
            .animation(.easeInOut(duration: 0.18), value: selectedFile)
            .frame(minWidth: 720, minHeight: 520)
            .appTextSelection()
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button(role: .cancel) {
                        onClose()
                    } label: {
                        Label("Close", systemImage: "xmark.circle.fill")
                    }
                }

                ToolbarItem(placement: .principal) {
                    VStack(spacing: 0) {
                        Text(selectedFile?.lastPathComponent ?? title)
                            .font(.headline)
                            .lineLimit(1)
                            .truncationMode(.middle)
                        if selectedFile == nil {
                            Text("\(files.count) document\(files.count == 1 ? "" : "s")")
                                .font(.caption2)
                                .foregroundStyle(.secondary)
                        }
                    }
                }
            }
        }
    }

    // MARK: - File List

    @ViewBuilder
    private var fileList: some View {
        if files.isEmpty {
            emptyState
        } else {
            List(files, id: \.self, selection: $selectedFile) { file in
                FileRow(url: file)
                    .tag(file)
                    .contextMenu {
                        Button {
                            selectedFile = file
                        } label: {
                            Label("Open", systemImage: "doc.text")
                        }
                        Button {
                            NSWorkspace.shared.activateFileViewerSelecting([file])
                        } label: {
                            Label("Reveal in Finder", systemImage: "folder")
                        }
                    }
            }
            .listStyle(.inset)
        }
    }

    private var emptyState: some View {
        VStack(spacing: 12) {
            Image(systemName: "doc.questionmark")
                .font(.system(size: 44))
                .foregroundStyle(.secondary)
            Text("No documents found")
                .font(.title3.weight(.semibold))
            Text("This run has no Markdown files in this folder yet.")
                .font(.callout)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
                .padding(.horizontal, 32)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

// MARK: - File Row

private struct FileRow: View {
    let url: URL

    private var sizeText: String? {
        let values = try? url.resourceValues(forKeys: [.fileSizeKey])
        guard let bytes = values?.fileSize else { return nil }
        let formatter = ByteCountFormatter()
        formatter.allowedUnits = [.useKB, .useMB]
        formatter.countStyle = .file
        return formatter.string(fromByteCount: Int64(bytes))
    }

    private var prettyTitle: String {
        let stem = url.deletingPathExtension().lastPathComponent
        return stem
            .replacingOccurrences(of: "_", with: " ")
            .replacingOccurrences(of: "-", with: " ")
    }

    var body: some View {
        HStack(spacing: 12) {
            Image(systemName: iconName)
                .foregroundStyle(.tint)
                .font(.title3)
                .frame(width: 28)

            VStack(alignment: .leading, spacing: 3) {
                Text(prettyTitle)
                    .font(.body.weight(.medium))
                    .lineLimit(1)
                    .truncationMode(.middle)

                Text(url.lastPathComponent)
                    .font(.system(.caption, design: .monospaced))
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .truncationMode(.middle)
            }

            Spacer(minLength: 8)

            if let size = sizeText {
                Text(size)
                    .font(.caption.monospacedDigit())
                    .foregroundStyle(.tertiary)
            }

            Image(systemName: "chevron.right")
                .font(.caption.weight(.semibold))
                .foregroundStyle(.tertiary)
        }
        .padding(.vertical, 4)
    }

    private var iconName: String {
        let lower = url.lastPathComponent.lowercased()
        if lower.contains("urlrefiner") { return "link.circle" }
        if lower.contains("wiki")       { return "book.closed" }
        if lower.contains("academic")   { return "graduationcap" }
        if lower.contains("social")     { return "bubble.left.and.bubble.right" }
        if lower.contains("synthes")    { return "sparkles" }
        return "doc.text"
    }
}
