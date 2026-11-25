# 短片联映提示的文字编审工具

这套工具用 Python 3.11 标准库实现，围绕 `data/` 中的具体送审剪辑、原始观察、场次版本、披露词表和工作签，完成：

- 从场记、看片笔记、策展手记、片方说明中抽取事件、片内范围、强度和观察者；
- 区分**片中呈现**、**人物讨论**、**外部转引/影评比附**和**待看片人确认的问题**；
- 对同一素材的重复描述去重，合并相邻镜头中的连续刺激；
- 按 `origin + occurrence` 迁移换版后仍可沿用的编审事实，识别被删/新增/改序镜头；
- 按场次的亮灯休息规则决定是否重新计算累计刺激；
- 生成售票短提示、观众主动展开的详细说明、现场交接 cue 单；
- 保留剧透/结尾，只在 `editor` 授权层返回；
- 追踪出票记录 `t8` 所依据的 `east:1` 与提示版本 `s2`，定位需要换提示的购票页并预览当前详细查询。

## 常用命令

以下命令均在仓库根目录运行：

```sh
# 原始资料字段校验
python3 read_materials.py
python3 tool.py check-data

# 全部测试
python3 -m unittest discover -s tests -v

# 所有场次版本的结构化分析
python3 tool.py analyze

# 当前 east 第 3 版的售票层
python3 tool.py query east --revision 3 --level public

# 观众主动选择闪光和主题明细
python3 tool.py query east --revision 3 --level audience --select flash,story

# 观众主动展开全部类别
python3 tool.py query east --revision 3 --level audience --select all

# 现场层：精确时间码、片间休息、证据号、保留意见
python3 tool.py query east --revision 3 --level staff --as staff

# 编审层：原始句子、来源、剧透和完整 claim
python3 tool.py query east --revision 3 --level editor --as editor

# 追踪 t8 出票依据、受影响页面和更新后的查询
python3 tool.py trace t8
python3 tool.py stale-pages

# 比较 east:1 -> east:3 的决策相关差异
python3 tool.py diff east 1 3

# 生成 release/ 下的 JSON/Markdown 快照
python3 tool.py publish --out release
```

接收一条新看片记录（会先验证 `cut` 存在，再按 `index.json` 字段追加）。下面示例写入临时复制目录，避免误改仓库样本：

```sh
cp -r data /tmp/shortwarn-data
python3 tool.py --data /tmp/shortwarn-data observe lantern-b \
  --observer "看片人" \
  --text "第8秒有一声突发高音量。"
```

编审确认与策展签发是分开的追加式账本。默认写入 `workstate/ledger.jsonl`，不会改写只读来源资料 `data/editor_book.jsonl`。试用时可显式指定临时账本：

```sh
python3 tool.py --ledger /tmp/shortwarn-ledger.jsonl \
  confirm o8 --copy "新镜头第8秒有突发高音量。"
SID=$(python3 tool.py --ledger /tmp/shortwarn-ledger.jsonl \
  sign east:3 --scope 售票 \
  --copy "含光线变化、突发声响及情绪主题；详情可主动展开。" \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)["id"])')
python3 tool.py --ledger /tmp/shortwarn-ledger.jsonl \
  issue east:3 --advisory-id "$SID" --ticket-class 普通票
```

真实购票页 ID/URL 可登记后进入 trace：

```sh
python3 tool.py register-page east-main east \
  --url "https://tickets.example/east" \
  --ticket-class 普通票
```

## Python API

```python
from shortwarn.workspace import load_workspace
from shortwarn.ledger import Ledger
from shortwarn.engine import analyse_programme, event_clearance
from shortwarn.disclosure import render_advisory
from shortwarn.publishing import trace_issuance

ws = load_workspace("data")
ledger = Ledger(ws, "workstate/ledger.jsonl")
analysis = analyse_programme(ws, "east", 3, ledger=ledger)

public = render_advisory(analysis, "public")
details = render_advisory(
    analysis, "audience", selected=["flash", "sound", "story"], role="audience"
)
site = render_advisory(analysis, "staff", role="staff")
trace = trace_issuance(ws, ledger, "t8")

# 某事件能否发布“全场无此内容”
clearance = event_clearance(analysis, "flash")
```

## 核心判定口径

### 1. 来源性质不混用

每条抽取结果都保留 `observation_id`、`observer`、`observer_role`、原句 `quote` 和 `stance`：

- `presented`：笔记明确描述本片实际呈现；
- `discussed`：片中人物谈论灯光/声音/往事，不构成刺激事实；
- `quoted` / `quoted_speculation`：评论、影评或外部作品比附，不能作为本片证据；
- `question`：原话或剪辑复看状态不清，进入看片待办。

例如 `o3` 中“评论里说像《长灯》一样闪”只作为外部转引保留；真正进入统计的是同句后文“实片……排练段开门后一阵爆音”和“走出过道灯切了五次”。`o6` 的“电影里的人谈了舞台灯光”是讨论，“画面没有变亮”是否定观察，二者都不会生成闪光段。

