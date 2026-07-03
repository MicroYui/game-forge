我觉得**最适合你的主项目**不是单纯做“AI NPC 聊天”或“多 Agent 小镇”，而是做一个更贴近大厂真实岗位的完整产品：

# 主推项目：GameForge Agent —— 游戏研发智能体工作台

一句话：**把一份游戏策划案 / 配置表 / 任务设计输入进去，Agent 能生成可运行内容、自动进游戏测试、发现问题、给出可审查的修复 diff，并在 Dashboard 里展示评测结果。**

这个方向很对口。腾讯游戏 AI Agent 岗位明确提到：策划辅助、剧情生成、任务设计、关卡检查、战斗调参、资产管理、Bug 分析、自动化测试、接入 UE5 / 任务系统 / 战斗系统，并要求建立任务成功率、人工节省时长、生成质量、一致性、稳定性、安全性等评估体系。([swiftcruit.ai][1]) 米哈游公开内推帖里也强调 Agent 在游戏研发管线中的应用，包括代码审查、自动化测试、策划配置生成、程序代码生成、美术资产生成，以及和策划、美术、程序团队协同重构工业化管线。([牛客网][2])

所以你做这个，面试官会觉得：**这不是普通 LLM 套壳，而是在模拟他们真实想落地的游戏研发 Agent。**

---

## 产品形态

你可以做成一个完整独立产品，名字可以叫：

**GameForge Agent：面向游戏研发流程的多智能体内容生成与自动化测试平台**

它包含 4 个核心模块：

### 1. 策划文档理解 Agent

输入：

游戏设定、角色设定、任务文档、数值配置表、地图区域说明、道具表。

输出：

结构化的世界模型，例如：

```json
{
  "factions": ["星穹商会", "旧城守卫"],
  "characters": ["林澈", "艾琳"],
  "quests": ["寻找失踪商队"],
  "constraints": [
    "林澈不能在第三章前暴露真实身份",
    "旧城守卫与星穹商会敌对",
    "任务奖励不能超过当前阶段经济上限"
  ]
}
```

这个模块不是简单 RAG，而是做成 **Game Knowledge Graph + Constraint Store**。也就是它会把设定、任务、角色、数值之间的关系抽出来，后续所有 Agent 都基于这个知识图谱工作。

这点很适合你，因为你本来就做过长期记忆、Recall、Compaction、Dashboard 这些东西，可以把你的 `mem-trace` 经验包装成“游戏研发知识记忆系统”。

---

### 2. 内容生成 Agent

它可以生成：

任务链、NPC 对话、支线剧情、道具描述、关卡事件、怪物配置、战斗参数建议。

但重点不是“生成文本”，而是生成**可落地的游戏配置**。

比如你做一个简化 Unity/网页游戏 Demo，任务配置长这样：

```json
{
  "quest_id": "q_missing_caravan",
  "title": "失踪的商队",
  "start_npc": "npc_lincheng",
  "steps": [
    {
      "type": "talk",
      "target": "npc_lincheng"
    },
    {
      "type": "collect",
      "item": "broken_emblem",
      "count": 3
    },
    {
      "type": "fight",
      "enemy_group": "bandit_small"
    }
  ],
  "reward": {
    "gold": 120,
    "item": "low_tier_blade"
  }
}
```

生成后不是直接“完成”，而是进入下一步检查。

---

### 3. 多角色 Review Agent

这里是创新点之一。

同一个生成结果，会被多个 Agent 从不同角度审查：

| Agent      | 关注点               |
| ---------- | ----------------- |
| 策划 Agent   | 任务是否有趣、节奏是否合理     |
| 世界观 Agent  | 是否违反角色设定、剧情设定     |
| 数值 Agent   | 奖励、怪物强度、资源产出是否失衡  |
| QA Agent   | 是否存在死任务、无法完成、循环依赖 |
| 玩家体验 Agent | 是否太长、太重复、目标不清楚    |

最后输出一个统一的 Review Report：

```text
严重问题：
1. q_missing_caravan 第 3 步需要 item_broken_emblem，但当前地图没有任何掉落源。
2. 奖励 gold=120 高于新手区推荐上限 80，可能破坏早期经济。

建议修复：
- 给 bandit_small 增加 broken_emblem 掉落。
- 将 gold 从 120 调整为 70。
```

这个比“多 Agent 聊天”更高级，因为它是**角色分工 + 结构化检查 + 可执行修复**。

---

### 4. 自动化 Playtest Agent

这是最能拉开差距的部分。

你做一个小型可运行游戏环境，推荐两种路线：

第一种，**Web/Unity 2D Demo**。例如一个俯视角小 RPG：NPC、任务、战斗、背包、地图、对话。

第二种，**更工程向的模拟器**。不用做很漂亮的画面，重点是有任务系统、战斗系统、地图导航、配置加载、日志输出。

