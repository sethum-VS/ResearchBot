//
//  SetupWizardView.swift
//  ResearchBot
//
//  First-launch onboarding — captures API keys and writes Application Support `.env`.
//

import SwiftUI

struct SetupWizardView: View {
    var onComplete: () -> Void

    @State private var googleCloudProjectID = ""
    @State private var tavilyAPIKey = ""
    @State private var semanticScholarAPIKey = ""
    @State private var saveError: String?
    @State private var isSaving = false

    private var canSave: Bool {
        !googleCloudProjectID.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            && !tavilyAPIKey.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            header

            Form {
                Section {
                    LabeledContent("Google Cloud Project ID") {
                        TextField("my-gcp-project", text: $googleCloudProjectID)
                            .textFieldStyle(.roundedBorder)
                    }
                    Text("Used for Vertex AI Llama 4 Scout capabilities.")
                        .font(.caption)
                        .foregroundStyle(.secondary)

                    LabeledContent("Tavily API Key") {
                        SecureField("tvly-…", text: $tavilyAPIKey)
                            .textFieldStyle(.roundedBorder)
                    }

                    LabeledContent("Semantic Scholar API Key") {
                        SecureField("Optional — leave blank for keyless access", text: $semanticScholarAPIKey)
                            .textFieldStyle(.roundedBorder)
                    }
                    Text("Optional. The academic pipeline uses Semantic Scholar without an API key by default.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                } header: {
                    Text("API Configuration")
                } footer: {
                    Text(
                        "Note: Google Docs export functionality will securely authenticate via your web browser when you export your first project."
                    )
                    .fixedSize(horizontal: false, vertical: true)
                }
            }
            .formStyle(.grouped)

            if let saveError {
                Text(saveError)
                    .font(.caption)
                    .foregroundStyle(.red)
                    .padding(.horizontal, 24)
                    .padding(.bottom, 8)
            }

            HStack {
                Spacer()
                Button("Save & Initialize Workspace") {
                    saveAndContinue()
                }
                .keyboardShortcut(.defaultAction)
                .disabled(!canSave || isSaving)
            }
            .padding(24)
        }
        .frame(minWidth: 520, minHeight: 420)
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("Welcome to Autonomous Research Graph")
                .font(.title2.weight(.semibold))
            Text("Configure your API keys to initialize the research workspace.")
                .font(.subheadline)
                .foregroundStyle(.secondary)
        }
        .padding(.horizontal, 24)
        .padding(.top, 28)
        .padding(.bottom, 8)
    }

    private func saveAndContinue() {
        saveError = nil
        isSaving = true

        var keys: [String: String] = [
            "GOOGLE_CLOUD_PROJECT_ID": googleCloudProjectID.trimmingCharacters(in: .whitespacesAndNewlines),
            "TAVILY_API_KEY": tavilyAPIKey.trimmingCharacters(in: .whitespacesAndNewlines),
        ]
        let s2Key = semanticScholarAPIKey.trimmingCharacters(in: .whitespacesAndNewlines)
        if !s2Key.isEmpty {
            keys["SEMANTIC_SCHOLAR_API_KEY"] = s2Key
        }

        do {
            try EnvironmentManager.saveEnvironment(keys: keys)
            onComplete()
        } catch {
            saveError = "Could not save configuration: \(error.localizedDescription)"
        }

        isSaving = false
    }
}

#Preview {
    SetupWizardView(onComplete: {})
}
