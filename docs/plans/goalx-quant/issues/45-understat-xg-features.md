# Understat xG 特征层（最小引入）

Type: impl
Status: resolved
Blocked by: none

## 问题

research/17 §一 重评 + 用户定案（2026-09-20 开票）：Understat 原"不进主力"判据 ②③已过时——联赛页 RC4 内嵌已撤，改纯 JSON AJAX `GET /getLeagueData/{league}/{season}`（首页取 PHPSESSID + Referer/XHR 头，~530KB 明文，无 Cloudflare）。xG/xGA 是公开域最强进球预测器（Mead 2023，xG 显著优于进球指标）：DC 是进球分布模型，攻防参数全季受益（早赛季小样本收缩边际最大、随赛季递减不清零）；ttg/crs/hafu/大小球等进球类玩法全部下游受益。每场自带 `forecast{w,d,l}` = 免费对标基准。覆盖仅五大+俄超（判据 ①仍在）——竞彩混编中的 J1/澳超/英冠场次不覆盖，不指望它。

## 范围

1. `data/ingest/understat` 采集模块：五大 getLeagueData，每日 ≤10 请求（俄超按需）；网络薄 + 解析纯函数 + fixture 实测样本，沿用既有 ingest 模式
2. **xG 特征表**（挂 fixtures 确定性键）：逐场 xG/xGA + 队级累计 npxG/npxGA；三时间齐全（observed_at 不伪造）；**特征生成只能用决策时点前已完场的累计**（getLeagueData 赛后滚动更新，防前视是本票红线，定则 2）
3. `forecast{w,d,l}` 存对标基准（进评测基准族）
4. DC 消费（最小形态）：早赛季累计 npxG/npxGA 收缩校准攻防参数，或 xG 版/进球版概率加权——票内实证对比后定
5. 不碰逐射门/球员级数据；robots 全站 Disallow 注记写进模块 docstring（个人研究低频使用）

## 不变量与人裁决项

- xG 进模型的融合方式（收缩校准 vs 概率加权 vs 实证对比后选）——票内出对比数据，终选待人裁决
- 覆盖确认：仅五大（俄超是否纳入）
- forecast 基准是否进前瞻评分集合（冻结口径，涉票 34 验证边界）

## Answer

### 实现（2026-09-20，feat/understat-xg 分支 fc8b5e2）

- **端点复核成立**：getLeagueData 纯 JSON 直通（首页暖 PHPSESSID + 浏览器
  UA/Referer/XHR，gzip ~40KB/联赛，无 Cloudflare）。载荷三实测坑：
  ①`teams.history` 时刻键是 `date`（`dates[]` 用 `datetime`）——首轮回填
  全库 npxG 为空即此坑，已修+金丝雀（完场缺 npxG 计数入 run 行）；
  ②forecast{w,d,l} **仅已赛场次携带**（未赛场次无此字段）——定位只能是
  历史对标基准，不是实时预测源；③数值一律字符串。
- **migrations v15**：`understat_matches`（现态 upsert by 源 match_id：逐场
  xG/xGA（含点球）+ npxG/npxGA（去点球，history 对齐）+ prior_* 本季累计 +
  forecast 三值 + fixture_id）+ `understat_sync_runs`（append-only）+
  source_coverage 复用（league_key=slug、coverage_date=起始年）。
  三时间：datetime_utc=event_time、observed_at/first_seen_at=本机观测、
  源不提供发布时间（无列不伪造）。
- **防前视红线**（评审 BLOCKER 修正后终态）：prior_* 只累计**开球日严格
  早于本场**的已完场 npxG——同日场次互不可见（同日 19:45/20:00 错峰早场
  的赛后终值不得泄入晚场决策）；每日 09:20 同步在当日开球前，天然无损。
  缺 npxG 的完场场跳过不按 0 计；未赛场次同样携带 prior（开球前已完场累计）。
- **确定性 join**（定则 1）：开球日 ±1 + 双队名经 team_aliases 解析唯一
  命中才落 fixture_id；窗口任一日歧义整体放弃（邻日候选不救回，评审修正）；
  未解析/歧义 → NULL + unmatched 计数。当前季实测 join 53 场竞彩。
- **采集纪律**：每日 1 首页 + 5 联赛 = 6 请求 ≤10 上限（调度 09:20 赶
  daily-capture 前）；单联赛失败/200 垃圾体 failed 跳过不炸整跑；404=
  not_covered；每请求间 0.5s 礼貌限速；robots Disallow 注记入模块 docstring。
- **模型数学件**（modelling/xg_dc，纯函数）：xG 版 DC = scipy 加权双
  Poisson（penaltyblog 把 goals 强转整型故自实现；Σattack=Σdefence=0
  参数化约束；**rho 不适用**——DC tau 修正定义在整型低比分格，浮点 xG
  无 0/1 格语义，文档化 rho=0）+ `shrink_dc_params` 收缩校准（速率空间
  混合 w=n/(n+k) → log 增量重归零均值，联赛水平/主场优势/ρ 不动）。
