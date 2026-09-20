"""输出表格测试：等宽对齐、代码块标记、超长截断、转义边界。"""

from __future__ import annotations

import sys

import pytest


@pytest.fixture(autouse=True)
def _prep(monkeypatch, tmp_path):
    for mod in ("app.bot", "app.data", "app.config", "app.db"):
        sys.modules.pop(mod, None)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'tbl.db'}")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("FOOTBALL_API_KEY", "test-key")
    monkeypatch.setenv("TIMEZONE", "Asia/Shanghai")
    yield


def test_display_width_counts_cjk_as_two():
    from app.bot import _display_width
    assert _display_width("abc") == 3
    assert _display_width("阿森纳") == 6          # 3 个汉字 × 2
    assert _display_width("英超") == 4


def test_table_columns_are_aligned():
    from app.bot import _display_width, render_table

    rows = [["2026-09-20 00:30", "英超", "阿森纳 vs 切尔西"],
            ["2026-09-20 19:45", "德甲", "拜仁慕尼黑 vs 多特蒙德"]]
    table = render_table(["时间", "联赛", "对阵"], rows)

    lines = table.splitlines()
    # 等宽对齐的前提：各行的「显示宽度」一致（字符数可不同，因中文占 2 列）
    assert len({_display_width(line) for line in lines}) == 1
    # 表头、分隔线、两行数据
    assert len(lines) == 4
    assert set(lines[1]) <= {"-", " "}, "第二行应为分隔线"


def test_cjk_rows_align_with_ascii_rows():
    """中文与英文混排时仍按显示宽度对齐，而不是按字符数。"""
    from app.bot import _display_width, render_table

    rows = [["曼城", "英超"], ["Chelsea", "英超"]]
    lines = render_table(["球队", "联赛"], rows).splitlines()
    # 中文 2 列 / ASCII 1 列，靠显示宽度而非字符数对齐
    assert len(set(_display_width(l) for l in lines)) == 1
    # 且「曼城」那行确实靠补空格与 Chelsea 行对齐
    assert _display_width(lines[2]) == _display_width(lines[3])


def test_code_block_keeps_fence_unescaped():
    """``` 标记不能被转义，否则 Telegram 不会渲染成等宽表格。"""
    from app.bot import _code_block

    block = _code_block("A | B\n1 | 2")
    assert block.startswith("```\n")
    assert block.endswith("\n```")
    assert "\\`" not in block, "反引号被错误转义会导致代码块失效"


def test_escape_md_does_not_touch_table_content_fence():
    from app.bot import _escape_md

    escaped = _escape_md("Arsenal_Chelsea")
    assert escaped == "Arsenal" + chr(92) + "_Chelsea"
    assert _escape_md("2-1 45%") == "2-1 45%"   # 普通比分/百分比保持原样


def test_ellipsis_truncates_overlong_names():
    from app.bot import _display_width, _ellipsis

    long_name = "拜仁慕尼黑足球俱乐部第一队"
    clipped = _ellipsis(long_name, 10)
    assert _display_width(clipped) <= 10
    assert _display_width("短名") <= 10
    assert _ellipsis("阿森纳", 12) == "阿森纳"   # 未超长则原样返回


def test_empty_table_returns_empty_string():
    from app.bot import render_table
    assert render_table(["A"], []) == ""
    assert render_table([], [["x"]]) == ""
