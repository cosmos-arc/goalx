# 存档：GoalX Data Platform V1 spec（24 表，外部推荐 B）

> 来源：2026-09-23 外部架构咨询原文，代称化转正（源T=srct、fdhist、oddsapi、propline）。
> 效力：仅作存档与 silver 逐张扩展的目标蓝图。其 PostgreSQL 底座、对象存储 RAW 永久
> 保存、模型栈（§31-40）三处已被 wayfinder 地图推翻或出图（ADR-0011 决策 1/2、票 05），
> 不具存档外效力。裁决记录：`.scratch/goalx-data-v2/map.md`；终审：`docs/adr/0011-corpusstore-data-v2.md`。

---

按现在已经收敛的方案，我建议把它正式定成 **GoalX Data Platform V1**。下面这版可以直接作为 repo 里的数据架构 spec：把 源T、竞彩、Understat、538、fdhist 的职责彻底分开，同时明确哪些东西暂时不再依赖。

# GoalX Data Platform V1

## 1. 架构目标

GoalX 当前的数据目标不是建立“全世界最大的足球数据库”，而是建立一套能够支撑：

* 足球概率模型训练
* 历史回测
* 赔率市场建模
* 竞彩价值发现
* 球员/阵容增强
* xG / 球队实力建模
* 后续 Agent 分析

的 **Point-in-Time 足球量化数据基座**。

核心原则：

> **Provider 可以换，GoalX Canonical Schema 不能跟着换。**

因此任何模型都禁止直接读取：

```text
srct_xxx
understat_xxx
fdhist_xxx
```

模型只能消费 GoalX 自己的：

```text
fixture
team
player
lineup
match_stats
xg_observation
odds_quote
sporttery_quote
feature_snapshot
```

---

# 2. 最终数据依赖

## 2.1 Production / Core

| 数据源          | 角色                        | 数据                                        |
| ------------ | ------------------------- | ----------------------------------------- |
| **源T** | 主足球数据源 + 主国际赔率源           | Fixture、球队、球员、阵容、伤停、事件、技术统计、赔率、赔率变化、近期 xG |
| **竞彩网官方**    | GoalX Target Market Truth | 竞彩比赛、SP、SP变化、让球、销售时间、赛果、支持率等              |

源T 计划覆盖最近约 10 年：

```text
Premier League
Championship
La Liga
Serie A
Bundesliga
Ligue 1

Eredivisie
Primeira Liga

Champions League
Europa League

Süper Lig
Scottish Premiership
Belgian Pro League
Allsvenskan
Eliteserien
```

后续可按实际竞彩覆盖增加赛事。

---

## 2.2 Historical / Supplement

| 数据源                             | 定位                                         |
| ------------------------------- | ------------------------------------------ |
| **Understat**                   | 五大联赛历史 xG / shot-level xG 主基准              |
| **FiveThirtyEight SPI Archive** | 2016～2023 非五大联赛历史 match-level xG / nsxG 补源 |
| **fdhist**               | 长历史结果 + bookmaker early/close odds 独立基准    |

---

## 2.3 Research Only

### StatsBomb Open Data

只进入：

```text
research/
```

用途：

```text
xG research
xT
VAEP
player impact
event modelling
```

不进入 GoalX Production Dependency。

---

# 3. 暂时取消的依赖

V1 **不依赖**：

```text
Betfair Historical
Sportmonks
API-Football
propline
oddsapi
FootyStats
WhoScored
国内付费 B2B 数据商
```

原因不是这些源没价值，而是当前都不存在不可替代性。

### API-Football

源T 正常时，Fixture / Player / Lineup / Injury / Stats 基本重复。

保留 Adapter 接口即可，不订阅。

### propline

未来如果需要：

```text
独立 Pinnacle price
market redundancy
低延迟 odds source
```

再加入。

现在 源T 已足够承担 Market Truth 主源。

### oddsapi

唯一核心优势是多年 bookmaker intraday snapshot。

