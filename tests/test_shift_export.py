"""`shift_reminder` 导出共享班次表：内容、时机、失败降级、以及"写入方唯一"。

这一包守的是**跨模块那条通道的写入端**。三条性质最要紧：

1. **写入方唯一**——全仓只有一个地方写这份文件。两份实现会让"什么时候该换班"
   变成两个答案，而漂移的代价是 MAA 在错误时刻被触发、班次永久跳一班
   （`docs/project-plan/10-maa-shift-switching.md` §4.3）。
2. **导出失败不许拖垮提醒**——它是给另一个模块的便利，不是核心功能
   （项目宪法 §2 第 7 条那条铁律的另一种形态）。
3. **导出的是已校验的表**——调用点都在 `parse_shift_table` 成功之后，
   所以文件里永远不会出现"非法但被写下去"的配置。
"""

from __future__ import annotations

import ast
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from core.shift_share import SHARED_SHIFT_FILENAME, load_shared, shared_shift_path
from modules.shift_reminder import module as reminder_module
from modules.shift_reminder.module import ShiftReminderModule

REPO = Path(__file__).resolve().parent.parent

CONFIG = {
    "shift_1_name": "第 1 班",
    "shift_1_start": "08:00",
    "shift_1_hours": 12,
    "shift_2_name": "第 2 班",
    "shift_2_start": "20:00",
    "shift_2_hours": 6,
    "shift_3_name": "第 3 班",
    "shift_3_start": "02:00",
    "shift_3_hours": 6,
    "lead_minutes": 10,
    "timezone": "Asia/Shanghai",
}

SWAPPED = {
    **CONFIG,
    "shift_1_start": "20:00",
    "shift_1_hours": 6,
    "shift_2_start": "02:00",
    "shift_2_hours": 6,
    "shift_3_start": "08:00",
    "shift_3_hours": 12,
}


def _job(name: str, job_id: str) -> SimpleNamespace:
    return SimpleNamespace(name=name, job_id=job_id)


class _FakeCronManager:
    """只实现本模块用到的那三个方法。"""

    def __init__(self) -> None:
        self._jobs: list[SimpleNamespace] = []
        self.deleted: list[str] = []
        self.added: list[str] = []

    async def list_jobs(self):
        return list(self._jobs)

    async def delete_job(self, job_id: str) -> None:
        self.deleted.append(job_id)
        self._jobs = [job for job in self._jobs if job.job_id != job_id]

    async def add_basic_job(self, **kwargs):
        new = _job(str(kwargs["name"]), f"new-{len(self.added) + 1}")
        self.added.append(str(kwargs["name"]))
        self._jobs.append(new)
        return new


def _boot(monkeypatch, tmp_path, config: dict | None = None):
    """把提醒模块跑起来，返回 (实例, cron 替身, 数据目录)。"""
    monkeypatch.setattr(reminder_module, "get_astrbot_plugin_data_path", lambda: str(tmp_path))
    cron = _FakeCronManager()
    instance = ShiftReminderModule()
    asyncio.run(instance.initialize(SimpleNamespace(cron_manager=cron), config or CONFIG))
    return instance, cron, tmp_path / reminder_module.PLUGIN_NAME


def _logs(monkeypatch) -> list[str]:
    sink: list[str] = []

    def _render(args):
        if not args:
            return ""
        template = str(args[0])
        if len(args) > 1:
            try:
                return template % tuple(args[1:])
            except Exception:  # noqa: BLE001 - 格式化失败就退回拼接
                pass
        return " ".join(str(arg) for arg in args)

    for level in ("info", "warning", "error", "exception", "debug"):
        monkeypatch.setattr(
            reminder_module.logger, level, lambda *a, _s=sink, **k: _s.append(_render(a))
        )
    return sink


# --- 内容 -------------------------------------------------------------------


