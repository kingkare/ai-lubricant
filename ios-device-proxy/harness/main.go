// Command ios-proxy-harness is the server side of the "device-side proxy"
// feasibility spike (docs/ios-device-proxy-remote-control.md).
//
// The iOS app (ios-device-proxy/) is a dumb TCP relay that dials OUT to this
// harness and then byte-pipes between us and a port on the device's OWN network
// stack. This harness answers the question the spec asks: can go-ios reach a
// device's RemotePairing / RSD service through that relay, with no USB and no
// shared LAN?
//
// Two modes:
//
//	harness                 — probe mode (V1–V3). Waits for one app connection,
//	                          performs the RSD handshake over the pipe using
//	                          go-ios's exported API, prints the UDID + service
//	                          list, and optionally opens the untrusted tunnel
//	                          service (the V4 surface).
//	harness --bridge-listen — bridge mode. Opens a local TCP listener; every
//	                          connection is relayed to the device through the app.
//	                          An UNMODIFIED `ios` CLI can then drive V4–V7 through
//	                          it: the bridge is the "wire" go-ios thinks is a
//	                          local device. No fork of go-ios required.
//
// App control protocol (one newline-terminated JSON line each way):
//
//	app  → harness: {"hello":"device-proxy/1","ips":["192.168.1.23"]}
//	app  → harness: {"rsd_port":58783}      (follow-up once its mDNS browse
//	                                          resolves _remoted._tcp)
//	harness → app:  {"target_host":"192.168.1.23","target_port":58783}
//	app  → harness: {"ok":true}                       (or {"ok":false,"errno":..})
//	then: raw bytes, no framing, until either side closes.
//
// One session carries MANY probes: a failed target leaves the app connected and
// awaiting the next control line, so a single installed app answers the whole
// (host × port) matrix. Only a SUCCESSFUL relay consumes the session — the app
// enters its byte pipe, and when the harness closes it the app redials.
//
// Everything here uses go-ios's EXPORTED API only (v1.3.2): the RSD handshake is
// reachable over any io.ReadWriteCloser via http.NewHttpConnection +
// ios.CreateXpcConnection, which is the linchpin that makes this spike possible
// without patching go-ios.
package main

import (
	"bufio"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"log"
	"net"
	"strings"
	"time"

	ios "github.com/danielpaulus/go-ios/ios"
	"github.com/danielpaulus/go-ios/ios/http"
)

// rsdPortDefault is the port the device's Remote Service Discovery (RSD) service
// listens on, and the port the app relays to for V2/V3. It is the value the spec
// cites (58783); the app may also be told the mDNS-advertised port instead.
const rsdPortDefault = 58783

// untrustedTunnelService is the RSD service name of the CoreDevice untrusted
// tunnel service — the one pairing (V4) is performed against. Same constant as
// go-ios's tunnel package (untrusted.go).
const untrustedTunnelService = "com.apple.internal.dt.coredevice.untrusted.tunnelservice"

