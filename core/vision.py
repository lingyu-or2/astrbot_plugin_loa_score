"""LLM 视觉识别：把装备截图交给当前会话的多模态模型，转写为结构化 JSON。

调用方式为 AstrBot v4.x Provider.text_chat（image_urls 支持本地文件路径），
不依赖任何在线文档未核验的新 API。
"""

import asyncio
import json
import re
from pathlib import Path

from astrbot.api import logger

from .cnparse import CLAUSE_TYPES

# 识别提示词：要求只做忠实转写与分类，数值一律取原文，不做推断
VISION_PROMPT = """你是《命运方舟》装备截图识别器。请只根据图片内容，输出一个 JSON 对象（不要输出任何其他文字、不要用代码块）。

{
  "item_type": "necklace",
  "grade": "ancient",
  "tier": 4,
  "quality": 94,
  "main_stat": {"name": "力量", "value": 17348},
  "stats": [{"name": "体力", "value": 4062}],
  "options": [
    {"text": "烙印力 +8.00%。", "type": "ally_dmg"},
    {"text": "对敌人造成的伤害+2.00%。", "type": "enemy_dmg"}
  ]
}

字段说明：
- item_type：部位，necklace=项链 / earring=耳环 / ring=戒指 / bracelet=手镯；看不出来填 other
- grade：品级，relic=遗物 / ancient=古代；看不出来填 unknown
- tier：道具阶级，看得到「道具阶级4」就填 4，否则填 null
- quality：「品质」后面的数字；没有填 null
- main_stat：主属性行（力量/敏捷/智力的大数值行，全职业三行相同时任取其一）；没有填 null
- stats：其余属性行（体力/会心/专长/迅捷/制压/忍耐/熟练等）；没有填 []
- options：特殊效果词条行，逐行原样转写（不要翻译、不要合并、不要省略句号）

输出规则（必须严格遵守）：
- 直接输出 JSON 对象本身，第一个字符是 {，最后一个字符是 }；
- 不要 markdown 代码块、不要任何注释（不要 // ）、不要尾逗号，字符串一律用英文双引号；
- 数值不加引号，空值用 null。

type 字段的可选值与含义：
__CLAUSE_LIST__

识别要点：
1. 全职业装备的基本效果会同时显示 力量/敏捷/智力 三行相同数值——main_stat 任取其一即可；
   体力/生命力、会心、专长、迅捷、制压、忍耐、熟练等行放 stats。
2. 饰品（项链/耳环/戒指）的特殊词条在「研磨效果」栏；手镯在「手镯效果」栏。
   options 只放该栏里的效果行，逐行转写；「一支队伍仅限获得一个该效果」这类说明行也要转写。
3. 「方舟被动点数效果」（顿悟/飞跃）、品质、交易限制道具等级、可交易次数、无法交易等
   都不是词条，不要放进 options 或 stats。
4. 数值、标点必须与图片原文一致；看不清的字用 "?" 占位。
5. 只要图片中包含装备属性详情就算有效——包括聊天窗口里分享的装备卡片/鼠标悬停弹窗
   （即使带有聊天背景、昵称、时间戳）。只有当图片里**完全没有任何装备属性信息**时，
   才输出 {"error": "not_equipment"}。
6. 如果你**没有收到图片**或完全看不到图片内容（只收到文字），必须输出
   {"error": "no_image_visible"}，不要猜测图片内容。
"""

# 注意：正文含大量字面量 '%'（如 2.5%。），不能用 % 格式化拼接，否则 '%。' 会被当成
# 格式占位符在 import 时抛 ValueError（真机安装事故见 astrbot/AGENTS.md §10）。
VISION_PROMPT = VISION_PROMPT.replace(
    '__CLAUSE_LIST__',
    '\n'.join(f'- {k}: {v}' for k, v in CLAUSE_TYPES.items()),
)

VISION_TIMEOUT = 120


# 中文全角引号/冒号 → ASCII（仅解析失败后的修复阶段使用）
_PUNCT_NORM = {'\uff1a': ':', '\u201c': '"', '\u201d': '"', '\u2018': '"', '\u2019': '"'}


