"""
源T 教练任期/队籍语料采集班（票 08，spec-srct-cutover）。

四个 bronze 数据集（srct 命名空间，raw-first 同构）——按实体/按季键控，
**不进 SPEC_ENDPOINTS**（那是每场端点循环的注册面；本模块沿 srct_shift
有界班模式自成一班）：

- ``league_season``：matchResult 逐季文件（当季球队枚举=队籍面）；
- ``team_detail``：tdl 球队详情（现任教练）；
- ``coach_page`` / ``coach_career``：教练档案页 + 履历 jsData（两步采集）。

首日三钉结论（research/08-srct-tenure-first-day-probes.md，2026-11-12 实测）：

- 钉① canonical 教练键 = playerInfo id——恰两处可得：match_detail 锚点
  href 第二段、tdl ``lineupDetail`` 主教练行首列；tdl ``coach`` 变量的
  id 与姓名系统性错配（三例实证），**永不用于身份**（姓名留作对账）。
- 钉② 履历 jsData 无任何名字变量（全世代）；中/英文名来源 = 教练档案
  HTML 页（历史教练实测可得）——coach 键形态=英文名优先、缺则 id。
- 钉③ 枚举种子 = raw match_detail 锚点离线重放（零网络）∪ tdl 现任；
  履历 jsData 冷取约半数 404（边缘缓存行为），可靠配方 = 先取档案页、
  再按其原样引用串（含 ``version`` 参数）取履历——两步法顺带归档
  ename 源，一石二鸟。

三相位一班：A matchResult（GB 双联赛逐季，球队并集）→ B tdl（逐队现任）
→ C coach 两步（种子并集，去重后逐教练）。礼貌三件套复用 srct 常量
（共享滑窗 30/min 硬顶 + 2.4s±0.8s 抖动 + NightBudget 预算/连败熔断）；
01:00-08:00 夜窗让位（复用 srct_shift 口径，手工回补 enforce_window=False）。
"""

from __future__ import annotations

import gzip
import html as html_mod
import random
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import httpx
from limits.strategies import MovingWindowRateLimiter
from loguru import logger

from goalx_backend.config import Settings
from goalx_backend.data.corpus_store import CorpusStore
from goalx_backend.data.ingest import srct
from goalx_backend.data.ingest.shell import DESKTOP_UA
from goalx_backend.data.ingest.srct_shift import BEIJING, SHIFT_INACTIVE_HOURS
from goalx_backend.rate_limit import default_limiter, throttle

# —— 数据集与解析器版本（模块自持；不进 srct.BRONZE_VERSIONS 每场注册面） ——

LEAGUE_SEASON_DATASET = "league_season"
TEAM_DETAIL_DATASET = "team_detail"
COACH_PAGE_DATASET = "coach_page"
COACH_CAREER_DATASET = "coach_career"

TENURE_BRONZE_VERSIONS: dict[str, str] = {
    LEAGUE_SEASON_DATASET: "srct_league_season_v1",
    TEAM_DETAIL_DATASET: "srct_team_detail_v1",
    COACH_PAGE_DATASET: "srct_coach_page_v1",
    COACH_CAREER_DATASET: "srct_coach_career_v1",
}

# GB 双联赛逐季键（首日探针实测：英超 36 裸用；英冠 37 是 SubLeague，
# 必须带阶段后缀 37_87——裸 s37 各季一律返回"页面不存在"桩页）
GB_LEAGUE_KEYS: tuple[tuple[str, str], ...] = (("英超", "36"), ("英冠", "37_87"))

# 逐季回填窗（spec：2015-16→，与 corpus Phase1 对齐；当季到 2026-27）
TENURE_SEASONS: tuple[str, ...] = tuple(
    f"{year}-{year + 1}" for year in range(2015, 2027)
)

# 一班请求上限：24 matchResult + ~65 tdl + ~180 教练 ×2 步 ≈ 450-700
TENURE_REQUEST_CAP = 900