func main() {
	log.SetFlags(log.Ltime | log.Lmicroseconds)

	var (
		listenAddr   = flag.String("listen", ":9999", "address the harness listens on for the app's dial-out connection")
		bridgeListen = flag.String("bridge-listen", "", "if set, run as a local bridge for an unmodified `ios` CLI instead of the one-shot probe")
		bridgeTarget = flag.Int("bridge-target-port", rsdPortDefault, "device port the bridge relays every connection to")
		targetPort   = flag.Int("target-port", rsdPortDefault, "device port to relay to in probe mode (V2/V3 uses the RSD port)")
		deviceIP     = flag.String("device-ip", "", "device LAN IP to hand the app as target_host (default: use the first IP the app reports)")
		probePair    = flag.Bool("probe-pair", false, "after the RSD handshake, also open the untrusted tunnel service (V4 surface)")
		scan         = flag.Bool("scan", false, "scan mode: try EVERY app-reported IP (plus loopback) against the mDNS-discovered RSD port, RSD-handshake any that connects, print a per-IP verdict table")
		hostFlag     = flag.String("host", "", "restrict the scan to this single device IP (the one YOU picked); empty = every IP the app reports")
		rsdPortFlag  = flag.Int("rsd-port", 0, "explicit remoted/RSD port to probe (e.g. learned from a PC-side mDNS sighting); 0 = ask the app (on-device mDNS) and fall back to 58783")
		discoverWait = flag.Duration("discover-wait", 15*time.Second, "how long to wait for the app's async mDNS {\"rsd_port\":N} report before falling back to candidate ports")
		timeout      = flag.Duration("timeout", 30*time.Second, "per-connection I/O deadline")
	)
	flag.Parse()

	if *bridgeListen != "" {
		if err := runBridge(*listenAddr, *bridgeListen, *bridgeTarget, *deviceIP, *timeout); err != nil {
			log.Fatalf("bridge: %v", err)
		}
		return
	}
	if *scan {
		if err := runScan(*listenAddr, *hostFlag, *rsdPortFlag, *discoverWait, *timeout); err != nil {
			log.Fatalf("scan: %v", err)
		}
		return
	}
	if err := runProbe(*listenAddr, *targetPort, *deviceIP, *probePair, *timeout); err != nil {
		log.Fatalf("probe: %v", err)
	}
}

