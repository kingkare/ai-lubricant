package main

import (
	"bufio"
	"encoding/json"
	"net"
	"testing"
	"time"
)

// fakeApp plays the iOS app's side of the control protocol over a real TCP
// connection, so negotiate/pipe can be exercised without a phone.
func fakeApp(t *testing.T, ln net.Listener, ips []string, ok bool, errno int) {
	t.Helper()
	conn, err := ln.Accept()
	if err != nil {
		t.Errorf("fakeApp accept: %v", err)
		return
	}
	defer conn.Close()
	rd := bufio.NewReader(conn)
	hello, _ := json.Marshal(map[string]any{"hello": "device-proxy/1", "ips": ips})
	if _, err := conn.Write(append(hello, '\n')); err != nil {
		t.Errorf("fakeApp write hello: %v", err)
		return
	}
	// Read the control line the harness sends.
	line, err := rd.ReadString('\n')
	if err != nil {
		t.Errorf("fakeApp read control: %v", err)
		return
	}
	ctrl := map[string]any{}
	if err := json.Unmarshal([]byte(line), &ctrl); err != nil {
		t.Errorf("fakeApp parse control: %v", err)
		return
	}
	if ctrl["target_host"] == "" || ctrl["target_port"] == nil {
		t.Errorf("control line missing target: %v", ctrl)
	}
	// Ack.
	ack := map[string]any{"ok": ok}
	if !ok {
		ack["errno"] = errno
		ack["error"] = "connection refused"
	}
	b, _ := json.Marshal(ack)
	if _, err := conn.Write(append(b, '\n')); err != nil {
		t.Errorf("fakeApp write ack: %v", err)
		return
	}
	if !ok {
		return
	}
	// Byte-pipe: echo anything the harness writes (simulating the device).
	_, _ = ioCopy(conn, conn)
}

func ioCopy(dst, src net.Conn) (int64, error) {
	buf := make([]byte, 1024)
	var total int64
	for {
		n, err := src.Read(buf)
		if n > 0 {
			if _, werr := dst.Write(buf[:n]); werr != nil {
				return total, werr
			}
			total += int64(n)
		}
		if err != nil {
			return total, err
		}
	}
}

func TestNegotiateHappyPath(t *testing.T) {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer ln.Close()
	go fakeApp(t, ln, []string{"127.0.0.1", "192.168.1.23"}, true, 0)

	// Harness side dials the listener as if it were the app.
	conn, err := net.Dial("tcp", ln.Addr().String())
	if err != nil {
		t.Fatal(err)
	}
	info, err := negotiate(conn, 58783, "", 5*time.Second)
	if err != nil {
		t.Fatalf("negotiate: %v", err)
	}
	if info.Host != "192.168.1.23" {
		t.Errorf("host = %q, want the first non-loopback IP", info.Host)
	}
	if info.Port != 58783 {
		t.Errorf("port = %d, want 58783", info.Port)
	}
}

func TestNegotiateExplicitDeviceIPOverrides(t *testing.T) {
	ln, _ := net.Listen("tcp", "127.0.0.1:0")
	defer ln.Close()
	go fakeApp(t, ln, []string{"192.168.1.23"}, true, 0)

	conn, _ := net.Dial("tcp", ln.Addr().String())
	info, err := negotiate(conn, 58783, "10.0.0.9", 5*time.Second)
	if err != nil {
		t.Fatalf("negotiate: %v", err)
	}
	if info.Host != "10.0.0.9" {
		t.Errorf("host = %q, want the -device-ip override", info.Host)
	}
}

func TestNegotiateAppConnectFailure(t *testing.T) {
	ln, _ := net.Listen("tcp", "127.0.0.1:0")
	defer ln.Close()
	go fakeApp(t, ln, []string{"192.168.1.23"}, false, 61) // ECONNREFUSED

	conn, _ := net.Dial("tcp", ln.Addr().String())
	_, err := negotiate(conn, 58783, "", 5*time.Second)
	if err == nil {
		t.Fatal("expected the app's connect failure to surface")
	}
	if !contains(err.Error(), "errno=61") {
		t.Errorf("error should carry the errno, got: %v", err)
	}
}

func TestNegotiateNoUsableIP(t *testing.T) {
	ln, _ := net.Listen("tcp", "127.0.0.1:0")
	defer ln.Close()
	go fakeApp(t, ln, []string{}, true, 0)

	conn, _ := net.Dial("tcp", ln.Addr().String())
	if _, err := negotiate(conn, 58783, "", 5*time.Second); err == nil {
		t.Fatal("expected failure when the app reports no IP")
	}
}

func TestParseHandshake(t *testing.T) {
	msg := map[string]interface{}{
		"MessageType": "Handshake",
		"Properties":  map[string]interface{}{"UniqueDeviceID": "00008110-ABCDEF"},
		"Services": map[string]interface{}{
			"com.apple.internal.dt.coredevice.untrusted.tunnelservice": map[string]interface{}{"Port": "50000"},
			"com.apple.testmanagerd.remote":                            map[string]interface{}{"Port": "50001"},
		},
	}
	h, err := parseHandshake(msg)
	if err != nil {
		t.Fatalf("parseHandshake: %v", err)
	}
	if h.Udid != "00008110-ABCDEF" {
		t.Errorf("udid = %q", h.Udid)
	}
	if h.Services[untrustedTunnelService].Port != 50000 {
		t.Errorf("tunnel service port = %d, want 50000", h.Services[untrustedTunnelService].Port)
	}
	if len(h.Services) != 2 {
		t.Errorf("services = %d, want 2", len(h.Services))
	}
}

func TestParseHandshakeMissingUdid(t *testing.T) {
	if _, err := parseHandshake(map[string]interface{}{"MessageType": "Handshake"}); err == nil {
		t.Fatal("expected failure with no UniqueDeviceID")
	}
}

func TestPrefixConnPreservesBuffered(t *testing.T) {
	ln, _ := net.Listen("tcp", "127.0.0.1:0")
	defer ln.Close()
	go func() {
		c, _ := ln.Accept()
		defer c.Close()
		// Send a control line immediately followed by raw device bytes.
		_, _ = c.Write([]byte("{\"ok\":true}\nDEVICEBYTES"))
	}()
	conn, _ := net.Dial("tcp", ln.Addr().String())
	rd := bufio.NewReader(conn)
	if _, err := readJSONLine(rd); err != nil {
		t.Fatalf("read ack: %v", err)
	}
	wrapped := newPrefixConn(conn, rd)
	buf := make([]byte, 32)
	_ = wrapped.SetReadDeadline(time.Now().Add(time.Second))
	n, err := wrapped.Read(buf)
	if err != nil {
		t.Fatalf("read prefix: %v", err)
	}
	if string(buf[:n]) != "DEVICEBYTES" {
		t.Errorf("prefix bytes = %q, want DEVICEBYTES", buf[:n])
	}
}

func contains(s, sub string) bool {
	for i := 0; i+len(sub) <= len(s); i++ {
		if s[i:i+len(sub)] == sub {
			return true
		}
	}
	return false
}
