import Foundation
import Combine

// MARK: - WebSocket Client

class WebSocketClient: ObservableObject {
    static let shared = WebSocketClient()

    @Published var connectionState: ConnectionState = .disconnected
    @Published var lastMinerUpdate: Miner? = nil
    @Published var discoveredIP: String? = nil

    private var webSocketTask: URLSessionWebSocketTask?
    private var pingTimer: Timer?
    private var reconnectTask: Task<Void, Never>?

    enum ConnectionState {
        case connected, disconnected, connecting
    }

    func connect(serverURL: String = UserDefaults.standard.string(forKey: "serverURL") ?? "http://192.168.1.100:8000") {
        guard connectionState != .connected else { return }

        let wsURL = serverURL
            .replacingOccurrences(of: "http://", with: "ws://")
            .replacingOccurrences(of: "https://", with: "wss://")
        guard let url = URL(string: "\(wsURL)/ws/miners") else { return }

        connectionState = .connecting
        let session = URLSession(configuration: .default)
        webSocketTask = session.webSocketTask(with: url)
        webSocketTask?.resume()
        connectionState = .connected

        receiveMessage()
        startPing()
    }

    func disconnect() {
        pingTimer?.invalidate()
        webSocketTask?.cancel(with: .normalClosure, reason: nil)
        connectionState = .disconnected
    }

    private func receiveMessage() {
        webSocketTask?.receive { [weak self] result in
            switch result {
            case .success(let message):
                switch message {
                case .string(let text):
                    self?.handleMessage(text)
                case .data(let data):
                    if let text = String(data: data, encoding: .utf8) {
                        self?.handleMessage(text)
                    }
                @unknown default:
                    break
                }
                self?.receiveMessage() // Listen for next message
            case .failure(let error):
                print("WebSocket receive error: \(error)")
                DispatchQueue.main.async {
                    self?.connectionState = .disconnected
                }
                self?.scheduleReconnect()
            }
        }
    }

    private func handleMessage(_ text: String) {
        guard let data = text.data(using: .utf8),
              let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let event = json["event"] as? String,
              let msgData = json["data"] as? [String: Any] else { return }

        DispatchQueue.main.async {
            switch event {
            case "miner_update":
                if let jsonData = try? JSONSerialization.data(withJSONObject: msgData),
                   let miner = try? JSONDecoder().decode(Miner.self, from: jsonData) {
                    self.lastMinerUpdate = miner
                }
            case "miner_discovered":
                self.discoveredIP = msgData["ip_address"] as? String
            case "diagnosis_complete":
                // Trigger refresh in relevant views
                NotificationCenter.default.post(
                    name: .diagnosisComplete,
                    object: nil,
                    userInfo: msgData
                )
            default:
                break
            }
        }
    }

    private func startPing() {
        pingTimer = Timer.scheduledTimer(withTimeInterval: 30, repeats: true) { [weak self] _ in
            self?.webSocketTask?.send(.string("ping")) { _ in }
        }
    }

    private func scheduleReconnect() {
        reconnectTask = Task {
            try? await Task.sleep(nanoseconds: 5_000_000_000) // 5 seconds
            await MainActor.run {
                self.connect()
            }
        }
    }
}

extension Notification.Name {
    static let diagnosisComplete = Notification.Name("diagnosisComplete")
    static let minerDiscovered = Notification.Name("minerDiscovered")
}
