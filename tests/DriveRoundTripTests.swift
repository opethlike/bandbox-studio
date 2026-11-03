import Foundation

// Synthetic byte fixtures only. This executable imports no Bluetooth framework.
private func expect(_ condition: @autoclosure () -> Bool, _ message: String) {
    if !condition() { fatalError(message) }
}
private func le(_ n: UInt16) -> [UInt8] { [UInt8(n & 255), UInt8(n >> 8)] }
private func float(_ n: Float) -> [UInt8] {
    (0..<4).map { UInt8((n.bitPattern >> ($0 * 8)) & 255) }
}
private func frame(_ command: UInt16, _ body: [UInt8], count: UInt8 = 1, index: UInt8 = 0) -> Data {
    Data([0, 0xDD] + le(command) + [count, index] + le(UInt16(body.count)) + body)
}
private func tlv(_ feature: UInt16, _ value: [UInt8]) -> [UInt8] {
    le(feature) + le(UInt16(value.count)) + value
}
private func slot(_ drive: Float, config: UInt8 = 1, bypass: UInt8 = 0) -> [UInt8] {
    [7, 6, config, bypass, 1, 0] + float(drive)
}
private func selection(_ preset: UInt8 = 2) -> Data { frame(3, [0, 0] + tlv(0x3442, [3, preset])) }
private func detail(_ drive: Float = 41, config: UInt8 = 1) -> Data {
    frame(3, [0, 0] + tlv(0x3444, [2, 0, 0, 1] + slot(drive, config: config)))
}
private func reported(_ drive: Float, config: UInt8 = 1, bypass: UInt8 = 0, change: UInt8 = 4) -> Data {
    frame(3, [0, 0] + tlv(0x3445, [3, 2, change] + slot(drive, config: config, bypass: bypass)))
}
private let successACK = frame(2, [0, 0])
private func plan(_ drive: Int = 41, config: UInt8 = 1, confirmed: Bool = true) -> DriveRoundTrip.Plan {
    .init(channel: 3, presetID: 2, slotID: 7, algorithmID: 6, configID: config, bypass: 0,
          originalFloatBytes: Data(float(Float(drive))), displayedDrive: drive,
          liveMetadataConfirmed: confirmed)
}
private func recoveryPlan(_ current: Int = 40, confirmed: Bool = true) -> DriveRoundTrip.Plan {
    .init(channel: 3, presetID: 2, slotID: 7, algorithmID: 6, configID: 1, bypass: 0,
          originalFloatBytes: Data(float(41)), displayedDrive: current, liveMetadataConfirmed: confirmed)
}
private final class Harness {
    let machine: DriveRoundTrip
    var time: Double = 0
    var writes = 0
    init(recovery: Int? = nil, requested: Int? = nil, baseline: Int = 41) throws {
        machine = try DriveRoundTrip(plan: recovery.map { recoveryPlan($0) } ?? plan(baseline),
                                    confirmedRecoveryDrive: recovery, requestedDrive: requested)
    }
    func next() -> (Int, Data, Bool) {
        let a = machine.actions()
        guard a.count == 1, case let .send(id, data, _, write) = a[0] else { fatalError("Expected one send") }
        return (id, data, write)
    }
    @discardableResult func sendNext() -> Data {
        let a = next(); time += 0.01
        machine.sent(actionID: a.0, connectionID: "session-1", now: time)
        if a.2 { writes += 1 }
        return a.1
    }
    func receive(_ data: Data) { time += 0.01; machine.receive(data, connectionID: "session-1", now: time) }
    func timeout() { time += 7; machine.tick(now: time) }
    func begin() { machine.begin(connectionID: "session-1", now: time) }
    func toDecrement(savedDrive: Float = 41) {
        begin()
        expect(sendNext() == Data([0, 0xDD, 2, 0, 1, 0, 5, 0, 0x41, 0x34, 1, 0, 3]), "Selection bytes")
        receive(selection()); receive(successACK)
        expect(sendNext() == Data([0, 0xDD, 2, 0, 1, 0, 5, 0, 0x43, 0x34, 1, 0, 2]), "Detail bytes")
        receive(successACK); receive(detail(savedDrive))
    }
    func toRestoreSelection() {
        toDecrement(); _ = sendNext(); receive(reported(40)); receive(successACK)
    }
    func toRestore() {
        toRestoreSelection(); _ = sendNext(); receive(successACK); receive(selection())
    }
    func finished(_ outcome: DriveRoundTrip.Outcome) {
        let a = machine.actions()
        guard a.count == 1, case let .finish(actual, _) = a[0] else { fatalError("Expected one finish") }
        expect(actual == outcome, "Wrong outcome")
        expect(machine.isFinished, "Not terminal")
        receive(successACK); receive(reported(40)); timeout(); begin()
        expect(machine.actions().isEmpty, "Terminal machine emitted another action")
    }
}

