# 04 backend：ingest 壳成 seam——身份/客户端一家一处

Status: resolved
Blocked by: None（可立即开工；建议排在 03 后——03 重写 cli/tasks corpus 接线会搬走大半 browser_ssl_context 调用点，先 03 后 04 避免双改）

**交付（用户视角）**：TLS/UA/身份一变只改一个文件（不再全家桶手术）；api 层不再下探
采集内部件；新增一个数据源不再重抄客户端样板。

## 现状

~30 个 ingest 源各自重抄同一层壳，「一个 ingest 源」是只有 adapter 没有 interface 的
假想 seam：

- Chrome UA/身份字典 ×8 份：srct.py:108、caiguo.py:125、clubelo.py:36、jc.py:81、
  srcb.py:57、sporttery.py:206、uniform.py:220、zucai_official.py:70。
- `srct.browser_ssl_context()`（srct.py:123-127）住在单个源模块里却是全家族客户端
  工厂：jc/zucai_official/okooo/sina/openfootball/jc_audit/jc_backfill 引用 + cli
  10 处 + tasks 6 处。
- `oddsapi.polite_client` 是第二个事实工厂，且曾被 api/pool.py、api/results.py 反向
  import（01 已带走两个调用点）。
- 预算台账四种变体：oddsapi cost_ledger 预留/核对、propline cost_ledger SUM、
  guardian checkpoint 日账、srct 夜班内存 NightBudget（guardian 文档自认「propline
  同型」仍是第四种写法）。
- stats_dict 手搓 ×12；beat-key 约定 ×2（srct_shift.py:139-152 与 jc_shift.py:99-114）；
  raw 落盘两套同形（observations.save_raw vs CorpusStore.ingest_raw）。
- 共享面只有 rate_limit.py 一个真 deep module；data/ingest/__init__.py 仅一行注释。

TLS/UA 一变就是全家桶手术（05d0499「采集客户端统一」实测横扫全家）。

## 方案

家族已形成事实共识的壳收进一个共享模块（data/ingest 的公共面）：浏览器身份、TLS/HTTP
客户端工厂、stats 装配。各源只剩抓取+解析的 implementation。两个现成 adapter（浏览器
TLS × polite_client）证明这个 seam 是真的，不是假想。

## 实现决策

- 先收「零争议三件」：UA/身份常量、客户端工厂（browser_ssl_context 与 polite_client
  迁入共享模块，调用点直接改引，不做 re-export 过渡期）、stats_dict→`dataclasses.asdict`。
- 预算台账四种变体**不统一**——各自语义真实不同（预留 vs 日限 vs 夜班内存），本票只收
  客户端/身份；台账收敛留观察。
- beat-key ×2 与 raw 两套落盘记录在案不动（分属运行面/语料面两脸，ADR-0011 接受的成本）。
- api 层不再 import ingest 内部件为验收项（01 之后复查为零）。

## 测试接缝

- 壳模块公开面：工厂产出的客户端带预期 UA/TLS 头（mock transport 断言出网请求头，
  先例：test_ingest_* 家族的 mock transport 形态）。
- 各源既有测试全绿不动 = 纯搬迁证据；不逐源加测。

## 范围外

- 不动 rate_limit.py（已 deep）；不建「Source 抽象基类」（单实现假想变体，YAGNI）；
  夜班预算机制不动。

## 不变量与人裁决项

- 不变量：迁移后每个源的出网请求头逐字节不变（UA/Referer/TLS 套件——WAF 指纹断连
  教训在前，见源T 运行手册）；行为零变化。
- 人裁决：壳模块命名与落点（建议 data/ingest 内公共模块，起领域名如「采集客户端」）。

## 验收

- [x] 身份常量/客户端工厂一处住所（data/ingest/shell.py），8 份 UA 与双工厂调用点全部改引（无 re-export）
- [x] 出网请求头逐字节不变（test_ingest_shell 金标：五族 UA/各源头字典 items() 序列断言钉值+键序；mock transport 端到端）
- [x] api 层对 ingest 内部件 import 归零（口径=客户端/身份内部件；域面 zucai.PERIOD_MARKET/uniform 读函数/import_draw_results 保留，人裁决面）
- [x] 各源既有测试全绿 + 全量 `task lint` 绿（783 过 / 覆盖 92.17% / importlinter 1 kept）

## Answer

PR #111（合 main d9d0281）。shell.py 收所五族 UA（值不归一——lean-audit
裁定四族历史非逐字同）+ sporttery_json_headers（uniform/jc 同串同头）+
browser_ssl_context/polite_client（调用点直接改引：tasks×17/cli×6）；
uniform.stats_dict 删（唯一纯拷贝，tasks 改 asdict）。

**⚠ 工作区混入事件（记录在案）**：会话中途 srct.py 的
`RATE_LIMIT_PER_MINUTE` 出现 20→25——非本票任何编辑（盘点两读均 20，
脚本/ruff 未触及该行），疑运维侧手工调参混入暂存。已从 PR 剔出（还原
20，diff 零 RATE_LIMIT 行）；**若 25 是有意调参，需单独一行提交落 main**。

**附带发现（记录不改）**：polite_client 的 `limits=max_connections=2`
配自定 transport 时 httpx 不并入连接池——自始 no-op（池默认 100），有效
面=传输层 retries=3；改并发属行为变化留另票。stats_dict ×12 实查纯拷贝
仅 uniform 1 处（clubelo/understat/reconcile 有派生字段、zucai_official
asdict 变宽载荷、zucai 源B 留档不动、srct_shift/guardian 已是 asdict 等价）。

**review 修正**：zucai_official 头字典 `**` 展开改显式组合（恢复原文键序
UA/Referer/Origin/Accept——线上字节序属红线）；金标改 items() 序列断言。

## Comments