### 2. 定位、去重和累计

镜头定位优先使用：

1. 明确的“第 N 秒”；
2. “前 N 秒”“约 N 秒”等片内偏移；
3. 中文地点词到 `origin` 的别名（见 `shortwarn/lexicon.json`）；
4. “第一遍/第二遍”对应的 `occurrence`；
5. 承前省略和“又……”的相邻镜头上下文。

同一镜头/同一素材的重复提及合并为一个事实，证据号保留在 `evidence_ids`，但时长不重复相加。不同镜头中的相邻刺激保留为不同 segment，再按场次 block 求并集时长。

场地策略来自 `policies.jsonl`：

- 单段时长达到 `short_ms`：进入提示；
- 同一累计 block 内总时长达到 `accumulated_ms`：标记累计提示；
- 只有相邻片间隔 `lights_on=true` 且 `interval_ms >= reset_ms` 时才开始新 block。

### 3. 换版沿用

跨 `lantern-a -> lantern-b` 不使用容易变化的 `shot` 编号，而使用 `(origin, occurrence)`：

- `('hall', 1)` 在 b 版被删除，旧“第一遍走廊”笔记不再适用于新剪辑；
- `('hall', 2)` 从 `a3` 映射到 `b2`，已签认的“第二遍走廊前四秒”映射为 `12500–16500ms`；
- b 版 `origin=null` 的新镜头不能凭旧笔记推断；`o4` 的“灯箱是否闪还没复看”生成看片问题，`o7` 的“亮度不变”可回答该问题；
- 若查询时刻停在 `2026-11-22T15:30+08:00`（east:3 排片时、`o7` 收到前），只有 `lantern-b` 带 `viewer_recheck` 保留意见。

### 4. 分众披露

层级为：

| 层级 | 调用身份 | 返回内容 |
| --- | --- | --- |
| `public` / `ticket` | 所有人 | `policies.public_label` 类别和策展人已签文案；不含时间码/结尾 |
| `audience` / `detail` | 主动选择类别的观众 | `detail_label`、单片/累计说明；结尾固定 `redacted` |
| `staff` / `site` | 现场授权人员 | 片间休息、精确片内/场次时间码、镜头、证据号、保留意见 |
| `editor` | 编审 | 原句、观察者、来源 stance、迁移、剧透和完整 claim |

未主动 `--select` 时，观众层只返回可展开菜单，不泄露明细。低授权身份请求高层级会抛出 `PermissionError`，CLI 退出码为 `3`。

## 当前资料对应的业务结论

- `east:1`：`lantern-a` 的两段走廊闪光约 `6s + 4s = 10s`，超过 `7.6s` 累计口径；`s1` 签认了第二遍走廊前四秒。第一部后有 `12s` 亮灯间隔，可休息并重置累计。
- `east:2`：换为 `lantern-b` 后，删除第一遍走廊，仅保留映射后的第二遍走廊前四秒；仍有 `12s` 亮灯休息，因此不与 `night-a` 跨片累计。
- `east:3`：片间改为 `2.5s`、`lights_on=false`，不构成休息/重置。`lantern-b` 的 `4.0s` 与 `night-a` 过道灯切五次所在 `5.4s` 合并为 `9.4s`，达到累计口径；现场不能承诺片间亮灯休息。
- `west:1`：`water-a` 无正证据，`lantern-a` 自身累计 `10s`；`3s` 暗场间隔不重置，但水面片不增加刺激时长。
- `t8` 可追溯到出票时的 `east:1 + s2`。当前最新为 `east:3`，因此逻辑购票页 `sales-page:east:普通票` 必须更换提示；在策展人签发 `east:3` 文案前，页面只能展示草稿/复核状态，不能把 draft 当作已签文案。
- `o2`/`o7` 的信件和结尾内容只进入 `editor` 层；观众的叙事主题查询返回“亲属失联与等待”，不返回“从未收到信/另一封信”等转折原句。

## 接入新片

1. 在 `reels.jsonl` 登记具体剪辑的每个镜头，务必填写稳定的 `origin` 和重复素材的 `occurrence`。
2. 在 `observations.jsonl` 追加原始文字，或使用 `tool.py observe`。
3. 若出现新的中文地点说法，把别名加入 `shortwarn/lexicon.json` 的 `location_aliases`。
4. 新事件类型/阈值加入 `policies.jsonl` 和词表。
5. 新场次版本加入 `programmes.jsonl`，保留递增 `revision`。
6. 运行 `python3 tool.py check-data` 与 `python3 -m unittest discover -s tests`。
7. 先处理 `tool.py tasks` 中的看片/映射问题，再由编审 `confirm`、策展人 `sign`，最后更新购票页并重新 `publish`。

解析器不猜测无法唯一映射的位置；这类记录会以 `needs_viewer` / `unresolved_scope` 进入保留意见，而不是被静默忽略。
