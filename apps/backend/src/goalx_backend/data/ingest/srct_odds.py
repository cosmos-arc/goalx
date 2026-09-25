"""
源T silver 切片 15：odds_change_event + bookmaker 字典（票 55/56 线）。

设计定案 `.scratch/goalx-data-v2/design-15-odds-change-events.md`
（2026-09-24 两轮钉案）落地要点：

- **字典=数据派生全量源身份+覆盖画像**（无 tier/is_core——选书归模型/
  策略层，不固化进数据层 schema；ADR-0011 决策 4 修订）。真实书商名只
  存语料数据文件，repo 代码/测试一律合成 cid+假名（代称红线）。
- **命名双 cid 空间防线**：1x2 game 联合空间与亚盘 changeDetail 端点空间
  不同构（design-15 §零实证）——bookmaker_id 带端点族前缀
  `srct:1x2:{cid}` / `srct:ah:{8}`（亚盘锚定 companyid，页面无名）。
- **变化事件流（非快照）**：按 (sid, bookmaker_id, market) 轨迹内
  (published_at, source_order) 升序，值组与上一保留行全等的行丢弃——
  A→B→A 回摆保留，首条完整报价自然保留，末条同值心跳丢弃（其时间不
  承诺保留）。值组：1x2=三价+三凯利；ah=line_raw+双水+status（minute/
  score 为上下文列不进值组）。
- **source_order**=源页轨迹原始行序，只作相同 published_at 的确定性并列
  规则（不声称该分钟内真实先后）；同分钟异值计 same_minute_conflicts。
- **时间口径**：published_at=源页分钟级标注按北京钟面固定 UTC+8 归一
  （不等同书商实际发布时间）；observed_at=bronze fetched_at。亚盘行无
  年份——候选年 ∈ {锚年-1, 锚年, 锚年+1} 取与 fixture 开球最近者
  （孤儿场无锚不推年，整场记坏时间行留痕）。历史回填仅供回顾性研究，
  不充作 PIT。
- **线值归一**：中文盘口→float，主队视角（主让为正、受让为负）；水位
  贴源不换算；未识别串计 bad_line_values 不丢行，line_raw 永久保留。
- **门④记账**（转换完整性，不判书商价值）：所选最新 bronze 轨迹行 100%
  入账为事件/心跳/坏时间行/未映射行，unexplained_gap 必须为 0。
- **写入**：分区流式（ParquetWriter 逐行组追加）——sid 按 (kickoff, sid)
  序流出、场内按 bookmaker_id/market/published/source_order 序到达，
  到达序即文件终序；禁止整表一次性物化（Phase1 ~80M 行量级）。输入侧
  同样内存有界（票 19 外排）：选轨遍只记信封行号 → sid 按开球全局排序
  连续切块（块序即输出全局序，排序契约不破）→ 载荷遍按块 spill 树内
  暂存 NDJSON → 逐块加载转换逐块释放——内存峰值=单块，分块参数对输出
  完全透明（design §八挂账已还）。
"""

# pyright: reportMissingTypeStubs=false, reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownParameterType=false
# （pyarrow 无官方 stub，同 srct_silver/dc_model 先例）

from __future__ import annotations

import json
import re
import shutil
import statistics
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from itertools import pairwise
from pathlib import Path
from typing import Protocol, TextIO, cast

import pyarrow as pa
import pyarrow.parquet as pq

from goalx_backend.data.corpus_store import CorpusStore
from goalx_backend.data.ingest import srct, srct_silver
from goalx_backend.db import utc_now_iso

BOOKMAKER_DATASET = "bookmaker"
ODDS_DATASET = "odds_change_event"
BOOKMAKER_SILVER_VERSION = "silver_bookmaker_v1"
ODDS_SILVER_VERSION = "silver_odds_v1"
SPACE_1X2 = "1x2"
SPACE_AH = "ah"
# 亚盘 changeDetail 锚定书商（URL 模板 companyid 固定值；页面无名——身份
# 按端点+cid 登记，人工核名挂账 design-15 §八）
AH_ANCHOR_CID = "8"
AH_ANCHOR_ID = f"srct:{SPACE_AH}:{AH_ANCHOR_CID}"
# 流式写行组阈值（测试经 build_odds_change_events(chunk_rows=…) 注入）
STREAM_CHUNK_ROWS = 200_000
# 分块默认：内存峰值=单块，与语料总量无关（2026-09-25 真树实测 4.7K sid
# 语料峰值 ~2.5GB vs 不分块 ~4.5GB；测试经 chunk_sids=… 注入，分块对输出
# 完全透明——Phase1 15.5K sid 重建峰值仍由本值决定）
STREAM_CHUNK_SIDS = 500
_UNSORTABLE_MS = 1 << 62  # 不可解时间的排序垫底值（随后按 bad_time 跳行）

_BEIJING = timezone(timedelta(hours=8))  # 中国无夏令时，固定偏移归一
_DETAIL_FIELDS = 8  # H|D|A|MM-DD HH:MM|凯利×3|YYYY（实测 516,027 行零异形）
_GAME_NAME_EN_IDX = 2
_GAME_NAME_ZH_IDX = 21  # 27 字段布局的中文遮罩短名（可缺席）
_GAME_INITIAL_END = 6  # game 行初盘 HDA 占列 3-5
_TIME_RE = re.compile(r"(\d{1,2})-(\d{1,2}) (\d{2}):(\d{2})")

