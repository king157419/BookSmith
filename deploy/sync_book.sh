#!/bin/sh
# 把树莓派上的书稿主克隆（~/BookSmith/book）和 GitHub 私有仓库双向同步（只做快进，绝不强推）。
#   本地领先（网页里审核通过后合并的）→ 推送到 GitHub
#   GitHub 领先（有人在别处推了新提交）→ 本地快进
#   双方分叉 → 什么都不动，返回非 0，等人工处理
# 由 booksmith-book-sync.timer 每 5 分钟运行一次。
set -u
BOOK="${BOOK_DIR:-$HOME/BookSmith/book}"
BRANCH=master
cd "$BOOK" || exit 2
exec 9>"$BOOK/.git/booksmith-sync.lock"
flock -n 9 || { echo "另一个同步正在进行"; exit 0; }

git fetch -q origin "$BRANCH" || { echo "fetch 失败（网络？）"; exit 1; }
L=$(git rev-parse "$BRANCH"); R=$(git rev-parse "origin/$BRANCH")
[ "$L" = "$R" ] && exit 0
B=$(git merge-base "$BRANCH" "origin/$BRANCH")

if [ "$B" = "$R" ]; then
    git push -q origin "$BRANCH:$BRANCH" && echo "已推送：$(git log --oneline "$R..$L" | wc -l) 个提交（$R → $L）"
elif [ "$B" = "$L" ]; then
    if [ "$(git symbolic-ref -q --short HEAD)" = "$BRANCH" ]; then
        if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
            echo "主克隆有未提交改动，暂不快进"; exit 1
        fi
        git merge -q --ff-only "origin/$BRANCH" && echo "已从 GitHub 快进到 $R"
    else
        git update-ref "refs/heads/$BRANCH" "$R" "$L" && echo "已从 GitHub 快进到 $R（未检出）"
    fi
else
    echo "本地与 GitHub 已分叉（本地 $L，GitHub $R），需要人工合并"; exit 1
fi