如果 源T 历史赔率变化完整度达到要求，就没有采购必要。

### FootyStats

只有当：

```text
Understat + 538 + 源T
```

仍留下重要 xG 空洞时，再一次性购买补洞。

---

# 4. 整体数据流

```text
                           RAW SOURCES
                                │
          ┌─────────────────────┼────────────────────┐
          │                     │                    │
        源T               Sporttery            Open Data
          │                 Official API             │
          │                     │             ┌──────┼───────┐
          │                     │             │      │       │
          │                     │        Understat 538  FootballData
          │                     │
          ▼                     ▼
──────────────────────────────────────────────────────────
                       BRONZE / RAW
──────────────────────────────────────────────────────────
          │
          │ 原始 HTML / JSON / CSV 永久保存
          ▼
──────────────────────────────────────────────────────────
                   SILVER / CANONICAL
──────────────────────────────────────────────────────────
  competition / season / fixture / team / player
  lineup / availability / match_stats / events
  xg_observation / odds_quote / sporttery_quote
──────────────────────────────────────────────────────────
          │
          ▼
──────────────────────────────────────────────────────────
                    POINT-IN-TIME
──────────────────────────────────────────────────────────
                 fixture @ as_of_time

      “这个时间点 GoalX 真正知道什么？”
──────────────────────────────────────────────────────────
          │
          ▼
──────────────────────────────────────────────────────────
                         GOLD
──────────────────────────────────────────────────────────
 team_state
 player_state
 xg_features
 market_features
 lineup_features
 sporttery_features
 feature_snapshot
──────────────────────────────────────────────────────────
          │
          ▼
                     MODEL STACK
          │
          ├─ Football Fundamental Model
          ├─ Market Model
          ├─ Lineup Adjustment
          ├─ Probability Blender
          └─ Sporttery Edge Engine
```

---

# 5. 存储架构

V1 不需要 Kafka、Spark、Snowflake、Feature Store SaaS。

建议：

```text
PostgreSQL
    │
    ├─ Canonical relational data
    ├─ PIT observations
    ├─ metadata / mappings
    │
    ▼
Parquet
    │
    ▼
DuckDB / Python
    │
    ▼
Training / Research

Object Storage
    │
    └─ RAW HTML/JSON/CSV
```

推荐：

```text
PostgreSQL
+
Parquet
+
DuckDB
+
S3 / R2 / MinIO
```

赔率规模未来达到数千万甚至上亿行，再考虑：

```text
ClickHouse
```

V1 不需要。

---

# 6. RAW Layer

## `raw_resource`

所有 provider 原始数据必须保存。

| 字段                | 类型                   | 说明                                                              |
| ----------------- | -------------------- | --------------------------------------------------------------- |
| id                | UUID                 | PK                                                              |
| provider          | varchar              | SRCT / SPORTTERY / UNDERSTAT / FIVETHIRTYEIGHT / FDHIST |
| dataset           | varchar              | fixture / lineup / odds / xg / stats 等                          |
| source_uri        | text                 | 来源                                                              |
| fetched_at        | timestamptz          | GoalX 获取时间                                                      |
| source_updated_at | timestamptz nullable | 来源自身更新时间                                                        |
| content_hash      | varchar              | SHA256                                                          |
| storage_path      | text                 | object storage                                                  |
| parser_version    | varchar              | parser版本                                                        |
| http_status       | int nullable         | HTTP状态                                                          |
| metadata          | jsonb                | request参数等                                                      |

原则：

```text
RAW immutable
```

永不覆盖。

---

## `ingestion_run`

| 字段               | 说明                         |
| ---------------- | -------------------------- |
| id               | PK                         |
| provider         | 数据源                        |
| dataset          | 数据集                        |
| started_at       | 开始                         |
| finished_at      | 完成                         |
| request_count    | 请求数量                       |
| records_seen     | 原始记录                       |
| records_inserted | 新数据                        |
| records_updated  | 新版本                        |
| error_count      | 错误                         |
| parser_version   | Parser                     |
| status           | success / partial / failed |