# 中文盘口 → float（主队视角：主让为正、受让为负）。基底词与其相邻
# 组合（X/Y=中点）程序化生成，杜绝手写映射表笔误；未识别串不丢行只计数。
_LINE_BASE: dict[str, float] = {
    "平手": 0.0,
    "半球": 0.5,
    "一球": 1.0,
    "球半": 1.5,
    "两球": 2.0,
    "两球半": 2.5,
    "三球": 3.0,
    "三球半": 3.5,
    "四球": 4.0,
    "四球半": 4.5,
    "五球": 5.0,
}


def _build_line_map() -> dict[str, float]:
    lines = dict(_LINE_BASE)
    for name, value in _LINE_BASE.items():
        lines[f"受让{name}"] = -value
    for shallower, deeper in pairwise(_LINE_BASE):
        mid = (_LINE_BASE[shallower] + _LINE_BASE[deeper]) / 2
        lines[f"{shallower}/{deeper}"] = mid
        lines[f"受让{shallower}/{deeper}"] = -mid
    return lines


LINE_MAP: dict[str, float] = _build_line_map()


def normalize_line(line_raw: str | None) -> float | None:
    """归一线值（主队视角，受让为负）；未识别 None。"""
    if line_raw is None:
        return None
    return LINE_MAP.get(line_raw.strip())


class _TimedRow(Protocol):
    """去重核的行协议：可排序时间 + 源页行序。"""

    published_ms: int | None
    source_order: int


@dataclass
class _DetailRec:
    """1x2 detail 行解析记录（H|D|A|MM-DD HH:MM|凯利×3|YYYY）。"""

    published_ms: int | None
    source_order: int  # 源页序（同分钟确定性并列规则）
    prices: list[float | None]
    kellys: list[float | None]
    bad_price: bool


@dataclass
class _AhRec:
    """亚盘行解析记录（bronze rows 原行，年份推断后）。"""

    published_ms: int | None
    source_order: int  # 源页序（bronze rows 数组原序）
    home_water: float | None
    line_raw: str | None
    away_water: float | None
    minute: int | None
    score: str | None
    status: str | None
    line: float | None = None  # account 阶段落值


@dataclass
class _BookEntry:
    """字典聚合条目（1x2 game 数组每 cid 一条）。"""

    match_count: int = 0
    first_ms: int = 0
    last_ms: int = 0
    name_en: str | None = None
    name_zh: str | None = None
    # 名字胜出轨迹键 (fetched_ms, sid)：流式按到达序吸收，等价于原
    # sorted(decorated) 后行胜出（票 19 bookmaker 侧流式化，语义零变化）
    en_key: tuple[int, str] | None = None
    zh_key: tuple[int, str] | None = None


_BOOKMAKER_SCHEMA = pa.schema(
    [
        pa.field("bookmaker_id", pa.string()),
        pa.field("space", pa.string()),  # 1x2 / ah（端点族，双空间撞名防线）
        pa.field("cid", pa.string()),
        pa.field("name_en", pa.string()),  # game 数组英文名（数据面资产）
        pa.field("name_zh_masked", pa.string()),  # 站点自带遮罩短名
        pa.field("match_count", pa.int32()),
        pa.field("first_seen", pa.timestamp("ms", tz="UTC")),  # 语料内观测窗
        pa.field("last_seen", pa.timestamp("ms", tz="UTC")),
    ]
)

_ODDS_SCHEMA = pa.schema(
    [
        pa.field("sid", pa.string()),
        pa.field("bookmaker_id", pa.string()),
        pa.field("market", pa.string()),  # 1x2 / ah
        pa.field("published_at", pa.timestamp("ms", tz="UTC")),  # 源页分钟级标注
        pa.field("observed_at", pa.timestamp("ms", tz="UTC")),  # bronze fetched_at
        pa.field("source_order", pa.int32()),  # 源页轨迹原始行序（同分钟并列规则）
        pa.field(
            "kickoff", pa.timestamp("ms", tz="UTC")
        ),  # fixture 冗余列（孤儿 None）
        pa.field("odds_home", pa.float64()),
        pa.field("odds_draw", pa.float64()),
        pa.field("odds_away", pa.float64()),
        pa.field("kelly_home", pa.float64()),
        pa.field("kelly_draw", pa.float64()),
        pa.field("kelly_away", pa.float64()),
        pa.field("line_raw", pa.string()),
        pa.field("line", pa.float64()),
        pa.field("home_water", pa.float64()),
        pa.field("away_water", pa.float64()),
        pa.field("minute", pa.int16()),
        pa.field("score", pa.string()),
        pa.field("status", pa.string()),  # 即/封（源页字样，非成交证据）
    ]
)


@dataclass
class SilverBookmakerReport:
    """一次 bookmaker 字典重物化报告。"""

    silver_version: str = BOOKMAKER_SILVER_VERSION
    rows: int = 0
    matches: int = 0  # 参与聚合的 1x2 选中轨迹数
    ah_anchor: bool = False  # 亚盘锚书商是否在列（有报价轨迹才登）
    built_at: str = ""


