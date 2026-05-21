//
//  ContentView.swift
//  ResearchBot
//
//  Created by Sethum Methsanda on 2026-05-09.
//
//  Professional macOS interface for the Research Graph pipeline.
//  Three screens: History (landing) → Input (new run) → Knowledge Graph Viewer.
//

import SwiftUI
import WebKit

// MARK: - App Routing

enum AppScreen: Hashable {
    case history
    case input
    case graph
}

// MARK: - Main Content View

struct ContentView: View {
    @State private var bridge = PythonBridge()
    @State private var idea: String = ""
    @State private var referenceURL: String = ""
    @State private var screen: AppScreen = .history

    var body: some View {
        Group {
            switch screen {
            case .history:
                HistoryView(
                    bridge: bridge,
                    onNewRun: { screen = .input },
                    onOpenSession: { _ in screen = .graph }
                )
            case .input:
                InputView(
                    idea: $idea,
                    referenceURL: $referenceURL,
                    bridge: bridge,
                    onGraphReady: { screen = .graph },
                    onBackToHistory: { screen = .history }
                )
            case .graph:
                if let graphPath = bridge.graphFilePath {
                    GraphView(
                        graphPath: graphPath,
                        gapAnalysis: bridge.academicGapAnalysis,
                        kbRoot: bridge.kbRoot,
                        bridge: bridge,
                        onBack: { screen = .history }
                    )
                } else {
                    // Defensive fallback — shouldn't normally happen.
                    HistoryView(
                        bridge: bridge,
                        onNewRun: { screen = .input },
                        onOpenSession: { _ in screen = .graph }
                    )
                }
            }
        }
        .frame(minWidth: 820, minHeight: 600)
    }
}

// MARK: - Input View

struct InputView: View {
    @Binding var idea: String
    @Binding var referenceURL: String
    @Bindable var bridge: PythonBridge
    var onGraphReady: () -> Void
    var onBackToHistory: () -> Void

    @State private var hasError = false

    var body: some View {
        VStack(spacing: 0) {
            headerBar

            VStack(spacing: 24) {
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

                    TextField("Optional reference URL (https://…)", text: $referenceURL)
                        .textFieldStyle(.roundedBorder)

                    Text("Paste a topic or question. Add an optional URL for Firecrawl and seed analysis.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }

                Button {
                    bridge.runPipeline(idea: idea, url: referenceURL)
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

    private var headerBar: some View {
        HStack(spacing: 10) {
            Button {
                onBackToHistory()
            } label: {
                Label("Archive", systemImage: "chevron.left")
            }
            .buttonStyle(.plain)
            .foregroundStyle(.tint)

            Spacer()

            Image(systemName: "atom")
                .font(.title2)
                .foregroundStyle(.tint)
            Text("Research Graph")
                .font(.title2.weight(.bold))

            Spacer()

            Color.clear.frame(width: 70)
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
    let gapAnalysis: AcademicGapAnalysis?
    let kbRoot: String?
    @Bindable var bridge: PythonBridge
    var onBack: () -> Void

    @State private var panelCollapsed = false
    @State private var showFullAnalysis = false
    @State private var showTerminal = true

    private var placeholderAnalysis: AcademicGapAnalysis {
        AcademicGapAnalysis(
            summary: "Gap analysis will appear here after the pipeline completes Phase 4.5.",
            structuralHoles: [],
            highDegreeLimitations: [],
            orphanedSolutions: [],
            sourceFiles: [],
            error: nil
        )
    }

    private var resolvedAnalysis: AcademicGapAnalysis {
        gapAnalysis ?? placeholderAnalysis
    }

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                Button {
                    onBack()
                } label: {
                    Label("Archive", systemImage: "chevron.left")
                }
                .buttonStyle(.plain)
                .foregroundStyle(.tint)

                Spacer()

                Text("Knowledge Graph")
                    .font(.headline)

                Spacer()

                Button {
                    withAnimation(.easeInOut(duration: 0.2)) {
                        showTerminal.toggle()
                    }
                } label: {
                    Label("Console", systemImage: showTerminal ? "terminal.fill" : "terminal")
                }
                .buttonStyle(.bordered)
            }
            .padding(.horizontal, 16)
            .padding(.vertical, 10)
            .background(.bar)
            .fixedSize(horizontal: false, vertical: true)

            Divider()

            HSplitView {
                VStack(spacing: 0) {
                    GraphWebView(filePath: graphPath)
                        .frame(maxWidth: .infinity, maxHeight: .infinity)

                    if showTerminal {
                        Divider()
                        GraphTerminalView(bridge: bridge)
                            .frame(height: 360)
                    }
                }
                .frame(minWidth: 400)
                .layoutPriority(1)

                GapAnalysisPanel(
                    analysis: resolvedAnalysis,
                    isCollapsed: $panelCollapsed,
                    onOpenFullAnalysis: { showFullAnalysis = true }
                )
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
        .sheet(isPresented: $showFullAnalysis) {
            FullDetailWindow(
                analysis: resolvedAnalysis,
                kbRoot: kbRoot,
                onClose: { showFullAnalysis = false }
            )
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
        webView.loadFileURL(fileURL, allowingReadAccessTo: directory)
    }
}

// MARK: - Preview

#Preview {
    ContentView()
}
