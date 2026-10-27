import AppKit
import Combine
import Darwin
import Foundation

struct CaptionTrack: Identifiable {
    let id: String
    let language: String
    let automatic: Bool
    var label: String { "\(language) · \(automatic ? "자동 자막" : "제작자 자막")" }
}

func captionTracks(_ json: [String: Any]) -> [CaptionTrack] {
    var result: [CaptionTrack] = []
    for (key, automatic) in [("subtitles", false), ("automatic_captions", true)] {
        let tracks = json[key] as? [String: [[String: Any]]] ?? [:]
        for (language, formats) in tracks where language != "live_chat" {
            guard formats.contains(where: { $0["ext"] as? String == "json3" }) else { continue }
            if result.contains(where: { $0.language == language }) { continue }
            result.append(CaptionTrack(id: language, language: language, automatic: automatic))
        }
    }
    func rank(_ s: String) -> Int { s == "ko" ? 0 : s == "en" ? 1 : 2 }
    return result.sorted { rank($0.language) == rank($1.language) ? $0.language < $1.language : rank($0.language) < rank($1.language) }
}

func transcriptText(_ data: Data) throws -> String {
    guard let json = try JSONSerialization.jsonObject(with: data) as? [String: Any],
          let events = json["events"] as? [[String: Any]] else {
        throw DownloadFailure.message("자막 형식을 읽을 수 없습니다.")
    }
    var lines: [String] = []
    for event in events {
        let segments = event["segs"] as? [[String: Any]] ?? []
        let text = segments.compactMap { $0["utf8"] as? String }.joined()
            .split(whereSeparator: { $0.isWhitespace }).joined(separator: " ")
        if !text.isEmpty { lines.append(text) }
    }
    guard !lines.isEmpty else { throw DownloadFailure.message("자막에 추출할 대사가 없습니다.") }
    return lines.joined(separator: "\n") + "\n"
}

struct VideoMetadata {
    let title: String
    let uploader: String
    let duration: Double
    let thumbnailURL: URL?
    let heights: [Int]
}

enum DownloadFailure: LocalizedError {
    case message(String)
    var errorDescription: String? {
        switch self { case .message(let message): return message }
    }
}

/// Returns a canonical single-video URL; playlist and non-YouTube URLs never reach a tool.
func canonicalYouTubeURL(_ input: String) throws -> URL {
    var value = input.trimmingCharacters(in: .whitespacesAndNewlines)
    if !value.contains("://") { value = "https://" + value }
    guard let parts = URLComponents(string: value),
          let scheme = parts.scheme?.lowercased(), ["https", "http"].contains(scheme),
          parts.user == nil, parts.password == nil,
          parts.port == nil || parts.port == 443,
          let host = parts.host?.lowercased() else {
        throw DownloadFailure.message("올바른 유튜브 영상 링크를 붙여 넣어 주세요.")
    }
    let path = parts.path.split(separator: "/").map(String.init)
    var videoID: String?
    if ["youtu.be", "www.youtu.be"].contains(host), path.count == 1 {
        videoID = path[0]
    } else if ["youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"].contains(host) {
        if path == ["watch"] {
            let ids = (parts.queryItems ?? []).filter { $0.name == "v" }
            if ids.count == 1 { videoID = ids[0].value }
        } else if path.count == 2, ["shorts", "live", "embed"].contains(path[0]) {
            videoID = path[1]
        }
    }
    guard let videoID, videoID.range(of: "^[A-Za-z0-9_-]{11}$", options: .regularExpression) != nil,
          let url = URL(string: "https://www.youtube.com/watch?v=\(videoID)") else {
        throw DownloadFailure.message("재생목록이나 채널 주소 대신 개별 영상의 유튜브 링크를 넣어 주세요.")
    }
    return url
}

