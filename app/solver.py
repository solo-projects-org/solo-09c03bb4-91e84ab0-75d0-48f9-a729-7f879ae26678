"""航段链 UTC 求解核心逻辑。

业务规则
--------
- 每行航段给出起飞/落地的当地时间与两端 IANA 时区；
- 当地时间在该时区中不存在（春季拨快产生的空洞）→ 该行报错，整架机尾禁止导出；
- 当地时间重复（秋季拨回产生的重叠）→ 保留两个 UTC 候选，参与整链求解；
- 飞行时长（落地 - 起飞）必须落在闭区间 [duration_min_minutes, duration_max_minutes]；
- 同一机尾按 seq 升序链接：下一段起飞不得早于上一段落地（相等允许）；
- 整链恰好唯一解才允许导出；无解展示冲突航段，多解展示可行候选。
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

REQUIRED_COLUMNS = [
    "tail_number",
    "seq",
    "dep_local",
    "dep_tz",
    "arr_local",
    "arr_tz",
    "duration_min_minutes",
    "duration_max_minutes",
]

_DT_FORMATS = (
    "%Y-%m-%d %H:%M",
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
)

# 多解时实际枚举并返回给前端展示的候选条数上限（分类本身不受此上限影响）
SOLUTION_CAP = 200
# 多解时返回给前端展示的候选条数
MAX_DISPLAY_SOLUTIONS = 20
# 路径计数饱和值：达到该值即认定为“多解”，避免超大整数运算
_COUNT_SATURATED = SOLUTION_CAP + 1


class CsvFormatError(Exception):
    """CSV 整体无法解析（缺列、空文件等）。"""


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _fmt_duration(minutes: float):
    return int(minutes) if float(minutes).is_integer() else round(minutes, 1)


def parse_naive(text: str):
    """解析当地时间字符串为 naive datetime，失败返回 None。"""
    text = (text or "").strip()
    for fmt in _DT_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def utc_candidates(naive: datetime, tz_name: str):
    """把 naive 当地时间映射为可能的 UTC 时刻。

    返回 (sorted_candidates, error_kind)：
    - error_kind 为 None：候选列表（重复时刻含 2 个候选，普通时刻 1 个）
    - error_kind 为 "unknown_tz" / "nonexistent"：候选为 None
    """
    try:
        tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        return None, "unknown_tz"
    found = set()
    for fold in (0, 1):
        aware = naive.replace(tzinfo=tz, fold=fold)
        utc = aware.astimezone(timezone.utc)
        # 回代校验：只有能还原成本地时间的偏移才是合法候选
        if utc.astimezone(tz).replace(tzinfo=None) == naive:
            found.add(utc)
    if not found:
        return None, "nonexistent"
    return sorted(found), None


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass
class RawRow:
    row_number: int  # 数据行号（表头为第 0 行，数据从 1 起）
    tail: str
    seq_raw: str
    dep_local_raw: str
    dep_tz: str
    arr_local_raw: str
    arr_tz: str
    dur_min_raw: str
    dur_max_raw: str


@dataclass
class Combo:
    dep_utc: datetime
    arr_utc: datetime

    @property
    def duration_minutes(self) -> float:
        return (self.arr_utc - self.dep_utc).total_seconds() / 60.0


@dataclass
class Segment:
    row_number: int
    seq: int
    dep_local_raw: str
    dep_tz: str
    arr_local_raw: str
    arr_tz: str
    dur_min: int
    dur_max: int
    dep_candidates: list = field(default_factory=list)  # list[datetime]
    arr_candidates: list = field(default_factory=list)
    combos: list = field(default_factory=list)  # 满足时长区间的 Combo


# ---------------------------------------------------------------------------
# CSV 读取与行级校验
# ---------------------------------------------------------------------------

def read_rows(csv_text: str) -> list:
    reader = csv.reader(io.StringIO(csv_text))
    rows = [r for r in reader if any(cell.strip() for cell in r)]
    if not rows:
        raise CsvFormatError("CSV 内容为空")
    header = [c.strip().lower().lstrip("﻿") for c in rows[0]]
    missing = [c for c in REQUIRED_COLUMNS if c not in header]
    if missing:
        raise CsvFormatError(
            "缺少必需列: " + ", ".join(missing)
            + "。需要的表头为: " + ", ".join(REQUIRED_COLUMNS)
        )
    idx = {name: header.index(name) for name in REQUIRED_COLUMNS}

    def get(r, name):
        j = idx[name]
        return r[j].strip() if j < len(r) else ""

    out = []
    for i, r in enumerate(rows[1:], start=1):
        out.append(RawRow(
            row_number=i,
            tail=get(r, "tail_number"),
            seq_raw=get(r, "seq"),
            dep_local_raw=get(r, "dep_local"),
            dep_tz=get(r, "dep_tz"),
            arr_local_raw=get(r, "arr_local"),
            arr_tz=get(r, "arr_tz"),
            dur_min_raw=get(r, "duration_min_minutes"),
            dur_max_raw=get(r, "duration_max_minutes"),
        ))
    if not out:
        raise CsvFormatError("CSV 没有任何数据行")
    return out


def _parse_minutes(raw: str):
    """解析分钟数，容忍 '420' 与 '420.0' 两种写法。"""
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        f = float(raw)
        if f.is_integer():
            return int(f)
    except ValueError:
        pass
    return None


def build_segment(raw: RawRow):
    """行级校验 + 候选生成。返回 (Segment|None, [错误信息])。"""
    errors = []

    if not raw.tail:
        errors.append("tail_number（机尾号）为空")

    try:
        seq = int(raw.seq_raw)
    except ValueError:
        errors.append(f"seq 不是整数: {raw.seq_raw!r}")
        seq = None

    dur_min = _parse_minutes(raw.dur_min_raw)
    dur_max = _parse_minutes(raw.dur_max_raw)
    if dur_min is None:
        errors.append(f"duration_min_minutes 不是整数分钟: {raw.dur_min_raw!r}")
    if dur_max is None:
        errors.append(f"duration_max_minutes 不是整数分钟: {raw.dur_max_raw!r}")
    if dur_min is not None and dur_max is not None:
        if dur_min < 0:
            errors.append(f"duration_min_minutes 不能为负: {dur_min}")
        if dur_min > dur_max:
            errors.append(f"飞行时长区间非法: 下限 {dur_min} 大于上限 {dur_max}")

    dep_naive = parse_naive(raw.dep_local_raw)
    if dep_naive is None:
        errors.append(
            f"起飞当地时间无法解析: {raw.dep_local_raw!r}（支持格式 YYYY-MM-DD HH:MM）")
    arr_naive = parse_naive(raw.arr_local_raw)
    if arr_naive is None:
        errors.append(
            f"落地当地时间无法解析: {raw.arr_local_raw!r}（支持格式 YYYY-MM-DD HH:MM）")

    dep_candidates = arr_candidates = None
    if dep_naive is not None:
        dep_candidates, err = utc_candidates(dep_naive, raw.dep_tz)
        if err == "unknown_tz":
            errors.append(f"起飞时区不是合法的 IANA 时区: {raw.dep_tz!r}")
        elif err == "nonexistent":
            errors.append(
                f"起飞当地时间 {raw.dep_local_raw} 在时区 {raw.dep_tz} 中不存在"
                "（夏令时拨快跳过了该时刻），请与地服核对")
    if arr_naive is not None:
        arr_candidates, err = utc_candidates(arr_naive, raw.arr_tz)
        if err == "unknown_tz":
            errors.append(f"落地时区不是合法的 IANA 时区: {raw.arr_tz!r}")
        elif err == "nonexistent":
            errors.append(
                f"落地当地时间 {raw.arr_local_raw} 在时区 {raw.arr_tz} 中不存在"
                "（夏令时拨快跳过了该时刻），请与地服核对")

    if errors:
        return None, errors

    seg = Segment(
        row_number=raw.row_number, seq=seq,
        dep_local_raw=raw.dep_local_raw, dep_tz=raw.dep_tz,
        arr_local_raw=raw.arr_local_raw, arr_tz=raw.arr_tz,
        dur_min=dur_min, dur_max=dur_max,
        dep_candidates=dep_candidates, arr_candidates=arr_candidates,
    )
    for d in dep_candidates:
        for a in arr_candidates:
            combo = Combo(d, a)
            if dur_min <= combo.duration_minutes <= dur_max:
                seg.combos.append(combo)
    return seg, []


# ---------------------------------------------------------------------------
# 整链求解（迭代式分层动态规划，无递归、不枚举完整路径）
# ---------------------------------------------------------------------------
#
# 模型：按 seq 排序后，每层（航段）的每个可行 Combo 是一个节点；
# 层间按「下一段起飞 >= 上一段落地」连边，得到一张分层 DAG。
# 整链解 = 从第一层到最后一层的路径。
#
# 与“把搜索中断当成多解”的旧做法不同，这里分三遍扫描，全程 O(N*K^2)：
#   1) 前向：每个节点记录可达路径数（饱和到 _COUNT_SATURATED）与前驱列表；
#   2) 后向：标记哪些节点能走到链尾；
#   3) 判定：总路径数 0=无解、1=唯一解、>=2=多解（精确区分，与枚举上限无关）。
# 唯一解直接沿前驱回溯；多解时展示用的候选链才做限量枚举（迭代、有硬上限）。
# 注意：路径计数的单位是“完整 UTC 链”，两条链即使末段落地时刻相同，
# 只要中间任一 Combo 不同就是两条路径，仍判定为多解。

@dataclass
class _Node:
    """分层 DAG 中的一个节点（某个航段的一个可行 Combo）。"""
    combo: Combo
    count: int = 0                 # 从链首到该节点的可行路径数（饱和计数）
    preds: list = field(default_factory=list)  # list[_Node]：上一层可衔接节点
    viable: bool = False          # 该节点能否走到最后一层


def _build_layers(segments: list) -> list:
    """前向扫描构建分层 DAG。返回每层的节点列表（某层为空即链断裂）。"""
    layers = []
    prev_nodes = None
    for seg in segments:
        nodes = [_Node(combo=c) for c in seg.combos]
        if prev_nodes is None:
            # 链首：每个 combo 自身都是一条前缀路径
            for nd in nodes:
                nd.count = 1
        else:
            for nd in nodes:
                total = 0
                for p in prev_nodes:
                    if nd.combo.dep_utc >= p.combo.arr_utc:
                        nd.preds.append(p)
                        total += p.count
                        if total >= _COUNT_SATURATED:
                            total = _COUNT_SATURATED
                            break
                nd.count = total
        if not nodes:
            break  # 该层无可行节点，不加入 layers，冲突按 duration_window 定位
        layers.append(nodes)
        prev_nodes = nodes
    return layers


def _mark_viable(layers: list) -> None:
    """后向扫描：沿前驱边标记能走到链尾的节点。"""
    if not layers or not layers[-1]:
        return
    for nd in layers[-1]:
        nd.viable = True
    for li in range(len(layers) - 1, 0, -1):
        for nxt in layers[li]:
            if not nxt.viable:
                continue
            for p in nxt.preds:
                p.viable = True


def _total_count(segments: list, layers: list) -> int:
    """完整链路径数。层构建在中途断裂（某层无可行 combo）时整链无解。"""
    if len(layers) != len(segments):
        return 0
    if not layers or not layers[-1]:
        return 0
    total = sum(nd.count for nd in layers[-1])
    return min(total, _COUNT_SATURATED)


def _reconstruct_unique(layers: list) -> list:
    """总路径数恰好为 1 时，沿前驱回溯唯一 Combo 链（按层顺序返回）。"""
    path = []
    cur = next(nd for nd in layers[-1] if nd.count > 0)
    path.append(cur.combo)
    for li in range(len(layers) - 1, 0, -1):
        cur = next(p for p in cur.preds if p.count > 0)
        path.append(cur.combo)
    path.reverse()
    return path


def _enumerate_capped(segments: list, layers: list, cap: int) -> list:
    """多解时仅供展示的路径枚举：迭代 DFS，收集满 cap 条即停。

    仅遍历 viable 节点，因此开销与“是否有解”的判定完全解耦——
    无解时本函数不会被调用，不存在指数搜索。
    """
    solutions = []
    # 栈帧：(层号, 该层待尝试的 viable 节点列表, 当前路径)
    first = [nd for nd in layers[0] if nd.viable]
    stack = [(0, first, [])]
    while stack:
        li, pending, path = stack[-1]
        if not pending:
            stack.pop()
            continue
        nd = pending.pop()
        new_path = path + [nd.combo]
        if li == len(layers) - 1:
            solutions.append(new_path)
            if len(solutions) >= cap:
                return solutions
            continue
        nxt_nodes = [
            n for n in layers[li + 1]
            if n.viable and any(p is nd for p in n.preds)
        ]
        stack.append((li + 1, nxt_nodes, new_path))
    return solutions


def _conflict_from_layers(segments: list, layers: list) -> dict:
    """依据前向 DAG 定位第一个无法衔接的航段（无解时调用）。"""
    # 层为空：某航段没有任何满足时长区间的 combo
    if len(layers) < len(segments):
        seg = segments[len(layers)]
        return {
            "type": "duration_window",
            "segment_seq": seg.seq,
            "row": seg.row_number,
            "message": (
                f"航段 seq={seg.seq}（第 {seg.row_number} 行）没有任何 "
                f"(起飞, 落地) 候选组合满足飞行时长闭区间 "
                f"[{seg.dur_min}, {seg.dur_max}] 分钟"),
            "dep_candidates": [iso(d) for d in seg.dep_candidates],
            "arr_candidates": [iso(a) for a in seg.arr_candidates],
            "previous_arrival_candidates": [],
        }

    # 首个 count 归零的层：该航段的所有 combo 都接不上前面任何可达落地
    for li, nodes in enumerate(layers):
        if any(nd.count > 0 for nd in nodes):
            continue
        seg = segments[li]
        prev_arrs = sorted({nd.combo.arr_utc for nd in layers[li - 1]})
        earliest_dep = min(nd.combo.dep_utc for nd in nodes)
        latest_prev = prev_arrs[-1]
        return {
            "type": "chain_order",
            "segment_seq": seg.seq,
            "row": seg.row_number,
            "message": (
                f"航段 seq={seg.seq}（第 {seg.row_number} 行）无法衔接："
                f"其最早可行起飞 {iso(earliest_dep)} 早于上一段最晚落地 "
                f"{iso(latest_prev)}"),
            "dep_candidates": sorted({iso(nd.combo.dep_utc) for nd in nodes}),
            "arr_candidates": sorted({iso(nd.combo.arr_utc) for nd in nodes}),
            "previous_arrival_candidates": [iso(pa) for pa in prev_arrs],
        }

    return {"type": "unknown", "message": "未能定位冲突", "segment_seq": None,
            "row": None, "dep_candidates": [], "arr_candidates": [],
            "previous_arrival_candidates": []}


def analyze_conflict(segments: list) -> dict:
    """无解时定位冲突：前向扫描，找到第一个无法衔接的航段。"""
    layers = _build_layers(segments)
    return _conflict_from_layers(segments, layers)


def solve_chain(segments: list) -> tuple:
    """对已按 seq 升序排好的航段做整链求解（纯迭代，长链安全）。

    返回 (status, payload)：
    - ("unique", {"solution": [Combo...]})
    - ("no_solution", {"conflict": {...}})
    - ("multiple", {"solution_count", "solution_count_truncated", "solutions"})
    """
    layers = _build_layers(segments)
    total = _total_count(segments, layers)

    if total == 0:
        return "no_solution", {"conflict": _conflict_from_layers(segments, layers)}

    if total == 1:
        return "unique", {"solution": _reconstruct_unique(layers)}

    # 精确已知多解（total>=2）；展示用候选链才做限量枚举
    _mark_viable(layers)
    display = _enumerate_capped(segments, layers, SOLUTION_CAP)
    truncated = total >= _COUNT_SATURATED or len(display) < total
    return "multiple", {
        "solution_count": total,
        "solution_count_truncated": truncated,
        "solutions": display,
    }


# ---------------------------------------------------------------------------
# 视图与入口
# ---------------------------------------------------------------------------

def _segment_view(seg: Segment) -> dict:
    return {
        "row": seg.row_number,
        "seq": seg.seq,
        "dep_local": seg.dep_local_raw,
        "dep_tz": seg.dep_tz,
        "arr_local": seg.arr_local_raw,
        "arr_tz": seg.arr_tz,
        "dur_min": seg.dur_min,
        "dur_max": seg.dur_max,
        "dep_candidates": [iso(d) for d in seg.dep_candidates],
        "arr_candidates": [iso(a) for a in seg.arr_candidates],
        "combos": [
            {"dep_utc": iso(c.dep_utc), "arr_utc": iso(c.arr_utc),
             "duration_minutes": _fmt_duration(c.duration_minutes)}
            for c in seg.combos
        ],
    }


def _solution_view(solution, segments) -> list:
    return [
        {"seq": seg.seq, "dep_utc": iso(c.dep_utc), "arr_utc": iso(c.arr_utc),
         "duration_minutes": _fmt_duration(c.duration_minutes)}
        for seg, c in zip(segments, solution)
    ]


def solve_tail(tail: str, raws: list) -> dict:
    errors = []
    segments = []
    for raw in raws:
        seg, errs = build_segment(raw)
        if errs:
            errors.append({"row": raw.row_number, "messages": errs})
        else:
            segments.append(seg)
    if errors:
        return {"tail": tail, "status": "error", "errors": errors}

    seqs = [s.seq for s in segments]
    seen = set()
    dup = set()
    for s in seqs:
        (dup if s in seen else seen).add(s)
    if dup:
        return {"tail": tail, "status": "error", "errors": [
            {"row": None, "messages": [
                f"机尾 {tail} 的航段顺序 seq 重复: {sorted(dup)}"]}]}

    segments.sort(key=lambda s: s.seq)
    base = {"tail": tail, "segments": [_segment_view(s) for s in segments]}

    status, payload = solve_chain(segments)
    if status == "unique":
        return {**base, "status": "unique",
                "solution": _solution_view(payload["solution"], segments)}
    if status == "no_solution":
        return {**base, "status": "no_solution",
                "conflict": payload["conflict"]}
    return {
        **base,
        "status": "multiple",
        "solution_count": payload["solution_count"],
        "solution_count_truncated": payload["solution_count_truncated"],
        "solutions": [_solution_view(s, segments)
                      for s in payload["solutions"][:MAX_DISPLAY_SOLUTIONS]],
    }


def solve_csv(csv_text: str) -> dict:
    """主入口：解析 CSV 文本，按机尾分组求解，返回完整结果 dict。"""
    raws = read_rows(csv_text)
    tails: dict = {}
    for raw in raws:
        tails.setdefault(raw.tail, []).append(raw)

    results = [solve_tail(tail, group) for tail, group in tails.items()]
    summary = {
        "rows": len(raws),
        "total_tails": len(results),
        "unique": sum(1 for r in results if r["status"] == "unique"),
        "no_solution": sum(1 for r in results if r["status"] == "no_solution"),
        "multiple": sum(1 for r in results if r["status"] == "multiple"),
        "error": sum(1 for r in results if r["status"] == "error"),
    }
    return {"summary": summary, "tails": results}


def build_export_csv(solve_result: dict, tails: list) -> str:
    """为已确认唯一解的机尾生成 UTC CSV（列：机尾、顺序、起降 UTC、时长）。"""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(["tail_number", "seq", "dep_utc", "arr_utc", "duration_minutes"])
    wanted = set(tails)
    for t in solve_result["tails"]:
        if t["tail"] in wanted and t["status"] == "unique":
            for item in t["solution"]:
                writer.writerow([t["tail"], item["seq"], item["dep_utc"],
                                 item["arr_utc"], item["duration_minutes"]])
    return buf.getvalue()
