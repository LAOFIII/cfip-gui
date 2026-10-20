# -*- coding: utf-8 -*-
"""预设优选源清单。

来源：https://github.com/MakkaPakka518/EmbyProxyCF-Lite
（该面板「预设优选源」下拉列表的静态快照，2026-10-20 整理）

面板把每个源以 <optgroup> + <option title="URL"> 的形式写死在页面里，
所以这里做的是静态快照，不依赖对方接口，也不会因为面板改版而突然失效。

格式：(分组名, [(显示名, 取 IP 的网址), ...])
"""

PRESET_SOURCES = [
    ('云枢全域 · 三网专线', [
        ('🌟 云枢全域融合(双栈) · 云枢全域诸源', 'https://ip.ehb.cc.cd/all?ips=100'),
        ('🚄 云枢移动起飞 · 云枢移动起飞', 'https://ip.ehb.cc.cd/cm?ips=100'),
        ('⚡ 云枢电信美西 · 云枢电信美西', 'https://ip.ehb.cc.cd/ct?ips=100'),
        ('🚀 云枢联通极速 · 云枢联通极速', 'https://ip.ehb.cc.cd/cu?ips=100'),
    ]),
    ('104.16.0.0/13 大段家族', [
        ('🏯 104 大段全域池 · 104大分类全域', 'https://ip.ehb.cc.cd/104?ips=100'),
        ('🚄 104.17 移动起飞段 · 104.17移动起飞', 'https://ip.ehb.cc.cd/104.17?ips=100'),
        ('🛡️ 104.19 移动抗QoS段 · 104.19防QoS', 'https://ip.ehb.cc.cd/104.19?ips=100'),
        ('📻 104.16 经典老牌段 · 104.16老牌广播', 'https://ip.ehb.cc.cd/104.16?ips=100'),
    ]),
    ('162.158.0.0/15 大段家族', [
        ('🏯 162 大段全域池 · 162大分类全域', 'https://ip.ehb.cc.cd/162?ips=100'),
        ('💎 162.159 SaaS/DoH段 · 162.159-saas高优', 'https://ip.ehb.cc.cd/162.159-saas?ips=100'),
        ('⚡ 162.159 电信美西最稳段 · 162.159-ct电信美西', 'https://ip.ehb.cc.cd/162.159-ct?ips=100'),
    ]),
    ('172.64.0.0/13 大段家族', [
        ('🏯 172 大段全域池 · 172大分类全域', 'https://ip.ehb.cc.cd/172?ips=100'),
        ('🚀 172.64 联通/电信主力段 · 172.64联通主力', 'https://ip.ehb.cc.cd/172.64?ips=100'),
        ('🌐 172.67 动态反代段 · 172.67动态反代', 'https://ip.ehb.cc.cd/172.67?ips=100'),
    ]),
    ('198 & 108 专线家族', [
        ('🚀 198.41 联通天花板大段 · 198联通天花板', 'https://ip.ehb.cc.cd/198?ips=100'),
        ('🛡️ 108.162 三网兜底大段 · 108三网兜底', 'https://ip.ehb.cc.cd/108?ips=100'),
    ]),
    ('IPv6 霄汉星轨家族', [
        ('🌌 云枢 IPv6 全域星轨 · 云枢IPv6全域', 'https://ip.ehb.cc.cd/v6?ips=100'),
        ('📱 云枢移动 5G 直连 · 云枢移动5G', 'https://ip.ehb.cc.cd/v6-cm?ips=100'),
        ('⚡ 云枢电信 纯净美西 · 云枢电信美西v6', 'https://ip.ehb.cc.cd/v6-ct?ips=100'),
        ('🚀 云枢联通 欧洲/东京 · 云枢联通欧洲v6', 'https://ip.ehb.cc.cd/v6-cu?ips=100'),
        ('🌏 2400:cb00 亚太全域 · 2400亚太全域', 'https://ip.ehb.cc.cd/2400?ips=100'),
        ('🇭🇰 2400.2048 香港近场段 · 2400.2048香港近场', 'https://ip.ehb.cc.cd/2400.2048?ips=100'),
        ('⚡ 2400.400 亚太低跳数段 · 2400.400亚太低跳', 'https://ip.ehb.cc.cd/2400.400?ips=100'),
        ('🛡️ 2400.2049 亚太抗抖动段 · 2400.2049抗抖动', 'https://ip.ehb.cc.cd/2400.2049?ips=100'),
        ('🌐 2606:4700 全球主干 · 2606全球主干', 'https://ip.ehb.cc.cd/2606?ips=100'),
        ('🏢 2606.3030 核心骨干段 · 2606.3030核心骨干', 'https://ip.ehb.cc.cd/2606.3030?ips=100'),
        ('👑 2606.4700 伴生特权段 · 2606.4700特权伴生', 'https://ip.ehb.cc.cd/2606.4700?ips=100'),
        ('🚄 2a06:98c0 欧洲特快大段 · 2a06欧洲特快', 'https://ip.ehb.cc.cd/2a06?ips=100'),
    ]),
    ('第三方备用源', [
        ('🌟 @robberer 专属 · @robberer',
         'https://gh.xxooo.cf/https://raw.githubusercontent.com/hc990275/yx/refs/heads/main/cfyxip.txt'),
        ('⚡ CM佬电信优选 · CM佬电信优选', 'https://cf.090227.xyz/ct?ips=100'),
        ('🚄 CM佬移动优选 · CM佬移动优选', 'https://cf.090227.xyz/cmcc?ips=100'),
        ('🚀 CM佬联通优选 · CM佬联通优选', 'https://cf.090227.xyz/cu?ips=100'),
        ('💡 v2too 源 · v2too默认源', 'https://ip.v2too.top/api/nodes'),
    ]),
]


def flat_sources():
    """展开成 [(分组, 显示名, 网址), ...]，便于下拉框逐项列出。"""
    out = []
    for group, items in PRESET_SOURCES:
        for label, url in items:
            out.append((group, label, url))
    return out


def short_name(label):
    """从「🌟 显示名 · 备注」里取出显示名部分。"""
    return label.split('·')[0].strip()
