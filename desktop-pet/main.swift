// 桌宠：秋招看板「小招」和求职助手「小航」共用这一份代码，由 Info.plist 的 PetKind 区分。
// 单击：服务没开就启动并打开页面，开着就直接打开；悬停：看今天的情况；拖动：挪位置；右键：菜单。

import AppKit
import SwiftUI

// MARK: - 配置

enum PetKind: String {
    case kanban, jobhunter
}

struct PetConfig {
    let kind: PetKind
    let petName: String
    let appTitle: String
    let port: Int
    let healthPath: String
    let workDir: String
    let executable: String
    let arguments: [String]
    /// 默认位置：离屏幕右边多远，两只桌宠并排不重叠
    let defaultRightInset: CGFloat

    var baseURL: URL { URL(string: "http://127.0.0.1:\(port)/")! }
    var logURL: URL {
        FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Logs/求职桌宠/\(kind.rawValue).log")
    }

    static func load(kind override: String? = nil) -> PetConfig {
        let raw = override
            ?? Bundle.main.object(forInfoDictionaryKey: "PetKind") as? String
            ?? ProcessInfo.processInfo.environment["PET_KIND"]
            ?? "kanban"
        let home = NSHomeDirectory()
        switch PetKind(rawValue: raw) ?? .kanban {
        case .kanban:
            let dir = home + "/qiuzhao-kanban"
            return PetConfig(kind: .kanban, petName: "小招", appTitle: "秋招看板", port: 8000,
                             healthPath: "api/status", workDir: dir, executable: dir + "/venv/bin/uvicorn",
                             arguments: ["main:app", "--host", "127.0.0.1", "--port", "8000", "--reload"],
                             defaultRightInset: 30)
        case .jobhunter:
            let dir = home + "/Desktop/job-hunter"
            return PetConfig(kind: .jobhunter, petName: "小航", appTitle: "求职助手", port: 8001,
                             healthPath: "api/stats", workDir: dir + "/backend", executable: dir + "/.venv/bin/uvicorn",
                             arguments: ["main:app", "--host", "127.0.0.1", "--port", "8001", "--reload",
                                         "--reload-dir", dir + "/backend"],
                             defaultRightInset: 160)
        }
    }
}

struct PetError: LocalizedError {
    let message: String
    var errorDescription: String? { message }
}

// MARK: - 后端服务

final class ServerController {
    let config: PetConfig
    private(set) var process: Process?
    private var stopping = false
    var onUnexpectedExit: (() -> Void)?

    init(config: PetConfig) { self.config = config }

    var ownsProcess: Bool { process?.isRunning == true }

    func start() throws {
        if ownsProcess { return }
        let fm = FileManager.default
        guard fm.isExecutableFile(atPath: config.executable) else {
            throw PetError(message: "找不到 \(config.executable)，项目是不是换位置了？")
        }
        try fm.createDirectory(at: config.logURL.deletingLastPathComponent(), withIntermediateDirectories: true)
        if !fm.fileExists(atPath: config.logURL.path) {
            fm.createFile(atPath: config.logURL.path, contents: nil)
        }
        let log = try FileHandle(forWritingTo: config.logURL)
        if (try? log.seekToEnd()) ?? 0 > 2_000_000 {
            try log.truncate(atOffset: 0)
        }
        log.write("\n===== \(Date()) 由\(config.petName)启动 =====\n".data(using: .utf8)!)

        let p = Process()
        p.executableURL = URL(fileURLWithPath: config.executable)
        p.arguments = config.arguments
        p.currentDirectoryURL = URL(fileURLWithPath: config.workDir)
        var env = ProcessInfo.processInfo.environment
        // 从访达启动的 App 拿不到终端里的 PATH
        env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONUTF8"] = "1"
        env["LANG"] = env["LANG"] ?? "zh_CN.UTF-8"
        p.environment = env
        p.standardOutput = log
        p.standardError = log
        p.terminationHandler = { [weak self] _ in
            DispatchQueue.main.async {
                guard let self else { return }
                let expected = self.stopping
                self.stopping = false
                self.process = nil
                if !expected { self.onUnexpectedExit?() }
            }
        }
        try p.run()
        process = p
    }

    /// 停止服务：自己启动的直接结束；终端里启动的，按端口找到同一个项目的 uvicorn 再结束
    func stop(completion: @escaping (Bool) -> Void) {
        if let p = process, p.isRunning {
            stopping = true
            p.terminate()
            waitUntilDown(completion: completion)
            return
        }
        DispatchQueue.global().async {
            let pids = Self.run("/usr/sbin/lsof", ["-t", "-iTCP:\(self.config.port)", "-sTCP:LISTEN"])
                .split(separator: "\n").compactMap { Int32($0.trimmingCharacters(in: .whitespaces)) }
            var killed = false
            for pid in pids {
                let command = Self.run("/bin/ps", ["-o", "command=", "-p", String(pid)])
                if command.contains(self.config.executable) {
                    kill(pid, SIGTERM)
                    killed = true
                }
            }
            DispatchQueue.main.async {
                if killed { self.waitUntilDown(completion: completion) } else { completion(false) }
            }
        }
    }

