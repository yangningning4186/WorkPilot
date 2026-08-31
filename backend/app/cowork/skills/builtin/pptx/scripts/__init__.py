"""内置 PPTX Skill 的受信任确定性执行脚本。

调用方必须导入具体模块。这里刻意不重导出 renderer/rasterizer：预览链只需要
pptx2image，若包入口同时加载 render_pptx，会把两个独立入口重新耦合成导入环。
"""
