import Foundation

/// Offline only: emits bounded actions; owns no Bluetooth objects and performs no I/O.
/// Run every call on one serial queue. Submit each drained .send action immediately,
/// then call sent() before processing another callback. Use a unique connection token
/// per connection; never resume this instance after reconnecting. Log raw traffic outside
/// this class. The protocol has no transaction ID: temporal correlation is not proof
/// of DSP application. InfoQuery reads saved state, so Plan requires separate live
/// confirmation of the displayed Drive, model and bypass before construction.
final class DriveRoundTrip {
    struct Plan {
        let channel: UInt8
        let presetID: UInt8
        let slotID: UInt8
        let algorithmID: UInt8
        let configID: UInt8
        let bypass: UInt8
        let originalFloatBytes: Data
        let displayedDrive: Int
        let liveMetadataConfirmed: Bool
    }

    enum Outcome: Equatable { case restoredDeviceReported, changedDeviceReported, stoppedBeforeWrite, uncertain }
    enum Action {
        case send(id: Int, packet: Data, purpose: String, settingWrite: Bool)
        case finish(outcome: Outcome, reason: String)
    }
    enum Failure: Error { case invalidPlan, malformedPacket }
    private enum Phase { case idle, selection, detail, decrement, restoreSelection, restore, requestedSet, done }
    private struct Slot {
        let id: UInt8, algorithm: UInt8, config: UInt8, bypass: UInt8
        let values: [UInt8: UInt32]
    }
    private enum Event {
        case ack
        case selection(UInt8, UInt8)
        case detail(UInt8, [Slot])
        case slot(UInt8, UInt8, UInt8, Slot)
    }
    private struct Fragments {
        let command: UInt16, count: UInt8
        var next: UInt8
        var payload: Data
    }

    let plan: Plan
    private let originalBits: UInt32, targetBits: UInt32, timeout: TimeInterval
    private let restoreOnly: Bool
    private let requestedSet: Bool
    private var phase = Phase.idle
    private var connection: String?
    private var lastTime = -Double.infinity
    private var deadline = Double.infinity
    private var sequence = 0
    private var pendingID: Int?
    private var sentAt: TimeInterval?
    private var ack = false, matched = false, targetReported = false
    private var mutationSent = false, restoreSent = false
    private var recoveryNeedsLiveReport = false, recoveryLiveReport = false
    private var fragments: Fragments?
    private var outbox: [Action] = []

    /// Recovery is a new, explicitly confirmed operation, never an automatic reconnect.
    /// In recovery mode displayedDrive is the CURRENT decremented value, while
    /// originalFloatBytes remain the original/saved value to restore exactly once.
    /// In requested mode both baseline fields are confirmed LIVE state, independent
    /// of saved Drive. Target is bounded to +/-5; caller handles no-op without I/O.
    init(plan: Plan, confirmedRecoveryDrive: Int? = nil, requestedDrive: Int? = nil,
         timeout: TimeInterval = 6) throws {
        guard plan.liveMetadataConfirmed, plan.channel == 1 || plan.channel == 3,
              plan.presetID != 0xFF, plan.slotID != 0xFF,
              plan.algorithmID == 6, plan.configID <= 8, plan.bypass <= 1,
              (0...100).contains(plan.displayedDrive), plan.originalFloatBytes.count == 4,
              timeout.isFinite, (1...30).contains(timeout),
              confirmedRecoveryDrive == nil || requestedDrive == nil else { throw Failure.invalidPlan }
        let bits = Self.bits(Array(plan.originalFloatBytes))
        let original = Float(bitPattern: bits)
        guard original.isFinite, original >= 0, original <= 100 else { throw Failure.invalidPlan }
        if let target = requestedDrive {
            guard original == Float(plan.displayedDrive), (0...100).contains(target),
                  abs(target - plan.displayedDrive) <= 5, target != plan.displayedDrive else {
                throw Failure.invalidPlan
            }
        } else if let current = confirmedRecoveryDrive {
            guard original >= 1 else { throw Failure.invalidPlan }
            guard current == plan.displayedDrive, Float(current) == original - 1 else { throw Failure.invalidPlan }
        } else {
            guard original >= 1, original == Float(plan.displayedDrive) else { throw Failure.invalidPlan }
        }
        self.plan = plan
        self.originalBits = bits
        self.targetBits = requestedDrive.map { Float($0).bitPattern } ?? (original - 1).bitPattern
        self.restoreOnly = confirmedRecoveryDrive != nil
        self.requestedSet = requestedDrive != nil
        self.timeout = timeout
    }