    /// 退出桌宠时同步结束自己启动的服务
    func terminateOwnedProcess() {
        guard let p = process, p.isRunning else { return }
        stopping = true
        p.terminate()
        let deadline = Date().addingTimeInterval(5)
        while p.isRunning && Date() < deadline { usleep(100_000) }
        if p.isRunning { kill(p.processIdentifier, SIGKILL) }
    }

    private func waitUntilDown(completion: @escaping (Bool) -> Void, attempt: Int = 0) {
        checkHealth { alive in
            if !alive {
                completion(true)
            } else if attempt > 40 {
                if let p = self.process, p.isRunning { kill(p.processIdentifier, SIGKILL) }
                completion(false)
            } else {
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.25) {
                    self.waitUntilDown(completion: completion, attempt: attempt + 1)
                }
            }
        }
    }

    func checkHealth(completion: @escaping (Bool) -> Void) {
        fetchJSON(config.healthPath) { completion($0 != nil) }
    }

    func fetchJSON(_ path: String, completion: @escaping (Any?) -> Void) {
        guard let url = URL(string: path, relativeTo: config.baseURL) else { return completion(nil) }
        var request = URLRequest(url: url, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: 3)
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        URLSession.shared.dataTask(with: request) { data, response, _ in
            let ok = (response as? HTTPURLResponse)?.statusCode == 200
            let object = ok ? data.flatMap { try? JSONSerialization.jsonObject(with: $0) } : nil
            DispatchQueue.main.async { completion(object) }
        }.resume()
    }

    private static func run(_ path: String, _ args: [String]) -> String {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: path)
        p.arguments = args
        let pipe = Pipe()
        p.standardOutput = pipe
        p.standardError = FileHandle.nullDevice
        guard (try? p.run()) != nil else { return "" }
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        p.waitUntilExit()
        return String(data: data, encoding: .utf8) ?? ""
    }
}

// MARK: - 形象

enum PetState {
    case sleeping, starting, awake
}

final class PetModel: ObservableObject {
    @Published var state: PetState = .sleeping
    @Published var blinking = false
    @Published var squash = false
    @Published var badge = 0
}

struct Palette {
    let top: Color
    let bottom: Color
    let line: Color
    let accent: Color

    static func of(_ kind: PetKind) -> Palette {
        switch kind {
        case .kanban:
            return Palette(top: Color(red: 0.99, green: 0.86, blue: 0.62), bottom: Color(red: 0.93, green: 0.62, blue: 0.40),
                           line: Color(red: 0.26, green: 0.17, blue: 0.12), accent: Color(red: 0.80, green: 0.33, blue: 0.22))
        case .jobhunter:
            return Palette(top: Color(red: 0.70, green: 0.86, blue: 1.00), bottom: Color(red: 0.36, green: 0.62, blue: 0.93),
                           line: Color(red: 0.12, green: 0.22, blue: 0.38), accent: Color(red: 0.92, green: 0.30, blue: 0.30))
        }
    }
}

struct BlobShape: Shape {
    func path(in r: CGRect) -> Path {
        let w = r.width, h = r.height, x = r.minX, y = r.minY
        var p = Path()
        p.move(to: CGPoint(x: x + w * 0.5, y: y))
        p.addCurve(to: CGPoint(x: x + w, y: y + h * 0.7),
                   control1: CGPoint(x: x + w * 0.88, y: y), control2: CGPoint(x: x + w, y: y + h * 0.32))
        p.addCurve(to: CGPoint(x: x + w * 0.5, y: y + h),
                   control1: CGPoint(x: x + w, y: y + h * 0.96), control2: CGPoint(x: x + w * 0.82, y: y + h))
        p.addCurve(to: CGPoint(x: x, y: y + h * 0.7),
                   control1: CGPoint(x: x + w * 0.18, y: y + h), control2: CGPoint(x: x, y: y + h * 0.96))
        p.addCurve(to: CGPoint(x: x + w * 0.5, y: y),
                   control1: CGPoint(x: x, y: y + h * 0.32), control2: CGPoint(x: x + w * 0.12, y: y))
        p.closeSubpath()
        return p
    }
}

struct LeafShape: Shape {
    func path(in r: CGRect) -> Path {
        var p = Path()
        p.move(to: CGPoint(x: r.midX, y: r.minY))
        p.addQuadCurve(to: CGPoint(x: r.midX, y: r.maxY), control: CGPoint(x: r.maxX + r.width * 0.3, y: r.midY))
        p.addQuadCurve(to: CGPoint(x: r.midX, y: r.minY), control: CGPoint(x: r.minX - r.width * 0.3, y: r.midY))
        p.closeSubpath()
        return p
    }
}

