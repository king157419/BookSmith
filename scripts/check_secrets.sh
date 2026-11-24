#!/bin/sh
# 扫描 git 暂存区里疑似密钥的内容，发现就拒绝提交。
# 安装为 pre-commit 钩子：
#   ln -sf ../../scripts/check_secrets.sh .git/hooks/pre-commit   （Linux/macOS/Git Bash）
#   或 cp scripts/check_secrets.sh .git/hooks/pre-commit
# 手动运行：sh scripts/check_secrets.sh
# 确属误报时可临时 git commit --no-verify（请三思）。

PATTERNS='sk-[A-Za-z0-9_-]{20,}|tvly-[A-Za-z0-9_-]{10,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|AKIA[0-9A-Z]{16}|xox[baprs]-[A-Za-z0-9-]{10,}|AIza[0-9A-Za-z_-]{35}|-----BEGIN [A-Z ]*PRIVATE KEY-----|(DEEPSEEK|OPENAI|TAVILY|ANTHROPIC)_API_KEY[[:space:]]*=[[:space:]]*["'"'"']?[A-Za-z0-9_-]{8,}'

fail=0

# 不允许提交 .env 本体
for f in $(git diff --cached --name-only --diff-filter=ACMR); do
  case "$f" in
    .env|*/.env|.env.*|*/.env.*)
      case "$f" in *.env.example) ;; *)
        echo "✗ 不能提交环境变量文件：$f"; fail=1 ;;
      esac ;;
  esac
done

# 只检查新增的行，输出「文件:行号: 内容（已打码）」；本脚本自身的规则行跳过
hits=$(git diff --cached -U0 --no-color --diff-filter=ACMR -- . ':(exclude)scripts/check_secrets.sh' \
  | awk -v pat="$PATTERNS" '
      /^\+\+\+ / { file = substr($0, 7); next }
      /^@@/ { split($3, a, ","); line = substr(a[1], 2) + 0; next }
      /^\+/ { if (substr($0, 2) ~ pat) print file ":" line ": " substr($0, 2); line++ }')
if [ -n "$hits" ]; then
  echo "✗ 暂存区里发现疑似密钥："
  echo "$hits" | sed -E -e 's/((sk|tvly|gh[pousr])[-_][A-Za-z0-9]{4})[A-Za-z0-9_-]+/\1****/g' \
                        -e 's/(_API_KEY[[:space:]]*=[[:space:]]*["'"'"']?[A-Za-z0-9]{2})[A-Za-z0-9_-]+/\1****/g' | head -20
  fail=1
fi

if [ "$fail" -ne 0 ]; then
  echo ""
  echo "提交已拦截。把密钥移到 .env（不进仓库），用 git restore --staged <文件> 撤出暂存区后再提交。"
  exit 1
fi
exit 0
