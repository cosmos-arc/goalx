#!/usr/bin/env python3
"""PROTOTYPE — 票 08 数据管道原型（throwaway，勿转生产代码）。

回答的问题：现有数据源（sporttery 竞彩 + The Odds API 欧赔 + football-data.co.uk 历史）
能否支撑双线预测与回测的最小闭环。

运行：python3 .scratch/goalx-quant/prototype/pipeline_prototype.py
产出：同目录 scratch_prototype.db（PROTOTYPE, wipe me）+ report.md
"""

from __future__ import annotations

import csv
import difflib
import io
import json
import sqlite3
import subprocess
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).parent
DB = HERE / "scratch_prototype.db"
REPORT = HERE / "report.md"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/126.0"
CST = timezone(timedelta(hours=8))  # 北京时间


def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    for line in (Path(__file__).resolve().parents[3] / ".env").read_text().splitlines():
        if line.strip() and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def http(url: str, headers: dict[str, str] | None = None) -> bytes:
    """统一走 curl（本机 python3.9 的 OpenSSL 与部分站点握手失败，curl 稳定）。"""
    cmd = ["curl", "-sL", "--max-time", "25", url, "-H", f"User-Agent: {UA}"]
    for k, v in (headers or {}).items():
        cmd += ["-H", f"{k}: {v}"]
    res = subprocess.run(cmd, capture_output=True, check=True)
    return res.stdout


# ---------- 1. 竞彩官方（sporttery 网关，须 Referer） ----------

def fetch_jingcai() -> list[dict]:
    raw = http(
        "https://webapi.sporttery.cn/gateway/jc/football/getMatchCalculatorV1.qry"
        "?poolCode=had,hhad&channel=c",
        {"Referer": "https://www.sporttery.cn/", "Accept": "application/json"},
    )
    data = json.loads(raw)["value"]["matchInfoList"]
    rows = []
    for day in data:
        for m in day["subMatchList"]:
            had, hhad = m.get("had") or {}, m.get("hhad") or {}
            kickoff_cst = datetime.strptime(
                f"{m['matchDate']} {m['matchTime']}", "%Y-%m-%d %H:%M:%S"
            ).replace(tzinfo=CST)
            rows.append({
                "match_num": m["matchNumStr"],
                "league": m["leagueAbbName"],
                "home": m["homeTeamAllName"],
                "away": m["awayTeamAllName"],
                "kickoff_utc": kickoff_cst.astimezone(timezone.utc).isoformat(),
                "single": m.get("bettingSingle"),
                "had_h": had.get("h"), "had_d": had.get("d"), "had_a": had.get("a"),
                "hhad_h": hhad.get("h"), "hhad_d": hhad.get("d"), "hhad_a": hhad.get("a"),
                "goal_line": hhad.get("goalLine"),
                "odds_updated_at": f"{had.get('updateDate','')}T{had.get('updateTime','')}",
                "vote": json.dumps(m.get("vote"), ensure_ascii=False) if m.get("vote") else None,
            })
    return rows


# ---------- 2. 欧赔（The Odds API；h2h outcome 的 name 是队名或 "Draw"） ----------

SPORT_KEYS = [
    "soccer_epl", "soccer_spain_la_liga", "soccer_italy_serie_a",
    "soccer_germany_bundesliga", "soccer_france_ligue_1",
    "soccer_uefa_champs_league", "soccer_uefa_europa_league",
]


def fetch_eu_odds(api_key: str) -> tuple[list[dict], int]:
    # 动态发现 sport key（命名会变，如欧联/欧协联 key 历年调整过）
    all_sports = json.loads(http(
        f"https://api.the-odds-api.com/v4/sports/?apiKey={api_key}"))
    wanted = ("soccer_epl", "soccer_spain", "soccer_italy", "soccer_germany",
              "soccer_france", "soccer_uefa_champ", "soccer_uefa_europa",
              "soccer_netherlands")
    keys = sorted(s["key"] for s in all_sports
                  if s["key"].startswith(wanted) and not s["key"].endswith("_winner"))
    print("      sport keys:", ", ".join(k.replace("soccer_", "") for k in keys))
    rows: list[dict] = []
    used = 0
    for sk in keys:
        url = (
            f"https://api.the-odds-api.com/v4/sports/{sk}/odds/"
            f"?apiKey={api_key}&regions=eu&markets=h2h&oddsFormat=decimal"
        )
        events = json.loads(http(url))
        used += 1  # 1 credit / (sport × region × market)
        for e in events:
            rows.append({
                "sport": sk,
                "home": e["home_team"],
                "away": e["away_team"],
                "kickoff_utc": e["commence_time"],
                "books": json.dumps([
                    {"book": b["key"],
                     "h2h": [o for mk in b["markets"] if mk["key"] == "h2h"
                             for o in [mk["outcomes"]]][0]}
                    for b in e["bookmakers"]
                ], ensure_ascii=False),
            })
    return rows, used