/// 向下弯的弧线：笑嘴、闭着的眼睛
struct SmileShape: Shape {
    func path(in r: CGRect) -> Path {
        var p = Path()
        p.move(to: CGPoint(x: r.minX, y: r.minY))
        p.addQuadCurve(to: CGPoint(x: r.maxX, y: r.minY), control: CGPoint(x: r.midX, y: r.maxY * 1.6 - r.minY * 0.6))
        return p
    }
}

struct TriangleShape: Shape {
    var pointsUp = false
    func path(in r: CGRect) -> Path {
        var p = Path()
        if pointsUp {
            p.move(to: CGPoint(x: r.minX, y: r.maxY))
            p.addLine(to: CGPoint(x: r.midX, y: r.minY))
            p.addLine(to: CGPoint(x: r.maxX, y: r.maxY))
        } else {
            p.move(to: CGPoint(x: r.minX, y: r.minY))
            p.addLine(to: CGPoint(x: r.midX, y: r.maxY))
            p.addLine(to: CGPoint(x: r.maxX, y: r.minY))
        }
        p.closeSubpath()
        return p
    }
}

struct PetView: View {
    let kind: PetKind
    @ObservedObject var model: PetModel
    var animated = true

    @State private var bob = false
    @State private var floatZ = false
    @State private var wiggle = false

    private var palette: Palette { Palette.of(kind) }
    private var sleeping: Bool { model.state == .sleeping }

    var body: some View {
        ZStack {
            Ellipse()
                .fill(Color.black.opacity(0.16))
                .frame(width: sleeping ? 70 : (bob ? 56 : 66), height: 9)
                .offset(y: 50)

            ZStack {
                accessory
                BlobShape()
                    .fill(LinearGradient(colors: [palette.top, palette.bottom], startPoint: .top, endPoint: .bottom))
                    .overlay(BlobShape().stroke(palette.line, lineWidth: 2.6))
                    .overlay(
                        Ellipse().fill(Color.white.opacity(0.45))
                            .frame(width: 18, height: 10).rotationEffect(.degrees(-25))
                            .offset(x: -22, y: -20)
                    )
                    .frame(width: 86, height: 74)
                    .offset(y: 10)
                face.offset(y: 10)
            }
            .scaleEffect(x: model.squash ? 1.14 : (sleeping ? 1.04 : 1), y: model.squash ? 0.86 : (sleeping ? 0.95 : 1), anchor: .bottom)
            .offset(y: sleeping ? 2 : (bob ? -5 : 0))

            if sleeping {
                Text("z Z")
                    .font(.system(size: 13, weight: .heavy, design: .rounded))
                    .foregroundColor(palette.line.opacity(floatZ ? 0.15 : 0.75))
                    .offset(x: 34, y: floatZ ? -46 : -30)
            }
            if model.badge > 0 && !sleeping {
                Text("\(model.badge)")
                    .font(.system(size: 11, weight: .bold, design: .rounded))
                    .foregroundColor(.white)
                    .frame(minWidth: 19, minHeight: 19)
                    .background(Circle().fill(Color(red: 0.90, green: 0.22, blue: 0.21)))
                    .overlay(Circle().stroke(Color.white, lineWidth: 1.5))
                    .offset(x: 38, y: -18)
            }
        }
        .frame(width: 120, height: 120)
        .onAppear {
            guard animated else { return }
            withAnimation(.easeInOut(duration: 1.3).repeatForever(autoreverses: true)) { bob = true }
            withAnimation(.easeOut(duration: 2.2).repeatForever(autoreverses: false)) { floatZ = true }
            withAnimation(.easeInOut(duration: 2.6).repeatForever(autoreverses: true)) { wiggle = true }
        }
    }

    @ViewBuilder private var accessory: some View {
        switch kind {
        case .kanban:
            // 头顶一片秋叶
            ZStack {
                Rectangle().fill(palette.line).frame(width: 2.2, height: 9).offset(y: -29)
                LeafShape()
                    .fill(LinearGradient(colors: [Color(red: 0.95, green: 0.55, blue: 0.25), palette.accent],
                                         startPoint: .top, endPoint: .bottom))
                    .overlay(LeafShape().stroke(palette.line, lineWidth: 1.8))
                    .overlay(Rectangle().fill(palette.line.opacity(0.45)).frame(width: 1, height: 13))
                    .frame(width: 14, height: 24)
                    .rotationEffect(.degrees(wiggle ? 36 : 22), anchor: .bottom)
                    .offset(y: -45)
            }
        case .jobhunter:
            // 头顶一个小指南针
            ZStack {
                Rectangle().fill(palette.line).frame(width: 2.4, height: 12).offset(y: -32)
                Circle().fill(Color.white).frame(width: 20, height: 20)
                    .overlay(Circle().stroke(palette.line, lineWidth: 2))
                    .offset(y: -45)
                VStack(spacing: 0) {
                    TriangleShape(pointsUp: true).fill(palette.accent).frame(width: 6, height: 7)
                    TriangleShape().fill(palette.line).frame(width: 6, height: 7)
                }
                .rotationEffect(.degrees(wiggle ? 30 : -25))
                .offset(y: -45)
            }
        }
    }

