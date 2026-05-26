import SwiftUI

struct ProposalManifest: Codable, Identifiable {
    var id: String { proposal_id }
    let proposal_id: String
    let session_id: String
    let user_idea: String
    let scoped_query: String
    let matched_paper_count: Int
    let created_at: String
    let proposal_file: String
    
    var formattedDate: String {
        let formatter = DateFormatter()
        formatter.dateFormat = "yyyyMMdd'T'HHmmss'Z'"
        formatter.timeZone = TimeZone(identifier: "UTC")
        if let date = formatter.date(from: created_at) {
            let display = DateFormatter()
            display.dateStyle = .medium
            display.timeStyle = .short
            return display.string(from: date)
        }
        return created_at
    }
}

struct ProposalHistorySheet: View {
    let session: HistorySession
    @Bindable var bridge: PythonBridge
    var onReview: (ProposalResult) -> Void
    var onDismiss: () -> Void
    
    @State private var manifests: [ProposalManifest] = []
    
    var body: some View {
        NavigationStack {
            List(manifests) { manifest in
                VStack(alignment: .leading, spacing: 8) {
                    Text(manifest.formattedDate)
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(.secondary)
                    
                    Text(manifest.user_idea)
                        .font(.headline)
                        .lineLimit(2)
                    
                    Text("Scoped Query: \(manifest.scoped_query)")
                        .font(.subheadline)
                        .foregroundStyle(.secondary)
                        .lineLimit(2)
                    
                    HStack {
                        Label("\(manifest.matched_paper_count) Papers", systemImage: "doc.on.doc")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                        Spacer()
                        Button("Review") {
                            let path = URL(fileURLWithPath: session.absolutePath)
                                .appendingPathComponent("proposals")
                                .appendingPathComponent(manifest.proposal_file).path
                            
                            let result = ProposalResult(
                                status: "success",
                                message: "Loaded from archive.",
                                proposalPath: path,
                                sessionId: session.id,
                                scopedQuery: manifest.scoped_query,
                                matchedPaperCount: manifest.matched_paper_count,
                                proposalId: manifest.proposal_id
                            )
                            onReview(result)
                        }
                        .buttonStyle(.borderedProminent)
                    }
                }
                .padding(.vertical, 8)
            }
            .navigationTitle("Historical Proposals")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Close", action: onDismiss)
                }
            }
            .onAppear(perform: loadManifests)
        }
        .frame(minWidth: 500, minHeight: 400)
    }
    
    private func loadManifests() {
        let proposalsDir = URL(fileURLWithPath: session.absolutePath).appendingPathComponent("proposals")
        guard let files = try? FileManager.default.contentsOfDirectory(at: proposalsDir, includingPropertiesForKeys: nil) else { return }
        
        let manifestFiles = files.filter { $0.lastPathComponent.hasSuffix("_manifest.json") }
        var loaded: [ProposalManifest] = []
        for file in manifestFiles {
            if let data = try? Data(contentsOf: file),
               let manifest = try? JSONDecoder().decode(ProposalManifest.self, from: data) {
                loaded.append(manifest)
            }
        }
        loaded.sort { $0.created_at > $1.created_at }
        self.manifests = loaded
    }
}