// runScan accepts ONE app connection and keeps it for the whole run. It reads
// the app's self-reported IPs, waits for the app's on-device mDNS port report,
// then probes each host over that single session: write a control line, read
// the ack, and on success run the REAL RSD handshake. A FAILED probe leaves the
// session alive (the app stays connected and awaits the next line); only a
// SUCCESSFUL relay consumes the session (the app enters its byte pipe), after
// which the app redials. One app install answers every host — no rebuild, no
// redial per combo, no blind port scan.
func runScan(listenAddr, hostFlag string, rsdPortFlag int, discoverWait, timeout time.Duration) error {
	ln, err := net.Listen("tcp", listenAddr)
	if err != nil {
		return fmt.Errorf("listen %s: %w", listenAddr, err)
	}
	defer ln.Close()
	log.Printf("[scan] waiting for the app on %s", ln.Addr())

	conn, err := ln.Accept()
	if err != nil {
		return err
	}
	log.Printf("[scan] app connected from %s", conn.RemoteAddr())

	sess := newAppSession(conn)
	defer sess.Close()

	hello, err := sess.readLine(timeout)
	if err != nil {
		return fmt.Errorf("read hello: %w", err)
	}
	if h, _ := hello["hello"].(string); h != "device-proxy/1" {
		return fmt.Errorf("unexpected hello %v", hello)
	}
	appIPs := toStrings(hello["ips"])
	log.Printf("[scan] app-reported addresses: %v", appIPs)

	// The port is NOT guessed. Priority: (1) an explicit -rsd-port (a port the
	// operator already knows), (2) the port the PHONE discovered by browsing its
	// own "_remoted._tcp" advertisement — the only place it is visible, since
	// the PC cannot see the phone's LAN mDNS — reported as {"rsd_port":N},
	// (3) fall back to the RSD port the spec cites.
	port := rsdPortFlag
	if port == 0 {
		port = sess.awaitRSDPort(discoverWait)
		if port > 0 {
			log.Printf("[scan] device-discovered remoted port: %d (on-device mDNS _remoted._tcp)", port)
		}
	} else {
		log.Printf("[scan] using explicit -rsd-port %d", port)
	}
	ports := []int{port}
	if port == 0 {
		port = rsdPortDefault
		ports = []int{port}
		log.Printf("[scan] WARNING: 无端口来源（未给 -rsd-port，App %v 内未上报 rsd_port：机内 mDNS 未解析，查本地网络权限/是否在广播），回退到规范 RSD 端口 %d", discoverWait, port)
	}

	hosts := append([]string{}, appIPs...)
	// The address the app DIALED IN FROM is the one address proven reachable
	// server→device→server; probe it first so a definitive verdict arrives before
	// the long tail of cellular/utun/awdl addresses.
	if ra, ok := conn.RemoteAddr().(*net.TCPAddr); ok && ra.IP != nil {
		dialFrom := ra.IP.String()
		if ra.IP.To4() == nil {
			dialFrom = ra.String() // keep scope if any
		}
		hosts = append([]string{dialFrom}, hosts...)
	}
	hosts = append(hosts, "127.0.0.1")
	if hostFlag != "" {
		hosts = []string{hostFlag}
	}

	type verdict struct {
		host string
		port int
		errn int
		msg  string
		ok   bool
		rsd  bool
	}
	var results []verdict

	for _, host := range hosts {
		for _, devPort := range ports {
			if sess.NeedsReaccept() {
				log.Printf("[scan] relay consumed; waiting for the app to redial…")
				// Never give up: the phone may be locked/suspended for minutes.
				// Keep waiting so ONE launch of the harness covers the whole test.
				for {
					if err := sess.Reaccept(ln, timeout); err == nil {
						break
					}
					log.Printf("[scan] redial wait timed out; still listening for the app…")
				}
			}
			log.Printf("[scan] → %-39s :%5d", host, devPort)
			pipeConn, err := sess.probe(host, devPort, timeout)
			if err != nil {
				errn, msg := parseErrno(err)
				results = append(results, verdict{host: host, port: devPort, errn: errn, msg: msg})
				log.Printf("[scan] %-39s :%5d  FAIL errno=%d %s", host, devPort, errn, msg)
				continue
			}
			// V2 OK — run the REAL protocol (V3 RSD handshake) over the pipe.
			hs, herr := rsdHandshake(pipeConn, timeout)
			if herr != nil {
				results = append(results, verdict{host: host, port: devPort, ok: true, msg: "tcp ok, RSD handshake failed: " + herr.Error()})
				log.Printf("[scan] %-39s :%5d  TCP OK, RSD FAIL: %v", host, devPort, herr)
			} else {
				results = append(results, verdict{host: host, port: devPort, ok: true, rsd: true, msg: fmt.Sprintf("udid=%s services=%d", hs.Udid, len(hs.Services))})
				log.Printf("[scan] %-39s :%5d  RSD OK udid=%s services=%d", host, devPort, hs.Udid, len(hs.Services))
				for name, entry := range hs.Services {
					log.Printf("        service %-62s port %d", name, entry.Port)
				}
			}
			// Whether the handshake completed or not, the relay carried raw
			// bytes — end the pipe so the app redials for the next probe.
			sess.EndPipe()
		}
	}

	log.Printf("========== SCAN SUMMARY ==========")
	anyOK := false
	for _, r := range results {
		state := "FAIL"
		if r.rsd {
			state = "RSD-OK"
			anyOK = true
		} else if r.ok {
			state = "TCP-OK"
			anyOK = true
		}
		log.Printf("  %-39s :%5d  %s  errno=%d  %s", r.host, r.port, state, r.errn, r.msg)
	}
	if !anyOK {
		log.Printf("  verdict: EVERY combo failed — only now may H be declared falsified at V2")
	}
	return nil
}

// appSession is ONE app connection carrying many probes. Control lines flow
// both ways; a successful probe turns the session into a raw byte pipe until
// the harness ends it and the app redials.
type appSession struct {
	conn net.Conn
	rd   *bufio.Reader

	rsdPort     int  // remoted port the app discovered on-device (mDNS); 0 = unknown
	pipeCarried bool // last probe succeeded: the session carried raw bytes
	pipeEnded   bool // that pipe has been closed; the app is redialing
	sessionDead bool // the app closed the connection (older app builds drop the session on a failed probe)
}

func newAppSession(conn net.Conn) *appSession {
	return &appSession{conn: conn, rd: bufio.NewReader(conn)}
}

