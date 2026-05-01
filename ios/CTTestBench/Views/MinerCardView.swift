import SwiftUI

// MARK: - Miner Card View

struct MinerCardView: View {
    let miner: Miner
    var onFlashLED: (() -> Void)?
    var onMarkReady: (() -> Void)?
    var onMarkParts: (() -> Void)?
    var onPrintLabel: (() -> Void)?
    var onFanControl: ((String) -> Void)?

    @State private var showingActions = false
    @State private var isFlashing = false
    @State private var showFanSheet = false

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            // ── Header ────────────────────────────────────────────────────────
            HStack(alignment: .top) {
                VStack(alignment: .leading, spacing: 4) {
                    Text(miner.model ?? "Unknown Model")
                        .font(.headline)
                        .fontWeight(.bold)

                    Text(miner.ipAddress)
                        .font(.subheadline)
                        .foregroundColor(.secondary)
                        .monospacedDigit()

                    if let serial = miner.serialNumber {
                        Text("S/N: \(serial)")
                            .font(.caption)
                            .foregroundColor(.secondary)
                    }
                    if let mac = miner.macAddress {
                        Text("MAC: \(mac)")
                            .font(.caption)
                            .foregroundColor(.secondary)
                    }
                }
                Spacer()
                StatusBadge(status: miner.status)
            }

            // ── Hashboards ────────────────────────────────────────────────────
            if let hbs = miner.hashboards, !hbs.isEmpty {
                HashboardLayoutView(hashboards: hbs)
            }

            Divider()

            // ── Stats row ─────────────────────────────────────────────────────
            HStack(spacing: 16) {
                if let rt = miner.rtHashrate {
                    StatPill(label: "RT", value: String(format: "%.1f TH/s", rt), color: .blue)
                }
                if let eff = miner.efficiencyPct {
                    StatPill(label: "EFF", value: String(format: "%.0f%%", eff),
                             color: eff >= 90 ? .green : eff >= 70 ? .orange : .red)
                }
                if let fans = miner.fanSpeeds, let first = fans.first {
                    StatPill(label: "FAN", value: "\(first) RPM", color: .cyan)
                }
            }

            // ── Worker ────────────────────────────────────────────────────────
            if let worker = miner.worker, !worker.isEmpty {
                Label(worker, systemImage: "person.fill")
                    .font(.caption)
                    .foregroundColor(.secondary)
            }

            // ── AI Diagnosis ──────────────────────────────────────────────────
            if let summary = miner.aiIssueSummary, !summary.isEmpty {
                AIBanner(
                    summary: summary,
                    fix: miner.aiSuggestedFix,
                    severity: miner.aiSeverity ?? "ok"
                )
            }

            Divider()

            // ── Action buttons ────────────────────────────────────────────────
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 8) {
                    ActionButton(title: "Print Label", icon: "printer", color: .indigo) {
                        onPrintLabel?()
                    }
                    ActionButton(
                        title: isFlashing ? "Stop Flash" : "Flash Lights",
                        icon: "flashlight.on.fill",
                        color: .yellow
                    ) {
                        isFlashing.toggle()
                        onFlashLED?()
                    }
                    ActionButton(title: "Mark Ready", icon: "checkmark.seal.fill", color: .green) {
                        onMarkReady?()
                    }
                    ActionButton(title: "Parts Unit", icon: "wrench.and.screwdriver", color: .purple) {
                        onMarkParts?()
                    }
                    ActionButton(title: "Fan Control", icon: "fan", color: .cyan) {
                        showFanSheet = true
                    }
                }
                .padding(.horizontal, 2)
            }
        }
        .padding()
        .background(Color(.secondarySystemBackground))
        .cornerRadius(16)
        .shadow(color: .black.opacity(0.05), radius: 4, x: 0, y: 2)
        .sheet(isPresented: $showFanSheet) {
            FanControlSheet { mode in
                onFanControl?(mode)
            }
        }
    }
}

// MARK: - Status Badge

struct StatusBadge: View {
    let status: MinerStatus

    var body: some View {
        Text(status.displayText)
            .font(.caption)
            .fontWeight(.semibold)
            .padding(.horizontal, 10)
            .padding(.vertical, 5)
            .background(status.badgeColor.opacity(0.15))
            .foregroundColor(status.badgeColor)
            .cornerRadius(20)
            .overlay(
                RoundedRectangle(cornerRadius: 20)
                    .stroke(status.badgeColor.opacity(0.4), lineWidth: 1)
            )
    }
}

extension MinerStatus {
    var displayText: String {
        switch self {
        case .readyToDeploy: return "✓ Ready"
        case .needsRepair: return "★ Repair"
        case .partsUnit: return "⚠ Parts"
        case .psuFault: return "✗ PSU"
        case .controlBoardFault: return "✗ CB Fault"
        case .testing: return "⟳ Testing"
        case .unknown: return "? Unknown"
        }
    }

