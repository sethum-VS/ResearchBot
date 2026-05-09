//
//  ContentView.swift
//  ResearchBot
//
//  Created by Sethum Methsanda on 2026-05-09.
//

import SwiftUI

struct ContentView: View {
    @State private var idea: String = ""
    @State private var url: String = ""
    @State private var outputLog: String = "Output will appear here..."
    @State private var isRunning: Bool = false

    private let bridge = PythonBridge()

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            // Header
            Label("Research Graph", systemImage: "network")
                .font(.title2.bold())
                .padding(.bottom, 4)

            // Idea input
            VStack(alignment: .leading, spacing: 4) {
                Text("Research Idea").font(.headline)
                TextEditor(text: $idea)
                    .font(.body)
                    .frame(minHeight: 100)
                    .overlay(RoundedRectangle(cornerRadius: 8).stroke(Color.secondary.opacity(0.3)))
            }

            // URL input
            VStack(alignment: .leading, spacing: 4) {
                Text("Seed URL (optional)").font(.headline)
                TextField("https://...", text: $url)
                    .textFieldStyle(.roundedBorder)
            }

            // Run button
            Button {
                runResearch()
            } label: {
                if isRunning {
                    ProgressView().controlSize(.small)
                    Text("Running…")
                } else {
                    Label("Run Research", systemImage: "play.circle.fill")
                }
            }
            .buttonStyle(.borderedProminent)
            .controlSize(.large)
            .disabled(isRunning || idea.trimmingCharacters(in: .whitespaces).isEmpty)

            // Output console
            VStack(alignment: .leading, spacing: 4) {
                Text("Output").font(.headline)
                ScrollView {
                    Text(outputLog)
                        .font(.system(.body, design: .monospaced))
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(8)
                }
                .frame(minHeight: 120)
                .background(Color(NSColor.textBackgroundColor))
                .overlay(RoundedRectangle(cornerRadius: 8).stroke(Color.secondary.opacity(0.3)))
            }
        }
        .padding()
        .frame(minWidth: 500, minHeight: 500)
    }

    private func runResearch() {
        isRunning = true
        outputLog = "Starting ingestion...\n"
        bridge.runIngestion(idea: idea, url: url) { result in
            DispatchQueue.main.async {
                outputLog = result
                isRunning = false
            }
        }
    }
}

#Preview {
    ContentView()
}