---

# 7. Canonical Identity Layer

## `competition`

```text
id
name
canonical_code
country_code
competition_type      league / cup
tier
gender
active
```

---

## `season`

```text
id
competition_id
name                  2025/26
start_date
end_date
```

Unique：

```text
competition_id + name
```

---

## `team`

```text
id
canonical_name
short_name
country_code
founded_year
active
```

---

## `player`

```text
id
canonical_name
birth_date
nationality
primary_position
preferred_foot
height_cm
```

玩家资料允许 NULL。

---

## `venue`

```text
id
name
city
country_code
capacity
latitude
longitude
```

---

# 8. Provider ID Mapping

这是最重要的基础表之一。

## `provider_entity_mapping`

```text
id

provider
entity_type

provider_entity_id
provider_entity_name

canonical_entity_id

valid_from
valid_to

match_confidence

created_at
updated_at
metadata
```

例如：

```text
SRCT
TEAM
"36"
"阿森纳"
→
TEAM_ARSENAL
```

Understat：

```text
UNDERSTAT
TEAM
"Arsenal"
→
TEAM_ARSENAL
```

fdhist：

```text
FDHIST
TEAM
"Arsenal"
→
TEAM_ARSENAL
```

禁止：

```sql
JOIN ON team_name
```

---

# 9. Fixture Domain

## `fixture`

GoalX 最重要的实体。

```text
id

competition_id
season_id

home_team_id
away_team_id

kickoff_at

round
stage

venue_id

status

created_at
updated_at
```

所有时间统一：

```text
UTC
```

---

## `fixture_revision`

Fixture 可能发生：

```text
延期
改时间
改球场
取消
```

不能覆盖。

```text
id
fixture_id

field_name

old_value
new_value

effective_at
observed_at

provider
```

---

## `match_result`

```text
fixture_id

home_score_ht
away_score_ht

home_score_ft
away_score_ft

home_score_et
away_score_et

home_penalty
away_penalty

winner
result_status
```

---

# 10. Squad / Player Domain

## `team_season_player`

表示某球员属于某球队某赛季。

```text
season_id
team_id
player_id

squad_number
position

joined_at
left_at

is_captain
```

Composite PK：

```text
season_id
team_id
player_id
```

---

# 11. Lineup Domain

不要只做一个：

```text
fixture_lineup
```

需要支持未来 PIT。

## `lineup_snapshot`

```text
id

fixture_id
team_id

provider

formation

confirmed

source_updated_at
observed_at
```

例如：

```text
T-24h predicted
T-6h predicted
T-60m confirmed
```

历史 源T 最终阵容：

```text
confirmed = true
```

---

## `lineup_player`

```text
snapshot_id
player_id

role
    STARTER
    BENCH

position
formation_slot
shirt_number

captain
goalkeeper
```

---

# 12. Injury / Availability

这里必须分两个概念。

## `player_absence_period`

事后事实：

```text
id

player_id
team_id

start_date
end_date

absence_type
    injury
    suspension
    illness
    other

reason
provider
```

适合：

```text
历史球员状态研究
```

---

## `player_availability_observation`

真正 PIT：

```text
id

fixture_id
team_id
player_id

status
    available
    doubtful
    questionable
    out
    suspended

reason

confidence

source_updated_at
observed_at

provider
```

未来模型严格：

```text
WHERE observed_at <= prediction_as_of
```

历史 源T “最终知道他缺阵”不能直接用于：

```text
T-24h historical backtest
```

否则 Look-ahead Bias。

---

# 13. Match Events

## `match_event`

```text
id
fixture_id

provider_event_id

period
minute
second
sequence

team_id

player_id
related_player_id

event_type
event_subtype

home_score
away_score

metadata JSONB
```

事件包括：

