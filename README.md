# 航段时间清洗 · DST 安全入仓

面向航司数据工程师的网页工具：把地服导出的**当地时间**航段链解析为唯一 **UTC** 时间链，
避免夏令时（DST）切换时被错误入仓。

## 快速启动

```bash
docker compose up --build
```

浏览器打开 <http://localhost:8000> ，点击「📦 载入内置夏令时样例」→「开始清洗」即可看到完整演示。

本地开发（不用 Docker）：

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

自检（仅标准库）：`python3 tests/test_solver.py`

## 输入 CSV 格式

UTF-8 编码，必需表头：

```csv
tail_number,seq,dep_local,dep_tz,arr_local,arr_tz,duration_min_minutes,duration_max_minutes
B-7001,1,2026-10-25 00:30,Europe/London,2026-10-25 02:15,Europe/Berlin,90,130
```

| 列 | 含义 |
| --- | --- |
| `tail_number` | 机尾号（同一机尾构成一条航段链） |
| `seq` | 航段顺序，整数，同一机尾内唯一，按升序链接 |
| `dep_local` / `arr_local` | 起飞/落地当地时间，`YYYY-MM-DD HH:MM`（也接受 `T` 分隔与秒） |
| `dep_tz` / `arr_tz` | 两端 IANA 时区名，如 `Europe/Berlin`、`America/New_York` |
| `duration_min_minutes` / `duration_max_minutes` | 飞行时长下/上限（分钟，**闭区间**） |

## 求解规则

1. 当地时间 → UTC 候选：
   - 普通时刻 → 1 个候选；
   - **重复时刻**（秋季拨回重叠，如柏林 2026-10-25 02:30）→ 保留 **2 个候选**参与整链求解；
   - **不存在的时刻**（春季拨快空洞，如柏林 2026-03-29 02:30）→ 该行报错，整架机尾禁止导出。
2. 每个航段的 (起飞, 落地) 候选组合必须满足：飞行时长 ∈ `[下限, 上限]`（闭区间）。
3. 按 `seq` 链接同一机尾：下一段起飞 **不得早于** 上一段落地（相等允许）。
4. 判定：
   - **整链唯一解** → 允许导出该机尾的 UTC CSV；
   - **无解** → 展示冲突航段（前向扫描定位第一个无法衔接的航段及其候选），禁止导出；
   - **多解** → 展示可行候选链（最多展示 20 条），禁止导出；
   - **行级错误**（不存在时刻 / 非法时区 / 格式错误 / seq 重复等）→ 禁止导出。

### 长链求解实现（无递归、不枚举全路径）

求解器把航段链建模为一张**分层 DAG**：每个航段的每个可行 (起飞, 落地) 组合是一个节点，
层间按「下一段起飞 ≥ 上一段落地」连边，整链解就是从第一层到最后一层的路径。

- **前向扫描（迭代）**：为每个节点累计可达路径数，并记录前驱节点列表；
- **后向扫描**：标记哪些节点能走到链尾；
- **判定与路径数无关地精确**：路径数 `0` 即无解、`1` 即唯一解、`≥2` 即多解。
  路径计数饱和到 201 仅用于抑制大整数运算，**不会**把「搜索中断」误判成多解；
- 唯一解沿前驱直接回溯；只有多解时才为「候选链展示」做限量枚举（硬上限 200 条，迭代实现）；
- 两条完整 UTC 链只要任一航段的起飞/落地时刻不同就算不同路径——
  **即使末段落地时刻完全相同，仍然判定为多解**。

全程没有递归调用（千段级航段不会触发 `RecursionError`），复杂度 O(段数 × 每层候选数²)，
无解场景在前向扫描即返回，不会因 2^N 条候选路径而长时间不返回。

## 导出

- 网页上按机尾导出，或「导出全部可导出机尾」；
- 导出列：`tail_number,seq,dep_utc,arr_utc,duration_minutes`（UTC 为 ISO 8601 `Z` 格式）；
- 服务端在导出时会**重新求解并复核唯一性**，非唯一解返回 409，杜绝误导出。

## 内置样例

### samples/dst_sample.csv（夏令时消歧演示）

| 机尾 | 场景 | 期望结果 |
| --- | --- | --- |
| B-7001 | 欧洲拨回日三连段，重复时刻由时长区间+链约束消歧 | 唯一解，可导出 |
| B-7002 | 柏林 2026-03-29 02:30 不存在（拨快空洞） | 数据错误 |
| B-7003 | 第二段起飞早于第一段落地 | 无解，展示冲突航段 |
| B-7004 | 起降均为重复时刻，区间宽松 | 多解（2 条候选链） |
| B-7005 | 美国拨回日（纽约/洛杉矶/丹佛） | 唯一解，可导出 |

### samples/long_chain_sample.csv（长链回归，2401 数据行）

| 机尾 | 场景 | 期望结果 |
| --- | --- | --- |
| LONG-1200 | 1200 段无歧义、可衔接航段（旧递归实现会 RecursionError） | 唯一解，可导出 |
| LONG-1200-CONFLICT | 同样 1200 段后追加无法衔接的第 1201 段 | 无解，冲突定位 seq=1201 |

网页点击「🧵 载入 1200 段长链样例」即可演示；该文件由
`python3 tests/make_long_chain_sample.py` 重新生成。

## API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/` | 网页入口 |
| `GET` | `/api/sample` | 下载内置夏令时样例 CSV |
| `GET` | `/api/sample/long` | 下载 1200 段长链样例 CSV |
| `POST` | `/api/solve` | 上传 CSV（multipart 字段 `file`），返回逐机尾求解结果 JSON |
| `POST` | `/api/export` | 字段 `file` + `tail`（`__all__` 表示全部可导出机尾），返回 UTC CSV；非唯一解 409 |
| `GET` | `/docs` | FastAPI 自动文档 |

## 目录结构

```
app/
  main.py                       # FastAPI 路由
  solver.py                     # 候选生成 + 分层 DAG 整链求解 + 冲突定位（纯标准库，无递归）
  static/index.html             # 单页前端
samples/dst_sample.csv          # 夏令时消歧样例
samples/long_chain_sample.csv   # 1200 段长链回归样例（由生成脚本产出）
tests/test_solver.py            # 求解器自检（含长链/指数链/同落地多解回归）
tests/make_long_chain_sample.py # 长链样例生成脚本
Dockerfile / docker-compose.yml / requirements.txt
```
