import Foundation
import CoreBluetooth
import Darwin

// Transport only: notification boundaries and bytes are passed through unchanged.
let outputLock = NSLock()
func emit(_ object: [String: Any]) {
    outputLock.lock()
    defer { outputLock.unlock() }
    if let data = try? JSONSerialization.data(withJSONObject: object, options: [.sortedKeys]) {
        FileHandle.standardOutput.write(data + Data([10]))
    }
}

func decodeHex(_ value: String) -> Data? {
    let bytes = Array(value.utf8)
    guard !bytes.isEmpty, bytes.count % 2 == 0 else { return nil }
    func nibble(_ b: UInt8) -> UInt8? {
        switch b {
        case 48...57: return b - 48
        case 65...70: return b - 55
        case 97...102: return b - 87
        default: return nil
        }
    }
    var result = Data()
    for i in stride(from: 0, to: bytes.count, by: 2) {
        guard let a = nibble(bytes[i]), let b = nibble(bytes[i + 1]) else { return nil }
        result.append(a * 16 + b)
    }
    return result
}
func validFrame(_ data: Data, _ maximum: Int) -> Bool {
    return maximum >= 58 && !data.isEmpty && data.count <= maximum
}

final class Bridge: NSObject, CBCentralManagerDelegate, CBPeripheralDelegate {
    let serviceID = CBUUID(string: "e7810a71-73ae-499d-8c15-faa9aef0c3f2")
    let scanSeconds: Double?
    let deviceID: UUID?
    var central: CBCentralManager!
    var peripheral: CBPeripheral?
    var characteristic: CBCharacteristic?
    var deadline: DispatchWorkItem?
    var idleDeadline: DispatchWorkItem?
    var signalSource: DispatchSourceSignal?
    var started = false
    var ready = false
    var stdinClosed = false
    var finished = false
    var maxWrite = 0
    var pending: [(Int, Data)] = []
    var pumpScheduled = false
    var lastWrite = DispatchTime(uptimeNanoseconds: 0)
    var outstandingIDs = Set<Int>()

