"""CI 用：把 pytest 的 JUnit 报告里失败的测试写成 GitHub 注释（annotation）。

注释在网页上直接显示在运行结果里，也能用 API 读成 JSON（/check-runs/{id}/annotations）；
完整日志要跟一次跳转去下载，工具不一定能跟，读注释最省事。

用法（CI 里测试失败时）：python scripts/ci_annotate.py report.xml
"""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET

MAX_ANNOTATIONS = 40  # GitHub 每步最多显示 10 条错误注释，API 里能读到更多


def escape(text: str) -> str:
    """GitHub 工作流命令的转义：% 与换行要编码，否则消息被截断。"""
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def escape_property(text: str) -> str:
    """属性值（title=…）还要转义 : 与 ,——测试名是「模块::测试名」，其中的 :: 恰好是命令的分隔符，
    不转义会被从中间截断。"""
    return escape(text).replace(":", "%3A").replace(",", "%2C")


def main(path: str, log_path: str | None = None) -> int:
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError) as exc:  # 测试还没出报告就挂了（收集阶段出错、进程崩溃……）
        print(f"::error title=没有测试报告::{escape(str(exc))}")
        root = ET.Element("testsuites")
    count = 0
    for case in root.iter("testcase"):
        problem = case.find("failure")
        if problem is None:
            problem = case.find("error")
        if problem is None:
            continue
        count += 1
        if count > MAX_ANNOTATIONS:
            continue
        name = f"{case.get('classname', '')}::{case.get('name', '')}"
        detail = (problem.get("message") or "").strip()
        body = (problem.text or "").strip().splitlines()
        # 报错的最后几行最有用（断言、异常类型与位置）
        tail = "\n".join(body[-12:])
        print(f"::error title={escape_property(name)}::{escape(detail[:500])}%0A{escape(tail[-1500:])}")
    print(f"共 {count} 个测试失败" + (f"（只写了前 {MAX_ANNOTATIONS} 个注释）" if count > MAX_ANNOTATIONS else ""))
    if count == 0 and log_path:
        # 报告里找不到是哪个测试失败：把 pytest 输出的末尾写成注释（实际碰到过：失败了却一条注释也没有）
        try:
            lines = open(log_path, encoding="utf-8", errors="replace").read().splitlines()
        except OSError as exc:
            lines = [f"读不到 pytest 输出：{exc}"]
        print(f"::error title=pytest 输出末尾::{escape(chr(10).join(lines[-60:])[-6000:])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(
        main(sys.argv[1] if len(sys.argv) > 1 else "report.xml", sys.argv[2] if len(sys.argv) > 2 else None)
    )