Playtest Agent 要能做这些事：

```text
1. 读取任务目标
2. 控制玩家移动到 NPC
3. 触发对话
4. 接任务
5. 找怪 / 找道具
6. 战斗
7. 交任务
8. 记录是否卡死、失败、循环、数值异常
```

它可以通过 API 操作游戏，而不一定非得通过视觉控制。比如：

```http
POST /game/action
{
  "action": "move_to",
  "target": "npc_lincheng"
}
```

然后游戏返回状态：

```json
{
  "player_pos": [12, 8],
  "current_quest": "q_missing_caravan",
  "inventory": ["broken_emblem"],
  "hp": 82,
  "logs": ["Quest step completed"]
}
```

这样你就能做出真正的 **Agent + Environment + Tool Calling + Evaluation** 闭环。

网易伏羲相关实践也强调智能 NPC / AI 队友不是单纯聊天，而是具备感知、表达、执行能力，并通过数据闭环持续优化；其 AI 队友涉及语音识别、语义理解、人设对话、语言生成、强化学习等链路。([智源社区][3]) 另一个网易伏羲案例也把智能 NPC 定义为具备感知、认知、决策与记忆能力、能与游戏环境交互的 Agent。([智能体开发者社区][4]) 你这个项目如果能让 Agent 真正“进游戏做事”，会比只做 NPC 对话强很多。

---

# 这个项目的独特创新点

## 创新点 1：从“生成内容”升级到“生成—验证—修复”闭环

很多项目停留在：

```text
输入：帮我写一个任务
输出：一段任务文案
```

你的项目要做到：

```text
输入策划目标
→ 生成任务配置
→ 多 Agent 检查
→ 自动进游戏跑任务
→ 发现 bug
→ 生成修复 diff
→ 再次回归测试
→ Dashboard 展示通过率
```

这就非常像工业界真实流程。

---

## 创新点 2：Game Knowledge Graph + 约束检查

比如世界观约束：

```text
角色 A 在第二章前不能死亡。
阵营 X 和阵营 Y 不可能合作。
道具 Z 是唯一神器，不能作为普通掉落。
```

数值约束：

```text
新手区金币奖励 <= 80
普通怪血量范围 50–120
三分钟内任务完成率应大于 70%
```

任务约束：

```text
每个 collect 任务必须有 item source。
每个 talk 任务必须有对应 NPC。
每个 quest step 必须可达。
```

这会让项目从“LLM 应用”变成“游戏研发 Agent 系统”。

---

## 创新点 3：可观测、可评测、可复现

你需要做一个 Dashboard，展示：

| 指标                  | 含义             |
| ------------------- | -------------- |
| Task Success Rate   | Agent 自动完成任务比例 |
| Bug Detection Rate  | 能发现多少配置错误      |
| Fix Pass Rate       | Agent 修复后回归通过率 |
| Consistency Score   | 是否符合世界观设定      |
| Balance Score       | 数值是否在合理区间      |
| Human Edit Distance | 人类需要改多少        |
| Cost / Latency      | 调用成本和耗时        |

腾讯 JD 里明确提到要建立 Agent 效果评估体系，包括任务成功率、人工节省时长、生成质量、一致性、稳定性、安全性等指标。([swiftcruit.ai][1]) 你如果简历上直接写这些指标，命中度会非常高。

---

# 最小可行版本怎么做

我建议不要一上来做 UE5。秋招前时间有限，UE5 插件会把你拖进大量工程细节。你可以先做：

## MVP 版本：2D RPG Quest Agent Platform

技术栈：

```text
前端：React + TypeScript + Vite
游戏 Demo：Phaser.js 或 PixiJS
后端：FastAPI / Node.js
Agent Orchestration：LangGraph / 自研状态机
存储：PostgreSQL + pgvector / SQLite 起步
模型：OpenAI / Qwen / Claude 可切换
配置：JSON / YAML
Dashboard：任务图、测试轨迹、Agent 日志、diff 视图
```

页面包括：

1. **Design Doc 页面**：上传/编辑世界观、角色、任务需求。
2. **Generation 页面**：生成任务、对话、怪物配置。
3. **Review 页面**：多 Agent 审查报告。
4. **Playtest 页面**：自动跑游戏，展示轨迹和失败点。
5. **Patch 页面**：展示配置 diff，一键应用。
6. **Eval 页面**：多轮 benchmark 指标。

---

# Demo 场景可以这样设计

不要做泛泛的“游戏生成器”，做一个具体垂直场景：

## 场景：开放世界 RPG 支线任务生产与质检

输入：

```text
请生成一个适合新手村的支线任务。
要求：
- 任务时长 3–5 分钟
- 包含 1 次 NPC 对话、1 次探索、1 次轻量战斗
- 奖励不能破坏早期经济
- 不能暴露主线角色“白鸢”的真实身份
```