# ---------- 3. 历史（football-data.co.uk，Pinnacle 收盘列） ----------

def fetch_history() -> list[dict]:
    rows: list[dict] = []
    for season in ("2425", "2526"):
        raw = http(f"https://www.football-data.co.uk/mmz4281/{season}/E0.csv")
        for rec in csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))):
            rows.append({
                "season": season, "date": rec.get("Date"), "home": rec.get("HomeTeam"),
                "away": rec.get("AwayTeam"), "fthg": rec.get("FTHG"), "ftag": rec.get("FTAG"),
                "ftr": rec.get("FTR"),
                "ps_ch": rec.get("PSCH"), "ps_cd": rec.get("PSCD"), "ps_ca": rec.get("PSCA"),
                "avg_ch": rec.get("AvgCH"), "avg_cd": rec.get("AvgCD"), "avg_ca": rec.get("AvgCA"),
            })
    return rows


# ---------- 4. Join + 对照 ----------

def consensus_implied(books_json: str, home: str, away: str) -> tuple[float, float, float] | None:
    books = json.loads(books_json)
    if not books:
        return None
    odds = []
    for b in books:
        vals = {o["name"]: o["price"] for o in b["h2h"]}
        if home in vals and away in vals:
            d = vals.get("Draw")
            odds.append([vals[home], d if d else 3.2, vals[away]])
    if not odds:
        return None
    n = len(odds)
    avg = [sum(col) / n for col in zip(*odds)]
    overround = sum(1 / o for o in avg)
    return tuple(round((1 / o) / overround, 4) for o in avg)  # type: ignore[return-value]


def jc_implied(h: str | None, d: str | None, a: str | None):
    if not (h and d and a):
        return None
    h, d, a = float(h), float(d), float(a)
    overround = 1 / h + 1 / d + 1 / a
    return round((1 / h) / overround, 4), round((1 / d) / overround, 4), round((1 / a) / overround, 4)


# 竞彩联赛名 → Odds API sport key（联赛是跨语言 join 的消歧键）
LEAGUE_MAP = {
    "英超": "soccer_epl",
    "西甲": "soccer_spain_la_liga",
    "意甲": "soccer_italy_serie_a",
    "德甲": "soccer_germany_bundesliga",
    "法甲": "soccer_france_ligue_one",
    "荷甲": "soccer_netherlands_eredivisie",
    "欧冠": "soccer_uefa_champs_league",
    "欧联": "soccer_uefa_europa_league",
    "欧协联": "soccer_uefa_europa_conference_league",
    "法乙": "soccer_france_ligue_two",
    "西乙": "soccer_spain_segunda_division",
    "意乙": "soccer_italy_serie_b",
    "德乙": "soccer_germany_bundesliga2",
    "德国杯": "soccer_germany_dfb_pokal",
}


