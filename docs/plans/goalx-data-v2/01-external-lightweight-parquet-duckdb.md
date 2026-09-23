# 存档：Parquet+DuckDB+SQLite 轻量架构方案（外部推荐 A）

> 来源：2026-09-23 外部架构咨询原文，代称化转正（源T=srct、fdhist）。
> 效力：工程手法（ZSTD/BIGINT ID/change-events/DuckDB 直读 Parquet）作实施参考；其
> "不留 HTML"的 raw 政策对源T 已被推翻（ADR-0011 决策 2：源T raw 全留——回填是一次性
> 资产，事后无法重取），对后续其他源的 HTML 处置仍可参考。裁决记录：
> `.scratch/goalx-data-v2/map.md`；终审：`docs/adr/0011-corpusstore-data-v2.md`。

---

按现在已经收敛的方案，我建议把它正式定成 **GoalX Data Platform V1**。下面这版可以直接作为 repo 里的数据架构 spec：把源T、竞彩、Understat、538、fdhist 的职责彻底分开，同时明确哪些东西暂时不再依赖。

# 先给你存储量结论

按现在规划：

* 约 **15 个赛事体系**
* **近 10 年**
* 粗略 **4.5～6 万场比赛**
* 源T：基础数据、阵容、伤停、技术统计、xG、赔率
* 核心赔率只保存 **6～10 家 bookmaker**
* 1X2 + AH + O/U 完整变动
* Understat + 538 + fdhist + 竞彩

我预计：

### **结构化数据本体：大约 10～30 GB**

比较保守地留余量：

### **50 GB 已经非常舒服。**

如果未来把赔率扩大到几十/上百家 bookmaker，才可能进入：

```text
100 GB+
```

量级。

---

# 一、真正占空间的是赔率，不是比赛数据

大概可以这样估。

| 数据                               |       10 年预估 |
| -------------------------------- | -----------: |
| Fixture / result / league / team |     < 200 MB |
| 球员 / squad                       |   200～500 MB |
| 历史 lineup / bench                |   300 MB～1 GB |
| Injury / suspension              |     < 500 MB |
| Match events                     |   500 MB～2 GB |
| Team match stats                 |     < 500 MB |
| Player match stats               |   500 MB～2 GB |
| Understat shots / xG             |    < 1～2 GB |
| 538                              |        几十 MB |
| fdhist                    |        几十～几百 MB |
| 竞彩 SP history                    |   500 MB～2 GB |
| **源T odds timeline**          | **5～20+ GB** |

所以绝大部分业务数据其实非常小。

---

# 二、为什么赔率会突然变大

假设：

```text
50,000 matches

8 bookmakers
3 markets

1X2
AH
OU
```

每场每家平均有几十次价格变化。

最后很容易达到：

```text
几千万 ～ 1亿条 odds observations
```

不过用 Parquet + ZSTD 后其实仍然不大，因为这些字段重复度极高：

```text
fixture_id
bookmaker_id
market_id
selection
timestamp
price
line
```

特别适合列式压缩。

所以几十 GB 就能装下非常可观的赔率历史。

---

# 三、但是如果你把源T 200 家 bookmaker 全抓下来

情况就完全不同了。

比如从：

```text
8 books
```

扩展到：

```text
200 books
```

差不多是：

### **25 倍。**

原本：

```text
10 GB odds
```

可能变成：

```text
200～300 GB
```

甚至更高。

所以之前我建议：

## RAW 页面可以全取到什么先不重要，Canonical 第一阶段只维护 6～10 家核心 book。

例如：

```text
Pinnacle

Bet365

Crown

188 / 12Bet

William Hill / Unibet

另外 1~2 个亚洲盘源
```

已经足够做第一代 Market Model。

---

# 四、不保存 HTML 完全可以

而且我建议：

# **不要永久保存所有 HTML。**

原因主要不是空间，而是实用价值。

假设源T 每场有：

```text
比赛页面
阵容页面
技术统计页面
欧赔页面
亚盘页面
大小球页面
```

如果赔率还按：

```text
bookmaker × market
```

分别请求 HTML，

那么：

```text
50,000 场
× 8 bookmakers
× 3 markets
```

已经是：

### 120 万个页面。

即使 gzip 后平均只有：

