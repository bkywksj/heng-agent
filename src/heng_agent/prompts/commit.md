帮我把当前的改动提交到 git。
1. 先跑 `git status`、`git diff` 和 `git diff --staged` 看清改了什么；再看 `git log -5 --oneline`，了解这个仓库提交信息的风格（用什么语言、有没有 feat: / fix: 这类前缀）。
2. 没有改动就直接告诉我，不要提交空的。
3. 按仓库风格写一条提交信息：第一行概括做了什么，内容多就空一行写要点。
4. 只 `git add` 和这次改动相关的文件，别用 `git add -A` 把不相干的也加进去；不要提交密钥、.env、临时文件、构建产物，发现可疑的文件先问我。然后 `git commit`。
5. 不要 push，也不要改历史（不 amend、不 rebase、不 reset），除非我明确要求。

$ARGUMENTS
