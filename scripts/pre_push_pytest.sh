#!/usr/bin/env bash
# pre-push backend fast tests（票 36）：输出落盘 + 失败时只回放有界尾部。
#
# 为什么不直接让 pytest 写 stdout：pre-commit 把 hook 的捕获输出作为一次
# write 整体重放（pre_commit/output.py::write_line_b）。pyproject 的
# log_cli=true 使逐用例 [gwN] 行不受 -q 压制（满量 xdist 约 1800 用例
# ≈360KB）；父进程（代理会话的 push 管道）排水慢/非阻塞时，这单次大写
# 触发 BlockingIOError [Errno 35]，pre-commit 自身崩溃（rc 120）而非传递
# 测试结果。失败回放的字节数必须小于管道容量（macOS/Linux 均为 64KB），
# 才能在零排水极端下也证明不触发；完整输出留在日志文件按路径指认。
#
# 退出码语义：原样透传 pytest 退出码；本脚本自身故障（mktemp 失败等）
# 经 set -eu 以非零退出 → push 被拦截（fail-closed，崩溃≠通过）。

set -eu

mkdir -p .scratch
log=$(mktemp .scratch/prepush-pytest.XXXXXXXX)

rc=0
uv run --no-sync pytest apps/backend/tests -q --no-cov -n auto -m "not slow" \
  >"$log" 2>&1 || rc=$?

if [ "$rc" -ne 0 ]; then
  tail -c 56000 "$log"
  printf '\npre-push pytest failed (rc=%s); full output: %s\n' "$rc" "$log"
fi

exit "$rc"
