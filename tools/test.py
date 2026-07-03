# -*- coding: utf-8 -*-
"""Wind 字段简单试跑（在 tools 目录或项目根执行：python tools/test.py）。"""

from WindPy import w

w.start()

# 多券单日 wsd
# data = w.wsd(
#     "111012.SH,111014.SH,111015.SH,111017.SH",
#     "latestissurercreditrating",
#     "2026-06-01",
#     "2026-06-01",
#     "",
# )
# print("D", data)
#
# # 单券区间 / 单日 / 其它券
# print("A", w.wsd("111012.SH", "latestissurercreditrating", "2026-06-01", "2026-06-09", ""))
# print("B", w.wsd("111012.SH", "latestissurercreditrating", "2026-06-01", "2026-06-01", ""))
# print("C", w.wsd("110073.SH", "latestissurercreditrating", "2026-06-01", "2026-06-01", ""))

data = w.wss("111012.SH,111014.SH,111015.SH", "latestissurercreditrating","2026-06-01")
print("D", data)

# codes = "128124.SZ,128125.SZ,128127.SZ"
# d = "tradeDate=20260610"
# print("无 rfIndex:", w.wss(codes, "impliedvol", d))
# print("有 rfIndex:", w.wss(codes, "impliedvol", d + ";rfIndex=1"))
