"""astrbot_plugin_loa_score 离线自测（无需 AstrBot 环境）。

运行: python tests/run_tests.py
覆盖: 规则表完整性、中文子句解析、手镯模板覆盖匹配、饰品评级与主属性百分比、
格式化输出，以及 main/vision 源码语法检查（astrbot 未安装，仅 ast 解析）。
样本: 用户手镯截图词条（2026-10-25 实测）与 나신검 项链原始数据（lopec API）。
"""

import ast
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import cnparse, grading  # noqa: E402

PASS = 0
FAIL = []


def check(name, cond, detail=''):
    global PASS
    if cond:
        PASS += 1
    else:
        FAIL.append(f'{name}: {detail}')
        print(f'  ✗ {name} {detail}')


# ---------- 1. 规则表完整性 ----------
tables = grading.load_tables()
bt = tables['bracelet_templates']
check('模板数量=132', len(bt) == 132, f'实际 {len(bt)}')
check('饰品选项种类=18（16 种 + 攻击力/武器攻击力的固定值变体）',
      len(tables['accessory_options']) == 18, f'实际 {len(tables["accessory_options"])}')

sigs = set()
for t in bt:
    for c in t['clauses']:
        check(f'子句类型已知 {c["type"]}', c['type'] in cnparse.CLAUSE_TYPES, c['type'])
    sig = json.dumps(sorted((c['type'], tuple(c['values'])) for c in t['clauses']),
                     ensure_ascii=False)
    check(f'签名唯一 {t["key"][:30]}', sig not in sigs, t['key'])
    sigs.add(sig)
    for g in (t['grade_relic'], t['grade_ancient']):
        check(f'评级合法 {g}', g in (None, '하', '중', '상'), str(g))

# 关键模板抽查：用户手镯两条词条对应的模板必须存在
crit_combo = next((t for t in bt
                   if t['key'].startswith('치명타적중률이4.2%증가한다')), None)
check('치적4.2+치피1.5 模板存在', crit_combo is not None)
if crit_combo:
    check('  其遗物评级=상', crit_combo['grade_relic'] == '상', str(crit_combo['grade_relic']))
    check('  其古代评级=중', crit_combo['grade_ancient'] == '중', str(crit_combo['grade_ancient']))
    check('  其战力倍率=400', crit_combo['atk_mult'] == 400, str(crit_combo['atk_mult']))
add_devil = next((t for t in bt
                  if t['key'].startswith('추가피해가2.5%증가한다.악마')), None)
check('추피2.5+악마2.5 模板存在', add_devil is not None)
if add_devil:
    check('  其遗物评级=중', add_devil['grade_relic'] == '중', str(add_devil['grade_relic']))
    check('  其古代评级=하', add_devil['grade_ancient'] == '하', str(add_devil['grade_ancient']))
    check('  其战力倍率=350', add_devil['atk_mult'] == 350, str(add_devil['atk_mult']))

# ---------- 1.5 权威词条池（2026-10-25 国服全量词条表，54 项 = 18 类 × 下/中/上） ----------
POOL = {
    '추가 피해': [0.7, 1.6, 2.6],
    '적에게 주는 피해': [0.55, 1.2, 2.0],
    '세레나데, 신앙, 조화 게이지 획득량': [1.6, 3.6, 6.0],
    '낙인력': [2.15, 4.8, 8.0],
    '최대 생명력': [1300, 3250, 6500],
    '공격력@flat': [80, 195, 390],
    '무기 공격력@flat': [195, 480, 960],
    '최대 마나': [6, 15, 30],
    '상태이상 공격 지속시간': [0.2, 0.5, 1.0],
    '전투 중 생명력 회복량': [10, 25, 50],
    '공격력': [0.4, 0.95, 1.55],
    '무기 공격력': [0.8, 1.8, 3.0],
    '파티원 회복 효과': [0.95, 2.1, 3.5],
    '파티원 보호막 효과': [0.95, 2.1, 3.5],
    '치명타 적중률': [0.4, 0.95, 1.55],
    '치명타 피해': [1.1, 2.4, 4.0],
    '아군 공격력 강화 효과': [1.35, 3.0, 5.0],
    '아군 피해량 강화 효과': [2.0, 4.5, 7.5],
}
check('权威池种类数=18', len(POOL) == 18, str(len(POOL)))
for kind, ladder in POOL.items():
    got = tables['accessory_options'].get(kind, {}).get('ancient')
    check(f'权威池 {kind}', got == [round(v, 2) for v in ladder], f'{got} vs {ladder}')