```text
goal
yellow_card
red_card
substitution
penalty
VAR
shot
...
```

如果 源T 将来出现坐标：

```text
x
y
end_x
end_y
```

可以扩展。

---

# 14. Team Match Statistics

我建议采用：

## 固定核心字段 + JSONB扩展

而不是完全 EAV。

## `team_match_stats`

```text
fixture_id
team_id
provider

possession

shots
shots_on_target
shots_off_target
shots_blocked

shots_inside_box
shots_outside_box

corners

passes
passes_completed
pass_accuracy

fouls
offsides

yellow_cards
red_cards

tackles
interceptions
clearances
saves

big_chances
big_chances_missed

box_touches

duels_won
aerial_duels_won

extra_stats JSONB
```

Unique：

```text
fixture_id
team_id
provider
```

这样：

* 常用字段查询快；
* 源T 新增奇怪 statistic 不需要迁表。

---

# 15. Player Match Statistics

## `player_match_stats`

```text
fixture_id
team_id
player_id
provider

starter

minutes

goals
assists

shots
shots_on_target

passes
passes_completed

key_passes

tackles
interceptions

fouls

yellow_cards
red_cards

rating

extra_stats JSONB
```

---

# 16. xG 数据设计

这是非常重要的一层。

绝对不能：

```text
fixture.xg_home
```

因为不同 provider 的 xG 不是同一个模型。

---

## `xg_match_observation`

每支球队每场一条 provider observation。

```text
fixture_id
team_id

provider

xg
npxg

open_play_xg
set_piece_xg

xgot

nsxg

big_chances
box_touches

provider_model_version

observed_at

metadata JSONB
```

例如同一场：

```text
UNDERSTAT
xG = 1.74
npxG = 1.74

FIVETHIRTYEIGHT
xG = 1.81
nsxG = 1.22

SRCT
xG = 1.78
xGOT = 2.13
open_play_xG = 1.51
```

全部保存。

---

# 17. Understat Shot-Level

## `shot_event`

```text
id

fixture_id
provider

team_id
player_id

minute

x
y

body_part

situation

shot_result

assist_player_id

xg

raw_metadata JSONB
```

Understat 对五大联赛保留：

```text
shot-level truth
```

未来如果 源T 或其他 source 有 shot-level：

同样进入这张表。

---

# 18. xG 统一标准化

不要改变 Raw xG。

建立：

## `xg_provider_calibration`

```text
provider

reference_provider

competition_group

valid_from
valid_to

model_type

intercept
slope

mae
rmse

model_version
```

第一阶段：

```text
Reference Provider = UNDERSTAT
```

利用 overlap：

### 538 vs Understat

```text
2016～2023
Big 5
```

估算：

$$
xG_{normalized}
=
a+b\times xG_{538}
$$

源T：

```text
2024+
Big 5
```

同样校准。

后期可用 isotonic / spline，而不是固定线性。

---

## `normalized_xg`

这是 Gold Layer，不属于事实表：

```text
fixture_id
team_id

raw_provider
raw_xg

normalized_xg

normalization_model_version
```

模型消费：

```text
normalized_xg
```

但永远保留：

```text
raw_provider + raw_xg
```

---

# 19. fdhist

不要把它融合进 源T 表。

它通过正常 canonical pipeline 进入系统。

核心作用：

```text
Result benchmark
basic stats benchmark
early odds
closing odds
```

尤其注意：

```text
fdhist early odds
≠ guaranteed true opening odds
```

因此字段定义：

```text
EARLY
CLOSE
```

不要叫：

```text
OPEN
CLOSE
```

---

# 20. Bookmaker Identity

## `bookmaker`

```text
id
canonical_name

type
    SPORTSBOOK
    EXCHANGE
    LOTTERY
    PREDICTION_MARKET

role
    SHARP
    ASIAN
    RETAIL
    TARGET
    OTHER

region
active
```

虽然 V1 暂不接 Betfair Historical，但 schema 一开始支持 Exchange。

