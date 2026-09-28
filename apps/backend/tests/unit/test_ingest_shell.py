"""采集客户端共享壳规格断言（deepen-20260928 票 04）：出网身份逐字节不变。

机器可校验契约：壳模块（data/ingest/shell）收所八源身份与双工厂——本文件
把每个源的**迁移前**头字典/UA 串钉进字面量（抄自 main 手写时代原文），
新产出与字面量任一漂移即红。WAF 头校验 + TLS 指纹断连教训在前（源T 运行
手册）：改值必须走「先改本文件、再改码」。

钉死时点：票 04（八源：srct/caiguo/clubelo/jc/srcb/sporttery/uniform/
zucai_official；值不归一——四族 UA 历史非逐字同，lean-audit 裁定）。
"""

from __future__ import annotations

import ssl

import httpx
import pytest

from goalx_backend.data.ingest import (
    caiguo,
    clubelo,
    shell,
    sporttery,
    srcb,
    srct,
    uniform,
    zucai_official,
)

# 迁移前原文（git show main 逐字抄录）——出网字节的金标
DESKTOP_UA_GOLDEN = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
MAC_UA_GOLDEN = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 Chrome/126.0 Safari/537.36"
)
CHROME_129_GOLDEN = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
)
SPORTTERY_CALC_GOLDEN = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/126.0"
)
MOBILE_UA_GOLDEN = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)
_CHROME_CIPHERS_GOLDEN = (
    "ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256:"
    "ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-RSA-AES256-GCM-SHA384:"
    "ECDHE-ECDSA-CHACHA20-POLY1305:ECDHE-RSA-CHACHA20-POLY1305:"
    "AES128-GCM-SHA256:AES256-GCM-SHA384"
)


def test_ua_constants_byte_identical() -> None:
    """五族 UA 常量逐字节等于迁移前原文（四族值非逐字同是历史事实，勿归一）。"""
    assert shell.DESKTOP_UA == DESKTOP_UA_GOLDEN
    assert shell.MAC_CHROME_UA == MAC_UA_GOLDEN
    assert shell.CHROME_129_UA == CHROME_129_GOLDEN
    assert shell.SPORTTERY_CALC_UA == SPORTTERY_CALC_GOLDEN
    assert shell.MOBILE_UA == MOBILE_UA_GOLDEN


def test_source_header_builders_unchanged() -> None:
    """各源头字典产出 == 迁移前原文，键序一并钉死（出网头字节序属红线，
    dict == 序不敏感，故用 items() 序列断言）。"""
    assert list(shell.sporttery_json_headers().items()) == [
        ("User-Agent", MAC_UA_GOLDEN),
        ("Referer", "https://www.sporttery.cn/"),
        ("Accept", "application/json, text/plain, */*"),
    ]
    assert list(uniform._headers().items()) == list(
        shell.sporttery_json_headers().items()
    )
    # caiguo：UA 同族 + html Accept（无 Referer）
    assert list(caiguo._browser_headers().items()) == [
        ("User-Agent", MAC_UA_GOLDEN),
        ("Accept", "text/html,application/xhtml+xml"),
    ]
    # clubelo：仅 UA（129 全串）
    assert list(clubelo._HEADERS.items()) == [("User-Agent", CHROME_129_GOLDEN)]
    # srcb：移动面 UA（绑定在册）
    assert srcb.MOBILE_UA is shell.MOBILE_UA
    # zucai_official：json 头 + 页面 Referer + Origin，键序按原文
    referer = "https://www.sporttery.cn/ctzc/jsq/index.html"
    assert list(zucai_official._headers(referer).items()) == [
        ("User-Agent", MAC_UA_GOLDEN),
        ("Referer", referer),
        ("Origin", "https://www.sporttery.cn"),
        ("Accept", "application/json, text/plain, */*"),
    ]
    # sporttery 计算器短串 / srct 桌面全串（绑定在册）
    assert sporttery.SPORTTERY_CALC_UA is shell.SPORTTERY_CALC_UA
    assert srct.DESKTOP_UA is shell.DESKTOP_UA


def test_browser_ssl_context_cipher_profile() -> None:
    """TLS 画像：Chrome 套件生效（ClientHello 指纹断连教训的修复面）。"""
    ctx = shell.browser_ssl_context()
    assert isinstance(ctx, ssl.SSLContext)
    enabled = {cipher["name"] for cipher in ctx.get_ciphers()}
    expected = set(_CHROME_CIPHERS_GOLDEN.split(":"))
    # 套件名单须全部启用（实现可能附 TLS1.3 系统项，只验目标集在册）
    assert expected <= enabled


def test_polite_client_profile() -> None:
    """polite 画像：传输层重试 3 有效；limits= 自始 no-op（池默认 100，实证）。"""
    with shell.polite_client() as client:
        transport = client._transport
        assert isinstance(transport, httpx.HTTPTransport)
        assert transport._pool._retries == 3
        assert transport._pool._max_connections == 100  # limits= 不并入自定池


def test_browser_face_sends_ua_over_wire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """端到端：浏览器面客户端 + 壳头出网，UA 原样上线路（mock transport）。"""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)
    with httpx.Client(
        transport=transport, verify=shell.browser_ssl_context()
    ) as client:
        client.get("https://example.test/", headers=shell.sporttery_json_headers())
    assert len(seen) == 1
    assert seen[0].headers["User-Agent"] == MAC_UA_GOLDEN
    assert seen[0].headers["Referer"] == "https://www.sporttery.cn/"
