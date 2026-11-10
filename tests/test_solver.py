"""求解器自检：直接运行 `python3 tests/test_solver.py`（仅依赖标准库 + tzdata）。"""
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.solver import (  # noqa: E402
    Combo, Segment, build_export_csv, solve_chain, solve_csv, utc_candidates,
)

SAMPLE = Path(__file__).resolve().parents[1] / "samples" / "dst_sample.csv"
LONG_SAMPLE = Path(__file__).resolve().parents[1] / "samples" / "long_chain_sample.csv"

Z = datetime(2026, 11, 1, tzinfo=timezone.utc)
HEADER = ("tail_number,seq,dep_local,dep_tz,arr_local,arr_tz,"
          "duration_min_minutes,duration_max_minutes\n")


def check(name, cond):
    print(("PASS" if cond else "FAIL"), "-", name)
    if not cond:
        raise SystemExit(1)


def main():
    # 1) 时区候选映射
    cands, err = utc_candidates(datetime(2026, 10, 25, 2, 30), "Europe/Berlin")
    check("柏林拨回：重复时刻保留两个候选", err is None and len(cands) == 2)

    cands, err = utc_candidates(datetime(2026, 3, 29, 2, 30), "Europe/Berlin")
    check("柏林拨快：不存在时刻报错", err == "nonexistent")

    cands, err = utc_candidates(datetime(2026, 6, 1, 12, 0), "Europe/Berlin")
    check("普通时刻：唯一候选", err is None and len(cands) == 1)

    cands, err = utc_candidates(datetime(2026, 1, 1, 0, 0), "Mars/Olympus")
    check("非法 IANA 时区", err == "unknown_tz")

    # 2) 样例整体求解
    result = solve_csv(SAMPLE.read_text(encoding="utf-8"))
    by_tail = {t["tail"]: t for t in result["tails"]}

    check("B-7001 整链唯一解", by_tail["B-7001"]["status"] == "unique")
    check("B-7002 不存在时刻 → error", by_tail["B-7002"]["status"] == "error")
    check("B-7003 链断裂 → no_solution", by_tail["B-7003"]["status"] == "no_solution")
    check("B-7003 冲突定位在 seq=2",
          by_tail["B-7003"]["conflict"]["segment_seq"] == 2)
    check("B-7004 多解", by_tail["B-7004"]["status"] == "multiple")
    check("B-7004 恰好 2 条候选链", by_tail["B-7004"]["solution_count"] == 2)
    check("B-7005 整链唯一解", by_tail["B-7005"]["status"] == "unique")

    # 3) 唯一解内容校验（B-7001 第一段：伦敦 00:30 BST → 23:30Z；
    #    柏林 02:15 双候选 00:15Z/01:15Z，时长 45/165 分钟，区间 [90,130] 只留下 01:15Z）
    sol1 = by_tail["B-7001"]["solution"][0]
    check("B-7001 seg1 起飞 UTC", sol1["dep_utc"] == "2026-10-24T23:30:00Z")
    check("B-7001 seg1 落地 UTC", sol1["arr_utc"] == "2026-10-25T01:15:00Z")

    # 4) 导出只包含唯一解机尾
    csv_out = build_export_csv(result, ["B-7001", "B-7004", "B-7005"])
    lines = [l for l in csv_out.strip().splitlines()]
    check("导出 CSV 行数 = 表头 + B-7001(3) + B-7005(2)", len(lines) == 1 + 3 + 2)
    check("导出 CSV 不含多解机尾 B-7004", "B-7004" not in csv_out)

    # 5) 长链回归（旧递归 DFS 在 1000 段即 RecursionError）
    check("长链样例文件存在", LONG_SAMPLE.exists())
    t0 = time.time()
    long_result = solve_csv(LONG_SAMPLE.read_text(encoding="utf-8"))
    long_dt = time.time() - t0
    long_tails = {t["tail"]: t for t in long_result["tails"]}
    check("1200 段无歧义长链判定 unique",
          long_tails["LONG-1200"]["status"] == "unique")
    check("1200 段解长度为 1200",
          len(long_tails["LONG-1200"]["solution"]) == 1200)
    check(f"长链求解在 2 秒内完成（实际 {long_dt:.3f}s）", long_dt < 2.0)
    check("1200 段 + 末尾冲突判定 no_solution",
          long_tails["LONG-1200-CONFLICT"]["status"] == "no_solution")
    check("末尾冲突定位在最后一个航段 seq=1201",
          long_tails["LONG-1200-CONFLICT"]["conflict"]["segment_seq"] == 1201)

    # 6) 指数分叉链：26 段各 2 个可衔接 combo（2^26 条路径），必须精确分类
    branched = [_branched_segment(i) for i in range(1, 27)]
    t0 = time.time()
    status, payload = solve_chain(branched)
    branched_dt = time.time() - t0
    check(f"26 段指数链判定 multiple（{branched_dt:.3f}s）", status == "multiple")
    check("路径数饱和到 201 而非把中断当结论",
          payload["solution_count"] == 201)
    check("展示候选限量 200 条", len(payload["solutions"]) == 200)
    check("指数链多解判定在 1 秒内完成", branched_dt < 1.0)

    # 7) 同样的指数链追加末尾冲突：旧实现会全枚举 2^26 节点后长时间不返回
    conflict = branched + [_conflict_segment(27)]
    t0 = time.time()
    status, payload = solve_chain(conflict)
    conflict_dt = time.time() - t0
    check(f"指数链+末尾冲突判定 no_solution（{conflict_dt:.3f}s）",
          status == "no_solution")
    check("冲突航段定位 seq=27",
          payload["conflict"]["segment_seq"] == 27)
    check("冲突类型 chain_order", payload["conflict"]["type"] == "chain_order")
    check("无解判定不得做指数搜索（< 1 秒）", conflict_dt < 1.0)

    # 8) 两条完整 UTC 链即使末段落地时刻相同，仍算多解
    d0 = Z
    s1 = Segment(
        row_number=1, seq=1, dep_local_raw="x", dep_tz="UTC",
        arr_local_raw="y", arr_tz="UTC", dur_min=0, dur_max=300,
        dep_candidates=[d0, d0 + timedelta(hours=1)],
        arr_candidates=[d0 + timedelta(hours=2)],
        combos=[Combo(d0, d0 + timedelta(hours=2)),
                Combo(d0 + timedelta(hours=1), d0 + timedelta(hours=2))],
    )
    s2 = Segment(
        row_number=2, seq=2, dep_local_raw="x", dep_tz="UTC",
        arr_local_raw="y", arr_tz="UTC", dur_min=0, dur_max=300,
        dep_candidates=[d0 + timedelta(hours=3)],
        arr_candidates=[d0 + timedelta(hours=5)],
        combos=[Combo(d0 + timedelta(hours=3), d0 + timedelta(hours=5))],
    )
    status, payload = solve_chain([s1, s2])
    check("末段落地相同的两条完整链仍判 multiple", status == "multiple")
    check("路径计数恰为 2", payload["solution_count"] == 2)

    # 9) 唯一解不受饱和计数影响，回溯内容正确
    u1 = Segment(
        row_number=1, seq=1, dep_local_raw="x", dep_tz="UTC",
        arr_local_raw="y", arr_tz="UTC", dur_min=60, dur_max=120,
        dep_candidates=[d0], arr_candidates=[d0 + timedelta(hours=2)],
        combos=[Combo(d0, d0 + timedelta(hours=2))],
    )
    u2 = Segment(
        row_number=2, seq=2, dep_local_raw="x", dep_tz="UTC",
        arr_local_raw="y", arr_tz="UTC", dur_min=60, dur_max=120,
        dep_candidates=[d0 + timedelta(hours=3)],
        arr_candidates=[d0 + timedelta(hours=5)],
        combos=[Combo(d0 + timedelta(hours=3), d0 + timedelta(hours=5))],
    )
    status, payload = solve_chain([u1, u2])
    check("两层唯一链判定 unique", status == "unique")
    check("唯一链回溯首段起飞正确", payload["solution"][0].dep_utc == d0)

    # 10) 真实 CSV 端到端：单段 DST 重复时刻，起降各双候选，精确计数
    one_csv = HEADER + (
        "DST-ONE,1,2026-11-01 01:30,America/New_York,"
        "2026-11-01 01:30,America/New_York,0,120\n"
    )
    one = solve_csv(one_csv)["tails"][0]
    check("单段 DST 双候选判 multiple", one["status"] == "multiple")
    check("单段精确计数 3（0/60/0 分钟三个组合）",
          one["solution_count"] == 3
          and len(one["segments"][0]["combos"]) == 3)

    # 11) 真实 CSV：同段双起飞、相同落地 UTC -> 2 条链仍算多解
    same_arr_csv = HEADER + (
        "DST-SAME,1,2026-10-25 02:30,Europe/Berlin,"
        "2026-10-25 03:30,Europe/Berlin,60,180\n"
    )
    same = solve_csv(same_arr_csv)["tails"][0]
    same_combos = same["segments"][0]["combos"]
    check("同落地双起飞判 multiple", same["status"] == "multiple")
    check("落地 UTC 相同、起飞 UTC 不同",
          len({c["arr_utc"] for c in same_combos}) == 1
          and len({c["dep_utc"] for c in same_combos}) == 2)
    check("同落地仍精确计数 2 条链", same["solution_count"] == 2)

    # 12) 真实 CSV：多段 DST 可衔接链 + 末尾冲突，无解定位
    chain_rows = [
        "DST-CHAIN,1,2026-10-25 02:30,Europe/Berlin,2026-10-25 02:30,Europe/Berlin,0,120\n",
        "DST-CHAIN,2,2026-11-01 01:30,America/New_York,2026-11-01 01:30,America/New_York,0,120\n",
        "DST-CHAIN,3,2026-11-01 01:30,America/New_York,2026-11-01 01:30,America/New_York,0,120\n",
        "DST-CHAIN,4,2026-11-01 01:30,America/New_York,2026-11-01 01:30,America/New_York,0,120\n",
    ]
    base_chain = solve_csv(HEADER + "".join(chain_rows))["tails"][0]
    check("多段 DST 链本身可解", base_chain["status"] in ("unique", "multiple"))
    chain_rows.append(
        "DST-CHAIN,5,2026-10-01 00:00,Europe/Berlin,2026-10-01 01:00,Europe/Berlin,30,90\n"
    )
    broken = solve_csv(HEADER + "".join(chain_rows))["tails"][0]
    check("多段 DST 链追加冲突判 no_solution", broken["status"] == "no_solution")
    check("DST 链冲突定位 seq=5 且类型 chain_order",
          broken["conflict"]["segment_seq"] == 5
          and broken["conflict"]["type"] == "chain_order")
    check("DST 冲突附带上一段落地候选",
          len(broken["conflict"]["previous_arrival_candidates"]) >= 1)

    print("\n全部自检通过 ✅")