// readLine reads one JSON control line. Lines that carry the app's async mDNS
// {"rsd_port":N} report are consumed transparently (recorded, not returned) —
// the report races with ack reads by design.
func (s *appSession) readLine(timeout time.Duration) (map[string]any, error) {
	_ = s.conn.SetDeadline(time.Now().Add(timeout))
	for {
		line, err := readJSONLine(s.rd)
		if err != nil {
			return nil, err
		}
		if p, ok := toIntValue(line["rsd_port"]); ok {
			if s.rsdPort != p {
				log.Printf("[scan] app-reported remoted port (on-device mDNS): %d", p)
			}
			s.rsdPort = p
			continue
		}
		return line, nil
	}
}

// awaitRSDPort blocks until the app reports the mDNS-discovered remoted port or
// the wait elapses.
func (s *appSession) awaitRSDPort(wait time.Duration) int {
	deadline := time.Now().Add(wait)
	for s.rsdPort == 0 && time.Now().Before(deadline) {
		if _, err := s.readLine(time.Until(deadline)); err != nil {
			break
		}
	}
	return s.rsdPort
}

// probe asks the app to relay to host:port over the live session. On success
// the returned net.Conn is the raw pipe (device side). On failure the session
// STAYS alive for the next probe — no redial, no re-accept.
func (s *appSession) probe(host string, port int, timeout time.Duration) (net.Conn, error) {
	_ = s.conn.SetDeadline(time.Now().Add(timeout))
	req, _ := json.Marshal(map[string]any{"target_host": host, "target_port": port})
	if _, err := s.conn.Write(append(req, '\n')); err != nil {
		s.sessionDead = true
		return nil, fmt.Errorf("write control: %w", err)
	}
	line, err := s.readLine(timeout)
	if err != nil {
		// A malformed ack is NOT a dead session — the app stays connected and
		// awaits the next control line (its v4 fail-line had a misplaced `}`).
		// Salvage the errno from the raw text and keep the session alive.
		if strings.Contains(err.Error(), "invalid JSON") {
			errn, msg := parseErrno(errors.New(err.Error()))
			return nil, fmt.Errorf("app could not connect to %s:%d (errno=%d %s) [malformed ack salvaged]", host, port, errn, msg)
		}
		s.sessionDead = true
		return nil, fmt.Errorf("read ack: %w", err)
	}
	if ok, _ := line["ok"].(bool); ok {
		s.pipeCarried = true
		s.pipeEnded = false
		_ = s.conn.SetDeadline(time.Time{})
		return s.rawConn(), nil
	}
	errno := int(toFloat(line["errno"]))
	// Older app builds (v3) close the session right after a failed ack and
	// redial. Peek briefly: an EOF marks the session dead so the next probe
	// re-accepts instead of burning a cycle writing into a closed socket.
	_ = s.conn.SetReadDeadline(time.Now().Add(300 * time.Millisecond))
	var one [1]byte
	if _, err := s.conn.Read(one[:]); err != nil && errors.Is(err, net.ErrClosed) || errors.Is(err, io.EOF) {
		s.sessionDead = true
	}
	_ = s.conn.SetDeadline(time.Time{})
	return nil, fmt.Errorf("app could not connect to %s:%d (errno=%d %v)", host, port, errno, line["error"])
}

// rawConn wraps the session conn so bytes buffered past the ack line (a device
// that speaks first) are delivered before fresh reads.
func (s *appSession) rawConn() net.Conn {
	if n := s.rd.Buffered(); n > 0 {
		buf := make([]byte, n)
		if _, err := io.ReadFull(s.rd, buf); err == nil {
			return &prefixConn{Conn: s.conn, prefix: buf}
		}
	}
	return s.conn
}

// EndPipe closes a consumed session so the app redials for the next probe.
func (s *appSession) EndPipe() {
	if !s.pipeCarried {
		return
	}
	_ = s.conn.Close()
	s.pipeEnded = true
}

// NeedsReaccept reports whether the session must be re-accepted before the next
// probe — because the last relay consumed it, or the app dropped the connection.
func (s *appSession) NeedsReaccept() bool { return (s.pipeCarried && s.pipeEnded) || s.sessionDead }

