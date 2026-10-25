import Foundation
import Darwin

/// TCPProxy — dials OUT to a remote control server, then acts as a raw byte pipe
/// between that server and a port on THIS device's own network stack
/// (e.g. remoted/RSD on 58783 or testmanagerd). No entitlement required.
///
/// Control protocol — one device-side TCP connection carries MANY probes:
///   1. connect TCP to serverHost:serverPort
///   2. emit  {"hello":"device-proxy/1","ips":["<ip1>",...]}\n
///   3. (async) once the on-device mDNS browse of "_remoted._tcp" resolves, emit
///      {"rsd_port":<int>}\n — so the server LEARNS the port instead of guessing
///   4. read  {"target_host":"<ip>","target_port":<int>}\n   (repeat per probe)
///   5. connect target; emit {"ok":true}\n then a raw byte pipe until the server
///      ends it (→ step 1 redial); on failure emit {"ok":false,"errno":<n>,...}\n
///      and STAY CONNECTED for the next probe (a failed target does NOT drop the
///      session — the server answers the whole (host×port) matrix in one install)
final class TCPProxy {

    private let EV_IN = Int16(POLLIN), EV_OUT = Int16(POLLOUT)
    private let EV_HUP = Int16(POLLHUP), EV_ERR = Int16(POLLERR)
    private let connectTimeoutSec: Int32 = 10
    private let reconnectDelaySec: UInt32 = 2
    private let ioBufSize = 65536

    private let lock = NSLock()
    private let sessionWriteLock = NSLock()   // serializes control-line writes (ack + rsd_port)
    private var stopped = true
    private var serverFD: Int32 = -1
    private var deviceFD: Int32 = -1
    private var cachedRSDPort: Int?          // mDNS-discovered remoted port (persists across redials)
    private var discoveryStarted = false      // one browse per app process
    private var piping = false               // session is carrying raw bytes — no control lines now
    private let queue = DispatchQueue(label: "device-proxy.loop")
    private var log: (String) -> Void = { _ in }
    private var serverHost = ""
    private var serverPort: UInt16 = 0

    // MARK: - Public API

    func start(serverHost: String, serverPort: UInt16, onLog: @escaping (String) -> Void) {
        self.log = onLog
        self.serverHost = serverHost
        self.serverPort = serverPort
        lock.lock(); stopped = false; lock.unlock()
        queue.async { [weak self] in self?.runLoop() }
    }

    func stop() {
        lock.lock()
        stopped = true
        let s = serverFD, d = deviceFD
        serverFD = -1; deviceFD = -1
        lock.unlock()
        // Closing the fds unblocks any in-flight read()/poll() on the loop thread.
        if s >= 0 { close(s) }
        if d >= 0 { close(d) }
    }

    // MARK: - Reconnect loop