---

# 21. Odds Market Model

## `market_definition`

```text
id

code

period

market_type
```

核心：

```text
FT_1X2

FT_AH
FT_OU

1H_1X2
1H_AH
1H_OU
```

后面可以继续：

```text
BTTS
CORRECT_SCORE
CORNERS
CARDS
```

V1 模型主要消费前三类。

---

# 22. Odds Quote

这是整个市场数据库最重要的一张表。

## `odds_quote`

```text
id

fixture_id

provider
bookmaker_id

market_id

line_value

selection

price_decimal

source_updated_at
observed_at

phase
    EARLY
    PREMATCH
    LIVE
    CLOSE

is_suspended

raw_resource_id
```

例如：

```text
fixture = ARS-CHE

bookmaker = Bet365
market = FT_AH

line_value = -0.5

selection = HOME
price = 1.93

observed_at = 2026-09-23T12:30Z
```

---

# 23. Odds 必须 Append Only

禁止：

```sql
UPDATE odds_quote
SET price = ...
```

正确：

```text
12:00  2.01
12:14  1.99
12:43  1.96
```

只在：

```text
price / line / state
```

发生变化时插入。

---

# 24. 源T Bookmaker 数据建议

如果页面本身返回所有 book 数据：

## RAW

尽可能完整保存原始内容。

这样未来：

```text
不用重新抓历史网页
```

---

## Canonical V1

先标准化少数核心 book，例如：

```text
Pinnacle

Bet365

William Hill / Unibet

Crown

188 / 12Bet 类亚洲源

其他一个亚洲源
```

大概：

```text
6～10 books
```

足够训练第一代 Market Model。

以后需要：

```text
bookmaker dispersion
regional bias
soft book behaviour
```

再把 RAW 中剩余公司 canonicalize。

---

# 25. Sporttery Domain

竞彩不要塞进通用 bookmaker odds。

因为它是 GoalX 的 Target Market。

---

## `sporttery_match`

```text
id

fixture_id

sporttery_match_id
match_num

sale_start_at
sale_end_at

status
```

---

## `sporttery_quote`

```text
id

sporttery_match_id

pool_code

line_value

selection

price

official_updated_at
observed_at
```

pool：

```text
HAD
HHAD
TTG
HAFU
CRS
```

---

例如：

```text
HHAD

line = -1

HOME  = 3.20
DRAW  = 3.55
AWAY  = 1.82
```

---

## `sporttery_support_rate`

如果官方能稳定获取：

```text
sporttery_match_id

pool_code

home_rate
draw_rate
away_rate

official_updated_at
observed_at
```

后续可以研究：

```text
public bias
```

但不是 V1 核心 feature。

---

# 26. 数据质量

## `data_quality_issue`

```text
id

fixture_id nullable

provider
dataset

issue_type

severity

detected_at

expected_value
actual_value

status
```

issue：

```text
MISSING_FIXTURE
DUPLICATE_FIXTURE

TEAM_MAPPING_FAILED

KICKOFF_CONFLICT
RESULT_CONFLICT

ODDS_OUTLIER
STALE_ODDS
MISSING_BOOK

XG_CONFLICT

LINEUP_INCOMPLETE
```

---

# 27. Provider Coverage

建立：

## `provider_coverage`

```text
provider

competition_id
season_id

dataset

expected_records
actual_records

coverage_pct

last_checked_at
```

dataset：

```text
fixture
lineup
injury
stats
xg
1x2
ah
ou
odds_movement
```

这就是以后决定：

```text
要不要买 API-Football
要不要买 FootyStats
```

的依据。

---

# 28. Point-in-Time Feature Layer

这是量化系统最关键的一层。

固定几个 prediction horizon：

```text
T-24h
T-6h
T-1h
T-15m
```

以后可增加：

```text
T-72h
T-3h
```

---

## `feature_snapshot`