# 钉②：档案页名字行标签（简体名/繁体名/英文名共用同一表格形态）
_PAGE_FIELD_FMT = (
    r"{label}[：:]\s*</span>\s*</td>\s*<td[^>]*>\s*<span[^>]*><strong>([^<]*)</strong>"
)
# 钉③：档案页对履历 jsData 的原样引用（路径形状无关，只认 coach{id}.js?version= 串）
_CAREER_REF_RE = re.compile(r'src=["\']([^"\']*?coach(\d+)\.js\?version=(\d+))["\']')
# 钉③：match_detail 首发块教练锚点（host 无关；/coach/ 路段是判据）
_COACH_ANCHOR_RE = re.compile(
    r"<a class='coach' href='[^']*/coach/(\d+)/(\d+)\.html' title='([^']*)'>"
)
# 行最小字段数（防残行/改版越位；贴源形状 2026-11-12 实测钉死）
_LEAGUE_MIN_FIELDS = 5
_TEAM_MIN_FIELDS = 4
_LINEUP_MIN_FIELDS = 10
_COACHVAR_MIN_FIELDS = 5
_TENURE_MIN_FIELDS = 9
_TRANSFER_EN_FROM = 11
_TRANSFER_EN_TO = 12
# jsData 数组行拆分：单引号串或裸数值（行内无嵌套括号，实测三族端点一致）
_JS_TOKEN_RE = re.compile(r"'([^']*)'|(-?\d+(?:\.\d+)?)")


@dataclass
class TenureStats:
    """一次任期班的统计（CLI/flow 摘要口径）。"""

    run_at: str = ""
    requests: int = 0
    league_new: int = 0
    team_new: int = 0
    coach_page_new: int = 0
    career_new: int = 0
    anchor_pages: int = 0  # 离线重放的 match_detail 页数（零网络）
    anchor_coaches: int = 0  # 锚点收获的唯一教练数（GB 过滤前）
    anchor_gb_coaches: int = 0  # 锚点教练中 GB 队过滤后入种子的数目
    tdl_coaches: int = 0  # tdl lineupDetail 现任教练种子数
    coaches_not_found: int = 0  # 档案页 404（死 id/已删档）
    failed: dict[str, str] = field(default_factory=dict)
    stopped: str | None = None  # budget/circuit/window_closed；None=全量跑完


def _decode_js(body: bytes) -> str:
    """jsData/档案页解码（实测 UTF-8 无 BOM；utf-8-sig 兼容带 BOM 变体）。"""
    return body.decode("utf-8-sig")


def _var(text: str, name: str) -> str | None:
    """`var name='v'` 取值（缺变量 None）。"""
    found = re.search(rf"var {name}\s*=\s*'([^']*)'", text)
    return found.group(1) if found is not None else None


def _js_rows(text: str, name: str) -> list[list[str]]:
    """`var name=[[..],[..]];` → 行列表（每行=字段串列表；缺变量空）。"""
    block = re.search(rf"var {name}\s*=\s*\[(.*?)\];", text, re.S)
    if block is None:
        return []
    rows: list[list[str]] = []
    for raw in re.findall(r"\[[^\[\]]*\]", block.group(1)):
        rows.append([quoted or num for quoted, num in _JS_TOKEN_RE.findall(raw)])
    return rows


def _js_flat(text: str, name: str) -> list[str]:
    """`var name=[a,'b',..];`（扁平数组）→ 字段串列表（缺变量空）。"""
    block = re.search(rf"var {name}\s*=\s*\[(.*?)\];", text, re.S)
    if block is None:
        return []
    return [quoted or num for quoted, num in _JS_TOKEN_RE.findall(block.group(1))]


def parse_league_season(body: bytes) -> dict[str, object]:
    """
    逐季 matchResult 文件 → 联赛元数据 + 当季球队枚举（队籍面）。

    桩页（SubLeague 缺数据/改版返回的"页面不存在"HTML）抛
    SrctContentError——不进 raw，下班自愈。
    """
    text = _decode_js(body)
    head = _js_flat(text, "arrLeague")
    if len(head) < _LEAGUE_MIN_FIELDS:
        msg = "league_season: 缺 arrLeague（桩页或改版）"
        raise srct.SrctContentError(msg)
    league = {
        "league_id": int(head[0]),
        "name_cn": head[1],
        "name_tw": head[2],
        "name_en": head[3],
        "season": head[4],
    }
    teams = [
        {
            "team_id": int(row[0]),
            "name_cn": row[1],
            "name_tw": row[2],
            "name_en": row[3],
        }
        for row in _js_rows(text, "arrTeam")
        if len(row) >= _TEAM_MIN_FIELDS
    ]
    return {
        "league": league,
        "teams": teams,
        "table_rows": len(_js_rows(text, "totalScore")),
        "last_update": _var(text, "lastUpdateTime"),
    }


