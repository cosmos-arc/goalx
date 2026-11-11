"""
六轴联结读缝 + 搜索窗特征重要性读数（mining-loop 票 06；LdP 中间层首产物）。

联结读缝=match-level as-of 特征联结（只读，不建新表）：hist_matches 为
锚（EM 扫描同空间），教练侧经 ``resolve_club_hist_names`` 逆（hist 英文名
→ TM club）→ tm_coach_tenures as-of 开球日指派（date_from/to 重叠消歧=
assign_manager_day 语义）；底蕴侧 club_pedigree 按 (hist 队名, season) 直键
（fd 面键空间同源）；裁判列直读 + 生涯 as-of 先验；阵型族经票 05 回填面
的 ``coach_family_prior`` 读缝（coach×season 键，不含当场面）。

三窗纪律（EM facet E-32 同界，hist 季码）：搜索 1617–2122 自由读（本模块
唯一消费窗）/ 验证 2223–2324 / 确认 2425–2526 一次一读 / 2627 禁入。特征
构造全部后视（as-of-season / match_date 严格早于 / last_match_date <
on_day），embargo=窗末特征不跨入下一窗首季——由单测钉（窗界后行不改变
窗内读数）。

**中间层只产读数不产结论**：特征重要性/分层读数是样本内描述统计，不做
假设检验结论、不回测、不进发现门；假设注册恒人审（票 07）。2026-11-11
映射率人裁决项触发实测：教练双侧 62.3%（E0/E1 搜索窗）——根因=TM 任期
宇宙现役快照（历史降级队无任期行，非别名可修），未解析侧特征 NULL 按
特征各自分母诚实缺。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import cast

from goalx_backend.data import results as rs_store
from goalx_backend.data.coach_changes import (
    Tenure,
    assign_manager_day,
    resolve_club_hist_names,
    tenures_by_club,
)
from goalx_backend.data.formations import coach_family_prior
from goalx_backend.data.pedigree import pedigree_by_team_season

#: 搜索窗（特征读数唯一消费窗；E-32 EM 三窗同界，hist 季码）
SEARCH_WINDOW: tuple[str, str] = ("1617", "2122")
#: 验证/确认窗（本模块不读——窗口清单单点注册，票 07 注册时消费）
VALIDATE_WINDOW: tuple[str, str] = ("2223", "2324")
CONFIRM_WINDOW: tuple[str, str] = ("2425", "2526")
#: 首批联赛域=E0+E1 切片（设计 §1 英伦域的裁判覆盖子集：英伦三联赛中
#: 仅 E0/E1 裁判近满覆，SC0 阵型/底蕴面薄——二批扩）
FIRST_BATCH_COMPETITIONS: tuple[str, ...] = ("E0", "E1")

#: run_params 留痕（可复算口径；报告与观测 payload 同源携带）
RUN_PARAMS: dict[str, object] = {
    "window": list(SEARCH_WINDOW),
    "competitions": list(FIRST_BATCH_COMPETITIONS),
    "implied": "psc Pinnacle 收盘原始隐含（未除水）",
    "side_residual": "1{该侧胜} − 1/psc_该侧",
    "importance": "η²（相关比，四分位/类别分层）",
    "referee_prior": "同裁判 E0/E1 内 match_date 严格早于当场的逐场先验",
}


def facet_season(hist_season: str) -> str:
    """季码折算：hist '1617' → facet '2016-17'（8 月界同源）。"""
    return f"20{hist_season[:2]}-{hist_season[2:]}"


@dataclass
class SideReading:
    """一侧的 as-of 特征（全可缺：未解析=NULL，诚实缺）。"""

    team: str
    residual: float | None = None  # 1{该侧胜} − 1/psc_该侧（psc 缺=None）
    club_tm_id: int | None = None
    coach_name: str | None = None
    coach_role: str | None = None
    tenure_days: int | None = None
    top_flight_streak: int | None = None
    points_pct_l5: float | None = None
    tier1_titles: int | None = None
    major_honours: int | None = None
    family: str | None = None
    family_n_known: int = 0


@dataclass
class MatchReading:
    """一场的联结读数（embargo/PIT：全部特征 as-of 开球前可知）。"""

    match_id: int
    season: str
    competition: str
    match_date: str
    home: SideReading = field(default_factory=lambda: SideReading(team=""))
    away: SideReading = field(default_factory=lambda: SideReading(team=""))
    referee: str | None = None
    referee_prior_n: int = 0
    referee_prior_avg_goals: float | None = None
    total_goals: int | None = None


@dataclass
class _Seam:
    """读缝装配上下文（一次构建全场复用：身份逆表/任期/底蕴）。"""

    conn: sqlite3.Connection
    club_of_name: dict[str, int]
    tenures: dict[int, list[Tenure]]
    pedigree: dict[tuple[str, str], sqlite3.Row]


def _side_features(
    seam: _Seam,
    team: str,
    won: bool,
    implied: float | None,
    on_day: str,
    hist_season: str,
) -> SideReading:
    """一侧 as-of 特征装配（身份/任期/底蕴任一环缺=该段 NULL，不硬配）。"""
    side = SideReading(
        team=team,
        residual=(1.0 if won else 0.0) - implied if implied is not None else None,
    )
    club = seam.club_of_name.get(team)
    if club is None:
        return side
    side.club_tm_id = club
    ped = seam.pedigree.get((team, hist_season))
    if ped is not None:
        for column in ("top_flight_streak", "tier1_titles", "major_honours"):
            if ped[column] is not None:
                setattr(side, column, int(ped[column]))
        if ped["points_pct_l5"] is not None:
            side.points_pct_l5 = float(ped["points_pct_l5"])
    club_tenures = seam.tenures.get(club, [])
    assigned = assign_manager_day(club_tenures, on_day)
    if assigned is None:
        return side
    coach, role = assigned
    side.coach_name, side.coach_role = coach, role
    # 任期天数=覆盖当日的任职行（date_from ≤ 当日 ≤ date_to）里最晚上任者
    covering = [
        t.date_from
        for t in club_tenures
        if t.coach_name == coach
        and t.role == role
        and t.date_from <= on_day
        and (t.date_to is None or on_day <= t.date_to)
    ]
    if covering:
        started = max(covering)
        side.tenure_days = (
            date.fromisoformat(on_day) - date.fromisoformat(started)
        ).days
    prior = coach_family_prior(
        seam.conn, coach, upto_season=facet_season(hist_season), on_day=on_day
    )
    dominant = prior.get("dominant_family")
    side.family = str(dominant) if dominant else None
    n_known = prior.get("n_known")
    side.family_n_known = n_known if isinstance(n_known, int) else 0
    return side


def match_readings(
    conn: sqlite3.Connection,
    *,
    season_from: str = SEARCH_WINDOW[0],
    season_to: str = SEARCH_WINDOW[1],
    competitions: tuple[str, ...] = FIRST_BATCH_COMPETITIONS,
) -> list[MatchReading]:
    """
    联结读缝主读口（只读）：窗内逐场 as-of 特征（PIT/embargo 见模块注释）。

    referee 先验=同裁判在该场之前（match_date **严格早于**，同日多场互不
    先验）E0/E1 已完赛逐场累计——生涯面受裁判覆盖域约束（英伦近满覆）。
    匹配序=match_date 升序；同日按日缓冲，当日全部装配完才入账。
    """
    seam = _Seam(
        conn=conn,
        club_of_name={
            hist_name: club for club, hist_name in resolve_club_hist_names(conn).items()
        },
        tenures=tenures_by_club(conn),
        pedigree=pedigree_by_team_season(conn),
    )
    matches = rs_store.hist_rows_in_season_window(
        conn,
        season_from=season_from,
        season_to=season_to,
        competitions=competitions,
    )
    ref_goals: dict[str, list[int]] = {}
    day_buffer: dict[str, list[tuple[str, int]]] = {}
    readings: list[MatchReading] = []
    prev_date = ""
    for row in matches:
        mdate = str(row["match_date"])
        if mdate != prev_date:
            # 日界翻转：上一日的裁判进球入账（同日互不可见）
            for referee, goals in day_buffer.pop(prev_date, []):
                ref_goals.setdefault(referee, []).append(goals)
            prev_date = mdate
        season = str(row["season"])
        referee = str(row["referee"]) if row["referee"] not in (None, "") else None
        hg, ag = int(row["fthg"]), int(row["ftag"])
        reading = MatchReading(
            match_id=int(row["id"]),
            season=season,
            competition=str(row["competition"]),
            match_date=mdate,
            referee=referee,
            total_goals=hg + ag,
        )
        if referee is not None:
            seen = ref_goals.get(referee, [])
            reading.referee_prior_n = len(seen)
            if seen:
                reading.referee_prior_avg_goals = round(sum(seen) / len(seen), 6)
            day_buffer.setdefault(mdate, []).append((referee, hg + ag))
        psc_home = float(row["psc_home"]) if row["psc_home"] else None
        psc_away = float(row["psc_away"]) if row["psc_away"] else None
        reading.home = _side_features(
            seam,
            str(row["home_team"]),
            hg > ag,
            1 / psc_home if psc_home else None,
            mdate,
            season,
        )
        reading.away = _side_features(
            seam,
            str(row["away_team"]),
            ag > hg,
            1 / psc_away if psc_away else None,
            mdate,
            season,
        )
        readings.append(reading)
    return readings


def linkage_report(readings: list[MatchReading]) -> dict[str, object]:
    """逐面联结映射率（票 06 首件活；人裁门槛 95% 的实测数字）。"""
    n = len(readings)
    if n == 0:
        return {"matches": 0}
    coach_h = sum(1 for r in readings if r.home.coach_name is not None)
    coach_a = sum(1 for r in readings if r.away.coach_name is not None)
    return {
        "matches": n,
        "coach_home": round(coach_h / n, 4),
        "coach_away": round(coach_a / n, 4),
        "coach_both_sides": round(
            sum(
                1
                for r in readings
                if r.home.coach_name is not None and r.away.coach_name is not None
            )
            / n,
            4,
        ),
        "coach_any_side": round(
            sum(1 for r in readings if r.home.coach_name or r.away.coach_name) / n, 4
        ),
        "pedigree_home": round(
            sum(1 for r in readings if r.home.points_pct_l5 is not None) / n, 4
        ),
        "pedigree_away": round(
            sum(1 for r in readings if r.away.points_pct_l5 is not None) / n, 4
        ),
        "referee": round(sum(1 for r in readings if r.referee is not None) / n, 4),
        "closing_anchor": round(
            sum(1 for r in readings if r.home.residual is not None) / n, 4
        ),
        "formation_prior": round(
            sum(1 for r in readings if r.home.family or r.away.family) / n, 4
        ),
    }


#: η² 最小可算样本（两组两值；不足=None 不硬算）
_ETA_MIN_SAMPLES = 2


def _eta_sq(groups: list[list[float]]) -> float | None:
    """η²（相关比）：组间方差份额；样本不足/零方差=None。"""
    values = [v for group in groups for v in group]
    if len(values) < _ETA_MIN_SAMPLES or len(groups) < _ETA_MIN_SAMPLES:
        return None
    mean = sum(values) / len(values)
    total = sum((v - mean) ** 2 for v in values)
    if total <= 0:
        return None
    between = sum(
        len(group) * (sum(group) / len(group) - mean) ** 2 for group in groups if group
    )
    return round(between / total, 6)


def _stratum_summary(label: str, residuals: list[float]) -> dict[str, object]:
    mean = sum(residuals) / len(residuals)
    var = sum((r - mean) ** 2 for r in residuals) / max(len(residuals) - 1, 1)
    return {
        "stratum": label,
        "n": len(residuals),
        "mean": round(mean, 6),
        "se": round((var**0.5) / (len(residuals) ** 0.5), 6),
    }


def _quartiles(pairs: list[tuple[float, float]]) -> list[dict[str, object]]:
    """连续特征四分位分层读数：[(特征值, 读数)] 升序切四段。"""
    ordered = sorted(pairs, key=lambda p: p[0])
    n = len(ordered)
    out: list[dict[str, object]] = []
    for q in range(4):
        chunk = ordered[q * n // 4 : (q + 1) * n // 4]
        if not chunk:
            continue
        summary = _stratum_summary(f"q{q + 1}", [r for _, r in chunk])
        summary |= {"lo": round(chunk[0][0], 4), "hi": round(chunk[-1][0], 4)}
        out.append(summary)
    return out


#: 侧级连续特征（读缝字段名 → 报告名）
_SIDE_CONTINUOUS: tuple[tuple[str, str], ...] = (
    ("coach_tenure_days", "tenure_days"),
    ("pedigree_top_flight_streak", "top_flight_streak"),
    ("pedigree_points_pct_l5", "points_pct_l5"),
    ("pedigree_tier1_titles", "tier1_titles"),
    ("pedigree_major_honours", "major_honours"),
)


def _quartile_groups(pairs: list[tuple[float, float]]) -> list[list[float]]:
    """按特征值升序四分位切组（η² 与分层读数共口径，各出现一次）。"""
    ordered = sorted(pairs, key=lambda p: p[0])
    n = len(ordered)
    return [
        [value for _, value in ordered[q * n // 4 : (q + 1) * n // 4]] for q in range(4)
    ]


def feature_importance(readings: list[MatchReading]) -> dict[str, object]:
    """
    搜索窗特征重要性（LdP 中间层：样本内描述统计，不出结论不回测）。

    侧级连续特征（特征 ↔ 该侧收盘残差）与阵型族（类别）四分位/逐族
    分层 + η²；裁判面场级（生涯场均进球 ↔ 总进球，无 OU 锚读原始值）。
    """
    sides = [(side, r) for r in readings for side in (r.home, r.away)]
    features: dict[str, object] = {}
    for name, attr in _SIDE_CONTINUOUS:
        pairs = [
            (float(getattr(side, attr)), side.residual)
            for side, _ in sides
            if getattr(side, attr) is not None and side.residual is not None
        ]
        features[name] = {
            "n": len(pairs),
            "eta_sq": _eta_sq(_quartile_groups(pairs)),
            "strata": _quartiles(pairs),
        }
    family_pairs = [
        (str(side.family), float(side.residual))
        for side, _ in sides
        if side.family is not None and side.residual is not None
    ]
    by_family: dict[str, list[float]] = {}
    for fam, res in family_pairs:
        by_family.setdefault(fam, []).append(res)
    features["coach_dominant_family"] = {
        "n": len(family_pairs),
        "eta_sq": _eta_sq([res for res in by_family.values() if len(res) > 1]),
        "strata": [
            _stratum_summary(fam, res) for fam, res in sorted(by_family.items())
        ],
    }
    ref_pairs = [
        (float(r.referee_prior_avg_goals), float(r.total_goals))
        for r in readings
        if r.referee_prior_avg_goals is not None and r.total_goals is not None
    ]
    features["referee_prior_avg_goals_vs_total_goals"] = {
        "n": len(ref_pairs),
        "eta_sq": _eta_sq(_quartile_groups(ref_pairs)),
        "strata": _quartiles(ref_pairs),
    }
    return features


def search_window_reading(conn: sqlite3.Connection) -> dict[str, object]:
    """S7 观测 payload 装配（读数+映射率+run_params 一体留痕）。"""
    readings = match_readings(conn)
    return {
        "run_params": dict(RUN_PARAMS),
        "linkage": linkage_report(readings),
        "features": feature_importance(readings),
    }


REPORT_BASENAME = "six-axes-search-window"


def latest_observation(conn: sqlite3.Connection) -> dict[str, object] | None:
    """最新 s7 观测 payload（窗冻结 → 恒为搜索窗那份；缺席=None）。"""
    row = conn.execute(
        """
        SELECT payload FROM edge_observations
        WHERE scanner = 's7' ORDER BY window_key DESC LIMIT 1
        """
    ).fetchone()
    return json.loads(str(row[0])) if row is not None else None


def write_search_report(conn: sqlite3.Connection, root: Path) -> Path:
    """
    报告落档（可复算：run_params 随 payload 一体写盘），返回 MD 落盘路径。

    读最新 s7 观测写语料树 reports/ 双视图（JSON 全量 + MD 人读）。
    观测缺席=从未跑过 s7 → fail-loud 不造空报告。
    """
    row = conn.execute(
        """
        SELECT payload, created_at FROM edge_observations
        WHERE scanner = 's7' ORDER BY window_key DESC LIMIT 1
        """
    ).fetchone()
    if row is None:
        msg = "s7 观测缺席：先跑 edge-scan --scanner s7 再落档"
        raise RuntimeError(msg)
    payload: dict[str, object] = json.loads(str(row["payload"]))
    payload["generated_at"] = str(row["created_at"])
    reports_dir = root / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    (reports_dir / f"{REPORT_BASENAME}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    features = cast("dict[str, dict[str, object]]", payload["features"])
    lines = [
        "# 六轴搜索窗特征重要性首报告（S7，mining-loop 票 06）",
        "",
        "LdP 中间层：样本内描述统计，不出结论不回测。假设注册恒人审（票 07）。",
        "",
        f"- 观测时点：{payload['generated_at']}",
        f"- run_params：`{json.dumps(payload['run_params'], ensure_ascii=False)}`",
        f"- 联结映射率：`{json.dumps(payload['linkage'], ensure_ascii=False)}`",
        "",
        "## 特征面（η² + 分层读数）",
        "",
    ]
    for name, face in features.items():
        strata = cast("list[dict[str, object]]", face["strata"])
        lines.append(f"### {name}（n={face['n']}，η²={face['eta_sq']}）")
        lines.append("")
        lines.append("| 分层 | n | mean | se | 值域 |")
        lines.append("|---|---|---|---|---|")
        for stratum in strata:
            lo_hi = f"[{stratum['lo']}, {stratum['hi']}]" if "lo" in stratum else "—"
            row = (
                f"| {stratum['stratum']} | {stratum['n']} | {stratum['mean']}"
                + f" | {stratum['se']} | {lo_hi} |"
            )
            lines.append(row)
        lines.append("")
    md_path = reports_dir / f"{REPORT_BASENAME}.md"
    md_path.write_text("\n".join(lines), encoding="utf-8")
    return md_path
