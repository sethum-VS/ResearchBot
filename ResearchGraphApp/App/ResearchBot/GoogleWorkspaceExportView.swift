//
//  GoogleWorkspaceExportView.swift
//  ResearchBot
//
//  Shared UI for triggering export_to_workspace via PythonBridge.
//

import SwiftUI

struct GoogleWorkspaceExportButton: View {
    @Bindable var bridge: PythonBridge
    let sessionId: String
    let kbRoot: String?
    var prominent: Bool = true

    @State private var showSuccessAlert = false
    @State private var showErrorAlert = false

    private var awaitingOAuth: Bool {
        bridge.isExportingWorkspace && !PythonBridge.hasGoogleOAuthToken
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            if bridge.isExportingWorkspace {
                HStack(spacing: 10) {
                    ProgressView()
                        .controlSize(.small)
                    Text(awaitingOAuth
                         ? "Waiting for Google Login in Browser…"
                         : "Exporting to Google Workspace…")
                        .font(.subheadline)
                        .foregroundStyle(.secondary)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            } else if prominent {
                Button {
                    bridge.exportToWorkspace(sessionId: sessionId, kbRoot: kbRoot)
                } label: {
                    Label("Export to Google Workspace", systemImage: "doc.badge.arrow.up")
                }
                .buttonStyle(.borderedProminent)
                .disabled(sessionId.isEmpty)
            } else {
                Button {
                    bridge.exportToWorkspace(sessionId: sessionId, kbRoot: kbRoot)
                } label: {
                    Label("Export to Google Workspace", systemImage: "doc.badge.arrow.up")
                }
                .buttonStyle(.bordered)
                .disabled(sessionId.isEmpty)
            }
        }
        .onChange(of: bridge.isExportingWorkspace) { wasExporting, isExporting in
            guard wasExporting, !isExporting else { return }
            if bridge.workspaceExportError != nil {
                showErrorAlert = true
            } else if bridge.masterDocumentURL != nil {
                showSuccessAlert = true
            }
        }
        .alert("Exported to Google Workspace", isPresented: $showSuccessAlert) {
            if let urlString = bridge.masterDocumentURL, let url = URL(string: urlString) {
                Button("Open Master Document") {
                    NSWorkspace.shared.open(url)
                }
            }
            Button("OK", role: .cancel) {}
        } message: {
            if let msg = bridge.workspaceExportMessage {
                Text(msg)
            } else {
                Text("Your run was added to the Master Tracking Document.")
            }
        }
        .alert("Export Failed", isPresented: $showErrorAlert) {
            Button("OK", role: .cancel) {
                bridge.workspaceExportError = nil
            }
        } message: {
            Text(bridge.workspaceExportError ?? "Unknown error")
        }
    }
}