def parse_team_detail(body: bytes) -> dict[str, object]:
    """
    球队详情 tdl → 球队元数据 + 现任教练（钉①口径）。

    教练 id 只取 ``lineupDetail`` 主教练行首列；``coach`` 变量只留
    中/繁/英姓名（其 id 与姓名系统性错配，永不用于身份）。
    """
    text = _decode_js(body)
    head = _js_flat(text, "teamDetail")
    if len(head) < _TEAM_MIN_FIELDS:
        msg = "team_detail: 缺 teamDetail（桩页或改版）"
        raise srct.SrctContentError(msg)
    team = {
        "team_id": int(head[0]),
        "name_cn": head[1],
        "name_tw": head[2],
        "name_en": head[3],
    }
    coach: dict[str, object] | None = None
    for row in _js_rows(text, "lineupDetail"):
        if len(row) >= _LINEUP_MIN_FIELDS and row[8] == "主教练":
            coach = {
                "coach_id": int(row[0]),
                "name_cn": row[2],
                "name_tw": row[3],
                "born": row[5] or None,
                "role": row[8],
                "nationality": row[9] or None,
            }
            break
    coach_var: dict[str, object] | None = None
    var_rows = _js_rows(text, "coach")
    if var_rows and len(var_rows[0]) >= _COACHVAR_MIN_FIELDS:
        first = var_rows[0]
        coach_var = {"name_cn": first[2], "name_tw": first[3], "name_en": first[4]}
    return {"team": team, "coach": coach, "coach_var": coach_var}


def parse_coach_page(body: bytes) -> dict[str, object]:
    """
    教练档案 HTML 页 → 中/英文名 + 履历 jsData 引用（钉②口径）。

    名字行（简体名/英文名）是 ename 全世代唯一来源；履历引用给两步法
    提供 cid 与 version（路径形状无关，只认 coach{id}.js?version= 串）。
    """
    text = _decode_js(body)
    payload: dict[str, object] = {}
    for key, label in (
        ("name_cn", "简体名"),
        ("name_tw", "繁体名"),
        ("name_en", "英文名"),
    ):
        found = re.search(_PAGE_FIELD_FMT.format(label=label), text)
        payload[key] = html_mod.unescape(found.group(1)).strip() if found else None
    ref = _CAREER_REF_RE.search(text)
    payload["career_cid"] = ref.group(2) if ref else None
    payload["career_version"] = ref.group(3) if ref else None
    return payload


def parse_coach_career(body: bytes) -> dict[str, object]:
    """
    教练履历 jsData → 任期行（起止/team_id/中英队名/分段战绩）+ 转会轨迹。

    钉②注：本 payload 无任何名字变量（名字在档案页）；缺履历变量抛
    SrctContentError（伪 200 防混入）。
    """
    text = _decode_js(body)
    if "var coachCount" not in text and "var transferInfo" not in text:
        msg = "coach_career: 缺履历变量（伪 200 或改版）"
        raise srct.SrctContentError(msg)
    tenures = [
        {
            "start": row[0],
            "end": row[1] or None,
            "team_id": int(row[3]),
            "name_cn": row[4].split("^")[0],
            "name_tw": row[4].split("^")[1],
            "name_en": row[4].split("^")[2],
            "matches": int(row[5]),
            "wins": int(row[6]),
            "draws": int(row[7]),
            "losses": int(row[8]),
        }
        for row in _js_rows(text, "coachCount")
        if len(row) >= _TENURE_MIN_FIELDS and "^" in row[4]
    ]
    transfers = [
        {
            "season": row[0],
            "from_team_id": int(row[1]) if row[1].lstrip("-").isdigit() else None,
            "to_team_id": int(row[2]) if row[2].lstrip("-").isdigit() else None,
            "start": row[3],
            "end": row[4] or None,
            "name_en_from": (
                row[_TRANSFER_EN_FROM] if len(row) > _TRANSFER_EN_FROM else None
            ),
            "name_en_to": row[_TRANSFER_EN_TO] if len(row) > _TRANSFER_EN_TO else None,
        }
        for row in _js_rows(text, "transferInfo")
        if len(row) >= _COACHVAR_MIN_FIELDS
    ]
    return {
        "once_team": _var(text, "onceTeam"),
        "now_team": _js_rows(text, "nowTeamInfo"),
        "transfers": transfers,
        "tenures": tenures,
        "last_update": _var(text, "lastUpdateTime"),
    }