@dataclass
class SilverOddsReport:
    """
    一次 odds_change_event 重物化报告（含门④转换完整性记账）。

    记账恒等式（票 56 门④）：源行 = 事件 + 心跳 + 坏时间行 + 未映射行 +
    未解释缺口，末项必须为 0。pre_kickoff_last_age 度量"距最后一次变价"
    （心跳已丢，非"距最后一次可见确认"——消费端读数须知）。
    """

    silver_version: str = ODDS_SILVER_VERSION
    events_1x2: int = 0
    events_ah: int = 0
    heartbeat_dropped: int = 0  # 与上一保留行值组全等而丢弃的行
    source_rows_1x2: int = 0  # 所选轨迹的 1x2 源行总数（记账分母）
    source_rows_ah: int = 0
    bad_time_rows: int = 0  # 时间不可解（无事件可落，跳行留痕）
    unmapped_gameid_rows: int = 0  # gameDetail 的 gameid 不在 game 数组（无法归书）
    unexplained_gap: int = 0  # 门④：必须为 0
    bad_prices: int = 0  # 价/水解析失败（值落 None，行保留）
    bad_line_values: int = 0  # 线串未识别（line=None，行保留）
    same_minute_conflicts: int = 0  # 同 published_at 多行组数（并列规则触发面）
    open_quote_missing: int = 0  # game 初盘价不在该书面轨迹首行（截断信号）
    no_pre_kickoff_quote: int = 0  # 有事件但无赛前行的轨迹数（赛中价不顶替）
    pre_kickoff_last_age: dict[str, float] = field(  # 距最后一次变价（秒）
        default_factory=dict
    )
    orphan_sids: int = 0
    trajectories: int = 0  # 至少一条事件的 (sid×书商) 轨迹数
    book_count: int = 0  # 事件面实际出现的书商数
    coverage_by_competition: dict[str, dict[str, float]] = field(
        default_factory=dict
    )  # 赛事级存在率/赛前末可见率摘要（书商级矩阵归切片 17 门报告查询）
    partitions: int = 0
    stale_partitions_removed: int = 0
    seasons: dict[str, int] = field(default_factory=dict)
    built_at: str = ""


def _beijing_ms(naive: datetime) -> int:
    """北京墙钟 naive → epoch 毫秒（固定 UTC+8，不依赖机器时区）。"""
    return int(naive.replace(tzinfo=_BEIJING).timestamp() * 1000)


def _fetched_ms(fetched_at: object) -> int | None:
    """信封 fetched_at（ISO 串）→ epoch 毫秒；不可解 None。"""
    try:
        return int(datetime.fromisoformat(str(fetched_at)).timestamp() * 1000)
    except ValueError:
        return None


def _nearest_year_ms(
    month: int, day: int, hour: int, minute: int, anchor: datetime
) -> int | None:
    """无年份的亚盘行时间 → 锚年 ±1 中与锚点最近的候选（跨年场安全）。"""
    best_delta: float | None = None
    best_ms: int | None = None
    for year in (anchor.year - 1, anchor.year, anchor.year + 1):
        try:
            candidate = datetime(year, month, day, hour, minute)
        except ValueError:
            continue
        delta = abs((candidate - anchor).total_seconds())
        if best_delta is None or delta < best_delta:
            best_delta = delta
            best_ms = _beijing_ms(candidate)
    return best_ms


def _parse_detail_row(raw: str, source_order: int) -> _DetailRec:
    fields = raw.split("|")
    published: int | None = None
    if len(fields) == _DETAIL_FIELDS:
        found = _TIME_RE.fullmatch(fields[3].strip())
        year = fields[7].strip()
        if found is not None and year.isdigit():
            month, day, hour, minute = (int(g) for g in found.groups())
            try:
                published = _beijing_ms(datetime(int(year), month, day, hour, minute))
            except ValueError:
                published = None
    prices = [srct_silver.to_float(v) for v in fields[0:3]]
    return _DetailRec(
        published_ms=published,
        source_order=source_order,
        prices=prices,
        kellys=[srct_silver.to_float(v) for v in fields[4:7]],
        bad_price=any(p is None for p in prices),
    )


def _values_1x2(rec: _DetailRec) -> tuple[object, ...]:
    """1x2 值组：三价+三凯利（去重核与同分钟冲突计数共用）。"""
    return (*rec.prices, *rec.kellys)


def _values_ah(rec: _AhRec) -> tuple[object, ...]:
    """亚盘值组：线原串+双水+状态（minute/score 为上下文列不进值组）。"""
    return (rec.line_raw, rec.home_water, rec.away_water, rec.status)


def _row_sort_key(row: _TimedRow) -> tuple[int, int]:
    """时间升序；不可解时间垫底（随后按 bad_time 跳行，序不影响结果）。"""
    published = row.published_ms
    return (
        published if published is not None else _UNSORTABLE_MS,
        row.source_order,
    )


def _same_minute_value_conflicts[R: _TimedRow](
    rows: Iterable[R], value_of: Callable[[R], tuple[object, ...]]
) -> int:
    """
    同 published_ms 且组内值组 ≥2 种的组数。

    design §四"同分钟异价"口径：同值心跳对不计入冲突。
    """
    groups: Counter[int] = Counter()
    distinct: dict[int, set[tuple[object, ...]]] = {}
    for row in rows:
        if row.published_ms is None:
            continue
        groups[row.published_ms] += 1
        distinct.setdefault(row.published_ms, set()).add(value_of(row))
    return sum(1 for ms, count in groups.items() if count > 1 and len(distinct[ms]) > 1)


