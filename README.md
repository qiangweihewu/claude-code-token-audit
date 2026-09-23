# Claude Code Token 审计：我的 token 到底花在哪了

Claude Code 额度老是不够用。我的用法是：**让最强的 Fable 5.1 只负责编排，具体活全部派给 Opus/Sonnet subagent 去干**，本以为这样既省又稳。让 Claude 翻了自己过去 14 天的本地会话日志，结论和我的直觉很不一样：

- **84% 的 token 花在 subagent 上**，不是主会话。「Fable 编排 + subagent 执行」这套流程 14 天派出了 1,047 个 subagent，每个都背着 47k 的开场上下文从零开始，同一份材料被编排者、实现者、验证者各读一遍。
- **输出只占加权成本的 6–7%**。让 Claude「少说话」的各种技巧，天花板就这么高。
- **装了约 480 个 skill，60 天只用过 25 个**。
- **每个会话一开场就是约 40k token 的固定上下文**，每次调用都要重读一遍。
- 频繁 `/model` 切换模型，几乎不费 token（14 天仅约 2.1M）。

这个仓库包含：分析方法和数据、我做的改动与效果、一个在你自己电脑上跑的审计脚本 `analyze.py`（纯本地，不上传任何东西），以及对 40 多个「省 token」GitHub 项目的评估。

> 环境：Claude Code v2.1.280，macOS，14 天约 4.6 万次 API 调用，混用 Fable 5.1 / Opus / Sonnet / Haiku。数字是我一个人的，你的分布可能完全不同——所以先跑脚本。

---

## 1. 数据：token 花在哪

| 类别 | 14 天用量 | 说明 |
|---|---|---|
| 缓存读取 (cache read) | 4.5B | 每次调用都把整个上下文从缓存里读一遍 |
| 缓存写入 (cache write) | 200M | 只有读取的 4.4%，说明缓存命中率很好 |
| 输出 (output) | 10M | 按价格加权后只占约 6–7% |
| 新鲜输入 | 可忽略 | |

按官方价格比例加权（缓存读 0.1×、缓存写 1.25–2×、输出 5× 输入价），**成本 90% 以上在「上下文大小 × 调用次数」上**，而不是 Claude 说了多少话。

### 1.1 Subagent 占 84%

| | 数量 | 调用次数中位数 | 开场上下文 | 峰值上下文中位数 |
|---|---|---|---|---|
| 主会话 | 341 | 2 | 40k | 44k |
| Subagent | 1,047 | 27 | 47k | 124k |

原因是我在全局 `CLAUDE.md` 里写了一条「铁律」：**Fable 5.1 是编排者不是执行者**——它只做需求澄清、拆解和验收，读代码、写代码、跑测试一律派给 subagent，默认并行，实现和验证还要分开派。初衷是把最贵的模型留给判断。按父会话的模型把 subagent 归类后：

| 父会话模型 | 派出的 subagent | subagent 消耗 | 占 subagent 总量 |
|---|---|---|---|
| **Fable 5.1（编排者）** | **680** | **2.59B** | **65%** |
| Opus 5 | 239 | 0.98B | 25% |
| Opus 5.5 | 129 | 0.41B | 10% |

Fable 主会话自己只用了约 0.44B，却带出了 2.59B 的 subagent 消耗——**编排者每花 1 个 token，下面的 subagent 就花约 6 个**。Fable 编排的会话（主会话 + 子代理）合计约占全部 token 的 64%。

问题出在哪：

1. **每个 subagent 都从零开始**：约 47k 的固定上下文，每次调用重读一遍。27 次调用 ≈ 1.3M token 只是在重读系统提示、skill 列表和 CLAUDE.md。1,000 多个 subagent 下来，**约三分之一的 subagent 开销是纯固定成本**。
2. **同一份材料被读三遍**：编排者规划时读一遍，实现 agent 读一遍，验证 agent 再读一遍。
3. **便宜的活没给便宜的模型**：514 个 subagent 用了 Opus，只有 23 个用了 Haiku。

编排模式不是错的——把大量阅读从最贵的模型挪到便宜模型，总 token 多了钱也可能更少。问题在于**过度派发**：小任务也派、并行太多、模型分配偏贵。

### 1.2 开场固定上下文 40k

每个会话、每个 subagent 在说第一句话之前就带着这些：Claude Code 自身的系统提示和工具定义、skill 列表、agent 类型列表、MCP 服务器说明、全局 `CLAUDE.md`。其中你能控制的部分，我砍掉后从 **~40k 降到 ~25.5k**。

### 1.3 Skill：装得多不等于用得多

- 装了约 480 个（含插件），60 天只调用过 25 个。
- Claude Code 的 skill 列表有预算上限（默认上下文的 1%，约 8000 字符），超出后**描述会被截掉只剩名字**。所以装太多不仅占 token，还让 Claude **更难自动选对 skill**。

### 1.4 工具输出被反复重读

工具返回的内容会留在上下文里，之后每次调用都要重读。估算下来：