def harvest_coach_anchors(detail_dir: Path) -> dict[str, dict[str, object]]:
    """
    锚点离线重放 raw match_detail → 教练种子（钉③零网络路径）。

    只读既有语料（首发块教练锚点：cid/team_id/中文名），不动网络不动
    台账；撕裂 gzip 文件跳过留痕（读者无锁纪律）。
    """
    names: dict[str, str] = {}
    teams: dict[str, set[str]] = {}
    for path in sorted(detail_dir.glob("*.html.gz")):
        try:
            body = gzip.decompress(path.read_bytes()).decode(
                "utf-8-sig", errors="replace"
            )
        except (OSError, EOFError, UnicodeDecodeError) as exc:
            logger.warning("tenure 锚点重放跳过撕裂文件 {}（{}）", path.name, exc)
            continue
        for team_id, cid, name in _COACH_ANCHOR_RE.findall(body):
            names.setdefault(cid, name)
            teams.setdefault(cid, set()).add(team_id)
    return {cid: {"name": names[cid], "teams": teams[cid]} for cid in names}


def _archive(  # noqa: PLR0913 一实体接缝集（raw/bronze 双写一路传参）
    store: CorpusStore,
    dataset: str,
    key: str,
    body: bytes,
    payload: dict[str, object],
    stats: TenureStats,
    *,
    ext: str,
    counter: str,
) -> None:
    """落盘 raw + 追加 bronze（台账先行由 store.ingest_raw 保证）。"""
    ref = store.ingest_raw(srct.SRCT_PROVIDER, dataset, key, body, ext=ext)
    store.append_bronze(
        srct.SRCT_PROVIDER,
        dataset,
        [
            {
                "provider": srct.SRCT_PROVIDER,
                "dataset": dataset,
                "sid": key,
                "fetched_at": srct.utc_now_iso(),
                "parser_version": TENURE_BRONZE_VERSIONS[dataset],
                "raw_sha": ref.sha256,
                "payload": payload,
            }
        ],
    )
    setattr(stats, counter, getattr(stats, counter) + 1)


def _fetch_and_archive(
    store: CorpusStore,
    dataset: str,
    key: str,
    url: str,
    parse: Callable[[bytes], dict[str, object]],
    stats: TenureStats,
    counter: str,
    _get: Callable[[str], httpx.Response],
) -> dict[str, object] | None:
    """一实体：拉取 → 内容判别 → 落盘 raw+bronze（失败留痕 None）。"""
    try:
        body = _get(url).content
        payload = parse(body)
    except srct.NightStop:
        raise
    except srct.SrctContentError as exc:
        stats.failed[f"{dataset}:{key}"] = f"SrctContentError: {exc}"[:120]
        logger.warning("tenure {} {} 坏体（{}）", dataset, key, exc)
        return None
    except httpx.HTTPError as exc:
        stats.failed[f"{dataset}:{key}"] = f"{type(exc).__name__}: {exc}"[:120]
        logger.warning("tenure {} {} 拉取失败（{}）", dataset, key, exc)
        return None
    _archive(store, dataset, key, body, payload, stats, ext=".js", counter=counter)
    return payload


def _is_not_found(exc: httpx.HTTPError) -> bool:
    """404/410 判定（死 id 已删档——确定性，计数不重试本班）。"""
    return isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in (
        404,
        410,
    )


