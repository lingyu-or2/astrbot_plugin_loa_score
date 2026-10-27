"""评分规则引擎（纯函数，可离线自测）。

数据来源 data/tables.json（构建脚本在 战力插件/.lopec_research/generate_tables.py）：
- 饰品（项链/耳环/戒指）：主属性百分比 = (stat-min)/区间×100（lopec 模块 72761）；
  부여 효과 按 T4 遗物/古代的 下/中/上 数值阶梯评级；战力倍率按 lopec 模块 47608 权重。
- 手镯：特殊选项按 T4 模板（语义子句签名）匹配，输出遗物/古代的 下/中/上 评级
  与战力倍率（值/10000+1 连乘，lopec 模块 21989/47608）；属性行不参与评级。
"""

import json
from collections import Counter
from pathlib import Path

from . import cnparse

GRADE_CN = {'하': '下', '중': '中', '상': '上'}
GRADE_ORDER_CN = ['下', '中', '上']
SLOT_CN = {'necklace': '项链', 'earring': '耳环', 'ring': '戒指', 'bracelet': '手镯'}
GRADE_TYPE_CN = {'relic': '遗物', 'ancient': '古代'}

_TABLES = None


def load_tables():
    """加载（并缓存）规则表。tables.json 与 core/ 同级位于插件 data/ 目录。"""
    global _TABLES
    if _TABLES is None:
        path = Path(__file__).resolve().parent.parent / 'data' / 'tables.json'
        with open(path, encoding='utf-8') as f:
            _TABLES = json.load(f)
    return _TABLES


def _sig(clause):
    return (clause['type'], tuple(clause['values']))


def _norm_clauses(clauses):
    """数值归一后的子句签名多重集。"""
    pool = Counter()
    for c in clauses:
        pool[(c['type'], tuple(round(float(v), 2) for v in c['values']))] += 1
    return pool


def cover_match(clauses, templates):
    """把手镯子句池按模板覆盖切分（贪心：每次取能覆盖最多子句的模板）。

    返回 (matched_templates, leftover_clause_tuples)。
    """
    pool = _norm_clauses(clauses)
    sigs = []
    for t in templates:
        need = Counter()
        for c in t['clauses']:
            need[(c['type'], tuple(round(float(v), 2) for v in c['values']))] += 1
        sigs.append((t, need))
    matched = []
    while True:
        best = None
        for t, need in sigs:
            if not need or not need <= pool:
                continue
            total = sum(need.values())
            if best is None or total > best[2]:
                best = (t, need, total)
        if best is None:
            break
        pool -= best[1]
        matched.append(best[0])
    return matched, pool


_FLAT_LABEL = {
    'hp_flat': '最大生命力', 'mana_flat': '最大法力',
    'watk_flat': '武器攻击力', 'watk_flat_sent': '武器攻击力',
    'phys_def': '物理防御力', 'magic_def': '魔法防御力',
    'hp_regen': '战斗中生命力回复量', 'resource_regen': '战斗资源自然恢复量',
    'crit_rate_plus': '暴击率', 'crit_dmg_plus': '暴击伤害',
    'add_dmg_plus': '额外伤害', 'ally_atk_plus': '我方攻击力强化效果',
    'ally_dmg_plus': '我方伤害量强化效果',
}
_PLUS_PCT = {'crit_rate_plus', 'crit_dmg_plus', 'add_dmg_plus',
             'ally_atk_plus', 'ally_dmg_plus'}


def _template_text(t: dict) -> str:
    """模板的人类可读描述（由子句生成）。"""
    parts = []
    for c in t['clauses']:
        ctype, vs = c['type'], c['values']
        desc = cnparse.CLAUSE_TYPES.get(ctype, ctype)
        if ctype == 'max_stack':
            parts.append(f'最多叠加{vs[0]:g}层')
        elif ctype in _FLAT_LABEL:
            suffix = '%' if ctype in _PLUS_PCT else ''
            parts.append(f'{_FLAT_LABEL[ctype]}+{vs[0]:g}{suffix}')
        elif ctype == 'watk_speed_buff':
            parts.append(f'攻击命中后{vs[0]:g}秒内武器攻击力+{vs[1]:g}，攻/移速+{vs[2]:g}%')
        elif ctype == 'watk_hp_cond':
            parts.append(f'生命>{vs[0]:g}%时，命中后{vs[1]:g}秒武器攻击力+{vs[2]:g}')
        elif ctype == 'watk_interval':
            parts.append(f'每{vs[0]:g}秒触发，{vs[1]:g}秒内武器攻击力+{vs[2]:g}')
        elif ctype == 'cdr_exchange':
            parts.append(f'技能冷却+{vs[0]:g}%，对敌伤害+{vs[1]:g}%')
        elif ctype == 'enemy_stagger':
            parts.append(f'对敌伤害+{vs[0]:g}%，无力化状态敌伤+{vs[1]:g}%')
        elif ctype == 'protect_buff_dmg':
            parts.append(f'受队友保护效果{vs[0]:g}秒内对敌伤害+{vs[1]:g}%')
        elif ctype in ('def_debuff', 'critres_debuff', 'critdmgres_debuff'):
            parts.append(f'{desc}{vs[1]:g}%（{vs[0]:g}秒）')
        elif ctype == 'superarmor':
            parts.append(f'{desc}（{vs[0]:g}秒）')
        else:
            parts.append(f'{desc}{vs[0]:g}%')
    return ' & '.join(parts)