    private var face: some View {
        ZStack {
            ForEach([-15.0, 15.0], id: \.self) { dx in
                eye.offset(x: dx, y: -3)
            }
            ForEach([-26.0, 26.0], id: \.self) { dx in
                Ellipse().fill(Color(red: 1, green: 0.45, blue: 0.5).opacity(0.5))
                    .frame(width: 13, height: 7)
                    .offset(x: dx, y: 8)
            }
            mouth.offset(y: 11)
        }
    }

    @ViewBuilder private var eye: some View {
        if sleeping {
            SmileShape().stroke(palette.line, style: StrokeStyle(lineWidth: 2.4, lineCap: .round))
                .frame(width: 11, height: 4)
        } else {
            Capsule().fill(palette.line)
                .frame(width: 7.5, height: model.blinking ? 1.6 : 11)
                .overlay(
                    Circle().fill(Color.white).frame(width: 2.8, height: 2.8)
                        .offset(x: 1.2, y: -2.2)
                        .opacity(model.blinking ? 0 : 1)
                )
        }
    }

    @ViewBuilder private var mouth: some View {
        switch model.state {
        case .sleeping:
            Capsule().fill(palette.line.opacity(0.8)).frame(width: 6, height: 2)
        case .starting:
            Circle().stroke(palette.line, lineWidth: 2).frame(width: 7, height: 7)
        case .awake:
            if model.badge > 0 {
                Ellipse().fill(palette.line).frame(width: 9, height: 7)
            } else {
                SmileShape().stroke(palette.line, style: StrokeStyle(lineWidth: 2.2, lineCap: .round))
                    .frame(width: 11, height: 4)
            }
        }
    }
}

// MARK: - 对话气泡

let bubbleFont = NSFont.systemFont(ofSize: 12.5, weight: .medium)

struct BubbleView: View {
    let text: String
    let textWidth: CGFloat
    let line: Color
    let tailOnTop: Bool

    var body: some View {
        VStack(spacing: -1.5) {
            if tailOnTop { tail(up: true) }
            Text(text)
                .font(Font(bubbleFont))
                .foregroundColor(Color(white: 0.15))
                .lineSpacing(2)
                .frame(width: textWidth, alignment: .leading)
                .fixedSize(horizontal: false, vertical: true)
                .padding(.horizontal, 12)
                .padding(.vertical, 8)
                .background(RoundedRectangle(cornerRadius: 12).fill(Color.white))
                .overlay(RoundedRectangle(cornerRadius: 12).stroke(line, lineWidth: 1.6))
            if !tailOnTop { tail(up: false) }
        }
        .padding(6)
        .fixedSize()
    }

    private func tail(up: Bool) -> some View {
        ZStack {
            TriangleShape(pointsUp: up).fill(line).frame(width: 15, height: 9)
            TriangleShape(pointsUp: up).fill(Color.white).frame(width: 10, height: 6)
                .offset(y: up ? 2 : -2)
        }
        .zIndex(1)
    }

    static func measure(_ text: String) -> CGFloat {
        let rect = (text as NSString).boundingRect(
            with: NSSize(width: 220, height: 2000),
            options: [.usesLineFragmentOrigin, .usesFontLeading],
            attributes: [.font: bubbleFont]
        )
        return ceil(rect.width) + 4
    }
}

// MARK: - 鼠标交互

protocol PetInteraction: AnyObject {
    func petClicked()
    func petDragEnded()
    func petHover(_ inside: Bool)
    func petMenu(_ event: NSEvent, in view: NSView)
}