    var isFinished: Bool { phase == .done }
    func actions() -> [Action] { let result = outbox; outbox.removeAll(); return result }

    func begin(connectionID: String, now: TimeInterval) {
        guard phase == .idle else { return }
        guard !connectionID.isEmpty, clock(now) else { finish("Invalid connection or clock"); return }
        connection = connectionID
        querySelection(phase: .selection, now: now)
    }

    /// Call immediately after submitting the corresponding packet, not on its later ACK.
    /// A received frame before this marker cannot satisfy a pending action.
    func sent(actionID: Int, connectionID: String, now: TimeInterval) {
        guard phase != .done else { return }
        guard connectionID == connection, clock(now), actionID == pendingID, sentAt == nil else {
            finish("Unexpected send marker or connection"); return
        }
        // A fragment sequence begun before submission must never become its readback.
        fragments = nil
        sentAt = now
        deadline = now + timeout
        if phase == .decrement || phase == .requestedSet { mutationSent = true }
        if phase == .restore { restoreSent = true; mutationSent = true }
    }

    /// Only raw Direct-protocol notifications from the same inspected device connection.
    func receive(_ packet: Data, connectionID: String, now: TimeInterval) {
        guard phase != .idle, phase != .done else { return }
        guard connectionID == connection, clock(now) else { finish("Connection or clock changed"); return }
        // Caller must tick on a timer; late frames do not revive an expired operation.
        guard now <= deadline else { tick(now: now); return }
        do {
            guard let message = try reassemble(packet) else { return }
            let events = try decode(command: message.0, payload: message.1)
            let fresh = sentAt.map { now >= $0 } ?? false
            for event in events {
                handle(event, fresh: fresh)
                if phase == .done { return }
            }
            if fresh { advance(now: now) }
        } catch { finish("Malformed, incomplete, or unsuccessful protocol response") }
    }

    func tick(now: TimeInterval) {
        guard phase != .idle, phase != .done else { return }
        guard clock(now) else { finish("Clock changed"); return }
        guard now > deadline else { return }
        // A saved detail response cannot establish the current unsaved Drive. Recovery
        // therefore requires a prior fresh, full-metadata report of our exact target.
        if phase == .decrement, mutationSent, targetReported, !restoreSent {
            recoveryNeedsLiveReport = true
            recoveryLiveReport = false
            querySelection(phase: .restoreSelection, now: now)
        } else { finish("Timed out; no unsafe retry or unverified restoration attempted") }
    }

    func transportFailed(connectionID: String, reason: String) {
        guard phase != .done, phase != .idle else { return }
        finish(connectionID == connection ? reason : "Connection changed")
    }