```text
30KB
```

也是几十 GB。

如果页面平均 100～200KB：

很容易上：

### 100GB+

而这些 HTML 九成九以后都不会再读。

---

# 五、我更建议保存“原始结构化抽取结果”

也就是不要：

```text
HTML
↓
Parse
↓
扔掉所有上下文
```

而是：

```text
HTML
     ↓
Parser
     ↓
Provider Raw Record
     ↓
Canonical
```

中间保存一层：

# `provider_raw`

但它应该是：

```json
{
  "provider": "srct",
  "match_id": "2607056",
  "dataset": "match_stats",
  "fetched_at": "...",
  "parser_version": "1.3.0",
  "source_url": "...",
  "payload": {
    ...
  }
}
```

也就是你真正需要的数据 JSON。

---

# 六、例如源T 技术统计

网页有：

```text
射门
射正
xG
npxG
xGOT
控球
...
```

不要保存：

```text
300 KB HTML
```

保存：

```json
{
  "match_id": 2607056,
  "home": {
    "shots": 24,
    "shots_on_target": 11,
    "xg": 2.14,
    "npxg": 2.14,
    "xgot": 2.31
  },
  "away": {
    ...
  }
}
```

可能只有：

```text
1～3 KB
```

压缩后更小。

差距是几个数量级。

---

# 七、赔率尤其适合彻底不保存 HTML

例如页面原始内容最终解析出：

```text
match_id
bookmaker
market

2024-01-01 12:03
HOME
2.04

2024-01-01 13:26
HOME
2.01
```

真正有价值的就是：

```text
timestamp
line
price
```

HTML wrapper 没什么研究价值。

直接保存：

```text
odds_quote.parquet
```

即可。

---

# 八、但我不建议“什么 Raw 都不留”

这是两个极端。

### 不建议：

```text
所有 HTML 永久保存
```

但也不建议：

```text
抓到 HTML
↓
只留最终 canonical DB
↓
什么原始信息都没有
```

因为以后 parser 很可能有 bug。

例如今天我们解析：

```text
平/半
```

错误映射成：

```text
-0.5
```

实际上应该：

```text
-0.25
```

半年以后发现了。

如果什么原始数据都没留：

> 只能重新爬网站。

---

# 九、因此我推荐三层

```text
源T HTML
    │
    │ temporary
    ▼
┌─────────────────────────┐
│ Bronze Structured Raw   │
│ JSON / NDJSON / Parquet │
└────────────┬────────────┘
             │
             ▼
┌─────────────────────────┐
│ Silver Canonical Parquet│
└────────────┬────────────┘
             │
             ▼
          Gold
        Features
```

HTML：

```text
fetch
↓
parse
↓
validate
↓
discard
```

---

# 十、但是保留少量 HTML 样本

我会留：

### ① Parser 失败的

```text
parse_failed/
```

100% 留。

### ② Schema change

例如突然发现：

```text
unknown field
missing selector
new layout
```

留。

### ③ Random sample

每种页面：

```text
0.5% ～ 1%
```

随机保存。

例如：

```text
100,000 页面
```

只留：

```text
500～1000 个
```

以后写 parser regression test 足够。

### ④ 特别重要的数据

如果某一种历史赔率页面：

> 未来很难再恢复，

可以选择性完整留。

---

# 十一、我会这样设计磁盘目录

例如：

```text
goalx-data/
│
├── raw/
│   ├── srct/
│   │   ├── parsed/
│   │   ├── failed/
│   │   └── samples/
│   │
│   ├── sporttery/
│   ├── understat/
│   ├── 538/
│   └── fdhist/
│
├── bronze/
│
├── silver/
│
├── gold/
│
└── duckdb/
    └── goalx.duckdb
```

---

# 十二、Bronze 层最好直接用 Parquet

例如：

```text
bronze/
  srct/
    fixtures/
      competition=EPL/
        season=2024-2025/
          part-000.parquet
```

赔率：

```text
bronze/
  srct/
    odds/
      competition=EPL/
        season=2024-2025/
          market=1x2/
          market=ah/
          market=ou/
```

这样非常方便。

---

# 十三、Silver 也继续 Parquet

Canonical：