def name_sim(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio()


def join_matches(jc: list[dict], eu: list[dict]) -> tuple[list[dict], list[str]]:
    out, unmatched = [], []
    for j in jc:
        sport = LEAGUE_MAP.get(j["league"])
        if sport is None:
            unmatched.append(f"{j['match_num']} {j['league']}（无联赛映射，Tier 2 缺口）")
            continue
        jt = datetime.fromisoformat(j["kickoff_utc"])
        cands = [(abs((datetime.fromisoformat(e["kickoff_utc"].replace("Z", "+00:00")) - jt).total_seconds()), e)
                 for e in eu if e["sport"] == sport]
        cands = [(dt, e) for dt, e in cands if dt <= 20 * 60]
        if not cands:
            unmatched.append(f"{j['match_num']} {j['league']}（联赛有映射但时间窗内无事件）")
            continue
        cands.sort(key=lambda x: x[0])
        dt, best = cands[0]
        ambiguous = len(cands) > 1 and cands[1][0] == dt
        cons = consensus_implied(best["books"], best["home"], best["away"])
        jimp = jc_implied(j["had_h"], j["had_d"], j["had_a"])
        if not cons or not jimp:
            continue
        ev = [round(c * float(o) - 1, 4) for c, o in zip(cons, (j["had_h"], j["had_d"], j["had_a"]))]
        out.append({**j, "eu_home": best["home"], "eu_away": best["away"],
                    "books_n": len(json.loads(best["books"])),
                    "p_eu_h": cons[0], "p_eu_d": cons[1], "p_eu_a": cons[2],
                    "p_jc_h": jimp[0], "p_jc_d": jimp[1], "p_jc_a": jimp[2],
                    "ev_h": ev[0], "ev_d": ev[1], "ev_a": ev[2],
                    "ambiguous": ambiguous})
    return out, unmatched


# ---------- 5. 存储 + 报告 ----------

def store(jc: list[dict], eu: list[dict], hist: list[dict]) -> None:
    db = sqlite3.connect(DB)
    for table, rows in (("fixtures_jc", jc), ("odds_eu", eu), ("hist_fd", hist)):
        db.execute(f"DROP TABLE IF EXISTS {table}")
        cols = rows[0].keys()
        db.execute(f"CREATE TABLE {table} ({', '.join(cols)})")
        db.executemany(
            f"INSERT INTO {table} VALUES ({', '.join('?' * len(cols))})",
            [[str(r.get(c)) for c in cols] for r in rows],
        )
    db.commit()
    db.close()


def report(joined: list[dict], unmatched: list[str], hist: list[dict], credits_used: int, jc_n: int, eu_n: int) -> None:
    lines = [
        "# 票 08 数据管道原型报告（PROTOTYPE）", "",
        f"- 竞彩场次：{jc_n}（sporttery，含 had/hhad 快照与调盘时点）",
        f"- 欧赔事件：{eu_n}（The Odds API {credits_used} 个 sport key，eu 区 h2h）",
        f"- join 成功：{len(joined)}（联赛映射 + ±20 分钟开球窗口）",
        f"- 历史 CSV：{len(hist)} 行（E0 两个赛季，含 FTR 与 Pinnacle 收盘列）", "",
        "## 竞彩 vs 欧洲共识隐含概率（归一化近似；正式版用 Shin 去晦）", "",
        "| 时间(UTC) | 编号 | 联赛 | 主 vs 客 | 竞彩H/D/A | 欧均p H/D/A | EV H/D/A | books | 标记 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in sorted(joined, key=lambda x: x["kickoff_utc"]):
        flag = []
        if max(abs(r["ev_h"]), abs(r["ev_d"]), abs(r["ev_a"])) >= 0.05:
            flag.append("EV偏差≥5%")
        if r["ambiguous"]:
            flag.append("同联赛同时间歧义")
        if r["books_n"] < 3:
            flag.append(f"样本少({r['books_n']})")
        lines.append(
            "| {} | {} | {} | {} vs {} | {}/{}/{} | {:.1%}/{:.1%}/{:.1%} | {:+.1%}/{:+.1%}/{:+.1%} | {} | {} |".format(
                r["kickoff_utc"][5:16], r["match_num"], r["league"], r["home"], r["away"],
                r["had_h"], r["had_d"], r["had_a"],
                r["p_eu_h"], r["p_eu_d"], r["p_eu_a"],
                r["ev_h"], r["ev_d"], r["ev_a"], r["books_n"], "、".join(flag) or "—"))
    pin_ok = sum(1 for h in hist if h["ps_ch"])
    avg_ok = sum(1 for h in hist if h["avg_ch"])
    lines += ["", "## 历史样本检查", "",
              f"- hist_fd 行数：{len(hist)}；Pinnacle 收盘非空：{pin_ok} 行（{pin_ok / max(len(hist), 1):.0%}）；"
              f"市场均值收盘（AvgCH 等）非空：{avg_ok} 行（{avg_ok / max(len(hist), 1):.0%}）"
              "——缺 Pinnacle 行可用 AvgC 系列补",
              "", "## 未 join 的竞彩场次（Tier 2 覆盖缺口证据）", "",
              *(f"- {u}" for u in unmatched)]
    REPORT.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    env = load_env()
    print("[1/4] sporttery 竞彩……")
    jc = fetch_jingcai()
    print(f"      {len(jc)} 场（{jc[0]['match_num'] if jc else '-'} 起）")
    print("[2/4] The Odds API 欧赔……")
    eu, used = fetch_eu_odds(env["ODDS_API_KEY"])
    print(f"      {len(eu)} 事件，credits used={used}")
    print("[3/4] football-data.co.uk 历史……")
    hist = fetch_history()
    print(f"      {len(hist)} 行")
    joined, unmatched = join_matches(jc, eu)
    print(f"[4/4] join：{len(joined)} 场对上（未 join {len(unmatched)}）；写库+报告")
    store(jc, eu, hist)
    report(joined, unmatched, hist, used, len(jc), len(eu))
    print(f"完成：{DB}  +  {REPORT}")


if __name__ == "__main__":
    sys.exit(main())
