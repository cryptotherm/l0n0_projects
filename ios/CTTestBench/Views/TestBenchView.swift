import SwiftUI

// MARK: - Test Bench View (main tab)

struct TestBenchView: View {
    @StateObject private var vm = TestBenchViewModel()
    @State private var showScanner = false
    @State private var showManualEntry = false
    @State private var manualIP = ""
    @State private var showSettings = false

    var body: some View {
        NavigationView {
            ZStack {
                Color(.systemGroupedBackground).ignoresSafeArea()

                VStack(spacing: 0) {
                    // ── Connection banner ──────────────────────────────────────
                    ConnectionBanner()

                    // ── Miner list ─────────────────────────────────────────────
                    if vm.miners.isEmpty && !vm.isLoading {
                        EmptyTestBenchView {
                            showScanner = true
                        }
                    } else {
                        ScrollView {
                            LazyVStack(spacing: 12) {
                                ForEach(vm.miners) { miner in
                                    MinerCardView(
                                        miner: miner,
                                        onFlashLED: { vm.toggleLED(miner) },
                                        onMarkReady: { vm.markReady(miner) },
                                        onMarkParts: { vm.markParts(miner) },
                                        onPrintLabel: { vm.printLabel(miner) },
                                        onFanControl: { mode in vm.controlFan(miner, mode: mode) }
                                    )
                                }
                            }
                            .padding()
                        }
                        .refreshable {
                            await vm.loadMiners()
                        }
                    }
                }

                // ── Scan FAB ──────────────────────────────────────────────────
                VStack {
                    Spacer()
                    HStack {
                        Spacer()
                        Button {
                            showScanner = true
                        } label: {
                            Image(systemName: "barcode.viewfinder")
                                .font(.title2)
                                .foregroundColor(.white)
                                .frame(width: 60, height: 60)
                                .background(Color.blue)
                                .clipShape(Circle())
                                .shadow(radius: 6)
                        }
                        .padding(.trailing, 24)
                        .padding(.bottom, 24)
                    }
                }
            }
            .navigationTitle("Test Bench")
            .toolbar {
                ToolbarItem(placement: .navigationBarLeading) {
                    Button {
                        showSettings = true
                    } label: {
                        Image(systemName: "gearshape")
                    }
                }
                ToolbarItem(placement: .navigationBarTrailing) {
                    Button {
                        showManualEntry = true
                    } label: {
                        Image(systemName: "plus.circle")
                    }
                }
            }
            .sheet(isPresented: $showScanner) {
                ScannerView { result in
                    if let ip = result.ipAddress {
                        Task { await vm.testMiner(ip: ip) }
                    } else if let serial = result.serialNumber {
                        Task { await vm.findAndTestBySerial(serial) }
                    }
                }
            }
            .sheet(isPresented: $showManualEntry) {
                ManualIPSheet { ip in
                    Task { await vm.testMiner(ip: ip) }
                }
            }
            .sheet(isPresented: $showSettings) {
                SettingsView()
            }
            .alert("Error", isPresented: $vm.showError) {
                Button("OK") {}
            } message: {
                Text(vm.errorMessage)
            }
            .overlay {
                if vm.isLoading {
                    ProgressView("Testing miner...")
                        .padding()
                        .background(.regularMaterial)
                        .cornerRadius(12)
                }
            }
        }
        .task {
            await vm.loadMiners()
        }
    }
}

// MARK: - View Model

@MainActor
class TestBenchViewModel: ObservableObject {
    @Published var miners: [Miner] = []
    @Published var isLoading = false
    @Published var showError = false
    @Published var errorMessage = ""

    private let api = APIClient.shared

    func loadMiners() async {
        do {
            miners = try await api.listMiners()
        } catch {
            showError(error)
        }
    }

    func testMiner(ip: String) async {
        isLoading = true
        defer { isLoading = false }
        do {
            let miner = try await api.testMiner(ip: ip)
            if let idx = miners.firstIndex(where: { $0.ipAddress == ip }) {
                miners[idx] = miner
            } else {
                miners.insert(miner, at: 0)
            }
        } catch {
            showError(error)
        }
    }

