import SwiftUI
import AVFoundation
import Vision

// MARK: - Scanner View (Barcode + OCR)

struct ScannerView: View {
    @Environment(\.dismiss) var dismiss
    let onScanned: (ScanResult) -> Void

    @State private var showCamera = true

    var body: some View {
        NavigationView {
            ZStack {
                CameraPreviewWrapper(onScanned: { result in
                    onScanned(result)
                    dismiss()
                })
                .ignoresSafeArea()

                VStack {
                    Spacer()

                    // Scan frame
                    RoundedRectangle(cornerRadius: 16)
                        .stroke(Color.white, lineWidth: 3)
                        .frame(width: 280, height: 180)
                        .shadow(color: .white.opacity(0.3), radius: 8)

                    Text("Scan barcode or label photo")
                        .font(.subheadline)
                        .foregroundColor(.white)
                        .padding(.top, 12)
                        .shadow(radius: 2)

                    Spacer()

                    Button("Enter IP Manually") {
                        dismiss()
                    }
                    .foregroundColor(.white)
                    .padding()
                    .background(Color.black.opacity(0.5))
                    .cornerRadius(10)
                    .padding(.bottom, 40)
                }
            }
            .navigationTitle("Scan Miner")
            .navigationBarTitleDisplayMode(.inline)
            .navigationBarItems(leading: Button("Cancel") { dismiss() }
                .foregroundColor(.white))
        }
    }
}

struct ScanResult {
    var barcode: String?
    var ipAddress: String?
    var serialNumber: String?
    var model: String?
}

// MARK: - Camera Preview + Vision

struct CameraPreviewWrapper: UIViewControllerRepresentable {
    let onScanned: (ScanResult) -> Void

    func makeUIViewController(context: Context) -> CameraViewController {
        let vc = CameraViewController()
        vc.onScanned = onScanned
        return vc
    }

    func updateUIViewController(_ uiViewController: CameraViewController, context: Context) {}
}

class CameraViewController: UIViewController, AVCaptureVideoDataOutputSampleBufferDelegate {
    var onScanned: ((ScanResult) -> Void)?

    private var captureSession: AVCaptureSession?
    private var previewLayer: AVCaptureVideoPreviewLayer?
    private var isProcessing = false
    private var barcodeRequest: VNDetectBarcodesRequest?

    override func viewDidLoad() {
        super.viewDidLoad()
        setupCamera()
        setupVision()
    }

    override func viewDidLayoutSubviews() {
        super.viewDidLayoutSubviews()
        previewLayer?.frame = view.bounds
    }

    private func setupCamera() {
        let session = AVCaptureSession()
        session.sessionPreset = .high

        guard let device = AVCaptureDevice.default(.builtInWideAngleCamera, for: .video, position: .back),
              let input = try? AVCaptureDeviceInput(device: device) else { return }

        session.addInput(input)

        let output = AVCaptureVideoDataOutput()
        output.setSampleBufferDelegate(self, queue: DispatchQueue(label: "camera.queue"))
        session.addOutput(output)

        let preview = AVCaptureVideoPreviewLayer(session: session)
        preview.videoGravity = .resizeAspectFill
        preview.frame = view.bounds
        view.layer.addSublayer(preview)
        previewLayer = preview

        captureSession = session
        DispatchQueue.global(qos: .userInitiated).async {
            session.startRunning()
        }
    }

    private func setupVision() {
        barcodeRequest = VNDetectBarcodesRequest { [weak self] request, error in
            guard let results = request.results as? [VNBarcodeObservation],
                  let first = results.first,
                  let payload = first.payloadStringValue else { return }
            self?.handleBarcode(payload)
        }
    }

    func captureOutput(_ output: AVCaptureOutput, didOutput sampleBuffer: CMSampleBuffer, from connection: AVCaptureConnection) {
        guard !isProcessing else { return }
        guard let pixelBuffer = CMSampleBufferGetImageBuffer(sampleBuffer) else { return }
        isProcessing = true

        let handler = VNImageRequestHandler(cvPixelBuffer: pixelBuffer, options: [:])
        try? handler.perform([barcodeRequest!])

        DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) {
            self.isProcessing = false
        }
    }

    private func handleBarcode(_ payload: String) {
        captureSession?.stopRunning()

        // Parse CRYPTOTHERM QR format: "CRYPTOTHERM|serial|model|mac"
        var result = ScanResult(barcode: payload)

        if payload.hasPrefix("CRYPTOTHERM|") {
            let parts = payload.split(separator: "|")
            if parts.count >= 2 { result.serialNumber = String(parts[1]) }
            if parts.count >= 3 { result.model = String(parts[2]) }
        } else if payload.contains(".") {
            // Might be an IP address
            result.ipAddress = payload
        } else {
            result.serialNumber = payload
        }

        DispatchQueue.main.async {
            self.onScanned?(result)
        }
    }

    override func viewWillDisappear(_ animated: Bool) {
        super.viewWillDisappear(animated)
        captureSession?.stopRunning()
    }
}
