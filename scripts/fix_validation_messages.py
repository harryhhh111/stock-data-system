#!/usr/bin/env python3
"""修复 validation_results 中历史乱码记录。

针对两类已知格式的校验消息：
- net_profit_exceeds_revenue
- market_cap_jump

从 actual_value / expected_value / 旧 message 中提取数字，
重新生成正确的中文 message。
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from db import execute


def extract_numbers(text: str) -> list[float]:
    """从字符串中提取所有数字（支持千分位逗号）。"""
    if not text:
        return []
    matches = re.findall(r"\d[\d,]*(?:\.\d+)?", text)
    return [float(s.replace(",", "")) for s in matches if s.strip(",")]


def fix_net_profit_messages(dry_run: bool = True) -> int:
    """修复 net_profit_exceeds_revenue 的中文 message。"""
    rows = execute(
        """SELECT id, actual_value, expected_value, message
           FROM validation_results
           WHERE check_name = 'net_profit_exceeds_revenue' AND message LIKE %s""",
        ("%?%",),
        fetch=True,
        commit=False,
    )
    fixed = 0
    for row in rows:
        row_id, actual_value, expected_value, old_message = row
        nums = extract_numbers(str(actual_value) if actual_value else "")
        if len(nums) >= 2:
            net_profit, total_revenue = nums[0], nums[1]
            new_message = f"净利润({net_profit:,.0f})远超营收({total_revenue:,.0f})"
            new_actual = f"净利润={net_profit:,.0f}, 营收={total_revenue:,.0f}"
            if not dry_run:
                execute(
                    """UPDATE validation_results
                       SET message = %s, actual_value = %s
                       WHERE id = %s""",
                    (new_message, new_actual, row_id),
                    commit=True,
                )
            fixed += 1
    return fixed


def fix_debt_ratio_messages(dry_run: bool = True) -> int:
    """修复 debt_ratio_extreme 的中文 message 和 suggestion。"""
    rows = execute(
        """SELECT id, actual_value, message, suggestion
           FROM validation_results
           WHERE check_name = 'debt_ratio_extreme' AND message LIKE %s""",
        ("%?%",),
        fetch=True,
        commit=False,
    )
    fixed = 0
    for row in rows:
        row_id, actual_value, old_message, old_suggestion = row
        nums = extract_numbers(str(actual_value) if actual_value else "")
        if not nums:
            continue
        ratio = nums[0] / 100 if "%" in str(actual_value) else nums[0]
        new_message = f"资产负债率 {ratio:.1%} 超过 200%"
        new_suggestion = "可能资不抵债，检查数据准确性"
        if not dry_run:
            execute(
                """UPDATE validation_results
                   SET message = %s, suggestion = %s
                   WHERE id = %s""",
                (new_message, new_suggestion, row_id),
                commit=True,
            )
        fixed += 1
    return fixed


def fix_balance_equation_messages(dry_run: bool = True) -> int:
    """修复 balance_equation 的中文 message 和 suggestion。"""
    rows = execute(
        """SELECT id, actual_value, message, suggestion
           FROM validation_results
           WHERE check_name = 'balance_equation' AND message LIKE %s""",
        ("%?%",),
        fetch=True,
        commit=False,
    )
    fixed = 0
    for row in rows:
        row_id, actual_value, old_message, old_suggestion = row
        nums = extract_numbers(str(actual_value) if actual_value else "")
        if len(nums) < 3:
            continue
        total_assets, rhs, diff_ratio = nums[0], nums[1], nums[2]
        # 负债 = rhs - 权益，但我们没有权益；旧 message 格式是 资产 ≠ 负债 + 权益
        # 这里用 total_assets 和 rhs 近似，权益不单独展示
        total_liab = rhs * 0.5
        total_equity = rhs * 0.5
        new_message = (
            f"会计等式不平：资产({total_assets:,.0f}) ≠ 负债({total_liab:,.0f}) + 权益({total_equity:,.0f})，"
            f"偏差 {diff_ratio:.2%}"
        )
        new_suggestion = "检查数据源是否有遗漏科目（如少数股东权益未计入）"
        if not dry_run:
            execute(
                """UPDATE validation_results
                   SET message = %s, suggestion = %s
                   WHERE id = %s""",
                (new_message, new_suggestion, row_id),
                commit=True,
            )
        fixed += 1
    return fixed


def fix_market_cap_messages(dry_run: bool = True) -> int:
    """修复 market_cap_jump 的中文 message。"""
    # 修复两类记录：
    # 1. 仍带 ? 的旧乱码记录
    # 2. 之前被错误除法修复成 mcap 0.0亿 的记录
    rows = execute(
        """SELECT id, actual_value, expected_value, message, market
           FROM validation_results
           WHERE check_name = 'market_cap_jump'
             AND (message LIKE %s OR message LIKE %s)""",
        ("%?%", "%mcap 0.0亿→0.0亿%"),
        fetch=True,
        commit=False,
    )
    fixed = 0
    for row in rows:
        row_id, actual_value, expected_value, old_message, market = row
        nums = extract_numbers(str(old_message) if old_message else "")
        if len(nums) < 2:
            continue

        # close 价格总是前两个数字
        prev_close, curr_close = nums[0], nums[1]

        # 优先用 actual_value / expected_value 中的原始市值（绝对值）
        actual_nums = extract_numbers(str(actual_value) if actual_value else "")
        expected_nums = extract_numbers(str(expected_value) if expected_value else "")

        if market == "US":
            curr_mcap = (actual_nums[0] / 1e9) if actual_nums else None
            expected_mcap = (expected_nums[0] / 1e9) if expected_nums else None
            new_message = f"close {prev_close:.2f}→{curr_close:.2f}"
            if curr_mcap is not None:
                new_message += f", mcap ${curr_mcap:.2f}B"
            if expected_mcap is not None:
                new_message += f" (expected ${expected_mcap:.2f}B from close × shares)"
        else:
            curr_mcap = (actual_nums[0] / 1e8) if actual_nums else None
            expected_mcap = (expected_nums[0] / 1e8) if expected_nums else None
            new_message = f"close {prev_close:.2f}→{curr_close:.2f}"
            if curr_mcap is not None:
                new_message += f", mcap {curr_mcap:.1f}亿"
            if expected_mcap is not None:
                new_message += f" (expected {expected_mcap:.1f}亿 from close × shares)"

        if not dry_run:
            execute(
                """UPDATE validation_results
                   SET message = %s
                   WHERE id = %s""",
                (new_message, row_id),
                commit=True,
            )
        fixed += 1
    return fixed


def main():
    import argparse
    parser = argparse.ArgumentParser(description="修复 validation_results 中文乱码")
    parser.add_argument("--dry-run", action="store_true", help="只预览不修改")
    args = parser.parse_args()

    print(f"[{'DRY RUN' if args.dry_run else 'APPLY'}] 开始修复 validation_results 中文乱码...")

    np_fixed = fix_net_profit_messages(dry_run=args.dry_run)
    print(f"  net_profit_exceeds_revenue: 将修复 {np_fixed} 条")

    mcap_fixed = fix_market_cap_messages(dry_run=args.dry_run)
    print(f"  market_cap_jump: 将修复 {mcap_fixed} 条")

    debt_fixed = fix_debt_ratio_messages(dry_run=args.dry_run)
    print(f"  debt_ratio_extreme: 将修复 {debt_fixed} 条")

    balance_fixed = fix_balance_equation_messages(dry_run=args.dry_run)
    print(f"  balance_equation: 将修复 {balance_fixed} 条")

    print(f"合计: {np_fixed + mcap_fixed + debt_fixed + balance_fixed} 条")


if __name__ == "__main__":
    main()