    private func clock(_ now: TimeInterval) -> Bool {
        guard now.isFinite, now >= lastTime else { return false }
        lastTime = now
        return true
    }
    private func finish(_ reason: String, success: Outcome? = nil) {
        guard phase != .done else { return }
        phase = .done
        fragments = nil
        pendingID = nil
        outbox.removeAll()
        outbox.append(.finish(outcome: success ?? (mutationSent ? .uncertain : .stoppedBeforeWrite), reason: reason))
    }
    private func queue(_ packet: Data, phase: Phase, purpose: String, now: TimeInterval) {
        self.phase = phase
        ack = false; matched = false; sentAt = nil; fragments = nil
        sequence += 1; pendingID = sequence; deadline = now + timeout
        outbox.append(.send(id: sequence, packet: packet, purpose: purpose,
                            settingWrite: phase == .decrement || phase == .restore || phase == .requestedSet))
    }
    private func querySelection(phase: Phase, now: TimeInterval) {
        queue(Self.packet(0x3441, [plan.channel]), phase: phase,
              purpose: "Read selection", now: now)
    }
    private func drivePacket(bits: UInt32) -> Data {
        Self.packet(0x3445, [plan.channel, plan.presetID, 4, plan.slotID, plan.algorithmID,
                            plan.configID, plan.bypass, 1, 0] + Self.bytes(bits))
    }
    private func advance(now: TimeInterval) {
        guard ack, matched else { return }
        switch phase {
        case .selection:
            queue(Self.packet(0x3443, [plan.presetID]), phase: .detail,
                  purpose: "Compare saved amp metadata with confirmed live baseline", now: now)
        case .detail:
            if requestedSet {
                queue(drivePacket(bits: targetBits), phase: .requestedSet,
                      purpose: "Set requested Drive once", now: now)
            } else if restoreOnly {
                queue(drivePacket(bits: originalBits), phase: .restore,
                      purpose: "Restore confirmed original Drive bytes once", now: now)
            } else {
                queue(drivePacket(bits: targetBits), phase: .decrement,
                      purpose: "Temporarily lower Drive by exactly one", now: now)
            }
        case .decrement:
            querySelection(phase: .restoreSelection, now: now)
        case .restoreSelection:
            guard targetReported, !restoreSent else { finish("Restoration guard failed"); return }
            // Selection says nothing about the live slot. After a timeout require a
            // new full-metadata target report as well, never just the earlier report.
            guard !recoveryNeedsLiveReport || recoveryLiveReport else { return }
            queue(drivePacket(bits: originalBits), phase: .restore,
                  purpose: "Restore original Drive bytes once", now: now)
        case .restore:
            finish(restoreOnly ? "Original Drive restoration device-reported" :
                   "Decrement and restoration both device-reported", success: .restoredDeviceReported)
        case .requestedSet:
            finish("Requested Drive device-reported", success: .changedDeviceReported)
        default: break
        }
    }
    private func metadataMatches(_ slot: Slot, change: UInt8? = nil) -> Bool {
        // App decoder maps bypass FF to null; copyWith preserves the prior bypass.
        // Restrict that omission to the observed exact singleton Drive/change4 shape.
        // AllInfo, bypass changes, other controls and unknown models cannot use it.
        let omittedDriveBypass = slot.bypass == 0xFF && change == 4 &&
            slot.values.count == 1 && slot.values[0] != nil
        return slot.id == plan.slotID && slot.algorithm == plan.algorithmID &&
        slot.config == plan.configID && (slot.bypass == plan.bypass || omittedDriveBypass)
    }
    private func handle(_ event: Event, fresh: Bool) {
        switch event {
        case .ack:
            if fresh { ack = true }
        case let .selection(channel, preset):
            guard channel == plan.channel else { return }
            guard preset == plan.presetID else { finish("CH3 selection changed"); return }
            if fresh, phase == .selection || phase == .restoreSelection { matched = true }
        case let .detail(preset, slots):
            guard phase == .detail, fresh, preset == plan.presetID else { return }
            guard let slot = slots.first(where: { $0.id == plan.slotID }),
                  metadataMatches(slot), let savedBits = slot.values[0],
                  (0...100).contains(Float(bitPattern: savedBits)),
                  requestedSet || savedBits == originalBits else {
                finish("Saved amp metadata or Drive does not match confirmed baseline"); return
            }
            matched = true
        case let .slot(channel, preset, change, slot):
            guard channel == plan.channel else { return }
            guard preset == plan.presetID else { finish("CH3 slot report has another preset"); return }
            guard slot.id == plan.slotID else { return }
            guard metadataMatches(slot, change: change) else { finish("Amp metadata changed or is incomplete"); return }
            guard let value = slot.values[0] else { return }
            switch phase {
            case .selection, .detail:
                if value != (restoreOnly ? targetBits : originalBits) {
                    finish("Live Drive conflicts with confirmed baseline")
                }
            case .decrement:
                if fresh, value == targetBits { targetReported = true; matched = true }
                else if value != originalBits || fresh { finish("Conflicting live Drive report") }
            case .requestedSet:
                if fresh, value == targetBits { matched = true }
                else if value != originalBits || fresh { finish("Conflicting requested Drive report") }
            case .restoreSelection:
                if value != targetBits { finish("Drive changed before restoration") }
                else if fresh, recoveryNeedsLiveReport, slot.bypass == plan.bypass { recoveryLiveReport = true }
            case .restore:
                if fresh, value == originalBits { matched = true }
                else if value != targetBits || matched { finish("Conflicting restoration report") }
            default: break
            }
        }
    }

