"""宿主「配置写回」路由的单测。

这是页面能改插件配置的**唯一入口**，所以重点盯安全边界：只允许合并已存在的
顶层键、值必须是对象、业务校验失败必须**回滚**（否则用户的配置文件会被一个
非法值永久污染，下次启动直接加载失败）。

宿主不认识「三班」——业务校验发生在模块侧（`apply_config`），这里验证的是
宿主对「失败」的处理是否诚实。
"""

import asyncio
from types import SimpleNamespace

import main as plugin_main
from modules.shift_reminder.module import JOB_PREFIX, ShiftReminderModule

CONFIG = {
    "modules": {"shift_reminder": True},
    "shift_reminder": {
        "shift_1_name": "早班",
        "shift_1_start": "08:00",
        "shift_1_hours": 12,
        "shift_2_name": "晚班",
        "shift_2_start": "20:00",
        "shift_2_hours": 6,
        "shift_3_name": "夜班",
        "shift_3_start": "02:00",
        "shift_3_hours": 6,
        "lead_minutes": 10,
    },
}


class FakeConfig(dict):
    """够用的 AstrBotConfig：能合并、能记录每次保存的内容。"""

    def __init__(self, initial):
        super().__init__(initial)
        self.saves = []

    async def save_config_async(self, replace_config=None):
        snapshot = dict(replace_config or {})
        self.saves.append(snapshot)
        if snapshot:
            self.update(snapshot)
        return True


class FakeJob:
    def __init__(self, name, job_id):
        self.name = name
        self.job_id = job_id


class FakeCronManager:
    """只实现模块用到的那三个方法。"""

    def __init__(self):
        self.jobs = []
        self.added = []

    async def list_jobs(self):
        return list(self.jobs)

    async def delete_job(self, job_id):
        self.jobs = [j for j in self.jobs if j.job_id != job_id]

    async def add_basic_job(self, **kwargs):
        job = FakeJob(kwargs["name"], f"job-{len(self.added) + 1}")
        self.added.append(kwargs["name"])
        self.jobs.append(job)
        return job


def _toolbox(config, payload):
    """造一个宿主实例，并把请求体塞给 stub request。"""
    ctx = SimpleNamespace(register_web_api=lambda *a, **k: None)
    plugin = plugin_main.ArknightsToolbox(ctx, config)

    async def fake_json(default=None):
        return payload if payload is not None else default

    plugin_main.request = SimpleNamespace(json=fake_json, query=SimpleNamespace(get=lambda *a, **k: None))
    return plugin


def test_rejects_unknown_top_level_section() -> None:
    """不许凭空新增配置段——那是往用户配置里塞垃圾。"""
    config = FakeConfig(CONFIG)
    plugin = _toolbox(config, {"brand_new_section": {"a": 1}})

    result = asyncio.run(plugin._web_save_config())

    assert result["status_code"] == 400
    assert "不允许新增配置段" in result["error"]
    assert config.saves == []


def test_rejects_non_mapping_value() -> None:
    config = FakeConfig(CONFIG)
    plugin = _toolbox(config, {"shift_reminder": "not-an-object"})

    result = asyncio.run(plugin._web_save_config())

    assert result["status_code"] == 400
    assert config.saves == []


def test_rejects_empty_body() -> None:
    config = FakeConfig(CONFIG)
    plugin = _toolbox(config, {})

    result = asyncio.run(plugin._web_save_config())

    assert result["status_code"] == 400
    assert config.saves == []


def test_reports_unsupported_when_config_cannot_save() -> None:
    """拿不到 save_config（老版本 AstrBot）时如实说 501，而不是假装成功。"""
    plugin = plugin_main.ArknightsToolbox(
        SimpleNamespace(register_web_api=lambda *a, **k: None), {"shift_reminder": {}}
    )
    plugin._config = {"shift_reminder": {}}  # 普通 dict：没有 save_config

    async def fake_json(default=None):
        return {"shift_reminder": {}}

    plugin_main.request = SimpleNamespace(json=fake_json, query=SimpleNamespace(get=lambda *a, **k: None))
    result = asyncio.run(plugin._web_save_config())

    assert result["status_code"] == 501


def test_saves_and_applies_valid_config(monkeypatch, tmp_path) -> None:
    """合法配置：写回、通知模块重建任务、回报已生效。"""
    monkeypatch.setattr(
        "modules.shift_reminder.module.get_astrbot_plugin_data_path", lambda: str(tmp_path)
    )

    config = FakeConfig(CONFIG)
    plugin = _toolbox(config, {"shift_reminder": dict(CONFIG["shift_reminder"], lead_minutes=30)})

    # 真起一个模块，才能验「通知到了」而不只是「函数被调过」
    cron = FakeCronManager()
    module = ShiftReminderModule()
    asyncio.run(module.initialize(SimpleNamespace(cron_manager=cron), CONFIG["shift_reminder"]))
    plugin._registry = [module]

    result = asyncio.run(plugin._web_save_config())

    assert result == {"saved": True, "applied": True}
    assert len(config.saves) == 1
    assert module._lead_minutes == 30
    # 任务按新提前量重建：早班 08:00 − 30 分 = 07:30
    assert any(name == f"{JOB_PREFIX}早班" for name in cron.added)


def test_rolls_back_when_business_validation_fails(monkeypatch, tmp_path) -> None:
    """业务校验失败时必须**回滚配置**——否则坏值留在盘上，下次启动直接挂。"""
    monkeypatch.setattr(
        "modules.shift_reminder.module.get_astrbot_plugin_data_path", lambda: str(tmp_path)
    )

    config = FakeConfig(CONFIG)
    # 三段时长合计 23 小时：模块侧会拒绝
    bad_section = dict(CONFIG["shift_reminder"], shift_1_hours=11)
    plugin = _toolbox(config, {"shift_reminder": bad_section})

    cron = FakeCronManager()
    module = ShiftReminderModule()
    asyncio.run(module.initialize(SimpleNamespace(cron_manager=cron), CONFIG["shift_reminder"]))
    plugin._registry = [module]

    result = asyncio.run(plugin._web_save_config())

    assert result["status_code"] == 400
    assert "已拒绝保存" in result["error"]
    # 两次保存：先写新值、失败后写回旧值
    assert len(config.saves) == 2
    assert config.saves[-1] == {"shift_reminder": CONFIG["shift_reminder"]}
    assert config["shift_reminder"] == CONFIG["shift_reminder"]
    # 运行中的任务没被拆掉
    assert len(module._job_ids) == 3