```text
id

fixture_id

as_of

feature_set_version

football_features JSONB
lineup_features JSONB
market_features JSONB
sporttery_features JSONB

created_at
```

生产环境推荐：

```text
metadata in PostgreSQL
actual feature vectors in Parquet
```

---

# 29. 派生球队状态

## `team_state_snapshot`

```text
team_id

as_of

attack_rating
defense_rating

xg_attack_rating
xg_defense_rating

home_advantage_adjustment

form_strength

model_version
```

---

# 30. Player Strength

## `player_state_snapshot`

```text
player_id

as_of

attack_impact
defense_impact

xg_impact
xa_impact

minutes_weight

uncertainty

model_version
```

这部分不是第一天必须完善，可以后续增长。

---

# 31. GoalX 模型体系

不要直接训练一个：

```text
XGBoost
→ 胜平负
```

推荐拆成四个独立模型。

---

# Model A — Fundamental Score Model

当前 DC 可以继续保留。

升级方向：

# Dynamic Dixon-Coles / Bayesian Poisson

输出：

```text
lambda_home
lambda_away
```

然后产生：

$$
P(H)
$$

$$
P(D)
$$

$$
P(A)
$$

以及：

```text
score matrix
OU probability
AH probability
```

输入：

```text
team attack
team defense
home advantage

time decay

xG attack
xG defense

rest / schedule
```

---

# Model B — xG Strength Model

独立维护：

```text
EWMA xG For
EWMA xGA

EWMA npxG

open_play_xG
set_piece_xG
```

例如：

$$
AttackStrength_{t}
=
\alpha xG_t
+
(1-\alpha)AttackStrength_{t-1}
$$

不要只做：

```text
last5_xg_avg
```

推荐：

```text
exponential time decay
```

---

# Model C — Market Model

源T odds 产生：

```text
P_market
```

首先每一家 sportsbook 去 vig。

对于 1X2：

$$
q_i = 1/O_i
$$

$$
P_i=\frac{q_i}{\sum q}
$$

每个 bookmaker 得：

```text
P_home
P_draw
P_away
```

然后 V1 可以构建：

```text
Pinnacle probability

Asian-book consensus

Retail consensus
```

而不是简单：

```text
average(all books)
```

---

# 32. AH + OU 也要进入 Market Model

后期非常重要。

AH：

```text
market expected goal difference
```

OU：

```text
market expected total goals
```

可以共同反解：

```text
market_lambda_home
market_lambda_away
```

于是我们得到：

```text
Fundamental Lambda
vs
Market Lambda
```

这通常比只比较 1X2 信息丰富。

V1 schema 现在就支持，feature 可以第二阶段做。

---

# Model D — Lineup Adjustment

这个模型需要特别防 leakage。

历史 actual lineup 可以用于：

```text
估计 player impact
```

但不能用于：

```text
T-24h historical prediction
```

因为 T-24h 当时不知道实际首发。

因此：

### T-24h / T-6h

只有真实 PIT availability 才能进入。

历史没有就：

```text
不使用 lineup information
```

### T-60m / T-15m

confirmed lineup：

```text
可以使用
```

这意味着未来可以维护两个模型：

```text
PRE_LINEUP_MODEL

POST_LINEUP_MODEL
```

非常重要。

---

# 33. Probability Blender

最终不要直接选择：

```text
Football Model
or
Market Model
```

而是：

```text
Fundamental
    │
    ├── score / xG
    │
    ▼
 P_fundamental
        │
        │
Market ─┼────→ P_market
        │
Lineup ─┼────→ adjustment
        │
        ▼
       Meta Model
        │
        ▼
      P_true
```

第一版可以：

### Multinomial Logistic Regression

输入：

```text
logit(P_fundamental)

logit(P_market)

xg_strength_diff

home_advantage

lineup_strength_diff
```

而不是一开始搞大模型。

原因：

```text
可解释
不容易 overfit
容易 calibration
```

---

# 34. 不同时间点使用不同模型

