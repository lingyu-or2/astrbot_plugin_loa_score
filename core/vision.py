"""LLM 视觉识别：把装备截图交给当前会话的多模态模型，转写为结构化 JSON。

调用方式为 AstrBot v4.x Provider.text_chat（image_urls 支持本地文件路径），
不依赖任何在线文档未核验的新 API。
"""

import asyncio
import json
import re

from astrbot.api import logger

from .cnparse import CLAUSE_TYPES

# 识别提示词：要求只做忠实转写与分类，数值一律取原文，不做推断
VISION_PROMPT = """你是《命运方舟》装备截图识别器。请只根据图片内容，输出一个 JSON 对象（不要输出任何其他文字、不要用代码块）。

{
  "item_type": "necklace|earring|ring|bracelet|other",  // 部位：项链/耳环/戒指/手镯；看不出来填 other
  "grade": "relic|ancient|unknown",  // 品级：遗物(relic)，古代(ancient)；看不出来填 unknown
  "tier": 4,                         // 能看出 道具阶层4 就填 4，否则填 null
  "quality": 94,                     // 「品质」后面显示的数字（如 品质 94）；没有则填 null
  "main_stat": {"name": "敏捷", "value": 14016},  // 主属性行；没有则填 null
  "stats": [{"name": "专长", "value": 91}],        // 其余属性行，没有则 []
  "options": [                                    // 特殊效果行，逐行原样转写（不要翻译、不要合并、不要省略句号）
    {"text": "额外伤害增加2.5%。", "type": "add_dmg"},
    {"text": "暴击率增加4.2%。", "type": "crit_rate"}
  ]
}

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


def _extract_json(text: str):
    """从模型输出中稳健提取 JSON 对象。"""
    text = (text or '').strip()
    m = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', text, re.S)
    if m:
        text = m.group(1)
    try:
        return json.loads(text)
    except Exception:
        pass
    start, end = text.find('{'), text.rfind('}')
    if 0 <= start < end:
        try:
            return json.loads(text[start:end + 1])
        except Exception:
            return None
    return None


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
    """尽力取出提供商/模型描述（仅用于错误提示，失败返回空串）。"""
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
    return ' / '.join(parts)


def _vision_error_msg(kind: str, desc: str) -> str:
    base = f'模型未识别到装备属性界面（{kind}）。'
    if desc:
        base = f'{base}\n当前模型：{desc}'
    return (base + '\n'
            '最常见原因：当前会话配置的对话模型**不支持图片输入**（纯文本模型看不到截图，'
            '只能按提示词规则回答"没有装备界面"）。\n'
            '解决办法：在 WebUI「提供商配置」给会话换成多模态模型'
            '（如 glm-4v / qwen-vl / qwen2.5-vl / gpt-4o / gemini）后重试。\n'
            '若模型确为多模态，请重发更清晰的截图（QQ 发图建议勾选"原图"）。')


async def extract_item(context, event, image_path: str):
    """调用当前会话的多模态模型识别装备截图。

    返回 (item_dict 或 None, 错误信息)。item_dict 已 normalize。
    """
    try:
        provider = await context.get_using_provider_async(umo=event.unified_msg_origin)
    except Exception as e:
        return None, f'获取模型提供商失败: {e}'
    if provider is None:
        return None, ('当前会话没有可用的对话模型。\n'
                      '请先在 AstrBot WebUI 配置一个支持图片输入的模型'
                      '（如 glm-4v / qwen-vl / gemini 等）再使用截图评分。')

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
                      + (f'当前模型：{desc}\n' if desc else '')
                      + '请在 WebUI 换成多模态模型（glm-4v / qwen-vl / gpt-4o / gemini 等）后重试。')
    if data.get('error') in ('no_image_visible', 'not_equipment'):
        kind = '未收到图片' if data.get('error') == 'no_image_visible' else '未识别到装备界面'
        logger.warning(f'[loa_score] 模型返回 {data.get("error")}: {text[:300]}')
        return None, _vision_error_msg(kind, _provider_desc(provider))
    return normalize_item(data), ''
