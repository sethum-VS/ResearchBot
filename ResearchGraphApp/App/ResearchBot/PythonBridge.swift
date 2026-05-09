//
//  PythonBridge.swift
//  ResearchBot
//
//  Executes the Backend Python script asynchronously via Process().
//  Swift handles ONLY process management — no business logic here.
//

import Foundation

final class PythonBridge {

    /// Locates the Backend/main.py using environment variables or repository structure traversal.
    private var scriptURL: URL? {
        // 1. Check for Environment Variable (Best for Xcode testing)
        // Ensure you set BACKEND_PATH in Xcode Schema -> Arguments -> Environment Variables
        if let envPath = ProcessInfo.processInfo.environment["BACKEND_PATH"] {
            let url = URL(fileURLWithPath: envPath).appendingPathComponent("main.py")
            if FileManager.default.fileExists(atPath: url.path) {
                return url
            }
        }

        // 2. Fallback: Search up the directory tree (Useful for 'run.sh' and local dev)
        // This handles cases where the app is in a deep build/ folder.
        var currentURL = Bundle.main.bundleURL
        for _ in 0..<8 { // Traverse up to 8 levels to find the repo root
            let checkURL = currentURL.appendingPathComponent("Backend/main.py")
            if FileManager.default.fileExists(atPath: checkURL.path) {
                return checkURL
            }
            currentURL = currentURL.deletingLastPathComponent()
        }

        return nil
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
