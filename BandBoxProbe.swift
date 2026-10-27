import Foundation
import CoreBluetooth
import IOBluetooth

// Default modes are read-only. A separate explicit, short-lived plan enables
// one guarded Drive decrement/restore test; no arbitrary packet interface.
func arg(_ name: String) -> String? {
    guard let i = CommandLine.arguments.firstIndex(of: name), i + 1 < CommandLine.arguments.count else { return nil }
    return CommandLine.arguments[i + 1]
}
// Discovery only. The Solo advertises/reports as "JBL BandBox Solo" and has
// been confirmed to answer channel-select guitar-preset reads (preset_controls
// accepts channel 1 or 3) -- see docs/protocol.md#bandbox-solo. Everything
// else in this repo (effects, mixer, looper, save/duplicate/rename) still
// hardcodes channel 3 in controls.py/DriveRoundTrip.swift and has not been
// adapted or tested against a Solo; connecting one will let you read presets
// but other operations are unverified and may behave incorrectly.
func isSupportedBandBoxName(_ name: String?) -> Bool {
    name == "JBL BandBox Trio" || name == "JBL BandBox Solo"
}
let seconds = Double(arg("--seconds") ?? "15") ?? -1
let listenOnly = CommandLine.arguments.contains("--listen")
let readState = CommandLine.arguments.contains("--read-state")
let readPresets = CommandLine.arguments.contains("--read-presets")
let readCurrent = CommandLine.arguments.contains("--read-current")
let readCurrentChannel = UInt8(arg("--channel") ?? "3") ?? 0
let driveTestRequested = arg("--drive-test-plan") != nil
let driveSetRequested = arg("--set-drive-plan") != nil
let driveControlRequested = driveTestRequested || driveSetRequested
let controlRequested = arg("--control-plan") != nil
let queryRequested = readState || readPresets || readCurrent || driveControlRequested || controlRequested
let maximumSeconds = listenOnly ? 300.0 : 60.0
guard (1...maximumSeconds).contains(seconds), (!driveControlRequested || seconds >= 45), (!controlRequested || seconds >= 12), [listenOnly, readState, readPresets, readCurrent, driveTestRequested, driveSetRequested, controlRequested].filter({ $0 }).count <= 1, !CommandLine.arguments.contains("--query-version"),
      arg("--id") == nil || UUID(uuidString: arg("--id")!) != nil,
      !readCurrent || readCurrentChannel == 1 || readCurrentChannel == 3 else {
    fputs("Usage: bandbox-probe [--id UUID] [--seconds 1..60 (1..300 for --listen)] [--read-state|--read-presets|--read-current [--channel 1|3]|--listen|--drive-test-plan FILE|--set-drive-plan FILE (45..60 seconds)]\n", stderr)
    exit(2)
}
guard !queryRequested || arg("--id") != nil else {
    fputs("State queries require a previously inspected --id.\n", stderr)
    exit(2)
}

func emit(_ event: String, _ fields: [String: Any] = [:]) {
    var object = fields
    object["event"] = event
    let timestamp = ISO8601DateFormatter()
    timestamp.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    object["time"] = timestamp.string(from: Date())
    if let bytes = try? JSONSerialization.data(withJSONObject: object, options: [.sortedKeys]),
       let string = String(data: bytes, encoding: .utf8) {
        print(string)
        fflush(stdout)
    }
}
func hex(_ data: Data) -> String { data.map { String(format: "%02X", $0) }.joined() }

let configuredControl: (ControlRequest, Data)? = {
    guard let path = arg("--control-plan") else { return nil }
    do {
        let request = try JSONDecoder().decode(ControlRequest.self, from: Data(contentsOf: URL(fileURLWithPath: path)))
        return (request, try request.packet(identifier: arg("--id")!, now: Date().timeIntervalSince1970))
    } catch {
        fputs("Unsupported, unsafe, stale or mismatched control request. No connection attempted.\n", stderr)
        exit(2)
    }
}()

