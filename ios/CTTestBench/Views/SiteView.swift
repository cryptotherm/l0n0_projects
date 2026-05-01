import SwiftUI

// MARK: - Site View

struct SiteView: View {
    @StateObject private var vm = SiteViewModel()
    @State private var showCreateSite = false

    var body: some View {
        NavigationView {
            Group {
                if vm.sites.isEmpty && !vm.isLoading {
                    VStack(spacing: 16) {
                        Image(systemName: "building.2")
                            .font(.system(size: 60))
                            .foregroundColor(.secondary)
                        Text("No Sites")
                            .font(.title2)
                        Button("Add Site") { showCreateSite = true }
                            .buttonStyle(.borderedProminent)
                    }
                } else {
                    List {
                        ForEach(vm.sites) { site in
                            NavigationLink(destination: SiteDetailView(site: site)) {
                                SiteRow(site: site)
                            }
                        }
                    }
                }
            }
            .navigationTitle("Sites")
            .toolbar {
                ToolbarItem(placement: .navigationBarTrailing) {
                    Button { showCreateSite = true } label: {
                        Image(systemName: "plus")
                    }
                }
            }
            .sheet(isPresented: $showCreateSite) {
                CreateSiteSheet { name, location in
                    Task { await vm.createSite(name: name, location: location) }
                }
            }
            .task {
                await vm.loadSites()
            }
        }
    }
}

struct SiteRow: View {
    let site: Site

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(site.name)
                .font(.headline)
            if let loc = site.location {
                Text(loc)
                    .font(.caption)
                    .foregroundColor(.secondary)
            }
            Text("\(site.racks.count) rack(s)")
                .font(.caption2)
                .foregroundColor(.secondary)
        }
        .padding(.vertical, 4)
    }
}

// MARK: - Site Detail / Rack List

struct SiteDetailView: View {
    let site: Site
    @StateObject private var vm: RackViewModel
    @State private var showAddRack = false

    init(site: Site) {
        self.site = site
        self._vm = StateObject(wrappedValue: RackViewModel(site: site))
    }

    var body: some View {
        List {
            Section("Racks") {
                ForEach(vm.racks) { rack in
                    NavigationLink(destination: RackView(site: site, rack: rack)) {
                        RackRow(rack: rack)
                    }
                }
            }
        }
        .navigationTitle(site.name)
        .toolbar {
            ToolbarItem(placement: .navigationBarTrailing) {
                Button { showAddRack = true } label: {
                    Image(systemName: "plus")
                }
            }
        }
        .sheet(isPresented: $showAddRack) {
            CreateRackSheet(siteId: site.id) { rack in
                vm.racks.append(rack)
            }
        }
        .task {
            vm.racks = site.racks
        }
    }
}

struct RackRow: View {
    let rack: Rack

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(rack.name)
                .font(.headline)
            if let cidr = rack.ipCidr {
                Text(cidr)
                    .font(.caption)
                    .foregroundColor(.secondary)
                    .monospacedDigit()
            } else if let start = rack.ipRangeStart, let end = rack.ipRangeEnd {
                Text("\(start) – \(end)")
                    .font(.caption)
                    .foregroundColor(.secondary)
                    .monospacedDigit()
            }
        }
        .padding(.vertical, 2)
    }
}

// MARK: - Rack View

struct RackView: View {
    let site: Site
    let rack: Rack
    @StateObject private var vm: RackDetailViewModel

    init(site: Site, rack: Rack) {
        self.site = site
        self.rack = rack
        self._vm = StateObject(wrappedValue: RackDetailViewModel(siteId: site.id, rackId: rack.id))
    }

    var body: some View {
        List {
            // ── Summary section ───────────────────────────────────────────────
            Section("Status") {
                if let resp = vm.rackMiners {
                    HStack {
                        VStack {
                            Text("\(resp.total)").font(.title).bold()
                            Text("Total").font(.caption).foregroundColor(.secondary)
                        }
                        Spacer()
                        VStack {
                            Text("\(resp.readyToDeploy)").font(.title).bold().foregroundColor(.green)
                            Text("Ready").font(.caption).foregroundColor(.secondary)
                        }
                        Spacer()
                        VStack {
                            Text("\(resp.needsSwap)").font(.title).bold().foregroundColor(.orange)
                            Text("Swap").font(.caption).foregroundColor(.secondary)
                        }
                    }
                    .padding(.vertical, 4)
                }
            }

            // ── Actions ───────────────────────────────────────────────────────
            Section("Actions") {
                Button {
                    Task { await vm.scanRack() }
                } label: {
                    Label("Scan All Miners", systemImage: "wifi")
                }

                Button {
                    Task { await vm.identifyNeedingSwap() }
                } label: {
                    Label("Flash Swap Miners", systemImage: "flashlight.on.fill")
                }
                    .foregroundColor(.orange)
            }

            // ── Swap suggestions ──────────────────────────────────────────────
            if let suggestions = vm.rackMiners?.swapSuggestions, !suggestions.isEmpty {
                Section("Needs Swap (\(suggestions.count))") {
                    ForEach(suggestions) { s in
                        VStack(alignment: .leading, spacing: 4) {
                            HStack {
                                Text(s.model ?? "Unknown")
                                    .font(.subheadline)
                                    .fontWeight(.medium)
                                Spacer()
                                Text(s.status.replacingOccurrences(of: "_", with: " ").capitalized)
                                    .font(.caption)
                                    .foregroundColor(.orange)
                            }
                            Text(s.ipAddress)
                                .font(.caption)
                                .foregroundColor(.secondary)
                                .monospacedDigit()
                            if let summary = s.aiSummary {
                                Text(summary)
                                    .font(.caption2)
                                    .foregroundColor(.secondary)
                                    .lineLimit(2)
                            }
                        }
                        .padding(.vertical, 2)
                    }
                }
            }
        }
        .navigationTitle(rack.name)
        .task {
            await vm.loadRackMiners()
        }
        .overlay {
            if vm.isLoading {
                ProgressView("Scanning...")
                    .padding()
                    .background(.regularMaterial)
                    .cornerRadius(12)
            }
        }
    }
}

