from __future__ import annotations

import json

import pytest

from user_platform.apple_signing import gsa


def test_sms_headers_are_json_and_match_isideload(monkeypatch):
    headers = gsa._sms_headers("adsid", "idms", {
        "X-Mme-Device-Id": "device",
        "X-Apple-I-MD": "otp",
        "X-Apple-I-MD-M": "machine",
        "X-Apple-I-MD-RINFO": "rinfo",
        "X-Apple-I-MD-LU": "must-not-be-in-sms",
    })
    assert headers["Content-Type"] == "application/json"
    assert headers["Accept"] == "application/json"
    assert headers["X-Mme-Device-Id"] == "device"
    assert headers["X-Apple-I-MD"] == "otp"
    assert headers["X-Apple-I-MD-M"] == "machine"
    assert headers["X-Apple-I-MD-RINFO"] == "rinfo"
    assert "X-Apple-I-MD-LU" not in headers
    assert "X-Apple-Identity-Token" in headers


def test_list_trusted_phone_numbers_parses_response(monkeypatch):
    seen = {}

    def fake_http(method, url, headers, body):
        seen.update(method=method, url=url, headers=headers, body=body)
        return 200, {}, json.dumps({
            "trustedPhoneNumbers": [
                {"id": 7, "numberWithDialCode": "+86 *** 1234"},
            ],
        }).encode()

    monkeypatch.setattr(gsa, "_http", fake_http)
    got = gsa.list_trusted_phone_numbers("adsid", "idms", {})
    assert got == [{"id": 7, "number_with_dial_code": "+86 *** 1234"}]
    assert seen["method"] == "GET"
    assert seen["url"] == "https://gsa.apple.com/auth"
    assert seen["headers"]["Content-Type"] == "application/json"


def test_send_sms_code_sends_isideload_json(monkeypatch):
    seen = {}

    def fake_http(method, url, headers, body):
        seen.update(method=method, url=url, headers=headers, body=body)
        return 200, {}, b""

    monkeypatch.setattr(gsa, "_http", fake_http)
    gsa.send_sms_code("adsid", "idms", 7, {})
    assert seen["method"] == "PUT"
    assert seen["url"].endswith("/auth/verify/phone")
    assert json.loads(seen["body"]) == {"phoneNumber": {"id": 7}, "mode": "sms"}
    assert seen["headers"]["Content-Type"] == "application/json"


def test_verify_sms_code_marks_21669_retryable(monkeypatch):
    def fake_http(method, url, headers, body):
        return 400, {}, json.dumps({
            "serviceErrors": [{"code": "-21669", "title": "Incorrect", "message": "wrong code"}],
        }).encode()

    monkeypatch.setattr(gsa, "_http", fake_http)
    with pytest.raises(gsa._SmsServiceError) as exc:
        gsa.verify_sms_code("adsid", "idms", 7, "000000", {})
    assert exc.value.code == "-21669"
    assert "wrong code" in str(exc.value)


def test_verify_sms_code_accepts_success(monkeypatch):
    monkeypatch.setattr(gsa, "_http", lambda *args: (200, {}, b""))
    assert gsa.verify_sms_code("adsid", "idms", 7, "123456", {}) is None
