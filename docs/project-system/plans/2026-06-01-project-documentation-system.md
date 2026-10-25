# 项目文档体系迁移计划

Whitepaper: 本计划是文档体系 bootstrap 计划；它创建白皮书层本身，因此本轮无需单独白皮书。长期规则写入 docs/project-system/README.md 和 master-plan.md。
Master plan item: `docs/project-system/master-plan.md` 中的 “文档体系迁移”

## 目标

建立 `docs/project-system/` 四级文档中枢，并把请求日志、Token 计费口径、模型路由、同协议直通四个高价值主题迁移为白皮书。

## 已确认需求

- 使用“代码 -> 计划 -> 白皮书 -> 总计划”的四层关系。
- 白皮书记录长期主题的背景、原则和决策依据。
- 总计划记录全项目路线图、优先级和主题状态。
- 文档体系优先服务用户快速看懂、验收、追溯和做决策，其次服务 AI 接手。
- 需求按 S/M/L/XL 分级，不强制所有需求补满四层。
- 采用彻底重整的新目录：`docs/project-system/`。
- 迁移中和迁移完成后都要审查结构是否合理。

## 影响文件

- `docs/project-system/README.md`
- `docs/project-system/master-plan.md`
- `docs/project-system/code-map.md`
- `docs/project-system/whitepapers/request-logging.md`
- `docs/project-system/whitepapers/token-accounting.md`
- `docs/project-system/whitepapers/model-routing.md`
- `docs/project-system/whitepapers/protocol-passthrough.md`
- `docs/project-system/archive/README.md`
- `docs/project-system/archive/old-plans/README.md`
- `docs/README.md`

## 实施步骤

- [x] 建立 `docs/project-system/` 入口、归档说明和旧计划入口说明。
- [x] 创建 `master-plan.md` 和 `code-map.md`。
- [x] 创建请求日志和 Token 计费口径白皮书。
- [x] 创建模型路由和同协议直通白皮书。
- [x] 更新 `docs/README.md`，把新文档中枢放到推荐阅读路径第一位。
- [x] 检查所有计划和白皮书链接规则。
- [x] 做迁移中合理性审查。
- [x] 做迁移后合理性审查。

## 迁移中合理性审查

每个主题迁移时检查迁移是否真正保留用户验收、追溯和决策所需的信息，而不是只把旧文档换位置：

| 主题 | 来源材料 | 迁移后目标文档 | 迁移是否合理 | 需要保留的关键决策 | 不迁移内容及原因 | 结论 |
| --- | --- | --- | --- | --- | --- | --- |
| 请求日志 | 旧计划、规格说明、反馈记忆中的“渠道请求逐条记录”约束 | `docs/project-system/whitepapers/request-logging.md`、`docs/project-system/master-plan.md`、`docs/project-system/code-map.md` | 合理。请求日志横跨代理请求、渠道尝试、管理端详情和统计验收，需要长期事实来源。 | 渠道请求必须逐条写入现有日志表；请求 ID 是请求与渠道尝试的关联关系；详情页必须显示请求路径；失败重试路径要能追溯到对应尝试。 | 不迁移一次性 UI 调整步骤、临时排查命令和已完成的实现流水账，因为这些不帮助后续判断当前口径。 | 迁移为白皮书，保留背景、口径、状态和验收入口。 |
| Token 计费口径 | 旧计划、反馈记忆中的“Token 计费口径”约束、相关测试说明 | `docs/project-system/whitepapers/token-accounting.md`、`docs/project-system/master-plan.md`、`docs/project-system/code-map.md` | 合理。Token 统计会影响账单、管理端展示和跨协议转换，是长期业务口径。 | `total = 输入 + 输出`；缓存读和缓存写属于输入 token 明细，不额外计入 total；任何展示或统计都不能把缓存 token 重复加总。 | 不迁移具体测试运行记录、已废弃字段名猜测和临时兼容说明，因为白皮书只保留稳定计费口径与风险。 | 迁移为白皮书，作为以后修改 token 统计时的基准。 |
| 模型路由 | 模型组、渠道路由、Public API 相关计划和测试 | `docs/project-system/whitepapers/model-routing.md`、`docs/project-system/master-plan.md`、`docs/project-system/code-map.md` | 合理。模型组与渠道路由决定外部 API 行为和管理端配置语义，不能散落在多个短期计划中。 | 模型组是对外暴露和管理的抽象；渠道属于模型组下的实际上游能力；Public API 需要维持模型组语义并避免泄露内部不稳定实现；路由变化要能被测试覆盖。 | 不迁移具体页面微调、临时数据库排查输出和重复的任务拆分，因为这些属于实施过程，不是长期路由规则。 | 迁移为白皮书，并在总计划中标明当前主题状态。 |
| 同协议直通 | 反馈记忆中的“同协议直通策略”、协议转换相关旧文档 | `docs/project-system/whitepapers/protocol-passthrough.md`、`docs/project-system/master-plan.md`、`docs/project-system/code-map.md` | 合理。协议边界是代理层长期约束，错误迁移会直接改变请求语义。 | anthropic 到 anthropic 必须原始字节流转发；只有跨协议才做字段映射；任何路径都不能补默认字段；直通策略优先保护上游原始语义。 | 不迁移早期方案中与该决策冲突的默认字段补齐思路、一次性调试日志和已关闭分支细节，因为保留会制造错误事实来源。 | 迁移为白皮书，明确协议边界和禁止事项。 |