    var badgeColor: Color {
        switch self {
        case .readyToDeploy: return .green
        case .needsRepair: return .orange
        case .partsUnit: return .purple
        case .psuFault, .controlBoardFault: return .red
        case .testing: return .blue
        case .unknown: return .gray
        }
    }
}

// MARK: - Supporting Views

struct StatPill: View {
    let label: String
    let value: String
    var color: Color = .primary

    var body: some View {
        VStack(spacing: 1) {
            Text(label)
                .font(.caption2)
                .foregroundColor(.secondary)
            Text(value)
                .font(.caption)
                .fontWeight(.semibold)
                .foregroundColor(color)
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 6)
        .background(color.opacity(0.08))
        .cornerRadius(10)
    }
}

struct AIBanner: View {
    let summary: String
    let fix: String?
    let severity: String

    var severityColor: Color {
        switch severity {
        case "critical": return .red
        case "warning": return .orange
        default: return .green
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack {
                Image(systemName: severity == "critical" ? "exclamationmark.triangle.fill" :
                      severity == "warning" ? "exclamationmark.circle.fill" : "checkmark.circle.fill")
                    .foregroundColor(severityColor)
                Text("AI Diagnosis")
                    .font(.caption)
                    .fontWeight(.bold)
                    .foregroundColor(severityColor)
            }
            Text(summary)
                .font(.caption)
                .foregroundColor(.primary)
            if let fix = fix, !fix.isEmpty, fix != "None required." {
                Text("Fix: \(fix)")
                    .font(.caption)
                    .foregroundColor(.secondary)
                    .italic()
            }
        }
        .padding(10)
        .background(severityColor.opacity(0.08))
        .cornerRadius(10)
        .overlay(
            RoundedRectangle(cornerRadius: 10)
                .stroke(severityColor.opacity(0.3), lineWidth: 1)
        )
    }
}

struct ActionButton: View {
    let title: String
    let icon: String
    var color: Color = .blue
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            VStack(spacing: 4) {
                Image(systemName: icon)
                    .font(.system(size: 18))
                Text(title)
                    .font(.caption2)
                    .fontWeight(.medium)
            }
            .foregroundColor(color)
            .frame(width: 72, height: 56)
            .background(color.opacity(0.1))
            .cornerRadius(12)
        }
    }
}

struct FanControlSheet: View {
    let onSelect: (String) -> Void
    @Environment(\.dismiss) var dismiss

    var body: some View {
        NavigationView {
            List {
                Button("Auto (Normal)") { onSelect("auto"); dismiss() }
                Button("Minimum (20%)") { onSelect("min"); dismiss() }
                Button("Maximum (100%)") { onSelect("max"); dismiss() }
                Button("Off (0%) ⚠") { onSelect("off"); dismiss() }
                    .foregroundColor(.red)
            }
            .navigationTitle("Fan Control")
            .navigationBarItems(trailing: Button("Cancel") { dismiss() })
        }
    }
}

// MARK: - Preview

#Preview {
    ScrollView {
        MinerCardView(miner: Miner(
            id: 1,
            ipAddress: "192.168.1.50",
            macAddress: "AA:BB:CC:DD:EE:FF",
            serialNumber: "SN12345678",
            model: "Antminer S19j Pro 104TH",
            firmwareVersion: "1.0.0",
            minerType: "antminer",
            poolUrl: "stratum+tcp://pool.example.com:3333",
            worker: "pool/worker1",
            hashboards: [
                Hashboard(index: 1, position: "LEFT", chipCount: 144, expectedChips: 144,
                          missingChips: 0, tempAvg: 65, tempMax: 72, healthPct: 100, active: true),
                Hashboard(index: 2, position: "CENTER", chipCount: 132, expectedChips: 144,
                          missingChips: 12, tempAvg: 70, tempMax: 85, healthPct: 91.7, active: true),
                Hashboard(index: 3, position: "RIGHT", chipCount: 80, expectedChips: 144,
                          missingChips: 64, tempAvg: 55, tempMax: 60, healthPct: 55.6, active: true),
            ],
            fanSpeeds: [4500, 4200],
            psuVoltage: 12.1,
            psuCurrent: 45.0,
            rtHashrate: 92.4,
            avgHashrate: 91.8,
            idealHashrate: 104.0,
            psuFault: false,
            controlBoardFault: false,
            deadChips: 64,
            aiIssueSummary: "HB3 (RIGHT) showing 64 missing chips.",
            aiSuggestedFix: "Replace voltage regulators on HB3.",
            aiAutoFixable: false,
            aiSeverity: "critical",
            status: .needsRepair,
            siteId: nil,
            rackId: nil,
            firstSeen: "2024-01-01T00:00:00",
            lastTested: "2024-01-15T10:30:00",
            lastSeen: "2024-01-15T10:30:00"
        ))
    }
    .padding()
}