    init(scanSeconds: Double?, deviceID: UUID?) {
        self.scanSeconds = scanSeconds
        self.deviceID = deviceID
    }
    func start() {
        signal(SIGTERM, SIG_IGN)
        let source = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .main)
        source.setEventHandler { [weak self] in self?.finish(0) }
        source.resume()
        signalSource = source
        armDeadline(15, "Bluetooth initialization timed out")
        central = CBCentralManager(delegate: self, queue: .main)
    }
    func armDeadline(_ seconds: Double, _ message: String) {
        deadline?.cancel()
        let work = DispatchWorkItem { [weak self] in self?.fail(message) }
        deadline = work
        DispatchQueue.main.asyncAfter(deadline: .now() + seconds, execute: work)
    }
    func touch() {
        idleDeadline?.cancel()
        let work = DispatchWorkItem { [weak self] in self?.fail("Bridge idle timeout (120 seconds)") }
        idleDeadline = work
        DispatchQueue.main.asyncAfter(deadline: .now() + 120, execute: work)
    }
    func finish(_ code: Int32) {
        guard !finished else { return }
        finished = true
        deadline?.cancel()
        idleDeadline?.cancel()
        if central != nil {
            if central.isScanning { central.stopScan() }
            if let p = peripheral { central.cancelPeripheralConnection(p) }
        }
        exit(code)
    }
    func fail(_ message: String) {
        emit(["event": "error", "message": message])
        finish(1)
    }
    func centralManagerDidUpdateState(_ central: CBCentralManager) {
        switch central.state {
        case .poweredOn:
            guard !started else { return }
            started = true
            deadline?.cancel()
            if let seconds = scanSeconds {
                central.scanForPeripherals(withServices: nil, options: [CBCentralManagerScanOptionAllowDuplicatesKey: false])
                let work = DispatchWorkItem { [weak self] in
                    emit(["event": "scan_complete"])
                    self?.finish(0)
                }
                deadline = work
                DispatchQueue.main.asyncAfter(deadline: .now() + seconds, execute: work)
            } else if let id = deviceID {
                guard let p = central.retrievePeripherals(withIdentifiers: [id]).first else {
                    fail("Device UUID is not available to CoreBluetooth; scan first")
                    return
                }
                peripheral = p
                p.delegate = self
                armDeadline(15, "Connection/service/notification setup timed out")
                central.connect(p, options: nil)
            }
        case .unauthorized: fail("Bluetooth authorization denied; allow NIIMBOT BLE Bridge in macOS Privacy settings")
        case .unsupported: fail("Bluetooth unsupported")
        case .poweredOff: fail("Bluetooth powered off")
        case .resetting:
            if started { fail("Bluetooth resetting") }
        case .unknown: break
        @unknown default: fail("Unknown Bluetooth state")
        }
    }
    func centralManager(_ central: CBCentralManager, didDiscover p: CBPeripheral, advertisementData: [String: Any], rssi RSSI: NSNumber) {
        let name = advertisementData[CBAdvertisementDataLocalNameKey] as? String ?? p.name ?? ""
        let upper = name.uppercased()
        guard upper.contains("B1") || upper.contains("NIIMBOT") else { return }
        emit(["event": "discovered", "device": p.identifier.uuidString, "name": name, "rssi": RSSI])
    }
    func centralManager(_ central: CBCentralManager, didConnect p: CBPeripheral) {
        p.discoverServices([serviceID])
    }
    func centralManager(_ central: CBCentralManager, didFailToConnect p: CBPeripheral, error: Error?) {
        fail("Connect failed: \(error?.localizedDescription ?? "unknown error")")
    }
    func centralManager(_ central: CBCentralManager, didDisconnectPeripheral p: CBPeripheral, error: Error?) {
        fail("Disconnected: \(error?.localizedDescription ?? "remote disconnect")")
    }
    func peripheral(_ p: CBPeripheral, didDiscoverServices error: Error?) {
        if let error = error { fail(error.localizedDescription); return }
        guard let service = p.services?.first(where: { $0.uuid == serviceID }) else {
            fail("Required service not found"); return
        }
        p.discoverCharacteristics(nil, for: service)
    }
    func peripheral(_ p: CBPeripheral, didDiscoverCharacteristicsFor service: CBService, error: Error?) {
        if let error = error { fail(error.localizedDescription); return }
        let candidates = (service.characteristics ?? []).filter {
            $0.uuid == CBUUID(string: "bef8d6c9-9c21-4c9e-b632-bd58c1009f9f") &&
                $0.properties.contains(.writeWithoutResponse) && $0.properties.contains(.notify)
        }
        guard candidates.count == 1, let c = candidates.first else {
            fail("Expected B1 Pro characteristic supporting notify and write without response; found \(candidates.count)"); return
        }
        characteristic = c
        maxWrite = p.maximumWriteValueLength(for: .withoutResponse)
        guard maxWrite >= 58 else { fail("Maximum write length below required 58 bytes"); return }
        p.setNotifyValue(true, for: c)
    }
    func peripheral(_ p: CBPeripheral, didUpdateNotificationStateFor c: CBCharacteristic, error: Error?) {
        if let error = error { fail(error.localizedDescription); return }
        guard c === characteristic, c.isNotifying else { fail("Notifications unavailable"); return }
        guard !ready else { return }
        ready = true
        deadline?.cancel()
        deadline = nil
        touch()
        emit(["event": "ready", "device": p.identifier.uuidString, "name": p.name ?? "", "service": serviceID.uuidString, "characteristic": c.uuid.uuidString, "max_write": maxWrite, "notify": true, "write_without_response": true, "write_mode": "without_response"])
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            while let line = readLine() {
                DispatchQueue.main.async { self?.receive(line) }
            }
            DispatchQueue.main.async {
                guard let self = self else { return }
                self.stdinClosed = true
                if self.pending.isEmpty { self.finish(0) }
            }
        }
    }
    func receive(_ line: String) {
        guard ready, !finished else { return }
        guard line.utf8.count <= 131072,
              let raw = line.data(using: .utf8),
              let object = try? JSONSerialization.jsonObject(with: raw) as? [String: Any],
              object["op"] as? String == "write",
              let number = object["id"] as? NSNumber,
              CFGetTypeID(number) != CFBooleanGetTypeID(),
              let id = Int(number.stringValue),
              let hex = object["hex"] as? String,
              let data = decodeHex(hex) else {
            fail("Invalid write request: expected op=write, integer id and nonempty even-length hexadecimal hex"); return
        }
        guard validFrame(data, maxWrite) else {
            fail("Write exceeds maximum whole-frame length; frames are never split"); return
        }
        guard pending.count < 64, !outstandingIDs.contains(id) else {
            fail("Write queue full or duplicate outstanding id"); return
        }
        outstandingIDs.insert(id)
        pending.append((id, data))
        touch()
        pump()
    }
    func pump() {
        guard !finished, !pumpScheduled, let p = peripheral, let c = characteristic else { return }
        guard !pending.isEmpty else {
            deadline?.cancel()
            if stdinClosed { finish(0) }
            return
        }
        guard p.canSendWriteWithoutResponse else {
            if deadline == nil { armDeadline(15, "Write flow control timed out") }
            return
        }
        let earliest = lastWrite.uptimeNanoseconds + 10_000_000
        if DispatchTime.now().uptimeNanoseconds < earliest {
            pumpScheduled = true
            DispatchQueue.main.asyncAfter(deadline: DispatchTime(uptimeNanoseconds: earliest)) { [weak self] in
                self?.pumpScheduled = false
                self?.pump()
            }
            return
        }
        deadline?.cancel()
        deadline = nil
        let request = pending.removeFirst()
        p.writeValue(request.1, for: c, type: .withoutResponse)
        lastWrite = .now()
        outstandingIDs.remove(request.0)
        // Local CoreBluetooth queue acceptance only; no radio or printer ACK is implied.
        emit(["event": "written", "id": request.0])
        touch()
        pump()
    }
    func peripheralIsReady(toSendWriteWithoutResponse p: CBPeripheral) {
        deadline?.cancel()
        deadline = nil
        pump()
    }
    func peripheral(_ p: CBPeripheral, didUpdateValueFor c: CBCharacteristic, error: Error?) {
        if let error = error { fail(error.localizedDescription); return }
        guard c === characteristic, let data = c.value else { return }
        touch()
        emit(["event": "data", "hex": data.map { String(format: "%02x", $0) }.joined()])
    }
}

