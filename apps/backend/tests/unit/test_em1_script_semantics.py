"""EM-1 研究脚本语义修正单测（edge-audit-20261109 票 12 + 票 20）。

外部行为断言（外部审计 @032324c 合成反例转正 + E-32 修订 C 落地）：
1. ttg「7+」档按 ≥7 结算：总进球 8/9 时 over 篮 7+ 腿必须回款；
2. OU 梯锚=每书最晚合格赛前报价→跨书均值（非最后更新者），带书覆盖与年龄；
3. BH 校正家族=当轮全部候选（含全阴），先筛后校路径不可再现；
4. 轮级单家族（票 20/E-32 修订 C）：一轮 EM 全 facet 候选并成一个家族过
   一次 BH，族成员从试验账装载，轮不完整 fail-closed。

被测面=docs/plans/autoresearch-20261107/reports/_lib.py 的纯函数接缝
（产品结算面不在本票范围，此处只钉研究脚本语义）。
"""

from __future__ import annotations

import csv
import datetime as dt
import importlib.util
import math
import sys
from pathlib import Path
from statistics import NormalDist

import pytest

_REPO = Path(__file__).resolve().parents[4]
_LIB_PATH = _REPO / "docs/plans/autoresearch-20261107/reports/_lib.py"


def _load_lib():
    spec = importlib.util.spec_from_file_location("em1_reports_lib", _LIB_PATH)
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    # dataclass 处理需要模块已注册进 sys.modules（否则 __module__ 查不到）
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


lib = _load_lib()

# 外部审计反例赔率表：{3、4、5、6、7+} 等注五腿，7+ 赔 30
TTG_ODDS = (9.5, 9.0, 8.5, 8.0, 7.5, 7.0, 6.5, 30.0)
OVER_KS = list(range(3, 8))
UNDER_KS = list(range(0, 3))


# ---------------------------------------------------------------- 1. ttg 7+ 结算


class TestTtgSevenPlusSettlement:
    def test_total_goals_8_pays_seven_plus_leg(self):
        # 审计反例：7+ 赔 30、总进球 8 → 等注五腿收益率 +500%（原 t==k 读 −100%）
        assert lib.ttg_basket_ev(TTG_ODDS, 8, OVER_KS) == pytest.approx(30.0 / 5 - 1.0)

    def test_total_goals_9_pays_seven_plus_leg(self):
        assert lib.ttg_basket_ev(TTG_ODDS, 9, OVER_KS) == pytest.approx(30.0 / 5 - 1.0)

    def test_total_goals_7_exact_hit_unchanged(self):
        assert lib.ttg_basket_ev(TTG_ODDS, 7, OVER_KS) == pytest.approx(30.0 / 5 - 1.0)

    def test_exact_tiers_still_require_equality(self):
        # 0–6 档维持恰好命中：总进球 6 只回 s6 腿
        assert lib.ttg_basket_ev(TTG_ODDS, 6, OVER_KS) == pytest.approx(6.5 / 5 - 1.0)
        # under 篮（s0..s2）总进球 8 全落空 → −100%（与 7+ 修正无关的方向不变）
        assert lib.ttg_basket_ev(TTG_ODDS, 8, UNDER_KS) == pytest.approx(-1.0)
        assert lib.ttg_basket_ev(TTG_ODDS, 1, UNDER_KS) == pytest.approx(9.0 / 3 - 1.0)

    def test_hit_predicate_semantics(self):
        assert lib.ttg_hit(7, 8)
        assert lib.ttg_hit(7, 12)
        assert not lib.ttg_hit(6, 8)
        assert lib.ttg_hit(6, 6)
        assert not lib.ttg_hit(6, 7)


# ------------------------------------------------- 2. OU 梯锚（每书末价→跨书均值）


def _at(hour: int, minute: int = 0) -> dt.datetime:
    return dt.datetime(2025, 5, 1, hour, minute, tzinfo=dt.UTC)


def _kickoff() -> dt.datetime:
    return dt.datetime(2025, 5, 1, 18, 0, tzinfo=dt.UTC)


def _pin_devig(hw: float, aw: float) -> float:
    d_o, d_u = 1.0 + hw, 1.0 + aw
    return (1.0 / d_o) / (1.0 / d_o + 1.0 / d_u)