/// Accept only direct posts from supported platforms; strip tracking parameters.
func canonicalVideoURL(_ input: String) throws -> URL {
    if let youtube = try? canonicalYouTubeURL(input) { return youtube }
    var value = input.trimmingCharacters(in: .whitespacesAndNewlines)
    if !value.contains("://") { value = "https://" + value }
    guard let c = URLComponents(string: value), ["http", "https"].contains(c.scheme ?? ""),
          c.user == nil, c.password == nil, c.port == nil || c.port == 443,
          let host = c.host?.lowercased(), !c.percentEncodedPath.contains("%"), !c.path.contains("\\") else {
        throw DownloadFailure.message("올바른 영상 게시물 링크를 입력해 주세요.")
    }
    let path = c.path
    func matches(_ pattern: String) -> Bool { path.range(of: pattern, options: .regularExpression) != nil }
    var target: String?
    if ["instagram.com", "www.instagram.com"].contains(host), matches("^/(p|reel|reels|tv)/[A-Za-z0-9_-]{5,64}/?$") {
        target = "https://www.instagram.com" + path
    } else if ["x.com", "www.x.com", "twitter.com", "www.twitter.com", "mobile.twitter.com"].contains(host),
              matches("^/[A-Za-z0-9_]+/status/[0-9]+(/video/[0-9]+)?/?$") {
        target = "https://x.com" + path
    } else if ["threads.com", "www.threads.com", "threads.net", "www.threads.net"].contains(host),
              matches("^/@[A-Za-z0-9._]{1,30}/post/[A-Za-z0-9_-]{5,64}(/media)?/?$") {
        target = "https://www.threads.com" + path.replacingOccurrences(of: "/media/?$", with: "", options: .regularExpression)
        if let index = c.queryItems?.first(where: { $0.name == "vitract_video" })?.value, let n = Int(index), n > 0, n <= 100 {
            target! += "?vitract_video=\(n)"
        }
    } else if ["facebook.com", "www.facebook.com", "m.facebook.com", "web.facebook.com"].contains(host) {
        if matches("^/(reel/[0-9]+|[A-Za-z0-9.]+/videos/[0-9]+)/?$") { target = "https://www.facebook.com" + path }
        else if path == "/watch" || path == "/watch/", let v = c.queryItems?.first(where: { $0.name == "v" })?.value,
                v.range(of: "^[0-9]+$", options: .regularExpression) != nil { target = "https://www.facebook.com/watch/?v=" + v }
    }
    guard let target, let url = URL(string: target) else {
        throw DownloadFailure.message("YouTube · Instagram · Threads · X · Facebook의 개별 영상 게시물 링크를 넣어 주세요. 프로필·목록·단축 공유 링크는 지원하지 않습니다.")
    }
    return url
}

func safeVideoFilename(_ title: String, videoID: String) -> String {
    let forbidden = CharacterSet(charactersIn: "/\\:<>\"|?*\0").union(.controlCharacters)
    let cleaned = title.unicodeScalars.map { forbidden.contains($0) ? " " : String($0) }.joined()
        .split(whereSeparator: { $0.isWhitespace }).joined(separator: " ")
        .trimmingCharacters(in: CharacterSet(charactersIn: ". "))
    var stem = ""
    // APFS limits names by UTF-8 bytes; emoji graphemes may contain many scalar values.
    for character in (cleaned.isEmpty ? "유튜브 영상" : cleaned) {
        let candidate = String(character)
        if stem.utf8.count + candidate.utf8.count > 180 { break }
        stem.append(character)
    }
    if stem.isEmpty { stem = "유튜브 영상" }
    return "\(stem) [\(videoID)].mp4"
}

/// Both paths are on the chosen output volume. RENAME_EXCL is atomic and never replaces a file.
func publishMP4Exclusively(staged: URL, directory: URL, filename: String) throws -> URL {
    let base = (filename as NSString).deletingPathExtension
    let ext = (filename as NSString).pathExtension
    for number in 1...10_000 {
        let name = number == 1 ? filename : "\(base) (\(number)).\(ext)"
        let destination = directory.appendingPathComponent(name)
        let result = staged.path.withCString { source in
            destination.path.withCString { target in renamex_np(source, target, UInt32(RENAME_EXCL)) }
        }
        if result == 0 { return destination }
        if errno == EEXIST { continue }
        let reason = String(cString: strerror(errno))
        throw DownloadFailure.message("완성된 파일을 저장하지 못했습니다: \(reason)")
    }
    throw DownloadFailure.message("같은 이름의 파일이 너무 많습니다. 다른 저장 폴더를 선택해 주세요.")
}

struct ToolLocations {
    let ytdlp: URL?
    let ffmpeg: URL?
    let ffprobe: URL?
    let node: URL?

    init(directoryOverride: URL? = nil) {
        let manager = FileManager.default
        var roots: [URL] = []
        if let directoryOverride { roots.append(directoryOverride) }
        if let resources = Bundle.main.resourceURL { roots.append(resources.appendingPathComponent("tools")) }
        roots.append(URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent().appendingPathComponent("Tools"))
        roots.append(URL(fileURLWithPath: manager.currentDirectoryPath).appendingPathComponent("Tools"))
        roots += [URL(fileURLWithPath: "/opt/homebrew/bin"), URL(fileURLWithPath: "/usr/local/bin")]
        func find(_ name: String) -> URL? {
            roots.map { $0.appendingPathComponent(name) }.first { manager.isExecutableFile(atPath: $0.path) }
        }
        ytdlp = find("yt-dlp")
        ffmpeg = find("ffmpeg")
        ffprobe = find("ffprobe")
        node = find("node")
    }

    var ready: Bool { ytdlp != nil && ffmpeg != nil && ffprobe != nil && node != nil }
    var missingNames: String {
        [("yt-dlp", ytdlp), ("FFmpeg", ffmpeg), ("FFprobe", ffprobe), ("Node.js", node)]
            .filter { $0.1 == nil }.map(\.0).joined(separator: ", ")
    }
}

