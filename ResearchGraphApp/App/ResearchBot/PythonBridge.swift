//
//  PythonBridge.swift
//  ResearchBot
//
//  Executes the Backend Python script asynchronously via Process().
//  Swift handles ONLY process management — no business logic here.
//

import Foundation

final class PythonBridge {

    /// Locates the Backend/main.py relative to the repo root.
    private var scriptURL: URL? {
        // Resolve path from the bundle's resource path up to the repo root.
        // During development the working directory is the repo root.
        let repoRoot: URL
        if let bundlePath = Bundle.main.resourceURL {
            // Walk up: ResearchGraphApp/App/ResearchBot.app/Contents/Resources → repo root
            repoRoot = bundlePath
                .deletingLastPathComponent() // Contents
                .deletingLastPathComponent() // .app
                .deletingLastPathComponent() // ResearchBot (xcodeproj dir)
                .deletingLastPathComponent() // App
                .deletingLastPathComponent() // ResearchGraphApp
        } else {
            repoRoot = URL(fileURLWithPath: FileManager.default.currentDirectoryPath)
        }
        let scriptPath = repoRoot
            .appendingPathComponent("ResearchGraphApp/Backend/main.py")
        return FileManager.default.fileExists(atPath: scriptPath.path) ? scriptPath : nil
    }

    /// Runs main.py asynchronously. Calls `completion` on the main queue with stdout output.
    func runIngestion(idea: String, url: String, completion: @escaping (String) -> Void) {
        guard let script = scriptURL else {
            completion("[Bridge Error] Could not locate Backend/main.py. Check repo structure.")
            return
        }

        DispatchQueue.global(qos: .userInitiated).async {
            let process = Process()
            process.executableURL = URL(fileURLWithPath: "/usr/bin/env")
            process.arguments = ["python3", script.path, "--idea", idea, "--url", url]

            let pipe = Pipe()
            let errorPipe = Pipe()
            process.standardOutput = pipe
            process.standardError = errorPipe

            do {
                try process.run()
                process.waitUntilExit()

                let stdout = String(data: pipe.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
                let stderr = String(data: errorPipe.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""

                let output = stdout.isEmpty ? (stderr.isEmpty ? "[No output]" : stderr) : stdout
                completion(output)
            } catch {
                completion("[Bridge Error] Failed to launch process: \(error.localizedDescription)")
            }
        }
    }
}