# 数值池外的值不再回退遗物低档，直接报未匹配
r_pool = grading.grade_accessory({'item_type': 'necklace', 'grade': 'relic',
                                  'main_stat': None, 'stats': [],
                                  'options': [{'raw': '额外伤害 +1.00%'}]})
pool_opt = r_pool['options'][0]
check('池外数值报未匹配并给出阶梯', pool_opt['grade_idx'] is None
      and '0.7 / 1.6 / 2.6' in pool_opt['ladder_note'], str(pool_opt))
check('池外数值不给战力倍率', pool_opt['opt_mult'] is None, str(pool_opt))

# ---------- 2. 中文子句解析 ----------
cases = [
    ('暴击率增加4.2%。', [('crit_rate', [4.2])]),
    ('额外伤害增加2.5%。', [('add_dmg', [2.5])]),
    ('恶魔与大恶魔系伤害增加2.5%。', [('devil_dmg', [2.5])]),
    ('攻击触发暴击时，对敌人造成的伤害增加1.5%。',
     [('crit_final_dmg', [1.5])]),
    ('对敌人造成的伤害增加2%。', [('enemy_dmg', [2.0])]),
    ('对敌人造成的伤害增加5%，对无力化状态的敌人造成的伤害增加3%。',
     [('enemy_stagger', [5.0, 3.0])]),
    ('攻击及移动速度增加3%。', [('atk_move_speed', [3.0])]),
    ('技能冷却时间增加2%，但对敌人造成的伤害增加4%。',
     [('cdr_exchange', [2.0, 4.0])]),
    ('背击技能对敌人造成的伤害增加2%。', [('back_atk_dmg', [2.0])]),
    ('最大生命力+8400。', [('hp_flat', [8400.0])]),
    ('武器攻击力增加6300。', [('watk_flat_sent', [6300.0])]),
    ('自身生命力在50%以上时，攻击命中敌人期间5秒内武器攻击力增加1800。',
     [('watk_hp_cond', [50.0, 5.0, 1800.0])]),
    ('攻击命中时每秒最多持续10秒武器攻击力增加1480，攻击及移动速度增加1%。（最多可叠加6层）',
     [('watk_speed_buff', [10.0, 1480.0, 1.0]), ('max_stack', [6.0])]),
    ('我方攻击力强化效果增加2%。', [('ally_atk', [2.0])]),
    ('战斗中生命力回复量+25。', [('hp_regen', [25.0])]),
]
for text, expect in cases:
    got = cnparse.parse_option_line(text)
    got_norm = [(t, [round(v, 2) for v in vs]) for t, vs in (got or [])]
    check(f'解析「{text[:22]}…」', got_norm == [(t, [round(v, 2) for v in vs]) for t, vs in expect],
          f'得到 {got_norm}')

check('属性行 敏捷+14016', cnparse.is_stat_line('敏捷 +14016') == ('敏捷', 14016.0),
      str(cnparse.is_stat_line('敏捷 +14016')))
check('属性行 会心+84', cnparse.is_stat_line('会心 +84') == ('会心', 84.0),
      str(cnparse.is_stat_line('会心 +84')))
check('属性行不误判效果行', cnparse.is_stat_line('暴击率增加4.2%') is None)

