export const DEMO_TARGET_DURATION_MS = 82_000;
export const DEMO_PROVENANCE_LABEL = "本地 API · CASSETTE 回放 · 外网已阻断";

export interface DemoScene {
  body: string;
  holdMs: number;
  key: string;
  kicker: string;
  title: string;
  variant?: "caption" | "hero";
}

export interface DemoReadmeFrame {
  capturePosition: "primary" | "secondary";
  filename: string;
  sceneKey: string;
}

export const DEMO_SCENES = Object.freeze([
  {
    body: "面向游戏内容的正确性编译器与 Agent 工作台。",
    holdMs: 3_500,
    key: "intro",
    kicker: "真实本地链路 · 无配音 V2",
    title: "GameForge",
    variant: "hero",
  },
  {
    body: "每个候选都始于不可变 Spec 与冻结的权威输入。",
    holdMs: 4_000,
    key: "spec",
    kicker: "01 / 设计规范 IR",
    title: "版本化意图，精确约束。",
  },
  {
    body: "实体、关系与证据始终可检查。",
    holdMs: 3_500,
    key: "graph",
    kicker: "02 / 知识图谱",
    title: "这张图可探索，不是装饰。",
  },
  {
    body: "有边界的 Agent 负责提议；确定性初步门禁决定能否继续。",
    holdMs: 5_000,
    key: "generation",
    kicker: "03 / 内容生成",
    title: "提议与裁决，彼此分离。",
  },
  {
    body: "检查器证据不会被混同为 LLM 意见。",
    holdMs: 4_500,
    key: "review",
    kicker: "04 / 审查报告",
    title: "裁决与建议，分栏呈现。",
  },
  {
    body: "Aureus 将行为失败转化为可回放证据。",
    holdMs: 5_000,
    key: "failed-playtest",
    kicker: "05 / 自动试玩",
    title: "运行结束了，任务没有通过。",
  },
  {
    body: "Review、轨迹、Finding 与回归证据共同绑定精确版本。",
    holdMs: 4_500,
    key: "failed-validation",
    kicker: "06 / 精确验证",
    title: "失败不能推动 live ref。",
  },
  {
    body: "修复使用 cassette 回放；确定性检查始终保有裁决权。",
    holdMs: 4_500,
    key: "repair",
    kicker: "07 / 修复",
    title: "新建不可变版本，不覆写历史。",
  },
  {
    body: "修复候选必须重新赢得一组证据。",
    holdMs: 5_000,
    key: "passed-playtest",
    kicker: "08 / 回归验证",
    title: "重新 Review，重新 Playtest，通过。",
  },
  {
    body: "第二个身份批准精确的目标版本。",
    holdMs: 4_500,
    key: "approval",
    kicker: "09 / 审批治理",
    title: "提议者不能批准自己的提议。",
  },
  {
    body: "这次迁移仍然可版本化、可审计、可回滚。",
    holdMs: 4_000,
    key: "apply",
    kicker: "10 / 应用变更",
    title: "只有此刻，live ref 才会移动。",
  },
  {
    body: "版本化 Bench 证据保持可检查。",
    holdMs: 4_000,
    key: "eval",
    kicker: "11 / 评测基准",
    title: "证据始终附着于结论。",
  },
  {
    body: "结构化日志与 cassette 成本始终绑定精确 Run。",
    holdMs: 4_500,
    key: "observability",
    kicker: "12 / 可观测性",
    title: "每次运行都留下可追踪证据。",
  },
  {
    body: "有边界的 Agent · 人工批准的变更",
    holdMs: 3_500,
    key: "outro",
    kicker: "确定性裁决",
    title: "让游戏内容可以被证明。",
    variant: "hero",
  },
] satisfies readonly DemoScene[]);

export const DEMO_README_FRAMES = Object.freeze([
  { capturePosition: "primary", filename: "01-spec-authority.png", sceneKey: "spec" },
  { capturePosition: "primary", filename: "02-knowledge-graph.png", sceneKey: "graph" },
  { capturePosition: "secondary", filename: "03-generation-gate.png", sceneKey: "generation" },
  { capturePosition: "secondary", filename: "04-review-evidence.png", sceneKey: "review" },
  { capturePosition: "secondary", filename: "05-playtest-failure.png", sceneKey: "failed-playtest" },
  { capturePosition: "primary", filename: "06-validation-failure.png", sceneKey: "failed-validation" },
  { capturePosition: "primary", filename: "07-repair-revision.png", sceneKey: "repair" },
  { capturePosition: "secondary", filename: "08-playtest-regression.png", sceneKey: "passed-playtest" },
  { capturePosition: "secondary", filename: "09-maker-checker-approval.png", sceneKey: "approval" },
  { capturePosition: "primary", filename: "10-eval-bench.png", sceneKey: "eval" },
  { capturePosition: "secondary", filename: "11-observability.png", sceneKey: "observability" },
] satisfies readonly DemoReadmeFrame[]);

const DISALLOWED_CLAIMS = ["100% correct", "online llm", "production-ready", "live model"];

export function validateDemoStoryboard(): string[] {
  const errors: string[] = [];
  const keys = new Set<string>();

  for (const scene of DEMO_SCENES) {
    if (keys.has(scene.key)) errors.push(`duplicate scene key: ${scene.key}`);
    keys.add(scene.key);
    if (scene.holdMs <= 0) errors.push(`non-positive hold: ${scene.key}`);

    const copy = `${scene.kicker} ${scene.title} ${scene.body}`.toLowerCase();
    for (const claim of DISALLOWED_CLAIMS) {
      if (copy.includes(claim)) errors.push(`disallowed claim in ${scene.key}: ${claim}`);
    }
  }

  if (DEMO_TARGET_DURATION_MS < 75_000 || DEMO_TARGET_DURATION_MS > 90_000) {
    errors.push("target duration must remain between 75 and 90 seconds");
  }
  return errors;
}
