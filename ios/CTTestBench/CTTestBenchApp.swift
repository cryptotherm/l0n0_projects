import SwiftUI

@main
struct CTTestBenchApp: App {
    @StateObject private var wsClient = WebSocketClient.shared

    var body: some Scene {
        WindowGroup {
            ContentView()
                .onAppear {
                    wsClient.connect()
                }
        }
    }
}

struct ContentView: View {
    var body: some View {
        TabView {
            TestBenchView()
                .tabItem {
                    Label("Test Bench", systemImage: "cpu")
                }

            SiteView()
                .tabItem {
                    Label("Sites", systemImage: "building.2")
                }
        }
    }
}