var args = Array(CommandLine.arguments.dropFirst())
if let index = args.firstIndex(of: "--pid-file") {
    guard index + 1 < args.count, args[index + 1].hasPrefix("/"),
          args.filter({ $0 == "--pid-file" }).count == 1 else {
        emit(["event": "error", "message": "--pid-file requires one absolute path"])
        exit(2)
    }
    let path = args[index + 1]
    let fd = open(path, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW, S_IRUSR | S_IWUSR)
    guard fd >= 0 else {
        emit(["event": "error", "message": "Cannot create private pid file"])
        exit(2)
    }
    let bytes = Array("\(getpid())\n".utf8)
    let count = bytes.withUnsafeBytes { Darwin.write(fd, $0.baseAddress!, $0.count) }
    close(fd)
    guard count == bytes.count else { exit(2) }
    args.removeSubrange(index...(index + 1))
}
if args == ["--self-test"] {
    // This path must remain free of CBCentralManager construction or radio activity.
    let info = Bundle.main.infoDictionary ?? [:]
    let id = info["CFBundleIdentifier"] as? String ?? ""
    let usage = info["NSBluetoothAlwaysUsageDescription"] as? String ?? ""
    let fixture = decodeHex("00aBff0102")
    let ok = id == "org.niimbot-ai-skill.ble-bridge" && !usage.isEmpty &&
        fixture == Data([0, 171, 255, 1, 2]) && validFrame(Data(repeating: 1, count: 58), 58) &&
        !validFrame(Data(repeating: 1, count: 59), 58) && !validFrame(Data([1]), 57) &&
        decodeHex("0") == nil && decodeHex("gg") == nil && decodeHex("") == nil && decodeHex("00 1") == nil
    emit(["event": "self_test", "ok": ok, "bundle_id": id, "usage_description": usage, "hex_and_whole_frame_tests": ok, "write_mode": "without_response", "never_split": true])
    exit(ok ? 0 : 1)
}
var scanSeconds: Double?
var deviceID: UUID?
if args.first == "scan" {
    if args.count == 1 { scanSeconds = 8 }
    else if args.count == 3 && args[1] == "--seconds" { scanSeconds = Double(args[2]) }
    else if args.count == 2 && args[1].hasPrefix("--seconds") { scanSeconds = Double(args[1].dropFirst(9)) }
} else if args.count == 3 && args[0] == "bridge" && args[1] == "--device" {
    deviceID = UUID(uuidString: args[2])
}
guard (scanSeconds != nil && scanSeconds!.isFinite && scanSeconds! > 0 && scanSeconds! <= 15) || deviceID != nil else {
    emit(["event": "error", "message": "Usage: --self-test | scan [--seconds 8] (0 < seconds <= 15) | bridge --device UUID"])
    exit(2)
}
let bridge = Bridge(scanSeconds: scanSeconds, deviceID: deviceID)
bridge.start()
RunLoop.main.run()
