"""Silver builder 内核规格断言（deepen-20260928 票 05）：一份语义两形态同构。

机器可校验契约：keep_value_changes 是 jc sp_change_event 与 srct
odds_change_event 两表事件流语义的唯一实现——本文件把 A→B→A / 心跳 /
缺列（不可解时间）钉进输入，断言属性行（srct 形态）与 dict 行（jc 形态）
穿过同一核后输出同构；CorpusStore.silver_path()/raw_dir() 布局口径一并钉死。

钉死时点：票 05（合并前两份同名同义 _keep_value_changes；重建幂等由各
builder 既有 fixture 测试继续钉）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import cast

from goalx_backend.data import silver
from goalx_backend.data.corpus_store import CorpusStore


@dataclass
class _AttrRow:
    """srct 形态行（属性访问）。"""

    published_ms: int | None
    source_order: int
    label: str


@dataclass
class _Counters:
    """记账面（两 builder report 的共同两计数）。"""

    bad_time_rows: int = 0
    heartbeat_dropped: int = 0
    accounted: int = field(default=0)


def _attr_input() -> list[_AttrRow]:
    """同一组轨迹（乱序给入，含 A→B→A、心跳、不可解时间、source_order 并列）。"""
    return [
        _AttrRow(1_000, 1, "B"),  # A→B 的 B
        _AttrRow(900, 0, "A"),  # 首
        _AttrRow(1_100, 2, "A"),  # B→A：保留（值组回到 A 但上一保留是 B）
        _AttrRow(1_200, 3, "A"),  # 心跳：丢
        _AttrRow(1_300, 4, "A"),  # 心跳：丢
        _AttrRow(950, 5, "A"),  # 时间早于上一保留但晚于首条：同值心跳丢
        _AttrRow(None, 6, "C"),  # 不可解时间：不落事件，bad_time 记账
        _AttrRow(1_400, 7, "C"),  # 新值：保留
    ]


def _dict_input() -> list[dict[str, object]]:
    return [
        {
            "published_ms": r.published_ms,
            "source_order": r.source_order,
            "label": r.label,
        }
        for r in _attr_input()
    ]


def test_keep_two_shapes_isomorphic() -> None:
    """同一轨迹喂同一核：属性形态与 dict 形态保留序/计数逐项同构。"""
    attr_report = _Counters()
    kept_attr = silver.keep_value_changes(
        _attr_input(),
        attr_report,
        time_of=lambda r: r.published_ms,
        order_of=lambda r: r.source_order,
        value_of=lambda r: (r.label,),
        account_row=lambda r: None,
    )
    dict_report = _Counters()
    kept_dict = silver.keep_value_changes(
        _dict_input(),
        dict_report,
        time_of=lambda r: cast("int | None", r["published_ms"]),
        order_of=lambda r: cast("int", r["source_order"]),
        value_of=lambda r: (r["label"],),
    )

    # 事件序：A(首) → B → A(B→A 回变，保留) → C；心跳×3 丢；缺列×1 跳
    assert [r.label for r in kept_attr] == ["A", "B", "A", "C"]
    assert [cast("str", r["label"]) for r in kept_dict] == ["A", "B", "A", "C"]
    # source_order 并列时按序稳定：首条取 order 0
    assert [r.source_order for r in kept_attr] == [0, 1, 2, 7]
    # 计数两形态一致；account_row 缺省不炸、有则对每个可解行调用（含被丢的）
    assert attr_report.bad_time_rows == dict_report.bad_time_rows == 1
    assert attr_report.heartbeat_dropped == dict_report.heartbeat_dropped == 3


def test_keep_account_row_counts_heartbeat_dropped_rows_too() -> None:
    """account_row 对每个可解时间行调用——含随后被心跳丢弃的行（srct 口径）。"""
    report = _Counters()

    def _account(row: _AttrRow) -> None:
        report.accounted += 1

    silver.keep_value_changes(
        _attr_input(),
        report,
        time_of=lambda r: r.published_ms,
        order_of=lambda r: r.source_order,
        value_of=lambda r: (r.label,),
        account_row=_account,
    )
    assert report.accounted == 7  # 8 行 − 1 不可解时间


def test_corpus_store_layout_anchors(tmp_path: object) -> None:
    """silver_path()/raw_dir() 是磁盘布局唯一落点（票 05 不变量）。"""
    store = CorpusStore(cast("str", tmp_path))
    root = store.root
    assert store.silver_path("jc", "jc_sp_change_event") == (
        root / "silver" / "jc" / "jc_sp_change_event"
    )
    assert store.silver_path("srct", "odds_change_event") == (
        root / "silver" / "srct" / "odds_change_event"
    )
    assert store.raw_dir("srct", "day_page") == root / "raw" / "srct" / "day_page"
    assert store.raw_path("srct", "day_page", "20260928") == (
        root / "raw" / "srct" / "day_page" / "20260928.gz"
    )