enum ToolStream { case stdout, stderr }

struct ToolResult {
    let status: Int32
    let stdout: Data
    let stderr: Data
    var errorOutput: String { String(decoding: stderr, as: UTF8.self) }
}

private final class ToolCapture: @unchecked Sendable {
    private(set) var data = Data()
    private var pending = Data()
    private let limit: Int
    private let callback: (String) -> Void

    init(limit: Int, callback: @escaping (String) -> Void) {
        self.limit = limit
        self.callback = callback
    }

    func readAll(from fd: Int32) {
        defer { Darwin.close(fd) }
        var buffer = [UInt8](repeating: 0, count: 16_384)
        while true {
            let count = Darwin.read(fd, &buffer, buffer.count)
            if count < 0, errno == EINTR { continue }
            if count <= 0 { break }
            let chunk = Data(buffer.prefix(count))
            if data.count < limit { data.append(chunk.prefix(limit - data.count)) }
            pending.append(chunk)
            while let newline = pending.firstIndex(of: 10) {
                let line = String(decoding: pending[..<newline], as: UTF8.self)
                    .trimmingCharacters(in: .newlines)
                pending.removeSubrange(...newline)
                callback(line)
            }
            // Progress lines are tiny. A metadata JSON line is captured separately in data.
            if pending.count > 4_194_304 { pending.removeAll(keepingCapacity: false) }
        }
        if !pending.isEmpty { callback(String(decoding: pending, as: UTF8.self)) }
    }
}

/// Each invocation leads its own process group, so cancellation also reaches child tools.
final class ManagedTool: @unchecked Sendable {
    private let lock = NSLock()
    private var processID: pid_t = 0
    private var cancelled = false

    func cancel() {
        lock.lock()
        cancelled = true
        let pid = processID
        if pid > 0 { Darwin.kill(-pid, SIGINT) }
        lock.unlock()
        guard pid > 0 else { return }
        for (delay, signal) in [(1.5, SIGTERM), (3.0, SIGKILL)] {
            DispatchQueue.global(qos: .utility).asyncAfter(deadline: .now() + delay) { [weak self] in
                guard let self else { return }
                self.lock.lock()
                if self.processID == pid { Darwin.kill(-pid, signal) }
                self.lock.unlock()
            }
        }
    }

    func run(executable: URL, arguments: [String],
             onLine: @escaping (ToolStream, String) -> Void = { _, _ in }) async throws -> ToolResult {
        try Task.checkCancellation()
        return try await withCheckedThrowingContinuation { continuation in
            DispatchQueue.global(qos: .userInitiated).async {
                self.lock.lock()
                if self.cancelled {
                    self.lock.unlock()
                    continuation.resume(throwing: CancellationError())
                    return
                }
                var outFD: [Int32] = [0, 0]
                var errFD: [Int32] = [0, 0]
                guard pipe(&outFD) == 0 else {
                    self.lock.unlock()
                    continuation.resume(throwing: DownloadFailure.message("도구 출력 연결을 만들지 못했습니다."))
                    return
                }
                guard pipe(&errFD) == 0 else {
                    outFD.forEach { Darwin.close($0) }
                    self.lock.unlock()
                    continuation.resume(throwing: DownloadFailure.message("도구 출력 연결을 만들지 못했습니다."))
                    return
                }
                var actions: posix_spawn_file_actions_t?
                var attributes: posix_spawnattr_t?
                posix_spawn_file_actions_init(&actions)
                posix_spawnattr_init(&attributes)
                posix_spawn_file_actions_addopen(&actions, STDIN_FILENO, "/dev/null", O_RDONLY, 0)
                posix_spawn_file_actions_adddup2(&actions, outFD[1], STDOUT_FILENO)
                posix_spawn_file_actions_adddup2(&actions, errFD[1], STDERR_FILENO)
                for fd in outFD + errFD { posix_spawn_file_actions_addclose(&actions, fd) }
                posix_spawnattr_setflags(&attributes, Int16(POSIX_SPAWN_SETPGROUP))
                posix_spawnattr_setpgroup(&attributes, 0)
                var environment = ProcessInfo.processInfo.environment
                environment["LANG"] = "en_US.UTF-8"
                environment["LC_ALL"] = "en_US.UTF-8"
                environment["PATH"] = executable.deletingLastPathComponent().path + ":/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
                let argv = ([executable.path] + arguments).map { strdup($0) } + [nil]
                let envp = environment.map { strdup("\($0.key)=\($0.value)") } + [nil]
                var pid: pid_t = 0
                let spawnResult = argv.withUnsafeBufferPointer { argvBuffer in
                    envp.withUnsafeBufferPointer { envBuffer in
                        posix_spawn(&pid, executable.path, &actions, &attributes,
                                    UnsafeMutablePointer(mutating: argvBuffer.baseAddress!),
                                    UnsafeMutablePointer(mutating: envBuffer.baseAddress!))
                    }
                }
                argv.forEach { free($0) }
                envp.forEach { free($0) }
                posix_spawn_file_actions_destroy(&actions)
                posix_spawnattr_destroy(&attributes)
                Darwin.close(outFD[1])
                Darwin.close(errFD[1])
                if spawnResult != 0 {
                    Darwin.close(outFD[0])
                    Darwin.close(errFD[0])
                    self.lock.unlock()
                    continuation.resume(throwing: DownloadFailure.message("\(executable.lastPathComponent)을 실행하지 못했습니다: \(String(cString: strerror(spawnResult)))"))
                    return
                }
                self.processID = pid
                self.lock.unlock()
                let stdout = ToolCapture(limit: 33_554_432) { onLine(.stdout, $0) }
                let stderr = ToolCapture(limit: 1_048_576) { onLine(.stderr, $0) }
                let stdoutReadFD = outFD[0]
                let stderrReadFD = errFD[0]
                let readers = DispatchGroup()
                readers.enter()
                DispatchQueue.global(qos: .utility).async { stdout.readAll(from: stdoutReadFD); readers.leave() }
                readers.enter()
                DispatchQueue.global(qos: .utility).async { stderr.readAll(from: stderrReadFD); readers.leave() }
                var rawStatus: Int32 = 0
                while waitpid(pid, &rawStatus, 0) < 0 { if errno != EINTR { break } }
                readers.wait()
                self.lock.lock()
                self.processID = 0
                let wasCancelled = self.cancelled
                self.lock.unlock()
                if wasCancelled {
                    continuation.resume(throwing: CancellationError())
                } else {
                    let signal = rawStatus & 0x7f
                    let status = signal == 0 ? (rawStatus >> 8) & 0xff : 128 + signal
                    continuation.resume(returning: ToolResult(status: status, stdout: stdout.data, stderr: stderr.data))
                }
            }
        }
    }
}

