"""astrbot_plugin_loa_score 核心逻辑包。

core/ 下所有模块不依赖 AstrBot，可离线自测（tests/run_tests.py）：
- grading:  评分规则引擎（查 data/tables.json，纯函数）
- cnparse:  中文词条文本解析（子句签名提取）
- imageio:  图片获取与校验
- vision:   LLM 视觉识别（调 AstrBot Provider.text_chat）
"""
