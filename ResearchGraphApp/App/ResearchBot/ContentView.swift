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
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
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
        VStack(alignment: .leading, spacing: 0) {
            headerBar

            pipelineOutputPane

            Divider()

            inputFormFooter
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .appTextSelection()
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

    /// Top toolbar — Archive pinned top-leading; title centered in the bar.
    private var headerBar: some View {
        ZStack {
            HStack {
                Button {
                    onBackToHistory()
                } label: {
                    Label("Archive", systemImage: "chevron.left")
                }
                .buttonStyle(.plain)
                .foregroundStyle(.tint)

                Spacer(minLength: 0)
            }

            HStack(spacing: 8) {
                Image(systemName: "atom")
                    .font(.title2)
                    .foregroundStyle(.tint)
                Text("Research Graph")
                    .font(.title2.weight(.bold))
            }
        }
        .padding(.horizontal, 24)
        .padding(.vertical, 14)
        .frame(maxWidth: .infinity, alignment: .topLeading)
        .background(.bar)
        .fixedSize(horizontal: false, vertical: true)
    }

    /// Middle pane — always visible; streams agent / pipeline stdout while running.
    private var pipelineOutputPane: some View {
        ScrollViewReader { proxy in
            ScrollView {
                pipelineOutputSection
                    .id("pipeline-output")
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.horizontal, 24)
                    .padding(.vertical, 16)
            }
            .frame(maxWidth: .infinity, minHeight: 160, maxHeight: .infinity, alignment: .top)
            .onChange(of: bridge.progress) { _, _ in
                withAnimation(.easeOut(duration: 0.15)) {
                    proxy.scrollTo("pipeline-output", anchor: .bottom)
                }
            }
        }
    }

    /// Expanding central pane — live pipeline logs scroll here.
    private var pipelineOutputSection: some View {
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

            Text(pipelineOutputDisplayText)
                .font(.system(.caption, design: .monospaced))
                .foregroundStyle(
                    bridge.progress.isEmpty && !bridge.isRunning
                        ? .secondary
                        : .primary
                )
                .frame(maxWidth: .infinity, minHeight: 120, alignment: .topLeading)
                .padding(10)
                .background(.ultraThinMaterial)
                .clipShape(RoundedRectangle(cornerRadius: 10))
                .overlay(
                    RoundedRectangle(cornerRadius: 10)
                        .strokeBorder(.quaternary, lineWidth: 1)
                )
        }
    }

    private var pipelineOutputDisplayText: String {
        if bridge.progress.isEmpty && !bridge.isRunning {
            return "Agent progress will stream here when you run research…"
        }
        return bridge.progress
    }

    /// Input form docked at the bottom with safe padding above the window edge.
    private var inputFormFooter: some View {
        inputFormContent
            .frame(maxWidth: 720)
            .frame(maxWidth: .infinity)
            .padding(.horizontal, 24)
            .padding(.top, 20)
            .padding(.bottom, 40)
            .background(.bar)
            .fixedSize(horizontal: false, vertical: true)
    }

    /// Core input controls — padding applied by `inputFormFooter`.
    private var inputFormContent: some View {
        VStack(spacing: 24) {
            VStack(alignment: .leading, spacing: 12) {
                Text("What do you want to research?")
                    .font(.title3.weight(.semibold))
                    .foregroundStyle(.primary)

                TextEditor(text: $idea)
                    .font(.body)
                    .scrollContentBackground(.hidden)
                    .frame(minHeight: 120, maxHeight: 160)
                    .padding(12)
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

    /// Active data-source browser (nil = none open).
    @State private var openBrowser: DataSourceFolder?

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

    /// Run is "live" once Python has emitted a session path with a graph.
    private var hasActiveRun: Bool {
        bridge.sessionPath?.isEmpty == false
    }

    var body: some View {
        Group {
            if showFullAnalysis {
                FullDetailWindow(
                    analysis: resolvedAnalysis,
                    kbRoot: kbRoot,
                    onClose: { showFullAnalysis = false }
                )
                .frame(maxWidth: .infinity, maxHeight: .infinity)
                .appTextSelection()
            } else {
                graphWorkspace
                    .appTextSelection()
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .sheet(item: $openBrowser) { folder in
            DocumentBrowserView(
                title: folder.title,
                files: bridge.listMarkdownFiles(in: folder.subfolder),
                kbRoot: kbRoot,
                sessionPath: bridge.sessionPath,
                onClose: { openBrowser = nil }
            )
            .appTextSelection()
        }
    }

    private var graphWorkspace: some View {
        VStack(spacing: 0) {
            HStack(spacing: 10) {
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

                dataSourcesMenu

                Button {
                    showTerminal.toggle()
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
                graphAndConsole
                    .frame(minWidth: 360)
                    .layoutPriority(1)

                GapAnalysisPanel(
                    analysis: resolvedAnalysis,
                    isCollapsed: $panelCollapsed,
                    onOpenFullAnalysis: { showFullAnalysis = true }
                )
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
    }

    // MARK: - Graph + console (stable hierarchy — never swap VSplitView ↔ single web view)

    private var graphAndConsole: some View {
        VSplitView {
            GraphWebView(filePath: graphPath)
                .frame(minHeight: 200)
                .frame(maxWidth: .infinity, maxHeight: .infinity)
                .layoutPriority(1)

            Group {
                if showTerminal {
                    GraphTerminalView(bridge: bridge)
                } else {
                    Color.clear
                }
            }
            .frame(
                minHeight: showTerminal ? 150 : 0,
                idealHeight: showTerminal ? 200 : 0,
                maxHeight: showTerminal ? .infinity : 0
            )
            .frame(maxWidth: .infinity)
            .clipped()
            .allowsHitTesting(showTerminal)
            .accessibilityHidden(!showTerminal)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    // MARK: - Data Sources menu

    private var dataSourcesMenu: some View {
        Menu {
            Button {
                openBrowser = .agentScrapes
            } label: {
                Label("View Academic Sources", systemImage: "doc.text.magnifyingglass")
            }
            .disabled(!hasActiveRun)

            Button {
                openBrowser = .processedSummaries
            } label: {
                Label("View Processed Summaries", systemImage: "text.book.closed")
            }
            .disabled(!hasActiveRun)
        } label: {
            Label("Data Sources", systemImage: "tray.full")
        }
        .menuStyle(.borderedButton)
        .disabled(!hasActiveRun)
        .help(hasActiveRun
              ? "Browse the raw Markdown corpus that fed this run"
              : "Open or run a session to browse its raw documents")
    }
}

// MARK: - Data Source folder mapping

enum DataSourceFolder: Identifiable {
    case agentScrapes
    case processedSummaries

    var id: String { subfolder }

    var subfolder: String {
        switch self {
        case .agentScrapes:       return "agent_scrapes"
        case .processedSummaries: return "processed_summaries"
        }
    }

    var title: String {
        switch self {
        case .agentScrapes:       return "Refined Academic Sources"
        case .processedSummaries: return "Processed Summaries"
        }
    }
}

// MARK: - WKWebView Wrapper

/// Host view keeps WKWebView pinned to bounds — prevents blank/white glitches after split toggles.
final class GraphWebViewHost: NSView {
    let webView: WKWebView

    init(webView: WKWebView) {
        self.webView = webView
        super.init(frame: .zero)
        wantsLayer = true
        addSubview(webView)
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    override func layout() {
        super.layout()
        webView.frame = bounds
    }

    override func resizeSubviews(withOldSize oldSize: NSSize) {
        super.resizeSubviews(withOldSize: oldSize)
        webView.frame = bounds
    }
}

struct GraphWebView: NSViewRepresentable {
    let filePath: String

    func makeCoordinator() -> Coordinator {
        Coordinator()
    }

    func makeNSView(context: Context) -> GraphWebViewHost {
        let config = WKWebViewConfiguration()
        config.preferences.setValue(true, forKey: "allowFileAccessFromFileURLs")
        let webView = WKWebView(frame: .zero, configuration: config)
        webView.navigationDelegate = context.coordinator
        context.coordinator.webView = webView
        return GraphWebViewHost(webView: webView)
    }

    func updateNSView(_ host: GraphWebViewHost, context: Context) {
        let webView = host.webView
        host.needsLayout = true
        host.layoutSubtreeIfNeeded()

        guard context.coordinator.lastLoadedPath != filePath else { return }
        context.coordinator.lastLoadedPath = filePath
        let fileURL = URL(fileURLWithPath: filePath)
        let directory = fileURL.deletingLastPathComponent()
        webView.loadFileURL(fileURL, allowingReadAccessTo: directory)
    }

    /// Re-enables text selection in PyVis HTML (often ships with user-select disabled).
    final class Coordinator: NSObject, WKNavigationDelegate {
        weak var webView: WKWebView?
        var lastLoadedPath: String?

        func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
            let script = """
            (function() {
              var style = document.createElement('style');
              style.textContent = '* { -webkit-user-select: text !important; user-select: text !important; }';
              document.head.appendChild(style);
            })();
            """
            webView.evaluateJavaScript(script, completionHandler: nil)
            webView.setNeedsDisplay(webView.bounds)
        }
    }
}

// MARK: - Preview

#Preview {
    ContentView()
}