@MainActor
final class DownloadModel: ObservableObject {
    @Published var urlText = ""
    @Published var metadata: VideoMetadata?
    @Published var selectedHeight = 1080
    @Published var captions: [CaptionTrack] = []
    @Published var selectedCaption = ""
    @Published var postVideoCount = 1
    @Published var postVideoIndex = 1
    @Published var outputDirectory: URL
    @Published var stage = "idle"
    @Published var statusText = "영상 링크를 붙여 넣어 주세요"
    @Published var progress = 0.0
    @Published var detailText = ""
    @Published var lastOutput: URL?
    @Published var errorText: String?
    @Published var logText = ""

    private let tools: ToolLocations
    private var inspectedURL: URL?
    private var operationID = UUID()
    private var task: Task<Void, Never>?
    private var activeTool: ManagedTool?
    private var activeInvocationID: UUID?

    init(toolsDirectory: URL? = nil, outputDirectory initialOutputDirectory: URL? = nil) {
        tools = ToolLocations(directoryOverride: toolsDirectory)
        let defaults = UserDefaults.standard
        let legacyDefaults = UserDefaults(suiteName: "local.clipdock.videosaver")
        if let initialOutputDirectory {
            outputDirectory = initialOutputDirectory
        } else if let saved = defaults.string(forKey: "outputDirectory") ?? legacyDefaults?.string(forKey: "outputDirectory") {
            outputDirectory = URL(fileURLWithPath: saved, isDirectory: true)
        } else {
            outputDirectory = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Downloads/영상저장", isDirectory: true)
        }
        if !tools.ready {
            stage = "error"
            errorText = "필수 도구를 찾지 못했습니다: \(tools.missingNames)"
            statusText = "실행 도구를 확인해 주세요"
        }
    }

    var isBusy: Bool { ["inspecting", "downloading", "converting", "transcribing"].contains(stage) }
    var toolsReady: Bool { tools.ready }
    var canDownload: Bool {
        !isBusy && tools.ready && metadata != nil && inspectedURL != nil && (try? canonicalVideoURL(urlText)) == inspectedURL
    }

    func pasteLink() {
        if let value = NSPasteboard.general.string(forType: .string) { urlText = value.trimmingCharacters(in: .whitespacesAndNewlines) }
    }

    func chooseDirectory() {
        guard !isBusy else { return }
        let panel = NSOpenPanel()
        panel.title = "저장 폴더 선택"
        panel.prompt = "이 폴더에 저장"
        panel.canChooseFiles = false
        panel.canChooseDirectories = true
        panel.canCreateDirectories = true
        panel.allowsMultipleSelection = false
        panel.directoryURL = outputDirectory
        if panel.runModal() == .OK, let url = panel.url {
            outputDirectory = url
            UserDefaults.standard.set(url.path, forKey: "outputDirectory")
        }
    }