struct DriveTestFile: Decodable {
    let device_uuid: String
    let created_at: Double
    let channel: UInt8
    let preset_id: UInt8
    let slot_id: UInt8
    let algorithm_id: UInt8
    let config_id: UInt8
    let bypass_raw: UInt8
    let original_float_hex: String
    let displayed_drive: Int
    let displayed_amp_model: String
    let displayed_amp_active: Bool
    let confirmed_recovery_drive: Int?
    let requested_drive: Int?
}
let configuredDriveTest: DriveRoundTrip? = {
    guard let path = arg("--drive-test-plan") ?? arg("--set-drive-plan") else { return nil }
    do {
        let planFile = try JSONDecoder().decode(DriveTestFile.self, from: Data(contentsOf: URL(fileURLWithPath: path)))
        let models = ["American Clean", "Jazz Sound", "Yellow Tweed", "UK Crunch", "British Lead", "Vintage Drive", "American Gain Stack", "US Metal", "Bass Amp"]
        let age = Date().timeIntervalSince1970 - planFile.created_at
        guard driveSetRequested == (planFile.requested_drive != nil),
              !driveSetRequested || planFile.confirmed_recovery_drive == nil,
              planFile.device_uuid.uppercased() == arg("--id")?.uppercased(),
              age.isFinite, (0...120).contains(age), planFile.preset_id > 0,
              planFile.channel == 1 || planFile.channel == 3,
              planFile.slot_id == 3, planFile.algorithm_id == 6, planFile.config_id < models.count,
              planFile.displayed_amp_model == models[Int(planFile.config_id)],
              planFile.bypass_raw <= 1, planFile.displayed_amp_active == (planFile.bypass_raw == 0),
              planFile.original_float_hex.count == 8 else { throw DriveRoundTrip.Failure.invalidPlan }
        let chars = Array(planFile.original_float_hex)
        var bytes = [UInt8]()
        for i in stride(from: 0, to: 8, by: 2) {
            guard let byte = UInt8(String(chars[i...i+1]), radix: 16) else { throw DriveRoundTrip.Failure.invalidPlan }
            bytes.append(byte)
        }
        return try DriveRoundTrip(plan: .init(channel: planFile.channel, presetID: planFile.preset_id, slotID: planFile.slot_id,
            algorithmID: planFile.algorithm_id, configID: planFile.config_id, bypass: planFile.bypass_raw,
            originalFloatBytes: Data(bytes), displayedDrive: planFile.displayed_drive, liveMetadataConfirmed: true),
            confirmedRecoveryDrive: planFile.confirmed_recovery_drive, requestedDrive: planFile.requested_drive)
    } catch {
        fputs("Drive test plan is invalid, mismatched, or older than two minutes. No connection attempted.\n", stderr)
        exit(2)
    }
}()

