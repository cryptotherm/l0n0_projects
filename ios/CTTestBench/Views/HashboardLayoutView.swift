import SwiftUI

// MARK: - Hashboard Layout View

struct HashboardLayoutView: View {
    let hashboards: [Hashboard]

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("HASHBOARDS")
                .font(.caption)
                .fontWeight(.bold)
                .foregroundColor(.secondary)

            HStack(spacing: 8) {
                ForEach(sortedBoards) { hb in
                    HashboardCell(hashboard: hb)
                }
                // Fill empty slots if < 3 boards
                if hashboards.count < 3 {
                    ForEach(hashboards.count..<3, id: \.self) { i in
                        HashboardCell(hashboard: nil, index: i + 1)
                    }
                }
            }

            // Position labels
            HStack(spacing: 8) {
                ForEach(sortedBoards) { hb in
                    Text(hb.position)
                        .font(.caption2)
                        .foregroundColor(.secondary)
                        .frame(maxWidth: .infinity)
                }
                if hashboards.count < 3 {
                    ForEach(hashboards.count..<3, id: \.self) { _ in
                        Text("—")
                            .font(.caption2)
                            .foregroundColor(.secondary)
                            .frame(maxWidth: .infinity)
                    }
                }
            }
        }
    }

    private var sortedBoards: [Hashboard] {
        hashboards.sorted { $0.index < $1.index }
    }
}

struct HashboardCell: View {
    let hashboard: Hashboard?
    var index: Int = 0

    var body: some View {
        VStack(spacing: 4) {
            if let hb = hashboard {
                RoundedRectangle(cornerRadius: 8)
                    .fill(hb.healthColor.opacity(0.15))
                    .overlay(
                        RoundedRectangle(cornerRadius: 8)
                            .stroke(hb.healthColor, lineWidth: 2)
                    )
                    .frame(height: 80)
                    .overlay(
                        VStack(spacing: 2) {
                            Text("HB\(hb.index)")
                                .font(.caption)
                                .fontWeight(.bold)
                                .foregroundColor(hb.healthColor)

                            Text("\(Int(hb.healthPct))%")
                                .font(.title3)
                                .fontWeight(.heavy)
                                .foregroundColor(hb.healthColor)

                            Text("\(hb.chipCount)/\(hb.expectedChips)")
                                .font(.caption2)
                                .foregroundColor(.secondary)

                            if hb.tempMax > 0 {
                                Text("\(Int(hb.tempMax))°C")
                                    .font(.caption2)
                                    .foregroundColor(hb.tempMax > 90 ? .red : .secondary)
                            }
                        }
                    )
            } else {
                RoundedRectangle(cornerRadius: 8)
                    .fill(Color.gray.opacity(0.1))
                    .overlay(
                        RoundedRectangle(cornerRadius: 8)
                            .stroke(Color.gray.opacity(0.3), lineWidth: 1)
                    )
                    .frame(height: 80)
                    .overlay(
                        VStack {
                            Text("HB\(index)")
                                .font(.caption)
                                .foregroundColor(.secondary)
                            Text("N/A")
                                .font(.caption2)
                                .foregroundColor(.secondary)
                        }
                    )
            }
        }
        .frame(maxWidth: .infinity)
    }
}

// MARK: - Preview

#Preview {
    HashboardLayoutView(hashboards: [
        Hashboard(index: 1, position: "LEFT", chipCount: 144, expectedChips: 144,
                  missingChips: 0, tempAvg: 65, tempMax: 72, healthPct: 100, active: true),
        Hashboard(index: 2, position: "CENTER", chipCount: 132, expectedChips: 144,
                  missingChips: 12, tempAvg: 70, tempMax: 85, healthPct: 91.7, active: true),
        Hashboard(index: 3, position: "RIGHT", chipCount: 80, expectedChips: 144,
                  missingChips: 64, tempAvg: 55, tempMax: 60, healthPct: 55.6, active: true),
    ])
    .padding()
}