// MARK: - View Models

@MainActor
class SiteViewModel: ObservableObject {
    @Published var sites: [Site] = []
    @Published var isLoading = false

    func loadSites() async {
        isLoading = true
        defer { isLoading = false }
        do {
            sites = try await APIClient.shared.listSites()
        } catch {}
    }

    func createSite(name: String, location: String?) async {
        do {
            let site = try await APIClient.shared.createSite(name: name, location: location)
            sites.append(site)
        } catch {}
    }
}

@MainActor
class RackViewModel: ObservableObject {
    @Published var racks: [Rack] = []
    let site: Site
    init(site: Site) { self.site = site }
}

@MainActor
class RackDetailViewModel: ObservableObject {
    @Published var rackMiners: APIClient.RackMinersResponse?
    @Published var isLoading = false

    let siteId: Int
    let rackId: Int

    init(siteId: Int, rackId: Int) {
        self.siteId = siteId
        self.rackId = rackId
    }

    func loadRackMiners() async {
        isLoading = true
        defer { isLoading = false }
        do {
            rackMiners = try await APIClient.shared.rackMiners(siteId: siteId, rackId: rackId)
        } catch {}
    }

    func scanRack() async {
        isLoading = true
        defer { isLoading = false }
        do {
            try await APIClient.shared.scanRack(siteId: siteId, rackId: rackId, runTests: true)
            await loadRackMiners()
        } catch {}
    }

    func identifyNeedingSwap() async {
        try? await APIClient.shared.identifyRack(
            siteId: siteId, rackId: rackId, statusFilter: "needs_repair"
        )
    }
}

// MARK: - Create Site/Rack Sheets

struct CreateSiteSheet: View {
    let onCreate: (String, String?) -> Void
    @State private var name = ""
    @State private var location = ""
    @Environment(\.dismiss) var dismiss

    var body: some View {
        NavigationView {
            Form {
                TextField("Site Name", text: $name)
                TextField("Location (optional)", text: $location)
            }
            .navigationTitle("New Site")
            .navigationBarItems(
                leading: Button("Cancel") { dismiss() },
                trailing: Button("Create") {
                    onCreate(name, location.isEmpty ? nil : location)
                    dismiss()
                }.disabled(name.isEmpty)
            )
        }
    }
}

struct CreateRackSheet: View {
    let siteId: Int
    let onCreate: (Rack) -> Void
    @State private var name = ""
    @State private var cidr = ""
    @State private var rangeStart = ""
    @State private var rangeEnd = ""
    @State private var useCIDR = true
    @Environment(\.dismiss) var dismiss

    var body: some View {
        NavigationView {
            Form {
                Section("Rack Info") {
                    TextField("Rack Name (e.g. Row-A-1)", text: $name)
                }
                Section("IP Range") {
                    Picker("Mode", selection: $useCIDR) {
                        Text("CIDR").tag(true)
                        Text("Range").tag(false)
                    }.pickerStyle(.segmented)

                    if useCIDR {
                        TextField("e.g. 192.168.1.0/24", text: $cidr)
                            .keyboardType(.asciiCapable)
                            .autocapitalization(.none)
                    } else {
                        TextField("Start IP", text: $rangeStart).keyboardType(.decimalPad)
                        TextField("End IP", text: $rangeEnd).keyboardType(.decimalPad)
                    }
                }
            }
            .navigationTitle("New Rack")
            .navigationBarItems(
                leading: Button("Cancel") { dismiss() },
                trailing: Button("Create") {
                    Task {
                        do {
                            struct Body: Encodable {
                                let name: String
                                let ip_cidr: String?
                                let ip_range_start: String?
                                let ip_range_end: String?
                            }
                            let body = Body(
                                name: name,
                                ip_cidr: useCIDR ? cidr : nil,
                                ip_range_start: useCIDR ? nil : rangeStart,
                                ip_range_end: useCIDR ? nil : rangeEnd
                            )
                            let rack: Rack = try await APIClient.shared.post("/api/sites/\(siteId)/racks", body: body)
                            onCreate(rack)
                            dismiss()
                        } catch {}
                    }
                }.disabled(name.isEmpty)
            )
        }
    }
}

#Preview {
    SiteView()
}