def grade_bracelet(item: dict, force_grade: str = None):
    """手镯评分。返回结果 dict（见 format_result）。"""
    tables = load_tables()
    effects = item.get('options', [])
    clauses, unknown = cnparse.parse_effect_clauses(
        [{'text': e['raw'] if isinstance(e, dict) else e,
          'tag': e.get('tag') if isinstance(e, dict) else None} for e in effects])
    matched, leftover = cover_match(clauses, tables['bracelet_templates'])

    grade_type = force_grade or item.get('grade')
    options = []
    total_mult = 1.0
    for t in matched:
        mult = t.get('atk_mult') or 0
        if mult:
            total_mult *= (mult / 10000.0 + 1)
        options.append({
            'text': _template_text(t),
            'grade_relic': t.get('grade_relic'),
            'grade_ancient': t.get('grade_ancient'),
            'atk_mult': mult,
        })
    leftover_parts = []
    for (ctype, values), count in leftover.items():
        desc = cnparse.CLAUSE_TYPES.get(ctype, ctype)
        leftover_parts.append(f'{desc} ' + '/'.join(f'{v:g}' for v in values)
                              + (f' ×{count}' if count > 1 else ''))
    # 手镯的主属性（如 敏捷+14016）也是属性行，并入展示
    stats = list(item.get('stats') or [])
    if item.get('main_stat'):
        stats = [dict(item['main_stat'])] + stats
    return {
        'slot': 'bracelet',
        'grade_type': grade_type,
        'stats': stats,
        'main_stat': None,
        'stat_percent': None,
        'options': options,
        'total_mult': total_mult,
        'leftovers': leftover_parts + list(unknown),
    }


def grade_accessory(item: dict, force_grade: str = None):
    """项链/耳环/戒指评分。"""
    tables = load_tables()
    slot = item.get('item_type')
    grade_type = force_grade or item.get('grade')
    rng = tables['stat_ranges'].get(slot, {})
    stat_percent = None
    main_stat = item.get('main_stat')
    percent_note = ''
    if main_stat and rng.get('range'):
        stat = float(main_stat['value'])
        stat_percent = round((stat - rng['min']) / rng['range'] * 100, 2)
        if not (rng['min'] - 5 <= stat <= rng['max'] + 5):
            percent_note = (f'⚠️ 主属性数值超出 T4 {SLOT_CN.get(slot, slot)}区间'
                            f'（{rng["min"]}~{rng["max"]}），请核对截图')
    elif main_stat is None:
        percent_note = '未识别到主属性数值，跳过主属性百分比'

    kinds = tables['accessory_options']
    weights = tables['accessory_weights']
    options, total_mult = [], 1.0
    unknown = []
    for raw in item.get('options', []):
        text = raw['raw'] if isinstance(raw, dict) else raw
        parsed = cnparse.parse_accessory_option(text, kinds)
        if parsed is None:
            unknown.append(text)
            continue
        kind_kr, value = parsed
        info = kinds[kind_kr]
        # T4 权威词条池（2026-10-25 国服全量词条表确认）：遗物/古代共用同一套阶梯，
        # 即 lopec 表的「T4 고대」行；「T4 유물」低档行在国服 T4 不存在，不做回退。
        vals = info.get('ancient') or []
        grade_idx = vals.index(value) if value in vals else None
        ladder_note = ''
        if grade_idx is None:
            ladder_note = f'（不在 T4 数值池内，该类阶梯为 {" / ".join(f"{v:g}" for v in vals)}）'
        weight = weights.get(kind_kr)
        opt_mult = None
        if weight and grade_idx is not None:
            # 仅对权威数值池内的数值计算战力倍率，池外值（多为识别误差）不给倍率
            opt_mult = 1 + value * weight
            total_mult *= opt_mult
        options.append({
            'kind': kind_kr,
            'cn': (info.get('cn') or [kind_kr])[0],
            'value': value,
            'percent': info.get('percent', True),
            'grade_type': 'ancient' if grade_idx is not None else None,
            'grade_idx': grade_idx,
            'ladder': vals,
            'ladder_note': ladder_note,
            'opt_mult': opt_mult,
        })
    return {
        'slot': slot,
        'grade_type': grade_type,
        'quality': item.get('quality'),
        'stats': item.get('stats', []),
        'main_stat': main_stat,
        'stat_percent': stat_percent,
        'percent_note': percent_note,
        'options': options,
        'total_mult': total_mult if len(options) else None,
        'leftovers': unknown,
    }


