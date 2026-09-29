/**
 * overviewTones 的单测（Node 原生断言，无需任何依赖）。
 *
 * 为什么单独用 Node 测：这是个前端纯函数，而项目的 pytest 只跑 Python。
 * 它可以被 node 直接 import（app.js 是纯 ESM，无顶层副作用），所以不引
 * 测试框架也能测——与本项目「零运行时依赖」的一贯取舍一致。
 *
 * 跑法：
 *   node tests/js/overview-tones.mjs
 *
 * 注意：CI（.github/workflows/ci.yml）目前只跑 Python，**这条需要单独加一步**
 * （见本次回报的「下一步建议」）。
 */
import assert from "node:assert/strict";

import { overviewTones } from "../../pages/shift-reminder/app.js";

const cases = [
  {
    name: "全部正常：已导入 + 已绑定 + 未熔断",
    input: { rosterImported: true, bound: true, breakerOpen: false },
    expect: { current: "is-info", roster: "is-good", binding: "is-good", push: "is-good" },
  },
  {
    name: "未绑定 -> 需要留意（提醒根本发不出去）",
    input: { rosterImported: true, bound: false, breakerOpen: false },
    expect: { current: "is-info", roster: "is-good", binding: "is-warn", push: "is-good" },
  },
  {
    name: "未导入排班表 -> 只算信息，不算错误（不导入也能用）",
    input: { rosterImported: false, bound: true, breakerOpen: false },
    expect: { current: "is-info", roster: "is-info", binding: "is-good", push: "is-good" },
  },
  {
    name: "熔断打开 -> 必须显眼（推送已经停了）",
    input: { rosterImported: true, bound: true, breakerOpen: true },
    expect: { current: "is-info", roster: "is-good", binding: "is-good", push: "is-bad" },
  },
  {
    name: "全空 -> 不崩、给出保守色调",
    input: {},
    expect: { current: "is-info", roster: "is-info", binding: "is-warn", push: "is-good" },
  },
  {
    name: "入参为 undefined -> 不崩",
    input: undefined,
    expect: { current: "is-info", roster: "is-info", binding: "is-warn", push: "is-good" },
  },
  {
    name: "当前班次永远是中立色（它是信息，不是状态告警）",
    input: { rosterImported: false, bound: false, breakerOpen: true },
    expect: { current: "is-info", roster: "is-info", binding: "is-warn", push: "is-bad" },
  },
];

let failed = 0;
for (const c of cases) {
  try {
    assert.deepEqual(overviewTones(c.input), c.expect);
    console.log("  PASS", c.name);
  } catch (err) {
    failed += 1;
    console.error("  FAIL", c.name);
    console.error("       ", err.message);
  }
}

// 返回值必须是「新对象」，调用方不该能改坏内部状态。
const first = overviewTones({ rosterImported: true, bound: true, breakerOpen: false });
first.roster = "tampered";
const second = overviewTones({ rosterImported: true, bound: true, breakerOpen: false });
try {
  assert.equal(second.roster, "is-good");
  console.log("  PASS 返回值互不影响");
} catch (err) {
  failed += 1;
  console.error("  FAIL 返回值互不影响");
  console.error("       ", err.message);
}

if (failed > 0) {
  console.error(`\n${failed} case(s) FAILED`);
  process.exit(1);
}
console.log(`\nALL ${cases.length + 1} OVERVIEW-TONE CHECKS PASSED`);
