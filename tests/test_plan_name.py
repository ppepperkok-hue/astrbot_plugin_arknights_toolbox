"""`modules/shift_reminder/plan_name.py` 的单测。

这个文件的存在意义就是**证明我们没有瞎猜**：现实里 ``plans[].name`` 的写法五花八门，
一个「看到数字就当小时」的实现会把 ``A+B 高效长班 1`` 读成「1 小时」，然后让用户
在页面上点一个错的「填入」。所以下面的用例分三类：

1. **必须读得出**——riic.autos（ArknightsInfraCalc-v3）导出的规整格式；
2. **必须读不出**——名字里根本没有时长，或时长不在可判定的位置；
3. **单条读得出、但整体必须作废**——`suggest_shift_minutes` 的闸门；
   以及**一份真实 MAA 样本确实读得出正确节奏**（16/4/4）的情形，如实记录在这里，
   免得后人以为「MAA 样本一律读不出」。

真实数据出处：开发机上 MAA 自带的 ``resource/custom_infrast/*.json``（只读核对，
未复制进仓库——那是 AGPL 项目）与用户提供的 riic.autos 导出文件。
"""

import pytest

from modules.shift_reminder.plan_name import (
    MINUTES_PER_DAY,
    parse_duration_hint,
    suggest_shift_minutes,
)

SHIFT_COUNT = 3


# --- 1. 真实导出格式：必须读得出 ---------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        # 用户那份 riic.autos 导出文件的原文
        ("Shift 1 · 12h", 720),
        ("Shift 2 · 6h", 360),
        ("Shift 3 · 6h", 360),
        # 分隔符与大小写差异
        ("Shift-2|6h", 360),
        ("Shift 1 - 12H", 720),
        ("shift 1 · 12 h", 720),
        # 单位写法
        ("12h", 720),
        ("12H", 720),
        ("12 小时", 720),
        ("12小时", 720),
        ("12 时", 720),
        # 小时 + 分钟
        ("6h30m", 390),
        ("6小时30分", 390),
        ("6 h 30 m", 390),
        # 小数小时（真实样本里出现过 `C 组 8.5H`）
        ("8.5H", 510),
        ("C 组 8.5H", 510),
        # 全角（中文输入法下很容易打出来）
        ("１２ｈ", 720),
        ("12　小时", 720),
        # 末尾标点
        ("Shift 1 · 12h。", 720),
        ("Shift 1 · 12h ", 720),
        # 上限：24 小时本身是允许的（虽然少见）
        ("24h", MINUTES_PER_DAY),
    ],
)
def test_reads_a_duration_when_the_name_really_carries_one(name: str, expected: int) -> None:
    assert parse_duration_hint(name) == expected


# --- 2. 必须读不出：不许看到数字就当小时 -------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        # 时长后面还有内容 → 分不清「这一班 12 小时」还是「12 小时轮次下的第一班」
        "12H第一班",
        "12H第二班",
        # 完全没有单位，只有序号
        "第1班",
        "第2班",
        "A+B 高效长班 1",
        "B+C 补充短班 2",
        # 有数字但没单位
        "Shift 1",
        "12",
        "第一班 6",
        # 空 / 空白 / 非字符串语义
        "",
        "   ",
        "。",
        # 数值本身不成立
        "0h",
        "25h",
        "30 小时",
        "-6h",
    ],
)
def test_refuses_to_guess_when_there_is_no_usable_duration(name: str) -> None:
    assert parse_duration_hint(name) is None


def test_non_string_input_is_refused() -> None:
    assert parse_duration_hint(None) is None  # type: ignore[arg-type]
    assert parse_duration_hint(12) is None  # type: ignore[arg-type]


# --- 3. 闸门：单条读得出不代表整体可用 ---------------------------------------


def test_accepts_the_real_export_rhythm() -> None:
    """用户那份文件：12/6/6，合计 24 小时 → 给出建议。"""
    names = ["Shift 1 · 12h", "Shift 2 · 6h", "Shift 3 · 6h"]

    assert suggest_shift_minutes(names, expected_count=SHIFT_COUNT) == (720, 360, 360)