```text
silver/
  fixture/
  team/
  player/
  lineup/
  match_event/
  team_match_stats/
  player_match_stats/
  xg_observation/
  odds_quote/
  sporttery_quote/
```

不需要数据库服务。

---

# 十四、DuckDB 只作为 Query Engine

你甚至不需要把所有数据“导入 DuckDB”。

DuckDB 可以直接：

```sql
SELECT *
FROM read_parquet(
  'silver/odds_quote/**/*.parquet'
)
WHERE competition = 'EPL'
AND season >= '2022-2023';
```

因此：

```text
Parquet = 数据本体

DuckDB = 查询/研究层
```

我很喜欢这种结构。

---

# 十五、metadata 怎么办？

如果不想 PostgreSQL：

有三个选择。

## 最简单

### DuckDB 本身。

例如维护：

```text
provider_entity_mapping
ingestion_run
coverage
```

这些小表。

完全没问题。

---

## 或者 SQLite

如果 collector 多进程状态比较多：

```text
SQLite
```

也非常合适。

例如：

```text
crawl_queue
crawl_checkpoint
failed_url
entity_mapping
```

体积基本可以忽略。

---

## 我当前最推荐

```text
SQLite
→ 爬虫状态 / checkpoint / queue

Parquet
→ 所有数据资产

DuckDB
→ research / analytics
```

非常适合个人量化系统。

---

# 十六、这样整个 GoalX Data V1 可以没有数据库服务

变成：

```text
                   Collectors

 源T       Sporttery     Understat
   │           │             │
   ▼           ▼             ▼
        Parsed structured data
                  │
                  ▼
               Parquet
                  │
           ┌──────┴───────┐
           │              │
         DuckDB         Python
           │              │
           └──────┬───────┘
                  │
               Models
```

同时：

```text
SQLite
```

只负责：

```text
crawl state
mapping
metadata
```

非常简单。

---

# 十七、Parquet 压缩推荐

直接：

# ZSTD

即可。

典型：

```python
compression="zstd"
```

对于赔率这种：

```text
大量重复 bookmaker_id
market_id
fixture_id
```

压缩效果很好。

---

# 十八、ID 不要用 UUID 到处乱飞

如果主要为了分析效率，我反而建议内部：

```text
BIGINT
```

例如：

```text
fixture_id  int64
team_id     int32/int64
player_id   int64
```

而不是：

```text
UUID
```

因为 Parquet、DuckDB、Polars：

```text
int
```

压缩和 join 都更舒服。

可以自己产生：

```text
goalx_fixture_id
```

Snowflake-style 或 deterministic hash。

---

# 十九、字符串重复字段也尽量编码

不要赔率表每一行：

```text
"Pinnacle"
"FULL_TIME_ASIAN_HANDICAP"
"HOME"
```

最好：

```text
bookmaker_id = 12

market_id = 2

selection = 0
```

然后 dimension：

```text
bookmaker
market_definition
```

单独存。

这样赔率表空间非常小。

---

# 二十、赔率还有一个巨大的省空间技巧

只存：

# **Change Events**

不要：

```text
每分钟 snapshot
```

如果：

```text
12:00 2.05
12:01 2.05
12:02 2.05
12:03 2.05
...
```

存一条：

```text
12:00 2.05
```

直到：

```text
12:27 2.02
```

再新增。

这本质就是：

```text
event sourcing
```

---

# 二十一、如果某模型需要 T-6h

运行时：

```sql
SELECT LAST(price)
WHERE observed_at <= kickoff - INTERVAL '6h'
```

即可。

不需要提前每分钟重复存。

这可能减少：

# 80～99%

赔率数据。

---

# 二十二、我估计最终空间大概会是这样

按 V1：

### 50k 左右比赛

```text
Core football:
2~5 GB
```

### Understat + 538 + fdhist

```text
< 2 GB
```

### Sporttery

```text
< 1~2 GB
```

### 源T 6～10 books full odds history

```text
5～20 GB
```

### metadata / indices / features

```text
2～5 GB
```

所以正常：

# **10～30 GB**

比较激进：

# **30～50 GB**

我会直接预留：

## **100 GB SSD**

根本不用操心。

---

# 二十三、如果以后加训练 dataset / features

真正可能增长的是：

```text
feature_snapshot
```