# ---------- 3. 手镯评分（用户截图样本） ----------
user_bracelet = {
    'item_type': 'bracelet',
    'grade': None,  # 截图未带品级
    'main_stat': {'name': '敏捷', 'value': 14016},
    'stats': [{'name': '专长', 'value': 91}, {'name': '会心', 'value': 84}],
    'options': [
        {'raw': '额外伤害增加2.5%。', 'tag': None},
        {'raw': '恶魔与大恶魔系伤害增加2.5%。', 'tag': None},
        {'raw': '暴击率增加4.2%。', 'tag': None},
        {'raw': '攻击触发暴击时，对敌人造成的伤害增加1.5%。', 'tag': None},
    ],
}
res = grading.grade_bracelet(user_bracelet)
check('手镯匹配到 2 条模板', len(res['options']) == 2, f'{len(res["options"])}')
check('手镯无未识别残留', not res['leftovers'], str(res['leftovers']))
texts = [o['text'] for o in res['options']]
check('模板1=暴击组合', any('暴击率增加' in t for t in texts), str(texts))
check('模板2=额外+恶魔组合', any('额外伤害' in t for t in texts), str(texts))
check('合计倍率 ×1.0764', abs(res['total_mult'] - 1.0764) < 1e-9, str(res['total_mult']))

opt_crit = next(o for o in res['options'] if '暴击率增加' in o['text'])
check('暴击组合遗物=상', opt_crit['grade_relic'] == '상', str(opt_crit['grade_relic']))
check('暴击组合古代=중', opt_crit['grade_ancient'] == '중', str(opt_crit['grade_ancient']))
opt_add = next(o for o in res['options'] if '额外伤害' in o['text'])
check('追加组合遗物=중', opt_add['grade_relic'] == '중', str(opt_add['grade_relic']))
check('追加组合古代=하', opt_add['grade_ancient'] == '하', str(opt_add['grade_ancient']))

txt = grading.format_result(res)
check('输出含合计倍率', '×1.0764' in txt and '+7.64%' in txt, txt[:200])
check('输出含遗物「上」', '遗物「上」' in txt, txt[:300])
check('输出含古代「下」', '古代「下」' in txt, txt[:300])
check('输出含属性行说明', '敏捷+14016' in txt and '不参与评级' in txt, txt[:300])

# 覆盖切分：多出来的子句要报残留
res2 = grading.grade_bracelet({
    'item_type': 'bracelet', 'grade': 'ancient',
    'options': [{'raw': '暴击率增加4.2%。'}, {'raw': '攻击触发暴击时，对敌人造成的伤害增加1.5%。'},
                {'raw': '额外伤害增加9.9%。'}],
})
check('残留子句被报告', len(res2['leftovers']) == 1, str(res2['leftovers']))
check('残留含 9.9', any('9.9' in x for x in res2['leftovers']), str(res2['leftovers']))
anc_only = [o for o in res2['options']]
check('指定古代时输出仍含两套评级', anc_only and res2['options'][0]['grade_ancient'] == '중',
      str(res2['options']))

# ---------- 4. 项链评分（나신검 原始数据） ----------
nasingeom = {
    'item_type': 'necklace',
    'grade': None,
    'main_stat': {'name': '敏捷', 'value': 17295},
    'stats': [],
    'options': [
        {'raw': '额外伤害 +1.60%'},
        {'raw': '最大生命力 +1300'},
        {'raw': '对敌人造成的伤害 +1.20%'},
    ],
}
res3 = grading.grade_accessory(nasingeom)
check('项链主属性百分比=79.02', res3['stat_percent'] == 79.02, str(res3['stat_percent']))
by_cn = {o['cn']: o for o in res3['options']}
check('额外伤害1.60→古代中', by_cn['额外伤害']['grade_type'] == 'ancient'
      and by_cn['额外伤害']['grade_idx'] == 1, str(by_cn.get('额外伤害')))
check('对敌伤害1.20→古代中', by_cn['对敌人造成的伤害']['grade_type'] == 'ancient'
      and by_cn['对敌人造成的伤害']['grade_idx'] == 1, str(by_cn.get('对敌人造成的伤害')))