def _row(book: str, line: float, t: dt.datetime, hw: float, aw: float):
    return ("s1", book, line, t, _kickoff(), hw, aw)


class TestOuLadderPerBookAnchor:
    def test_mean_of_per_book_last_quotes_not_last_updater(self):
        # 书 A 两时刻（末价 9 点）；书 B 两时刻（末价 10 点=全局最晚）——
        # 旧 arg_max 口径拿到 B 一家，新口径=两家末价均值
        rows = [
            _row("srct:ou:14", 2.5, _at(8), 0.90, 0.95),
            _row("srct:ou:14", 2.5, _at(9), 0.85, 0.90),
            _row("srct:ou:42", 2.5, _at(7), 0.80, 0.85),
            _row("srct:ou:42", 2.5, _at(10), 0.95, 0.90),
        ]
        rung = lib.aggregate_ou_ladder(rows)["s1"][2.5]
        pa, pb = _pin_devig(0.85, 0.90), _pin_devig(0.95, 0.90)
        assert rung.p_over == pytest.approx((pa + pb) / 2)
        assert rung.p_over != pytest.approx(pb)  # 不是最后更新者（B）一家的水位
        assert rung.p_over != pytest.approx(pa)  # 也不是被覆盖掉的 A 中间时刻

    def test_rung_carries_book_coverage_and_quote_age(self):
        rows = [
            _row("srct:ou:14", 2.5, _at(8), 0.85, 0.90),
            _row("srct:ou:42", 2.5, _at(12), 0.95, 0.90),
            # 单书线位：书覆盖=1，年龄只算这一家
            _row("srct:ou:8", 0.5, _at(6), 0.70, 1.20),
        ]
        lad = lib.aggregate_ou_ladder(rows)
        rung25 = lad["s1"][2.5]
        assert rung25.n_books == 2
        # 年龄=距开球最陈旧书的报价年龄（A 10h vs B 6h → 10h）
        assert rung25.age_h == pytest.approx(10.0)
        rung05 = lad["s1"][0.5]
        assert rung05.n_books == 1
        assert rung05.age_h == pytest.approx(12.0)
        assert rung05.p_over == pytest.approx(_pin_devig(0.70, 1.20))

    def test_mid_quote_superseded_within_book_only(self):
        # 同书更早报价被本书末价覆盖；跨书互不覆盖
        rows = [
            _row("srct:ou:14", 2.5, _at(9), 0.85, 0.90),
            _row("srct:ou:42", 2.5, _at(6), 1.50, 1.60),
            _row("srct:ou:42", 2.5, _at(8), 0.90, 0.95),
        ]
        rung = lib.aggregate_ou_ladder(rows)["s1"][2.5]
        # B 的 6 点深尾水位不参与：只算 B 末价 8 点
        expected = (_pin_devig(0.85, 0.90) + _pin_devig(0.90, 0.95)) / 2
        assert rung.p_over == pytest.approx(expected)
        assert rung.n_books == 2


# ------------------------------------------------- 3. BH 家族=当轮全候选


def _z_for_p(p: float) -> float:
    """单侧 p → z（独立实现：NormalDist vs _lib 的 erfc 口径互证）。"""
    return NormalDist().inv_cdf(1.0 - p)


def _family_of_20(p_lone: float) -> list[tuple[str, float]]:
    return [("c01", _z_for_p(p_lone))] + [
        (f"c{i:02d}", _z_for_p(0.5)) for i in range(2, 21)
    ]


class TestBhFullFamily:
    def test_audit_counterexample_full_family_blocks_lone_significant(self):
        # 审计反例：20 候选=1 个 p=0.02 + 19 个 p=0.5；全家族 BH 无一通过
        zs = _family_of_20(0.02)
        assert lib.one_sided_p(zs[0][1]) == pytest.approx(0.02, abs=1e-9)
        assert lib.bh_family(zs) == set()
        # 而先筛后校（只把显著者送进校正）会让它通过——该路径必须不可再现
        assert lib.bh([("c01", 0.02)]) == {"c01"}

    def test_strong_candidate_survives_full_family_bh(self):
        assert lib.bh_family(_family_of_20(0.001)) == {"c01"}  # p=0.001 ≤ 0.05×1/20

    def test_family_includes_non_computable_candidates(self):
        # z 不可计算（样本不足等）的候选入族（p=1.0），收紧其余候选的分位阈值
        zs = [("a", _z_for_p(0.03)), ("b", float("nan"))]
        assert lib.bh_family(zs) == set()  # m=2：0.03 > 0.05×1/2 不通过
        assert lib.bh([("a", 0.03)]) == {"a"}  # 若把它筛掉（m=1）就会通过