    private func runLoop() {
        while !isStopped() {
            let (sfd, serr) = connectTCP(host: serverHost, port: serverPort, timeoutSec: connectTimeoutSec)
            if sfd < 0 {
                log("服务器连接失败 errno=\(serr) (\(errnoText(serr)))，\(reconnectDelaySec)s 后重试")
                sleep(reconnectDelaySec); continue
            }
            storeServer(sfd); setBlocking(sfd, true)

            // hello — advertise our own IPv4s immediately. The remoted port is
            // discovered ASYNC (mDNS browse+resolve can take >5s on first try) and
            // reported with a follow-up {"rsd_port":N} line once known — never
            // block the session on it.
            let ips = TCPProxy.deviceAddresses()
            log("已连接服务器 \(serverHost):\(serverPort)，本机地址: [\(ips.joined(separator: ", "))]")
            let hello = "{\"hello\":\"device-proxy/1\",\"ips\":[\(ips.map { "\"\($0)\"" }.joined(separator: ","))]}\n"
            if !writeAll(sfd, hello) { log("发送 hello 失败，重连"); closeSession(sfd, -1); continue }
            log("→ \(hello.trimmingCharacters(in: .newlines))")

            // Kick off (or reuse) the mDNS discovery; when it finds the port,
            // push {"rsd_port":N} to the server (it races with control reads, so
            // writes on this session are serialized by sessionWriteLock).
            if cachedRSDPort == nil && !discoveryStarted {
                discoveryStarted = true
                TCPProxy.discoverRSDPortAsync(timeoutSec: 15) { [weak self] port in
                    guard let self = self else { return }
                    guard let p = port else {
                        self.log("mDNS 未发现 _remoted._tcp（超时）— 请检查「本地网络」权限")
                        return
                    }
                    self.cachedRSDPort = p
                    let line = "{\"rsd_port\":\(p)}\n"
                    if let s = self.snapshotServer() {
                        self.sessionWriteLock.lock()
                        if !self.piping { _ = self.writeAll(s, line) }
                        self.sessionWriteLock.unlock()
                        if self.piping { self.log("mDNS 发现 remoted 端口 \(p)（当前在管道内，跳过上报）") }
                        else { self.log("→ mDNS 发现 remoted 端口 \(p)") }
                    }
                }
            } else if let p = cachedRSDPort {
                _ = writeAll(sfd, "{\"rsd_port\":\(p)}\n")
            }

            // Control loop — stay on ONE connection across probe attempts. A
            // FAILED target does NOT drop the session (the server scans many
            // combos; reconnecting per combo hung when iOS suspended the app
            // mid-scan). Only a closed session or a finished pipe redials.
            while !isStopped() {
                guard let ctlData = readLine(sfd) else { log("服务端断开，重连"); break }
                log("← \(String(decoding: ctlData, as: UTF8.self))")
                guard let obj = try? JSONSerialization.jsonObject(with: ctlData) as? [String: Any],
                      let tHost = obj["target_host"] as? String,
                      let tPortNum = obj["target_port"] as? NSNumber else {
                    log("控制行格式错误，忽略")
                    continue
                }
                let tPort = UInt16(truncating: tPortNum)
                log("目标 = \(tHost):\(tPort)，尝试连接…")

                let (dfd, derr) = connectTCP(host: tHost, port: tPort, timeoutSec: connectTimeoutSec)
                if dfd < 0 {
                    let fail = "{\"ok\":false,\"errno\":\(derr),\"error\":\"\(escape(errnoText(derr)))\"}\n"
                    sessionWriteLock.lock()
                    _ = writeAll(sfd, fail)
                    sessionWriteLock.unlock()
                    log("目标连接失败 errno=\(derr) (\(errnoText(derr))) → \(fail.trimmingCharacters(in: .newlines))")
                    continue   // stay on the session; await next probe
                }
                storeDevice(dfd)
                sessionWriteLock.lock()
                _ = writeAll(sfd, "{\"ok\":true}\n")
                sessionWriteLock.unlock()
                log("目标连接成功 → {\"ok\":true}，进入字节管道")

                lock.lock(); piping = true; lock.unlock()
                pipe(sfd, dfd)
                lock.lock(); piping = false; lock.unlock()
                log("管道关闭，结束本次 relay")
                lock.lock(); if deviceFD == dfd { deviceFD = -1 }; lock.unlock()
                close(dfd)
                break          // server ends the pipe session → redial for next
            }
            closeSession(sfd, -1)
            if !isStopped() { sleep(reconnectDelaySec) }
        }
        log("代理已停止")
    }

    // MARK: - Byte pipe (poll-based, non-blocking, partial-write safe)

    private func pipe(_ a: Int32, _ b: Int32) {
        setBlocking(a, false); setBlocking(b, false)
        var toB = [UInt8](), toA = [UInt8]()   // pending: server→device, device→server
        var eofA = false, eofB = false
        while !isStopped() && !eofA && !eofB {
            var aEv = EV_IN, bEv = EV_IN
            if !toA.isEmpty { aEv |= EV_OUT }
            if !toB.isEmpty { bEv |= EV_OUT }
            var fds = [pollfd(fd: a, events: aEv, revents: 0), pollfd(fd: b, events: bEv, revents: 0)]
            let pr = poll(&fds, 2, 1000)
            if isStopped() { return }
            if pr < 0 { if errno == EINTR { continue }; break }
            if pr == 0 { continue }
            if fds[0].revents & EV_IN != 0, !readInto(a, &toB) { eofA = true }
            if fds[1].revents & EV_IN != 0, !readInto(b, &toA) { eofB = true }
            if !toB.isEmpty, fds[1].revents & EV_OUT != 0, !writeFrom(b, &toB) { eofB = true }
            if !toA.isEmpty, fds[0].revents & EV_OUT != 0, !writeFrom(a, &toA) { eofA = true }
            if fds[0].revents & (EV_HUP | EV_ERR) != 0, toA.isEmpty { eofA = true }
            if fds[1].revents & (EV_HUP | EV_ERR) != 0, toB.isEmpty { eofB = true }
        }
    }

    /// Read available bytes into buf; false on EOF/hard error.
    private func readInto(_ fd: Int32, _ buf: inout [UInt8]) -> Bool {
        var tmp = [UInt8](repeating: 0, count: ioBufSize)
        let n = read(fd, &tmp, tmp.count)
        if n > 0 { buf.append(contentsOf: tmp[0..<n]); return true }
        return n < 0 && (errno == EAGAIN || errno == EINTR)
    }

    /// Write as much of buf as possible; false on hard error.
    private func writeFrom(_ fd: Int32, _ buf: inout [UInt8]) -> Bool {
        let n = buf.withUnsafeBytes { Darwin.write(fd, $0.baseAddress, buf.count) }
        if n > 0 { buf.removeFirst(n); return true }
        return n < 0 && (errno == EAGAIN || errno == EINTR)
    }

    // MARK: - BSD socket helpers

    /// Non-blocking connect with a timeout. Returns (fd, 0) on success, or
    /// (-1, errno) on failure — errno is the real POSIX error from connect()
    /// (recovered via SO_ERROR), the spike's key output. Supports IPv4 AND
    /// IPv6 targets (go-ios's RSD path is IPv6-first); scoped IPv6 literals
    /// ("fe80::1%en0") are parsed by getaddrinfo on Darwin.
    private func connectTCP(host: String, port: UInt16, timeoutSec: Int32) -> (Int32, Int32) {
        var hints = addrinfo()
        hints.ai_family = AF_UNSPEC
        hints.ai_socktype = SOCK_STREAM
        hints.ai_protocol = IPPROTO_TCP
        var res: UnsafeMutablePointer<addrinfo>?
        let gr = getaddrinfo(host, String(port), &hints, &res)
        guard gr == 0, let first = res else { return (-1, Int32(gr)) }  // EAI_* (name resolution)
        defer { freeaddrinfo(res) }

        var lastErr: Int32 = ECONNREFUSED
        var ai: UnsafeMutablePointer<addrinfo>? = first
        while let cur = ai {
            let fd = socket(cur.pointee.ai_family, cur.pointee.ai_socktype, cur.pointee.ai_protocol)
            if fd < 0 { lastErr = errno; ai = cur.pointee.ai_next; continue }
            let flags = fcntl(fd, F_GETFL, 0)
            _ = fcntl(fd, F_SETFL, flags | O_NONBLOCK)
            if Darwin.connect(fd, cur.pointee.ai_addr, cur.pointee.ai_addrlen) == 0 { return (fd, 0) }
            if errno != EINPROGRESS { lastErr = errno; close(fd); ai = cur.pointee.ai_next; continue }
            var pfd = pollfd(fd: fd, events: EV_OUT, revents: 0)
            let pr = poll(&pfd, 1, timeoutSec * 1000)
            if pr <= 0 { lastErr = (pr == 0 ? ETIMEDOUT : errno); close(fd); ai = cur.pointee.ai_next; continue }
            var soErr: Int32 = 0
            var len = socklen_t(MemoryLayout<Int32>.size)
            getsockopt(fd, SOL_SOCKET, SO_ERROR, &soErr, &len)
            if soErr != 0 { lastErr = soErr; close(fd); ai = cur.pointee.ai_next; continue }
            return (fd, 0)
        }
        return (-1, lastErr)
    }

    /// Blocking read of exactly one '\n'-terminated line (control lines are tiny).
    private func readLine(_ fd: Int32) -> Data? {
        var out = [UInt8]()
        var one = [UInt8](repeating: 0, count: 1)
        while out.count < 1 << 16 {
            let n = read(fd, &one, 1)
            if n == 1 {
                if one[0] == 0x0A { return Data(out) }
                out.append(one[0])
            } else if n == 0 {
                return out.isEmpty ? nil : Data(out)
            } else if errno == EINTR {
                continue
            } else {
                return nil
            }
        }
        return Data(out)
    }

    /// Blocking write-all (used for handshake lines on a blocking fd).
    private func writeAll(_ fd: Int32, _ s: String) -> Bool {
        let bytes = Array(s.utf8)
        var off = 0
        while off < bytes.count {
            let n = bytes.withUnsafeBytes { Darwin.write(fd, $0.baseAddress!.advanced(by: off), bytes.count - off) }
            if n > 0 { off += n } else if n < 0 && errno == EINTR { continue } else { return false }
        }
        return true
    }

    private func setBlocking(_ fd: Int32, _ blocking: Bool) {
        let flags = fcntl(fd, F_GETFL, 0)
        guard flags >= 0 else { return }
        _ = fcntl(fd, F_SETFL, blocking ? (flags & ~O_NONBLOCK) : (flags | O_NONBLOCK))
    }

    /// Active, non-loopback addresses of this device for the server to pick a
    /// target_host from. Includes BOTH IPv4 and IPv6 (go-ios's RSD path dials
    /// the device's scoped IPv6, e.g. "fe80::…%en0"); IPv6 link-local literals
    /// are emitted with their interface name as the scope, exactly the form
    /// getaddrinfo on this same device can redial.
    static func deviceAddresses() -> [String] {
        var head: UnsafeMutablePointer<ifaddrs>?
        guard getifaddrs(&head) == 0, let first = head else { return [] }
        defer { freeifaddrs(head) }
        var result: [String] = []
        var ptr: UnsafeMutablePointer<ifaddrs>? = first
        while let cur = ptr {
            let ifa = cur.pointee
            if let sa = ifa.ifa_addr {
                let fam = Int32(sa.pointee.sa_family)
                if fam == AF_INET || fam == AF_INET6 {
                    var host = [CChar](repeating: 0, count: Int(NI_MAXHOST))
                    if getnameinfo(sa, socklen_t(sa.pointee.sa_len), &host, socklen_t(host.count),
                                   nil, 0, NI_NUMERICHOST) == 0 {
                        let ip = String(cString: host)
                        let iface = String(cString: ifa.ifa_name)
                        if !ip.hasPrefix("127.") && ip != "::1" && ip != "fe80::1%lo0" {
                            // Link-local IPv6 needs a scope for getaddrinfo to dial it
                            // back from this device; if getnameinfo didn't add one, tag it.
                            if ip.contains("::") && !ip.contains("%") {
                                result.append("\(ip)%\(iface)")
                            } else {
                                result.append(ip)
                            }
                        }
                    }
                }
            }
            ptr = ifa.ifa_next
        }
        return result
    }

    /// Discover the remoted (RSD) port via mDNS "_remoted._tcp". This runs
    /// ASYNC: discovery can take >5s on the first attempt, so the caller
    /// proceeds and gets notified via `onResolved` when the port is known
    /// (and reuses it across redials via `cachedRSDPort`).
    static func discoverRSDPortAsync(timeoutSec: Int, onResolved: @escaping (Int?) -> Void) {
        DispatchQueue.global().async {
            let resolver = RSDResolver()
            resolver.start(timeoutSec: timeoutSec)
            onResolved(resolver.port)
        }
    }

    /// Foundation NetService browse + resolve for "_remoted._tcp". The port is
    /// only known after a service resolves, so a semaphore gates the wait; the
    /// first resolved service wins. resolve uses the FULL remaining budget (the
    /// old fixed 2s cap was the reason an on-device advertisement that WAS found
    /// still reported rsd_port=0), and each service gets one retry.
    private final class RSDResolver: NSObject, NetServiceBrowserDelegate, NetServiceDelegate {
        private let browser = NetServiceBrowser()
        private let semaphore = DispatchSemaphore(value: 0)
        private var services: [NetService] = []   // retain until resolved
        private var retried = Set<ObjectIdentifier>() // one retry per service
        private var signalled = false
        private var foundCount = 0                 // didFind fired at all? distinguishes "browse empty" from "browse saw N, none resolved"
        private var deadline = Date.distantFuture
        private(set) var port: Int?

        func start(timeoutSec: Int) {
            // NetService/NetServiceBrowser deliver delegate callbacks on the run
            // loop of the thread that started the browse. runLoop() runs on a GCD
            // queue (no run loop), so drive the browser from the MAIN run loop and
            // wait on this background thread with the semaphore.
            deadline = Date().addingTimeInterval(TimeInterval(timeoutSec))
            let sem = semaphore
            DispatchQueue.main.async { [weak self] in
                guard let self = self else { sem.signal(); return }
                self.browser.delegate = self
                self.browser.searchForServices(ofType: "_remoted._tcp", inDomain: "local.")
            }
            _ = sem.wait(timeout: .now() + .seconds(timeoutSec))
            DispatchQueue.main.async { [weak self] in
                guard let self = self else { return }
                self.browser.stop()
                for s in self.services { s.delegate = nil }
            }
        }

        /// Remaining resolve budget — never less than 1s.
        private var budget: TimeInterval {
            max(1, deadline.timeIntervalSinceNow)
        }

        func netServiceBrowser(_ b: NetServiceBrowser, didFind service: NetService, moreComing: Bool) {
            if port != nil { return }
            foundCount += 1
            services.append(service)
            service.delegate = self
            service.resolve(withTimeout: budget)
        }

        func netServiceDidResolveAddress(_ sender: NetService) {
            guard sender.port > 0 else { return }
            port = Int(sender.port)
            signalOnce()
        }

        func netService(_ sender: NetService, didNotResolve errorDict: [String: NSNumber]) {
            // One retry per service with the remaining budget, then move on.
            let id = ObjectIdentifier(sender)
            if retried.insert(id).inserted && budget > 1 {
                sender.resolve(withTimeout: budget)
                return
            }
            services.removeAll { $0 === sender }
            if !moreToResolve() && port == nil { signalOnce() }
        }

        private func moreToResolve() -> Bool {
            for s in services where s.port <= 0 && !retried.contains(ObjectIdentifier(s)) {
                s.delegate = self
                s.resolve(withTimeout: budget)
                return true
            }
            return false
        }

        private func signalOnce() {
            if signalled { return }
            signalled = true
            semaphore.signal()
        }
    }
    // MARK: - Small helpers

    private func storeServer(_ fd: Int32) { lock.lock(); serverFD = fd; lock.unlock() }
    private func storeDevice(_ fd: Int32) { lock.lock(); deviceFD = fd; lock.unlock() }
    private func isStopped() -> Bool { lock.lock(); defer { lock.unlock() }; return stopped }
    private func snapshotServer() -> Int32? {
        lock.lock(); defer { lock.unlock() }
        return serverFD >= 0 ? serverFD : nil
    }

    private func closeSession(_ s: Int32, _ d: Int32) {
        lock.lock()
        if serverFD == s { serverFD = -1 }
        if deviceFD == d { deviceFD = -1 }
        lock.unlock()
        if s >= 0 { close(s) }
        if d >= 0 { close(d) }
    }

    private func errnoText(_ e: Int32) -> String {
        guard let c = strerror(e) else { return "errno \(e)" }
        return String(cString: c)
    }

    private func escape(_ s: String) -> String {
        s.replacingOccurrences(of: "\\", with: "\\\\").replacingOccurrences(of: "\"", with: "\\\"")
    }
}