check('最大生命值可解析', '最大生命值' in by_cn, str(list(by_cn)))
expect_mult = (1 + 1.60 * 0.007692) * (1 + 1.20 * 0.01)
check('项链合计倍率', abs(res3['total_mult'] - expect_mult) < 1e-9,
      f'{res3["total_mult"]} vs {expect_mult}')
txt3 = grading.format_result(res3)
check('项链输出含 79.02%', '79.02%' in txt3, txt3[:300])
check('项链输出含区间', '15178~17857' in txt3, txt3[:300])
check('额外伤害显示 T4「中」', 'T4「中」（0.7 / 1.6 / 2.6）' in txt3, txt3[:400])

# T4 词条阶梯不分品级（实测：遗物装备也滚动古代档数值），强制遗物品级不改变评级
res3b = grading.grade_accessory(dict(nasingeom, grade='relic'))
by_cn3b = {o['cn']: o for o in res3b['options']}
check('遗物品级下仍按 T4 阶梯评级', by_cn3b['额外伤害']['grade_idx'] == 1
      and by_cn3b['额外伤害']['grade_type'] == 'ancient', str(by_cn3b['额外伤害']))

# 长名称优先：武器攻击力不被攻击力抢占
res3c = grading.grade_accessory({'item_type': 'earring', 'grade': 'ancient',
                                 'main_stat': {'name': '敏捷', 'value': 13535},
                                 'stats': [],
                                 'options': [{'raw': '武器攻击力 +1.80%'},
                                             {'raw': '攻击力 +1.55%'}]})
by_cn3c = {o['cn']: o for o in res3c['options']}
check('武器攻击力识别', '武器攻击力' in by_cn3c, str(list(by_cn3c)))
check('攻击力识别', '攻击力' in by_cn3c, str(list(by_cn3c)))
check('武器攻击力1.80→古代中', by_cn3c['武器攻击力']['grade_idx'] == 1, str(by_cn3c['武器攻击力']))
check('攻击力1.55→古代上', by_cn3c['攻击力']['grade_idx'] == 2, str(by_cn3c['攻击力']))

# ---------- 4.5 用户例图回归样本（2026-10-25 五张实测截图） ----------
# 图1 相迎终结项链（古代项链，品质94）
neck_img = {
    'item_type': 'necklace', 'grade': 'ancient', 'quality': 94,
    'main_stat': {'name': '力量', 'value': 17348}, 'stats': [{'name': '体力', 'value': 4062}],
    'options': [{'raw': '烙印力 +8.00%'}, {'raw': '对敌人造成的伤害+2.00%'},
                {'raw': '小夜曲、信仰、和谐能量获得量+1.60%'}],
}
r_img1 = grading.grade_accessory(neck_img)
check('图1 主属性 17348→81.0', r_img1['stat_percent'] == 81.0, str(r_img1['stat_percent']))
by1 = {o['cn']: o for o in r_img1['options']}
check('图1 烙印力8.0→上', by1['烙印力']['grade_idx'] == 2, str(by1.get('烙印力')))
check('图1 对敌2.0→上', by1['对敌人造成的伤害']['grade_idx'] == 2, str(by1.get('对敌人造成的伤害')))
check('图1 小夜曲1.6→下', by1['小夜曲、信仰、和谐能量获得量']['grade_idx'] == 0,
      str(by1.get('小夜曲、信仰、和谐能量获得量')))
check('图1 品质透传', r_img1['quality'] == 94)
txt1 = grading.format_result(r_img1)
check('图1 输出含品质口径说明', '品质 94' in txt1 or '品质：94' in txt1, txt1[:300])