def _strip_comments(s: str) -> str:
    """去掉 // 行注释与 /* */ 块注释（字符串字面量内的不动）。"""
    out = []
    i, n = 0, len(s)
    in_str = False
    quote = ''
    while i < n:
        c = s[i]
        if in_str:
            out.append(c)
            if c == '\\' and i + 1 < n:
                out.append(s[i + 1])
                i += 2
                continue
            if c == quote:
                in_str = False
            i += 1
            continue
        if c in '"\'':
            in_str = True
            quote = c
            out.append(c)
            i += 1
            continue
        if c == '/' and i + 1 < n and s[i + 1] == '/':
            while i < n and s[i] != '\n':
                i += 1
            continue
        if c == '/' and i + 1 < n and s[i + 1] == '*':
            i += 2
            while i + 1 < n and not (s[i] == '*' and s[i + 1] == '/'):
                i += 1
            i += 2
            continue
        out.append(c)
        i += 1
    return ''.join(out)


def _try_loads(s: str):
    try:
        v = json.loads(s)
        return v if isinstance(v, dict) else None
    except Exception:
        return None


def _extract_json(text: str):
    """从模型输出中稳健提取 JSON 对象。

    逐级加码修复（任何一级成功即返回）：整体解析 → 代码块 → 大括号区间 →
    去注释 → 中文引号/冒号归一 → 去尾逗号 → 单引号/Python 字面量修复。
    """
    raw = (text or '').strip()
    if not raw:
        return None
    v = _try_loads(raw)
    if v is not None:
        return v
    m = re.search(r'```(?:json)?\s*(.+?)\s*```', raw, re.S)
    if m:
        v = _try_loads(m.group(1))
        if v is not None:
            return v
    start, end = raw.find('{'), raw.rfind('}')
    if not (0 <= start < end):
        return None
    s = raw[start:end + 1]
    v = _try_loads(s)
    if v is not None:
        return v
    s = _strip_comments(s)
    v = _try_loads(s)
    if v is not None:
        return v
    s = ''.join(_PUNCT_NORM.get(ch, ch) for ch in s)
    # 全角逗号仅在"结构位"（前后邻引号/括号/数字）转半角，避免破坏字符串内容里的中文逗号
    s = re.sub(r'(?<=[\"\u007d\]\d])\uff0c(?=[\"\u007b\[\u007d\]])', ',', s)
    v = _try_loads(s)
    if v is not None:
        return v
    s = re.sub(r',\s*([}\]])', r'\1', s)
    v = _try_loads(s)
    if v is not None:
        return v
    if "'" in s and '"' not in s:
        v = _try_loads(s.replace("'", '"'))
        if v is not None:
            return v
    s = re.sub(r'\b(None|True|False)\b',
               lambda mm: {'None': 'null', 'True': 'true', 'False': 'false'}[mm.group(1)], s)
    return _try_loads(s)


def normalize_item(data: dict) -> dict:
    """保守清洗 LLM 输出，字段不合法时置空。"""
    item_type = data.get('item_type')
    if item_type not in ('necklace', 'earring', 'ring', 'bracelet'):
        item_type = None
    grade = data.get('grade')
    if grade not in ('relic', 'ancient'):
        grade = None
    tier = data.get('tier')
    if not isinstance(tier, int):
        tier = None

    quality = None
    try:
        if data.get('quality') is not None:
            quality = float(str(data['quality']).replace(',', ''))
    except (TypeError, ValueError):
        quality = None

    main_stat = None
    ms = data.get('main_stat')
    if isinstance(ms, dict) and ms.get('value') is not None:
        try:
            main_stat = {'name': str(ms.get('name', '')).strip(),
                         'value': float(str(ms['value']).replace(',', ''))}
        except (TypeError, ValueError):
            main_stat = None

    stats = []
    for s in data.get('stats') or []:
        if not isinstance(s, dict) or s.get('value') is None:
            continue
        try:
            stats.append({'name': str(s.get('name', '')).strip(),
                          'value': float(str(s['value']).replace(',', ''))})
        except (TypeError, ValueError):
            continue

    options = []
    for o in data.get('options') or []:
        if isinstance(o, str) and o.strip():
            options.append({'raw': o.strip(), 'tag': None})
        elif isinstance(o, dict) and str(o.get('text', '')).strip():
            options.append({'raw': str(o['text']).strip(),
                            'tag': o.get('type') if isinstance(o.get('type'), str) else None})
    return {'item_type': item_type, 'grade': grade, 'tier': tier, 'quality': quality,
            'main_stat': main_stat, 'stats': stats, 'options': options}


