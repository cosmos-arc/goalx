"""
采集客户端共享壳（deepen-20260928 票 04）：出网身份与 TLS/HTTP 工厂一家一处。

红线：出网请求头逐字节不变（WAF 头校验 + TLS 指纹断连教训，见源T 运行
手册）。UA 族间值差异是历史事实（lean-audit 裁定不逐字归一——四族值非
逐字同），本模块只收住所不改值；各源的 Referer/Origin/Accept 组合仍由
源模块按其身份拼装，UA 串一律引自这里。

两个现成 adapter（浏览器 TLS 画像 × polite 重试限速）证明这个 seam 是
真的：新增数据源不再重抄客户端样板。预算台账四种变体不在此收口
（预留/日限/夜班内存语义真实不同，票 04 记录在案）。
"""

from __future__ import annotations

import ssl

import httpx

# ——— 出网身份（八处收一所；值冻结，勿"顺手统一"） ———

#: 桌面 Chrome 全串（Windows/124，源T 族）
DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

#: Mac Chrome/126（caiguo / jc / uniform / zucai_official 四源同串）
MAC_CHROME_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 Chrome/126.0 Safari/537.36"
)

#: Mac Chrome/129 全串（clubelo）
CHROME_129_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
)

#: 竞彩计算器短串（sporttery，票 01 实测该形态可用）
SPORTTERY_CALC_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/126.0"
)

#: 移动端 Safari（源B 手机面）
MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)

_SPORTTERY_REFERER = "https://www.sporttery.cn/"


def sporttery_json_headers() -> dict[str, str]:
    """官方 json 族同头直通（uniform/jc 实测同串，票 44）。"""
    return {
        "User-Agent": MAC_CHROME_UA,
        "Referer": _SPORTTERY_REFERER,
        "Accept": "application/json, text/plain, */*",
    }


# Chrome 风格密码套件（2026-09-27：源T vip/zq 边缘按 Python 默认 TLS 指纹断连,
# 换套件改变 ClientHello 指纹后实测立通;对其它源无副作用——Chrome 套件兼容性最广）
_CHROME_CIPHERS = (
    "ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256:"
    "ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-RSA-AES256-GCM-SHA384:"
    "ECDHE-ECDSA-CHACHA20-POLY1305:ECDHE-RSA-CHACHA20-POLY1305:"
    "AES128-GCM-SHA256:AES256-GCM-SHA384"
)


def browser_ssl_context() -> ssl.SSLContext:
    """采集客户端统一 TLS 画像:``httpx.Client(verify=browser_ssl_context())``。"""
    ctx = ssl.create_default_context()
    ctx.set_ciphers(_CHROME_CIPHERS)
    return ctx


def polite_client() -> httpx.Client:
    """
    带连接级重试与限速的采集客户端（票 19 重试+礼貌限速）。

    ponytail: limits= 配自定 transport 时 httpx 不并入连接池——
    max_connections=2 自始 no-op（池走默认 100，票 04 规格测试实证），
    有效面=传输层 retries=3；按值保留原样（改并发数属行为变化，另票）。
    """
    return httpx.Client(
        transport=httpx.HTTPTransport(retries=3),
        limits=httpx.Limits(max_connections=2),
    )