| 来源 | 占缓存读取 |
|---|---|
| 所有工具输出 | ~17% |
| 其中 Bash | ~12% |
| 其中 Read | ~4% |

这就是「压缩工具输出」类工具（RTK、caveman 的 proxy）真正有价值的地方，比「让 Claude 少说话」有用得多。

### 1.5 切换模型

缓存按模型隔离，会话中途切模型要重写整个上下文的缓存。但我 127 次 `/model` 大多在会话开头，中途切换只有 23 次，额外成本约 2.1M，约 1%。**开头切随便切，别在上下文很大时切就行。**

---

## 2. 我做了什么改动

### 2.1 `skillOverrides`：隐藏不用的 skill，但保留 `/name` 手动调用

这是 Claude Code 原生设置（我在 v2.1.280 源码里确认过），每个 skill 可设四种状态：

| 值 | 效果 |
|---|---|
| `on` | 正常列出，带描述 |
| `name-only` | 只列名字，不带描述 |
| `user-invocable-only` | 模型看不到，但你仍可以 `/name` 调用 |
| `off` | 完全禁用 |

```jsonc
// ~/.claude/settings.json
{
  "skillOverrides": {
    "browse": "on",
    "investigate": "on",
    "cold-email": "user-invocable-only"
    // ...
  }
}
```

注意：**插件带的 skill 不受 `skillOverrides` 控制**，只能整体禁用插件（`enabledPlugins` 里设为 `false`），需要时在项目级 `.claude/settings.json` 里再开。

我的做法：常用的和确实值得自动触发的设为 `on`（37 个），其余全部 `user-invocable-only`。一个不删，模型看到的从 ~480 降到 56 个，而且列表够短，描述也回来了。

用脚本自动生成（最近 60 天用过的设为 `on`，其余 `user-invocable-only`）：

```bash
python3 analyze.py --days 60 --suggest-overrides > overrides.json
```

生成后请人工过一遍，把你想让 Claude 主动用的 skill 改回 `on`。

### 2.2 其他设置

```jsonc
{
  "bashOutputMaxChars": 15000,   // Bash 输出内联上限，默认 30000 字符；超出部分存文件，Claude 仍可读
  "autoCompactWindow": 160000,   // 我原来设了 500000，但上下文窗口只有 200k，等于没设
  "enabledPlugins": { "some-unused-plugin": false }
}
```

- 60 天没用过的全局 MCP 服务器，改成只在需要的项目里配置。
- 整套没用过的工作流框架（50 个 skill + 50 个命令 + 17 个 agent）移到备份目录。

### 2.3 重写全局 CLAUDE.md 里的 subagent 规则

从「默认并行、主模型不动手」改成：

```markdown
## 任务分配与 subagent
- 能直接做就直接做：≤3 个文件、目标单一的任务不派 subagent。
- 只在两种情况派 subagent：① 大量探索输出需要与主上下文隔离；② 确有互不依赖的并行子任务。一次并行 ≤3 个。
- 模型：探索/搜索/批量看图 → haiku；实现、模板、机械改动 → sonnet；复杂判断、独立验证 → opus。
- 继续已有 agent 用 SendMessage，不要为同一件事重开新 agent。
- 派 ≥3 个 agent 或处理 ≥50 条数据前，先报模型、数量、预估 token。
```

全局 CLAUDE.md 也从 9KB 压到 2.5KB——它同样每次调用都被重读。

### 2.4 一个额外发现

有个项目在做批量扫描件 OCR，**每一页都开一个完整的 Claude Code 会话**（`claude -p`），每页都背着 40k 的固定上下文。这类机械批处理应该直接调 API，用短提示和便宜模型，单页开销能从 ~40k 降到 ~2k。检查一下你有没有类似的脚本。

---

## 3. 省 token 的 GitHub 项目评估

先说结论：**看清你的开销结构再选工具**。如果输出只占 6%，任何「让 AI 少说话」的方案最多省 6%。

