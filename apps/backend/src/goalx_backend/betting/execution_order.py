"""
执行单 schema 定稿（vision-build-20261110 票 12 交付 2）。

执行单=真金 γ 件（票 10）的纸面产物：live-real 策略版本 × 单关/串关/池面
候选 → 一张人照单投注的执行单；仓库无任何下单代码路径（spec US29/人裁
4），成交靠人照单、回执走 web 只读录入面（票 10，契约先行）。

本票只落 schema（票面定稿：先 schema 后建面，γ 时刻零等待）：

- pydantic 模型即代码（票 10 建表/建面直接 import 本模块）；
- 受控文档 ``docs/plans/execution-order-schema.md``（字段清单+评审签字节，
  状态=已定稿待人过目签字）；
- 不建表、不建 web 面、无生成/下单路径、零 API 契约改动（contract 无
  drift）。

字段（票面：场次/玩法/选项/金额/截止/策略版本 h_id 回指）见
``ExecutionLine``/``ExecutionOrder``。结构不变量（模型级强制）：

- 形状：单关恰 1 腿；串关 ≥2 腿且异场（每注腿必异场，与 bet_legs 同律）；
  池面须期次回指且腿无锁价（奖池奖金额由瓜分决定）；期次回指仅池面可有。
- 税感知（票 12 交付 1 同规则）：固定赔率面单票奖金=stake×联合赔率
  ≤¥1 万免税桶（越线全额 20%）；池面豁免——大奖越线口径=税后
  （``betting/tax.py`` 视图，H7）。
- 截止：固定赔率面截止须早于最早开球（kickoff 前置的下界）；池面截止
  =期次销售截止语义，不做开球比对。
- 口径钉死（correctness 批修）：时刻一律带时区（naive 拒——混比防
  TypeError 穿透）；锁价两位小数（竞彩报价口径，eps 1e-9 闭死与税视图
  的口径缝）；玩法/选项接 markets 单源闭集（闭集外拒收=结算纪律）。
- 回指：策略版本三元组+版本 id（票 01 状态机，生成时版本须 live-real）
  必填非空；h_id（票 02 假设注册表）可空——池面既有策略底注等无假设
  归属。

回执（成交价/部分成交/拒单因由）与三级对账是票 10 的录入面，不在本
schema（执行单是不可变纸面产物，回执是另行录入的事件）。
"""

from __future__ import annotations

import math
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any

from annotated_types import Gt
from pydantic import AfterValidator, BaseModel, Field, model_validator

from goalx_backend.betting.tax import TAX_FREE_THRESHOLD
from goalx_backend.markets import LEG_MARKETS, LEG_SELECTIONS
from goalx_backend.models import IsoTimestamp, MoneyCny

# 浮点联合赔率乘积的容差（correctness F3 收紧至 1e-9：2dp 赔率×2dp 注额
# 的连乘表示误差量级 ~1e-12，旧 1e-6 会放行 10000+5e-7 的口径缝——schema
# 放行而 tax 视图全额计税）
_ODDS_PRODUCT_EPS = 1e-9
# 串关最少腿数（现行引擎=2串1；schema 不封上界，多腿过关到来时直接可表达）
_MIN_PARLAY_LEGS = 2
# 盘口线承载玩法（hhad 唯一；markets 单源未建模线承载，此处钉死——与
# 引擎 _fixture_picks 的 hhad 整数线口径一致）
_GOAL_LINE_MARKETS = frozenset({"hhad"})
# 玩法枚举的 schema 附加面（dict[str, Any] 过 pydantic JsonDict 不变性）
_MARKET_ENUM_SCHEMA_EXTRA: dict[str, Any] = {"enum": sorted(LEG_MARKETS)}


def _check_tz_aware(value: str) -> str:
    """
    时区强制（correctness F2）：naive 时刻拒绝。

    IsoTimestamp 只验可解析；deadline×kickoff 混 naive/aware 会裸抛
    TypeError 穿透校验（票 10 web 面即 500）。schema 层从严：一律拒。
    """
    if datetime.fromisoformat(value).utcoffset() is None:
        raise ValueError(f"时刻 {value!r} 必须带时区（UTC ISO-8601，±hh:mm）")
    return value


