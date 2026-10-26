import Foundation
import Darwin

enum TestFailure: Error, CustomStringConvertible {
    case failed(String)
    var description: String { switch self { case .failed(let message): return message } }
}

func expect(_ condition: @autoclosure () throws -> Bool, _ message: String) throws {
    if try !condition() { throw TestFailure.failed(message) }
}

@MainActor
final class BackendSuite {
    let tools: URL
    let output: URL
    var passed = 0
    var failed = 0
    let manager = FileManager.default

    init(tools: URL, output: URL) { self.tools = tools; self.output = output }

    func test(_ name: String, _ body: () async throws -> Void) async {
        do { try await body(); passed += 1; print("PASS \(name)") }
        catch { failed += 1; print("FAIL \(name): \(error)") }
    }

    func wait(_ label: String, seconds: Double = 15, until condition: () -> Bool) async throws {
        let deadline = Date().addingTimeInterval(seconds)
        while !condition() {
            if Date() > deadline { throw TestFailure.failed("Timed out: \(label)") }
            try await Task.sleep(nanoseconds: 30_000_000)
        }
    }

    func mode(_ value: String) throws {
        try value.write(to: tools.appendingPathComponent("mode"), atomically: true, encoding: .utf8)
    }

    func model(_ folder: String) -> DownloadModel {
        DownloadModel(toolsDirectory: tools, outputDirectory: output.appendingPathComponent(folder))
    }

    func ready(_ model: DownloadModel) async throws {
        model.urlText = "https://www.youtube.com/watch?v=BaW_jenozKc&list=PLignored"
        model.inspect()
        try await wait("inspect") { !model.isBusy }
        try expect(model.stage == "ready", "Expected ready, got \(model.stage): \(model.errorText ?? "")")
        try expect(model.canDownload, "Inspected single video should be downloadable")
    }

    func save(_ model: DownloadModel) async throws -> URL {
        model.download()
        try await wait("download and conversion", seconds: 30) { !model.isBusy }
        try expect(model.stage == "completed", "Expected completed, got \(model.stage): \(model.errorText ?? ""); \(model.logText)")
        guard let result = model.lastOutput else { throw TestFailure.failed("Missing lastOutput") }
        return result
    }

    func probe(_ file: URL) async throws -> [String: Any] {
        let result = try await ManagedTool().run(executable: tools.appendingPathComponent("ffprobe"), arguments: [
            "-v", "error", "-show_entries", "stream=codec_name,codec_type,width,height,pix_fmt:format=format_name,duration",
            "-of", "json", file.path])
        try expect(result.status == 0, "ffprobe failed: \(result.errorOutput)")
        guard let json = try JSONSerialization.jsonObject(with: result.stdout) as? [String: Any] else {
            throw TestFailure.failed("Invalid probe JSON")
        }
        return json
    }

    func scratchGone(_ directory: URL) -> Bool {
        let contents = (try? manager.contentsOfDirectory(atPath: directory.path)) ?? []
        return !contents.contains { $0.hasPrefix(".영상저장-") }
    }