# --------------------------------- 4. 轮级单家族（票 20 / E-32 修订 C）

_LEDGER_COLS = [
    "entry_id",
    "ts",
    "ticket",
    "facet",
    "family",
    "slice",
    "window",
    "n_fx",
    "n_sel",
    "metric",
    "value",
    "se",
    "z",
    "p",
    "stage",
    "note",
]


def _ledger_row(entry_id: str, *, stage: str, z: str = "") -> dict:
    """合成试验账行：ticket/facet 取 entry_id 前两段、window 取末段（与真实账同构）。"""
    ticket, facet, _ = entry_id.split("-", 2)
    window = entry_id.rpartition("-")[2]
    return dict(
        zip(
            _LEDGER_COLS,
            [
                entry_id,
                "2026-11-10T00:00:00",
                ticket,
                facet,
                "F1",
                "s",
                window,
                "200",
                "20",
                "ev",
                "0.0000",
                "0.0000",
                z,
                "",
                stage,
                "",
            ],
            strict=True,
        )
    )


def _two_script_ledger() -> list[dict]:
    """合成台账：脚本 09-A 出 1 个孤强候选（p≈0.02），脚本 10-B 出 19 个弱候选。

    外加各窗噪声行（all_negative/insufficient/structure_only/dead/confirmation）——
    均不得入族。含内嵌连字符切片名（慢-联赛带），钉 entry_id 后缀解析取最后一段。
    """
    z_lone = f"{_z_for_p(0.02):.3f}"
    z_weak = f"{_z_for_p(0.5):.3f}"
    rows = [
        _ledger_row("09-A-F1-英甲-search", stage="all_negative", z="0.500"),
        _ledger_row("09-A-F2-<20%×主-search", stage="insufficient", z="nan"),
        _ledger_row("09-A-F1-英甲-validation", stage="dead", z="0.300"),
        _ledger_row("09-A-F1-英甲-confirmation", stage="dead", z="0.100"),
        _ledger_row("09-A-F7-≤-2×让方-search", stage="structure_only", z="1.000"),
        _ledger_row("09-A-F3-英超×<20%-search", stage="candidate", z=z_lone),
        _ledger_row("09-A-F3-英超×<20%-validation", stage="bh_pending", z=z_lone),
        _ledger_row("10-B-B4-慢-联赛带-search", stage="candidate", z=z_weak),
        _ledger_row("10-B-B4-慢-联赛带-validation", stage="bh_pending", z=z_weak),
    ]
    for i in range(18):
        rows.append(
            _ledger_row(f"10-B-B1-6h×1pp-{i:02d}-search", stage="candidate", z=z_weak)
        )
        rows.append(
            _ledger_row(f"10-B-B1-6h×1pp-{i:02d}-validation", stage="dead", z=z_weak)
        )
    return rows


