export const DEMO_TARGET_DURATION_MS = 86_000;
export const DEMO_PROVENANCE_LABEL = "本地 API · CASSETTE 回放 · 外网已阻断";
export const DEMO_AUTHORING_GOAL = "Raise the caravan emblem requirement from three to four.";

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
    body: "当前任务需要 3 枚徽记，地图也只提供 3 枚。现在请求把需求提高到 4。",
    holdMs: 3_500,
    key: "intro",
    kicker: "一个真实任务 · 完整使用流程",
    title: "把徽记需求从 3 改成 4，会发生什么？",
    variant: "hero",
  },
  {
    body: "在“内容生成”页填写：把商队任务需要的徽记从 3 枚提高到 4 枚，然后开始生成。",
    holdMs: 6_500,
    key: "input",
    kicker: "01 / 在哪里输入",
    title: "先告诉 GameForge 想改什么。",
  },
  {
    body: "Agent 生成 Patch、预览与配置导出；它们只是候选，正式版本仍保持 3 / 3。",
    holdMs: 5_000,
    key: "generation",
    kicker: "02 / 生成候选",
    title: "生成成功，不等于已经上线。",
  },
  {
    body: "差异页明确显示 Base = 3、Proposed = 4；地图的徽记供给仍是 3。",
    holdMs: 5_500,
    key: "candidate-diff",
    kicker: "03 / 查看变化",
    title: "候选真的把需求从 3 改成了 4。",
  },
  {
    body: "确定性证据、仿真结果与模型建议分开呈现，用户可以逐项检查。",
    holdMs: 4_500,
    key: "review",
    kicker: "04 / 审查候选",
    title: "先检查，再进入真实试玩。",
  },
  {
    body: "任务要求 4 枚，但地图只提供 3 枚；Agent 无法取得第 4 枚，任务失败。",
    holdMs: 6_500,
    key: "failed-playtest",
    kicker: "05 / 自动试玩",
    title: "真实运行找到了纸面审查漏掉的问题。",
  },
  {
    body: "验证失败后，提交审批与 Apply 都不可用；这个危险候选没有进入 live ref。",
    holdMs: 5_000,
    key: "failed-validation",
    kicker: "06 / 阻止发布",
    title: "失败证据把正式版本挡在安全状态。",
  },
  {
    body: "本次修复撤销这项孤立变更，把任务需求从 4 恢复为 3，并创建新 revision。",
    holdMs: 5_500,
    key: "repair",
    kicker: "07 / 修复",
    title: "修复历史，而不是改写历史。",
  },
  {
    body: "需求与供给重新一致为 3 / 3；新 revision 重新 Review、重新 Playtest 并通过。",
    holdMs: 5_500,
    key: "passed-playtest",
    kicker: "08 / 回归验证",
    title: "修复后必须从头证明一次。",
  },
  {
    body: "提议者不能批准自己的变更；第二个身份只批准已经验证的精确 revision。",
    holdMs: 4_500,
    key: "approval",
    kicker: "09 / 审批治理",
    title: "验证通过，仍然不能绕过审批。",
  },
  {
    body: "ref history 记录新的安全 revision；不安全的“需求 4、供给 3”从未成为正式内容。",
    holdMs: 5_000,
    key: "apply",
    kicker: "10 / 应用变更",
    title: "只有验证过的版本，才能移动 live ref。",
  },
  {
    body: "从一句修改需求，到候选、失败、修复、复测、审批与发布，每一步都有证据。",
    holdMs: 4_000,
    key: "outro",
    kicker: "GameForge · 游戏内容正确性编译器",
    title: "输入会产生结果，错误不会直接上线。",
    variant: "hero",
  },
] satisfies readonly DemoScene[]);

export const DEMO_README_FRAMES = Object.freeze([
  { capturePosition: "primary", filename: "flow-01-input.png", sceneKey: "input" },
  { capturePosition: "secondary", filename: "flow-02-candidate.png", sceneKey: "generation" },
  { capturePosition: "primary", filename: "flow-03-diff.png", sceneKey: "candidate-diff" },
  { capturePosition: "secondary", filename: "flow-04-review.png", sceneKey: "review" },
  { capturePosition: "secondary", filename: "flow-05-playtest-failure.png", sceneKey: "failed-playtest" },
  { capturePosition: "primary", filename: "flow-06-release-blocked.png", sceneKey: "failed-validation" },
  { capturePosition: "primary", filename: "flow-07-repair.png", sceneKey: "repair" },
  { capturePosition: "secondary", filename: "flow-08-regression-passed.png", sceneKey: "passed-playtest" },
  { capturePosition: "primary", filename: "flow-09-independent-approval.png", sceneKey: "approval" },
  { capturePosition: "primary", filename: "flow-10-live-ref-history.png", sceneKey: "apply" },
] satisfies readonly DemoReadmeFrame[]);

const DISALLOWED_CLAIMS = [
  "100% correct",
  "online llm",
  "production-ready",
  "live model",
  "journey a",
  "m4d",
  "v2",
];

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