    private static func packet(_ feature: UInt16, _ value: [UInt8]) -> Data {
        let length = UInt16(value.count)
        return Data([0, 0xDD, 2, 0, 1, 0, UInt8(length + 4), 0,
                     UInt8(feature & 255), UInt8(feature >> 8), UInt8(length), 0] + value)
    }
    private static func bytes(_ bits: UInt32) -> [UInt8] {
        (0..<4).map { UInt8((bits >> ($0 * 8)) & 255) }
    }
    private static func bits(_ bytes: [UInt8]) -> UInt32 {
        bytes.enumerated().reduce(0) { $0 | (UInt32($1.element) << ($1.offset * 8)) }
    }
    private struct Cursor {
        let bytes: [UInt8]
        var offset = 0
        var remaining: Int { bytes.count - offset }
        mutating func take(_ n: Int) throws -> [UInt8] {
            guard n >= 0, n <= remaining else { throw Failure.malformedPacket }
            defer { offset += n }
            return Array(bytes[offset..<(offset + n)])
        }
        mutating func byte() throws -> UInt8 { try take(1)[0] }
        mutating func word() throws -> UInt16 { UInt16(try DriveRoundTrip.bits(take(2))) }
        mutating func slot() throws -> Slot {
            let id = try byte(), algorithm = try byte(), config = try byte(), bypass = try byte()
            let count = Int(try byte())
            var values: [UInt8: UInt32] = [:]
            for _ in 0..<count {
                let id = try byte(), bits = try DriveRoundTrip.bits(take(4))
                guard values[id] == nil, Float(bitPattern: bits).isFinite else { throw Failure.malformedPacket }
                values[id] = bits
            }
            return Slot(id: id, algorithm: algorithm, config: config, bypass: bypass, values: values)
        }
    }
    private func reassemble(_ packet: Data) throws -> (UInt16, Data)? {
        var c = Cursor(bytes: Array(packet))
        let identifier = try c.word(), command = try c.word(), count = try c.byte(), index = try c.byte()
        let length = Int(try c.word())
        guard identifier == 0xDD00, (1...3).contains(command), count > 0, count <= 32,
              index < count, c.remaining == length else { throw Failure.malformedPacket }
        let body = Data(try c.take(length))
        if index == 0 {
            guard fragments == nil else { throw Failure.malformedPacket }
            fragments = Fragments(command: command, count: count, next: 0, payload: Data())
        }
        guard var parts = fragments, parts.command == command, parts.count == count,
              parts.next == index, parts.payload.count + body.count <= 8192 else { throw Failure.malformedPacket }
        parts.payload.append(body); parts.next += 1
        if parts.next == count { fragments = nil; return (command, parts.payload) }
        fragments = parts
        return nil
    }
    private func decode(command: UInt16, payload: Data) throws -> [Event] {
        var c = Cursor(bytes: Array(payload))
        guard try c.word() == 0 else { throw Failure.malformedPacket }
        if command == 2 {
            guard c.remaining == 0 else { throw Failure.malformedPacket }
            return [.ack]
        }
        var events: [Event] = []
        while c.remaining > 0 {
            let feature = try c.word(), length = Int(try c.word())
            var v = Cursor(bytes: try c.take(length))
            switch feature {
            case 0x3442: events.append(.selection(try v.byte(), try v.byte()))
            case 0x3444:
                let preset = try v.byte()
                _ = try v.byte() // genre
                _ = try v.take(Int(v.byte())) // UTF-8 name is irrelevant to this guard
                let count = Int(try v.byte())
                var slots: [Slot] = []
                for _ in 0..<count { slots.append(try v.slot()) }
                guard Set(slots.map { $0.id }).count == slots.count else { throw Failure.malformedPacket }
                events.append(.detail(preset, slots))
            case 0x3445:
                let channel = try v.byte(), preset = try v.byte(), change = try v.byte()
                guard change <= 4 else { throw Failure.malformedPacket }
                events.append(.slot(channel, preset, change, try v.slot()))
            default: _ = try v.take(v.remaining)
            }
            guard v.remaining == 0 else { throw Failure.malformedPacket }
        }
        return events
    }
}
