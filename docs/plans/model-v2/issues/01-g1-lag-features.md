# 01 G1 滞后特征面：xG/技统/Elo 滚动 lag 特征

Status: resolved
Spec: `.scratch/model-v2/spec.md` S1
Blocked by: （无）

## 任务

- **特征模块** `modelling/lag_features.py`：gold match_features 行集
  （按 kickoff 定序）→ 每场赛前特征 dict（sid 键）：
  - 每队（league, team 键控）滚动统计（窗=最近 N 场，缺省 10）：
    xG for/against 均值（xG 覆盖 30-55%，**只对有 xG 的场滚均值，
    场数不足诚实留空不插补**）、射门/射正 for/against、净胜球、
    场均积分（胜 3 平 1）；
  - 两队对照差：Elo 差（elo_home_pre−elo_away_pre 全量）、以上滚动
    量的 home−away 差；
  - **严格 lag**：某场的特征只用 kickoff 严格早于它的同队比赛
    （同日多场也不入——保守 PIT，断言常开）。
- **确定性纯函数**：同输入同输出（无随机源）；不落 gold 树（每次
  run 从 gold 行即时算）。
- 单测：滚动窗边界/跨季连续/缺数据留空/同场隔离/差量口径手算对齐。

## 不变量与人裁决项

- 标签侧信息（xG/技统/比分）lag 化后才是特征——本票的全部意义；
  同场泄漏=构建失败（fail-loud 断言）；
- (league, team) 键控：跨联赛同名队不串（中文队名语料内唯一性由
  fixture_universe 保证，league 维度隔离双保险）；
- 人裁决项：无（纯特征面；消费在 G2）。

## Out of scope

- GBM 拟合（G2）；特征落 gold 树；新特征工程（Elo 斜率等留下一迭代）。


## Answer

2026-10-15 交付（分支 feat/lag-features-g1-20261015，PR #151，两提交，
合 main d03f2a3）：

- **模块** `modelling/lag_features.py`：LagRow 投影 + build_lag_features
  （9 维差量特征：elo/xG×2/射门×2/射正×2/净胜/积分）；**同 kickoff 日
  批隔离**（先全体快照再全体入窗——同日互不可见，票面保守 PIT）；
  乱序输入 fail-loud 断言常开。
- **不插补纪律**：xG 配对观测一侧缺则整场不进两侧窗；xG 滚动样本
  <3 留空；计数特征缺场不进窗（显式 None 判）。
- **review 战果**：双轴六修含两个真 bug——xG 配对被拆（一侧缺原先
  被 else 0.0 污染对手窗）+ window 参数死签名（maxlen 冻结在模块常量
  调用方传 5 得 10 静默）。MIN_XG_SAMPLES 死常量转样本门；
  LAG_FEATURE_NAMES 死导出删（与 dict 双写漂移温床）。
- 966 tests / type 0 / lint / fmt 全绿；12 单测覆盖滚动窗/截断/
  跨季/联赛隔离/同日隔离/配对/样本门/window 生效/确定性/乱序。