/// 盖在形象上的透明层：统一处理点击、拖动、悬停和右键
final class PetContainerView: NSView {
    weak var delegate: PetInteraction?
    private var downMouse = NSPoint.zero
    private var downOrigin = NSPoint.zero
    private var dragging = false
    private var tracking: NSTrackingArea?

    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }
    override func hitTest(_ point: NSPoint) -> NSView? { frame.contains(point) ? self : nil }

    override func updateTrackingAreas() {
        super.updateTrackingAreas()
        if let tracking { removeTrackingArea(tracking) }
        let area = NSTrackingArea(rect: bounds, options: [.mouseEnteredAndExited, .activeAlways, .inVisibleRect],
                                  owner: self, userInfo: nil)
        addTrackingArea(area)
        tracking = area
    }

    override func mouseDown(with event: NSEvent) {
        if event.modifierFlags.contains(.control) {
            delegate?.petMenu(event, in: self)
            return
        }
        downMouse = NSEvent.mouseLocation
        downOrigin = window?.frame.origin ?? .zero
        dragging = false
    }

    override func mouseDragged(with event: NSEvent) {
        let p = NSEvent.mouseLocation
        let dx = p.x - downMouse.x, dy = p.y - downMouse.y
        if !dragging && hypot(dx, dy) < 4 { return }
        dragging = true
        window?.setFrameOrigin(NSPoint(x: downOrigin.x + dx, y: downOrigin.y + dy))
    }

    override func mouseUp(with event: NSEvent) {
        if event.modifierFlags.contains(.control) { return }
        if dragging { delegate?.petDragEnded() } else { delegate?.petClicked() }
        dragging = false
    }

    override func rightMouseDown(with event: NSEvent) { delegate?.petMenu(event, in: self) }
    override func mouseEntered(with event: NSEvent) { delegate?.petHover(true) }
    override func mouseExited(with event: NSEvent) { delegate?.petHover(false) }
}

// MARK: - 主控

final class AppDelegate: NSObject, NSApplicationDelegate, PetInteraction {
    let config: PetConfig
    let model = PetModel()
    let server: ServerController

    private var petWindow: NSPanel!
    private var bubbleWindow: NSPanel!
    private var bubbleHost: NSHostingView<BubbleView>!
    private var statusText = ""
    private var hovering = false
    private var messageUntil = Date.distantPast
    private var hideWork: DispatchWorkItem?
    private var lastUrgent = -1
    private var openWhenReady = false
    private var timers: [Timer] = []
    private var sigtermSource: DispatchSourceSignal?
    /// 构建脚本重新拉起桌宠时带 --quiet：不自动打开网页
    private let quietLaunch = CommandLine.arguments.contains("--quiet")

    private let defaults = UserDefaults.standard
    private var pinned: Bool {
        get { defaults.object(forKey: "pinned") as? Bool ?? true }
        set { defaults.set(newValue, forKey: "pinned") }
    }

    init(config: PetConfig) {
        self.config = config
        self.server = ServerController(config: config)
        super.init()
        statusText = "\(config.appTitle)还没启动，点我叫醒它"
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        server.onUnexpectedExit = { [weak self] in
            guard let self else { return }
            self.model.state = .sleeping
            self.say("服务停掉了 😵 右键 →「查看运行日志」看看原因", seconds: 8)
            self.refresh()
        }
        setupWindows()

        // kill / pkill 也走正常退出，顺带关掉自己启动的服务
        signal(SIGTERM, SIG_IGN)
        let source = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .main)
        source.setEventHandler { NSApp.terminate(nil) }
        source.resume()
        sigtermSource = source

        timers.append(Timer.scheduledTimer(withTimeInterval: 3.6, repeats: true) { [weak self] _ in self?.blink() })
        timers.append(Timer.scheduledTimer(withTimeInterval: 20, repeats: true) { [weak self] _ in self?.refresh() })

