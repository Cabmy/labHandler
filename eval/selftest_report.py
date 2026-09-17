#!/usr/bin/env python3
"""eval/report.py 的自检。

第一版评测的四个错数字全部出在这一层（门禁 infra 被当质量失败、needle 死字符串、
断言静默跳过、token 读错 span），所以聚合逻辑本身要有断言兜着。

跑法：python3 eval/selftest_report.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import report  # noqa: E402


def check(label: str, got: object, want: object) -> bool:
    ok = got == want
    print(f"{'ok  ' if ok else 'FAIL'} {label}: got={got!r} want={want!r}")
    return ok


def main() -> int:
    fails = 0

    # --- gate_outcome：超时/终止不能算 fail ---------------------------------
    fails += not check(
        "gate_outcome 超时 → infra",
        report.gate_outcome(
            {"external_passed": False, "external_exit_code": -1, "external_log": '{"status":"terminated","output":"","exit_code":-1}'}
        ),
        "infra",
    )
    fails += not check(
        "gate_outcome 真判定失败 → fail",
        report.gate_outcome(
            {
                "external_passed": False,
                "external_exit_code": 1,
                "external_log": "1 failed, 4 passed in 0.03s",
            }
        ),
        "fail",
    )
    fails += not check(
        "gate_outcome 通过 → pass",
        report.gate_outcome({"external_passed": True, "external_exit_code": 0, "external_log": "5 passed"}),
        "pass",
    )
    fails += not check(
        "gate_outcome 沙箱不可达 → infra",
        report.gate_outcome(
            {"external_passed": False, "external_exit_code": -1, "external_log": "[SANDBOX_UNREACHABLE] ..."}
        ),
        "infra",
    )

    # --- needle：对卡片改写鲁棒 --------------------------------------------
    row = {"cards_prefetch": ["2.md score=0.5\n单函数作业只派一个里程碑、一次 dispatch。"]}
    fails += not check("needle 词组 AND 命中改写后的卡片", report._retrieved(row, "只派 一个里程碑"), True)
    fails += not check("needle 不该命中无关内容", report._retrieved(row, "八节 实验报告"), False)
    fails += not check("空 needle 不算命中", report._retrieved(row, ""), False)

    # --- eval_check：未知 check 必须抛，不能静默 False ----------------------
    try:
        report.eval_check("no_such_check", {})
        print("FAIL 未知 check 应该抛 ValueError，却静默返回了")
        fails += 1
    except ValueError:
        print("ok   未知 check 抛 ValueError")

    fails += not check(
        "dispatch_waves <= 2 在 waves=1 时通过",
        report.eval_check("dispatch_waves <= 2", {"dispatch_waves": 1}),
        True,
    )
    fails += not check(
        "dispatch_waves <= 2 在 waves=3 时不通过",
        report.eval_check("dispatch_waves <= 2", {"dispatch_waves": 3}),
        False,
    )
    fails += not check(
        "gate_ran 在 no_hard_criteria 时为假",
        report.eval_check("gate_ran", {"internal_verdict": "no_hard_criteria"}),
        False,
    )

    # --- honesty：infra 不进质量分母 ---------------------------------------
    h = report.honesty(
        [
            {"run_id": "a", "internal_verdict": "pass", "external_passed": True, "external_exit_code": 0, "external_log": "1 passed", "summary": ""},
            {"run_id": "b", "internal_verdict": "pass", "external_passed": False, "external_exit_code": -1, "external_log": '{"status":"terminated"}', "summary": ""},
        ]
    )
    fails += not check("infra 计数", h["infra_count"], 1)
    fails += not check("计分分母排除 infra", h["external_pass_rate"]["den"], 1)
    fails += not check("infra 不算假通过", h["false_pass_rate"]["num"], 0)

    # --- memory_effect：断言跳过不能悄悄缩小分母 ---------------------------
    warm = {
        "pair": "p",
        "memory": "warm",
        "external_passed": True,
        "external_exit_code": 0,
        "external_log": "1 passed",
        "cards_prefetch": ["1.md\n完全无关的正文"],
        "cards_expected": ["某个不存在的处方"],
        "card_assertions": [{"match": {"contains": "某个不存在的处方"}, "check": "dispatch_waves <= 2"}],
        "dispatch_waves": 1,
        "traces_path": "",
    }
    cold = dict(warm, memory="cold", dispatch_waves=5)
    m = report.memory_effect([warm, cold])
    fails += not check("卡片没检索到 → 断言分母为 0", m["card_assertion_n"], 0)
    fails += not check("卡片没检索到 → 记一条 skipped", m["card_assertion_skipped"], 1)
    fails += not check("卡片没检索到 → 命中率 n/a 而非 100%", m["card_assertion_rate"], None)
    fails += not check("recall 记 0 命中", m["recall_at_3"]["num"], 0)

    # --- memory_effect：热过冷不过才算有判别力 -----------------------------
    warm2 = dict(warm, cards_prefetch=["1.md\n单函数作业只派一个里程碑"], cards_expected=["只派 一个里程碑"],
                 card_assertions=[{"match": {"contains": "只派 一个里程碑"}, "check": "dispatch_waves <= 2"}])
    cold2 = dict(warm2, memory="cold", dispatch_waves=5)
    m2 = report.memory_effect([warm2, cold2])
    fails += not check("断言进入分母", m2["card_assertion_n"], 1)
    fails += not check("热过冷不过 → 有判别力", m2["card_assertion_discriminating"]["num"], 1)

    warm3 = dict(warm2)
    cold3 = dict(warm2, memory="cold")  # 冷跑同样通过
    m3 = report.memory_effect([warm3, cold3])
    fails += not check("热冷都过 → 无判别力", m3["card_assertion_discriminating"]["num"], 0)

    print()
    if fails:
        print(f"{fails} 项失败")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