## 迁移后合理性审查

检查项：

- [x] 是否存在两个当前事实来源互相冲突。结论：新中枢声明为后续默认入口，旧目录保留为历史参考，未形成两个当前事实来源。
- [x] 是否有白皮书写成实施步骤。结论：四篇白皮书只记录背景、问题、原则、决策、非目标和关联计划。
- [x] 是否有计划承担长期原则。结论：迁移计划只记录本轮迁移工作，长期规则保留在白皮书。
- [x] 是否有总计划写得过细。结论：总计划只记录主题状态、优先级和下一步，没有实施步骤。
- [x] 是否能从总计划追溯到白皮书、计划和代码索引。结论：`master-plan.md` 链接白皮书，白皮书链接迁移计划，`code-map.md` 链接相关白皮书和计划。
- [x] 是否能从代码模块反向追溯到相关白皮书和计划。结论：`code-map.md` 按模块列出关键文件、白皮书和活跃计划。
- [x] 是否为用户复盘保留了关键背景、关键决策、当前状态和验收入口。结论：四篇白皮书包含背景、决策、非目标和关联计划；总计划标明状态和下一步；迁移计划记录了验收入口。
- [x] 是否比旧的散落计划、规格和反馈记忆更容易进入主题并判断下一步。结论：所有新需求默认从 `docs/project-system/README.md` 进入，再按主题跳到白皮书和计划；旧目录已通过归档说明标记。
- [x] 是否没有丢失仍然有效的旧决策、约束、风险和禁止事项。结论：四项长期决策（请求日志、Token 计费、模型路由、同协议直通）都从记忆和旧计划中提炼，列入白皮书原则和决策。
- [x] 是否避免“统一治理”“持续优化”这类无法指导验收的空泛口号。结论：白皮书使用具体可验证口径，例如 `total = prompt + completion`、原始字节流转发。
- [x] 是否明确说明哪些旧文档内容不迁移，以及不迁移原因。结论：迁移中合理性审查表逐项列出了不迁移内容及原因。
- [ ] 是否为用户复盘保留了关键背景、关键决策、当前状态和验收入口。
- [ ] 是否比旧的散落计划、规格和反馈记忆更容易进入主题并判断下一步。
- [ ] 是否没有丢失仍然有效的旧决策、约束、风险和禁止事项。
- [ ] 是否避免“统一治理”“持续优化”这类无法指导验收的空泛口号。
- [ ] 是否明确说明哪些旧文档内容不迁移，以及不迁移原因。

## 验证方式

运行链接和结构检查：

```bash
python - <<'PY'
from pathlib import Path
root = Path('docs/project-system')
required = [
    root / 'README.md',
    root / 'master-plan.md',
    root / 'code-map.md',
    root / 'whitepapers/request-logging.md',
    root / 'whitepapers/token-accounting.md',
    root / 'whitepapers/model-routing.md',
    root / 'whitepapers/protocol-passthrough.md',
    root / 'plans/2026-06-01-project-documentation-system.md',
    root / 'archive/README.md',
    root / 'archive/old-plans/README.md',
]
missing = [str(path) for path in required if not path.exists()]
if missing:
    raise SystemExit(f'missing files: {missing}')

for path in (root / 'whitepapers').glob('*.md'):
    text = path.read_text(encoding='utf-8')
    if '## Plans' not in text:
        raise SystemExit(f'{path} missing Plans section')

plan = (root / 'plans/2026-06-01-project-documentation-system.md').read_text(encoding='utf-8')
for marker in ['Whitepaper:', 'Master plan item:']:
    if marker not in plan:
        raise SystemExit(f'plan missing {marker}')

master = (root / 'master-plan.md').read_text(encoding='utf-8')
for link in [
    './whitepapers/request-logging.md',
    './whitepapers/token-accounting.md',
    './whitepapers/model-routing.md',
    './whitepapers/protocol-passthrough.md',
]:
    if link not in master:
        raise SystemExit(f'master plan missing {link}')

print('project documentation system structure ok')
PY
```

Expected output:

```text
project documentation system structure ok
```

## 完成状态

- 已建立 `docs/project-system/` 文档中枢。
- 已创建总计划、代码索引、四篇主题白皮书和迁移计划。
- 已更新 `docs/README.md`，把项目文档中枢放到推荐阅读路径第一位。
- 已运行结构检查，结果为 `project documentation system structure ok`。
- 本轮迁移没有移动旧文件，避免破坏现有引用；旧内容仍可通过归档说明追溯。

## 遗留问题

- 旧计划是否迁入 `archive/old-plans/`，需要等新中枢使用一轮后决定。
- 旧工具产物目录是否保留，需要等执行流程稳定后决定。

## 执行复盘

本轮执行以未提交工作树形式完成。最终检查确认：

- `docs/project-system/` 成为后续文档中枢。
- 旧目录没有被删除或移动，因此不会破坏已有引用。
- 四篇白皮书记录长期原因和规则，没有写成任务清单。
- 迁移计划记录了迁移中和迁移后的合理性审查。
- 结构检查通过，输出 `project documentation system structure ok`。
- 占位符检查通过，未发现占位符。
- 旧需求原因（请求日志、Token 计费、模型路由、同协议直通）已从记忆和旧计划提炼到白皮书原则和决策，没有散落。
