"""astrbot_plugin_loa_score — 命运方舟首饰/手镯评分插件。

用法：发送 /首饰评分 并附带装备属性截图（同一条消息），机器人识别词条后
按 lopec.kr 的评分口径回复：
- 项链/耳环/戒指：主属性百分比 + 부여 효과 下/中/上评级 + 战力倍率；
- 手镯：特殊词条 下/中/上评级 + 战力倍率合计（属性行不参与评级）。

评分规则表 data/tables.json 由 lopec 前端逆向数据离线构建；
词条识别由多模态模型（会话模型或识别专用提供商）完成，评分全部离线计算。
"""

import asyncio
import uuid
from pathlib import Path

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import AstrBotConfig, logger
from astrbot.api.message_components import Image
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

try:  # AstrBot 以包形式加载插件
    from .core import grading
    from .core.imageio import acquire_image_bytes, cleanup_stale_files, validate_image
    from .core.vision import VISION_TIMEOUT, extract_item
except ImportError:  # 兼容以目录为根的直接加载
    from core import grading
    from core.imageio import acquire_image_bytes, cleanup_stale_files, validate_image
    from core.vision import VISION_TIMEOUT, extract_item

PLUGIN_NAME = 'astrbot_plugin_loa_score'
PLUGIN_VERSION = '1.2.0'

SLOT_ARG = {'手镯': 'bracelet', '팔찌': 'bracelet', 'bracelet': 'bracelet',
            '项链': 'necklace', '목걸이': 'necklace', 'necklace': 'necklace',
            '耳环': 'earring', '귀걸이': 'earring', 'earing': 'earring',
            '戒指': 'ring', '반지': 'ring', 'ring': 'ring'}
GRADE_ARG = {'遗物': 'relic', '유물': 'relic', 'relic': 'relic',
             '古代': 'ancient', '远古': 'ancient', '고대': 'ancient', 'ancient': 'ancient'}

HELP_TEXT = """📿 命运方舟首饰评分（lopec 口径 · 仅 T4）

用法：/首饰评分 + 装备截图（图片和指令同一条消息）
可选参数：/首饰评分 [部位] [品级]（顺序不限）
  部位：手镯 / 项链 / 耳环 / 戒指（截图看不出来时手动指定）
  品级：遗物 / 古代（不填则按截图/共用规则展示）

示例：
  /首饰评分（附手镯截图）
  /首饰评分 项链 古代（附截图）

评分内容：
  · 项链/耳环/戒指：主属性百分比 + 词条 下/中/上 评级 + 战力倍率
  · 手镯：特殊词条 下/中/上 评级 + 特殊词条战力倍率（属性行另有 lopec 属性系数）

说明：
  · 识别需要支持图片输入的模型；可在插件配置指定「识别专用提供商」
  · 仅支持 T4（道具阶级 4）；T3 装备会直接提示不支持
  · 一次只评分第一张图，多件请分条发送
  · 词条与部位不符时会给出核对提醒，不改变评分"""


@register(
    PLUGIN_NAME,
    'moLing',
    '命运方舟首饰/手镯截图评分插件（lopec 口径）',
    PLUGIN_VERSION,
    'https://github.com/lingyu-or2/astrbot_plugin_loa_score',
)
class LoaScorePlugin(Star):

    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config or {}
        # 临时截图存 data/plugin_data/<插件名>/tmp/，防插件更新被覆盖
        self.data_dir = Path(get_astrbot_plugin_data_path()) / PLUGIN_NAME
        self.tmp_dir = self.data_dir / 'tmp'
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        removed = cleanup_stale_files(self.tmp_dir)
        if removed:
            logger.info(f'[loa_score] 已清理过期临时截图 {removed} 张')
        self.vision_enabled = bool(self.config.get('vision_enabled', True))
        self.vision_provider_id = str(self.config.get('vision_provider_id', '') or '').strip()
        self.vision_timeout = self._read_timeout()

    async def terminate(self):
        logger.info('[loa_score] 插件已卸载')

    def _read_timeout(self) -> float:
        try:
            value = float(self.config.get('vision_timeout', VISION_TIMEOUT))
        except (TypeError, ValueError):
            value = VISION_TIMEOUT
        return max(10.0, min(600.0, value))

    def _reload_config(self):
        self.vision_enabled = bool(self.config.get('vision_enabled', True))
        self.vision_provider_id = str(self.config.get('vision_provider_id', '') or '').strip()
        self.vision_timeout = self._read_timeout()

    async def _save_temp_image(self, image_data: bytes, ext: str) -> Path:
        # uuid 避免同一毫秒并发时互相覆盖
        path = self.tmp_dir / f'score_{uuid.uuid4().hex[:12]}.{ext}'

        def _write():
            path.write_bytes(image_data)

        await asyncio.to_thread(_write)
        return path

    @filter.command('首饰评分')
    async def score_item(self, event: AstrMessageEvent, arg1: str = '', arg2: str = ''):
        '''识别装备截图并按 lopec 口径评分

        Args:
            arg1: 可选，部位（手镯/项链/耳环/戒指）
            arg2: 可选，品级（遗物/古代）
        '''
        try:
            self._reload_config()
            if not self.vision_enabled:
                yield event.plain_result('⚠️ 截图评分功能已在插件配置中关闭（vision_enabled=false）。')
                return

            images = [comp for comp in event.get_messages() if isinstance(comp, Image)]
            if not images:
                yield event.plain_result(HELP_TEXT)
                return
            image_comp = images[0]

            # 指令参数可覆盖部位/品级（截图信息不全时手动指定）
            force_grade = None
            force_slot = None
            for arg in (arg1, arg2):
                key = arg.strip()
                if key in SLOT_ARG:
                    force_slot = SLOT_ARG[key]
                elif key in GRADE_ARG:
                    force_grade = GRADE_ARG[key]

            notice = (f'（检测到 {len(images)} 张图片，本次仅评分第一张）'
                      if len(images) > 1 else '')
            yield event.plain_result('🔍 正在识别截图，请稍候…' + notice)

            image_data, err = await acquire_image_bytes(image_comp)
            if not image_data:
                yield event.plain_result(f'❌ {err}')
                return
            is_valid, err, ext = validate_image(image_data)
            if not is_valid:
                yield event.plain_result(f'❌ 图片校验失败: {err}')
                return
            image_path = await self._save_temp_image(image_data, ext)

            try:
                item, err = await extract_item(
                    self.context, event, str(image_path),
                    provider_id=self.vision_provider_id,
                    timeout=self.vision_timeout)
            finally:
                try:
                    image_path.unlink(missing_ok=True)
                except OSError:
                    pass
            if item is None:
                yield event.plain_result(f'❌ {err}')
                return
            if force_slot:
                item['item_type'] = force_slot
            if force_grade:
                item['grade'] = force_grade

            result = grading.grade_item(item)
            yield event.plain_result(grading.format_result(result))
        except Exception as e:
            logger.error(f'[loa_score] /首饰评分 异常: {e}', exc_info=True)
            yield event.plain_result('❌ 评分过程出错，请稍后重试（详情见日志）。')

    @filter.command('评分帮助')
    async def score_help(self, event: AstrMessageEvent):
        '''显示首饰评分插件帮助'''
        try:
            yield event.plain_result(HELP_TEXT)
        except Exception as e:
            logger.error(f'[loa_score] /评分帮助 异常: {e}', exc_info=True)
            yield event.plain_result('❌ 帮助生成失败，请稍后重试。')