# 图2 决战将至戒指（古代戒指，品质98，辅助词条）
ring_img = {
    'item_type': 'ring', 'grade': 'ancient', 'quality': 98,
    'main_stat': {'name': '敏捷', 'value': 12801}, 'stats': [{'name': '体力', 'value': 2340}],
    'options': [{'raw': '我军伤害量强化效果+7.50%'}, {'raw': '我军攻击力强化效果+3.00%'},
                {'raw': '暴击率 +0.95%'}],
}
r_img2 = grading.grade_accessory(ring_img)
check('图2 主属性 12801→95.04', r_img2['stat_percent'] == 95.04, str(r_img2['stat_percent']))
by2 = {o['cn']: o for o in r_img2['options']}
check('图2 我军伤害7.5→上', by2['我军伤害量强化效果']['grade_idx'] == 2, str(by2.get('我军伤害量强化效果')))
check('图2 我军攻击3.0→中', by2['我军攻击力强化效果']['grade_idx'] == 1, str(by2.get('我军攻击力强化效果')))
check('图2 暴击率0.95→中', by2['暴击率']['grade_idx'] == 1, str(by2.get('暴击率')))

# 图3 庄严决意耳环（遗物耳环，品质91，词条全部命中 T4 古代档阶梯）
ear_img = {
    'item_type': 'earring', 'grade': 'relic', 'quality': 91,
    'main_stat': {'name': '敏捷', 'value': 12869}, 'stats': [{'name': '体力', 'value': 2919}],
    'options': [{'raw': '队友护盾效果+2.10%'}, {'raw': '武器攻击力 +3.00%'},
                {'raw': '最大生命值 +3250'}],
}
r_img3 = grading.grade_accessory(ear_img)
check('图3 主属性 12869→51.03（lopec 口径）', r_img3['stat_percent'] == 51.03,
      str(r_img3['stat_percent']))
by3 = {o['cn']: o for o in r_img3['options']}
check('图3 护盾2.1→中', by3['队友护盾效果']['grade_idx'] == 1, str(by3.get('队友护盾效果')))
check('图3 武器攻击3.0→上', by3['武器攻击力']['grade_idx'] == 2, str(by3.get('武器攻击力')))
check('图3 最大生命值3250→中', by3['最大生命值']['grade_idx'] == 1, str(by3.get('最大生命值')))
check('图3 无未识别残留', not r_img3['leftovers'], str(r_img3['leftovers']))

# 图4 灿烂的救赎者手镯（古代，含 +型 追加伤害词条）
br1 = {
    'item_type': 'bracelet', 'grade': 'ancient',
    'main_stat': {'name': '智力', 'value': 9856},
    'stats': [{'name': '迅捷', 'value': 98}, {'name': '体力', 'value': 5080}],
    'options': [{'raw': '额外伤害 +3.50%'}, {'raw': '额外伤害增加2.5%。'},
                {'raw': '恶魔与大恶魔系伤害增加2.5%。'}],
}
r_img4 = grading.grade_bracelet(br1)
check('图4 匹配 2 条模板', len(r_img4['options']) == 2, str(len(r_img4['options'])))
check('图4 无残留', not r_img4['leftovers'], str(r_img4['leftovers']))
opt_plus = next(o for o in r_img4['options'] if '+' in o['text'] and '额外伤害' in o['text'])
check('图4 +型追加伤害 古代=中', opt_plus['grade_ancient'] == '중', str(opt_plus))
check('图4 +型倍率 269.22', abs(opt_plus['atk_mult'] - 269.22) < 0.01, str(opt_plus['atk_mult']))
opt_combo = next(o for o in r_img4['options'] if '恶魔' in o['text'])
check('图4 追加+恶魔组合 古代=下', opt_combo['grade_ancient'] == '하', str(opt_combo))
expect4 = (1 + 269.22 / 10000) * (1 + 350 / 10000)
check('图4 合计倍率', abs(r_img4['total_mult'] - expect4) < 1e-9, str(r_img4['total_mult']))
txt4 = grading.format_result(r_img4)
check('图4 属性行含智力', '智力+9856' in txt4, txt4[:300])