def _keep_value_changes[R: _TimedRow](
    rows: list[R],
    report: SilverOddsReport,
    *,
    value_of: Callable[[R], tuple[object, ...]],
    account_row: Callable[[R], None],
) -> list[R]:
    """
    去重核（两 market 共用）。

    按 (published_at, source_order) 升序，值组与上一保留行全等的行丢弃——
    A→B→A 保留、首条自然保留、末条同值心跳丢弃。不可解时间行不落事件
    （bad_time 记账）；行级坏值口径由 account_row 注入。
    """
    rows.sort(key=_row_sort_key)
    kept: list[R] = []
    last_values: tuple[object, ...] | None = None
    for rec in rows:
        if rec.published_ms is None:
            report.bad_time_rows += 1
            continue
        account_row(rec)
        values = value_of(rec)
        if values == last_values:
            report.heartbeat_dropped += 1
            continue
        last_values = values
        kept.append(rec)
    return kept


def _initial_triple(game_fields: list[str]) -> tuple[float, float, float] | None:
    """取 game 行初盘 HDA 三值（列位 3-5）；缺席/坏值 None。"""
    if len(game_fields) < _GAME_INITIAL_END:
        return None
    values = [srct_silver.to_float(v) for v in game_fields[3:6]]
    if any(v is None for v in values):
        return None
    return cast("tuple[float, float, float]", tuple(values))


def _to_int(value: object) -> int | None:
    """临场分钟数值化（贴源串；非整数 None——早场无分钟列）。"""
    if value is None:
        return None
    try:
        return int(str(value))
    except ValueError:
        return None


class _StreamingPartitions:
    """
    分区流式写（事件表专用）：buffer 达 chunk_rows 即落一行组。

    到达序即文件终序——上游按 (kickoff, sid, bookmaker_id, market,
    published_at, source_order) 序 append；tmp 原子替换 + 陈旧分区清理
    与一次写形 builder 同语义。
    """

    def __init__(self, root: Path, schema: pa.Schema, chunk_rows: int) -> None:
        self._root = root
        self._schema = schema
        self._chunk = max(1, chunk_rows)
        self._buffers: dict[Path, list[dict[str, object]]] = {}
        self._writers: dict[Path, pq.ParquetWriter] = {}
        self.season_rows: Counter[str] = Counter()

    def append(self, season: str, competition: str, row: dict[str, object]) -> None:
        part = self._root / f"season={season}" / f"competition={competition}"
        buffer = self._buffers.setdefault(part, [])
        buffer.append(row)
        self.season_rows[season] += 1
        if len(buffer) >= self._chunk:
            self._flush(part)

    def _flush(self, part: Path) -> None:
        buffer = self._buffers.get(part)
        if not buffer:
            return
        part.mkdir(parents=True, exist_ok=True)
        tmp = part / "data.parquet.tmp"
        writer = self._writers.get(part)
        if writer is None:
            writer = pq.ParquetWriter(tmp, self._schema, compression="zstd")
            self._writers[part] = writer
        writer.write_table(pa.Table.from_pylist(buffer, schema=self._schema))
        self._buffers[part] = []

    def close(self) -> tuple[int, int]:
        """收尾：冲全部 buffer、关 writer、tmp 替换、清陈旧分区。"""
        for part in list(self._buffers):
            self._flush(part)
        for writer in self._writers.values():
            writer.close()
        for part in self._writers:
            (part / "data.parquet.tmp").replace(part / "data.parquet")
        target = set(self._writers)
        return len(target), srct_silver.remove_stale(self._root, target)


@dataclass
class _CompetitionCoverage:
    """按赛事覆盖摘要（design §七：存在率/赛前末可见率的报告侧原料）。"""

    trajectories: int = 0
    events: int = 0
    pre_kickoff_trajectories: set[tuple[str, str]] = field(default_factory=set)
    books: set[str] = field(default_factory=set)


@dataclass
class _EmitTracker:
    """事件面聚合记账（build_odds_change_events 汇总用）。"""

    seen_books: set[str] = field(default_factory=set)
    pre_kickoff_ages: list[float] = field(default_factory=list)
    trajectory_pre: dict[tuple[str, str], bool] = field(default_factory=dict)
    by_competition: dict[str, _CompetitionCoverage] = field(default_factory=dict)

    def register(
        self,
        sid: str,
        kept: list[tuple[str, int]],
        kickoff_ms: int | None,
        competition: str,
    ) -> None:
        """
        一场一 market 的保留行入账（kept 含多书）：按 (sid×书商) 轨迹分账。

        pre 年龄取赛前末笔（min 距开球）。
        """
        books: dict[str, list[int]] = {}
        for bookmaker_id, published in kept:
            books.setdefault(bookmaker_id, []).append(published)
        coverage = self.by_competition.setdefault(competition, _CompetitionCoverage())
        for bookmaker_id, publisheds in books.items():
            key = (sid, bookmaker_id)
            self.seen_books.add(bookmaker_id)
            coverage.books.add(bookmaker_id)
            coverage.trajectories += 1
            coverage.events += len(publisheds)
            self.trajectory_pre.setdefault(key, False)
            if kickoff_ms is None:
                continue
            pre_ages = [
                (kickoff_ms - published) / 1000
                for published in publisheds
                if published < kickoff_ms
            ]
            if pre_ages:
                self.trajectory_pre[key] = True
                self.pre_kickoff_ages.append(min(pre_ages))
                coverage.pre_kickoff_trajectories.add(key)


