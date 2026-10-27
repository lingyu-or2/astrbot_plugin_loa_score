"""中文词条文本解析：把识别出的效果行解析成「语义子句签名」，供 grading 查表。

设计要点：
- 类型空间与 data/tables.json 中手镯模板的子句类型完全一致（构建期由韩文模板解析而来）；
- 解析顺序即优先级：复合/限定式在前（如"暴击时对敌伤害"先于"对敌伤害"）；
- LLM 识别结果里的 type 标签只作为解析提示，数值一律从原文提取；
- 属性行（敏捷+14016 等）与效果行分开解析。
"""

import re

_NUM = r'(\d+(?:\.\d+)?)'

# 子句类型 → 中文说明（LLM 提示词的枚举 + 文档）
CLAUSE_TYPES = {
    'crit_rate': '暴击率增加',
    'crit_final_dmg': '攻击触发暴击时，对敌人造成的伤害增加',
    'crit_dmg': '暴击伤害增加',
    'add_dmg': '额外伤害增加',
    'devil_dmg': '恶魔与大恶魔系伤害增加',
    'enemy_dmg': '对敌人造成的伤害增加（无暴击条件）',
    'enemy_stagger': '对敌人造成的伤害增加，且对无力化状态敌人伤害增加',
    'atk_move_speed': '攻击及移动速度增加',
    'watk_speed_buff': '攻击命中时持续期间武器攻击力增加，且攻击及移动速度增加',
    'watk_hp_cond': '自身生命满足条件期间武器攻击力增加',
    'watk_interval': '每隔一段时间武器攻击力增加',
    'cdr_exchange': '技能冷却时间增加，换取对敌人造成的伤害增加',
    'ally_atk': '我方（我军/队友）攻击力强化效果增加或提升',
    'ally_dmg': '我方（我军/队友）伤害量强化效果增加或提升',
    'party_protect_heal': '队友保护及回复效果增加',
    'protect_buff_dmg': '受队友保护效果时对敌人造成的伤害增加',
    'back_atk_dmg': '背击技能对敌人造成的伤害增加',
    'head_atk_dmg': '头击技能对敌人造成的伤害增加',
    'nondir_dmg': '非方向性技能对敌人造成的伤害增加',
    'seed_dmg': '对种子等级以下怪物伤害增加',
    'seed_def': '受种子等级以下怪物伤害减少',
    'def_debuff': '攻击命中时降低目标防御力',
    'critres_debuff': '攻击命中时降低目标暴击抗性/抵抗',
    'critdmgres_debuff': '攻击命中时降低目标暴击伤害抗性/抵抗',
    'superarmor': '攻击命中时获得僵直/受击异常免疫',
    'dash_cdr': '移动技能及起身技能冷却时间减少',
    'max_stack': '最大叠加层数',
    'hp_flat': '最大生命力+（固定值）',
    'mana_flat': '最大法力+（固定值）',
    'watk_flat': '武器攻击力+（固定值）',
    'watk_flat_sent': '武器攻击力增加（固定值）',
    'crit_rate_plus': '暴击率+（固定值）',
    'crit_dmg_plus': '暴击伤害+（固定值）',
    'add_dmg_plus': '额外伤害+（固定值）',
    'ally_atk_plus': '我方攻击力强化效果+（固定值）',
    'ally_dmg_plus': '我方伤害量强化效果+（固定值）',
    'phys_def': '物理防御力+',
    'magic_def': '魔法防御力+',
    'hp_regen': '战斗中生命力回复量+',
    'resource_regen': '战斗资源自然恢复量+',
}

