"""AI 测试平台（testing_platform）。

本包 v1 有两个确定性子包（04 §6 决策 4：引擎在服务端进程内）：
- `engine`  — 确定性执行引擎；AI 工作流（Debugger / 场景生成）与引擎物理分离。
- `knowledge` — 知识摄取层（09-knowledge-system.md §4.7「工具即抽取器」），
  Graphify → 映射 → 幂等入库，表 mc_knowledge_* / mc_test_profile。
"""
