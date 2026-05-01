import Foundation
import Combine

// MARK: - Models

struct Miner: Codable, Identifiable {
    let id: Int
    var ipAddress: String
    var macAddress: String?
    var serialNumber: String?
    var model: String?
    var firmwareVersion: String?
    var minerType: String
    var poolUrl: String?
    var worker: String?
    var hashboards: [Hashboard]?
    var fanSpeeds: [Int]?
    var psuVoltage: Double?
    var psuCurrent: Double?
    var rtHashrate: Double?
    var avgHashrate: Double?
    var idealHashrate: Double?
    var psuFault: Bool
    var controlBoardFault: Bool
    var deadChips: Int
    var aiIssueSummary: String?
    var aiSuggestedFix: String?
    var aiAutoFixable: Bool
    var aiSeverity: String?
    var status: MinerStatus
    var siteId: Int?
    var rackId: Int?
    var firstSeen: String
    var lastTested: String?
    var lastSeen: String

    enum CodingKeys: String, CodingKey {
        case id, model, worker, status, hashboards
        case ipAddress = "ip_address"
        case macAddress = "mac_address"
        case serialNumber = "serial_number"
        case firmwareVersion = "firmware_version"
        case minerType = "miner_type"
        case poolUrl = "pool_url"
        case fanSpeeds = "fan_speeds"
        case psuVoltage = "psu_voltage"
        case psuCurrent = "psu_current"
        case rtHashrate = "rt_hashrate"
        case avgHashrate = "avg_hashrate"
        case idealHashrate = "ideal_hashrate"
        case psuFault = "psu_fault"
        case controlBoardFault = "control_board_fault"
        case deadChips = "dead_chips"
        case aiIssueSummary = "ai_issue_summary"
        case aiSuggestedFix = "ai_suggested_fix"
        case aiAutoFixable = "ai_auto_fixable"
        case aiSeverity = "ai_severity"
        case siteId = "site_id"
        case rackId = "rack_id"
        case firstSeen = "first_seen"
        case lastTested = "last_tested"
        case lastSeen = "last_seen"
    }

    var statusColor: Color {
        switch status {
        case .readyToDeploy: return .green
        case .needsRepair: return .orange
        case .partsUnit: return .purple
        case .psuFault, .controlBoardFault: return .red
        case .testing: return .blue
        case .unknown: return .gray
        }
    }

    var statusDisplayText: String {
        switch status {
        case .readyToDeploy: return "✓ Ready to Deploy"
        case .needsRepair: return "★ Needs Repair"
        case .partsUnit: return "⚠ Parts Unit"
        case .psuFault: return "✗ PSU Fault"
        case .controlBoardFault: return "✗ Control Board Fault"
        case .testing: return "⟳ Testing..."
        case .unknown: return "? Unknown"
        }
    }

    var efficiencyPct: Double? {
        guard let rt = rtHashrate, let ideal = idealHashrate, ideal > 0 else { return nil }
        return (rt / ideal) * 100
    }
}

enum MinerStatus: String, Codable {
    case readyToDeploy = "ready_to_deploy"
    case needsRepair = "needs_repair"
    case partsUnit = "parts_unit"
    case psuFault = "psu_fault"
    case controlBoardFault = "control_board_fault"
    case testing = "testing"
    case unknown = "unknown"
}

struct Hashboard: Codable, Identifiable {
    var id: Int { index }
    let index: Int
    let position: String
    let chipCount: Int
    let expectedChips: Int
    let missingChips: Int
    let tempAvg: Double
    let tempMax: Double
    let healthPct: Double
    let active: Bool

    enum CodingKeys: String, CodingKey {
        case index, position, active
        case chipCount = "chip_count"
        case expectedChips = "expected_chips"
        case missingChips = "missing_chips"
        case tempAvg = "temp_avg"
        case tempMax = "temp_max"
        case healthPct = "health_pct"
    }

    var healthColor: Color {
        if healthPct >= 85 { return .green }
        if healthPct >= 60 { return .orange }
        return .red
    }
}

// MARK: - Site/Rack Models

struct Site: Codable, Identifiable {
    let id: Int
    let name: String
    let location: String?
    let createdAt: String
    var racks: [Rack]

    enum CodingKeys: String, CodingKey {
        case id, name, location, racks
        case createdAt = "created_at"
    }
}

struct Rack: Codable, Identifiable {
    let id: Int
    let siteId: Int
    let name: String
    let ipRangeStart: String?
    let ipRangeEnd: String?
    let ipCidr: String?

    enum CodingKeys: String, CodingKey {
        case id, name
        case siteId = "site_id"
        case ipRangeStart = "ip_range_start"
        case ipRangeEnd = "ip_range_end"
        case ipCidr = "ip_cidr"
    }
}

// MARK: - WebSocket Messages

struct WSMessage: Codable {
    let event: String
    let data: AnyCodable
}

struct AnyCodable: Codable {
    let value: Any

    init(_ value: Any) { self.value = value }

    init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        if let dict = try? container.decode([String: AnyJSON].self) {
            value = dict
        } else {
            value = [:]
        }
    }

    func encode(to encoder: Encoder) throws {}
}

struct AnyJSON: Codable {
    let value: Any
    init(from decoder: Decoder) throws {
        let c = try decoder.singleValueContainer()
        if let s = try? c.decode(String.self) { value = s }
        else if let i = try? c.decode(Int.self) { value = i }
        else if let d = try? c.decode(Double.self) { value = d }
        else if let b = try? c.decode(Bool.self) { value = b }
        else { value = "" }
    }
    func encode(to encoder: Encoder) throws {}
}

// Import SwiftUI Color for use in model
import SwiftUI