    func revealOutput() {
        guard let output = lastOutput, FileManager.default.fileExists(atPath: output.path) else { return }
        NSWorkspace.shared.activateFileViewerSelecting([output])
    }

    func openOutputDirectory() {
        do {
            try FileManager.default.createDirectory(at: outputDirectory, withIntermediateDirectories: true)
            NSWorkspace.shared.open(outputDirectory)
        } catch { errorText = "저장 폴더를 열 수 없습니다: \(error.localizedDescription)" }
    }

    func inspect() {
        guard !isBusy, tools.ready, let ytdlp = tools.ytdlp else { return }
        let url: URL
        do { url = try canonicalVideoURL(urlText) }
        catch { fail(error); return }
        let id = begin(stage: "inspecting", text: "영상 정보를 확인하고 있어요")
        metadata = nil
        captions = []
        selectedCaption = ""
        inspectedURL = nil
        lastOutput = nil
        task = Task<Void, Never> { [weak self] in
            guard let self else { return }
            do {
                let result = try await runTool(ytdlp, commonArguments() + ["--dump-single-json", "--skip-download", "--", url.absoluteString], id: id, metadataOnly: true)
                try check(result)
                guard operationID == id else { return }
                guard let json = try JSONSerialization.jsonObject(with: result.stdout) as? [String: Any] else {
                    throw DownloadFailure.message("영상 정보를 읽을 수 없습니다. 다른 링크로 다시 시도해 주세요.")
                }
                if json["is_live"] as? Bool == true || json["live_status"] as? String == "is_upcoming" {
                    throw DownloadFailure.message("실시간 방송이나 예정된 방송은 저장할 수 없습니다. 방송이 끝난 영상으로 시도해 주세요.")
                }
                postVideoCount = json["vitract_video_count"] as? Int ?? 1
                postVideoIndex = json["vitract_video_index"] as? Int ?? 1
                captions = captionTracks(json)
                selectedCaption = captions.first?.id ?? ""
                let formats = json["formats"] as? [[String: Any]] ?? []
                var heights = Array(Set(formats.compactMap { format -> Int? in
                    guard let codec = format["vcodec"] as? String, codec != "none", !codec.isEmpty,
                          format["has_drm"] as? Bool != true,
                          let height = format["height"] as? Int, height > 0 else { return nil }
                    return height
                })).sorted(by: >)
                if heights.isEmpty, !formats.isEmpty { heights = [0] }
                guard !heights.isEmpty else { throw DownloadFailure.message("저장할 단일 영상이 없습니다. 여러 영상이 포함된 게시물은 개별 영상 링크를 사용해 주세요.") }
                metadata = VideoMetadata(title: json["title"] as? String ?? "유튜브 영상",
                                         uploader: json["uploader"] as? String ?? json["channel"] as? String ?? "",
                                         duration: (json["duration"] as? NSNumber)?.doubleValue ?? 0,
                                         thumbnailURL: (json["thumbnail"] as? String).flatMap(URL.init(string:)),
                                         heights: heights)
                if !heights.contains(selectedHeight) { selectedHeight = heights.first(where: { $0 <= 1080 }) ?? heights.last! }
                inspectedURL = url
                stage = "ready"
                statusText = "저장할 준비가 되었어요"
                detailText = "화질과 저장 폴더를 확인한 뒤 MP4로 저장하세요."
                activeTool = nil
            } catch { finishFailure(error, id: id) }
        }
    }

    func selectPostVideo(_ index: Int) {
        guard !isBusy, let url = inspectedURL, var parts = URLComponents(url: url, resolvingAgainstBaseURL: false) else { return }
        parts.queryItems = [URLQueryItem(name: "vitract_video", value: String(index))]
        urlText = parts.url?.absoluteString ?? urlText
        inspect()
    }