// Reaccept waits for the app's redial and re-reads its hello. The app
// re-reports its cached rsd_port immediately, so the port stays current. The
// redial window is long (2 min): iOS suspends a backgrounded app, so the phone
// may need to be unlocked / foregrounded before it reconnects.
func (s *appSession) Reaccept(ln net.Listener, timeout time.Duration) error {
	redialWindow := 2 * time.Minute
	log.Printf("[scan] waiting up to %v for the app to redial（请保持手机解锁、App 在前台）…", redialWindow)
	if t, ok := ln.(*net.TCPListener); ok {
		_ = t.SetDeadline(time.Now().Add(redialWindow))
		defer t.SetDeadline(time.Time{})
	}
	conn, err := ln.Accept()
	if err != nil {
		return err
	}
	s.conn = conn
	s.rd = bufio.NewReader(conn)
	s.pipeCarried = false
	s.pipeEnded = false
	s.sessionDead = false
	hello, err := s.readLine(timeout)
	if err != nil {
		return fmt.Errorf("read hello after redial: %w", err)
	}
	log.Printf("[scan] app reconnected from %s (ips=%v, rsd_port=%d)", conn.RemoteAddr(), toStrings(hello["ips"]), s.rsdPort)
	return nil
}

func (s *appSession) Close() {
	_ = s.conn.Close()
	s.pipeCarried = false
}

// parseErrno digs the errno out of negotiate's error text — either the harness
// form "errno=60" or the app's malformed-JSON form "errno\":60".
func parseErrno(err error) (int, string) {
	s := err.Error()
	for _, key := range []string{"errno=", `errno":`} {
		i := strings.Index(s, key)
		if i < 0 {
			continue
		}
		rest := s[i+len(key):]
		j := 0
		for j < len(rest) && rest[j] >= '0' && rest[j] <= '9' {
			j++
		}
		n := 0
		fmt.Sscanf(rest[:j], "%d", &n)
		msg := strings.TrimSpace(strings.TrimPrefix(rest[j:], " "))
		msg = strings.TrimSuffix(msg, `"}"`)
		msg = strings.TrimSuffix(msg, `"`)
		return n, msg
	}
	return 0, s
}

// runProbe accepts ONE app connection and drives the RSD handshake over it.
// This is the V1–V3 path: if the handshake returns a UDID and a service list,
// hypothesis H holds for the discovery half.
func runProbe(listenAddr string, targetPort int, deviceIP string, probePair bool, timeout time.Duration) error {
	ln, err := net.Listen("tcp", listenAddr)
	if err != nil {
		return fmt.Errorf("listen %s: %w", listenAddr, err)
	}
	defer ln.Close()
	log.Printf("[V1] waiting for the app to dial out on %s", ln.Addr())

	conn, err := ln.Accept()
	if err != nil {
		return err
	}
	defer conn.Close()
	log.Printf("[V1] app connected from %s", conn.RemoteAddr())

	info, err := negotiate(conn, targetPort, deviceIP, timeout)
	if err != nil {
		log.Printf("[V2] relay negotiation FAILED: %v", err)
		log.Printf("[V2] verdict: H is FALSIFIED at V2 — the app could not reach the device's own port %d", targetPort)
		return err
	}
	log.Printf("[V2] relay established → device %s:%d (app-reported IPs: %v)", info.Host, info.Port, info.IPs)

	// The pipe is now transparent to the device's RSD service. Perform the RSD
	// handshake over it (V3) using go-ios's own HTTP/2 + XPC stack.
	handshake, err := rsdHandshake(info.Conn, timeout)
	if err != nil {
		log.Printf("[V3] RSD handshake FAILED: %v", err)
		log.Printf("[V3] verdict: relay works (V2 ok) but the RSD protocol did not complete — likely a wrong target port, not a sandbox block")
		return err
	}
	log.Printf("[V3] RSD handshake OK: udid=%s services=%d", handshake.Udid, len(handshake.Services))
	for name, entry := range handshake.Services {
		log.Printf("        service %-62s port %d", name, entry.Port)
	}
	if _, ok := handshake.Services[untrustedTunnelService]; ok {
		log.Printf("[V3] untrusted tunnel service IS advertised → V4 (pair-verify) is reachable")
	} else {
		log.Printf("[V3] WARNING: untrusted tunnel service NOT advertised; pairing (V4) may not be reachable on this port")
	}

	if !probePair {
		log.Printf("[V3] verdict: H HOLDS for V1–V3. Run with -probe-pair to open the tunnel service (V4 surface).")
		return nil
	}

	// V4 surface: open the untrusted tunnel service over a SECOND app connection.
	// The app carries exactly one device connection per server connection and only
	// redials after the first closes, so close the RSD relay before asking for the
	// tunnel one — the listener stays open across both.
	entry, ok := handshake.Services[untrustedTunnelService]
	if !ok {
		return errors.New("untrusted tunnel service not advertised")
	}
	_ = conn.Close()
	log.Printf("[V4] untrusted tunnel service is on port %d; waiting for the app to redial …", entry.Port)
	conn2, err := dialRelay(ln, info.Host, int(entry.Port), deviceIP, timeout)
	if err != nil {
		log.Printf("[V4] tunnel service relay FAILED: %v", err)
		return err
	}
	defer conn2.Close()
	// Confirm the device accepts the RSD checkin on the tunnel service — this is
	// the exact precondition go-ios's ManualPair runs against.
	if err := ios.RsdCheckin(conn2); err != nil {
		log.Printf("[V4] tunnel service RSD checkin FAILED: %v", err)
		return err
	}
	log.Printf("[V4] tunnel service RSD checkin ACCEPTED — the pairing surface is reachable through the relay")
	log.Printf("[V4] verdict: H HOLDS for V1–V4 plumbing. The pair-verify/Trust prompt itself needs the go-ios tunnel package (use -bridge-listen + `ios` CLI).")
	return nil
}