# 中文子句模式（顺序即优先级；数值组与 tables.json 模板里该类型的 values 顺序一致）
CN_CLAUSE_PATTERNS = [
    ('watk_speed_buff', re.compile(
        r'每秒?(?:最多)?(?:持续)?' + _NUM + r'秒[^。；]*?武器攻击力增加' + _NUM +
        r'[^。；]*?攻击及移动速度增加' + _NUM + r'%')),
    ('watk_hp_cond', re.compile(
        r'生命力(?:在)?' + _NUM + r'%以上[^。；]*?' + _NUM + r'秒[^。；]*?武器攻击力增加' + _NUM)),
    ('watk_interval', re.compile(
        r'每' + _NUM + r'秒[^。；]*?' + _NUM + r'秒[^。；]*?武器攻击力增加' + _NUM)),
    ('cdr_exchange', re.compile(
        r'技能(?:的)?冷却时间增加' + _NUM + r'%[^。；]*?对敌人造成的伤害增加' + _NUM + r'%')),
    ('enemy_stagger', re.compile(
        r'对敌人造成的伤害增加' + _NUM + r'%[，,][^。；]*?伤害增加' + _NUM + r'%')),
    ('crit_final_dmg', re.compile(
        r'暴击时[，,]?\s*对敌人造成的伤害增加' + _NUM + r'%')),
    ('protect_buff_dmg', re.compile(
        r'保护效果[^。；]*?' + _NUM + r'秒[^。；]*?对敌人造成的伤害增加' + _NUM + r'%')),
    ('back_atk_dmg', re.compile(r'背击技能[^。；]*?伤害增加' + _NUM + r'%')),
    ('head_atk_dmg', re.compile(r'头击技能[^。；]*?伤害增加' + _NUM + r'%')),
    ('nondir_dmg', re.compile(r'非方向性技能[^。；]*?伤害增加' + _NUM + r'%')),
    ('seed_dmg', re.compile(r'种子等级以下怪物[^。；]*?伤害增加' + _NUM + r'%')),
    ('seed_def', re.compile(r'种子等级以下怪物[^。；]*?伤害减少' + _NUM + r'%')),
    ('def_debuff', re.compile(
        r'命中[^。；]*?' + _NUM + r'秒[^。；]*?防御力(?:降低|减少)' + _NUM + r'%')),
    ('critres_debuff', re.compile(
        r'命中[^。；]*?' + _NUM + r'秒[^。；]*?暴击(?:抗性|抵抗)(?:降低|减少)' + _NUM + r'%')),
    ('critdmgres_debuff', re.compile(
        r'命中[^。；]*?' + _NUM + r'秒[^。；]*?暴击伤害(?:抗性|抵抗)(?:降低|减少)' + _NUM + r'%')),
    ('superarmor', re.compile(
        r'命中[^。；]*?' + _NUM + r'秒[^。；]*?免疫僵直')),
    ('dash_cdr', re.compile(r'移动技能及起身技能冷却时间减少' + _NUM + r'%')),
    ('max_stack', re.compile(r'最多(?:可)?叠加' + _NUM + r'层')),
    ('max_stack', re.compile(r'最多' + _NUM + r'层')),
    ('crit_rate', re.compile(r'暴击率增加' + _NUM + r'%')),
    ('crit_dmg', re.compile(r'暴击伤害增加' + _NUM + r'%')),
    ('add_dmg', re.compile(r'额外伤害增加' + _NUM + r'%')),
    ('devil_dmg', re.compile(r'恶魔(?:与大恶魔|和大恶魔|系|类)[^。；]*?伤害增加' + _NUM + r'%')),
    ('atk_move_speed', re.compile(r'攻击(?:及|和|与)移动速度增加' + _NUM + r'%')),
    ('ally_atk', re.compile(
        r'(?:我方|我军|队友)攻击力强化效果(?:增加|提升)' + _NUM + r'%')),
    ('ally_dmg', re.compile(
        r'(?:我方|我军|队友)伤害量?强化效果(?:增加|提升)' + _NUM + r'%')),
    ('party_protect_heal', re.compile(
        r'(?:队友|我方)(?:保护及|保护与|的)?(?:护盾|保护)?(?:及|与|和)?(?:回复|恢复|治疗)效果增加' + _NUM + r'%')),
    ('enemy_dmg', re.compile(r'对敌人造成的伤害增加' + _NUM + r'%')),
    ('watk_flat_sent', re.compile(r'武器攻击力增加' + _NUM)),
    ('hp_flat', re.compile(r'最大生命力\+' + _NUM)),
    ('mana_flat', re.compile(r'最大法力\+' + _NUM)),
    ('watk_flat', re.compile(r'武器攻击力\+' + _NUM)),
    ('crit_rate_plus', re.compile(r'暴击率\+' + _NUM + r'%')),
    ('crit_dmg_plus', re.compile(r'暴击伤害\+' + _NUM + r'%')),
    ('add_dmg_plus', re.compile(r'额外伤害\+' + _NUM + r'%')),
    ('ally_atk_plus', re.compile(r'(?:我方|我军|队友)攻击力强化效果\+' + _NUM + r'%')),
    ('ally_dmg_plus', re.compile(r'(?:我方|我军|队友)伤害量?强化效果\+' + _NUM + r'%')),
    ('phys_def', re.compile(r'物理防御力\+' + _NUM)),
    ('magic_def', re.compile(r'魔法防御力\+' + _NUM)),
    ('hp_regen', re.compile(r'战斗中生命力回复量\+' + _NUM)),
    ('resource_regen', re.compile(r'战斗资源自然恢复量\+' + _NUM)),
]