class TestBhRoundSingleFamily:
    def test_family_is_union_of_both_scripts_candidates(self):
        family = lib.em1_round_family(_two_script_ledger())
        ids = {eid for eid, _z in family}
        assert len(family) == 20  # m=并集：1（09-A）+ 19（10-B）；噪声行不入族
        assert "09-A-F3-英超×<20%-validation" in ids
        assert "10-B-B4-慢-联赛带-validation" in ids
        assert all(f"10-B-B1-6h×1pp-{i:02d}-validation" in ids for i in range(18))

    def test_merged_family_threshold_uses_union_m(self):
        family = lib.em1_round_family(_two_script_ledger())
        # 孤强候选 p≈0.02：并族后 k=1 阈值=0.05×1/20=0.0025 → 不通过
        assert lib.bh_family(family) == set()
        # 而按脚本各自成族（旧粒度）它会通过——并族只收紧不放松，方向钉死
        lone = [kv for kv in family if kv[0].startswith("09-A-")]
        assert len(lone) == 1
        assert lib.bh_family(lone) == {lone[0][0]}

    def test_strong_candidate_still_passes_union_threshold(self):
        # p≈0.001 ≤ 0.05×1/20：并族下真强候选仍通过（m 收紧阈值、不封死）
        rows = _two_script_ledger()
        z_strong = f"{_z_for_p(0.001):.3f}"
        for r in rows:
            if r["entry_id"].startswith("09-A-F3-英超"):
                r["z"] = z_strong
        family = lib.em1_round_family(rows)
        assert lib.bh_family(family) == {"09-A-F3-英超×<20%-validation"}

    def test_candidates_without_validation_row_enter_family_as_nan(self):
        # 验证窗无行者（无 -validation 条目）与验证 z 不可计算者（"nan"）均入族
        rows = [
            _ledger_row("09-A-F8-意甲×over-search", stage="candidate", z="1.5"),
            _ledger_row("10-C-C1-Elo高-search", stage="candidate", z="0.5"),
            _ledger_row("10-C-C1-Elo高-validation", stage="dead", z="nan"),
        ]
        family = dict(lib.em1_round_family(rows))
        assert len(family) == 2
        assert math.isnan(family["09-A-F8-意甲×over-validation"])
        assert math.isnan(family["10-C-C1-Elo高-validation"])

    def test_missing_groups_fail_closed(self):
        rows = _two_script_ledger()  # 有 09-A 与 10-B 的 search 账，缺 10-C
        assert lib.em1_round_missing_groups(rows) == [("10", "C")]
        rows.append(_ledger_row("10-C-C3-荷甲-search", stage="all_negative", z="1.0"))
        assert lib.em1_round_missing_groups(rows) == []
        # 空台账：整轮缺报——缺报缩族与先筛后校同罪，不得校正
        expected_all = [("09", "A"), ("10", "B"), ("10", "C")]
        assert lib.em1_round_missing_groups([]) == expected_all

    def test_read_trial_ledger_roundtrip(self, tmp_path):
        path = tmp_path / "09-em1-trial-ledger.csv"
        assert lib.read_trial_ledger(str(path)) == []  # 未落账=空表
        with path.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=_LEDGER_COLS)
            w.writeheader()
            w.writerow(_ledger_row("10-C-C3-荷甲-search", stage="candidate", z="2.151"))
        rows = lib.read_trial_ledger(str(path))
        assert rows[0]["entry_id"] == "10-C-C3-荷甲-search"
        assert rows[0]["stage"] == "candidate"

    def _write_ledger(self, tmp_path: Path, rows: list[dict]) -> str:
        path = tmp_path / "09-em1-trial-ledger.csv"
        with path.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=_LEDGER_COLS)
            w.writeheader()
            w.writerows(rows)
        return str(path)

    def test_adjudicate_fail_closed_on_incomplete_round(self, tmp_path):
        # 缺 10-C 的 search 账：族仍在（m=20）但不裁（bh_pass 空）——缺报缩族
        # 与先筛后校同罪，宁可留 bh_pending 也不做变松的裁定
        path = self._write_ledger(tmp_path, _two_script_ledger())
        bh_pass, family, missing = lib.em1_round_adjudicate(path)
        assert missing == [("10", "C")]
        assert len(family) == 20
        assert bh_pass == set()

    def test_adjudicate_complete_round_returns_bh_pass(self, tmp_path):
        rows = _two_script_ledger()
        rows.append(_ledger_row("10-C-C3-荷甲-search", stage="all_negative", z="1.0"))
        path = self._write_ledger(tmp_path, rows)
        bh_pass, family, missing = lib.em1_round_adjudicate(path)
        assert missing == []
        assert len(family) == 20
        assert bh_pass == set()  # 孤强候选 p≈0.02 仍不过并族阈值（与上互证）

    def test_adjudicate_complete_round_strong_candidate_passes(self, tmp_path):
        rows = _two_script_ledger()
        rows.append(_ledger_row("10-C-C3-荷甲-search", stage="all_negative", z="1.0"))
        z_strong = f"{_z_for_p(0.001):.3f}"
        for r in rows:
            if r["entry_id"].startswith("09-A-F3-英超"):
                r["z"] = z_strong
        path = self._write_ledger(tmp_path, rows)
        bh_pass, family, missing = lib.em1_round_adjudicate(path)
        assert missing == []
        assert len(family) == 20
        assert bh_pass == {"09-A-F3-英超×<20%-validation"}