强烈建议：

```text
M24
T-24h

M6
T-6h

M1
T-1h

M15
T-15m
```

因为这些时间点可获得的信息不同。

而不是训练：

```text
一个模型
```

然后动态缺 feature。

例如：

### M24

```text
team strength
xG
early market
schedule
```

### M1

额外：

```text
market movement
injury information
```

### M15

额外：

```text
confirmed lineup
late market movement
```

---

# 35. Sporttery Edge Engine

最终：

$$
EV =
P_{true}\times SP - 1
$$

例如：

```text
GoalX P(Home)
= 0.48

Sporttery SP
= 2.25
```

那么：

$$
EV
=
0.48\times2.25-1
=
8\%
$$

---

建立：

## `market_edge`

```text
fixture_id

prediction_id

sporttery_quote_id

selection

true_probability
sporttery_price

implied_probability

edge
expected_value

created_at
```

---

# 36. Prediction 存档

## `model_prediction`

```text
id

fixture_id

as_of

model_id
model_version

lambda_home
lambda_away

p_home
p_draw
p_away

fair_home_odds
fair_draw_odds
fair_away_odds

feature_snapshot_id

created_at
```

每一次 prediction 永久保存。

---

# 37. Model Registry

## `model_registry`

```text
id

name
version

model_type

training_start
training_end

feature_set_version

artifact_path

git_commit

metrics JSONB

created_at
```

以后必须能回答：

> 2026-09-23 这场比赛为什么预测 47.3%？

---

# 38. Backtest

## `backtest_run`

```text
id

model_id

start_date
end_date

decision_horizon

competitions

config JSONB

log_loss
brier_score

roi
yield
max_drawdown

created_at
```

---

# 39. 模型验证指标

预测质量：

```text
Log Loss
Brier Score
Calibration
```

概率模型不要主要看：

```text
Accuracy
```

---

Market：

```text
CLV
```

竞彩：

```text
ROI
Yield
Max Drawdown
Turnover
```

---

# 40. 训练切分

禁止：

```text
random train_test_split
```

必须：

# Walk-forward

例如：

```text
2016─2021   train
2022        validation
2023        test

↓

2016─2022   train
2023        validation
2024        test
```

严格按时间。

---

# 41. 数据依赖最终关系

```text
                    SRCT
                      │
      ┌───────────────┼──────────────────┐
      │               │                  │
 Football Truth     Market Truth      Recent xG
      │               │                  │
      └───────────────┼──────────────────┘
                      │
                GoalX Canonical
                      ▲
                      │
        ┌─────────────┼───────────────┐
        │             │               │
    Understat        538        fdhist
        │             │               │
    Historical     Historical     Results/Odds
       xG             xG           Validation
        │             │               │
        └─────────────┼───────────────┘
                      │
                 XG Normalizer
                      │
                      ▼
               Football Features
                      │
                      ▼
              Fundamental Model
                      │
                      │
源T Odds ────── Market Model
                      │
                      ▼
               Probability Blender
                      │
                      ▼
                   P_true
                      │
                      │
           SPORTTERY OFFICIAL
                      │
                      ▼
               Sporttery Edge
```

---

# 42. GoalX V1 的真正核心表

如果现在只允许优先实现，我会先做这 **24 张**：

### Infrastructure

```text
raw_resource
ingestion_run
data_quality_issue
provider_coverage
```

### Identity

```text
competition
season
team
player
venue
provider_entity_mapping
```

### Football

```text
fixture
fixture_revision
match_result
match_event
team_season_player
team_match_stats
player_match_stats
```

### Availability

```text
lineup_snapshot
lineup_player
player_absence_period
player_availability_observation
```

### xG

```text
xg_match_observation
shot_event
xg_provider_calibration
```

### Market

```text
bookmaker
market_definition
odds_quote
```

### Sporttery

```text
sporttery_match
sporttery_quote
```

实际上核心 relational schema 就这些。