# 图5 灿烂的救赎者手镯（古代，防御力/暴伤抗性削弱 + 我军攻击强化 复合模板）
br2 = {
    'item_type': 'bracelet', 'grade': 'ancient',
    'main_stat': {'name': '压制', 'value': 114},
    'stats': [{'name': '专长', 'value': 69}, {'name': '体力', 'value': 4880}],
    'options': [
        {'raw': '攻击命中怪物时，在8秒内使目标的防御力减少1.8%。'},
        {'raw': '一支队伍仅限获得一个该效果。'},
        {'raw': '我军攻击力强化效果提升2%。'},
        {'raw': '攻击命中怪物时，在8秒内使目标的暴击伤害抗性减少4.8%。'},
        {'raw': '一支队伍仅限获得一个该效果。'},
        {'raw': '我军攻击力强化效果提升3%。'},
    ],
}
r_img5 = grading.grade_bracelet(br2)
check('图5 匹配 2 条复合模板', len(r_img5['options']) == 2, str(len(r_img5['options'])))
check('图5 说明行被忽略且无残留', not r_img5['leftovers'], str(r_img5['leftovers']))
opt_def = next(o for o in r_img5['options'] if '防御力' in o['text'])
check('图5 防御削弱组合 古代=下', opt_def['grade_ancient'] == '하', str(opt_def))
check('图5 防御削弱倍率 126', opt_def['atk_mult'] == 126, str(opt_def['atk_mult']))
opt_res = next(o for o in r_img5['options'] if '暴击伤害抗性' in o['text'])
check('图5 暴伤抗性组合 古代=上', opt_res['grade_ancient'] == '상', str(opt_res))
check('图5 暴伤抗性倍率 160', opt_res['atk_mult'] == 160, str(opt_res['atk_mult']))
expect5 = (1 + 126 / 10000) * (1 + 160 / 10000)
check('图5 合计倍率', abs(r_img5['total_mult'] - expect5) < 1e-9, str(r_img5['total_mult']))

# ---------- 5. 部位/品级兜底与错误路径 ----------
err_res = grading.grade_item({'item_type': None})
check('未知部位返回错误', 'error' in err_res, str(err_res))
check('guess_item_type', cnparse.guess_item_type('手镯效果') == 'bracelet')
check('guess_grade', cnparse.guess_grade('古代') == 'ancient')
res4 = grading.grade_bracelet({'item_type': 'bracelet', 'grade': 'unknown',
                               'options': [{'raw': ' Completely unknown text 123 '}]})
check('完全未知行进残留', len(res4['leftovers']) == 1, str(res4['leftovers']))

# ---------- 6. 语法检查（astrbot 未安装，仅 ast 解析） ----------
for py in ['main.py', 'core/cnparse.py', 'core/grading.py', 'core/imageio.py',
           'core/vision.py', 'core/__init__.py']:
    src = (ROOT / py).read_text(encoding='utf-8')
    try:
        ast.parse(src, filename=py)
        check(f'语法 {py}', True)
    except SyntaxError as e:
        check(f'语法 {py}', False, str(e))

# metadata/_conf_schema JSON/YAML 粗校验
conf = json.loads((ROOT / '_conf_schema.json').read_text(encoding='utf-8'))
check('配置模式含 vision_enabled', 'vision_enabled' in conf)
meta = (ROOT / 'metadata.yaml').read_text(encoding='utf-8')
check('metadata 含插件名', 'astrbot_plugin_loa_score' in meta)

# ---------- 6.5 真实导入验证（astrbot 打桩，拦截加载期错误） ----------
# 背景：vision.py 曾用 % 格式化拼接含全角'。'的提示词，import 时抛
# ValueError: unsupported format character，导致真机安装失败（2026-10-27）。
# ast.parse 查不出这类错误，必须真实导入。
import types as _types
import importlib as _importlib