    func run() async {
        await test("Threads media route and selected video canonicalization") {
            let base = "https://www.threads.com/@test/post/ABCdef123"
            try expect(try canonicalVideoURL(base + "/media").absoluteString == base, "media path was not normalized")
            try expect(try canonicalVideoURL(base + "/media?vitract_video=2").absoluteString == base + "?vitract_video=2", "selection was lost")
        }

        await test("SNS post URLs accepted and unsafe URLs rejected") {
            for url in ["https://www.instagram.com/reel/ABCdef123/?igsh=tracking", "https://x.com/test/status/123456789?s=20", "https://www.threads.net/@test/post/ABCdef123", "https://www.threads.com/@test/post/ABCdef123/media", "https://www.facebook.com/reel/123456789"] {
                _ = try canonicalVideoURL(url)
            }
            for url in ["https://instagram.com.evil.test/reel/ABCdef123/", "https://x.com/test", "https://threads.com/@test", "https://user:pass@instagram.com/reel/ABCdef123/"] {
                do { _ = try canonicalVideoURL(url); throw TestFailure.failed("Accepted invalid SNS URL") }
                catch is DownloadFailure { }
            }
        }
        await test("SNS inspection and MP4 publication use the accepted link") {
            try mode("normal")
            for (i, url) in ["https://www.instagram.com/reel/ABCdef123/", "https://x.com/test/status/123456789", "https://www.threads.com/@test/post/ABCdef123"].enumerated() {
                let m = model("sns-\(i)")
                m.urlText = url
                m.inspect()
                try await wait("SNS inspect") { !m.isBusy }
                try expect(m.canDownload, "SNS inspection blocked: \(m.errorText ?? "")")
                _ = try await save(m)
            }
        }

        await test("caption TXT extraction and collision-safe repeated save") {
            try mode("normal")
            let m = model("captions")
            try await ready(m)
            try expect(m.captions.count == 2 && m.selectedCaption == "ko", "Language selection failed")
            m.extractTranscript()
            try await wait("captions") { !m.isBusy }
            try expect(m.stage == "completed", "Caption save failed")
            let first = m.lastOutput!
            let text = try String(contentsOf: first, encoding: .utf8)
            try expect(text.contains("안녕하세요. 테스트입니다.\n다음 문장입니다."), "Text lost or malformed")
            try expect(first.pathExtension == "txt", "Expected TXT")
            m.extractTranscript()
            try await wait("captions second save") { !m.isBusy }
            try expect(m.stage == "completed" && m.lastOutput != first, "Existing script overwritten")
            try expect(m.lastOutput?.pathExtension == "txt", "Collision extension wrong")
            let empty = Data("{\"events\":[]}".utf8)
            do { _ = try transcriptText(empty); throw TestFailure.failed("Accepted empty transcript") }
            catch is DownloadFailure { }
        }

        await test("canonical single-video links and playlist suppression") {
            let expected = "https://www.youtube.com/watch?v=BaW_jenozKc"
            let accepted = [
                "https://www.youtube.com/watch?v=BaW_jenozKc&list=PL123&index=2",
                "  youtu.be/BaW_jenozKc?t=4  ",
                "https://m.youtube.com/watch?v=BaW_jenozKc",
                "https://youtube.com/shorts/BaW_jenozKc?si=tracking",
                "https://youtube.com/live/BaW_jenozKc",
                "https://www.youtube.com/embed/BaW_jenozKc",
                "https://music.youtube.com/watch?v=BaW_jenozKc",
            ]
            for input in accepted {
                try expect(try canonicalYouTubeURL(input).absoluteString == expected, "Unexpected canonical URL for \(input)")
            }
        }

        await test("reject spoofed hosts, credentials, redirects, playlists, malformed IDs") {
            let rejected = [
                "https://youtube.com.evil.example/watch?v=BaW_jenozKc",
                "https://evilyoutube.com/watch?v=BaW_jenozKc",
                "https://youtube.com@evil.example/watch?v=BaW_jenozKc",
                "https://evil@youtube.com/watch?v=BaW_jenozKc",
                "https://youtube.com:8080/watch?v=BaW_jenozKc",
                "https://youtube.com/redirect?q=https://evil.example",
                "https://youtube.com/playlist?list=PL123",
                "https://youtube.com/@channel",
                "file:///etc/passwd", "javascript:alert(1)",
                "https://127.0.0.1/watch?v=BaW_jenozKc",
                "https://youtube.com/watch?v=BaW_jenozKc&v=aqz-KE-bpKQ",
                "https://youtube.com/watch?v=../etc/passwd",
                "https://youtu.be/BaW_jenozKc/extra",
                "https://youtu.be/BaW_jenozKc%0A--exec",
            ]
            for input in rejected {
                do { _ = try canonicalYouTubeURL(input); throw TestFailure.failed("Accepted unsafe input \(input)") }
                catch is DownloadFailure { }
            }
        }

        await test("AV1 + Opus in MP4 becomes playable H.264 + AAC MP4 through model") {
            try mode("normal")
            let sourceInfo = try await probe(tools.appendingPathComponent("synthetic-av1-opus.mp4"))
            let sourceStreams = sourceInfo["streams"] as! [[String: Any]]
            try expect(sourceStreams.contains { $0["codec_name"] as? String == "av1" }, "Fixture must contain AV1")
            try expect(sourceStreams.contains { $0["codec_name"] as? String == "opus" }, "Fixture must contain Opus")
            let model = model("conversion")
            try await ready(model)
            try expect(model.metadata?.heights == [360, 180], "Metadata quality choices must be sorted and present")
            model.selectedHeight = 180
            let file = try await save(model)
            let info = try await probe(file)
            let streams = info["streams"] as! [[String: Any]]
            try expect(file.pathExtension == "mp4", "Final extension is not MP4")
            try expect(streams.contains { $0["codec_name"] as? String == "h264" && $0["pix_fmt"] as? String == "yuv420p" }, "Video is not H.264/yuv420p")
            try expect(streams.contains { $0["codec_name"] as? String == "aac" }, "Audio is not AAC")
            let duration = Double((info["format"] as? [String: Any])?["duration"] as? String ?? "") ?? 0
            try expect(abs(duration - 1.2) < 0.15, "Duration was not preserved: \(duration)")
            let firstBytes = try Data(contentsOf: file)
            let another = try await save(model)
            try expect(another != file, "Repeated save reused existing filename")
            try expect(try Data(contentsOf: file) == firstBytes, "Original file was overwritten")
            try expect(scratchGone(model.outputDirectory), "Conversion scratch directory remains")
            let calls = try String(contentsOf: tools.appendingPathComponent("calls.jsonl"), encoding: .utf8)
                .split(separator: "\n").map { try JSONSerialization.jsonObject(with: Data($0.utf8)) as! [String] }
            guard let downloadCall = calls.last(where: { $0.contains("-f") }) else { throw TestFailure.failed("Missing download invocation") }
            try expect(downloadCall.contains("bv*[height<=?180]+ba/b[height<=?180]"), "Selected height not honored by yt-dlp selector")
            try expect(downloadCall.contains("--no-playlist") && downloadCall.contains("--ignore-config"), "Single-video safety flags missing")
            try expect(downloadCall.last == "https://www.youtube.com/watch?v=BaW_jenozKc", "Tracking/playlist parameters passed to tool")
            print("  validated codecs=h264,aac duration=\(duration)s bytes=\(firstBytes.count)")
        }

        await test("changed links cannot reuse old metadata; selection and output directory retained") {
            try mode("normal")
            let model = model("settings")
            let directory = model.outputDirectory
            try await ready(model)
            model.selectedHeight = 180
            model.urlText = "https://youtu.be/aqz-KE-bpKQ"
            try expect(!model.canDownload, "Changed video can use stale metadata")
            model.download()
            try expect(!model.isBusy, "Changed link started downloading old video")
            try await ready(model)
            try expect(model.selectedHeight == 180 && model.outputDirectory == directory, "Selected quality or folder reset")
        }

        await test("long emoji title creates an actual file under filesystem byte limit") {
            let title = String(repeating: "👨‍👩‍👧‍👦", count: 75) + " ../:" + String(repeating: "가", count: 100)
            try title.write(to: tools.appendingPathComponent("title.txt"), atomically: true, encoding: .utf8)
            defer { try? manager.removeItem(at: tools.appendingPathComponent("title.txt")) }
            let filename = safeVideoFilename(title, videoID: "BaW_jenozKc")
            try expect(filename.utf8.count <= 240, "Filename too long in UTF-8 bytes: \(filename.utf8.count)")
            try expect(!filename.contains("/") && !filename.contains(":"), "Forbidden filename characters survived")
            let model = model("emoji")
            try await ready(model)
            let saved = try await save(model)
            try expect(manager.fileExists(atPath: saved.path), "Emoji title final file missing")
        }

        await test("atomic publication preserves a preexisting unrelated file") {
            let directory = output.appendingPathComponent("publication")
            try manager.createDirectory(at: directory, withIntermediateDirectories: true)
            let existing = directory.appendingPathComponent("existing.mp4")
            let staged = directory.appendingPathComponent("new.mp4")
            let oldBytes = Data("unrelated original bytes".utf8)
            let newBytes = Data("new final bytes".utf8)
            try oldBytes.write(to: existing)
            try newBytes.write(to: staged)
            let result = try publishMP4Exclusively(staged: staged, directory: directory, filename: "existing.mp4")
            try expect(result != existing, "Publication selected occupied filename")
            try expect(try Data(contentsOf: existing) == oldBytes, "Publication overwrote unrelated file")
            try expect(try Data(contentsOf: result) == newBytes, "Published bytes are incorrect")
        }

        await test("malformed metadata, live broadcasts, and process failures recover") {
            let model = model("errors")
            for failureMode in ["malformed", "live", "fail"] {
                try mode(failureMode)
                model.urlText = "https://youtu.be/BaW_jenozKc"
                model.inspect()
                try await wait("expected error \(failureMode)") { !model.isBusy }
                try expect(model.stage == "error" && model.errorText != nil && !model.canDownload, "Failure mode \(failureMode) was not reported")
            }
            try mode("normal")
            try await ready(model)
        }

        await test("cancel before process creation cannot affect the next operation") {
            try mode("normal")
            let model = model("cancel-race")
            for _ in 0..<20 {
                model.urlText = "https://youtu.be/BaW_jenozKc"
                model.inspect()
                model.cancel()
                try expect(model.stage == "cancelled" && !model.isBusy, "Immediate cancellation state incorrect")
            }
            try await ready(model)
            _ = try await save(model)
        }

        await test("cancellation kills stubborn descendants holding stdout pipes") {
            try mode("normal")
            let model = model("cancel-tree")
            try await ready(model)
            try mode("hang")
            model.download()
            let pidFile = tools.appendingPathComponent("descendant.pid")
            try await wait("fixture descendant spawn") { manager.fileExists(atPath: pidFile.path) }
            let descendant = Int32(try String(contentsOf: pidFile, encoding: .utf8))!
            let parent = Int32(try String(contentsOf: tools.appendingPathComponent("parent.pid"), encoding: .utf8))!
            defer { Darwin.kill(descendant, SIGKILL); Darwin.kill(parent, SIGKILL) }
            let started = Date()
            model.cancel()
            try await wait("descendant termination and pipe cleanup", seconds: 8) {
                Darwin.kill(descendant, 0) == -1 && errno == ESRCH && scratchGone(model.outputDirectory)
            }
            try expect(model.stage == "cancelled" && model.lastOutput == nil, "Cancelled download produced success")
            let remaining = (try? manager.contentsOfDirectory(atPath: model.outputDirectory.path)) ?? []
            try expect(remaining.isEmpty, "Cancelled job left files: \(remaining)")
            print("  descendant process group cleaned in \(String(format: "%.2f", Date().timeIntervalSince(started)))s")
            try mode("normal")
            _ = try await save(model)
        }
        print("RESULT \(passed) passed; \(failed) failed")
    }
}

@main
struct BackendTests {
    @MainActor
    static func main() async {
        guard CommandLine.arguments.count == 3 else { print("Expected tools and output directories"); exit(2) }
        let suite = BackendSuite(tools: URL(fileURLWithPath: CommandLine.arguments[1]),
                                 output: URL(fileURLWithPath: CommandLine.arguments[2]))
        await suite.run()
        exit(suite.failed == 0 ? 0 : 1)
    }
}
