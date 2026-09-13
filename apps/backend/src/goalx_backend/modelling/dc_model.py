"""
Dixon-Coles 分池训练器（票 26）：penaltyblog 拟合 + JSON 工件可复载。

- 拟合走 penaltyblog ``DixonColesGoalModel``（MIT，研究 03 首选；C 编译损失
  + 解析梯度），时间衰减权重 w = exp(−ξ·age_days)，ξ = ln2 / 半衰期（默认
  约 1 年起步，用户裁决 2026-09-13）。
- 预测不走 penaltyblog 对象：工件只存参数（JSON），复载后用
  ``score_matrix.matrix_from_lambdas`` 纯函数重建 10×10 矩阵——与
  penaltyblog ``predict`` 逐位一致（test_score_matrix 交叉验证），因此
  「工件复载后预测结果逐位一致」成立。
- 工件版本化：内容寻址文件名（数据指纹），参数 + 训练窗口 + ξ + 指纹齐备；
  bootstrap 重采样集合存进同一 run 文件（票 27 CI 用）。
"""

# penaltyblog 无 py.typed 存根，以下规则的第三方 unknown 在本文件放宽。
# pyright: reportMissingTypeStubs=false, reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from importlib import metadata
from pathlib import Path

from numpy import random as np_random
from penaltyblog.models import DixonColesGoalModel

from goalx_backend.data import results as rs_store
from goalx_backend.modelling.score_matrix import MATRIX_SIZE, ScoreMatrix

# Tier1 五大 fd 代码（票 26：五大主动；Tier2 如 N1 荷甲扩导列为可选项）
TIER1_COMPETITIONS = ("E0", "SP1", "I1", "D1", "F1")
DEFAULT_HALF_LIFE_DAYS = 365.0
ARTIFACT_SCHEMA_VERSION = 1
_MIN_TEAMS = 2


def implementation_versions() -> dict[str, str]:
    """实现与关键依赖版本（进工件身份；版本变化 ≠ 同一工件，票 34 验收 6）。"""
    versions = {"artifact_schema": str(ARTIFACT_SCHEMA_VERSION)}
    for dist in ("goalx-backend", "penaltyblog"):
        try:
            versions[dist] = metadata.version(dist)
        except metadata.PackageNotFoundError:  # 环境未装分发包时如实标注
            versions[dist] = "unknown"
    return versions


@dataclass(frozen=True)
class TrainingRow:
    """一行训练数据（hist_matches 投影）。"""

    match_date: str
    home_team: str
    away_team: str
    fthg: int
    ftag: int