# 属性行（手镯的 会心+84 / 敏捷+14016 等）
STAT_LINE_PATTERNS = [
    re.compile(r'^(力量|敏捷|智力|会心|专长|迅捷|制压|忍耐|熟练|体力)\s*\+\s*' + _NUM + r'%?$'),
    re.compile(r'^(힘|민첩|지능|치명|특화|신속|제압|인내|숙련|체력)\s*\+\s*' + _NUM + r'%?$'),
]

_CLEAN_RE = re.compile(r'[\s\u3000]+')
# 千分位逗号（3,250 → 3250）；只删夹在数字中间的，不碰文本里的中文逗号
_THOUSANDS_RE = re.compile(r'(?<=\d),(?=\d{3}(?!\d))')
_BULLET_RE = re.compile(r'^[•·●◆▪►#]+')
# 全角符号只归一这些（全角逗号保留，交给结构位处理）
_FULLWIDTH = str.maketrans({'％': '%', '＋': '+', '－': '-', '．': '.'})


def normalize_value(x) -> float:
    return round(float(x), 2)


def clean_line(line: str) -> str:
    text = (line or '').translate(_FULLWIDTH)
    text = _CLEAN_RE.sub('', text)
    text = _THOUSANDS_RE.sub('', text)
    text = _BULLET_RE.sub('', text)
    return text.strip('。.,，；;：:')


def is_stat_line(line: str):
    """属性行判定。返回 (名称, 数值) 或 None。"""
    line = clean_line(line)
    for pat in STAT_LINE_PATTERNS:
        m = pat.match(line)
        if m:
            return m.group(1), normalize_value(m.group(2))
    return None


def parse_option_line(line: str, tag: str = None):
    """把一条效果行解析成子句列表 [(type, [values...])]。

    tag 为 LLM 给出的类型提示：先只尝试该类型的模式，失败再全量尝试。
    解析不出任何已知子句时返回 None。
    """
    text = clean_line(line)
    if not text:
        return None
    patterns = list(CN_CLAUSE_PATTERNS)
    if tag:
        hint = [(t, p) for t, p in CN_CLAUSE_PATTERNS if t == tag]
        rest = [(t, p) for t, p in CN_CLAUSE_PATTERNS if t != tag]
        patterns = hint + rest
    found = []
    remaining = text
    for ctype, pat in patterns:
        m = pat.search(remaining)
        if not m:
            continue
        values = [normalize_value(x) for x in m.groups()]
        found.append((ctype, values))
        # 命中的部分从剩余文本中移除，允许一行包含多个子句
        remaining = remaining.replace(m.group(0), ' ', 1)
        if not remaining.strip('，,、 '):
            break
    return found or None


def parse_stat_lines(lines):
    """拆分属性行与效果行。返回 (stats, effect_lines)。"""
    stats, effects = [], []
    for raw in lines:
        text = raw.get('text') if isinstance(raw, dict) else raw
        tag = raw.get('type') if isinstance(raw, dict) else None
        if not isinstance(text, str) or not text.strip():
            continue
        stat = is_stat_line(text)
        if stat:
            stats.append({'name': stat[0], 'value': stat[1], 'raw': text})
        else:
            effects.append({'text': text.strip(), 'tag': tag})
    return stats, effects


