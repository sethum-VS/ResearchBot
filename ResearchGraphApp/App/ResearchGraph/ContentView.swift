import SwiftUI

struct ContentView: View {
    var body: some View {
        VStack(spacing: 20) {
            Image(systemName: "network")
                .imageScale(.large)
                .foregroundStyle(.tint)
            Text("Research Graph")
                .font(.largeTitle)
                .fontWeight(.bold)
            Text("Autonomous Domain Exploration")
                .font(.subheadline)
                .foregroundStyle(.secondary)
            
            Divider()
            
            Button(action: {
                print("Start Research Ingestion")
            }) {
                Label("Start New Research", systemName: "plus.circle.fill")
            }
            .buttonStyle(.borderedProminent)
            .controlSize(.large)
        }
        .padding()
        .frame(minWidth: 400, minHeight: 300)
    }
}

#Preview {
    ContentView()
}
