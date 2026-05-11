//
//  ContentView.swift
//  ResearchBot
//
//  Created by Sethum Methsanda on 2026-05-09.
//
//  Professional macOS interface for the Research Graph pipeline.
//  Two screens: Input → Knowledge Graph Viewer.
//

import SwiftUI
import WebKit

// MARK: - Main Content View

struct ContentView: View {
    @State private var bridge = PythonBridge()
    @State private var idea: String = ""
    @State private var showGraph = false

    var body: some View {
        Group {
            if showGraph, let graphPath = bridge.graphFilePath {
                GraphView(
                    graphPath: graphPath,
                    onBack: { showGraph = false }
                )
            } else {
                InputView(
                    idea: $idea,
                    bridge: bridge,
                    onGraphReady: { showGraph = true }
                )
            }
        }
        .frame(minWidth: 720, minHeight: 560)
    }
}

// MARK: - Input View

struct InputView: View {
    @Binding var idea: String
    @Bindable var bridge: PythonBridge
    var onGraphReady: () -> Void

    @State private var hasError = false

    var body: some View {
        VStack(spacing: 0) {
            // ── Header ──────────────────────────────────────────────
            headerBar

            // ── Content ─────────────────────────────────────────────
            VStack(spacing: 24) {
                // Input card
                VStack(alignment: .leading, spacing: 12) {
                    Text("What do you want to research?")
                        .font(.title3.weight(.semibold))
                        .foregroundStyle(.primary)

                    TextEditor(text: $idea)
                        .font(.body)
                        .scrollContentBackground(.hidden)
                        .padding(12)
                        .frame(minHeight: 100, maxHeight: 140)
                        .background(.ultraThinMaterial)
                        .clipShape(RoundedRectangle(cornerRadius: 12))
                        .overlay(
                            RoundedRectangle(cornerRadius: 12)
                                .strokeBorder(.quaternary, lineWidth: 1)
                        )

                    Text("Paste a topic, question, or URL. The AI automatically extracts keywords and intent.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }

                // Run button
                Button {
                    bridge.runPipeline(idea: idea)
                } label: {
                    HStack(spacing: 8) {
                        if bridge.isRunning {
                            ProgressView()
                                .controlSize(.small)
                            Text("Researching…")
                        } else {
                            Image(systemName: "sparkle.magnifyingglass")
                            Text("Run Research")
                        }
                    }
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 10)
                }
                .buttonStyle(.borderedProminent)
                .tint(.accentColor)
                .controlSize(.large)
                .disabled(bridge.isRunning || idea.trimmingCharacters(in: .whitespaces).isEmpty)

                // Output console
                if bridge.isRunning || !bridge.progress.isEmpty {
                    outputConsole
                }
            }
            .padding(24)

            Spacer()
        }
        .onChange(of: bridge.graphFilePath) { _, newPath in
            if newPath != nil && bridge.errorMessage == nil {
                onGraphReady()
            }
        }
        .alert("Pipeline Error", isPresented: $hasError) {
            Button("OK") { hasError = false }
        } message: {
            Text(bridge.errorMessage ?? "Unknown error.")
        }
        .onChange(of: bridge.errorMessage) { _, newErr in
            if newErr != nil { hasError = true }
        }
    }

    // MARK: - Sub-views

    private var headerBar: some View {
        HStack(spacing: 10) {
            Image(systemName: "atom")
                .font(.title2)
                .foregroundStyle(.tint)
            Text("Research Graph")
                .font(.title2.weight(.bold))
            Spacer()
        }
        .padding(.horizontal, 24)
        .padding(.vertical, 16)
        .background(.bar)
    }

    private var outputConsole: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Text("Pipeline Output")
                    .font(.headline)
                Spacer()
                if bridge.isRunning {
                    ProgressView()
                        .controlSize(.mini)
                }
            }

            ScrollViewReader { proxy in
                ScrollView {
                    Text(bridge.progress)
                        .font(.system(.caption, design: .monospaced))
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(10)
                        .id("bottom")
                }
                .frame(maxHeight: 200)
                .background(.ultraThinMaterial)
                .clipShape(RoundedRectangle(cornerRadius: 10))
                .overlay(
                    RoundedRectangle(cornerRadius: 10)
                        .strokeBorder(.quaternary, lineWidth: 1)
                )
                .onChange(of: bridge.progress) { _, _ in
                    proxy.scrollTo("bottom", anchor: .bottom)
                }
            }
        }
    }
}

// MARK: - Graph Viewer

struct GraphView: View {
    let graphPath: String
    var onBack: () -> Void

    var body: some View {
        VStack(spacing: 0) {
            // Toolbar
            HStack {
                Button {
                    onBack()
                } label: {
                    Label("New Research", systemImage: "chevron.left")
                }
                .buttonStyle(.plain)
                .foregroundStyle(.tint)

                Spacer()

                Text("Knowledge Graph")
                    .font(.headline)

                Spacer()

                // Balance the back button width
                Color.clear.frame(width: 100)
            }
            .padding(.horizontal, 16)
            .padding(.vertical, 10)
            .background(.bar)

            Divider()

            // WebView
            GraphWebView(filePath: graphPath)
        }
    }
}

// MARK: - WKWebView Wrapper

struct GraphWebView: NSViewRepresentable {
    let filePath: String

    func makeNSView(context: Context) -> WKWebView {
        let config = WKWebViewConfiguration()
        config.preferences.setValue(true, forKey: "allowFileAccessFromFileURLs")
        let webView = WKWebView(frame: .zero, configuration: config)
        return webView
    }

    func updateNSView(_ webView: WKWebView, context: Context) {
        let fileURL = URL(fileURLWithPath: filePath)
        let directory = fileURL.deletingLastPathComponent()

        // CRITICAL: loadFileURL with allowingReadAccessTo grants the
        // WKWebView permission to read sibling files (JS, CSS, JSON).
        webView.loadFileURL(fileURL, allowingReadAccessTo: directory)
    }
}

// MARK: - Preview

#Preview {
    ContentView()
}
