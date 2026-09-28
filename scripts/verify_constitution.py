"""校验项目宪法（AGENTS.md）存在、非空，且十条条款齐全。

用法：
    python scripts/verify_constitution.py

退出码：0 通过；1 有缺项。CI 与开工前都应跑一次。
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CONSTITUTION = REPO_ROOT / "AGENTS.md"

# 强制项：宪法必须包含的条款关键词（缺一即不合规）
REQUIRED_SECTIONS = (
    "验证纪律",
    "失败显式化",
    "配置驱动",
    "幂等",
    "最小改动",
    "文档如实",
    "审查义务",
    "权限边界",
    "沟通纪律",
    "冲突规则",
)
REQUIRED_MECHANISMS = (
    "开工前必须全量加载",
    "强制读取验证",
)
MIN_LENGTH = 1000


def main() -> int:
    problems: list[str] = []

    if not CONSTITUTION.is_file():
        problems.append(f"缺少宪法文件：{CONSTITUTION.relative_to(REPO_ROOT)}")
        text = ""
    else:
        text = CONSTITUTION.read_text(encoding="utf-8")
        if len(text.strip()) < MIN_LENGTH:
            problems.append(f"宪法内容过短（{len(text.strip())} 字符），疑似残缺")
        for keyword in REQUIRED_SECTIONS:
            if keyword not in text:
                problems.append(f"宪法缺少条款：{keyword}")
        for keyword in REQUIRED_MECHANISMS:
            if keyword not in text:
                problems.append(f"宪法缺少机制：{keyword}")

    if problems:
        print("宪法校验失败：")
        for item in problems:
            print(f"  - {item}")
        return 1

    print(f"宪法校验通过：{CONSTITUTION.relative_to(REPO_ROOT)}")
    print(f"  条款 {len(REQUIRED_SECTIONS)} 项齐全，机制 {len(REQUIRED_MECHANISMS)} 项齐全")
    return 0


if __name__ == "__main__":
    sys.exit(main())