final class Probe: NSObject, CBCentralManagerDelegate, CBPeripheralDelegate {
    var central: CBCentralManager!
    var target: CBPeripheral?
    var seen = Set<UUID>()
    var candidates = [UUID: CBPeripheral]()
    // The amp can advertise with no name and no manufacturer data (observed
    // on the Solo); these are tried, strongest RSSI first, only when no
    // named candidate was found -- see tools/bandbox_ctl.py's find_amp_client.
    var anonymousCandidates: [(peripheral: CBPeripheral, rssi: Int)] = []
    var probingAnonymous: CBPeripheral?
    var ignoredDisconnects = Set<UUID>()
    var done = false
    var tx: CBCharacteristic?
    var queried = false
    var connected = false
    var responseReceived = false
    var detailQueried = false
    var queriedPresetID: UInt8?
    var controlACK = false
    var controlReply = false
    let driveTest = configuredDriveTest
    var driveConnection = ""
    var driveTimer: Timer?
    var settingWrites = 0
    var driveOutcome: String?
    var driveSucceeded = false
    var startedAt: TimeInterval = 0
    let standardReads: Set<String> = ["2A19", "2A24", "2A26", "2A27", "2A28", "2A29"]
    func start() {
        startedAt = ProcessInfo.processInfo.systemUptime
        emit("authorization", ["value": CBManager.authorization.rawValue])
        central = CBCentralManager(delegate: self, queue: .main)
        if driveTest != nil {
            driveTimer = Timer.scheduledTimer(withTimeInterval: 0.1, repeats: true) { _ in
                guard !self.done else { return }
                self.driveTest?.tick(now: ProcessInfo.processInfo.systemUptime)
                self.drainDriveActions()
            }
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + seconds) { self.finish() }
    }
    func finish() {
        guard !done else { return }
        done = true
        driveTimer?.invalidate()
        if let driveTest, !driveTest.isFinished {
            driveTest.transportFailed(connectionID: driveConnection, reason: "Probe ended before verified operation result")
            for action in driveTest.actions() {
                if case let .finish(outcome, reason) = action { recordDriveFinish(outcome, reason) }
            }
        }
        central.stopScan()
        if let p = target { central.cancelPeripheralConnection(p) }
        if let p = probingAnonymous { central.cancelPeripheralConnection(p) }
        emit("complete", ["nearby_count": seen.count, "target_found": target != nil,
                         "connected": connected, "response_received": responseReceived, "setting_writes": settingWrites,
                         "drive_test_outcome": driveOutcome ?? NSNull()])
        CFRunLoopStop(CFRunLoopGetMain())
    }
    func recordDriveFinish(_ outcome: DriveRoundTrip.Outcome, _ reason: String) {
        driveOutcome = String(describing: outcome)
        driveSucceeded = outcome == .restoredDeviceReported || outcome == .changedDeviceReported
        if driveSucceeded { responseReceived = true }
        emit("drive_test_result", ["outcome": driveOutcome!, "reason": reason, "setting_writes": settingWrites,
                                  "restoration_device_reported": outcome == .restoredDeviceReported,
                                  "change_device_reported": outcome == .changedDeviceReported])
    }
    func drainDriveActions() {
        guard !done, let driveTest else { return }
        for action in driveTest.actions() {
            switch action {
            case let .send(id, packet, purpose, settingWrite):
                guard let target, target.state == .connected, let tx, tx.properties.contains(.write) else {
                    driveTest.transportFailed(connectionID: driveConnection, reason: "Control connection unavailable")
                    drainDriveActions(); return
                }
                emit("drive_test_send", ["action_id": id, "hex": hex(packet), "purpose": purpose, "setting_write": settingWrite])
                if settingWrite { settingWrites += 1 }
                target.writeValue(packet, for: tx, type: .withResponse)
                driveTest.sent(actionID: id, connectionID: driveConnection, now: ProcessInfo.processInfo.systemUptime)
            case let .finish(outcome, reason):
                recordDriveFinish(outcome, reason)
                finish()
            }
        }
    }
    func centralManagerDidUpdateState(_ central: CBCentralManager) {
        emit("bluetooth_state", ["value": central.state.rawValue,
                                "authorization": CBManager.authorization.rawValue])
        if central.state == .poweredOn {
            if arg("--id") == nil {
                let matches = central.retrieveConnectedPeripherals(withServices: [
                    CBUUID(string: "65786365-6C70-6F69-6E74-2E636F6D0000")
                ]).filter { isSupportedBandBoxName($0.name) }
                for peripheral in matches {
                    candidates[peripheral.identifier] = peripheral
                    emit("retrieved_connected_target", ["id": peripheral.identifier.uuidString, "name": peripheral.name ?? ""])
                }
                if matches.count > 1 {
                    emit("error", ["message": "Multiple connected BandBox devices; specify an inspected --id."])
                    finish()
                    return
                }
            }
            if let index = CommandLine.arguments.firstIndex(of: "--id"),
               CommandLine.arguments.count > index + 1,
               let id = UUID(uuidString: CommandLine.arguments[index + 1]),
               let peripheral = central.retrievePeripherals(withIdentifiers: [id]).first,
               isSupportedBandBoxName(peripheral.name) {
                target = peripheral
                peripheral.delegate = self
                emit("retrieved_target", ["id": id.uuidString, "name": peripheral.name ?? ""])
                central.connect(peripheral, options: nil)
                return
            }
            if arg("--id") != nil {
                emit("error", ["message": "The inspected UUID is unavailable or its name no longer matches."])
                finish()
                return
            }
            central.scanForPeripherals(withServices: nil, options: [CBCentralManagerScanOptionAllowDuplicatesKey: false])
            // Collect candidates before choosing. Never select the first of
            // multiple nearby amps with the same advertised name.
            DispatchQueue.main.asyncAfter(deadline: .now() + min(5.0, seconds / 2)) {
                guard !self.done, self.target == nil else { return }
                central.stopScan()
                guard self.candidates.count <= 1 else {
                    emit("error", ["message": "Multiple BandBox devices found; specify an inspected --id."])
                    self.finish()
                    return
                }
                guard let peripheral = self.candidates.values.first else {
                    self.anonymousCandidates.sort { $0.rssi > $1.rssi }
                    self.probeNextAnonymous()
                    return
                }
                self.target = peripheral
                peripheral.delegate = self
                central.connect(peripheral, options: nil)
            }
        } else if central.state == .unauthorized || central.state == .unsupported || central.state == .poweredOff {
            finish()
        }
    }
    /// The amp can advertise with no name and no manufacturer data; probe
    /// such candidates one at a time (strongest RSSI first), connecting and
    /// checking for the control service, same as tools/bandbox_ctl.py does.
    func probeNextAnonymous() {
        guard !done, target == nil else { return }
        guard !anonymousCandidates.isEmpty else {
            emit("error", ["message": "No JBL BandBox found. Enable its Bluetooth discovery."])
            finish()
            return
        }
        let next = anonymousCandidates.removeFirst().peripheral
        probingAnonymous = next
        next.delegate = self
        emit("probing_anonymous", ["id": next.identifier.uuidString])
        central.connect(next, options: nil)
    }
    func centralManager(_ central: CBCentralManager, didDiscover peripheral: CBPeripheral,
                        advertisementData: [String: Any], rssi RSSI: NSNumber) {
        seen.insert(peripheral.identifier)
        let name = advertisementData[CBAdvertisementDataLocalNameKey] as? String ?? peripheral.name ?? ""
        let services = (advertisementData[CBAdvertisementDataServiceUUIDsKey] as? [CBUUID] ?? []).map(\.uuidString)
        let manufacturer = advertisementData[CBAdvertisementDataManufacturerDataKey] as? Data ?? Data()
        if name.isEmpty, manufacturer.isEmpty, target == nil,
           !anonymousCandidates.contains(where: { $0.peripheral.identifier == peripheral.identifier }) {
            anonymousCandidates.append((peripheral, RSSI.intValue))
        }
        guard isSupportedBandBoxName(name) else { return }
        emit("advertisement", ["name": name, "id": peripheral.identifier.uuidString, "rssi": RSSI,
                               "services": services, "manufacturer_hex": hex(manufacturer),
                               "connectable": advertisementData[CBAdvertisementDataIsConnectable] ?? NSNull()])
        guard target == nil else { return }
        candidates[peripheral.identifier] = peripheral
    }
    func centralManager(_ central: CBCentralManager, didConnect peripheral: CBPeripheral) {
        if probingAnonymous === peripheral {
            peripheral.discoverServices([CBUUID(string: "65786365-6C70-6F69-6E74-2E636F6D0000")])
            return
        }
        connected = true
        driveConnection = peripheral.identifier.uuidString + ":" + UUID().uuidString
        emit("connected", ["id": peripheral.identifier.uuidString])
        peripheral.discoverServices(nil)
    }
    func centralManager(_ central: CBCentralManager, didFailToConnect peripheral: CBPeripheral, error: Error?) {
        if probingAnonymous === peripheral {
            probingAnonymous = nil
            probeNextAnonymous()
            return
        }
        emit("connection_failed", ["error": error?.localizedDescription ?? "unknown"])
        finish()
    }
    func centralManager(_ central: CBCentralManager, didDisconnectPeripheral peripheral: CBPeripheral, error: Error?) {
        if ignoredDisconnects.remove(peripheral.identifier) != nil { return }
        if probingAnonymous === peripheral {
            probingAnonymous = nil
            probeNextAnonymous()
            return
        }
        if !done { emit("disconnected", ["error": error?.localizedDescription ?? NSNull()]); finish() }
    }
    func peripheral(_ peripheral: CBPeripheral, didDiscoverServices error: Error?) {
        if probingAnonymous === peripheral {
            probingAnonymous = nil
            if error == nil, let services = peripheral.services, !services.isEmpty {
                target = peripheral
                emit("retrieved_target", ["id": peripheral.identifier.uuidString, "name": peripheral.name ?? ""])
                connected = true
                driveConnection = peripheral.identifier.uuidString + ":" + UUID().uuidString
                emit("connected", ["id": peripheral.identifier.uuidString])
                for service in services {
                    emit("service", ["uuid": service.uuid.uuidString, "primary": service.isPrimary])
                    peripheral.discoverCharacteristics(nil, for: service)
                }
            } else {
                ignoredDisconnects.insert(peripheral.identifier)
                central.cancelPeripheralConnection(peripheral)
                probeNextAnonymous()
            }
            return
        }
        if let error { emit("service_error", ["error": error.localizedDescription]); return }
        for service in peripheral.services ?? [] {
            emit("service", ["uuid": service.uuid.uuidString, "primary": service.isPrimary])
            peripheral.discoverCharacteristics(nil, for: service)
        }
    }
    func peripheral(_ peripheral: CBPeripheral, didDiscoverCharacteristicsFor service: CBService, error: Error?) {
        if let error { emit("characteristic_error", ["error": error.localizedDescription]); return }
        for char in service.characteristics ?? [] {
            emit("characteristic", ["service": service.uuid.uuidString, "uuid": char.uuid.uuidString,
                                    "properties": char.properties.rawValue])
            if (listenOnly || queryRequested) && service.uuid.uuidString == "65786365-6C70-6F69-6E74-2E636F6D0000" {
                if char.uuid.uuidString == "65786365-6C70-6F69-6E74-2E636F6D0002" { tx = char }
            }
            peripheral.discoverDescriptors(for: char)
            if char.properties.contains(.read) && standardReads.contains(char.uuid.uuidString) {
                peripheral.readValue(for: char)
            }
        }
        if (listenOnly || queryRequested), tx != nil,
           let rx = service.characteristics?.first(where: { $0.uuid.uuidString == "65786365-6C70-6F69-6E74-2E636F6D0001" }),
           rx.properties.contains(.notify) {
            peripheral.setNotifyValue(true, for: rx)
        }
    }
    func peripheral(_ peripheral: CBPeripheral, didUpdateNotificationStateFor characteristic: CBCharacteristic, error: Error?) {
        emit("notification_state", ["uuid": characteristic.uuid.uuidString, "enabled": characteristic.isNotifying,
                                    "error": error?.localizedDescription ?? NSNull()])
        guard queryRequested, characteristic.isNotifying,
              error == nil, !queried, let tx, tx.properties.contains(.write) else { return }
        queried = true
        if let (request, packet) = configuredControl {
            guard seconds - (ProcessInfo.processInfo.systemUptime - startedAt) >= 9 else {
                emit("control_refused", ["reason": "Insufficient reply time", "setting_writes": 0])
                finish(); return
            }
            emit("control_send", ["action": request.action, "hex": hex(packet), "setting_write": request.setting_write])
            if request.setting_write { settingWrites += 1 }
            peripheral.writeValue(packet, for: tx, type: .withResponse)
            DispatchQueue.main.asyncAfter(deadline: .now() + 8) { self.finish() }
            return
        }
        if let driveTest {
            let now = ProcessInfo.processInfo.systemUptime
            let remaining = seconds - (now - startedAt)
            // Five bounded six-second phases must fit before the probe deadline.
            // Reserve another five seconds for dispatch/callback overhead.
            guard remaining >= 35 else {
                recordDriveFinish(.stoppedBeforeWrite, "Connection setup left insufficient time for the complete decrement/restore cycle")
                finish()
                return
            }
            driveTest.begin(connectionID: driveConnection, now: now)
            drainDriveActions()
            return
        }
        if readCurrent {
            // JBL One's GuitarPresetSelectQuery(channel), NOT GuitarPresetSelect.
            // Semantic read 3441 returns feature3442; command2 is its app transport.
            // channel is 3 on the Trio, 1 on the Solo (see docs/protocol.md#bandbox-solo).
            let packet = Data([0x00, 0xDD, 0x02, 0x00, 0x01, 0x00, 0x05, 0x00, 0x41, 0x34, 0x01, 0x00, readCurrentChannel])
            emit("query", ["purpose": "get_channel3_selection_3441", "hex": hex(packet), "setting_write": false])
            peripheral.writeValue(packet, for: tx, type: .withResponse)
            return
        }
        // Fixed GET allowlist. 0x3440 inventory is backed by JBL One 2.7.9's
        // V5GuitarPresetAllSimpleInfoList (IV5Read) and sa.j GET builder/callsite.
        // This is command 0001 (GET), never command 0002 (SET).
        let feature: [UInt8] = readPresets ? [0x40, 0x34] : [0x0D, 0x00]
        let packet = Data([0x00, 0xDD, 0x01, 0x00, 0x01, 0x00, 0x02, 0x00] + feature)
        emit("query", ["purpose": readPresets ? "get_preset_inventory_3440" : "get_observed_feature_000D", "hex": hex(packet), "setting_write": false])
        peripheral.writeValue(packet, for: tx, type: .withResponse)
    }
    func peripheral(_ peripheral: CBPeripheral, didWriteValueFor characteristic: CBCharacteristic, error: Error?) {
        emit("query_transport_ack", ["uuid": characteristic.uuid.uuidString, "error": error?.localizedDescription ?? NSNull()])
        if let error, let driveTest {
            driveTest.transportFailed(connectionID: driveConnection, reason: "GATT write failed: " + error.localizedDescription)
            drainDriveActions()
        }
    }
    func peripheral(_ peripheral: CBPeripheral, didDiscoverDescriptorsFor characteristic: CBCharacteristic, error: Error?) {
        emit("descriptors", ["characteristic": characteristic.uuid.uuidString,
                             "uuids": (characteristic.descriptors ?? []).map { $0.uuid.uuidString },
                             "error": error?.localizedDescription ?? NSNull()])
    }
    func peripheral(_ peripheral: CBPeripheral, didUpdateValueFor characteristic: CBCharacteristic, error: Error?) {
        let data = characteristic.value ?? Data()
        emit(standardReads.contains(characteristic.uuid.uuidString) ? "standard_value" : "notification", ["uuid": characteristic.uuid.uuidString, "hex": hex(data),
                                "text": String(data: data, encoding: .utf8) ?? "",
                                "error": error?.localizedDescription ?? NSNull()])
        if let driveTest, characteristic.uuid.uuidString == "65786365-6C70-6F69-6E74-2E636F6D0001" {
            if let error { driveTest.transportFailed(connectionID: driveConnection, reason: error.localizedDescription) }
            else { driveTest.receive(data, connectionID: driveConnection, now: ProcessInfo.processInfo.systemUptime) }
            drainDriveActions()
            return
        }
        if let (request, _) = configuredControl, queried, error == nil,
           characteristic.uuid.uuidString == "65786365-6C70-6F69-6E74-2E636F6D0001" {
            if data == Data([0,0xDD,2,0,1,0,2,0,0,0]) { controlACK = true }
            if data.count >= 14, data[0] == 0, data[1] == 0xDD,
               (data[2] == 1 || data[2] == 3), data[3] == 0, data[4] == 1, data[5] == 0,
               data[8] == 0, data[9] == 0,
               UInt16(data[10]) + (UInt16(data[11]) << 8) == request.response_feature,
               Int(data[12]) + (Int(data[13]) << 8) == data.count - 14 {
                controlReply = true
            }
            // Python still performs complete strict correlation and domain matching.
            if controlReply && (request.command == 1 || controlACK) {
                responseReceived = true; finish(); return
            }
        }
        if readCurrent, queried, !detailQueried, error == nil,
           characteristic.uuid.uuidString == "65786365-6C70-6F69-6E74-2E636F6D0001",
           data.count == 16, data[0] == 0x00, data[1] == 0xDD,
           (data[2] == 1 || data[2] == 3), data[3] == 0,
           Array(data[4..<15]) == [1, 0, 8, 0, 0, 0, 0x42, 0x34, 2, 0, readCurrentChannel],
           (1...254).contains(Int(data[15])), let tx {
            detailQueried = true
            queriedPresetID = data[15]
            // App-proven info query only; no preset selection, slot edit, save or operation.
            let packet = Data([0x00, 0xDD, 0x02, 0x00, 0x01, 0x00, 0x05, 0x00, 0x43, 0x34, 0x01, 0x00, data[15]])
            emit("query", ["purpose": "get_preset_detail_3443", "hex": hex(packet), "setting_write": false])
            peripheral.writeValue(packet, for: tx, type: .withResponse)
        }
        if readCurrent, detailQueried, error == nil, data.count >= 15,
           characteristic.uuid.uuidString == "65786365-6C70-6F69-6E74-2E636F6D0001",
           data[0] == 0, data[1] == 0xDD, (data[2] == 1 || data[2] == 3),
           data[3] == 0, data[4] == 1, data[5] == 0, data[8] == 0, data[9] == 0,
           data[10] == 0x44, data[11] == 0x34, data[14] == queriedPresetID,
           Int(data[12]) + (Int(data[13]) << 8) == data.count - 14 {
            responseReceived = true; finish(); return
        }
        // Header classification only. Python validates the complete status/TLV payload.
        if queried, error == nil, data.count >= 8, data[0] == 0, data[1] == 0xDD,
           (data[2] == 1 || ((readCurrent || controlRequested) && data[2] == 3)), data[3] == 0, data[4] > 0, data[5] < data[4] {
            responseReceived = true
        }
    }
}