def _rows_fingerprint(rows: list[TrainingRow]) -> str:
    """数据指纹：排序后的规范行序列化 sha256（防前视审计 + 内容寻址）。"""
    canonical = "\n".join(
        f"{r.match_date}|{r.home_team}|{r.away_team}|{r.fthg}:{r.ftag}"
        for r in sorted(rows, key=lambda r: (r.match_date, r.home_team, r.away_team))
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def decay_weights(
    rows: list[TrainingRow], *, as_of: date, half_life_days: float
) -> list[float]:
    """指数时间衰减权重（age 以 as_of 为基准，半衰期参数化）。"""
    xi = math.log(2.0) / half_life_days
    weights: list[float] = []
    for row in rows:
        age = (as_of - date.fromisoformat(row.match_date)).days
        weights.append(math.exp(-xi * age))
    return weights


def fit_dc_model(
    rows: list[TrainingRow],
    *,
    competition: str,
    half_life_days: float = DEFAULT_HALF_LIFE_DAYS,
    as_of: date | None = None,
) -> DCArtifact:
    """对一个联赛的训练池拟合时间衰减 DC；返回可序列化工件。"""
    if len(rows) < _MIN_TEAMS:
        raise ValueError(f"{competition} 训练样本不足: {len(rows)}")
    resolved_as_of = as_of or date.fromisoformat(max(r.match_date for r in rows))
    weights = decay_weights(rows, as_of=resolved_as_of, half_life_days=half_life_days)
    model = DixonColesGoalModel(
        [r.fthg for r in rows],
        [r.ftag for r in rows],
        [r.home_team for r in rows],
        [r.away_team for r in rows],
        weights=weights,
    )
    model.fit()
    params: dict[str, float] = dict(model.params)
    teams = sorted(
        name.removeprefix("attack_") for name in params if name.startswith("attack_")
    )
    return DCArtifact(
        competition=competition,
        train_window_start=min(r.match_date for r in rows),
        train_window_end=max(r.match_date for r in rows),
        as_of=resolved_as_of.isoformat(),
        n_matches=len(rows),
        half_life_days=half_life_days,
        teams={
            team: {
                "attack": float(params[f"attack_{team}"]),
                "defence": float(params[f"defence_{team}"]),
            }
            for team in teams
        },
        home_advantage=float(params["home_advantage"]),
        rho=float(params["rho"]),
        data_fingerprint=_rows_fingerprint(rows),
        loglikelihood=float(model.loglikelihood) if model.loglikelihood else None,
        fitted_at=datetime.now(tz=None).isoformat(timespec="seconds"),
    )


@dataclass
class DCArtifact:
    """一次拟合的版本化工件（参数 + 训练窗口 + 数据指纹）。"""

    competition: str
    train_window_start: str
    train_window_end: str
    as_of: str
    n_matches: int
    half_life_days: float
    teams: dict[str, dict[str, float]]
    home_advantage: float
    rho: float
    data_fingerprint: str
    loglikelihood: float | None = None
    fitted_at: str = ""

    def lambdas(self, home_team: str, away_team: str) -> tuple[float, float]:
        """两队期望进球（λ_home, λ_away）；未训练球队抛 KeyError。"""
        for team in (home_team, away_team):
            if team not in self.teams:
                raise KeyError(f"{team} 不在 {self.competition} 训练池")
        home = self.teams[home_team]
        away = self.teams[away_team]
        lam_home = math.exp(self.home_advantage + home["attack"] + away["defence"])
        lam_away = math.exp(away["attack"] + home["defence"])
        return lam_home, lam_away

    def predict(self, home_team: str, away_team: str) -> ScoreMatrix:
        """从工件参数重建 10×10 canonical 矩阵（纯函数，逐位可复现）。"""
        lam_home, lam_away = self.lambdas(home_team, away_team)
        return ScoreMatrix.from_lambdas(
            lam_home, lam_away, rho=self.rho, size=MATRIX_SIZE
        )


@dataclass
class TrainingRun:
    """一个联赛一次训练的落盘单元：基准工件 + bootstrap 集合（票 27 CI）。"""

    base: DCArtifact
    bootstrap: list[DCArtifact] = field(default_factory=list)
    seed: int = 0

    def artifact_id(self) -> str:
        """
        工件身份 = 数据指纹 + 训练配置(half-life) + seed + 实现/依赖版本。

        票 34 验收 6：同数据不同 half-life/seed/实现版本必须生成不同身份；
        身份决定文件名（内容寻址、不互相覆盖，旧工件可追溯）。
        """
        canonical = json.dumps(
            {
                "data_fingerprint": self.base.data_fingerprint,
                "half_life_days": self.base.half_life_days,
                "seed": self.seed,
                "versions": implementation_versions(),
            },
            sort_keys=True,
        )
        return hashlib.sha256(canonical.encode()).hexdigest()

    def to_payload(self) -> dict[str, object]:
        """Serialize to the on-disk JSON payload shape."""
        return {
            "schema_version": ARTIFACT_SCHEMA_VERSION,
            "artifact_id": self.artifact_id(),
            "base": asdict(self.base),
            "bootstrap": [asdict(boot) for boot in self.bootstrap],
            "seed": self.seed,
            "versions": implementation_versions(),
        }

    @classmethod
    def from_payload(cls, payload: dict[str, object]) -> TrainingRun:
        """Rebuild a run from its on-disk JSON payload."""
        base = DCArtifact(**payload["base"])  # type: ignore[arg-type]
        boots = [DCArtifact(**item) for item in payload["bootstrap"]]  # type: ignore[arg-type]
        seed = int(str(payload.get("seed", 0)))
        return cls(base=base, bootstrap=boots, seed=seed)


def bootstrap_dc_models(
    rows: list[TrainingRow],
    *,
    competition: str,
    half_life_days: float = DEFAULT_HALF_LIFE_DAYS,
    as_of: date | None = None,
    n_boot: int = 50,
    seed: int = 0,
) -> list[DCArtifact]:
    """行级重采样 bootstrap（同种子可复现）；个别重采样拟合失败跳过。"""
    rng = np_random.default_rng(seed)
    artifacts: list[DCArtifact] = []
    for _ in range(n_boot):
        indices: list[int] = [int(i) for i in rng.integers(0, len(rows), len(rows))]
        sampled = [rows[i] for i in indices]
        try:
            artifacts.append(
                fit_dc_model(
                    sampled,
                    competition=competition,
                    half_life_days=half_life_days,
                    as_of=as_of,
                )
            )
        except ValueError:
            continue
    return artifacts


# --- 工件目录（内容寻址 + latest 解析） ---


def run_filename(run: TrainingRun) -> str:
    """内容寻址文件名：工件身份（数据+配置+seed+版本）决定，不同配置不覆盖。"""
    return f"{run.base.competition}-{run.artifact_id()[:16]}.json"


def save_run(run: TrainingRun, models_dir: str | Path) -> Path:
    """把训练 run 落盘（JSON，幂等覆盖同指纹文件）；返回文件路径。"""
    directory = Path(models_dir) / run.base.competition
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / run_filename(run)
    path.write_text(
        json.dumps(run.to_payload(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


def list_runs(
    models_dir: str | Path, competition: str
) -> list[tuple[TrainingRun, Path]]:
    """一个联赛的全部训练 run，按训练窗口上界排序（旧→新）。"""
    directory = Path(models_dir) / competition
    if not directory.exists():
        return []
    runs: list[tuple[TrainingRun, Path]] = []
    for path in sorted(directory.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        runs.append((TrainingRun.from_payload(payload), path))
    runs.sort(key=lambda item: (item[0].base.train_window_end, item[0].base.fitted_at))
    return runs


def load_latest_run(models_dir: str | Path, competition: str) -> TrainingRun | None:
    """最新（训练窗口最晚）的训练 run；无工件返回 None。"""
    runs = list_runs(models_dir, competition)
    return runs[-1][0] if runs else None


# --- 从 hist_matches 取训练数据 ---


def training_rows_for(
    conn: sqlite3.Connection, competition: str, *, as_of: str
) -> list[TrainingRow]:
    """某联赛 as_of（含）之前的全部 hist 行（防前视：上界由调用方给出）。"""
    return [
        TrainingRow(
            match_date=str(row["match_date"]),
            home_team=str(row["home_team"]),
            away_team=str(row["away_team"]),
            fthg=int(row["fthg"]),
            ftag=int(row["ftag"]),
        )
        for row in rs_store.hist_rows_through(conn, competition, as_of)
    ]


def train_competition(
    conn: sqlite3.Connection,
    competition: str,
    *,
    models_dir: str | Path,
    half_life_days: float = DEFAULT_HALF_LIFE_DAYS,
    as_of: str | None = None,
    n_boot: int = 0,
    seed: int = 0,
) -> TrainingRun:
    """训练一个联赛并落盘（基准 + 可选 bootstrap）。"""
    resolved_as_of = as_of or rs_store.latest_hist_date(conn, competition)
    rows = training_rows_for(conn, competition, as_of=str(resolved_as_of))
    base = fit_dc_model(rows, competition=competition, half_life_days=half_life_days)
    as_of_date = date.fromisoformat(str(resolved_as_of))
    boots = (
        bootstrap_dc_models(
            rows,
            competition=competition,
            half_life_days=half_life_days,
            as_of=as_of_date,
            n_boot=n_boot,
            seed=seed,
        )
        if n_boot
        else []
    )
    run = TrainingRun(base=base, bootstrap=boots, seed=seed)
    save_run(run, models_dir)
    return run
