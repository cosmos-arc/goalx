# 20: BH 轮级单家族合并（E-32 修订 C 落地小票）

Type: task
Status: ready-for-agent
Blocked by: None（2026-11-10 E-38 收官人裁 G-02=b 开出）

**What to build:** 把 09-em1-facet-a.py（F1-F6 与 F8 两处）+10-em1-facet-bc.py 的 bh_family 调用合并为**一轮 EM 全 facet 单家族**（E-32 修订 C 粒度裁定）。历史各轮候选数 0，合并不改已落账数字，只约束未来轮次。
- [ ] 三处调用合并为轮级单家族（跨脚本共享族成员装载）
- [ ] 合成反例单测：两 facet 脚本候选并族后 m 计数与阈值正确
- [ ] 三轴走 code-review + correctness（行为变更，非纯 docs）

## 不变量与人裁决项
- 家族=该轮进入验证窗全部候选（全阴/样本不足/验证窗无行者均在族内，z 不可计算 p=1.0）；任何先筛减族再校正=违约。
- 09-em1-protocol.md 的 E-32 措辞同步（修订 C 已随收官 PR 落账本，协议文件随本票同步）。
