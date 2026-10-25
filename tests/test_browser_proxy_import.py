"""SwitchyOmega / ZeroOmega 备份导入契约（proxy.js importSwitchyOmegaBackup）。

Node 侧以字面量执行 proxy.js 的纯函数段（importScripts 不可用），验证：
- 五种 SO profile 类型 → 我们模型的映射；
- SwitchProfile 规则的 profileName → profileId 解析；
- 同名/非法输入的容错。
"""
import json
import subprocess
import sys
from pathlib import Path

EXT = Path(__file__).resolve().parents[1] / "mcp_builtin" / "cdp_bridge" / "tmwd_cdp_bridge"

# 在 Node 里加载 proxy.js（importScripts 在 SW 外不可用，这里打桩后取函数引用）。
LOADER = """
const fs = require('fs');
global.chrome = { storage: { local: { get: async () => ({}), set: async () => {} } }, alarms: { create(){}, clear(){} } };
global.importScripts = () => {};
global.loadBridgeConfig = async () => ({});
let bridgeWsUrl = 'ws://127.0.0.1:8001/mcp/cdp-bridge/session';
const src = fs.readFileSync(process.argv[1], 'utf8');
const fn = new Function(src + '; return { importSwitchyOmegaBackup, sanitizeProfile, compileProfile, findProfile };');
const api = fn();
const backup = JSON.parse(process.argv[2]);
const result = api.importSwitchyOmegaBackup(backup);
console.log(JSON.stringify(result));
"""

SO_BACKUP = {
    "profiles": {
        "proxy": {
            "type": "FixedProfile",
            "name": "proxy",
            "fallbackProxy": {"scheme": "http", "host": "127.0.0.1", "port": 7890},
            "bypassList": [{"pattern": "*.internal"}, "<local>"],
        },
        "direct": {"type": "DirectProfile", "name": "direct"},
        "system proxy": {"type": "SystemProfile", "name": "system proxy"},
        "pac profile": {"type": "PacProfile", "name": "pac profile", "pacUrl": "https://x/pac"},
        "auto switch": {
            "type": "SwitchProfile",
            "name": "auto switch",
            "rules": [
                {"condition": {"conditionType": "HostWildcardCondition", "pattern": "*.example.com"}, "profileName": "proxy"},
                {"condition": {"conditionType": "UrlRegexCondition", "pattern": "^https://x/"}, "profileName": "proxy"},
                {"condition": {"conditionType": "KeywordCondition", "pattern": "foo"}, "profileName": "direct"},
            ],
            "defaultProfileName": "direct",
        },
    }
}


def run_import(backup: dict) -> list:
    out = subprocess.run(
        ["node", "-e", LOADER, str(EXT / "proxy.js"), json.dumps(backup)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
    )
    if out.returncode != 0:
        raise AssertionError(f"node 执行失败: {(out.stderr or '').strip()[:300]}")
    return json.loads(out.stdout.strip())


def test_import_maps_all_profile_types():
    profiles = {p["name"]: p for p in run_import(SO_BACKUP)}
    assert set(profiles) == {"proxy", "direct", "system proxy", "pac profile", "auto switch"}
    assert profiles["proxy"]["type"] == "fixed"
    assert profiles["proxy"]["fixed"]["host"] == "127.0.0.1"
    assert profiles["proxy"]["fixed"]["port"] == 7890
    assert profiles["proxy"]["fixed"]["bypass"] == ["*.internal", "<local>"]
    assert profiles["direct"]["type"] == "direct"
    assert profiles["system proxy"]["type"] == "system"
    assert profiles["pac profile"]["type"] == "pac"
    assert profiles["pac profile"]["pac"]["url"] == "https://x/pac"
    assert profiles["auto switch"]["type"] == "auto"


def test_import_resolves_rule_names_to_ids():
    profiles = {p["name"]: p for p in run_import(SO_BACKUP)}
    auto = profiles["auto switch"]["auto"]
    assert len(auto["rules"]) == 3
    # 规则目标解析为 proxy 的 id（id 派生自 name）。
    assert auto["rules"][0]["profileId"] == profiles["proxy"]["id"]
    assert auto["rules"][2]["profileId"] == profiles["direct"]["id"]
    assert auto["defaultProfileId"] == profiles["direct"]["id"]
    types = [r["condition"]["type"] for r in auto["rules"]]
    assert types == ["hostWildcard", "urlRegex", "keyword"]


def test_import_invalid_backup_raises():
    out = subprocess.run(
        ["node", "-e", LOADER, str(EXT / "proxy.js"), json.dumps({"foo": 1})],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
    )
    # importSwitchyOmegaBackup 抛错 → node 未打印 JSON。优雅失败即可（不崩溃）。
    assert out.stdout.strip() == "" or out.returncode != 0


def test_import_ids_are_unique_and_sanitized():
    backup = {"profiles": {
        "a/b": {"type": "DirectProfile"}, "a b": {"type": "DirectProfile"}, "a/b": {"type": "DirectProfile"},
    }}
    profiles = run_import(backup)
    ids = [p["id"] for p in profiles]
    assert len(ids) == len(set(ids))  # 同名 key 在 JS 对象里会合并，这里只验证不崩不重
    for pid in ids:
        assert all(c.isalnum() or c in "_-" for c in pid)