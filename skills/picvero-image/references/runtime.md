# 本地运行记录

runtime.py 只读写本地文件，不联网、不读取密钥、不生成或修改位图。Python 3.10+，标准库可运行；Pillow 仅是可选的只读解码增强。

使用当前技能目录内的 scripts/runtime.py。目录以实际读取到的 SKILL.md 为准，不能猜测用户的 HOME 路径。引用中所有占位路径须替换成实际绝对路径。

```bash
python3 "/实际技能目录/scripts/runtime.py" init --request "/实际/request.json" --runs "/实际项目/runs"
```

request 结构见 [商品与图位](product-and-shots.md)。其中相对参考路径相对于 request.json 所在目录解析。程序复制原图快照，返回唯一 run 路径。

默认每个图位 `max_renders: 1`，省略时运行器也按 1 处理；CSV 导入同样如此。次数不因检查结果提高；用户明确要求新版本时按下面的续做说明建立关联修改任务，旧任务不改写。主入口五图与文案由 Codex 按 [整套交付](launch-kit.md) 策划，CSV 的基础三类图只是导入草稿，不替代主入口的完整图位计划。

## 一张图

1. 查看快照，Codex 调用内置图片工具。
2. 把实际使用的制作说明写入 prompt.txt。
3. 使用返回的真实文件路径记录结果：

```bash
python3 "/实际技能目录/scripts/runtime.py" record --run "/实际/run" --sku A001 --shot main --image "/实际/工具结果.png" --prompt-file "/实际/prompt.txt"
python3 "/实际技能目录/scripts/runtime.py" review-template --run "/实际/run" --sku A001 --shot main
```

review-template 的输出在 result 中。只把 result 对象保存为 review.json，再根据真实对照填写五项结论，不要把外层 ok/result 包进去。

```bash
python3 "/实际技能目录/scripts/runtime.py" review --run "/实际/run" --sku A001 --shot main --report "/实际/review.json"
```

只有用户明确选择时记录 select。AI 复核通过无需等待用户同意就可以交付候选，不要擅自代替用户选用。

```bash
python3 "/实际技能目录/scripts/runtime.py" select --run "/实际/run" --sku A001 --shot main --decision selected --note "用户明确选用第一张"
```

用户明确选用未审核或有问题的图时，select 可加 --allow-unreviewed，但必须记录其原意。不会改变质量状态。

## 恢复与交付

查看某个图位的原图快照路径、真实参数、所有图片版本和审核状态：

```bash
python3 "/实际技能目录/scripts/runtime.py" show --run "/实际/run" --sku A001 --shot main
```

review-template、review、select 均可加 --version 1 操作历史版本。用户改选旧版时明确记录版本，不能自行替换成最新图片。每个图位只保留一个当前选用版本，历史记录不删除。

```bash
python3 "/实际技能目录/scripts/runtime.py" status --run "/实际/run"
python3 "/实际技能目录/scripts/runtime.py" export --run "/实际/run" --out "/实际项目/交付-v1"
```

status 区分待制作、待审核、待修改、待补信息和已可交付；原图快照／结果哈希变化会阻止沿用。用户修改了商品事实或品牌计划时，从新请求建立新任务，保留旧版本。

`needs_input` 是当前图位的状态，不要求停下整套等用户回答；默认不重试，继续剩余图位和文案后交付具体问题。用户后续说“改第二张”就已授权该图修改：若旧任务次数已满，建立只含该图位的新任务，仍包含原始商品参考、当前修改目标，并在工作记录链接旧 run 与版本。不要重建或重做整套，也不再询问已经说清的修改。新增修改目标作为辅助参考，不能替代原照提供商品事实。

export 优先取用户选定的版本，否则取最新版本；默认只导出已核对且未被拒绝的候选。--selected-only 只导出明确选用项；--drafts 明确导出带问题状态的草稿。用户选用未通过的版本也不等于检查通过，必须带状态。导出文件夹、index.html、manifest.json 和同名 ZIP；已有目标不覆盖。

## CSV 导入

CSV 至少包含 sku、name、image 三列，可有 description。相对图片路径按 CSV 所在目录解析，支持引号内的逗号和 UTF-8 BOM；远程 URL 不会由运行器下载。

```bash
python3 "/实际技能目录/scripts/runtime.py" import-csv --csv "/实际/商品.csv" --out "/实际/request.json" --shots packshot,feature,scene --brand "/实际/brand.json"
```

--brand 可省略。生成的是结构化草稿：供应商描述仍为 unknown，Codex 要查看原图、完善图位目标与事实依据后再 init。重复 SKU、缺图和已有输出会阻止写入，避免混货或覆盖。

## 元数据与项目规格

```bash
python3 "/实际技能目录/scripts/runtime.py" inspect "/实际/image.png"
python3 "/实际技能目录/scripts/runtime.py" check --image "/实际/image.png" --profile "/实际/profile.json"
```

```json
{
  "name": "本次演示交付",
  "scope": "项目要求；不是平台规范",
  "basis": "用户指定的方图与文件限制",
  "checked_at": "2026-09-15",
  "rules": {"aspect": "1:1", "formats": ["PNG", "JPEG"], "min_width": 800, "max_bytes": 10000000}
}
```

返回 ok 表示程序执行成功，不表示检查通过。查看 result.verdict；inconclusive 不能解释成通过。

## 失败

实际出图调用失败或工具受阻时记录原因，避免中断后丢失信息：

```bash
python3 "/实际技能目录/scripts/runtime.py" failure --run "/实际/run" --sku A001 --shot main --event failed --note "内置工具本次生成失败"
```

failed 表示实际尝试失败，计入次数；blocked 表示未能开始的工具或素材限制。失败记录保留已有版本，status 会给出下一步。不要为同一次失败重复记录，也不要把未执行的调用记成尝试。

文件缺失先核对实际路径，不重新生图填坑；JSON 问题按说明修正；已到生成次数上限时与用户的新范围对应后新建任务。记录失败不抹除已成功的图片。不要将全部错误、私人绝对路径和内部日志直接复制给不需要它们的卖家。
