# GitHub main 分支服务端门禁（boundary-hardening 票 06）

**状态（2026-11-03）**：`main` protected=false、rulesets=0——本地 `no-commit-to-branch`
hook + CI 只是客户端约束，服务端无强制。本文交付 ruleset 配置与验证步骤；
**由用户在 gh/网页执行**（人裁决 2026-11-02②，本票不代执行）。

## 1. 为什么

- 2026-11-02 外部审阅 P1：本地 hook 可被 `--no-verify` 绕过，CI 只在 PR 路径生效，
  直接 push `main` 无任何服务端拦截。
- 目标性质：必须 PR 合入、必需 CI 绿才可合、禁强推/删除；绕过权限仅 owner。

## 2. 配置（gh CLI，JSON body 稳态）

```bash
cat > /tmp/goalx-ruleset.json <<'EOF'
{
  "name": "main-protection",
  "target": "branch",
  "enforcement": "active",
  "conditions": {
    "ref_name": {
      "include": ["refs/heads/main"],
      "exclude": []
    }
  },
  "rules": [
    {
      "type": "pull_request",
      "parameters": {
        "required_approving_review_count": 0,
        "dismiss_stale_reviews": false,
        "require_code_owner_review": false,
        "require_last_push_approval": false
      }
    },
    {
      "type": "required_status_checks",
      "parameters": {
        "strict": false,
        "required_status_checks": [
          { "context": "CI / CI gate" },
          { "context": "PR base guard / base-must-be-main" }
        ]
      }
    },
    { "type": "non_fast_forward" },
    { "type": "deletion" }
  ],
  "bypass_actors": []
}
EOF
gh api -X POST repos/cosmos-arc/goalx/rulesets --input /tmp/goalx-ruleset.json
```

说明：

- `required_status_checks` 的 context = `<workflow name> / <job display name>`：
  `CI gate` job 在 ci.yml 里带 `name: CI gate`（key 是 `ci-gate`，context 用
  显示名）；`base-must-be-main` job 无 name，用 key。写错 context 会表现为
  required check 永不满足——配置后用一个 PR 验证 check 名被正确匹配。
- 个人单人仓库：`required_approving_review_count=0`（第二人审批明确出局），
  但"必须 PR"仍强制——历史不经 PR 的直推路径被关死。
- 绕过：`bypass_actors` 空=无人可绕。如需 owner 紧急通道，网页 Rules →
  main-protection → Bypass list 加 owner 本人，日常不开。
- 等价网页路径：Settings → Rules → Rulesets → New branch ruleset，逐项对应
  上面 rules 列表（同名同参数）。

## 3. 验证（生效后各跑一次）

```bash
# ① ruleset 已挂
gh api repos/cosmos-arc/goalx/rulesets --jq '.[] | {name, enforcement, rules: [.rules[].type]}'

# ② 直接 push main 被拒（建一个一次性提交验证,预期 remote 拒绝;
#    --no-verify 跳过本地 no-commit-to-branch hook——否则先被本地拦住到不了远端）
git commit --no-verify --allow-empty -m "chore: ruleset verification push" && git push origin main
# 预期: remote: error: GH013: Ruleset violation ... pull_request
git reset --hard origin/main   # 清理本地验证提交

# ③ CI 未绿时合 PR 被拒
gh pr merge <pr> --merge   # 预期: required status checks not met
```

## 4. 运行隔离的姊妹面（同票已交付）

- `/healthz` 携带进程运行指纹：`git_sha/git_dirty/config_hash/schema_version`
  （serve 启动时算一次）。
- 调度进程（`task serve-schedules`）启动打 `runtime fingerprint: …` 日志行；
  工作树 dirty 追加 `WORKTREE DIRTY` 亮黄行——**照跑不拒**（人裁决①）。
- 晨检第 13 项（`goalx morning-check`）读双面：healthz dirty 旗 + 调度日志
  dirty 行，dirty=degraded（黄）。
- release 目录（完整发布产物）按裁决③拆后续票，本票只做指纹上报。