    func download() {
        guard canDownload, let video = metadata, let url = inspectedURL,
              let ytdlp = tools.ytdlp, let ffmpeg = tools.ffmpeg, let ffprobe = tools.ffprobe else { return }
        let height = selectedHeight
        let directory = outputDirectory
        let id = begin(stage: "downloading", text: "영상을 다운로드하고 있어요")
        lastOutput = nil
        task = Task<Void, Never> { [weak self] in
            guard let self else { return }
            let manager = FileManager.default
            let scratch = directory.appendingPathComponent(".영상저장-\(UUID().uuidString)", isDirectory: true)
            defer { try? manager.removeItem(at: scratch) }
            do {
                try manager.createDirectory(at: directory, withIntermediateDirectories: true)
                try manager.createDirectory(at: scratch, withIntermediateDirectories: false)
                let filter = height > 0 ? "bv*[height<=?\(height)]+ba/b[height<=?\(height)]" : "bv*+ba/b"
                let arguments = commonArguments() + [
                    "--no-simulate", "--progress", "--newline", "--progress-delta", "0.3",
                    "--concurrent-fragments", "4", "-f", filter, "-S", "res,vcodec:h264,acodec:aac",
                    "--merge-output-format", "mkv", "-P", scratch.path, "-o", "source.%(ext)s",
                    "--progress-template", "download:__DOWNLOAD__%(progress.{status,downloaded_bytes,total_bytes,total_bytes_estimate,speed,eta})j",
                    "--progress-template", "postprocess:__POSTPROCESS__%(progress.{status,postprocessor})j",
                    "--print", "after_move:__FILE__%(filepath)j", "--", url.absoluteString
                ]
                let downloaded = try await runTool(ytdlp, arguments, id: id)
                try check(downloaded)
                try ensureCurrent(id)
                let lines = String(decoding: downloaded.stdout, as: UTF8.self).components(separatedBy: .newlines)
                guard let outputLine = lines.last(where: { $0.hasPrefix("__FILE__") }),
                      let encodedPath = outputLine.dropFirst(8).data(using: .utf8),
                      let path = try JSONSerialization.jsonObject(with: encodedPath, options: [.fragmentsAllowed]) as? String else {
                    throw DownloadFailure.message("다운로드된 영상 파일의 위치를 확인하지 못했습니다.")
                }
                let source = URL(fileURLWithPath: path).standardizedFileURL
                guard source.path.hasPrefix(scratch.standardizedFileURL.path + "/"), manager.fileExists(atPath: source.path) else {
                    throw DownloadFailure.message("다운로드된 영상 파일을 찾지 못했습니다.")
                }
                let sourceInfo = try await probe(source, executable: ffprobe, id: id)
                let videoCodec = sourceInfo.video["codec_name"] as? String ?? ""
                let pixelFormat = sourceInfo.video["pix_fmt"] as? String ?? ""
                let copyVideo = videoCodec == "h264" && pixelFormat == "yuv420p"
                let audioCodec = sourceInfo.audio?["codec_name"] as? String
                let copyAudio = sourceInfo.audio == nil || audioCodec == "aac"
                stage = "converting"
                statusText = copyVideo && copyAudio ? "MP4 파일을 마무리하고 있어요" : "재생하기 편한 MP4로 변환하고 있어요"
                detailText = copyVideo && copyAudio ? "원본 화질을 유지하며 영상과 소리를 정리합니다." : "H.264 영상과 AAC 소리로 변환합니다. 긴 영상은 시간이 걸릴 수 있어요."
                progress = 0.82
                let staged = scratch.appendingPathComponent("finished.mp4")
                var encodeArgs = ["-hide_banner", "-nostdin", "-loglevel", "error", "-stats_period", "0.3", "-progress", "pipe:1",
                                  "-i", source.path, "-map", "0:v:0", "-map", "0:a:0?", "-map_metadata", "0", "-map_chapters", "0"]
                if copyVideo {
                    encodeArgs += ["-c:v", "copy"]
                } else {
                    encodeArgs += ["-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p",
                                   "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2"]
                }
                encodeArgs += copyAudio ? ["-c:a", "copy"] : ["-c:a", "aac", "-b:a", "192k"]
                encodeArgs += ["-movflags", "+faststart", "-n", staged.path]
                let encoded = try await runTool(ffmpeg, encodeArgs, id: id, duration: sourceInfo.duration > 0 ? sourceInfo.duration : video.duration)
                try check(encoded)
                try ensureCurrent(id)
                let finalInfo = try await probe(staged, executable: ffprobe, id: id)
                let finalVideoCodec = finalInfo.video["codec_name"] as? String
                let finalAudioCodec = finalInfo.audio?["codec_name"] as? String
                let fileAttributes = try manager.attributesOfItem(atPath: staged.path)
                let fileSize = (fileAttributes[.size] as? NSNumber)?.int64Value ?? 0
                guard finalVideoCodec == "h264", finalInfo.audio == nil || finalAudioCodec == "aac", fileSize > 0 else {
                    throw DownloadFailure.message("완성된 MP4 파일 검증에 실패했습니다. 다시 시도해 주세요.")
                }
                try ensureCurrent(id)
                let videoID = URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems?.first(where: { $0.name == "v" })?.value ?? url.lastPathComponent
                let output = try publishMP4Exclusively(staged: staged, directory: directory, filename: safeVideoFilename(video.title, videoID: videoID))
                lastOutput = output
                progress = 1
                stage = "completed"
                statusText = "MP4 저장이 완료됐어요"
                detailText = output.lastPathComponent
                activeTool = nil
            } catch { finishFailure(error, id: id) }
        }
    }

