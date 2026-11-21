"""JME 数据库初始化入口，已被 P0-03 第一阶段安全策略禁用。"""
from __future__ import annotations

import sys


def main() -> None:
    print(
        "JME 数据库初始化已被第一阶段安全策略禁用。\n"
        "\n"
        "当前禁止：\n"
        "- 自动创建数据库\n"
        "- 自动初始化数据库结构\n"
        "- 自动执行数据库迁移\n"
        "- 自动回滚\n"
        "- 自动删除或重建数据库对象\n"
        "\n"
        "如需执行数据库操作，必须等待后续独立审批。"
    )
    sys.exit(2)


if __name__ == "__main__":
    main()