def _check_two_decimal_places(value: float) -> float:
    """赔率两位小数校验（correctness F3）：竞彩报价 2dp 口径。"""
    if round(value, 2) != value:
        raise ValueError(f"赔率 {value} 须两位小数（竞彩报价口径）")
    return value


UtcIsoTimestamp = Annotated[IsoTimestamp, AfterValidator(_check_tz_aware)]
"""带时区强制的 ISO 时刻（naive 拒——混比防 TypeError 穿透）。"""

FixedOdds = Annotated[float, Gt(1.0), AfterValidator(_check_two_decimal_places)]
"""固定赔率锁价（decimal >1、两位小数）。"""


class OrderKind(StrEnum):
    """执行单类型：固定赔率（单关/串关）与奖池面（ttt14/pick9/goals4）。"""

    SINGLE = "single"
    PARLAY = "parlay"
    POOL = "pool"


class ExecutionLine(BaseModel):
    """执行单一条腿：场次×玩法×选项（人照单的最小投注单元）。"""

    fixture_id: int = Field(ge=1, description="库内场次 id（对账 join 键）")
    match_code: str = Field(min_length=1, description="JC 编号（周六001，人照单标识）")
    market_code: str = Field(
        # 枚举进 JSON schema（票 10 codegen 对照）；执行=下方闭集校验器，
        # 两处同读 LEG_MARKETS 同序——零漂移（动态 Literal 过不了 strict）
        json_schema_extra=_MARKET_ENUM_SCHEMA_EXTRA,
        description="玩法（markets.LEG_MARKETS 闭集；池面=ttt14/pick9/goals4）",
    )
    selection_code: str = Field(
        min_length=1, description="选项码（LEG_SELECTIONS 闭集）"
    )
    goal_line: float | None = Field(
        default=None, description="hhad 盘口线（prereg 整数线）"
    )
    kickoff_utc: UtcIsoTimestamp = Field(description="开球时刻（带时区 ISO-8601）")
    locked_odds: FixedOdds | None = Field(
        default=None,
        description="固定赔率面锁价（decimal >1、两位小数）；池面=None（奖池无锁价）",
    )

    @model_validator(mode="after")
    def _closed_sets_and_goal_line(self) -> ExecutionLine:
        """
        闭集校验（correctness F4）+盘口线必填。

        玩法/选项接 markets 单源（闭集外市场结算不收敛、闭集外选择静默
        记输——入口拒收=结算纪律）；hhad 判定用常量集非字面量比较。
        """
        if self.market_code not in LEG_MARKETS:
            raise ValueError(f"未知玩法 {self.market_code!r}（markets 闭集外拒收）")
        if self.selection_code not in LEG_SELECTIONS[self.market_code]:
            raise ValueError(
                f"玩法 {self.market_code} 选项 {self.selection_code!r} 不在闭集"
            )
        if self.market_code in _GOAL_LINE_MARKETS and self.goal_line is None:
            raise ValueError("hhad 腿必须带 goal_line（盘口线）")
        return self