@main struct Tests {
    static func main() throws {
        var passed = 0
        func test(_ name: String, _ body: () throws -> Void) rethrows {
            try body(); passed += 1; print("PASS \(name)")
        }
        try test("reject unsafe or unconfirmed plans") {
            for p in [plan(0), plan(101), plan(config: 9), plan(confirmed: false)] {
                do { _ = try DriveRoundTrip(plan: p); fatalError("Accepted invalid plan") }
                catch DriveRoundTrip.Failure.invalidPlan { }
            }
            let p = DriveRoundTrip.Plan(channel: 3, presetID: 2, slotID: 7, algorithmID: 6, configID: 1, bypass: 0,
                originalFloatBytes: Data(float(.nan)), displayedDrive: 41, liveMetadataConfirmed: true)
            do { _ = try DriveRoundTrip(plan: p); fatalError("Accepted NaN") }
            catch DriveRoundTrip.Failure.invalidPlan { }
        }
        try test("normal round trip, either event order, exact wire bytes") {
            let h = try Harness(); h.toDecrement()
            let expected = Data([0, 0xDD, 2, 0, 1, 0, 17, 0, 0x45, 0x34, 13, 0,
                                 3, 2, 4, 7, 6, 1, 0, 1, 0] + float(40))
            expect(h.sendNext() == expected, "Not exact one-value target packet")
            h.receive(reported(40))
            expect(h.machine.actions().isEmpty, "Advanced without ACK")
            h.receive(successACK)
            _ = h.sendNext(); h.receive(selection()); h.receive(successACK)
            let restore = h.sendNext()
            expect(Array(restore.suffix(4)) == float(41), "Original raw float not restored")
            h.receive(successACK)
            expect(h.machine.actions().isEmpty, "Finished without restored report")
            h.receive(reported(41)); h.finished(.restoredDeviceReported)
            expect(h.writes == 2, "More than decrement and restore")
        }
        try test("queued but unsent frames cannot satisfy preflight") {
            let h = try Harness(); h.begin()
            let a = h.next(); h.receive(successACK); h.receive(selection())
            expect(h.machine.actions().isEmpty, "Pre-send responses advanced phase")
            h.time += 0.01; h.machine.sent(actionID: a.0, connectionID: "session-1", now: h.time)
            h.receive(selection()); h.receive(successACK)
            expect(h.next().2 == false, "Expected detail query")
        }
        try test("baseline mismatch stops before mutation") {
            let h = try Harness(); h.begin(); _ = h.sendNext(); h.receive(successACK); h.receive(selection())
            _ = h.sendNext(); h.receive(successACK); h.receive(detail(42)); h.finished(.stoppedBeforeWrite)
        }
        try test("another preset stops before mutation") {
            let h = try Harness(); h.begin(); _ = h.sendNext(); h.receive(selection(3)); h.finished(.stoppedBeforeWrite)
        }
        try test("selection change after target forbids restoration") {
            let h = try Harness(); h.toRestoreSelection(); _ = h.sendNext()
            h.receive(selection(3)); h.finished(.uncertain); expect(h.writes == 1, "Restored stale preset")
        }
        try test("metadata change after target forbids restoration") {
            let h = try Harness(); h.toRestoreSelection(); _ = h.sendNext()
            h.receive(reported(40, config: 2)); h.finished(.uncertain)
        }
        try test("external Drive change forbids restoration") {
            let h = try Harness(); h.toRestoreSelection(); _ = h.sendNext()
            h.receive(reported(39)); h.finished(.uncertain)
        }
        try test("ACK-only timeout cannot infer live target") {
            let h = try Harness(); h.toDecrement(); _ = h.sendNext(); h.receive(successACK)
            h.timeout(); h.finished(.uncertain); expect(h.writes == 1, "Blind restore")
        }
        try test("target-reported timeout permits one guarded restoration") {
            let h = try Harness(); h.toDecrement(); _ = h.sendNext(); h.receive(reported(40))
            h.timeout(); expect(h.sendNext().count == 13, "Expected selection requery")
            h.receive(successACK); h.receive(selection())
            expect(h.machine.actions().isEmpty, "Old target report permitted timeout recovery")
            h.receive(reported(40)); _ = h.sendNext()
            h.receive(reported(41)); h.receive(successACK); h.finished(.restoredDeviceReported)
            expect(h.writes == 2, "Incorrect recovery write count")
        }
        try test("timeout recovery needs new live metadata, not just selection") {
            let h = try Harness(); h.toDecrement(); _ = h.sendNext(); h.receive(reported(40))
            h.timeout(); _ = h.sendNext(); h.receive(successACK); h.receive(selection())
            expect(h.machine.actions().isEmpty, "Recovered without fresh live target")
            h.timeout(); h.finished(.uncertain); expect(h.writes == 1, "Unverified recovery write")
        }
        try test("restore timeout is terminal, with no resend") {
            let h = try Harness(); h.toRestore(); _ = h.sendNext(); h.receive(successACK)
            h.timeout(); h.finished(.uncertain); expect(h.writes == 2, "Restore retried")
        }
        try test("disconnect is terminal; reconnect never resumes") {
            let h = try Harness(); h.toDecrement(); _ = h.sendNext()
            h.machine.transportFailed(connectionID: "session-1", reason: "Disconnected")
            h.finished(.uncertain)
        }
        try test("foreign connection cannot satisfy readback") {
            let h = try Harness(); h.toDecrement(); _ = h.sendNext(); h.time += 0.01
            h.machine.receive(reported(40), connectionID: "session-2", now: h.time); h.finished(.uncertain)
        }
        try test("nonzero ACK fails closed") {
            let h = try Harness(); h.toDecrement(); _ = h.sendNext()
            h.receive(frame(2, [1, 0])); h.finished(.uncertain)
        }
        try test("fragmented detail reassembles status only once") {
            let h = try Harness(); h.begin(); _ = h.sendNext(); h.receive(successACK); h.receive(selection())
            _ = h.sendNext(); h.receive(successACK)
            let body = [UInt8](detail().dropFirst(8))
            h.receive(frame(3, Array(body.prefix(9)), count: 2, index: 0))
            expect(h.machine.actions().isEmpty, "Partial detail advanced")
            h.receive(frame(3, Array(body.dropFirst(9)), count: 2, index: 1))
            expect(h.next().2, "Reassembled detail did not permit planned decrement")
        }
        try test("fragment mismatch fails closed") {
            let h = try Harness(); h.begin(); _ = h.sendNext()
            h.receive(frame(3, [0], count: 2, index: 0))
            h.receive(frame(3, [0], count: 2, index: 0)); h.finished(.stoppedBeforeWrite)
        }
        try test("pre-send fragment cannot become fresh readback") {
            let h = try Harness(); h.begin(); let a = h.next()
            h.receive(frame(3, [0], count: 2, index: 0))
            h.time += 0.01; h.machine.sent(actionID: a.0, connectionID: "session-1", now: h.time)
            h.receive(frame(3, [0] + tlv(0x3442, [3, 2]), count: 2, index: 1))
            h.finished(.stoppedBeforeWrite)
        }
        try test("duplicate Drive parameters are rejected") {
            let h = try Harness(); h.toDecrement(); _ = h.sendNext()
            let duplicate = [UInt8]([3, 2, 4, 7, 6, 1, 0, 2, 0]) + float(40) + [0] + float(40)
            h.receive(frame(3, [0, 0] + tlv(0x3445, duplicate))); h.finished(.uncertain)
        }
        try test("AllInfo cannot use omitted bypass as proof of live state") {
            let h = try Harness(); h.toDecrement(); _ = h.sendNext()
            h.receive(reported(40, bypass: 0xFF, change: 0)); h.finished(.uncertain)
        }
        try test("whole message checked before sending next action") {
            let h = try Harness(); h.toDecrement(); _ = h.sendNext(); h.receive(successACK)
            let body = [UInt8]([0, 0]) + tlv(0x3445, [3, 2, 4] + slot(40)) + tlv(0x3442, [3, 9])
            h.receive(frame(3, body)); h.finished(.uncertain)
        }
        try test("stale baseline before restore send cannot count as success") {
            let h = try Harness(); h.toRestore(); _ = h.next()
            h.receive(reported(41)); h.finished(.uncertain)
        }
        try test("restore-only requires explicitly matching current decrement") {
            for current in [39, 41] {
                do {
                    _ = try DriveRoundTrip(plan: recoveryPlan(current), confirmedRecoveryDrive: current)
                    fatalError("Accepted incorrect recovery target")
                } catch DriveRoundTrip.Failure.invalidPlan { }
            }
            do { _ = try DriveRoundTrip(plan: recoveryPlan()); fatalError("Implicit recovery accepted") }
            catch DriveRoundTrip.Failure.invalidPlan { }
            do {
                _ = try DriveRoundTrip(plan: recoveryPlan(confirmed: false), confirmedRecoveryDrive: 40)
                fatalError("Unconfirmed recovery accepted")
            } catch DriveRoundTrip.Failure.invalidPlan { }
            do {
                _ = try DriveRoundTrip(plan: recoveryPlan(), confirmedRecoveryDrive: 39)
                fatalError("Mismatched displayed recovery accepted")
            } catch DriveRoundTrip.Failure.invalidPlan { }
        }
        try test("restore-only emits exactly one original-value write") {
            let h = try Harness(recovery: 40); h.toDecrement()
            let expected = Data([0, 0xDD, 2, 0, 1, 0, 17, 0, 0x45, 0x34, 13, 0,
                                 3, 2, 4, 7, 6, 1, 0, 1, 0] + float(41))
            expect(h.sendNext() == expected, "Recovery emitted a decrement or altered metadata")
            h.receive(reported(41, bypass: 0xFF))
            expect(h.machine.actions().isEmpty, "Recovery succeeded without ACK")
            h.receive(successACK); h.finished(.restoredDeviceReported)
            expect(h.writes == 1, "Recovery emitted more than one write")
        }
        try test("restore-only preflight checks current40, not saved41") {
            let h = try Harness(recovery: 40); h.begin(); _ = h.sendNext()
            h.receive(reported(40, bypass: 0xFF)); h.receive(selection()); h.receive(successACK)
            _ = h.sendNext(); h.receive(successACK); h.receive(detail())
            expect(h.sendNext().suffix(4) == Data(float(41)), "Expected original41 restoration")
            h.receive(successACK); h.receive(reported(41)); h.finished(.restoredDeviceReported)
        }
        try test("restore-only preflight contradiction stops before write") {
            let h = try Harness(recovery: 40); h.begin(); _ = h.sendNext()
            h.receive(reported(41)); h.finished(.stoppedBeforeWrite)
            expect(h.writes == 0, "Recovery ignored changed current value")
        }
        try test("restore-only timeout is uncertain and never retries") {
            let h = try Harness(recovery: 40); h.toDecrement(); _ = h.sendNext()
            h.receive(successACK); h.timeout(); h.finished(.uncertain)
            expect(h.writes == 1, "Recovery retry")
        }
        try test("normal round trip permits exact partial Drive echo") {
            let h = try Harness(); h.toDecrement(); _ = h.sendNext()
            h.receive(successACK); h.receive(reported(40, bypass: 0xFF))
            _ = h.sendNext(); h.receive(successACK); h.receive(selection()); _ = h.sendNext()
            h.receive(reported(41, bypass: 0xFF)); h.receive(successACK)
            h.finished(.restoredDeviceReported); expect(h.writes == 2, "Partial echo altered write count")
        }
        try test("omitted bypass rejected for other change types") {
            for change: UInt8 in [1, 2, 3] {
                let h = try Harness(); h.toDecrement(); _ = h.sendNext()
                h.receive(reported(40, bypass: 0xFF, change: change)); h.finished(.uncertain)
            }
        }
        try test("omitted bypass requires singleton parameter0") {
            for pairs in [[UInt8]([1]) + float(40), [0] + float(40) + [1] + float(40)] {
                let h = try Harness(); h.toDecrement(); _ = h.sendNext()
                let count = UInt8(pairs.count / 5)
                let value = [UInt8]([3, 2, 4, 7, 6, 1, 0xFF, count]) + pairs
                h.receive(frame(3, [0, 0] + tlv(0x3445, value))); h.finished(.uncertain)
            }
        }
        try test("explicit bypass mismatch is never omitted metadata") {
            let h = try Harness(recovery: 40); h.begin(); _ = h.sendNext()
            h.receive(reported(40, bypass: 1)); h.finished(.stoppedBeforeWrite)
            let after = try Harness(); after.toDecrement(); _ = after.sendNext()
            after.receive(reported(40, bypass: 1)); after.finished(.uncertain)
        }
        try test("omitted bypass cannot hide mismatched model") {
            let h = try Harness(); h.toDecrement(); _ = h.sendNext()
            h.receive(reported(40, config: 2, bypass: 0xFF)); h.finished(.uncertain)
        }
        try test("timeout recovery still needs explicitly refreshed bypass") {
            let h = try Harness(); h.toDecrement(); _ = h.sendNext()
            h.receive(reported(40, bypass: 0xFF)); h.timeout(); _ = h.sendNext()
            h.receive(successACK); h.receive(selection()); h.receive(reported(40, bypass: 0xFF))
            expect(h.machine.actions().isEmpty, "Timeout recovery treated omission as fresh bypass")
            h.timeout(); h.finished(.uncertain)
        }
        try test("requested target enforces bounds, delta and no-op rejection") {
            for target in [-1, 101, 35, 47, 41] {
                do { _ = try DriveRoundTrip(plan: plan(), requestedDrive: target); fatalError("Unsafe target accepted") }
                catch DriveRoundTrip.Failure.invalidPlan { }
            }
            do {
                _ = try DriveRoundTrip(plan: recoveryPlan(), confirmedRecoveryDrive: 40, requestedDrive: 42)
                fatalError("Requested and recovery modes both accepted")
            } catch DriveRoundTrip.Failure.invalidPlan { }
        }
        try test("requested baseline must exactly match displayed live value") {
            let p = DriveRoundTrip.Plan(channel: 3, presetID: 2, slotID: 7, algorithmID: 6, configID: 1, bypass: 0,
                originalFloatBytes: Data(float(42)), displayedDrive: 41, liveMetadataConfirmed: true)
            do { _ = try DriveRoundTrip(plan: p, requestedDrive: 43); fatalError("Mismatched baseline accepted") }
            catch DriveRoundTrip.Failure.invalidPlan { }
            do { _ = try DriveRoundTrip(plan: plan(confirmed: false), requestedDrive: 43); fatalError("Unconfirmed baseline accepted") }
            catch DriveRoundTrip.Failure.invalidPlan { }
        }
        try test("requested bounds allow baseline0 and100, exact five-point changes") {
            for (current, target) in [(0, 5), (5, 0), (95, 100), (100, 95)] {
                let h = try Harness(requested: target, baseline: current)
                h.toDecrement(savedDrive: 41)
                let data = h.sendNext()
                expect(data.suffix(4) == Data(float(Float(target))), "Wrong bounded target bytes")
                h.receive(successACK); h.receive(reported(Float(target), bypass: 0xFF))
                h.finished(.changedDeviceReported)
                expect(h.writes == 1, "Requested mode emitted extra mutation")
            }
        }
        try test("requested set accepts saved Drive different from live baseline") {
            let h = try Harness(requested: 45, baseline: 40)
            h.begin(); _ = h.sendNext(); h.receive(reported(40, bypass: 0xFF))
            h.receive(selection()); h.receive(successACK); _ = h.sendNext()
            h.receive(detail(41)); h.receive(successACK)
            let expected = Data([0, 0xDD, 2, 0, 1, 0, 17, 0, 0x45, 0x34, 13, 0,
                                 3, 2, 4, 7, 6, 1, 0, 1, 0] + float(45))
            expect(h.sendNext() == expected, "Requested mode altered metadata or target")
            h.receive(reported(45, bypass: 0xFF))
            expect(h.machine.actions().isEmpty, "Requested result accepted without ACK")
            h.receive(successACK); h.finished(.changedDeviceReported)
            expect(h.writes == 1, "Requested set did not remain one write")
        }
        try test("requested preflight live contradiction stops before any write") {
            let h = try Harness(requested: 45, baseline: 40)
            h.begin(); _ = h.sendNext(); h.receive(reported(41, bypass: 0xFF))
            h.finished(.stoppedBeforeWrite); expect(h.writes == 0, "Used saved rather than live baseline")
        }
        try test("requested mode still checks saved amp metadata") {
            let h = try Harness(requested: 43)
            h.begin(); _ = h.sendNext(); h.receive(selection()); h.receive(successACK)
            _ = h.sendNext(); h.receive(successACK); h.receive(detail(17, config: 2))
            h.finished(.stoppedBeforeWrite)
        }
        try test("requested mode rejects invalid saved Drive data") {
            let h = try Harness(requested: 43)
            h.toDecrement(savedDrive: 101); h.finished(.stoppedBeforeWrite)
        }
        try test("requested ACK-only timeout never restores or retries") {
            let h = try Harness(requested: 43); h.toDecrement(); _ = h.sendNext()
            h.receive(successACK); h.timeout(); h.finished(.uncertain)
            expect(h.writes == 1, "Requested timeout emitted extra write")
        }
        try test("requested target-only timeout never enters recovery") {
            let h = try Harness(requested: 43); h.toDecrement(); _ = h.sendNext()
            h.receive(reported(43, bypass: 0xFF)); h.timeout(); h.finished(.uncertain)
            expect(h.writes == 1, "Requested target-only timeout entered recovery")
        }
        try test("requested disconnect after write remains uncertain") {
            let h = try Harness(requested: 43); h.toDecrement(); _ = h.sendNext()
            h.machine.transportFailed(connectionID: "session-1", reason: "Disconnected")
            h.finished(.uncertain); expect(h.writes == 1, "Disconnected requested operation retried")
        }
        try test("requested target before submission is conflicting external state") {
            let h = try Harness(requested: 43); h.toDecrement(); _ = h.next()
            h.receive(reported(43, bypass: 0xFF)); h.finished(.stoppedBeforeWrite)
            expect(h.writes == 0, "Pre-send target falsely confirmed a mutation")
        }
        try test("requested wrong value or failed ACK cannot succeed") {
            let h = try Harness(requested: 43); h.toDecrement(); _ = h.sendNext()
            h.receive(successACK); h.receive(reported(42, bypass: 0xFF)); h.finished(.uncertain)
            let ackFailure = try Harness(requested: 43); ackFailure.toDecrement(); _ = ackFailure.sendNext()
            ackFailure.receive(frame(2, [0, 0x80])); ackFailure.finished(.uncertain)
        }
        print("\(passed) offline tests passed; no hardware accessed.")
    }
}
