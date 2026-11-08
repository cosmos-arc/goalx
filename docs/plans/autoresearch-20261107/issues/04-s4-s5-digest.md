# 票 04：S4 陈盘观测日 + S5 滚存扫描 + 周报

Status: ready-for-agent

## 交付

1. S4：每日陈盘信号统计（confirmed/fired/命中）落台账式留痕——
   "有分辨观测日"积累器（had_quote 闸 + stale_line_signal 面已在）。
2. S5：池期次兑付后回看（滚存标记/池规模/派彩分布），滚存窗 EV
   结构标注——首份历史回放（pool_periods×public_shares×draw_results
   存量）。
3. 周报 digest：周版汇总（各扫描器窗口统计+新台账行+晋升建议），
   挂 daily-wrap 周版或独立 flow；晨检 +1 项（扫描器绿）。

## 验收

- S4 首周出观测日表；S5 历史回放标注历史滚存期；周报样例一期。