// runBridge exposes a local listener that relays each connection to a device
// port through the app, so an unmodified `ios` CLI can drive V4–V7. Each local
// connection consumes one fresh app relay (the app reconnects after each).
func runBridge(listenAddr, bridgeListen string, targetPort int, deviceIP string, timeout time.Duration) error {
	appLn, err := net.Listen("tcp", listenAddr)
	if err != nil {
		return fmt.Errorf("app listen %s: %w", listenAddr, err)
	}
	defer appLn.Close()
	brLn, err := net.Listen("tcp", bridgeListen)
	if err != nil {
		return fmt.Errorf("bridge listen %s: %w", bridgeListen, err)
	}
	defer brLn.Close()
	log.Printf("bridge ready: `ios` CLI dials %s; the app dials %s (device port %d)", brLn.Addr(), appLn.Addr(), targetPort)

	appCh := make(chan net.Conn, 8)
	go func() {
		for {
			c, err := appLn.Accept()
			if err != nil {
				close(appCh)
				return
			}
			log.Printf("app connected from %s", c.RemoteAddr())
			appCh <- c
		}
	}()

	for {
		local, err := brLn.Accept()
		if err != nil {
			return err
		}
		log.Printf("bridge connection from %s", local.RemoteAddr())
		go func(local net.Conn) {
			defer local.Close()
			var app net.Conn
			select {
			case app = <-appCh:
			case <-time.After(timeout):
				log.Printf("bridge: no app connection available within %s", timeout)
				return
			}
			defer app.Close()
			info, err := negotiate(app, targetPort, deviceIP, timeout)
			if err != nil {
				log.Printf("bridge: relay negotiation failed: %v", err)
				return
			}
			pipe(local, info.Conn)
		}(local)
	}
}

// relayInfo is what negotiate learned about the relay target. Conn is the
// relay connection ready for raw piping; any bytes the control-line reader
// buffered past the ack (a device that speaks first) are preserved.
type relayInfo struct {
	Host string
	Port int
	IPs  []string
	Conn net.Conn
}