def _absorb_games(
    bronze: dict[str, object], entries: dict[str, _BookEntry], fetched: int, sid: str
) -> None:
    """一场 1x2 轨迹的 game 数组聚合进字典条目（最新轨迹名胜出）。"""
    payload = bronze.get("payload")
    games = (
        cast("list[object]", payload.get("game", []))
        if isinstance(payload, dict)
        else []
    )
    key = (fetched, sid)
    for game in games:
        fields = str(game).split("|")
        if len(fields) <= _GAME_NAME_EN_IDX or not fields[0].strip():
            continue
        cid = fields[0].strip()
        entry = entries.setdefault(cid, _BookEntry(first_ms=fetched, last_ms=fetched))
        entry.match_count += 1
        entry.first_ms = min(entry.first_ms, fetched)
        entry.last_ms = max(entry.last_ms, fetched)
        name_en = fields[_GAME_NAME_EN_IDX].strip()
        if name_en and (entry.en_key is None or key > entry.en_key):
            entry.name_en = name_en
            entry.en_key = key
        if len(fields) > _GAME_NAME_ZH_IDX:
            name_zh = fields[_GAME_NAME_ZH_IDX].strip()
            if name_zh and (entry.zh_key is None or key > entry.zh_key):
                entry.name_zh = name_zh
                entry.zh_key = key


def build_bookmakers(store: CorpusStore) -> SilverBookmakerReport:
    """
    重物化 silver bookmaker 字典（幂等；输入=bronze odds_1x2d 当前版本）。

    数据派生全量：每 cid 一行=源身份（en 名/站点遮罩短名，最新轨迹胜出）
    + 覆盖画像（match_count 与语料内首/末观测时刻）。亚盘锚书商按
    `srct:ah:8` 登记仅当其有非空报价轨迹。**无 tier/is_core**（选书归
    模型/策略层，ADR-0011 决策 4 修订）。小表不分区、单文件。

    输入路径流式（票 19）：选轨账本只记行号，载荷遍命中才物化单行——
    内存=聚合条目，与轨迹总量无关（字典只需 game 数组与信封，无需 spill）。
    """
    report = SilverBookmakerReport(built_at=utc_now_iso())
    entries: dict[str, _BookEntry] = {}
    ledger = srct_silver.latest_bronze_ledger(store, srct.ODDS_DATASET)
    for sid, bronze in srct_silver.iter_selected_rows(store, srct.ODDS_DATASET, ledger):
        _absorb_games(bronze, entries, _fetched_ms(bronze.get("fetched_at")) or 0, sid)
    ah_count = 0
    ah_first = 0
    ah_last = 0
    hdp_ledger = srct_silver.latest_bronze_ledger(store, srct.HANDICAP_DATASET)
    for _, bronze in srct_silver.iter_selected_rows(
        store, srct.HANDICAP_DATASET, hdp_ledger
    ):
        payload = bronze.get("payload")
        if isinstance(payload, dict) and payload.get("rows"):
            fetched = _fetched_ms(bronze.get("fetched_at"))
            if fetched is not None:
                ah_count += 1
                ah_first = fetched if ah_count == 1 else min(ah_first, fetched)
                ah_last = max(ah_last, fetched)
    # 锚书商独立成行：即便 1x2 联合空间真有同号 cid 也互不覆盖（前缀已隔离）
    anchor_book: _BookEntry | None = None
    if ah_count:
        anchor_book = _BookEntry(
            match_count=ah_count,
            first_ms=ah_first,
            last_ms=ah_last,
            # 页面无名（design-15 §二），人工核名挂账
        )
        report.ah_anchor = True
    rows: list[dict[str, object]] = [
        {
            "bookmaker_id": f"srct:{SPACE_1X2}:{cid}",
            "space": SPACE_1X2,
            "cid": cid,
            "name_en": entry.name_en,
            "name_zh_masked": entry.name_zh,
            "match_count": entry.match_count,
            "first_seen": entry.first_ms,
            "last_seen": entry.last_ms,
        }
        for cid, entry in sorted(entries.items())
    ]
    if anchor_book is not None:
        rows.append(
            {
                "bookmaker_id": AH_ANCHOR_ID,
                "space": SPACE_AH,
                "cid": AH_ANCHOR_CID,
                "name_en": anchor_book.name_en,
                "name_zh_masked": anchor_book.name_zh,
                "match_count": anchor_book.match_count,
                "first_seen": anchor_book.first_ms,
                "last_seen": anchor_book.last_ms,
            }
        )
    root = store.root / "silver" / srct.SRCT_PROVIDER / BOOKMAKER_DATASET
    root.mkdir(parents=True, exist_ok=True)
    tmp = root / "data.parquet.tmp"
    pq.write_table(
        pa.Table.from_pylist(rows, schema=_BOOKMAKER_SCHEMA), tmp, compression="zstd"
    )
    tmp.replace(root / "data.parquet")
    report.rows = len(rows)
    report.matches = len(ledger)
    srct_silver.write_dataset_meta(
        root,
        {
            "silver_version": BOOKMAKER_SILVER_VERSION,
            "bronze_version": srct.BRONZE_VERSIONS[srct.ODDS_DATASET],
            "built_at": report.built_at,
            "rows": report.rows,
            "matches": report.matches,
            "ah_anchor": report.ah_anchor,
        },
    )
    return report