    func findAndTestBySerial(_ serial: String) async {
        // Look up in local list
        if let m = miners.first(where: { $0.serialNumber == serial }) {
            await testMiner(ip: m.ipAddress)
        }
    }

    func toggleLED(_ miner: Miner) {
        Task {
            try? await api.flashLED(minerId: miner.id, blink: true)
        }
    }

    func markReady(_ miner: Miner) {
        Task {
            try? await api.markReady(minerId: miner.id)
            await loadMiners()
        }
    }

    func markParts(_ miner: Miner) {
        Task {
            try? await api.markPartsUnit(minerId: miner.id)
            await loadMiners()
        }
    }

    func printLabel(_ miner: Miner) {
        Task {
            do {
                try await api.printLabel(minerId: miner.id)
            } catch {
                showError(error)
            }
        }
    }

    func controlFan(_ miner: Miner, mode: String) {
        Task {
            try? await api.controlFans(minerId: miner.id, mode: mode)
        }
    }

    private func showError(_ error: Error) {
        errorMessage = error.localizedDescription
        showError = true
    }
}

// MARK: - Supporting Views

struct EmptyTestBenchView: View {
    let onScan: () -> Void

    var body: some View {
        VStack(spacing: 24) {
            Spacer()
            Image(systemName: "cpu")
                .font(.system(size: 64))
                .foregroundColor(.secondary)
            Text("No Miners Tested")
                .font(.title2)
                .fontWeight(.semibold)
            Text("Tap Scan to test a miner on the bench,\nor add one manually.")
                .multilineTextAlignment(.center)
                .foregroundColor(.secondary)
            Button(action: onScan) {
                Label("Scan Miner", systemImage: "barcode.viewfinder")
                    .font(.headline)
                    .padding()
                    .background(Color.blue)
                    .foregroundColor(.white)
                    .cornerRadius(12)
            }
            Spacer()
        }
        .padding()
    }
}

struct ConnectionBanner: View {
    @ObservedObject var ws = WebSocketClient.shared

    var body: some View {
        HStack(spacing: 6) {
            Circle()
                .fill(ws.connectionState == .connected ? Color.green : Color.red)
                .frame(width: 8, height: 8)
            Text(ws.connectionState == .connected ? "Live" : "Offline")
                .font(.caption)
                .foregroundColor(ws.connectionState == .connected ? .green : .secondary)
            Spacer()
        }
        .padding(.horizontal)
        .padding(.vertical, 6)
        .background(Color(.systemBackground))
    }
}

struct ManualIPSheet: View {
    let onSubmit: (String) -> Void
    @State private var ip = ""
    @Environment(\.dismiss) var dismiss

    var body: some View {
        NavigationView {
            Form {
                Section("Miner IP Address") {
                    TextField("192.168.1.xx", text: $ip)
                        .keyboardType(.decimalPad)
                }
                Section {
                    Button("Test Miner") {
                        if !ip.isEmpty {
                            onSubmit(ip)
                            dismiss()
                        }
                    }
                    .disabled(ip.isEmpty)
                }
            }
            .navigationTitle("Manual Entry")
            .navigationBarItems(trailing: Button("Cancel") { dismiss() })
        }
    }
}

struct SettingsView: View {
    @AppStorage("serverURL") var serverURL = "http://192.168.1.100:8000"
    @AppStorage("minerPassword") var minerPassword = "root"
    @Environment(\.dismiss) var dismiss

    var body: some View {
        NavigationView {
            Form {
                Section("Server") {
                    TextField("http://192.168.1.100:8000", text: $serverURL)
                        .keyboardType(.URL)
                        .autocapitalization(.none)
                }
                Section("Miner Credentials") {
                    SecureField("Password (default: root)", text: $minerPassword)
                }
                Section {
                    Button("Connect WebSocket") {
                        WebSocketClient.shared.connect(serverURL: serverURL)
                    }
                }
            }
            .navigationTitle("Settings")
            .navigationBarItems(trailing: Button("Done") { dismiss() })
        }
    }
}

// MARK: - Preview

#Preview {
    TestBenchView()
}