def _install_astrbot_stubs():
    mods = {}
    astrbot = _types.ModuleType('astrbot')
    api = _types.ModuleType('astrbot.api')
    ev = _types.ModuleType('astrbot.api.event')
    star = _types.ModuleType('astrbot.api.star')
    comp = _types.ModuleType('astrbot.api.message_components')
    core = _types.ModuleType('astrbot.core')
    core_utils = _types.ModuleType('astrbot.core.utils')
    path_mod = _types.ModuleType('astrbot.core.utils.astrbot_path')

    class _Logger:
        def info(self, *a, **k):
            pass

        def warning(self, *a, **k):
            pass

        def error(self, *a, **k):
            pass

    class _Filter:
        def command(self, *a, **k):
            def deco(fn):
                return fn
            return deco

    api.logger = _Logger()
    api.AstrBotConfig = dict
    ev.filter = _Filter()
    ev.AstrMessageEvent = object
    star.Context = object
    star.Star = object
    star.register = lambda *a, **k: (lambda cls: cls)
    comp.Image = object
    path_mod.get_astrbot_plugin_data_path = lambda: str(ROOT / '.test_plugin_data')

    for name, mod in (('astrbot', astrbot), ('astrbot.api', api),
                      ('astrbot.api.event', ev), ('astrbot.api.star', star),
                      ('astrbot.api.message_components', comp),
                      ('astrbot.core', core), ('astrbot.core.utils', core_utils),
                      ('astrbot.core.utils.astrbot_path', path_mod)):
        sys.modules.setdefault(name, mod)
        mods[name] = mod
    return mods


_install_astrbot_stubs()
try:
    vision = _importlib.import_module('core.vision')
    check('core.vision 真实导入成功', True)
    check('vision 提示词构建成功', '暴击率增加' in vision.VISION_PROMPT
          and '- crit_rate:' in vision.VISION_PROMPT
          and 'not_equipment' in vision.VISION_PROMPT,
          vision.VISION_PROMPT[:120])
    check('vision 提示词无残留占位符', '__CLAUSE_LIST__' not in vision.VISION_PROMPT)
except Exception as e:
    check('core.vision 真实导入成功', False, repr(e))

try:
    main_mod = _importlib.import_module('main')
    check('main.py 真实导入成功（@register 装饰可执行）',
          hasattr(main_mod, 'LoaScorePlugin'), repr(getattr(main_mod, 'LoaScorePlugin', None)))
except Exception as e:
    check('main.py 真实导入成功', False, repr(e))

# ---------- 6.6 vision.extract_item 全路径（假 Provider，覆盖 2026-10-27 真机事故） ----------
import asyncio as _asyncio


class _Resp:
    def __init__(self, text):
        self.completion_text = text


class _FakeProvider:
    def __init__(self, text='{"error": "no_image_visible"}'):
        self.text = text
        self.calls = []

    async def text_chat(self, prompt=None, image_urls=None, **kw):
        self.calls.append({'prompt': prompt, 'image_urls': image_urls})
        return _Resp(self.text)

    def meta(self):
        return _types.SimpleNamespace(
            id='test_prov',
            model=_types.SimpleNamespace(name='test-vl-1', id='test-vl-1'))


class _FakeCtx:
    def __init__(self, provider):
        self._p = provider

    async def get_using_provider_async(self, umo=None):
        return self._p

    def get_provider_by_id(self, pid):
        return self._p if pid == 'vision_prov' else None


class _FakeEvent:
    unified_msg_origin = 'qq:group:test'


GOOD_JSON = ('{"item_type":"necklace","grade":"ancient","tier":4,"quality":94,'
             '"main_stat":{"name":"力量","value":"17,348"},'
             '"stats":[{"name":"体力","value":4062}],'
             '"options":[{"text":"烙印力 +8.00%","type":"ally_dmg"},'
             '{"text":"对敌人造成的伤害+2.00%"},{"text":"小夜曲、信仰、和谐能量获得量+1.60%"}]}')


def _run_extract(provider):
    return _asyncio.run(
        vision.extract_item(_FakeCtx(provider), _FakeEvent(), '/tmp/fake.png'))


