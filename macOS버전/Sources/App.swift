import SwiftUI
import AppKit

private let ink = Color(red: 0.13, green: 0.20, blue: 0.20)
private let accent = Color(red: 0.87, green: 0.10, blue: 0.14)
private let muted = Color(red: 0.40, green: 0.46, blue: 0.45)
private let canvas = Color(red: 0.96, green: 0.97, blue: 0.95)

@main
struct VideoSaverApp: App {
    @StateObject private var model = DownloadModel()
    var body: some Scene {
        WindowGroup("Vitract") {
            MainView(model: model)
                .frame(minWidth: 820, minHeight: 720)
                .preferredColorScheme(.light)
                .onReceive(NotificationCenter.default.publisher(for: NSApplication.willTerminateNotification)) { _ in model.cancel() }
        }
        .defaultSize(width: 940, height: 810)
        .windowStyle(.hiddenTitleBar)
        .commands {
            CommandGroup(replacing: .newItem) {}
            CommandMenu("영상") {
                Button("링크 붙여넣기") { model.pasteLink() }.keyboardShortcut("v", modifiers: [.command, .shift])
                Button("영상 확인") { model.inspect() }.keyboardShortcut(.return, modifiers: .command).disabled(model.isBusy)
                Button("저장 폴더 열기") { model.openOutputDirectory() }.keyboardShortcut("o", modifiers: [.command, .shift])
            }
        }
    }
}