class ExecutionOrder(BaseModel):
    """执行单（纸面产物；γ 生成器=票 10，本票只定 schema）。"""

    order_id: str = Field(
        min_length=1, description="幂等键（生成器决定，如 auto-v1:{date}:{anchor}:x:…）"
    )
    kind: OrderKind
    lines: list[ExecutionLine] = Field(min_length=1)
    stake: MoneyCny = Field(description="注额（元，分位精度；税感知 sizing 后）")
    deadline_utc: UtcIsoTimestamp = Field(
        description="截止（固定赔率=最早开球−前置；池面=期次销售截止）",
    )
    strategy_version_id: int = Field(
        ge=1, description="策略版本 id（票 01 状态机；生成时须 live-real）"
    )
    estimator_version: str = Field(min_length=1, description="估计器版本（三元组之一）")
    calibration_version: str = Field(min_length=1, description="校准版本（三元组之一）")
    params_version: str = Field(min_length=1, description="参数指纹（三元组之一）")
    h_id: str | None = Field(
        default=None,
        description=(
            "假设注册表 h_id 回指（票 02）；None=无假设归属（池面既有策略底注等）"
        ),
    )
    pool_period_id: int | None = Field(
        default=None, ge=1, description="彩池期次 id 回指（仅 kind=pool）"
    )
    generated_at: UtcIsoTimestamp = Field(description="生成时刻（带时区 ISO-8601）")

    @model_validator(mode="after")
    def _check_kind_shape(self) -> ExecutionOrder:
        """形状不变量：腿数/异场/期次回指按类型分治。"""
        if self.kind is not OrderKind.POOL and self.pool_period_id is not None:
            raise ValueError("期次回指（pool_period_id）仅池面执行单可有")
        if self.kind is OrderKind.SINGLE:
            if len(self.lines) != 1:
                raise ValueError("单关执行单恰 1 条腿")
        elif self.kind is OrderKind.PARLAY:
            if len(self.lines) < _MIN_PARLAY_LEGS:
                raise ValueError("串关执行单至少 2 条腿")
            if len({line.fixture_id for line in self.lines}) != len(self.lines):
                raise ValueError("串关执行单各腿必须异场（每注腿必异场）")
        else:  # POOL
            if self.pool_period_id is None:
                raise ValueError("池面执行单必须带期次回指（pool_period_id）")
            priced = [line for line in self.lines if line.locked_odds is not None]
            if priced:
                raise ValueError(f"池面执行单腿不得带锁价（{len(priced)} 条违规）")
        return self

    @model_validator(mode="after")
    def _check_tax_free_bucket(self) -> ExecutionOrder:
        """税感知不变量：固定赔率面奖金=stake×联合赔率 ≤¥1 万免税桶。"""
        if self.kind is OrderKind.POOL:
            return self  # 池面豁免：大奖越线口径=税后（tax 视图，H7）
        joint = self.joint_odds()
        if joint is None:
            raise ValueError("固定赔率执行单每腿必须带锁价")
        prize = self.stake * joint
        if prize > TAX_FREE_THRESHOLD + _ODDS_PRODUCT_EPS:
            raise ValueError(
                f"单票奖金 ¥{prize:.2f} 越 ¥{TAX_FREE_THRESHOLD:.0f} 免税线"
                + "（越线全额 20% 计税）——注额须压帽 floor(免税线/联合赔率)"
            )
        return self

    @model_validator(mode="after")
    def _check_deadline_before_kickoff(self) -> ExecutionOrder:
        """截止不变量：固定赔率面截止须早于最早开球（池面=期次截止语义，不比对）。"""
        if self.kind is OrderKind.POOL:
            return self
        deadline = datetime.fromisoformat(self.deadline_utc)
        earliest = min(datetime.fromisoformat(line.kickoff_utc) for line in self.lines)
        if deadline >= earliest:
            raise ValueError(
                f"截止 {self.deadline_utc} 须早于最早开球 {earliest.isoformat()}"
            )
        return self

    def joint_odds(self) -> float | None:
        """联合赔率（固定赔率面=腿锁价连乘；池面=None）。只读派生，非字段。"""
        if self.kind is OrderKind.POOL:
            return None
        odds_list: list[float] = []
        for line in self.lines:
            if line.locked_odds is None:  # pragma: no cover - 校验后不可达
                return None
            odds_list.append(float(line.locked_odds))
        return math.prod(odds_list)


def model_json_schema() -> dict[str, Any]:
    """两模型的 JSON Schema（受控文档再生/票 10 建面时对照用）。"""
    return {
        "ExecutionLine": ExecutionLine.model_json_schema(),
        "ExecutionOrder": ExecutionOrder.model_json_schema(),
    }