def _branched_segment(i: int) -> Segment:
    """每段两个可衔接 combo，跨段全部连得通 -> 整链 2^n 条路径。"""
    base = Z + timedelta(days=i)
    dep1, dep2 = base, base + timedelta(hours=1)
    arr1, arr2 = base + timedelta(hours=2), base + timedelta(hours=3)
    return Segment(
        row_number=i, seq=i, dep_local_raw="x", dep_tz="UTC",
        arr_local_raw="y", arr_tz="UTC", dur_min=60, dur_max=240,
        dep_candidates=[dep1, dep2], arr_candidates=[arr1, arr2],
        combos=[Combo(dep1, arr1), Combo(dep2, arr2)],
    )


def _conflict_segment(i: int) -> Segment:
    """起飞时刻远早于上一段落地，且时长窗口无法挽救的冲突航段。"""
    base = Z + timedelta(days=i)
    dep = base - timedelta(days=10)
    arr = dep + timedelta(minutes=30)
    return Segment(
        row_number=i, seq=i, dep_local_raw="x", dep_tz="UTC",
        arr_local_raw="y", arr_tz="UTC", dur_min=10, dur_max=60,
        dep_candidates=[dep], arr_candidates=[arr],
        combos=[Combo(dep, arr)],
    )


if __name__ == "__main__":
    main()