// negotiate performs the one-line control handshake with the app and returns the
// connection ready for raw byte piping to the device. targetPort is the device
// port to reach; deviceIP overrides the app-reported IP when non-empty.
func negotiate(conn net.Conn, targetPort int, deviceIP string, timeout time.Duration) (*relayInfo, error) {
	_ = conn.SetDeadline(time.Now().Add(timeout))
	rd := bufio.NewReader(conn)

	hello, err := readJSONLine(rd)
	if err != nil {
		return nil, fmt.Errorf("read hello: %w", err)
	}
	ips := toStrings(hello["ips"])
	host := strings.TrimSpace(deviceIP)
	if host == "" {
		host = firstNonLoopback(ips)
	}
	if host == "" {
		return nil, errors.New("app reported no usable IP and -device-ip was not set")
	}

	req, _ := json.Marshal(map[string]any{"target_host": host, "target_port": targetPort})
	if _, err := conn.Write(append(req, '\n')); err != nil {
		return nil, fmt.Errorf("write control: %w", err)
	}
	// The app may push an async {"rsd_port":N} mDNS report before the ack — skip
	// any such follow-up lines and read until the real ack arrives.
	var resp map[string]any
	for {
		resp, err = readJSONLine(rd)
		if err != nil {
			return nil, fmt.Errorf("read ack: %w", err)
		}
		if _, isPort := toIntValue(resp["rsd_port"]); !isPort {
			break
		}
	}
	if ok, _ := resp["ok"].(bool); !ok {
		errno := int(toFloat(resp["errno"]))
		return nil, fmt.Errorf("app could not connect to %s:%d (errno=%d %v)", host, targetPort, errno, resp["error"])
	}
	_ = conn.SetDeadline(time.Time{})
	// bufio may have read bytes past the ack line — a device that speaks first
	// (RSD's server can) would lose them if we dropped the buffer. Wrap the conn
	// so the pipe drains those bytes first.
	return &relayInfo{Host: host, Port: targetPort, IPs: ips, Conn: newPrefixConn(conn, rd)}, nil
}

// dialRelay accepts the next app connection off ln and negotiates a relay to
// host:port. The app redials after each relay, so the caller keeps ln open
// across relays. Used by the probe's optional V4 step.
func dialRelay(ln net.Listener, host string, port int, deviceIP string, timeout time.Duration) (net.Conn, error) {
	if t, ok := ln.(*net.TCPListener); ok {
		_ = t.SetDeadline(time.Now().Add(timeout))
	}
	conn, err := ln.Accept()
	if err != nil {
		return nil, err
	}
	info, err := negotiate(conn, port, deviceIP, timeout)
	if err != nil {
		_ = conn.Close()
		return nil, err
	}
	return info.Conn, nil
}

// rsdHandshake performs the Remote Service Discovery handshake over conn using
// go-ios's own HTTP/2 + XPC stack — the same code path go-ios uses for a real
// device, just fed our relay pipe instead of a socket it dialed itself.
func rsdHandshake(conn net.Conn, timeout time.Duration) (ios.RsdHandshakeResponse, error) {
	// http.NewHttpConnection writes the HTTP/2 client preface and settings; it
	// needs a ReadWriteCloser, which a net.Conn is.
	_ = conn.SetDeadline(time.Now().Add(timeout))
	defer conn.SetDeadline(time.Time{})
	h, err := http.NewHttpConnection(conn)
	if err != nil {
		return ios.RsdHandshakeResponse{}, fmt.Errorf("http2 init: %w", err)
	}
	xpcConn, err := ios.CreateXpcConnection(h)
	if err != nil {
		return ios.RsdHandshakeResponse{}, fmt.Errorf("xpc init: %w", err)
	}
	_ = timeout
	m, err := xpcConn.ReceiveOnClientServerStream()
	if err != nil {
		return ios.RsdHandshakeResponse{}, fmt.Errorf("handshake read: %w", err)
	}
	return parseHandshake(m)
}