    func extractTranscript() {
        guard canDownload, let video = metadata, let url = inspectedURL,
              let ytdlp = tools.ytdlp, let track = captions.first(where: { $0.id == selectedCaption }) else { return }
        let directory = outputDirectory
        let id = begin(stage: "transcribing", text: "영상 스크립트를 추출하고 있어요")
        lastOutput = nil
        task = Task<Void, Never> { [weak self] in
            guard let self else { return }
            let scratch = directory.appendingPathComponent(".스크립트-\(UUID().uuidString)", isDirectory: true)
            defer { try? FileManager.default.removeItem(at: scratch) }
            do {
                try FileManager.default.createDirectory(at: scratch, withIntermediateDirectories: true)
                let language = "^" + NSRegularExpression.escapedPattern(for: track.language) + "$"
                let args = commonArguments() + ["--skip-download", track.automatic ? "--write-auto-subs" : "--write-subs",
                    "--sub-langs", language, "--sub-format", "json3", "-o", scratch.appendingPathComponent("captions.%(ext)s").path,
                    "--", url.absoluteString]
                let result = try await runTool(ytdlp, args, id: id)
                try check(result)
                let files = try FileManager.default.contentsOfDirectory(at: scratch, includingPropertiesForKeys: nil)
                guard let caption = files.first(where: { $0.pathExtension == "json3" }) else {
                    throw DownloadFailure.message("이 영상의 자막을 가져오지 못했습니다. 자막 제공 상태를 확인해 주세요.")
                }
                let body = try transcriptText(Data(contentsOf: caption))
                let text = "\(video.title)\n\(url.absoluteString)\n\(track.label)\n\n" + body
                let staged = scratch.appendingPathComponent("script.txt")
                try text.write(to: staged, atomically: true, encoding: .utf8)
                try ensureCurrent(id)
                let videoID = URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems?.first(where: { $0.name == "v" })?.value ?? url.lastPathComponent
                let stem = (safeVideoFilename(video.title, videoID: videoID) as NSString).deletingPathExtension
                lastOutput = try publishMP4Exclusively(staged: staged, directory: directory, filename: stem + " 스크립트.txt")
                progress = 1
                stage = "completed"
                statusText = "스크립트 저장이 완료됐어요"
                detailText = lastOutput?.lastPathComponent ?? ""
            } catch { finishFailure(error, id: id) }
        }
    }

    func cancel() {
        guard isBusy else { return }
        operationID = UUID()
        activeTool?.cancel()
        task?.cancel()
        activeTool = nil
        activeInvocationID = nil
        stage = "cancelled"
        statusText = "작업을 취소했어요"
        detailText = "필요할 때 다시 저장할 수 있어요."
        progress = 0
        errorText = nil
    }

    private func begin(stage newStage: String, text: String) -> UUID {
        let id = UUID()
        operationID = id
        stage = newStage
        statusText = text
        detailText = "잠시만 기다려 주세요."
        progress = 0
        errorText = nil
        logText = ""
        return id
    }

    private func commonArguments() -> [String] {
        var args = ["--ignore-config", "--no-playlist", "--no-colors", "--no-cache-dir", "--abort-on-error",
                    "--socket-timeout", "25", "--retries", "3", "--fragment-retries", "3"]
        if let resource = Bundle.main.resourceURL {
            let plugins = resource.appendingPathComponent("plugins")
            if FileManager.default.fileExists(atPath: plugins.path) { args += ["--plugin-dirs", plugins.path] }
        }
        if let ffmpeg = tools.ffmpeg { args += ["--ffmpeg-location", ffmpeg.deletingLastPathComponent().path] }
        if let node = tools.node { args += ["--js-runtimes", "node:\(node.path)"] }
        return args
    }

    private func ensureCurrent(_ id: UUID) throws {
        try Task.checkCancellation()
        if id != operationID { throw CancellationError() }
    }

    private func runTool(_ executable: URL, _ arguments: [String], id: UUID,
                         metadataOnly: Bool = false, duration: Double = 0) async throws -> ToolResult {
        try ensureCurrent(id)
        let tool = ManagedTool()
        let invocationID = UUID()
        activeTool = tool
        activeInvocationID = invocationID
        let result = try await withTaskCancellationHandler {
            try await tool.run(executable: executable, arguments: arguments) { [weak self] stream, line in
                guard !line.isEmpty else { return }
                Task { @MainActor [weak self] in
                    guard let self, self.operationID == id, self.activeInvocationID == invocationID else { return }
                    if metadataOnly && stream == .stdout { return }
                    self.consume(line, duration: duration)
                }
            }
        } onCancel: { tool.cancel() }
        try ensureCurrent(id)
        activeTool = nil
        activeInvocationID = nil
        return result
    }

    private struct ProbeInfo {
        let video: [String: Any]
        let audio: [String: Any]?
        let duration: Double
    }