| 项目 | 机制 | 砍的是哪块 | 结论 |
|---|---|---|---|
| [luongnv89/asm](https://github.com/luongnv89/asm) | skill 管理 CLI，已安装/保存/禁用三态，项目/全局作用域，`asm stats --tokens` | 固定上下文 | **推荐** |
| [garrytan/gstack](https://github.com/garrytan/gstack) 的 `gstack-context-bill` | 离线审计已装 skill 各自的 token 成本 | 固定上下文 | **推荐** |
| [kenn-io/agentsview](https://github.com/kenn-io/agentsview) | 本地会话分析，按模型、含缓存的成本 | 观测 | **推荐** |
| [Graphify-Labs/graphify](https://github.com/Graphify-Labs/graphify) | 代码库转知识图谱，查图代替反复读文件 | 工具输出 | 大代码库推荐 |
| [xingkongliang/skills-manager](https://github.com/xingkongliang/skills-manager) | GUI，按项目的 skill 预设 | 固定上下文 | 想用图形界面就选它 |
| [codeprakhar25/optimize](https://github.com/codeprakhar25/optimize) | 扫描历史，给不用的 skill 写 `skillOverrides` | 固定上下文 | 思路对，本仓库脚本做的同一件事 |
| [DietrichGebert/ponytail](https://github.com/DietrichGebert/ponytail) | 写代码前走「最懒」阶梯：能不写就不写 | 输出 + 后续重读 | 借鉴。基准最可信（-22% token），且主动撤回过夸大数据 |
| [JuliusBrussee/caveman](https://github.com/JuliusBrussee/caveman) | 穴居人式简短回复 + 压缩工具输出的 proxy | 输出 / 工具输出 | proxy 部分有价值；简短回复部分收益小，ponytail 的测试里甚至 +7% |
| [oratelecom/tokenwar](https://github.com/oratelecom/tokenwar) | 打包 7 个工具（caveman、RTK、context-mode、claude-mem…） | 全部 | 借鉴分类思路，不建议整包装 |
| [nesaminua/claude-code-lsp-enforcement-kit](https://github.com/nesaminua/claude-code-lsp-enforcement-kit) | hook 强制用 LSP 查符号代替 grep+read | 工具输出 | 借鉴。用 hook 强制比写规则可靠 |
| [drona23/claude-token-efficient](https://github.com/drona23/claude-token-efficient) | 一份让回复变简洁的 CLAUDE.md | 输出 | 可以抄几条，别指望大幅省 |
| [100yenadmin/fable-token-saving-skills-orchestrator](https://github.com/100yenadmin/fable-token-saving-skills-orchestrator) | 按 5 分钟缓存窗口安排编排节奏 | 缓存 | 借鉴思路 |
| [shanraisshan/claude-code-best-practice](https://github.com/shanraisshan/claude-code-best-practice) | 实践合集：手动 compact、上下文腐化阈值 | 习惯 | 值得读 |
| [affaan-m/ECC](https://github.com/affaan-m/ECC) | 默认装 292 个 skill + 68 个 agent | — | **反而增加开销**，除非用精简 profile |
| [nyldn/claude-octopus](https://github.com/nyldn/claude-octopus) | 多模型「议会」 | — | 设计上就是乘法开销，一次 60–150k |
| [thedotmack/claude-mem](https://github.com/thedotmack/claude-mem) | 每次工具调用后跑 hook 记忆压缩 | — | 检索设计值得学，但每步都有 hook 开销 |
| [yoloshii/ClawMem](https://github.com/yoloshii/ClawMem) | 每条 prompt 注入记忆 | — | 增加开销 |

评估基于各项目 README 和公开数据，没有逐个安装实测。

---

## 4. 在你的电脑上跑

```bash
git clone https://github.com/qiangweihewu/claude-code-token-audit
cd claude-code-token-audit
python3 analyze.py            # 最近 14 天
python3 analyze.py --days 30
```

只读 `~/.claude/projects/` 下的本地日志，只用 Python 标准库，不联网、不上传。输出包括：各模型用量、主会话 vs subagent 占比、开场上下文大小、工具输出重读估算、中途切换模型次数、实际用过的 skill。

**口径说明**：
- 工具输出重读量是估算：文本按约 3.5 字符/token、图片按约 1.6k token 计，遇到 compact 截止。
- 成本权重用的是官方价格比例（缓存读 0.1×、写 1.25–2×、输出 5×），订阅额度的实际计算方式可能不同。
- 只统计日志文件修改时间在窗口内的会话。

---

## 5. 优先级（按我的数据）

1. **少派 subagent**，派就派对模型 —— 最大头
2. **砍开场固定上下文**：skillOverrides、禁用不用的插件和 MCP、精简 CLAUDE.md —— 每次调用都受益，subagent 越多收益越大
3. **压缩工具输出**：`bashOutputMaxChars`、RTK 类工具 —— 约 5–8%
4. 机械批处理别走 Claude Code 会话
5. 「让 AI 少说话」—— 最后才考虑

---

## English summary

My setup: Fable 5.1 only orchestrates, and all real work is delegated to Opus/Sonnet subagents. I audited 14 days of my local Claude Code logs (~46k API calls). **84% of tokens went to subagents** (1,047 of them, each starting from a 47k-token fixed context, with orchestrator, implementer and verifier re-reading the same material); output was only ~6–7% of weighted cost, so "make Claude terse" tools have a low ceiling. Only 25 of ~480 installed skills were used in 60 days. Fixed per-session context was ~40k tokens, re-read on every call. Cutting it with the native `skillOverrides` setting (hide unused skills from the model, keep them invokable via `/name`), disabling unused plugins/MCP, and slimming CLAUDE.md brought it to ~25k. Mid-session model switching turned out negligible (~1%). Old tool output being re-read accounts for ~17% of cache reads, which is where output-compression tools like RTK actually help. Run `python3 analyze.py` to see your own numbers — stdlib only, fully local.

## License

MIT
