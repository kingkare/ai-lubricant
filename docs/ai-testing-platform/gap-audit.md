# 落地缺口审计（design vs built）

**日期**：2026-10-03 ｜ **触发**：用户指出「用例太简陋、没有步骤说明、用例跟脚本没绑定、接口没有管理、接口导入没有」

## 方法

对照 `design.md` 声明的全部 `mc_*` 表 vs `testing_platform/*/models.py` 实际建的，逐表核。

## 结果：设计 34 张表，实际建 23 张，**缺 12 张**

| 缺口表 | 设计出处 | 作用 | 优先级 |
|---|---|---|---|
| `mc_test_transport` | 01 §3 / design §15.4 | **接口管理**（鉴权/公共头/路径替换，场景引用它） | **P0** |
| `mc_test_method_module` | 01 §5.1 | **方法脚本模块**（base/project Python 代码库） | **P0** |
| `mc_test_method` | 01 §5.1 | 方法注册表（full_ref/signature） | **P0** |
| `mc_test_dataset` | 01 §6 / design §15.4 | **数据集**（参数化数据） | **P0** |
| `mc_test_environment` | 05 / design §15.4 | **环境管理**（分层继承） | **P0** |
| `mc_test_env_variable` | 05 / design §15.4 | 环境变量（7 类型 + 密钥脱敏） | **P0** |
| `mc_test_custom_tool` | design §15.5 | 自定义工具（可扩展沉淀载体） | P1 |
| `mc_test_custom_tool_version` | design §15.5 | 自定义工具版本 | P1 |
| `mc_test_craft_asset` | design §15.6 | 沉淀四件套（约定/模板/样例/校验器） | P1 |
| `mc_test_question` | design §15.2 | **疑问（流程一等公民）** | P1 |
| `mc_knowledge_source` | design §15.1 | 知识源注册 | P2 |
| `mc_test_agent_binding` | design §15.9 | agent 绑定（设计注明「本阶段留空」） | P3 |

**接口导入**（swagger/apifox）**完全没做**——框架原话「支持接口的管理，支持接口的导入，如 swagger/apifox 等框架」。

## 根因（我的问题）

第 1 层规划时**只按「service 里已有什么写方法」列清单**，**没回头核对最初方案承诺的抽象**。导致：
- 设计了 transport/method/dataset/environment 四类抽象，**模型都没建**，前端自然做不出
- 用例页只显示"标题+意图+预期"（因为 `mc_test_case` 只有这些字段），**没有步骤说明**——但设计里用例本就**不含步骤**（步骤属场景），**缺的是"用例↔场景绑定"的可视化**（前端没展示 link_case_scenario 的结果）
- 用户说「用例跟脚本做绑定」——设计里是**用例↔场景**多对多（`mc_test_case_scenario` 表**已建**），但前端没把它渲染出来

## 教训（写进流程）

**规划补方法/页面时，必须先 grep `design.md` 的全部实体表，逐张核对是否已建**——不能只看 service 现状。

## 修复顺序

1. **P0 六张表**（transport/method×2/dataset/environment×2）+ 接口导入 —— 本轮
2. **P1 四张**（custom_tool×2/craft_asset/question）—— 次轮
3. 前端补：用例↔场景绑定展示、场景编辑器引用 transport/dataset、环境页 CRUD、接口管理页