p = _FakeProvider('```json\n' + GOOD_JSON + '\n```')
item, err = _run_extract(p)
check('识别成功路径', item is not None and err == '', f'{item} {err}')
if item:
    check('normalize: main_stat 千分位转数', item['main_stat']['value'] == 17348.0,
          str(item['main_stat']))
    check('normalize: 选项带 tag', item['options'][0]['tag'] == 'ally_dmg',
          str(item['options'][0]))
check('image_urls 传本地路径', p.calls and p.calls[0]['image_urls'] == ['/tmp/fake.png'],
      str(p.calls[:1]))

item, err = _run_extract(_FakeProvider('{"error": "no_image_visible"}'))
check('no_image_visible → 日志排查指引', item is None and '图片可能没有到达模型' in err
      and '当前会话模型：test_prov / test-vl-1' in err
      and '适配器:_FakeProvider' in err
      and '检测到图片请求失败' in err, err)

item, err = _run_extract(_FakeProvider('{"error": "not_equipment"}'))
check('not_equipment → 多模态指引', item is None and '多模态' in err, err)

item, err = _run_extract(_FakeProvider('这张图片我看不清楚哦，请重新发送~'))
check('闲聊回复 → 无法解析指引', item is None and '无法解析' in err
      and '当前会话模型' in err, err)

item, err = _run_extract(_FakeProvider(''))
check('空回复 → 指引', item is None and '不支持图片' in err, err)


class _NoneCtx:
    async def get_using_provider_async(self, umo=None):
        return None


item, err = _asyncio.run(
    vision.extract_item(_NoneCtx(), _FakeEvent(), '/tmp/fake.png'))
check('无提供商 → 明确指引', item is None and '没有可用的对话模型' in err, err)

# 指定识别专用提供商（vision_provider_id 路径）
item, err = _asyncio.run(vision.extract_item(
    _FakeCtx(_FakeProvider(GOOD_JSON)), _FakeEvent(), '/tmp/fake.png',
    provider_id='vision_prov'))
check('指定识别提供商成功路径', item is not None and err == '', f'{item} {err}')
item, err = _asyncio.run(vision.extract_item(
    _FakeCtx(_FakeProvider(GOOD_JSON)), _FakeEvent(), '/tmp/fake.png',
    provider_id='no_such'))
check('指定提供商不存在 → 明确指引', item is None and 'no_such' in err
      and 'vision_provider_id' in err, err)

# ---------- 6.7 _extract_json 修复管线（glm-4.5v 真机"无法解析"事故回归） ----------
XJ = vision._extract_json
robust_cases = {
    '裸JSON': '{"item_type":"necklace"}',
    '代码块': '```json\n{"item_type":"necklace"}\n```',
    '前后说明文字': '好的，识别结果如下：\n{"item_type":"necklace"}\n希望对你有帮助！',
    '照抄注释': '{"item_type":"necklace", // 部位\n"grade":"ancient"}',
    '块注释': '{/* 识别结果 */\n"item_type":"necklace"}',
    '尾逗号': '{"item_type":"necklace","stats":[{"name":"体力","value":1}],}',
    '中文引号冒号': '{"item_type"："necklace"，"grade"："ancient"}',
    '单引号': "{'item_type':'necklace','grade':'ancient'}",
    'Python字面量': '{"main_stat": None, "tier": null, "ok": True}',
    '注释+尾逗号叠加': '{\n"item_type":"bracelet", // 手镯\n"options":[],\n}',
    '字符串内斜杠不被注释误伤': '{"text":"https://a//b 不删"}',
}
for name, text in robust_cases.items():
    got = XJ(text)
    ok = isinstance(got, dict) and len(got) > 0
    if name == '字符串内斜杠不被注释误伤':
        ok = ok and got.get('text') == 'https://a//b 不删'
    check(f'提取 {name}', ok, repr(got))
check('无 JSON 的闲聊返回 None', XJ('这张图片我看不清楚哦~') is None)
check('数组顶层不算对象', XJ('[1,2,3]') is None)

print()
print(f'通过 {PASS} 项，失败 {len(FAIL)} 项')
if FAIL:
    for f in FAIL:
        print(' -', f)
    sys.exit(1)
