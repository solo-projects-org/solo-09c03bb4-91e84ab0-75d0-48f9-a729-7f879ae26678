"""生成长链回归样例 samples/long_chain_sample.csv（仅标准库）。

包含两个机尾：
- LONG-1200：1200 段完全无歧义、可衔接的航段（Asia/Shanghai，无 DST），
  旧递归 DFS 会直接 RecursionError，新迭代 DP 应快速给出唯一解；
- LONG-1200-CONFLICT：同样的 1200 段后追加一个必然无法衔接的第 1201 段，
  用于验证长链无解也能快速返回并准确定位冲突航段。

运行：python3 tests/make_long_chain_sample.py
"""
from datetime import datetime, timedelta
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "samples" / "long_chain_sample.csv"
HEADER = ("tail_number,seq,dep_local,dep_tz,arr_local,arr_tz,"
          "duration_min_minutes,duration_max_minutes\n")
N = 1200


def main() -> None:
    lines = [HEADER]
    base = datetime(2026, 1, 1, 8, 0)
    for i in range(1, N + 1):
        dep = base + timedelta(days=i - 1)
        arr = dep + timedelta(hours=2)
        lines.append(
            f"LONG-1200,{i},{dep:%Y-%m-%d %H:%M},Asia/Shanghai,"
            f"{arr:%Y-%m-%d %H:%M},Asia/Shanghai,100,140\n"
        )
        # 冲突机尾：前 1200 段与上面完全一致
        lines.append(
            f"LONG-1200-CONFLICT,{i},{dep:%Y-%m-%d %H:%M},Asia/Shanghai,"
            f"{arr:%Y-%m-%d %H:%M},Asia/Shanghai,100,140\n"
        )
    # 第 1201 段：起飞时间在链首之前，任何前缀都无法衔接
    clash_dep = datetime(2025, 12, 1, 9, 0)
    clash_arr = clash_dep + timedelta(hours=1)
    lines.append(
        f"LONG-1200-CONFLICT,{N + 1},"
        f"{clash_dep:%Y-%m-%d %H:%M},Asia/Shanghai,"
        f"{clash_arr:%Y-%m-%d %H:%M},Asia/Shanghai,40,80\n"
    )
    OUT.write_text("".join(lines), encoding="utf-8")
    print(f"written {OUT} ({len(lines) - 1} data rows)")


if __name__ == "__main__":
    main()