def _append_1x2_rows(
    bronze: dict[str, object],
    partition: tuple[str, str],
    kickoff_ms: int | None,
    observed_ms: int | None,
    bookmaker_id: str,
    kept: list[_DetailRec],
    out: _StreamingPartitions,
) -> None:
    """一书保留行 → 事件行（亚盘侧列全 None；行构造两 market 对称）。"""
    for rec in kept:
        out.append(
            partition[0],
            partition[1],
            {
                "sid": bronze.get("sid"),
                "bookmaker_id": bookmaker_id,
                "market": SPACE_1X2,
                "published_at": rec.published_ms,
                "observed_at": observed_ms,
                "source_order": rec.source_order,
                "kickoff": kickoff_ms,
                "odds_home": rec.prices[0],
                "odds_draw": rec.prices[1],
                "odds_away": rec.prices[2],
                "kelly_home": rec.kellys[0],
                "kelly_draw": rec.kellys[1],
                "kelly_away": rec.kellys[2],
                "line_raw": None,
                "line": None,
                "home_water": None,
                "away_water": None,
                "minute": None,
                "score": None,
                "status": None,
            },
        )


def _emit_1x2_events(
    bronze: dict[str, object],
    partition: tuple[str, str],
    kickoff_ms: int | None,
    report: SilverOddsReport,
    out: _StreamingPartitions,
) -> list[tuple[str, int]]:
    """一场 1x2 轨迹 → 变化事件；返回 (bookmaker_id, published) 保留行元数据。"""
    payload = bronze.get("payload")
    payload_dict = payload if isinstance(payload, dict) else {}
    observed = _fetched_ms(bronze.get("fetched_at"))
    game_map: dict[str, str] = {}
    initials: dict[str, tuple[float, float, float] | None] = {}
    for game in cast("list[object]", payload_dict.get("game", [])):
        fields = str(game).split("|")
        if len(fields) > 1 and fields[0].strip():
            game_map.setdefault(fields[1], fields[0].strip())
            initials.setdefault(fields[0].strip(), _initial_triple(fields))
    books: dict[str, list[_DetailRec]] = {}
    for entry in cast("list[object]", payload_dict.get("game_detail", [])):
        gameid, sep, body = str(entry).partition("^")
        raw_rows = [r for r in body.split(";") if r]
        report.source_rows_1x2 += len(raw_rows)
        cid = game_map.get(gameid) if sep else None
        if cid is None:
            report.unmapped_gameid_rows += len(raw_rows)
            continue
        shelf = books.setdefault(cid, [])
        base = len(shelf)  # 先取基准：extend 内 len 是惰性求值会随消费增长
        shelf.extend(_parse_detail_row(raw, base + i) for i, raw in enumerate(raw_rows))
    kept_meta: list[tuple[str, int]] = []
    for cid in sorted(books):
        rows = books[cid]

        def value_of(rec: _DetailRec) -> tuple[object, ...]:
            return (*rec.prices, *rec.kellys)  # 值组两用（冲突计数+去重核）

        report.same_minute_conflicts += _same_minute_value_conflicts(rows, value_of)

        def _account(rec: _DetailRec) -> None:
            if rec.bad_price:
                report.bad_prices += 1

        kept = _keep_value_changes(
            rows, report, value_of=_values_1x2, account_row=_account
        )
        # §四口径：与 detail 首行（pre-dedup 最早可解时间行）比，非首个保留行
        earliest = next((r.prices for r in rows if r.published_ms is not None), [])
        initial = initials.get(cid)
        if kept and initial is not None and earliest != list(initial):
            report.open_quote_missing += 1
        bookmaker_id = f"srct:{SPACE_1X2}:{cid}"
        _append_1x2_rows(
            bronze, partition, kickoff_ms, observed, bookmaker_id, kept, out
        )
        kept_meta.extend((bookmaker_id, rec.published_ms or 0) for rec in kept)
        report.events_1x2 += len(kept)
    return kept_meta


