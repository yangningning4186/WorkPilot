"""Cowork 本地持久化边界。

具体类型从各自子模块导入；这里不做 eager re-export，避免 write_note 的租约类型与
Store Protocol 在解释器加载阶段形成循环依赖。
"""