def test_initialize_writes_a_readable_shared_table(monkeypatch, tmp_path) -> None:
    """启动时就写出来，而且**读的人拿到的东西是对的**。

    用读取方那套函数读（`load_shared`），不手工断 JSON 结构——那样才能证明
    "写出去的东西真的能被读回来"。
    """
    _instance, _cron, data_dir = _boot(monkeypatch, tmp_path)

    shared = load_shared(json.loads((data_dir / SHARED_SHIFT_FILENAME).read_text("utf-8")))

    assert shared.names == ("第 1 班", "第 2 班", "第 3 班")
    assert [shift.duration_minutes for shift in shared.shifts] == [720, 360, 360]
    assert shared.timezone == "Asia/Shanghai"
    assert shared.generated_at, "写的时候要留下时刻，排障时要知道这份表是什么时候写的"


def test_the_exported_file_sits_beside_the_module_s_own_state(monkeypatch, tmp_path) -> None:
    """放在插件数据目录（`plugin_data/<插件>/`），**不是**插件自身目录。

    官方硬约束：插件目录会在更新/重装时被覆盖，用户数据不能放那儿。
    """
    _instance, _cron, data_dir = _boot(monkeypatch, tmp_path)

    assert (data_dir / SHARED_SHIFT_FILENAME).is_file()
    assert data_dir.parent == tmp_path, "应当落在 <数据根>/<插件名>/ 里"


def test_the_shared_path_matches_what_the_export_wrote(monkeypatch, tmp_path) -> None:
    """写入方与读取方必须**算出同一个路径**——各拼一次就会拼岔，而那是静默的。"""
    _instance, _cron, data_dir = _boot(monkeypatch, tmp_path)

    assert shared_shift_path(tmp_path, reminder_module.PLUGIN_NAME) == (
        data_dir / SHARED_SHIFT_FILENAME
    )


def test_slot_order_is_preserved_not_sorted_by_time(monkeypatch, tmp_path) -> None:
    """槽位顺序 = 排班表 `plans` 下标对应的依据，**不能**按时刻排序。

    第 3 班是 02:00（最早），按时刻排会跑到最前——那会让"第 i 班配了哪一班"错位。
    """
    _instance, _cron, data_dir = _boot(monkeypatch, tmp_path)

    shared = load_shared(json.loads((data_dir / SHARED_SHIFT_FILENAME).read_text("utf-8")))

    assert [shift.name for shift in shared.shifts] == ["第 1 班", "第 2 班", "第 3 班"]
    assert [shift.name for shift in shared.table.shifts] == ["第 3 班", "第 1 班", "第 2 班"]


def test_apply_config_rewrites_the_table_so_the_other_module_sees_the_new_times(
    monkeypatch, tmp_path
) -> None:
    """改了时刻就要立刻让另一个模块看到。

    不重写的话，`maa` 会按**旧**时刻询问——「两份时刻漂移」那个 bug 的时间维度版本。
    """
    instance, _cron, data_dir = _boot(monkeypatch, tmp_path)

    asyncio.run(instance.apply_config(SWAPPED))

    shared = load_shared(json.loads((data_dir / SHARED_SHIFT_FILENAME).read_text("utf-8")))
    assert [shift.duration_minutes for shift in shared.shifts] == [360, 360, 720]
    assert [shift.start_minute for shift in shared.shifts] == [20 * 60, 2 * 60, 8 * 60]


# --- 失败降级：不许拖垮提醒 --------------------------------------------------