// parseHandshake turns the raw RSD handshake XPC message into the typed
// response. Mirrors go-ios's RsdService.Handshake parsing (ios/rsd.go).
func parseHandshake(m map[string]interface{}) (ios.RsdHandshakeResponse, error) {
	udid := ""
	if props, ok := m["Properties"].(map[string]interface{}); ok {
		if u, ok := props["UniqueDeviceID"].(string); ok {
			udid = u
		}
	}
	if udid == "" {
		return ios.RsdHandshakeResponse{}, errors.New("handshake: no UniqueDeviceID (not an RSD service?)")
	}
	if mt, _ := m["MessageType"].(string); mt != "Handshake" {
		return ios.RsdHandshakeResponse{}, fmt.Errorf("handshake: unexpected MessageType %q", mt)
	}
	rawServices, ok := m["Services"].(map[string]interface{})
	if !ok {
		return ios.RsdHandshakeResponse{}, errors.New("handshake: no Services map")
	}
	out := make(map[string]ios.RsdServiceEntry, len(rawServices))
	for name, raw := range rawServices {
		entry, ok := raw.(map[string]interface{})
		if !ok {
			continue
		}
		portStr, _ := entry["Port"].(string)
		var port int
		if _, err := fmt.Sscanf(portStr, "%d", &port); err != nil {
			continue
		}
		out[name] = ios.RsdServiceEntry{Port: uint32(port)}
	}
	return ios.RsdHandshakeResponse{Udid: udid, Services: out}, nil
}

// readJSONLine reads one newline-terminated JSON object.
func readJSONLine(rd *bufio.Reader) (map[string]any, error) {
	line, err := rd.ReadString('\n')
	if err != nil {
		return nil, err
	}
	out := map[string]any{}
	if err := json.Unmarshal([]byte(strings.TrimSpace(line)), &out); err != nil {
		return nil, fmt.Errorf("invalid JSON %q: %w", strings.TrimSpace(line), err)
	}
	return out, nil
}

// pipe copies bytes in both directions until either side closes.
func pipe(a, b net.Conn) {
	done := make(chan struct{}, 2)
	go func() { _, _ = io.Copy(a, b); done <- struct{}{} }()
	go func() { _, _ = io.Copy(b, a); done <- struct{}{} }()
	<-done
	_ = a.Close()
	_ = b.Close()
	<-done
}

// prefixConn is a net.Conn whose first Read returns bytes already buffered by
// the control-line reader before falling through to the underlying conn. This
// preserves any bytes the device sent immediately after the app's ack line.
type prefixConn struct {
	net.Conn
	prefix []byte
}

func newPrefixConn(conn net.Conn, rd *bufio.Reader) net.Conn {
	if n := rd.Buffered(); n > 0 {
		buf := make([]byte, n)
		if _, err := io.ReadFull(rd, buf); err == nil {
			return &prefixConn{Conn: conn, prefix: buf}
		}
	}
	return conn
}

func (c *prefixConn) Read(p []byte) (int, error) {
	if len(c.prefix) > 0 {
		n := copy(p, c.prefix)
		c.prefix = c.prefix[n:]
		return n, nil
	}
	return c.Conn.Read(p)
}

func toStrings(v any) []string {
	raw, _ := v.([]any)
	out := make([]string, 0, len(raw))
	for _, r := range raw {
		if s, ok := r.(string); ok {
			out = append(out, s)
		}
	}
	return out
}

func toFloat(v any) float64 {
	if f, ok := v.(float64); ok {
		return f
	}
	return 0
}

// toIntValue reads a JSON number (decoded as float64) or numeric string into an
// int, reporting whether it was present and parseable.
func toIntValue(v any) (int, bool) {
	switch n := v.(type) {
	case float64:
		return int(n), n > 0
	case int:
		return n, n > 0
	case string:
		var i int
		if _, err := fmt.Sscanf(n, "%d", &i); err == nil && i > 0 {
			return i, true
		}
	}
	return 0, false
}

func firstNonLoopback(ips []string) string {
	for _, ip := range ips {
		if ip != "" && !strings.HasPrefix(ip, "127.") {
			return ip
		}
	}
	if len(ips) > 0 {
		return ips[0]
	}
	return ""
}
