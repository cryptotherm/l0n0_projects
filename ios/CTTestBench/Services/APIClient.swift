import Foundation
import Combine

// MARK: - API Client

class APIClient: ObservableObject {
    static let shared = APIClient()

    var baseURL: String {
        UserDefaults.standard.string(forKey: "serverURL") ?? "http://192.168.1.100:8000"
    }

    private let session: URLSession

    init() {
        let config = URLSessionConfiguration.default
        config.timeoutIntervalForRequest = 30
        self.session = URLSession(configuration: config)
    }

    // MARK: - Generic request

    func get<T: Decodable>(_ path: String) async throws -> T {
        let url = URL(string: "\(baseURL)\(path)")!
        let (data, _) = try await session.data(from: url)
        return try decode(data)
    }

    func post<T: Decodable>(_ path: String, body: Encodable? = nil) async throws -> T {
        var req = URLRequest(url: URL(string: "\(baseURL)\(path)")!)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        if let body = body {
            req.httpBody = try JSONEncoder().encode(body)
        }
        let (data, _) = try await session.data(for: req)
        return try decode(data)
    }

    func patch<T: Decodable>(_ path: String, body: Encodable) async throws -> T {
        var req = URLRequest(url: URL(string: "\(baseURL)\(path)")!)
        req.httpMethod = "PATCH"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try JSONEncoder().encode(body)
        let (data, _) = try await session.data(for: req)
        return try decode(data)
    }

    func postVoid(_ path: String, body: Encodable? = nil) async throws {
        var req = URLRequest(url: URL(string: "\(baseURL)\(path)")!)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        if let body = body {
            req.httpBody = try JSONEncoder().encode(body)
        }
        _ = try await session.data(for: req)
    }

    private func decode<T: Decodable>(_ data: Data) throws -> T {
        let decoder = JSONDecoder()
        return try decoder.decode(T.self, from: data)
    }

    // MARK: - Miners API

    func listMiners(status: String? = nil) async throws -> [Miner] {
        var path = "/api/miners"
        if let status = status {
            path += "?status=\(status)"
        }
        return try await get(path)
    }

    func testMiner(ip: String, runAI: Bool = true) async throws -> Miner {
        return try await post("/api/miners/test?ip_address=\(ip)&run_ai=\(runAI)")
    }

    func flashLED(minerId: Int, blink: Bool) async throws {
        try await postVoid("/api/miners/\(minerId)/led?blink=\(blink)")
    }

    func controlFans(minerId: Int, mode: String) async throws {
        try await postVoid("/api/miners/\(minerId)/fans?mode=\(mode)")
    }

    func markReady(minerId: Int) async throws {
        try await postVoid("/api/miners/\(minerId)/mark_ready")
    }

    func markPartsUnit(minerId: Int) async throws {
        try await postVoid("/api/miners/\(minerId)/mark_parts")
    }

    func printLabel(minerId: Int) async throws {
        try await postVoid("/api/labels/\(minerId)/print")
    }

    func labelPreviewURL(minerId: Int) -> URL {
        URL(string: "\(baseURL)/api/labels/\(minerId)/preview")!
    }

    // MARK: - Discovery

    struct ProbeResult: Codable {
        let ipAddress: String
        let reachable: Bool
        let minerType: String?
        let model: String?
        let macAddress: String?

        enum CodingKeys: String, CodingKey {
            case reachable
            case ipAddress = "ip_address"
            case minerType = "miner_type"
            case model
            case macAddress = "mac_address"
        }
    }

    func probeIP(_ ip: String) async throws -> ProbeResult {
        struct Body: Encodable { let ip_address: String }
        return try await post("/api/discovery/probe", body: Body(ip_address: ip))
    }

    // MARK: - Sites

    func listSites() async throws -> [Site] {
        return try await get("/api/sites")
    }

    func createSite(name: String, location: String?) async throws -> Site {
        struct Body: Encodable { let name: String; let location: String? }
        return try await post("/api/sites", body: Body(name: name, location: location))
    }

    func rackMiners(siteId: Int, rackId: Int) async throws -> RackMinersResponse {
        return try await get("/api/sites/\(siteId)/racks/\(rackId)/miners")
    }

    struct RackMinersResponse: Codable {
        let total: Int
        let readyToDeploy: Int
        let needsSwap: Int
        let swapSuggestions: [SwapSuggestion]

        enum CodingKeys: String, CodingKey {
            case total
            case readyToDeploy = "ready_to_deploy"
            case needsSwap = "needs_swap"
            case swapSuggestions = "swap_suggestions"
        }
    }

    struct SwapSuggestion: Codable, Identifiable {
        let minerId: Int
        let ipAddress: String
        let model: String?
        let status: String
        let aiSummary: String?

        var id: Int { minerId }

        enum CodingKeys: String, CodingKey {
            case model, status
            case minerId = "miner_id"
            case ipAddress = "ip_address"
            case aiSummary = "ai_summary"
        }
    }

    func scanRack(siteId: Int, rackId: Int, runTests: Bool = false) async throws {
        try await postVoid("/api/sites/\(siteId)/racks/\(rackId)/scan?run_tests=\(runTests)")
    }

    func identifyRack(siteId: Int, rackId: Int, statusFilter: String? = nil) async throws {
        var path = "/api/sites/\(siteId)/racks/\(rackId)/identify"
        if let f = statusFilter { path += "?status_filter=\(f)" }
        try await postVoid(path)
    }
}