def test_export_failure_is_loud_but_does_not_stop_the_reminder(monkeypatch, tmp_path) -> None:
    """导出写不进去时：**提醒照常注册**，只多一条 WARNING，也不留下半个文件。

    为什么值得单独钉：这是"给另一个模块的便利"能出的最坏情况——它必须**响**
    （用户才知道 MAA 那边不会问了），但**不许**把核心功能一起拖下去。
    用真实的写盘失败（`write_json_atomic` 抛 OSError）来构造，而不是打桩一个假的
    "导出失败"标记——那样测的是我的桩，不是代码。
    """
    monkeypatch.setattr(reminder_module, "get_astrbot_plugin_data_path", lambda: str(tmp_path))
    logs = _logs(monkeypatch)

    def _boom(*_args, **_kwargs):
        raise OSError("磁盘满了")

    monkeypatch.setattr(reminder_module, "write_json_atomic", _boom)
    cron = _FakeCronManager()

    instance = ShiftReminderModule()
    asyncio.run(instance.initialize(SimpleNamespace(cron_manager=cron), CONFIG))

    assert len(cron.added) == 3, "导出失败不许影响提醒的注册"
    assert len(instance._job_ids) == 3
    assert any("导出共享班次表失败" in line for line in logs), "失败了必须留痕"
    assert not (tmp_path / reminder_module.PLUGIN_NAME / SHARED_SHIFT_FILENAME).exists()


def test_the_export_never_runs_before_validation(monkeypatch, tmp_path) -> None:
    """非法配置**写不出任何文件**：调用点在校验之后，这是顺序保证，不是巧合。

    写出一份非法配置比不写坏得多——另一个模块会拿它去算换班时刻。
    """
    monkeypatch.setattr(reminder_module, "get_astrbot_plugin_data_path", lambda: str(tmp_path))
    bad = {**CONFIG, "shift_1_hours": 5}  # 合计 17 小时，校验必失败
    try:
        asyncio.run(
            ShiftReminderModule().initialize(SimpleNamespace(cron_manager=_FakeCronManager()), bad)
        )
    except Exception:  # noqa: BLE001 - 具体异常类型不是本用例的关注点
        pass

    assert not (tmp_path / reminder_module.PLUGIN_NAME / SHARED_SHIFT_FILENAME).exists()


# --- 写入方唯一 -------------------------------------------------------------


def test_only_one_module_writes_the_shared_table() -> None:
    """**全仓只有一个地方写这份文件。**

    用 AST 找 `write_json_atomic` 的调用点，只允许出现在两处：
    `core/storage.py`（它是定义者，也用它实现键值存档）与
    `modules/shift_reminder/module.py`（唯一的写入方）。

    ⚠️ 这条护栏抓的是"新加一个写入方"（最可能的走法就是调用共用工具）。
    真有人手搓 `open(path,'w')` 绕过它，这条看不见——所以下面还有一条
    **行为上**的用例（另一个模块跑一轮之后文件内容不许变）作补充。
    """
    allowed = {"core.storage", "modules.shift_reminder.module"}
    writers: set[str] = set()
    for path in sorted((REPO / "core").rglob("*.py")) + sorted((REPO / "modules").rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            if name != "write_json_atomic":
                continue
            writers.add(".".join(path.relative_to(REPO).with_suffix("").parts))

    assert writers <= allowed, f"多了一处写入共享班次表的地方：{sorted(writers - allowed)}"
    assert "modules.shift_reminder.module" in writers, "写入方不见了？那这份表就没人写了"


def test_the_reader_never_modifies_the_shared_file(monkeypatch, tmp_path) -> None:
    """另一个模块（`maa`）跑一轮 tick 之后，**文件内容一个字节都不许变**。

    这是"写入方唯一"的行为侧证据：读的人只读。
    """
    from modules.maa import module as maa_module

    path = shared_shift_path(tmp_path, reminder_module.PLUGIN_NAME)
    path.parent.mkdir(parents=True, exist_ok=True)
    reminder = ShiftReminderModule()
    monkeypatch.setattr(reminder_module, "get_astrbot_plugin_data_path", lambda: str(tmp_path))
    asyncio.run(reminder.initialize(SimpleNamespace(cron_manager=_FakeCronManager()), CONFIG))
    before = path.read_bytes()

    monkeypatch.setattr(maa_module, "get_astrbot_plugin_data_path", lambda: str(tmp_path))
    reader = maa_module.MaaModule()
    asyncio.run(reader.initialize(SimpleNamespace(register_web_api=lambda *a, **k: None), {}))
    for _ in range(3):
        asyncio.run(reader._auto_ask_tick())

    assert path.read_bytes() == before
