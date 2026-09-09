# -*- coding: utf-8 -*-
"""platforms.douyin.config — 抖音专属调参常量与接口子串集中地。

core 不携带平台调参；各抖音模块（search/modes）统一从这里取。
实测依据(2026-09)见各常量注释。
"""

# ---------- 会话/搜索调参 ----------

LOGIN_TIMEOUT = 120        # 扫码登录等待
VERIFY_WAIT = 240          # 滑块验证最长等待（实测用户可能不在屏幕前）
SCROLL_WAIT = 1.5          # 搜索页滚动间隔(秒)
MAX_IDLE_SCROLLS = 3       # 连续无新数据滚动次数上限
# 长跑定期重载搜索页（秒）：搜索结果卡片只增不删，DOM 无限膨胀是
# 通宵跑浏览器内存暴涨/崩溃的主因；重载后快进重扫已见内容（seen 去重）
SEARCH_RELOAD_S = 1500

# ---------- 抖音接口子串（XHR 拦截匹配用） ----------

# 搜索接口：标准/精选路由共用
SEARCH_URL_PREFIX = "aweme/v1/web/search/item/"
# root 入口 type=general 走综合搜索接口(general/search/single)——
# 视频/精选路由走 search/item，两种都要监听（散片模式用）
GENERAL_SEARCH_PREFIX = "aweme/v1/web/general/search/single/"
# 剧集面板接口有两种形态：合集(mix)与系列(series)，实测(2026-09)同一作者
# 只命中其中一种，两种都要拦
EPISODE_URL_SUBSTRS = ("/aweme/v1/web/mix/aweme/",
                       "/aweme/v1/web/series/aweme/")
# 作者主页作品流
USER_POST_URL_SUBSTR = "/aweme/v1/web/aweme/post/"
# 视频详情接口（浏览器兜底/付费检测拦截用）
DETAIL_SUBSTR = "/aweme/v1/web/aweme/detail/"
