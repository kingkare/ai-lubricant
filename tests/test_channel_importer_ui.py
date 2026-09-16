"""外部渠道导入的前端接线断言（源码文本级）。

沿用 tests/test_channel_template_ui.py 的风格：读文件、断子串。目的是防止「后端加了
路由但前端没接」「en.ts 漏改」这类容易漏的接线断点。
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHANNELS = ROOT / "user-frontend/src/pages/manager/platform/Channels.tsx"
DIALOG = ROOT / "user-frontend/src/pages/manager/platform/channel-catalog/ExternalChannelImportDialog.tsx"
API = ROOT / "user-frontend/src/@admin-port/api/providers.ts"
TYPES = ROOT / "user-frontend/src/@admin-port/types/admin.ts"
CN = ROOT / "user-frontend/src/i18n/resources/cn.ts"
EN = ROOT / "user-frontend/src/i18n/resources/en.ts"


def test_channels_page_has_import_entry_beside_add_button():
    source = CHANNELS.read_text(encoding="utf-8")
    assert "<ExternalChannelImportDialog" in source
    assert "导入外部渠道" in source
    # 原有统一添加入口保持不变（test_channel_template_ui 也断言它）。
    assert "添加供应商" in source


def test_dialog_uses_the_three_import_api_functions():
    source = DIALOG.read_text(encoding="utf-8")
    assert "getChannelImporters" in source
    assert "previewChannelImport" in source
    assert "commitChannelImport" in source


def test_dialog_does_not_fetch_github_directly():
    """导入走服务端路由，前端不直连 raw.githubusercontent。"""
    source = DIALOG.read_text(encoding="utf-8")
    assert "raw.githubusercontent.com" not in source


def test_dialog_surfaces_conflict_overwrite_and_missing_keys():
    source = DIALOG.read_text(encoding="utf-8")
    # 冲突三模式 + 覆盖是用户明确要的能力
    assert "overwrite" in source
    assert "merge_accounts" in source
    # 列表接口不回传密钥这件事必须在 UI 里说清楚
    assert "不回传密钥" in source


def test_api_layer_declares_import_routes():
    source = API.read_text(encoding="utf-8")
    assert '"/admin/channel-importers"' in source
    assert '"/admin/channel-importers/preview"' in source
    assert '"/admin/channel-importers/commit"' in source


def test_types_layer_declares_import_contracts():
    source = TYPES.read_text(encoding="utf-8")
    for name in (
        "ChannelImporterInfo",
        "ExternalChannelCandidate",
        "ChannelImportPreviewRequest",
        "ChannelImportPreviewResponse",
        "ChannelImportCommitRequest",
        "ChannelImportCommitResponse",
    ):
        assert name in source


def test_operation_log_labels_exist_in_both_locales():
    """logs.tsx 按 managerLogs.operations.<key> 取标签；en.ts 漏改会显示成原始 key。"""
    for path in (CN, EN):
        source = path.read_text(encoding="utf-8")
        assert "import_external_channels" in source
        assert "overwrite_external_channel" in source
        assert "merge_external_channel_accounts" in source
