"""测试设计包——机械枚举常量与纯函数（17/18 号文 L0-L1 层）。

模块：
- ``transform_table``——覆盖轴 C01-C14 + 参数变换表 + slot_key + enumerate_api
- ``skeleton``——骨架用例落库（bulk_create 幂等 + 存量库自愈 ensure_slot_index）
- ``coverage``——module×axis 覆盖矩阵 + 缺口（治断点 5：漏没漏无法度量）
- ``token_budget``——上下文窗口/预留/三态判定 + 标题感知切分（治「一万个需求炸 token」）
"""
