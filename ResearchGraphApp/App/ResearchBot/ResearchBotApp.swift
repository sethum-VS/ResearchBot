//
//  ResearchBotApp.swift
//  ResearchBot
//
//  Created by Sethum Methsanda on 2026-05-09.
//

import SwiftUI

@main
struct ResearchBotApp: App {
    var body: some Scene {
        WindowGroup {
            ContentView()
                .appTextSelection()
        }
    }
}

// MARK: - App-wide text selection

extension View {
    /// Enables standard macOS copy/select for all `Text` in this view hierarchy.
    func appTextSelection() -> some View {
        textSelection(.enabled)
    }
}