        server.checkHealth { [weak self] alive in
            guard let self else { return }
            if alive {
                self.model.state = .awake
                if !self.quietLaunch { self.openPage() }
                self.refresh()
            } else {
                self.wake(openWhenReady: !self.quietLaunch)
            }
        }
    }

    /// 再次双击桌面上的 App：打开页面
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        petClicked()
        return false
    }

    func applicationWillTerminate(_ notification: Notification) {
        server.terminateOwnedProcess()
    }

    // MARK: 窗口

    private func setupWindows() {
        let size = NSSize(width: 120, height: 120)
        petWindow = makePanel(NSRect(origin: savedOrigin(size: size), size: size))
        let container = PetContainerView(frame: NSRect(origin: .zero, size: size))
        container.delegate = self
        let host = NSHostingView(rootView: PetView(kind: config.kind, model: model))
        host.frame = container.bounds
        host.autoresizingMask = [.width, .height]
        container.addSubview(host)
        petWindow.contentView = container
        petWindow.orderFrontRegardless()

        bubbleWindow = makePanel(NSRect(x: 0, y: 0, width: 10, height: 10))
        bubbleWindow.ignoresMouseEvents = true
        bubbleHost = NSHostingView(rootView: BubbleView(text: "", textWidth: 10, line: Palette.of(config.kind).line, tailOnTop: false))
        bubbleWindow.contentView = bubbleHost
        applyPinned()
    }

    private func makePanel(_ rect: NSRect) -> NSPanel {
        let panel = NSPanel(contentRect: rect, styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = false
        panel.hidesOnDeactivate = false
        panel.isReleasedWhenClosed = false
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        return panel
    }

    private func applyPinned() {
        petWindow.level = pinned ? .floating : .normal
        bubbleWindow.level = pinned ? .floating : .normal
    }

    private func savedOrigin(size: NSSize) -> NSPoint {
        let screen = NSScreen.main?.visibleFrame ?? NSRect(x: 0, y: 0, width: 1440, height: 900)
        if let x = defaults.object(forKey: "originX") as? Double, let y = defaults.object(forKey: "originY") as? Double {
            let p = NSPoint(x: x, y: y)
            if NSScreen.screens.contains(where: { $0.visibleFrame.insetBy(dx: -60, dy: -60).contains(p) }) { return p }
        }
        return NSPoint(x: screen.maxX - config.defaultRightInset - size.width, y: screen.minY + 6)
    }

    // MARK: 气泡

    private func showBubble(_ text: String) {
        guard !text.isEmpty else { return hideBubble() }
        let pet = petWindow.frame
        let screen = petWindow.screen?.visibleFrame ?? NSScreen.main?.visibleFrame ?? .zero
        let width = BubbleView.measure(text)
        let line = Palette.of(config.kind).line

        bubbleHost.rootView = BubbleView(text: text, textWidth: width, line: line, tailOnTop: false)
        var size = bubbleHost.fittingSize
        var origin = NSPoint(x: pet.midX - size.width / 2, y: pet.maxY - 14)
        if origin.y + size.height > screen.maxY {
            bubbleHost.rootView = BubbleView(text: text, textWidth: width, line: line, tailOnTop: true)
            size = bubbleHost.fittingSize
            origin.y = pet.minY - size.height + 8
        }
        origin.x = min(max(origin.x, screen.minX + 4), screen.maxX - size.width - 4)
        bubbleWindow.setFrame(NSRect(origin: origin, size: size), display: true)
        if bubbleWindow.parent == nil { petWindow.addChildWindow(bubbleWindow, ordered: .above) }
        bubbleWindow.orderFront(nil)
    }

    private func hideBubble() {
        bubbleWindow.orderOut(nil)
    }

    /// 主动说一句话，seconds 秒后收起（鼠标还停在身上就换回状态）
    private func say(_ text: String, seconds: Double = 4) {
        messageUntil = Date().addingTimeInterval(seconds)
        showBubble(text)
        hideWork?.cancel()
        let work = DispatchWorkItem { [weak self] in
            guard let self else { return }
            if self.hovering { self.showBubble(self.statusText) } else { self.hideBubble() }
        }
        hideWork = work
        DispatchQueue.main.asyncAfter(deadline: .now() + seconds, execute: work)
    }

    // MARK: 交互

    func petClicked() {
        squash()
        switch model.state {
        case .awake: openPage()
        case .sleeping: wake(openWhenReady: true)
        case .starting: say("别急别急，还在启动…", seconds: 2)
        }
    }

    func petDragEnded() {
        let origin = petWindow.frame.origin
        defaults.set(Double(origin.x), forKey: "originX")
        defaults.set(Double(origin.y), forKey: "originY")
    }

    func petHover(_ inside: Bool) {
        hovering = inside
        guard Date() >= messageUntil else { return }
        if inside { showBubble(statusText) } else { hideBubble() }
    }

    func petMenu(_ event: NSEvent, in view: NSView) {
        let menu = NSMenu()
        let running = model.state == .awake
        menu.addItem(menuItem("打开\(config.appTitle)", #selector(openPageAction), enabled: running))
        if running {
            menu.addItem(menuItem("重启服务", #selector(restartAction)))
            menu.addItem(menuItem("停止服务", #selector(stopAction)))
        } else {
            menu.addItem(menuItem(model.state == .starting ? "正在启动…" : "启动服务", #selector(startAction),
                                  enabled: model.state == .sleeping))
        }
        menu.addItem(.separator())
        menu.addItem(menuItem("查看运行日志", #selector(openLogAction)))
        let pin = menuItem("总在最前面", #selector(togglePinAction))
        pin.state = pinned ? .on : .off
        menu.addItem(pin)
        menu.addItem(.separator())
        let quitTitle = "让\(config.petName)下班" + (server.ownsProcess ? "（同时关闭服务）" : "")
        menu.addItem(menuItem(quitTitle, #selector(quitAction)))
        NSMenu.popUpContextMenu(menu, with: event, for: view)
    }

    private func menuItem(_ title: String, _ action: Selector, enabled: Bool = true) -> NSMenuItem {
        let item = NSMenuItem(title: title, action: enabled ? action : nil, keyEquivalent: "")
        item.target = self
        item.isEnabled = enabled
        return item
    }

    @objc private func openPageAction() { openPage() }
    @objc private func startAction() { wake(openWhenReady: true) }
    @objc private func openLogAction() {
        if FileManager.default.fileExists(atPath: config.logURL.path) {
            NSWorkspace.shared.open(config.logURL)
        } else {
            say("还没有日志：服务不是我启动的，日志在启动它的终端窗口里", seconds: 5)
        }
    }
    @objc private func togglePinAction() {
        pinned.toggle()
        applyPinned()
    }
    @objc private func quitAction() { NSApp.terminate(nil) }

    @objc private func stopAction() {
        say("正在关闭服务…", seconds: 10)
        server.stop { [weak self] ok in
            guard let self else { return }
            if ok {
                self.model.state = .sleeping
                self.say("服务关掉啦，我先睡会儿 💤", seconds: 3)
            } else {
                self.say("这个服务不是我启动的，没找到它，关不掉 😣", seconds: 5)
            }
            self.refresh()
        }
    }

    @objc private func restartAction() {
        say("正在重启服务…", seconds: 30)
        model.state = .starting
        server.stop { [weak self] _ in
            self?.model.state = .sleeping
            self?.wake(openWhenReady: false)
        }
    }

    private func openPage() {
        NSWorkspace.shared.open(config.baseURL)
    }

    private func squash() {
        withAnimation(.spring(response: 0.18, dampingFraction: 0.5)) { model.squash = true }
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.16) {
            withAnimation(.spring(response: 0.35, dampingFraction: 0.45)) { self.model.squash = false }
        }
    }

    private func blink() {
        guard model.state != .sleeping, Double.random(in: 0...1) < 0.75 else { return }
        model.blinking = true
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.13) { self.model.blinking = false }
    }

    // MARK: 启动与状态

    private func wake(openWhenReady: Bool) {
        guard model.state == .sleeping else { return }
        model.state = .starting
        self.openWhenReady = openWhenReady
        say("正在启动\(config.appTitle)…", seconds: 60)
        do {
            try server.start()
        } catch {
            model.state = .sleeping
            say("启动失败：\(error.localizedDescription)", seconds: 8)
            return
        }
        waitForReady(attempt: 0)
    }

    private func becameReady() {
        model.state = .awake
        say("启动好啦 ✨", seconds: 2.5)
        if openWhenReady { openPage() }
        openWhenReady = false
    }

    private func waitForReady(attempt: Int) {
        server.checkHealth { [weak self] alive in
            guard let self, self.model.state == .starting else { return }
            if alive {
                self.becameReady()
                self.refresh()
                return
            }
            if !self.server.ownsProcess {
                self.model.state = .sleeping
                self.say("没启动起来 😵 右键 →「查看运行日志」看看原因", seconds: 8)
                return
            }
            if attempt == 12 && self.config.workDir.hasPrefix(NSHomeDirectory() + "/Desktop") {
                // 项目在「桌面」文件夹里，macOS 第一次会弹窗确认，确认前 Python 会一直卡住
                self.say("如果弹窗问能不能访问「桌面」文件夹，点「好」我才能启动哦 🙏", seconds: 30)
            } else if attempt == 120 {
                self.say("启动有点慢，右键 →「查看运行日志」看看卡在哪", seconds: 8)
            }
            DispatchQueue.main.asyncAfter(deadline: .now() + (attempt < 120 ? 0.5 : 2)) {
                self.waitForReady(attempt: attempt + 1)
            }
        }
    }

    private func refresh() {
        server.checkHealth { [weak self] alive in
            guard let self else { return }
            guard alive else {
                if self.model.state == .awake { self.model.state = .sleeping }
                if self.model.state == .sleeping {
                    self.model.badge = 0
                    self.statusText = "\(self.config.appTitle)还没启动，点我叫醒它"
                }
                return
            }
            switch self.model.state {
            case .sleeping: self.model.state = .awake
            case .starting: self.becameReady()
            case .awake: break
            }
            switch self.config.kind {
            case .kanban: self.refreshKanban()
            case .jobhunter: self.refreshJobHunter()
            }
        }
    }

    private func updateStatus(_ lines: [String], urgent: Int, headline: String?) {
        statusText = lines.joined(separator: "\n")
        model.badge = urgent
        // 新冒出来的紧急事项主动提醒一次
        if lastUrgent >= 0, urgent > lastUrgent, let headline { say(headline, seconds: 8) }
        lastUrgent = urgent
        if hovering && Date() >= messageUntil { showBubble(statusText) }
    }

    private func refreshKanban() {
        server.fetchJSON("api/agenda") { [weak self] agenda in
            self?.server.fetchJSON("api/data") { cards in
                guard let self else { return }
                let data = agenda as? [String: Any] ?? [:]
                let items = data["items"] as? [[String: Any]] ?? []
                let stale = data["stale"] as? [[String: Any]] ?? []
                func bucket(_ name: String) -> [[String: Any]] { items.filter { $0["bucket"] as? String == name } }
                func describe(_ item: [String: Any], withDate: Bool) -> String {
                    let title = item["title"] as? String ?? ""
                    let at = item["at"] as? String ?? ""
                    let allDay = item["all_day"] as? Bool ?? false
                    let date = String(at.prefix(10).suffix(5)).replacingOccurrences(of: "-", with: "/")
                    let time = allDay ? "" : String(at.suffix(5))
                    return "\(title) \(withDate ? date + " " : "")\(time)".trimmingCharacters(in: .whitespaces)
                }
                let overdue = bucket("overdue"), today = bucket("today"), tomorrow = bucket("tomorrow")

                var lines: [String] = []
                if !overdue.isEmpty {
                    lines.append("⚠️ 过期 \(overdue.count) 件：" + overdue.prefix(2).map { describe($0, withDate: true) }.joined(separator: "、"))
                }
                if !today.isEmpty {
                    lines.append("今天：" + today.prefix(3).map { describe($0, withDate: false) }.joined(separator: "、"))
                }
                if !tomorrow.isEmpty {
                    lines.append("明天：" + tomorrow.prefix(2).map { describe($0, withDate: false) }.joined(separator: "、"))
                }
                if let s = stale.first, let company = s["company"] as? String, let days = s["days"] as? Int {
                    let more = stale.count > 1 ? " 等 \(stale.count) 家" : ""
                    lines.append("\(company)\(more)等了 \(days) 天没动静，该跟进啦")
                }
                if lines.isEmpty { lines.append("近期没有测评和面试，可以多投几家 📮") }

                let list = cards as? [[String: Any]] ?? []
                let count = { (s: String) in list.filter { $0["status"] as? String == s }.count }
                var summary = "在跟 \(list.count) 家"
                for (key, label) in [("pending", "待测评"), ("interview", "面试中"), ("waiting", "等结果"), ("offer", "Offer")] where count(key) > 0 {
                    summary += " · \(label) \(count(key))"
                }
                lines.append(summary)

                let urgent = overdue.count + today.count
                self.updateStatus(lines, urgent: urgent, headline: lines.first)
            }
        }
    }

    private func refreshJobHunter() {
        server.fetchJSON("api/risk_status") { [weak self] risk in
            self?.server.fetchJSON("api/stats") { stats in
                guard let self else { return }
                let r = risk as? [String: Any] ?? [:]
                let s = stats as? [String: Any] ?? [:]
                let applied = r["today_applied"] as? Int ?? s["today_applied"] as? Int ?? 0
                let limit = r["daily_limit"] as? Int ?? 0
                let approved = s["approved"] as? Int ?? 0
                let unscored = s["unscored"] as? Int ?? 0

                var lines: [String] = []
                if limit > 0 && applied >= limit {
                    lines.append("今天投了 \(applied)/\(limit)，投满啦，歇会儿 ☕")
                } else if applied == 0 {
                    lines.append("今天还没投哦" + (limit > 0 ? "（上限 \(limit)）" : ""))
                } else {
                    lines.append("今天投了 \(applied)/\(limit)，继续加油 💪")
                }
                lines.append(approved > 0 ? "待投递 \(approved) 个岗位" : "待投递清空了，去「按公司推荐」挑挑")
                if unscored > 0 { lines.append("AI 还在分析 \(unscored) 个岗位") }
                self.updateStatus(lines, urgent: 0, headline: nil)
            }
        }
    }
}

// MARK: - 生成图标

/// 构建脚本调用：Pet --render-icon <kanban|jobhunter> <输出 png>
func renderIcon(kind: PetKind, to path: String) {
    let model = PetModel()
    model.state = .awake
    let side: CGFloat = 1024
    let view = PetView(kind: kind, model: model, animated: false)
        .scaleEffect(side / 120 * 0.92)
        .frame(width: side, height: side)
    let host = NSHostingView(rootView: view)
    host.frame = NSRect(x: 0, y: 0, width: side, height: side)
    host.layoutSubtreeIfNeeded()
    guard let rep = host.bitmapImageRepForCachingDisplay(in: host.bounds) else { exit(1) }
    host.cacheDisplay(in: host.bounds, to: rep)
    guard let png = rep.representation(using: .png, properties: [:]) else { exit(1) }
    do { try png.write(to: URL(fileURLWithPath: path)) } catch { exit(1) }
}

// MARK: - 入口

let args = CommandLine.arguments
if let i = args.firstIndex(of: "--render-icon"), i + 2 < args.count {
    renderIcon(kind: PetKind(rawValue: args[i + 1]) ?? .kanban, to: args[i + 2])
    exit(0)
}

let app = NSApplication.shared
let delegate = AppDelegate(config: PetConfig.load())
app.delegate = delegate
app.setActivationPolicy(.accessory)
app.run()
