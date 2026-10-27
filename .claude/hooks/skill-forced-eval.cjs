#!/usr/bin/env node
/**
 * UserPromptSubmit 钩子：强制技能评估——每次提问先对照技能表，列出用得上的技能、逐个读完再动手。
 * 不加这一步，模型经常忘了有技能可用（ruoyi-plus-uniapp 实测：激活率从约 25% 提到 90% 以上）。
 *
 * 和 ruoyi-plus-uniapp 的版本不同：技能表不写死在脚本里，而是每次从 .claude/skills/<名字>/SKILL.md 的
 * description 读（名字 + 「触发场景：」那一行）——新增、改技能不用再记得同步这个脚本。
 *
 * Claude Code、智码与 heng-agent（兼容 Claude 钩子）都会跑它：输入是标准输入里的 JSON（prompt、cwd），
 * 输出的纯文字被当成补充上下文附在用户的消息后面。任何异常都静默放行，不能因为它坏了就拦住提问。
 */
const fs = require("fs");
const path = require("path");

function readInput() {
  try {
    return JSON.parse(fs.readFileSync(0, "utf8"));
  } catch {
    return null;
  }
}

/** 斜杠命令、恢复会话、编排通知、后台任务通知：不是普通提问，不做评估 */
function shouldSkip(prompt) {
  const text = prompt.trim();
  if (/^\/[^\/\s]+$/.test(text.split(/\s/)[0] || "")) return true; // /exp、/status……（排除 /a/b 这样的路径）
  if (/<command-name>/.test(text)) return true; // Claude Code 已展开的斜杠命令
  const unwrapped = text.replace(/^(?:<pasted_content[^>]*>\s*)+/, "");
  if (unwrapped.startsWith("[AGENT-EVENT]") || unwrapped.startsWith("<task-notification>")) return true;
  const lower = text.toLowerCase();
  const recovery = ["continued from a previous conversation", "conversation compacted", "session is being continued",
    "[stop 钩子要求继续]"];
  return recovery.some((p) => lower.includes(p.toLowerCase()));
}

/** 从 SKILL.md 的 frontmatter 取名字与触发场景（没有「触发场景」就取 description 第一行） */
function readSkill(file) {
  const text = fs.readFileSync(file, "utf8").replace(/^\uFEFF/, "");
  const match = /^---\r?\n([\s\S]*?)\r?\n---/.exec(text);
  if (!match) return null;
  const front = match[1];
  const name = (/^name:\s*(.+)$/m.exec(front) || [])[1] || path.basename(path.dirname(file));
  const trigger = (/触发场景[:：]\s*([\s\S]*?)(?:\r?\n\s*\r?\n|\r?\n\s*（|$)/.exec(front) || [])[1];
  const firstLine = (front.split(/\r?\n/).find((l) => l.trim() && !/^(name|description):\s*\|?\s*$/.test(l.trim())) || "")
    .replace(/^description:\s*/, "");
  const hint = (trigger || firstLine).replace(/\s+/g, " ").trim();
  return `- ${name.trim()}: ${hint.slice(0, 200)}`;
}

function main() {
  const input = readInput();
  if (!input || typeof input.prompt !== "string" || shouldSkip(input.prompt)) return;
  const root = process.env.CLAUDE_PROJECT_DIR || input.cwd || process.cwd();
  const dir = path.join(root, ".claude", "skills");
  let lines = [];
  try {
    lines = fs.readdirSync(dir)
      .map((name) => path.join(dir, name, "SKILL.md"))
      .filter((file) => fs.existsSync(file))
      .map(readSkill)
      .filter(Boolean);
  } catch {
    return;
  }
  if (!lines.length) return;
  process.stdout.write(`## 技能评估（先做这一步）

1. 对照下面的技能，列出这次用得上的：\`技能名: 理由\`；都用不上就写「无匹配技能」。
2. 用得上的逐个读完它的完整说明（一次一个）。
3. 读完再动手。不要跳过评估直接写，也不要漏掉对得上的技能。

可用技能：
${lines.join("\n")}
`);
}

try {
  main();
} catch {
  // 静默放行
}
process.exit(0);