def _provider_desc(provider) -> str:
    """尽力取出提供商/模型/适配器描述（仅用于错误提示与日志，失败返回空串）。"""
    parts = []
    try:
        meta = provider.meta()
        pid = getattr(meta, 'id', '')
        if pid:
            parts.append(str(pid))
        model = getattr(meta, 'model', None)
        name = getattr(model, 'name', '') or getattr(model, 'id', '')
        if name:
            parts.append(str(name))
    except Exception:
        pass
    parts.append(f'适配器:{type(provider).__name__}')
    return ' / '.join(parts)


def _vision_error_msg(kind: str, desc: str) -> str:
    lines = [f'模型回复「{kind}」，图片可能没有到达模型。']
    if desc:
        lines.append(f'当前会话模型：{desc}')
    lines.append(
        '排查步骤：\n'
        '1. 查 AstrBot 日志：出现「检测到图片请求失败」= 接口拒收了图片，'
        'AstrBot 已自动去掉图片纯文本重试（该接口/网关实际不支持图片输入）；\n'
        '2. 出现「图片预处理结果为空」= 图片转换失败被忽略；\n'
        '3. 快速验证：在 WebUI 聊天页直接发同一张图让模型描述，同样看不见 = 接口问题，\n'
        '   请在「提供商配置」换用多模态模型（glm-4v / qwen-vl / gpt-4o / gemini）后重试。'
    )
    return '\n'.join(lines)


async def extract_item(context, event, image_path: str, provider_id: str = ''):
    """调用多模态模型识别装备截图。

    provider_id 非空时用该指定提供商（识别专用视觉模型），否则用会话当前提供商。
    返回 (item_dict 或 None, 错误信息)。item_dict 已 normalize。
    """
    provider = None
    if provider_id:
        try:
            provider = context.get_provider_by_id(provider_id)
        except Exception as e:
            return None, f'获取指定识别提供商「{provider_id}」失败: {e}'
        if provider is None:
            return None, (f'未找到配置的识别提供商「{provider_id}」。\n'
                          '请在 WebUI「提供商配置」核对 ID，'
                          '或清空插件配置里的 vision_provider_id 改用会话默认模型。')
    else:
        try:
            provider = await context.get_using_provider_async(umo=event.unified_msg_origin)
        except Exception as e:
            return None, f'获取模型提供商失败: {e}'
        if provider is None:
            return None, ('当前会话没有可用的对话模型。\n'
                          '请先在 AstrBot WebUI 配置一个支持图片输入的模型'
                          '（如 glm-4v / qwen-vl / gemini 等）再使用截图评分，'
                          '或在插件配置里指定识别专用提供商 vision_provider_id。')

    try:
        size_kb = Path(image_path).stat().st_size // 1024
    except Exception:
        size_kb = -1
    logger.info(f'[loa_score] 发送识别请求: {_provider_desc(provider)}, 图片 {size_kb}KB')

    try:
        resp = await asyncio.wait_for(
            provider.text_chat(
                prompt=VISION_PROMPT,
                image_urls=[str(image_path)],
            ),
            timeout=VISION_TIMEOUT,
        )
    except asyncio.TimeoutError:
        return None, '模型识别超时，请稍后重试或换一张更清晰的截图。'
    except Exception as e:
        return None, f'模型调用失败: {e}'

    text = ''
    try:
        text = resp.completion_text or ''
    except Exception:
        pass
    if not text:
        return None, ('模型没有返回内容——当前模型很可能不支持图片输入。\n'
                      '请在 WebUI 换成多模态模型（glm-4v / qwen-vl / gpt-4o / gemini 等）后重试。')

    data = _extract_json(text)
    if data is None:
        logger.warning(f'[loa_score] 模型输出无法解析为 JSON: {text[:300]}')
        desc = _provider_desc(provider)
        return None, ('模型输出无法解析（可能不支持图片输入，把提示词当成了普通聊天）。\n'
                      + (f'当前会话模型：{desc}\n' if desc else '')
                      + '请在 WebUI 换成多模态模型（glm-4v / qwen-vl / gpt-4o / gemini 等）后重试。')
    if data.get('error') in ('no_image_visible', 'not_equipment'):
        kind = '未收到图片' if data.get('error') == 'no_image_visible' else '未识别到装备界面'
        logger.warning(f'[loa_score] 模型返回 {data.get("error")}: {text[:300]}')
        return None, _vision_error_msg(kind, _provider_desc(provider))
    return normalize_item(data), ''