def T2CN(gt):
    return GRADE_TYPE_CN.get(gt, gt or '未知品级')


def grade_item(item: dict, force_grade: str = None):
    """统一入口。item 来自 vision 识别结果（经 cnparse 规范化）。"""
    slot = item.get('item_type')
    if slot == 'bracelet':
        return grade_bracelet(item, force_grade)
    if slot in ('necklace', 'earring', 'ring'):
        return grade_accessory(item, force_grade)
    return {'slot': slot, 'error': f'暂不支持的部位类型: {slot or "未知"}（仅支持 项链/耳环/戒指/手镯）'}


# ---------- 输出格式化 ----------

def format_grade(gt: str, g) -> str:
    if not g:
        return '—'
    return GRADE_CN.get(g, g)


def format_option_grade(opt: dict) -> str:
    """饰品选项评级描述（T4 权威数值池，下/中/上）。"""
    if opt.get('grade_idx') is None:
        return f'未匹配档位{opt.get("ladder_note", "")}'
    idx = opt['grade_idx']
    ladder = opt.get('ladder') or []
    label = GRADE_ORDER_CN[idx] if idx < len(GRADE_ORDER_CN) else str(idx)
    ladder_txt = ' / '.join(f'{v:g}' for v in ladder)
    return f'T4「{label}」（{ladder_txt}）{opt.get("ladder_note", "")}'


def format_result(result: dict) -> str:
    if result.get('error'):
        return f'❌ {result["error"]}'
    slot = result.get('slot')
    lines = ['📊 命运方舟装备评分（lopec 口径 · 仅 T4）', '']
    grade_type = result.get('grade_type')
    slot_txt = SLOT_CN.get(slot, slot or '未知')
    lines.append(f'🧿 装备：{slot_txt}｜{T2CN(grade_type)}' if grade_type in ('relic', 'ancient')
                 else f'🧿 装备：{slot_txt}｜品级未知（同时给出遗物/古代两套评级）')

    stats = result.get('stats') or []
    if stats:
        stat_txt = ' / '.join(f"{s['name']}+{s['value']:g}" for s in stats)
        lines.append(f'属性：{stat_txt}' + ('（属性行不参与评级）' if slot == 'bracelet' else ''))

    if slot == 'bracelet':
        lines.append('')
        lines.append('【特殊词条】')
        if not result['options']:
            lines.append('（未匹配到 T4 特殊词条模板）')
        for i, opt in enumerate(result['options'], 1):
            rel = format_grade('relic', opt['grade_relic'])
            anc = format_grade('ancient', opt['grade_ancient'])
            mult = f'｜战力倍率 +{opt["atk_mult"] / 100:.2f}%' if opt['atk_mult'] else ''
            lines.append(f'{i}. {opt["text"]}')
            lines.append(f'    评级：遗物「{rel}」 / 古代「{anc}」{mult}')
        tm = result.get('total_mult')
        if tm and tm != 1.0:
            lines.append(f'➡ 特殊词条合计战力倍率：×{tm:.4f}（{(tm - 1) * 100:+.2f}%）')
    else:
        ms = result.get('main_stat')
        lines.append('')
        if result.get('stat_percent') is not None:
            rng = load_tables()['stat_ranges'].get(slot, {})
            lines.append(
                f'主属性：{ms["name"]} {ms["value"]:g} → lopec 百分比 '
                f'{result["stat_percent"]:.2f}%（区间 {rng.get("min")}~{rng.get("max")}）')
            if result.get('quality') is not None:
                lines.append(f'游戏内品质：{result["quality"]:g}'
                             '（品质与 lopec 百分比口径不同，非线性对应）')
        elif result.get('percent_note'):
            lines.append(f'主属性：{result["percent_note"]}')
        lines.append('')
        lines.append('【词条评级】')
        if not result['options']:
            lines.append('（未识别到可评级的 부여 효과 词条）')
        for i, opt in enumerate(result['options'], 1):
            val_txt = f'{opt["value"]:g}%' if opt.get('percent') else f'{opt["value"]:g}'
            grade_txt = format_option_grade(opt)
            mult_txt = f'｜战力倍率 +{(opt["opt_mult"] - 1) * 100:.3f}%' \
                if opt.get('opt_mult') and opt['opt_mult'] != 1.0 else ''
            lines.append(f'{i}. {opt["cn"]} +{val_txt}：{grade_txt}{mult_txt}')
        tm = result.get('total_mult')
        if tm and tm != 1.0:
            lines.append(f'➡ 词条合计战力倍率：×{tm:.4f}（{(tm - 1) * 100:+.3f}%）')

    if result.get('leftovers'):
        lines.append('')
        lines.append('⚠️ 以下内容不在 T4 规则表内，未参与评级：')
        for u in result['leftovers'][:6]:
            lines.append(f'    · {u}')
    lines.append('')
    lines.append('数据口径：lopec.kr（2026-10-25 逆向），评级为 T4 遗物/古代的 下/中/上 档。')
    return '\n'.join(lines)
