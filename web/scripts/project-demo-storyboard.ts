export const PROJECT_DEMO_TARGET_DURATION_MS = 90_000;
export const PROJECT_DEMO_PROVENANCE_LABEL = "本地 API / Worker · 固定模型替身 · 浏览器外网已阻断";

export interface ProjectDemoScene {
  body: string;
  holdMs: number;
  key: string;
  kicker: string;
  title: string;
  variant?: "caption" | "hero";
}

export interface ProjectDemoReadmeFrame {
  filename: string;
  sceneKey: string;
}

export const PROJECT_DEMO_SCENES = Object.freeze([
  {
    body: "新建游戏项目、组合策划材料、生成可编辑内容，再用规则、审批与试玩逐步收敛。",
    holdMs: 4_000,
    key: "intro",
    kicker: "GameForge · 当前产品旅程",
    title: "从一份策划，走到可验证的游戏版本。",
    variant: "hero",
  },
  {
    body: "先创建“天空港计划”。项目会持续保存材料、提案、内容版本、规则与运行证据。",
    holdMs: 4_500,
    key: "project",
    kicker: "01 / 建立游戏项目",
    title: "创作从项目开始，不从一次临时对话开始。",
  },
  {
    body: "粘贴飞书块 JSON 并命名材料；同一项目可以长期累积和组合多份策划来源。",
    holdMs: 4_500,
    key: "material",
    kicker: "02 / 加入策划材料",
    title: "原始材料先成为可追溯的项目资产。",
  },
  {
    body: "AI 只产生候选。确定性同一化把 air.quality 与 air_quality 识别为同一内容，草案仍可编辑。",
    holdMs: 5_500,
    key: "proposal",
    kicker: "03 / 提取内容提案",
    title: "先消解身份，再让人检查实体与关系。",
  },
  {
    body: "策划可以补充“云港向导”，并把它明确连接到天空港；修改发生在候选图谱上。",
    holdMs: 4_500,
    key: "graph-edit",
    kicker: "04 / 编辑内容图谱",
    title: "AI 草案不是黑盒终稿。",
  },
  {
    body: "候选通过确定性验证、平台管理员显式自审与 Apply 后，项目首页才显示第 1 版内容。",
    holdMs: 5_000,
    key: "content-v1",
    kicker: "05 / 发布首个内容版本",
    title: "正式版本来自证据与明确决定。",
  },
  {
    body: "AI 起草“任务依赖必须无环”，Human 修订，编译器与验证器给出确定性证据，再发布为权威规则。",
    holdMs: 5_000,
    key: "rules",
    kicker: "06 / 建立项目规则",
    title: "模型写提案，模型不裁定对错。",
  },
  {
    body: "后续生成同时绑定当前内容、项目材料与已发布规则；这次新增“风暴观测员”。",
    holdMs: 5_500,
    key: "continuation",
    kicker: "07 / 基于现状继续创作",
    title: "每次生成都站在项目的当前版本上。",
  },
  {
    body: "新候选重新走验证、审批与 Apply，项目进入第 2 版；旧版本和发布历史仍被保留。",
    holdMs: 4_500,
    key: "content-v2",
    kicker: "08 / 形成第二个版本",
    title: "版本前进，历史不被改写。",
  },
  {
    body: "当前材料没有定义完整可玩任务链，真实 Playtest 因而报告“仍有试玩任务未完成”，不会伪造成功。",
    holdMs: 5_500,
    key: "playtest",
    kicker: "09 / 进入自动试玩",
    title: "证据不足时，产品给出诚实的失败结果。",
  },
  {
    body: "项目把材料、AI 提案、确定性验证、版本治理与真实试玩连成一条可回放的生产链。",
    holdMs: 4_000,
    key: "outro",
    kicker: "GameForge · 游戏内容正确性编译器",
    title: "让 AI 加速创作，让证据决定能否发布。",
    variant: "hero",
  },
] satisfies readonly ProjectDemoScene[]);

export const PROJECT_DEMO_README_FRAMES = Object.freeze([
  { filename: "project-flow-01-project.png", sceneKey: "project" },
  { filename: "project-flow-02-material.png", sceneKey: "material" },
  { filename: "project-flow-03-proposal.png", sceneKey: "proposal" },
  { filename: "project-flow-04-graph-edit.png", sceneKey: "graph-edit" },
  { filename: "project-flow-05-content-v1.png", sceneKey: "content-v1" },
  { filename: "project-flow-06-rules.png", sceneKey: "rules" },
  { filename: "project-flow-07-continuation.png", sceneKey: "continuation" },
  { filename: "project-flow-08-content-v2.png", sceneKey: "content-v2" },
  { filename: "project-flow-09-playtest.png", sceneKey: "playtest" },
] satisfies readonly ProjectDemoReadmeFrame[]);

const DISALLOWED_CLAIMS = [
  "100% correct",
  "online llm",
  "production-ready",
  "live model",
  "100% 正确",
  "在线模型",
  "生产就绪",
];

export function validateProjectDemoStoryboard(): string[] {
  const errors: string[] = [];
  const keys = new Set<string>();

  for (const scene of PROJECT_DEMO_SCENES) {
    if (keys.has(scene.key)) errors.push(`duplicate scene key: ${scene.key}`);
    keys.add(scene.key);
    if (scene.holdMs <= 0) errors.push(`non-positive hold: ${scene.key}`);

    const copy = `${scene.kicker} ${scene.title} ${scene.body}`.toLowerCase();
    for (const claim of DISALLOWED_CLAIMS) {
      if (copy.includes(claim)) errors.push(`disallowed claim in ${scene.key}: ${claim}`);
    }
  }

  for (const frame of PROJECT_DEMO_README_FRAMES) {
    if (!keys.has(frame.sceneKey)) errors.push(`unknown README frame scene: ${frame.sceneKey}`);
  }

  if (PROJECT_DEMO_TARGET_DURATION_MS < 85_000 || PROJECT_DEMO_TARGET_DURATION_MS > 100_000) {
    errors.push("target duration must remain between 85 and 100 seconds");
  }
  return errors;
}