- **实证对比 runner**（evaluation/xg_compare）：walk-forward 周口径
  （M2 同源），corpus 全量载入、周 cutoff 严格 < 防前视、**本季增量训练**
  （评审修正：首版把目标季整体排除出训练池——升班马永不可训、收缩拿空
  累计 delta 恒 0，回归测试锁定）；变体 = goal_dc / xg_dc / blend_goal_xg
  （概率加权臂，had 线性池 50/50）/ shrink_k{6,10} / understat_forecast。
  语料自足于 understat 命名域（SQL 走 compare_rows，ADR-0008）。
- **语料落库**：2019-2026 八季 × 五联赛 14,312 场、12,689 完场 npxG 零缺失
  （2019 法甲 279 完场 = 疫情停摆季，源数据质量佐证）。

### 实证结果（五联赛 2022-2026 五季 walk-forward，7,284 场配对）

RPS（低=优）；delta = 对 goal_dc 配对差（负=优于基准）：

| 变体 | overall RPS | delta | wk1-4 delta | wk5-8 delta | wk9+ delta |
|---|---|---|---|---|---|
| goal_dc（基准） | 0.20158 | — | — | — | — |
| xg_dc（xG 版替换） | 0.20137 | -0.00021 | +0.00035 | -0.00075 | -0.00022 |
| blend_goal_xg（线性池） | 0.20041 | **-0.00117** | **-0.00139** | **-0.00161** | -0.00106 |
| shrink_k6 | 0.20047 | -0.00111 | +0.00191 | +0.00080 | -0.00193 |
| shrink_k10 | 0.19995 | -0.00163 | +0.00059 | -0.00039 | -0.00220 |
| understat_forecast | 0.16565 | -0.03585 | -0.03959 | -0.03664 | -0.03509 |

判读（附数字实例）：

1. **早季节值假设未获支持（与票面预期相反）**：收缩校准在 wk1-4 反而
   伤害（k6 +0.0019 ≈ 把 RPS 从 0.1975 推高到 0.1994）——1-4 场的 npxG
   速率噪声大于其信息量；增益集中在 wk9+（k10 -0.0022，全季末段最强）。
   即 xG 累计的价值不在"早季收缩"而在"全季持续校准"。
2. **概率加权（blend）是唯一全桶一致改善的变体**：三桶全负、overall
   -0.0012（RPS 0.2016→0.2004，相对改善 ~0.6%）。稳健但幅度小。
3. **understat 自家 forecast 大幅领先所有 DC 变体**（RPS 0.166 vs 我们
   ~0.20，相对低 ~18%）——射门级模型的可达性上限参照。但该字段仅已赛
   场次携带，**不可作实时预测源**，只能进对标基准族（本票定位即此）。
   对 ML 线的含义：DC 基座的提升空间明确存在，但不来自本票的轻量融合。
4. 幅度量级对照：M2 结论 skill -3.75%（vs 市场）；本票最好融合 +0.6%
   RPS 改善——不改变"竞彩打平需 +37% 相对 edge"的格局判断。

### 待追认 → 已裁决（2026-09-21 用户"同意"）

1. **融合终选 = blend_goal_xg**（had 线性池 50/50）：已接线进 Forecast
   生成（[issues/47](47-xg-blend-forecast.md)）。
2. **俄超不纳入默认**：追认（按需 `understat-sync --leagues rfpl`）。
3. **forecast 基准进前瞻评分集合**：维持只进对比基准族，不进冻结集合。

### 评审与修正（两轴 sub-agent + 人工复核）

- Standards 轴：0 BLOCKER；SHOULD-FIX 已修三处——bucket_edges 解包
  （[1,5,9]→桶名）、join 同日歧义被邻日唯一候选"救回"（现窗口含歧义日
  整体放弃+测试）、cli 示例年份格式误导。NICE 已修：xg_dc docstring
  与重归一行为矛盾、forecast 三值残缺守卫、200 垃圾体整跑炸（现单联赛
  failed）、回填无限速（现 0.5s/请求）、export_schema_doc 把非唯一索引
  误标"唯一键"（生成器修，全文档受益）。
- Spec 轴：1 BLOCKER（防前视：同日错峰早场的赛后终值泄入晚场 prior——
  按日截断修正+专项测试）；SHOULD-FIX 已修（概率加权臂补齐、cli 示例）。
  五定则逐条过（§八）；留档未修：现态表无源值修订留痕（run 行+coverage
  为证据层，源值哈希待有复现需求再加）、join 冲突无逐条人工队列（unmatched
  信息位呈现——非竞彩场次占绝大多数，逐条队列是噪音）。
- 留档未修（判定可接受）：compare_rows 的 season 过滤在内存（每联赛
  全量 ~3k 行，索引可服务但成本可忽略）；prior_* 可随源修订回移
  （与完场态粘滞性不对称，docstring 注明，无生产消费者）。

### 质量门

`task fmt-check/lint/type/test` 全绿（后端 480 全绿，+21 新测试）；
import-linter kept；schema-doc 生成块同步（v15 两新表章节+注解，
export_schema_doc 唯一键误标修正全文档生效）；contract 零漂移；
web 未动（本票无 UI 契约）。
