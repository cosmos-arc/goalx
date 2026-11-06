# GitHub main 分支服务端门禁

**状态（2026-11-06）**：**已配置**——ruleset `main-protection`（id 26675911，
enforcement=active）按 §2 模板落地，随 V1 发布收口生效；必须 PR 合入、
必需检查（`CI gate`/`base-must-be-main`）绿才可合、禁强推/删除，无人可绕。
本文保留为配置模板与验证步骤记录。

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
        "dismiss_stale_reviews_on_push": false,
        "require_code_owner_review": false,
        "require_last_push_approval": false,
        "required_review_thread_resolution": true
      }
    },
    {
      "type": "required_status_checks",
      "parameters": {
        "strict_required_status_checks_policy": false,
        "required_status_checks": [
          { "context": "CI gate" },
          { "context": "base-must-be-main" }
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

- `required_status_checks` 的 context = **check-run 名**（≠ `<workflow> / <job>`
  拼法）：job 带显式 `name:` 时 context=该显示名（ci.yml 的 `ci-gate` job 带
  `name: CI gate` → context `CI gate`）；job 无 name 时 context=job key
  （pr-base-guard.yml 的 `base-must-be-main`）。两处均已对实际 PR
  check-runs 核过（2026-11-04）。写错 context 表现为
  required check 永不满足——配置后用一个 PR 验证 check 名被正确匹配。
- ruleset 字段名按 GitHub rulesets REST schema：`pull_request` 规则的参数是
  `dismiss_stale_reviews_on_push`/`required_review_thread_resolution`（非
  网页 UI 文案）；`required_status_checks` 的严格分支策略键是
  `strict_required_status_checks_policy`（非 `strict`）。
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

## 4. 运行隔离的姊妹面（同批交付）

- `/healthz` 携带进程运行指纹：`git_sha/git_dirty/config_hash/schema_version`
  （serve 启动时算一次）。
- 调度进程（`task serve-schedules`）启动打 `runtime fingerprint: …` 日志行；
  工作树 dirty 追加 `WORKTREE DIRTY` 亮黄行——**照跑不拒**（人裁决①）。
- 晨检第 13 项（`goalx morning-check`）读双面：healthz dirty 旗 + 调度日志
  dirty 行，dirty=degraded（黄）。
- release 目录（完整发布产物）另拆后续工作，此处只记录指纹上报。