def test_records_that_one_real_maa_layout_is_read_correctly() -> None:
    """**如实记录**：MAA 的一份真实样本（153 布局）读出来是 16/4/4，合计正好 24 小时。

    这不是「瞎猜成功」，而是名字里确实写着 ``A+B 16H`` 这样的时长。把它写进测试
    是为了让后人知道「MAA 样本一律读不出」这个说法**不准确**——真正的保证是
    「三个名字全部读得出且合计 24 小时」，而不是「MAA 的一律作废」。
    """
    names = ["A+B 16H", "A+C 4H", "B+C 4H"]

    assert suggest_shift_minutes(names, expected_count=SHIFT_COUNT) == (960, 240, 240)


@pytest.mark.parametrize(
    ("names", "why"),
    [
        # 合计 1950 分钟 ≠ 24 小时 —— 闸门拦下，即便每个单条都读得出
        (["A 组 12 H", "B 组 12H", "C 组 8.5H"], "合计不是 24 小时"),
        # 时长不在名字末尾，全部读不出
        (["12H第一班", "12H第二班", "12H第三班"], "时长后面还有内容"),
        # 完全没有时长
        (["第1班", "第2班", "第3班"], "名字里没有单位"),
        (["A+B 高效长班 1", "A+B 高效长班 2", "B+C 补充短班 1"], "只有序号"),
        # 部分读得出 = 整体作废（不许拿读得出的那几个凑）
        (["Shift 1 · 12h", "第2班", "Shift 3 · 6h"], "部分读不出"),
        # 三个都读得出、合计 24 小时，但个数不对
        (["A+B 16H", "A+C 4H", "B+C 4H", "D 0h"], "个数不是三班"),
    ],
)
def test_gate_rejects_real_maa_name_sets(names: list[str], why: str) -> None:
    assert suggest_shift_minutes(names, expected_count=SHIFT_COUNT) is None, why


def test_gate_requires_exactly_the_expected_count() -> None:
    names = ["12h", "6h", "6h"]

    assert suggest_shift_minutes(names, expected_count=3) == (720, 360, 360)
    assert suggest_shift_minutes(names, expected_count=4) is None
    assert suggest_shift_minutes(names, expected_count=2) is None


def test_gate_rejects_a_sum_that_is_not_24_hours() -> None:
    # 注意 `8h/8h/8h` 与 `12h/12h` **都是**正好 24 小时，属合法组合，
    # 所以这里用的是真的凑不上的组合（25 小时 / 19 小时）。
    assert suggest_shift_minutes(["8h", "8h", "9h"], expected_count=3) is None
    assert suggest_shift_minutes(["13h", "12h"], expected_count=2) is None
    assert suggest_shift_minutes(["7h", "5h", "7h"], expected_count=3) is None


def test_gate_accepts_other_24_hour_rhythms() -> None:
    """闸门只认「合计 24 小时」，不预设 12/6/6 —— 8/8/8 同样是合法节奏。"""
    assert suggest_shift_minutes(["8h", "8h", "8h"], expected_count=3) == (480, 480, 480)
    assert suggest_shift_minutes(["12h", "12h"], expected_count=2) == (720, 720)
    assert suggest_shift_minutes(["16h", "4h", "4h"], expected_count=3) == (960, 240, 240)


def test_gate_rejects_empty_and_wrong_shapes() -> None:
    assert suggest_shift_minutes([], expected_count=3) is None
    assert suggest_shift_minutes(["12h", "6h", "6h"], expected_count=0) is None
    assert suggest_shift_minutes("156h", expected_count=3) is None  # type: ignore[arg-type]
    assert suggest_shift_minutes(None, expected_count=3) is None  # type: ignore[arg-type]


def test_gate_keeps_the_order_of_the_names() -> None:
    """顺序必须与文件一致——调用方按 ``plan_index`` 回配置取班次。"""
    assert suggest_shift_minutes(["6h", "12h", "6h"], expected_count=3) == (360, 720, 360)


def test_gate_supports_non_whole_hours_in_the_sum() -> None:
    """合计 24 小时的半点组合也算读得出（能不能存进配置是另一回事，见 roster）。"""
    assert suggest_shift_minutes(["8.5h", "8.5h", "7h"], expected_count=3) == (510, 510, 420)