例如你给：

```text
50,000 fixtures
× 4 horizons
× 500 features
```

如果傻存成 JSON：

可能越来越大。

正确方式是：

```text
Parquet float32
```

仍然只是：

```text
几百 MB ～ 数 GB
```

不是什么问题。

---

# 二十四、甚至模型文件本身更小

```text
LightGBM
XGBoost
sklearn
PyTorch
```

模型文件往往：

```text
MB ～ 几百 MB
```

完全不用考虑。

---

# 二十五、原始 HTML 我会采用这个 retention policy

我觉得最适合你的：

| HTML 类型                  |       保存 |
| ------------------------ | -------: |
| 成功且成功解析                  |   **删除** |
| Parser failure           | **永久保存** |
| 页面 schema changed        | **永久保存** |
| Random regression sample |   **1%** |
| 特别稀缺历史页                  |      视情况 |
| 已转成完整赔率序列                |      删除 |

也就是说：

# **HTML 是调试资产，不是数据资产。**

真正的数据资产是：

```text
Parsed Raw
+
Canonical
```

---

# 二十六、还有一个特别重要的字段

虽然不存 HTML，但每个 parsed record 至少保存：

```text
source_url

fetched_at

parser_version

source_hash
```

例如：

```json
{
  "source_url": "...",
  "fetched_at": "2026-09-23T...",
  "parser_version": "srct-stats-v3",
  "source_hash": "sha256..."
}
```

`source_hash` 可以在解析前：

```text
hash(html)
```

算出来。

HTML 删除没关系。

以后至少能知道：

> “这批记录来源于哪个原始 response。”

---

# 二十七、再加一个 `raw_snapshot`

不是保存 HTML，而是保存 parser 从网页读取的**未规范化字段**。

例如源T：

```json
{
  "比赛": "...",
  "预期进球": "1.74",
  "运动战预期进球": "1.41",
  "射正": "6",
  "控球率": "58%"
}
```

然后 Silver：

```json
{
  "xg": 1.74,
  "open_play_xg": 1.41,
  "shots_on_target": 6,
  "possession": 0.58
}
```

这样 parser normalization 出错时还有一层可以重跑。

这比存整个 HTML 有价值得多。

---

# 二十八、所以架构我会从上一版调整成

不再：

```text
Bronze = HTML
```

而改成：

```text
            transient HTML
                  │
                  ▼
          ┌──────────────┐
          │ Bronze       │
          │ Parsed Raw   │
          │ JSON/Parquet │
          └──────┬───────┘
                 │
                 ▼
          ┌──────────────┐
          │ Silver       │
          │ Canonical    │
          │ Parquet      │
          └──────┬───────┘
                 │
                 ▼
          ┌──────────────┐
          │ Gold         │
          │ Features     │
          │ Parquet      │
          └──────────────┘
```

HTML 只短暂存在：

```text
/tmp
```

失败才转入：

```text
raw_failed/
```

---

# 二十九、对你这个项目我现在最推荐的具体技术栈

```text
Python collectors
        │
        ▼
BeautifulSoup / lxml / httpx
        │
        ▼
Pydantic schemas
        │
        ▼
PyArrow / Polars
        │
        ▼
Parquet + ZSTD
        │
        ▼
DuckDB
        │
        ▼
Polars / pandas / sklearn
```

状态：

```text
SQLite
```

完全够。

---

## 最终结论

### **不存完整 HTML，完全可行，而且我认为更适合 GoalX。**

但不要变成：

> HTML → Canonical → HTML删除

而应该：

> **HTML → Parsed Raw/Bronze → Canonical/Silver → 删除 HTML。**

这样你既节省空间，又保留重新解析/重新映射的能力。

按现在大约 **15 个赛事 × 10 年 × 6～10 家核心 bookmaker**，我会把 GoalX V1 的实际数据量预期定在：

> **约 10～30 GB，极端保守预留 50 GB；给它一块 100 GB SSD 就足够富裕。**

只要不把源T **200 家博彩公司 × 所有滚球盘口 × 每一分钟 snapshot** 全部无脑保存，短期完全不需要专门的数据基础设施。**Parquet + DuckDB + SQLite 已经足够支撑几千万甚至上亿条赔率 observation 的研究阶段。**
