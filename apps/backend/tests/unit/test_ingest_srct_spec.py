"""源T 端点规格断言测试（票 59）：注册表与 collection-spec.md §一钉死一致。

机器可校验契约：`.scratch/goalx-quant/collection-spec.md` §一六端点表是
唯一事实源（先改表、再改码）——本文件把该表钉进测试字面量，注册表
（srct.SPEC_ENDPOINTS）与字面量任一漂移即红。新端点入列（票 60/61/62）
与撤除（票 66 stats→detail 切换）都必须同步改本文件——这就是"改表"
动作的测试面。

钉死时点：票 61（AsianOdds+OverDown+detail 入列；changeDetail 撤采留档；stats
过渡在列至票 66）。票 66 后全深每场端点=5（规格 §一"每场 5 请求"终态）。
"""

from __future__ import annotations

import httpx
import pytest

from goalx_backend.config import Settings
from goalx_backend.data.ingest import srct


def test_spec_registry_pinned_to_collection_spec() -> None:
    """注册表逐行钉死（dataset/ext/深度成员/状态）——单侧漂移即红。"""
    assert (
        srct.SpecEndpoint("day_page", ".htm", per_day=True),
        srct.SpecEndpoint("odds_1x2d", ".js", in_shallow=True),
        srct.SpecEndpoint("asian_odds", ".html"),
        srct.SpecEndpoint("over_down", ".html"),
        srct.SpecEndpoint("match_detail", ".html"),
        srct.SpecEndpoint("match_stats", ".html"),
        srct.SpecEndpoint("asian_handicap", ".html", status="retired"),
    ) == srct.SPEC_ENDPOINTS


def test_depth_sets_derived_from_registry() -> None:
    """全深/浅深端点集是注册表派生（硬编码 if 链已消灭）。"""
    assert srct.match_endpoint_datasets(srct.DEPTH_FULL) == (
        "odds_1x2d",
        "asian_odds",
        "over_down",
        "match_detail",
        "match_stats",
    )
    assert srct.match_endpoint_datasets(srct.DEPTH_SHALLOW) == ("odds_1x2d",)
    assert srct.deep_endpoint_datasets() == (
        "asian_odds",
        "over_down",
        "match_detail",
        "match_stats",
    )
    assert srct.retired_endpoint_datasets() == ("asian_handicap",)


def test_retired_never_collected_nor_required() -> None:
    """撤采数据集不进任何深度端点集、不在必配端点、无接线。"""
    retired = set(srct.retired_endpoint_datasets())
    assert retired.isdisjoint(srct.match_endpoint_datasets(srct.DEPTH_FULL))
    assert retired.isdisjoint(srct.match_endpoint_datasets(srct.DEPTH_SHALLOW))
    assert retired.isdisjoint(srct._ENDPOINT_WIRING)
    assert retired.isdisjoint(srct._ENDPOINT_SETTINGS)
    # bronze 版本留档：silver 存量口径（odds_change_event ah 面）仍可引用
    assert srct.BRONZE_VERSIONS["asian_handicap"] == "srct_hdp_v1"


def test_full_depth_request_count_per_match() -> None:
    """每场请求数=全深端点数（夜班预算/规格 §一测算口径）。"""
    settings = Settings(
        _env_file=None,
        srct_day_url="https://srct.test/over/{date}.htm",
        srct_odds_url="https://srct.test/odds/{sid}.js",
        srct_odds_referer="https://srct.test/oddslist/{sid}.htm",
        srct_asianodds_url="https://srct.test/asian/{sid}",
        srct_overdown_url="https://srct.test/overdown/{sid}",
        srct_detail_url="https://srct.test/detail/{sid}cn.htm",
        srct_stats_url="https://srct.test/shijian/{sid}.htm",
    )
    specs = srct._endpoint_specs(httpx.Client(), settings)
    assert len(specs) == len(srct.match_endpoint_datasets(srct.DEPTH_FULL))
    assert [s.dataset for s in specs] == list(
        srct.match_endpoint_datasets(srct.DEPTH_FULL)
    )
    # 每端点 ext 与注册表一致（raw 落盘扩展名契约）
    ext_of = {e.dataset: e.ext for e in srct.SPEC_ENDPOINTS}
    assert all(spec.ext == ext_of[spec.dataset] for spec in specs)


def test_bronze_versions_cover_all_registered() -> None:
    """注册表内（含 retired）每数据集都有 bronze 版本——信封契约完备。"""
    for endpoint in srct.SPEC_ENDPOINTS:
        assert endpoint.dataset in srct.BRONZE_VERSIONS, endpoint.dataset


@pytest.mark.parametrize("dataset", ["asian_odds", "match_stats"])
def test_deep_evidence_replay_set_matches_registry(dataset: str) -> None:
    """中断证据重放集=注册表深端点派生（探针语义随表走）。"""
    assert dataset in srct.deep_endpoint_datasets()