    private func probe(_ file: URL, executable: URL, id: UUID) async throws -> ProbeInfo {
        let result = try await runTool(executable, ["-v", "error", "-show_entries", "stream=codec_type,codec_name,pix_fmt,width,height:format=duration", "-of", "json", file.path], id: id, metadataOnly: true)
        try check(result)
        guard let json = try JSONSerialization.jsonObject(with: result.stdout) as? [String: Any],
              let streams = json["streams"] as? [[String: Any]],
              let video = streams.first(where: { $0["codec_type"] as? String == "video" }) else {
            throw DownloadFailure.message("영상 파일을 검사하지 못했습니다.")
        }
        let audio = streams.first(where: { $0["codec_type"] as? String == "audio" })
        let duration = Double((json["format"] as? [String: Any])?["duration"] as? String ?? "") ?? 0
        return ProbeInfo(video: video, audio: audio, duration: duration)
    }

    private func consume(_ line: String, duration: Double) {
        if line.hasPrefix("__DOWNLOAD__"),
           let data = line.dropFirst(12).data(using: .utf8),
           let info = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
            let downloaded = (info["downloaded_bytes"] as? NSNumber)?.doubleValue ?? 0
            let total = (info["total_bytes"] as? NSNumber)?.doubleValue ?? (info["total_bytes_estimate"] as? NSNumber)?.doubleValue ?? 0
            if total > 0 { progress = max(progress, min(0.79, downloaded / total * 0.78)) }
            let size = ByteCountFormatter.string(fromByteCount: Int64(downloaded), countStyle: .file)
            var text = "\(size) 다운로드"
            if let speed = (info["speed"] as? NSNumber)?.doubleValue, speed > 0 {
                text += " · \(ByteCountFormatter.string(fromByteCount: Int64(speed), countStyle: .file))/초"
            }
            if let seconds = (info["eta"] as? NSNumber)?.intValue, seconds > 0 { text += " · 약 \(seconds < 60 ? "\(seconds)초" : "\((seconds + 59) / 60)분") 남음" }
            detailText = text
            return
        }
        if line.hasPrefix("__POSTPROCESS__") {
            if stage == "downloading" { statusText = "영상과 소리를 합치고 있어요"; progress = max(progress, 0.8) }
            return
        }
        if line.hasPrefix("__FILE__") { return }
        if duration > 0, line.hasPrefix("out_time_us="), let micros = Double(line.dropFirst(12)) {
            progress = max(progress, min(0.98, 0.82 + (micros / 1_000_000 / duration) * 0.16))
            return
        }
        if duration > 0, line.contains("="), !line.hasPrefix("ERROR") { return }
        logText += line + "\n"
        if logText.count > 40_000 { logText = String(logText.suffix(40_000)) }
    }

    private func check(_ result: ToolResult) throws {
        guard result.status == 0 else {
            let detail = result.errorOutput.trimmingCharacters(in: .whitespacesAndNewlines)
            if !detail.isEmpty { logText = String((logText + "\n" + detail).suffix(40_000)) }
            let lower = detail.lowercased()
            let message: String
            if lower.contains("no public video found in threads embed") {
                message = "Threads 공개 응답에서 영상을 찾지 못했습니다. 게시물에 직접 첨부된 영상인지 확인해 주세요."
            } else if lower.contains("threads shared post has no public video") {
                message = "Threads에 연결된 원본 게시물에서 다운로드 가능한 영상을 찾지 못했습니다."
            } else if lower.contains("sign in") || lower.contains("private video") || lower.contains("members-only") || lower.contains("age-restricted") {
                message = "이 영상은 로그인이나 별도 접근 권한이 필요합니다. 공개된 다른 영상으로 시도해 주세요."
            } else if lower.contains("unavailable") || lower.contains("not available") || lower.contains("removed") {
                message = "현재 저장할 수 없는 영상입니다. 삭제 또는 공개 상태를 확인해 주세요."
            } else if lower.contains("timed out") || lower.contains("network") || lower.contains("resolve") || lower.contains("connection") {
                message = "네트워크 연결을 확인한 뒤 다시 시도해 주세요."
            } else if lower.contains("no space left") {
                message = "저장 공간이 부족합니다. 여유 공간을 확보하거나 다른 폴더를 선택해 주세요."
            } else if lower.contains("permission denied") || lower.contains("operation not permitted") {
                message = "저장 폴더에 접근할 수 없습니다. 쓸 수 있는 다른 폴더를 선택해 주세요."
            } else {
                message = "작업을 완료하지 못했습니다. 다시 시도하거나 오류 상세 내용을 확인해 주세요."
            }
            throw DownloadFailure.message(message)
        }
    }

    private func fail(_ error: Error) {
        stage = "error"
        statusText = "작업을 완료하지 못했어요"
        errorText = error.localizedDescription
        detailText = ""
    }

    private func finishFailure(_ error: Error, id: UUID) {
        guard operationID == id else { return }
        activeTool = nil
        activeInvocationID = nil
        if error is CancellationError {
            stage = "cancelled"
            statusText = "작업을 취소했어요"
            detailText = ""
        } else { fail(error) }
    }
}