Agent 输出：

1. 任务配置 JSON。
2. NPC 对话。
3. 怪物配置。
4. 掉落配置。
5. 地图事件配置。

然后 Playtest Agent 自动跑。

故意在 benchmark 里放一些坏 case：

```text
1. NPC 不存在。
2. 道具没有掉落源。
3. 任务目标在不可达区域。
4. 奖励过高。
5. 对话泄露未解锁剧情。
6. 怪物强度超过新手区上限。
7. 任务循环依赖。
8. 任务完成条件无法触发。
```

最后展示：

```text
GameForge Agent 在 50 个任务样本上：
- 发现 43/50 个配置问题
- 自动修复 36/43 个问题
- 回归测试通过率 82%
- 平均每个任务节省人工检查时间 6.4 分钟
```

这就是简历里的含金量。

---

# 简历上怎么写会很强

可以写成这样：

> 独立设计并实现 GameForge Agent，一个面向游戏研发流程的多智能体内容生成与自动化测试平台。系统支持从策划文档中构建游戏知识图谱，自动生成任务 / NPC 对话 / 数值配置，并通过多角色 Agent 进行世界观一致性、任务可达性、数值平衡和玩家体验审查。进一步接入可运行 2D RPG 环境，Playtest Agent 可自动执行任务链、发现配置缺陷并生成可审查修复 diff。构建包含 50+ 缺陷样例的游戏研发 Agent Benchmark，评估任务成功率、Bug 检出率、修复通过率、一致性与调用成本。

关键词很重要：

```text
Game AI Agent
游戏研发工具链
Multi-Agent Review
自动化 Playtest
Tool Calling
RAG / Knowledge Graph
可执行环境
可观测 Agent
Benchmark / Evaluation
Human-in-the-loop
Diff-based Patch
```

这些关键词对米哈游、网易、腾讯都很友好。

---

# 为什么它比“AI NPC 项目”更适合秋招

AI NPC 很容易撞车。现在很多人都会做：

```text
NPC + Prompt + RAG + 长期记忆 + TTS
```

这个方向当然也有价值，尤其网易伏羲就有智能 NPC、AI 队友、实时语音交互等实践。([智能体开发者社区][4]) 但作为秋招项目，它容易被面试官追问：

```text
你的 NPC 和普通聊天机器人有什么区别？
真的接入游戏了吗？
能控制游戏状态吗？
怎么评测？
延迟、成本、安全怎么处理？
```

而 GameForge Agent 的优势是，它天然回答了这些问题：

```text
我不是只做对话。
我做的是游戏研发流程里的 Agent。
它能生成内容、检查内容、进入环境测试、发现 bug、修复配置、回归验证。
我还有 benchmark 和 dashboard。
```

这会更像“工程岗位候选人”，而不是“套壳 Demo 候选人”。

---

# 我的最终建议

你最应该做的是：

## GameForge Agent：游戏任务生成 + 多 Agent 审查 + 自动 Playtest + 修复闭环

不要做太大，但要做完整闭环。

做到这个程度就够强：

```text
1. 一个可运行 2D RPG Demo
2. 一个 Agent 后端
3. 一个 Web Dashboard
4. 支持上传策划文档 / 配置
5. 自动生成任务配置
6. 多 Agent 审查
7. 自动跑任务测试
8. 发现 5–8 类典型 bug
9. 自动生成修复 diff
10. 有一套 benchmark 和量化指标
```

这个项目同时命中：

```text
米哈游：AI + 游戏研发管线 + 体验升级 + 工程落地
网易：智能交互、Agent 感知/决策/记忆、数据闭环
腾讯：游戏研发 Agent、UE/工具链接入、自动化测试、评估体系
```

而且它和你已有能力高度重合：长期记忆、Agent、Dashboard、评测、工程系统、游戏策划兴趣，都能放进去。这个项目如果完成度高，确实可以作为秋招敲门砖。

[1]: https://www.swiftcruit.ai/jobs/ai-agent-3535?utm_source=chatgpt.com "AI Agent开发工程师（游戏研发） at tencent"
[2]: https://www.nowcoder.com/discuss/860885674365317120 "游戏AI研发实习生（Agent方向）| 米哈游| 内推 | 转正率高_牛客网"
[3]: https://hub.baai.ac.cn/view/41514 "和网易伏羲共探100个值得深入学习的技术创新案例｜TOP100Summit - 智源社区"
[4]: https://adg.csdn.net/6970a470437a6b40336b0483.html "当游戏NPC有了“灵魂”，网易伏羲解码游戏智能交互场景新实践_人工智能_网易伏羲-智能体开发者社区"
[5]: https://arxiv.org/abs/2304.03442 "Generative Agents: Interactive Simulacra of Human Behavior"