let classicTargets = (IOBluetoothDevice.pairedDevices() as? [IOBluetoothDevice] ?? []).filter {
    isSupportedBandBoxName($0.name)
}
for device in classicTargets {
    emit("classic_target", ["name": device.name ?? "", "address": device.addressString ?? "",
                            "connected": device.isConnected()])
    for record in device.services as? [IOBluetoothSDPServiceRecord] ?? [] {
        var channel: BluetoothRFCOMMChannelID = 0
        var psm: BluetoothL2CAPPSM = 0
        let rc = record.getRFCOMMChannelID(&channel)
        let pc = record.getL2CAPPSM(&psm)
        emit("classic_service", ["name": record.getServiceName() ?? "",
                                 "cached": true,
                                 "rfcomm_channel": rc == kIOReturnSuccess ? Int(channel) : -1,
                                 "l2cap_psm": pc == kIOReturnSuccess ? Int(psm) : -1,
                                 "attributes": String(describing: record.attributes)])
    }
}
if classicTargets.isEmpty { emit("classic_target_absent") }
let probe = Probe()
probe.start()
while !probe.done { RunLoop.main.run(until: Date(timeIntervalSinceNow: 0.25)) }
RunLoop.main.run(until: Date(timeIntervalSinceNow: 0.25))
exit(driveControlRequested ? (probe.driveSucceeded ? 0 : 1) : (probe.connected && (!queryRequested || probe.responseReceived) ? 0 : 1))