---

# 43. 后续 Derived Tables

等模型阶段增加：

```text
normalized_xg

team_state_snapshot
player_state_snapshot

feature_snapshot

model_registry
model_prediction

market_edge

backtest_run
```

这些属于 Gold Layer。

---

# 44. 源T 采集顺序

不要一开始对所有页面全量并发。

我会按：

```text
competition
    ↓
season
    ↓
fixture index
    ↓
fixture
```

先建立：

# `fixture universe`

确认：

```text
比赛总数
match id
season
home
away
kickoff
```

全部正确。

然后第二轮：

```text
lineup
stats
events
xG
```

第三轮：

```text
odds
```

这样任何时候失败：

```text
fixture universe
```

仍然是稳定的。

---

# 45. 10 年 源T 数据回填优先级

我会按：

### P0

```text
Fixture
Result
Team
Player
Lineup
Stats
```

### P0

```text
1X2
AH
OU
```

至少：

```text
initial
closing
```

如果完整 timeline 容易获取：

```text
全部保存
```

### P1

```text
events
injury
player stats
```

### P1

```text
xG
```

有就抓，没有 NULL。

---

# 46. 对历史 Odds 一个建议

完整赔率 change 很有价值，但不要为了：

```text
200 bookmakers
```

拖死历史回填。

正确策略：

### Phase A

全赛事 × 10 年：

```text
fixture
result
stats
xG

+ 6~10 core books
1X2/AH/OU timeline
```

### Phase B

后面有研究 hypothesis：

```text
regional bookmaker dispersion
```

再补：

```text
20 / 50 / 200 books
```

---

# 47. V1 不要追求“100%完整”

最危险的思想是：

```text
某年缺 xG
→ 必须找一个 provider 补成100%
```

真实世界的数据本来就是：

```text
missing not at random
```

正确方式是：

```text
xG_provider
xG_available

lineup_available
odds_available
```

模型知道信息是否存在。

---

# 48. 最终 GoalX 应该拥有三种 Truth

## Football Truth

```text
源T
+
Understat / 538 validation
```

回答：

> 球队真正有多强？

---

## Market Truth

```text
源T bookmaker market
+
fdhist validation
```

回答：

> 国际市场如何定价？

---

## Target Market Truth

```text
Sporttery official
```

回答：

> 我真正能买到什么价格？

---

# 49. V1 成功标准

数据层先达到：

```text
15 competitions

≈ 10 seasons

fixture coverage > 99%

result coverage > 99%

lineup coverage 可量化

stats coverage 可量化

xG coverage 可量化

核心 odds coverage > 95%

Sporttery official PIT
100% archived
```

之后再谈：

```text
复杂 ML
Agent
NLP
news
weather
referee
```

---

# 50. 最终决策

当前 GoalX 不再是：

```text
找到一个万能 API
```

而是：

```text
源T
= 主事实库 + 主赔率库

Sporttery
= 目标市场

Understat
= 五大历史 xG 标尺

FiveThirtyEight
= 非五大历史 xG 补丁

fdhist
= 长期结果/赔率独立 benchmark
```

其它数据源：

```text
API-Football
propline
oddsapi
FootyStats
Sportmonks
```

全部先变成：

# `optional adapters`

只有 Coverage Audit 明确证明存在不可接受的洞时才启用。

这样 GoalX V1 的长期第三方订阅成本基本可以接近 **0**，而数据结构已经支持以后无痛增加任何商业源。

最重要的是：**历史 RAW、Provider ID Mapping、赔率 append-only、xG provider-aware、PIT availability** 这五件事情一开始就必须做对。模型以后可以全部推倒重训，数据资产不用重建。

如果按这版继续实施，下一步最合适的是把这 24 张核心表直接落成 **PostgreSQL DDL + 索引/分区策略 + 源T/Sporttery/Understat/538/fdhist Adapter 接口定义**，这样基本就可以直接开始编码了。
