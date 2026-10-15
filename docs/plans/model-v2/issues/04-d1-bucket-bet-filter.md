# 04 D1 错价桶下注过滤：门控实验 + 同接受率安慰剂

Status: resolved
Spec: `.scratch/model-v2/spec.md` S4
Blocked by: （无——与 G 相并行，吃既有 DC 变体管线）

## 任务

- **引擎过滤开关**：`BacktestParams.bet_filter`（"off"|"buckets"|
  "random"，缺省 off 零行为变化）：
  - buckets：候选门控谓词——K3 同源 8 特征的条件（初版口径：候选
    selection 方向与 drift/late-move 方向一致，或相位特征在桶内——
    具体谓词以 K3 系数面前三特征定，报告记档谓词全文）；
  - random：确定性随机门（与 buckets **同接受率**——按 run 开头
    依 sid 哈希采样，门后接受率对齐 buckets 变体实测值）；
  - 门控作用于候选资格（kelly/结算不变）。
- **真树对照**：off vs buckets vs random 三 run（同底管线，如
  iso+era 融合），model-layers 报告：接受率/ROI/staked/每特征增量。
- 结论材料：buckets ROI 显著优于 random（同接受率）⇒ 桶特征含下注面
  信息（概率面压平≠下注面无用）；否则诚实留空（K3 结论补全）。

## 不变量与人裁决项

- random 门接受率对齐 buckets（不对齐=门槛削减收益冒充特征收益）；
  断言两变体接受率差 <1%；
- 谓词全文入报告（可复现可追认）；确定性（无随机源或固定种子）；
- 人裁决项：过滤进缺省（门=报告+点头）。

## Out of scope

- 概率调整（K3 已留空）；阈值调优扫参。


## Answer

2026-10-15 交付（分支 feat/bucket-filter-d1-20261015，PR #153，三提交，
合 main 39783f9）：

- **引擎** `bet_filter`（off/buckets/random）+ `_Candidate.bucket` +
  门控谓词（had 侧 selection 与 late_move 同号+死区 0.005、hhad/ttg
  侧 drift×move 同号）+ 同接受率随机门（sha256<21，两轮标定对齐
  buckets 注量 4188/4629≈9% 差）。
- **真树终审（run=14/15 vs 基线 5，注量对齐）**：
  - **buckets ROI −21.4% vs random −17.3%**——桶特征门控**主动选出
    更差的注**（比不筛选还差 5pp）；
  - **结论：8 桶特征在概率面（K3）和下注面（D1）都不构成可利用
    信号**。K3 的 6.1pp "分离"在同接受率对照下消失——signal-
    attribution 的桶级 |t|≥2 是窗内噪声不是出窗信号。
- 980 tests / type 0 / lint / fmt 绿。