struct MainView: View {
    @ObservedObject var model: DownloadModel
    @StateObject private var ui = OverlayState()
    @FocusState private var linkFocused: Bool

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 24) {
                header
                HStack(alignment: .center) {
                    VStack(alignment: .leading, spacing: 10) {
                        Text("링크 하나로, 내 Mac에.")
                            .font(.system(size: 12, weight: .semibold)).tracking(1.1).foregroundStyle(accent)
                        Text("유튜브 영상을\n영상과 스크립트로 저장하세요.")
                            .font(.system(size: 34, weight: .bold)).tracking(-1.1).lineSpacing(3).foregroundStyle(ink)
                        Text("영상 주소를 넣고, 화질을 고르면 준비 끝.")
                            .font(.system(size: 14)).foregroundStyle(muted)
                    }
                    Spacer()
                    Image(systemName: "arrow.down.document.fill")
                        .font(.system(size: 74, weight: .regular))
                        .foregroundStyle(accent.opacity(0.16))
                        .rotationEffect(.degrees(8))
                        .padding(.trailing, 22)
                }
                linkCard
                if let video = model.metadata {
                    videoCard(video)
                } else {
                    emptyCard
                }
                if model.isBusy || model.stage == "completed" || model.stage == "cancelled" || model.errorText != nil {
                    statusCard
                }
                footer
            }
            .padding(.horizontal, 38)
            .padding(.top, 35)
            .padding(.bottom, 25)
            .frame(maxWidth: 1080)
            .frame(maxWidth: .infinity)
        }
        .background(canvas)
        .tint(accent)
        .onAppear { linkFocused = true }
        .sheet(isPresented: $ui.showLog) {
            VStack(alignment: .leading, spacing: 16) {
                HStack {
                    Text("작업 기록").font(.title2.bold())
                    Spacer()
                    Button("닫기") { ui.showLog = false }.keyboardShortcut(.escape, modifiers: [])
                }
                ScrollView {
                    Text(model.logText.isEmpty ? "아직 작업 기록이 없습니다." : model.logText)
                        .font(.system(size: 11, design: .monospaced))
                        .textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading)
                }.padding(12).background(canvas).clipShape(RoundedRectangle(cornerRadius: 10))
            }.padding(24).frame(width: 720, height: 450)
        }
    }

    private var header: some View {
        HStack(spacing: 10) {
            ZStack {
                RoundedRectangle(cornerRadius: 11).fill(accent).frame(width: 39, height: 39)
                Image(systemName: "play.fill").font(.system(size: 18, weight: .bold)).foregroundStyle(.white).offset(x: 1)
            }
            Text("Vitract").font(.system(size: 20, weight: .bold)).foregroundStyle(ink)
            Text("FOR MAC").font(.system(size: 9, weight: .bold)).tracking(1.4).foregroundStyle(muted).padding(.leading, 5)
            Spacer()
            Label("MP4 · 영상 + 소리", systemImage: "checkmark.circle.fill")
                .font(.system(size: 11, weight: .medium)).foregroundStyle(accent)
                .padding(.horizontal, 12).padding(.vertical, 8)
                .background(accent.opacity(0.07), in: Capsule())
        }
    }

    private var linkCard: some View {
        VStack(alignment: .leading, spacing: 13) {
            HStack(spacing: 8) {
                step("01")
                Text("영상 링크").font(.system(size: 14, weight: .semibold))
                Spacer()
                Text("일반 영상 · Shorts").font(.system(size: 11)).foregroundStyle(muted)
            }
            HStack(spacing: 10) {
                HStack(spacing: 9) {
                    Image(systemName: "link").foregroundStyle(muted)
                    TextField("https://www.youtube.com/watch?v=…", text: $model.urlText)
                        .textFieldStyle(.plain).font(.system(size: 13))
                        .focused($linkFocused).onSubmit { if !model.isBusy { model.inspect() } }
                        .accessibilityLabel("유튜브 영상 주소")
                        .disabled(model.isBusy)
                    Button { model.pasteLink() } label: { Image(systemName: "doc.on.clipboard") }
                        .buttonStyle(.plain).foregroundStyle(accent).help("클립보드에서 붙여넣기")
                        .accessibilityLabel("링크 붙여넣기").disabled(model.isBusy)
                }
                .padding(14).background(canvas, in: RoundedRectangle(cornerRadius: 10))
                .overlay(RoundedRectangle(cornerRadius: 10).strokeBorder(Color.black.opacity(0.07)))
                Button { model.inspect() } label: {
                    HStack(spacing: 7) {
                        if model.stage == "inspecting" { ProgressView().controlSize(.small).tint(.white) }
                        Text(model.stage == "inspecting" ? "확인 중" : "영상 확인")
                        if model.stage != "inspecting" { Image(systemName: "arrow.right") }
                    }.font(.system(size: 13, weight: .semibold)).frame(minWidth: 100).padding(.vertical, 14).padding(.horizontal, 8)
                }.buttonStyle(FilledButton()).disabled(model.isBusy || model.urlText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            }
        }.padding(20).background(.white, in: RoundedRectangle(cornerRadius: 18))
    }

    private func videoCard(_ video: VideoMetadata) -> some View {
        VStack(alignment: .leading, spacing: 18) {
            HStack(alignment: .top, spacing: 18) {
                ZStack(alignment: .bottomTrailing) {
                    AsyncImage(url: video.thumbnailURL) { image in image.resizable().scaledToFill() } placeholder: {
                        ZStack { accent.opacity(0.08); Image(systemName: "play.rectangle").font(.system(size: 34)).foregroundStyle(accent) }
                    }
                    .frame(width: 194, height: 110).clipped().clipShape(RoundedRectangle(cornerRadius: 10))
                    Text(duration(video.duration)).font(.system(size: 10, weight: .semibold, design: .monospaced))
                        .padding(.horizontal, 6).padding(.vertical, 3).background(.black.opacity(0.75), in: RoundedRectangle(cornerRadius: 4))
                        .foregroundStyle(.white).padding(7)
                }
                VStack(alignment: .leading, spacing: 8) {
                    Text("저장할 영상").font(.system(size: 10, weight: .semibold)).foregroundStyle(accent)
                    Text(video.title).font(.system(size: 16, weight: .semibold)).lineLimit(3).foregroundStyle(ink).textSelection(.enabled)
                    Text(video.uploader).font(.system(size: 12)).foregroundStyle(muted)
                }.padding(.top, 2)
                Spacer(minLength: 0)
            }
            VStack(alignment: .leading, spacing: 10) {
                HStack {
                    Text("스크립트 추출").font(.system(size: 13, weight: .semibold))
                    Spacer()
                    if !model.captions.isEmpty {
                        Picker("자막 언어", selection: $model.selectedCaption) {
                            ForEach(model.captions) { track in Text(track.label).tag(track.id) }
                        }.frame(width: 230).disabled(model.isBusy)
                        Button("스크립트 저장 (TXT)") { model.extractTranscript() }
                            .disabled(!model.canDownload).buttonStyle(.borderedProminent)
                    }
                }
                Text(model.captions.isEmpty ? "제공되는 자막이 없어 스크립트를 추출할 수 없습니다." : "영상 다운로드 없이 자막을 텍스트로 저장합니다. 자동 자막에는 오인식이 있을 수 있습니다.")
                    .font(.system(size: 11)).foregroundStyle(muted)
            }
            Divider().overlay(canvas)
            HStack(alignment: .top, spacing: 26) {
                VStack(alignment: .leading, spacing: 10) {
                    HStack(spacing: 8) { step("02"); Text("화질 선택").font(.system(size: 13, weight: .semibold)) }
                    Picker("화질", selection: $model.selectedHeight) {
                        ForEach(video.heights, id: \.self) { height in Text(qualityLabel(height)).tag(height) }
                    }.labelsHidden().frame(width: 165).disabled(model.isBusy)
                    Text("원본에서 제공하는 화질").font(.system(size: 10)).foregroundStyle(muted)
                }
                VStack(alignment: .leading, spacing: 10) {
                    HStack(spacing: 8) { step("03"); Text("저장 위치").font(.system(size: 13, weight: .semibold)) }
                    HStack(spacing: 8) {
                        Image(systemName: "folder").foregroundStyle(accent)
                        Text(model.outputDirectory.lastPathComponent).font(.system(size: 12)).lineLimit(1).truncationMode(.middle)
                        Button("변경") { model.chooseDirectory() }.buttonStyle(.link).font(.system(size: 11)).disabled(model.isBusy)
                    }.frame(height: 24)
                    Text(model.outputDirectory.path).font(.system(size: 10)).foregroundStyle(muted).lineLimit(1).truncationMode(.middle).help(model.outputDirectory.path)
                }.frame(maxWidth: .infinity, alignment: .leading)
                Button { model.download() } label: {
                    Label("MP4 저장", systemImage: "arrow.down.to.line").font(.system(size: 14, weight: .semibold))
                        .padding(.horizontal, 22).padding(.vertical, 17)
                }.buttonStyle(FilledButton()).disabled(!model.canDownload).padding(.top, 17)
            }
        }.padding(22).background(.white, in: RoundedRectangle(cornerRadius: 18))
    }

    private var emptyCard: some View {
        VStack(spacing: 19) {
            ZStack {
                Circle().fill(accent.opacity(0.06)).frame(width: 57, height: 57)
                Image(systemName: "play.rectangle.on.rectangle").font(.system(size: 24)).foregroundStyle(accent.opacity(0.75))
            }
            VStack(spacing: 7) {
                Text("저장할 영상이 여기에 표시됩니다").font(.system(size: 14, weight: .semibold)).foregroundStyle(ink)
                Text("위에 유튜브 링크를 붙여넣고 ‘영상 확인’을 눌러주세요.")
                    .font(.system(size: 12)).foregroundStyle(muted)
            }
            HStack(spacing: 25) {
                Label("화질 선택", systemImage: "slider.horizontal.3")
                Label("소리까지 함께", systemImage: "speaker.wave.2")
                Label("내 폴더에 저장", systemImage: "folder")
            }.font(.system(size: 11)).foregroundStyle(muted)
        }
        .frame(maxWidth: .infinity).padding(.vertical, 32)
        .background(.white.opacity(0.6), in: RoundedRectangle(cornerRadius: 18))
        .overlay(RoundedRectangle(cornerRadius: 18).strokeBorder(accent.opacity(0.10), style: StrokeStyle(lineWidth: 1, dash: [5, 5])))
    }

    private var statusCard: some View {
        VStack(alignment: .leading, spacing: 11) {
            HStack(spacing: 10) {
                Image(systemName: model.errorText != nil ? "exclamationmark.circle.fill" : (model.stage == "completed" ? "checkmark.circle.fill" : "arrow.down.circle"))
                    .foregroundStyle(model.errorText != nil ? Color.orange : accent)
                Text(model.statusText).font(.system(size: 13, weight: .semibold))
                Spacer()
                if model.isBusy { Button("취소") { model.cancel() }.buttonStyle(.bordered).controlSize(.small) }
                if model.stage == "completed" { Button("Finder에서 보기") { model.revealOutput() }.buttonStyle(.borderedProminent).controlSize(.small) }
            }
            if model.isBusy {
                if model.stage == "inspecting" || model.stage == "transcribing" { ProgressView().progressViewStyle(.linear) }
                else { ProgressView(value: model.progress).progressViewStyle(.linear) }
            }
            if let error = model.errorText {
                Text(error).font(.system(size: 12)).foregroundStyle(muted).textSelection(.enabled)
            } else if !model.detailText.isEmpty {
                Text(model.detailText).font(.system(size: 11)).foregroundStyle(muted).textSelection(.enabled)
            }
        }.padding(17).background(accent.opacity(0.055), in: RoundedRectangle(cornerRadius: 12))
    }

    private var footer: some View {
        HStack(alignment: .top) {
            VStack(alignment: .leading, spacing: 5) {
                Text("본인 소유 또는 다운로드 허가를 받은 영상에 사용하세요.")
                Text("비공개·유료·라이브 영상은 지원하지 않습니다.").foregroundStyle(muted.opacity(0.8))
            }.font(.system(size: 10)).foregroundStyle(muted)
            Spacer()
            Button("저장 폴더") { model.openOutputDirectory() }.buttonStyle(.link)
            Text("·").foregroundStyle(muted)
            Button("작업 기록") { ui.showLog = true }.buttonStyle(.link)
        }.font(.system(size: 11))
    }

    private func step(_ value: String) -> some View {
        Text(value).font(.system(size: 9, weight: .bold, design: .monospaced)).foregroundStyle(accent)
            .frame(width: 23, height: 23).background(accent.opacity(0.08), in: RoundedRectangle(cornerRadius: 6))
    }
    private func duration(_ seconds: Double) -> String {
        guard seconds.isFinite && seconds > 0 else { return "영상" }
        let total = Int(seconds)
        return total >= 3600 ? String(format: "%d:%02d:%02d", total / 3600, (total / 60) % 60, total % 60) : String(format: "%d:%02d", total / 60, total % 60)
    }
    private func qualityLabel(_ height: Int) -> String {
        switch height { case 2160: return "2160p · 4K"; case 1440: return "1440p · QHD"; case 1080: return "1080p · Full HD"; case 720: return "720p · HD"; default: return "\(height)p" }
    }
}

private struct FilledButton: ButtonStyle {
    @Environment(\.isEnabled) private var isEnabled
    func makeBody(configuration: Configuration) -> some View {
        configuration.label.foregroundStyle(.white)
            .background(isEnabled ? accent.opacity(configuration.isPressed ? 0.80 : 1) : accent.opacity(0.35), in: RoundedRectangle(cornerRadius: 10))
            .contentShape(RoundedRectangle(cornerRadius: 10))
    }
}

private final class OverlayState: ObservableObject {
    @Published var showLog = false
}