def _emit_ah_events(
    bronze: dict[str, object],
    partition: tuple[str, str],
    kickoff_ms: int | None,
    anchor: datetime | None,
    report: SilverOddsReport,
    out: _StreamingPartitions,
) -> list[tuple[str, int]]:
    """一场亚盘轨迹 → 变化事件（年份推断/线值归一/上下文列随行）。"""
    payload = bronze.get("payload")
    payload_dict = payload if isinstance(payload, dict) else {}
    raw_rows = [
        r
        for r in cast("list[object]", payload_dict.get("rows", []))
        if isinstance(r, dict)
    ]
    report.source_rows_ah += len(raw_rows)
    if not raw_rows:
        return []
    if anchor is None:
        # 无锚即无年可推：整场记坏时间行（留痕不丢账）
        report.bad_time_rows += len(raw_rows)
        return []
    observed = _fetched_ms(bronze.get("fetched_at"))
    parsed: list[_AhRec] = []
    for idx, row in enumerate(cast("list[dict[str, object]]", raw_rows)):
        found = _TIME_RE.fullmatch(str(row.get("change_time") or "").strip())
        published: int | None = None
        if found is not None:
            month, day, hour, minute = (int(g) for g in found.groups())
            published = _nearest_year_ms(month, day, hour, minute, anchor)
        parsed.append(
            _AhRec(
                published_ms=published,
                source_order=idx,
                home_water=srct_silver.to_float(row.get("home_water")),
                line_raw=(str(row["line"]) if row.get("line") is not None else None),
                away_water=srct_silver.to_float(row.get("away_water")),
                minute=_to_int(row.get("minute")),
                score=(str(row["score"]) if row.get("score") is not None else None),
                status=(str(row["status"]) if row.get("status") is not None else None),
            )
        )

    def value_of(rec: _AhRec) -> tuple[object, ...]:  # 值组两用（冲突计数+去重核）
        return (rec.line_raw, rec.home_water, rec.away_water, rec.status)

    report.same_minute_conflicts += _same_minute_value_conflicts(parsed, value_of)

    def _account(rec: _AhRec) -> None:
        rec.line = normalize_line(rec.line_raw)
        if rec.line_raw is not None and rec.line is None:
            report.bad_line_values += 1
        if rec.line_raw is not None and (
            rec.home_water is None or rec.away_water is None
        ):
            report.bad_prices += 1

    kept = _keep_value_changes(
        parsed, report, value_of=_values_ah, account_row=_account
    )
    kept_meta: list[tuple[str, int]] = []
    for rec in kept:
        out.append(
            partition[0],
            partition[1],
            {
                "sid": bronze.get("sid"),
                "bookmaker_id": AH_ANCHOR_ID,
                "market": SPACE_AH,
                "published_at": rec.published_ms,
                "observed_at": observed,
                "source_order": rec.source_order,
                "kickoff": kickoff_ms,
                "odds_home": None,
                "odds_draw": None,
                "odds_away": None,
                "kelly_home": None,
                "kelly_draw": None,
                "kelly_away": None,
                "line_raw": rec.line_raw,
                "line": rec.line,
                "home_water": rec.home_water,
                "away_water": rec.away_water,
                "minute": rec.minute,
                "score": rec.score,
                "status": rec.status,
            },
        )
        kept_meta.append((AH_ANCHOR_ID, rec.published_ms or 0))
        report.events_ah += 1
    return kept_meta


def _emit_sid(
    sid: str,
    meta: dict[str, dict[str, object]],
    odds_bronze: dict[str, object] | None,
    hdp_bronze: dict[str, object] | None,
    report: SilverOddsReport,
    out: _StreamingPartitions,
    tracker: _EmitTracker,
) -> None:
    """一场 → 两 market 事件（分区/年份锚/记账注册；1x2 先 ah 后=文件序）。"""
    fixture = meta.get(sid)
    if fixture is None:
        report.orphan_sids += 1
        partition: tuple[str, str] = ("_unknown", "_unknown")
        kickoff_ms = None
    else:
        kickoff = cast("datetime", fixture["kickoff"])
        partition = (srct_silver.season_of(kickoff), str(fixture["league"]))
        kickoff_ms = _beijing_ms(kickoff)
    if odds_bronze is not None:
        tracker.register(
            sid,
            _emit_1x2_events(odds_bronze, partition, kickoff_ms, report, out),
            kickoff_ms,
            partition[1],
        )
    if hdp_bronze is not None:
        # 年份锚=fixture 开球（北京墙钟）——孤儿无锚不推年（design §四未授权
        # 其他锚：抓取时刻锚会给回填场造出错误甚至未来的时间戳），整场记坏时间行
        anchor = cast("datetime", fixture["kickoff"]) if fixture is not None else None
        tracker.register(
            sid,
            _emit_ah_events(hdp_bronze, partition, kickoff_ms, anchor, report, out),
            kickoff_ms,
            partition[1],
        )


def _spill_name(chunk_idx: int) -> str:
    return f"{chunk_idx:06d}.ndjson"


def _spill_selected(
    store: CorpusStore,
    dataset: str,
    ledger: dict[str, int],
    chunk_of: dict[str, int],
    spill_root: Path,
) -> None:
    """
    载荷遍（票 19 spill 步）：单次流过 bronze，选中行按块写暂存 NDJSON。

    与 iter_selected_rows 不同：原文透传零 json.loads——spill 保字节，
    未选中行连解析都不付。
    """
    want = {lineno: sid for sid, lineno in ledger.items()}
    handles: dict[int, TextIO] = {}
    try:
        for lineno, line in enumerate(
            store.iter_bronze_lines(srct.SRCT_PROVIDER, dataset)
        ):
            sid = want.get(lineno)
            if sid is None:
                continue  # 旧版本/已被后行胜出的轨迹：不物化
            idx = chunk_of[sid]
            fh = handles.get(idx)
            if fh is None:
                # 追加模式：odds/hdp 两遍共用块文件（目录已先清空，首写即建）
                fh = (spill_root / _spill_name(idx)).open("a", encoding="utf-8")
                handles[idx] = fh
            fh.write(line + "\n")
    finally:
        for fh in handles.values():
            fh.close()