def run_tenure_shift(  # noqa: C901, PLR0912, PLR0913, PLR0915 三相位接缝集
    store: CorpusStore,
    settings: Settings,
    client: httpx.Client,
    *,
    seasons: Sequence[str] = TENURE_SEASONS,
    leagues: Sequence[tuple[str, str]] = GB_LEAGUE_KEYS,
    request_cap: int = TENURE_REQUEST_CAP,
    now_fn: Callable[[], datetime] | None = None,
    sleeper: Callable[[float], None] = time.sleep,
    rng: random.Random | None = None,
    jitter: tuple[float, float] | None = srct.JITTER_RANGE,
    rate_limiter: MovingWindowRateLimiter | None = None,
    budget: srct.NightBudget | None = None,
    enforce_window: bool = True,
) -> TenureStats:
    """
    推进一次任期班：A matchResult → B tdl → C coach 两步。

    幂等：每工件以 raw 键查 checkpoint，已抓零重抓（缓存件离线重解析补
    枚举，断点续跑）；预算/熔断触顶记 stopped 正常返回（进度已落库）。
    404 死 id 不占 raw 键，下班重试（量级=每死 id 每周一请求，可接受）。
    """
    now = now_fn() if now_fn is not None else datetime.now(UTC)
    stats = TenureStats(run_at=now.isoformat(timespec="seconds"))
    if enforce_window and now.astimezone(BEIJING).hour in SHIFT_INACTIVE_HOURS:
        stats.stopped = "window_closed"
        return stats
    missing = [
        name
        for name, value in (
            ("srct_league_season_url", settings.srct_league_season_url),
            ("srct_team_detail_url", settings.srct_team_detail_url),
            ("srct_coach_page_url", settings.srct_coach_page_url),
            ("srct_coach_career_url", settings.srct_coach_career_url),
        )
        if not value
    ]
    if missing:
        msg = f"srct tenure 端点未配置：{missing}（真值见 .env，代称红线）"
        raise RuntimeError(msg)
    budget = budget if budget is not None else srct.NightBudget(request_cap=request_cap)
    limiter = rate_limiter if rate_limiter is not None else default_limiter()
    jitter_rng = random.Random() if rng is None else rng  # noqa: S311 抖动非加密用途
    # 日级 version（tdl/逐季缓存参数；教练履历用档案页引用串，见相位 C）
    version_day = now.strftime("%Y%m%d")

    def _get(url: str) -> httpx.Response:
        """一请求：闸门 → 预算 → 裸头 GET（实测 Referer 无益、根 Referer 有害）。"""
        throttle(limiter, srct.REQUEST_WINDOW, sleeper)
        budget.charge(1)
        stats.requests += 1
        response = client.get(url, headers={"User-Agent": DESKTOP_UA}, timeout=30.0)
        if jitter is not None:
            sleeper(jitter_rng.uniform(*jitter))
        response.raise_for_status()
        return response

    # —— 相位 A：matchResult 逐季（GB 球队并集 = 枚举权威源） ——
    gb_team_ids: set[str] = set()
    try:
        for season in seasons:
            for _label, skey in leagues:
                key = f"{season}-s{skey}"
                if store.has(srct.SRCT_PROVIDER, LEAGUE_SEASON_DATASET, key):
                    try:
                        payload = parse_league_season(
                            store.read_raw(
                                srct.SRCT_PROVIDER,
                                LEAGUE_SEASON_DATASET,
                                key,
                                ext=".js",
                            )
                        )
                    except (srct.SrctContentError, OSError, EOFError):
                        continue  # 已占键坏体：留档下班不重抓，枚举缺该季
                else:
                    payload = _fetch_and_archive(
                        store,
                        LEAGUE_SEASON_DATASET,
                        key,
                        "{}?version={}".format(
                            settings.srct_league_season_url.format(
                                season=season, key=skey
                            ),
                            version_day,
                        ),
                        parse_league_season,
                        stats,
                        "league_new",
                        _get,
                    )
                    if payload is None:
                        continue
                for team in payload["teams"]:  # type: ignore[union-attr]
                    gb_team_ids.add(str(team["team_id"]))  # type: ignore[index]
    except srct.NightStop as stop:
        stats.stopped = stop.reason
        return stats

    # —— 相位 B：tdl 逐队（现任教练种子，钉① lineupDetail 口径） ——
    coach_seeds: dict[str, str] = {}
    try:
        for team_id in sorted(gb_team_ids):
            if store.has(srct.SRCT_PROVIDER, TEAM_DETAIL_DATASET, team_id):
                try:
                    payload = parse_team_detail(
                        store.read_raw(
                            srct.SRCT_PROVIDER, TEAM_DETAIL_DATASET, team_id, ext=".js"
                        )
                    )
                except (srct.SrctContentError, OSError, EOFError):
                    continue
            else:
                base = settings.srct_team_detail_url.format(id=team_id)
                payload = _fetch_and_archive(
                    store,
                    TEAM_DETAIL_DATASET,
                    team_id,
                    f"{base}?version={version_day}",
                    parse_team_detail,
                    stats,
                    "team_new",
                    _get,
                )
                if payload is None:
                    continue
            coach = cast("dict[str, object] | None", payload.get("coach")) or {}
            cid = coach.get("coach_id")
            if cid is not None:
                coach_seeds.setdefault(str(cid), team_id)
        stats.tdl_coaches = len(coach_seeds)
    except srct.NightStop as stop:
        stats.stopped = stop.reason
        return stats

    # —— 相位 C：coach 两步（锚点种子 ∪ tdl 种子；履历在档即跳过） ——
    detail_dir = store.raw_dir(srct.SRCT_PROVIDER, srct.DETAIL_DATASET)
    if detail_dir.is_dir():
        anchors = harvest_coach_anchors(detail_dir)
        stats.anchor_coaches = len(anchors)
        for cid, entry in anchors.items():
            anchor_teams = cast("set[str]", entry["teams"])
            gb_hit = anchor_teams & gb_team_ids
            if not gb_hit:
                continue
            # 档案页 URL 的 team 段取任一 GB 锚点队（页面内容只随 cid 走）
            coach_seeds.setdefault(cid, sorted(gb_hit)[0])
        stats.anchor_gb_coaches = sum(
            1
            for entry in anchors.values()
            if cast("set[str]", entry["teams"]) & gb_team_ids
        )
        stats.anchor_pages = sum(1 for _ in detail_dir.glob("*.html.gz"))
    try:
        for cid, team_ctx in sorted(coach_seeds.items()):
            if store.has(srct.SRCT_PROVIDER, COACH_CAREER_DATASET, cid):
                continue
            page_key = f"{cid}@{team_ctx}"
            page_new = not store.has(srct.SRCT_PROVIDER, COACH_PAGE_DATASET, page_key)
            try:
                page_body = _get(
                    settings.srct_coach_page_url.format(team_id=team_ctx, cid=cid)
                ).content
                page = parse_coach_page(page_body)
            except srct.NightStop:
                raise
            except httpx.HTTPError as exc:
                if _is_not_found(exc):
                    stats.coaches_not_found += 1
                stats.failed[f"coach:{cid}"] = f"{type(exc).__name__}: {exc}"[:120]
                logger.warning("tenure coach {} 档案页失败（{}）", cid, exc)
                continue
            if page_new:
                _archive(
                    store,
                    COACH_PAGE_DATASET,
                    page_key,
                    page_body,
                    page,
                    stats,
                    ext=".html",
                    counter="coach_page_new",
                )
            career_cid = str(page.get("career_cid") or "")
            career_version = str(page.get("career_version") or "")
            if not career_cid or not career_version:
                stats.failed[f"coach:{cid}"] = "档案页无履历引用（改版或空档）"
                continue
            if store.has(srct.SRCT_PROVIDER, COACH_CAREER_DATASET, career_cid):
                continue
            career_url = (
                f"{settings.srct_coach_career_url.format(cid=career_cid)}"
                f"?version={career_version}"
            )
            fetched = _fetch_and_archive(
                store,
                COACH_CAREER_DATASET,
                career_cid,
                career_url,
                parse_coach_career,
                stats,
                "career_new",
                _get,
            )
            if fetched is not None and career_cid != cid:
                logger.info(
                    "tenure coach {} 档案页解析到 id {}（旧 id 已随页解析归档）",
                    cid,
                    career_cid,
                )
    except srct.NightStop as stop:
        stats.stopped = stop.reason
        return stats
    return stats


def stats_dict(stats: TenureStats) -> dict[str, object]:
    """统计 → 可 JSON 化 dict（flow 返回值/日志口径）。"""
    return asdict(stats)