# 纯说明性文字（不是词条本身），静默跳过，不算未知
IGNORABLE_CN_HINTS = [
    '仅限获得一个', '一支队伍', '队伍仅限', '해당 효과는 한 파티당',
    '无法赋予效果', '已装备', '角色已绑定',
]


def is_ignorable_line(line: str) -> bool:
    text = clean_line(line)
    return any(h in text for h in IGNORABLE_CN_HINTS)


def parse_effect_clauses(effects):
    """效果行 → 子句池。返回 (clauses, unknown_lines)。

    clauses: [{'type', 'values', 'raw'}]
    """
    clauses, unknown = [], []
    for eff in effects:
        text = eff['text'] if isinstance(eff, dict) else eff
        tag = eff.get('tag') if isinstance(eff, dict) else None
        if is_ignorable_line(text):
            continue
        parsed = parse_option_line(text, tag)
        if parsed:
            for ctype, values in parsed:
                clauses.append({'type': ctype, 'values': values, 'raw': text})
        else:
            unknown.append(text)
    return clauses, unknown


# ---------- 饰品 부여 효과（种类+数值型词条） ----------

def parse_accessory_option(line: str, kinds: dict):
    """解析饰品选项行 "额外伤害 +1.60%"（种类+数值型词条）。

    kinds 来自 tables['accessory_options']（含中文名别名；固定值变体的键带 @flat 后缀）。
    返回 (kind_kr, value) 或 None。

    百分号缺失/多写时，只有数值确实落在该类的 下/中/上 阶梯内才接受（防误配），
    因此 "额外伤害 +1.60" 与 "最大生命值 +3250%" 这类转写误差也能正确归类。
    """
    text = clean_line(line)
    # 去掉行尾括号说明（如「（不可叠加）」），不影响主体词条
    text = re.sub(r'[（(][^（）()]*[）)]$', '', text)
    m = re.match(r'^(.*?)[+\-]?\s*' + _NUM + r'\s*(%?)$', text)
    if not m:
        return None
    name = m.group(1).strip('+- ')
    value = normalize_value(m.group(2))
    is_pct = bool(m.group(3))
    if not name:
        return None

    def alias_strength(kind_kr: str, info: dict):
        best = (0, 0)
        for alias in list(info.get('cn', [])) + [kind_kr.replace('@flat', '')]:
            alias = alias.replace(' ', '')
            if not alias:
                continue
            if alias in name:
                cand = (2, len(alias))  # 正向包含优先
            elif name in alias:
                cand = (1, len(alias))  # 反向包含兜底
            else:
                continue
            if cand > best:
                best = cand
        return best if best != (0, 0) else None

    def in_ladder(info: dict) -> bool:
        for key in ('relic', 'ancient'):
            vals = [round(float(v), 2) for v in (info.get(key) or [])]
            if value in vals:
                return True
        return False

    exact = None
    loose = None
    for kind_kr, info in kinds.items():
        strength = alias_strength(kind_kr, info)
        if not strength:
            continue
        if info.get('percent', True) == is_pct:
            if exact is None or strength > exact[0]:
                exact = (strength, kind_kr)
        elif in_ladder(info):
            if loose is None or strength > loose[0]:
                loose = (strength, kind_kr)
    best = exact or loose
    if best is None:
        return None
    return best[1], value


def guess_item_type(text: str):
    """从文本猜部位类型（用于无 LLM 元数据时的兜底）。"""
    t = text or ''
    if '手镯' in t or '팔찌' in t:
        return 'bracelet'
    if '项链' in t or '목걸이' in t:
        return 'necklace'
    if '耳环' in t or '귀걸이' in t:
        return 'earring'
    if '戒指' in t or '반지' in t:
        return 'ring'
    return None


def guess_grade(text: str):
    """从文本猜品级。"""
    t = text or ''
    if '古代' in t or '고대' in t or '远古' in t:
        return 'ancient'
    if '遗物' in t or '유물' in t:
        return 'relic'
    return None