def build_odds_change_events(
    store: CorpusStore,
    *,
    chunk_rows: int = STREAM_CHUNK_ROWS,
    chunk_sids: int = STREAM_CHUNK_SIDS,
) -> SilverOddsReport:
    """
    重物化 silver odds_change_event（幂等；两数据集 bronze 当前版本）。

    输入路径内存有界（票 19 外排三步）：① 选轨遍只记信封行号；② sid 按
    (kickoff, sid) 全局排序后连续切块——块内连续块间有序，**块序即输出
    全局序**（排序契约不破），单次流过 bronze 把选中行按块 spill 树内
    暂存 NDJSON；③ 逐块加载→现有转换逻辑逐场执行→分区 writer 跨块追加
    →释放→下一块。内存峰值=单块，与语料总量无关；分块参数对输出完全
    透明（字节级）。孤儿场排序垫后，天然归末块。暂存目录处理完即删，
    异常残留由下次重建先行清空（重跑幂等）。

    流出序=文件终序：sid 按 (kickoff, sid)（孤儿垫后），场内先 1x2（cid
    升序）后 ah，书内 (published_at, source_order) 升序。分区取 fixture
    元数据（orphan → `_unknown`）。门④记账见 SilverOddsReport。
    """
    report = SilverOddsReport(built_at=utc_now_iso())
    meta = {str(r["sid"]): r for r in srct_silver.fixture_rows(store)[0]}
    odds_ledger = srct_silver.latest_bronze_ledger(store, srct.ODDS_DATASET)
    hdp_ledger = srct_silver.latest_bronze_ledger(store, srct.HANDICAP_DATASET)
    root = store.root / "silver" / srct.SRCT_PROVIDER / ODDS_DATASET
    out = _StreamingPartitions(root, _ODDS_SCHEMA, chunk_rows)
    tracker = _EmitTracker()

    def _sid_sort_key(sid: str) -> tuple[bool, datetime, str]:
        fixture = meta.get(sid)
        kickoff = cast("datetime", fixture["kickoff"]) if fixture else datetime.min
        return (fixture is None, kickoff, sid)

    sids = sorted(set(odds_ledger) | set(hdp_ledger), key=_sid_sort_key)
    size = max(1, chunk_sids)
    chunks = [sids[start : start + size] for start in range(0, len(sids), size)]
    chunk_of = {sid: idx for idx, part in enumerate(chunks) for sid in part}

    spill_root = store.root / "tmp" / "srct_odds_spill"
    if spill_root.exists():  # 上次异常退出的残留：先清再跑
        shutil.rmtree(spill_root)
    spill_root.mkdir(parents=True)
    try:
        for dataset, ledger in (
            (srct.ODDS_DATASET, odds_ledger),
            (srct.HANDICAP_DATASET, hdp_ledger),
        ):
            _spill_selected(store, dataset, ledger, chunk_of, spill_root)
        for idx, part in enumerate(chunks):
            odds_map: dict[str, dict[str, object]] = {}
            hdp_map: dict[str, dict[str, object]] = {}
            with (spill_root / _spill_name(idx)).open(encoding="utf-8") as fh:
                for line in fh:
                    row = json.loads(line)
                    if row.get("dataset") == srct.HANDICAP_DATASET:
                        hdp_map[str(row["sid"])] = row
                    else:
                        odds_map[str(row["sid"])] = row
            for sid in part:  # 块内序=全局序切片：输出与不分块字节级一致
                _emit_sid(
                    sid, meta, odds_map.get(sid), hdp_map.get(sid), report, out, tracker
                )
    finally:
        shutil.rmtree(spill_root, ignore_errors=True)
        partitions, stale = out.close()
    report.partitions = partitions
    report.stale_partitions_removed = stale
    report.seasons = dict(out.season_rows)
    report.trajectories = len(tracker.trajectory_pre)
    report.no_pre_kickoff_quote = sum(
        1 for has in tracker.trajectory_pre.values() if not has
    )
    report.book_count = len(tracker.seen_books)
    report.coverage_by_competition = {
        competition: {
            "trajectories": cov.trajectories,
            "events": cov.events,
            "books": len(cov.books),
            "pre_kickoff_coverage": (
                len(cov.pre_kickoff_trajectories) / cov.trajectories
                if cov.trajectories
                else 0.0
            ),
        }
        for competition, cov in sorted(tracker.by_competition.items())
    }
    if tracker.pre_kickoff_ages:
        report.pre_kickoff_last_age = {
            "p50_s": statistics.median(tracker.pre_kickoff_ages),
            "max_s": max(tracker.pre_kickoff_ages),
        }
    accounted = (
        report.events_1x2
        + report.events_ah
        + report.heartbeat_dropped
        + report.bad_time_rows
        + report.unmapped_gameid_rows
    )
    total = report.source_rows_1x2 + report.source_rows_ah
    report.unexplained_gap = total - accounted  # 负值=过记账，同样不许静默
    srct_silver.write_dataset_meta(
        root,
        {
            "silver_version": ODDS_SILVER_VERSION,
            "bronze_versions": {
                "odds": srct.BRONZE_VERSIONS[srct.ODDS_DATASET],
                "handicap": srct.BRONZE_VERSIONS[srct.HANDICAP_DATASET],
            },
            "built_at": report.built_at,
            "events_1x2": report.events_1x2,
            "events_ah": report.events_ah,
            "heartbeat_dropped": report.heartbeat_dropped,
            "source_rows": total,
            "unexplained_gap": report.unexplained_gap,
            "orphan_sids": report.orphan_sids,
            "partitions": report.partitions,
            "seasons": report.seasons,
            "coverage_by_competition": report.coverage_by_competition,
        },
    )
    return report
