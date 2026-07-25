import { setTimeout as delay } from "node:timers/promises";

import { expect, test, type Locator, type Page } from "@playwright/test";

import {
  guardAuthoringEgress,
  loginAuthoringPage,
  startAuthoringStack,
  type AuthoringStack,
} from "./support/authoring-live-stack";

const adminCredentials = { login: "admin", password: "admin-password-1" };
const feishuMaterial = JSON.stringify({
  blocks: [
    {
      block_type: 3,
      heading1: { elements: [{ text_run: { content: "世界观" } }] },
    },
    {
      block_type: 2,
      text: { elements: [{ text_run: { content: "天空港由天气管理员维护。" } }] },
    },
    {
      block_type: 12,
      bullet: {
        elements: [
          {
            text_run: {
              content: "空气质量 air.quality 与 air_quality 是同一属性",
            },
          },
        ],
      },
    },
  ],
});

let stack: AuthoringStack | undefined;

async function waitForEnabled(page: Page, locator: Locator): Promise<void> {
  for (let attempt = 0; attempt < 120; attempt += 1) {
    if (await locator.isEnabled().catch(() => false)) return;
    await delay(250);
    await page.reload();
  }
  await expect(locator).toBeEnabled();
}

async function approveAsPlatformAdmin(page: Page): Promise<void> {
  const requirements = page.getByRole("checkbox", { name: /^选择 /u });
  await expect(requirements.first()).toBeVisible();
  const count = await requirements.count();
  expect(count).toBeGreaterThan(0);
  for (let index = 0; index < count; index += 1) {
    await expect(requirements.nth(index)).toBeEnabled();
    await requirements.nth(index).check();
  }
  await page
    .getByRole("combobox", { name: "决定原因", exact: true })
    .selectOption("content_and_evidence_reviewed");
  await page.getByLabel("补充说明").fill("平台管理员已核对内容差异和确定性验证证据。");
  await page.getByRole("button", { name: "提交批准" }).click();
  const confirmation = page.getByRole("dialog", { name: "确认批准决定" });
  await expect(confirmation).toBeVisible();
  await page.getByRole("button", { name: "确认批准" }).click();
  await expect(page.getByText("已批准", { exact: true }).first()).toBeVisible();
}

test.describe("project-first-authoring", () => {
  test.describe.configure({ mode: "serial" });
  test.setTimeout(300_000);

  test.beforeAll(async () => {
    test.setTimeout(120_000);
    stack = await startAuthoringStack({
      launcherModule: "tests.e2e.m4d_support.project_live",
      manifestName: "project-live-manifest.json",
      transportLogName: "project-live-transport.log",
      workspacePrefix: "gameforge-project-first-",
    });
  });

  test.afterAll(async () => {
    await stack?.stop();
  });

  test("creates a game from Feishu material, publishes content, and establishes project rules", async ({
    browser,
  }) => {
    if (stack === undefined) throw new Error("Project authoring stack did not start.");
    const unexpectedRequests = new Set<string>();
    const context = await browser.newContext({
      baseURL: stack.baseURL,
      ignoreHTTPSErrors: true,
    });
    await guardAuthoringEgress(context, stack.baseURL, unexpectedRequests);
    const page = await context.newPage();
    page.setDefaultTimeout(15_000);

    try {
      await loginAuthoringPage(page, adminCredentials);
      await expect(page.getByRole("heading", { name: "游戏项目", level: 1 })).toBeVisible();

      await page.getByLabel("游戏名称").fill("天空港计划");
      await page.getByLabel("项目代号").fill("sky-harbor");
      await page.getByLabel("游戏类型").fill("叙事经营");
      await page
        .getByLabel("一句话创意")
        .fill("玩家经营一座漂浮在云海中的港口，并与天气管理员共同维护生态。");
      await page.getByRole("button", { name: "创建并添加材料" }).click();
      await expect(page.getByRole("heading", { name: "天空港计划", level: 1 })).toBeVisible();

      const projectPath = new URL(page.url()).pathname;
      expect(projectPath).toMatch(/^\/projects\/project%3A/u);

      await page.getByLabel("材料名称").fill("天空港核心创意");
      await page.getByLabel("粘贴格式").selectOption("feishu_blocks_json");
      await page.getByLabel("策划内容").fill(feishuMaterial);
      await page.getByRole("button", { name: "保存这份材料" }).click();
      await expect(page.getByText("材料已保存，可以交给 AI 提取。", { exact: false })).toBeVisible();
      await expect(page.getByText("天空港核心创意", { exact: true }).last()).toBeVisible();

      await page.getByRole("button", { name: "AI 提取实体与关系" }).click();
      await expect(page.getByRole("heading", { name: "已得到可编辑内容草案" })).toBeVisible({
        timeout: 45_000,
      });
      const aliases = page.getByRole("region", { name: "同一内容识别结果" });
      await expect(aliases).toContainText("air.quality");
      await expect(aliases).toContainText("air_quality");
      await expect(page.getByRole("region", { name: "实体与关系编辑器" })).toBeVisible();

      await page.getByRole("button", { name: "添加实体" }).click();
      await page.getByLabel("内容名称").fill("云港向导");
      await expect(page.getByLabel("编辑已选内容").getByRole("heading", { name: "云港向导" })).toBeVisible();
      await page.getByRole("button", { name: "添加关系" }).click();
      await expect(page.getByRole("button", { name: "删除关系" })).toBeVisible();
      await page.getByRole("button", { name: "删除关系" }).click();
      await page.getByRole("button", { name: "添加关系" }).click();
      await page.getByLabel("关系类型").selectOption("LOCATED_IN");
      await page
        .getByLabel("编辑已选内容")
        .getByRole("combobox", { name: /^起点内容/u })
        .selectOption({ label: "云港向导" });
      await page
        .getByLabel("编辑已选内容")
        .getByRole("combobox", { name: /^终点内容/u })
        .selectOption({ label: "天空港" });

      await page.getByRole("button", { name: "创建发布草案" }).click();
      const publishLink = page.getByRole("link", { name: "验证并发布这个版本" });
      await expect(publishLink).toBeVisible();
      await publishLink.click();
      await expect(page.getByRole("heading", { name: /修改草案 · 第 \d+ 版/u, level: 1 })).toBeVisible();
      const patchPath = new URL(page.url()).pathname;

      const validate = page.getByRole("button", { name: "开始验证", exact: true });
      await waitForEnabled(page, validate);
      await validate.click();
      await expect(page.getByRole("link", { name: "查看已受理的运行" })).toBeVisible();

      const submit = page.getByRole("button", { name: "提交独立审批" });
      await waitForEnabled(page, submit);
      await submit.click();
      const approvalLink = page.getByRole("link", { name: "打开审批详情" });
      await expect(approvalLink).toBeVisible();
      await approvalLink.click();
      await expect(page.getByRole("heading", { name: "审批详情", level: 1 })).toBeVisible();
      await approveAsPlatformAdmin(page);

      await page.goto(patchPath);
      const apply = page.getByRole("button", { name: "应用已批准的修改" });
      await waitForEnabled(page, apply);
      await apply.click();
      await expect(page.getByRole("dialog", { name: "确认应用已批准的修改？" })).toBeVisible();
      await page.getByRole("button", { name: "确认应用" }).click();
      await expect(page.getByRole("heading", { name: "修改已应用" })).toBeVisible();

      await page.goto(projectPath);
      await expect(page.getByRole("heading", { name: "首个内容版本已发布" })).toBeVisible();
      const rulesLink = page.getByRole("link", { name: /生成与维护规则/u });
      await expect(rulesLink).toHaveAttribute("href", /\/specs\?.*project=/u);
      await rulesLink.click();

      await expect(page.getByRole("heading", { name: "内容与规则", level: 1 })).toBeVisible();
      await expect(page.getByText("已绑定天空港计划项目的 1 份策划材料")).toBeVisible();
      const ruleEntry = page.locator('article[data-entry="agent"]');
      await ruleEntry
        .getByLabel("你希望 AI 重点提取什么？")
        .fill("提取任务依赖必须无环的确定性规则，并保留策划材料来源。");
      await ruleEntry.getByText("高级设置", { exact: true }).click();
      await ruleEntry.getByRole("combobox", { name: "规则格式", exact: true }).selectOption("dsl@1");
      await ruleEntry
        .getByRole("combobox", { name: "AI 提取方案", exact: true })
        .selectOption("builtin.constraint_extraction@1");
      await ruleEntry.getByRole("combobox", { name: "AI 运行方式", exact: true }).selectOption("record");
      await ruleEntry.getByRole("button", { name: "生成规则提案" }).click();

      const projectProposals = ruleEntry.getByRole("link", { name: "提取完成后查看项目提案" });
      await expect(projectProposals).toBeVisible();
      const projectProposalsHref = await projectProposals.getAttribute("href");
      expect(projectProposalsHref).not.toBeNull();
      await ruleEntry.getByRole("link", { name: "查看提取进度" }).click();
      await expect(
        page.getByRole("region", { name: "运行状态" }).getByText("已完成", { exact: true }),
      ).toBeVisible({ timeout: 45_000 });

      await page.goto(projectProposalsHref!);
      const proposalLink = page.getByRole("link", { name: "查看提案", exact: true });
      await expect(proposalLink).toBeVisible();
      await proposalLink.click();
      await expect(page.getByRole("heading", { name: "规则修改草案", level: 1 })).toBeVisible();
      await expect(page.getByText("已绑定天空港计划项目的规则发布位置")).toBeVisible();
      await page.getByLabel("修订说明").fill("策划确认：任务步骤依赖图必须保持无环。");
      await page.getByRole("button", { name: "提交人工修订" }).click();
      await expect(page.getByText("人工已修订", { exact: true })).toBeVisible();
      const proposalPath = `${new URL(page.url()).pathname}${new URL(page.url()).search}`;

      await page
        .getByRole("combobox", { name: "约束编译器", exact: true })
        .selectOption("builtin.constraint_compiler@1");
      await page
        .getByRole("combobox", { name: "验证方案", exact: true })
        .selectOption("builtin.validation@1");
      await page.getByRole("button", { name: "开始确定性验证" }).click();
      await expect(page.getByText("确定性证据：validated", { exact: true })).toBeVisible({
        timeout: 45_000,
      });

      await page.getByRole("combobox", { name: "审批职责", exact: true }).selectOption({ index: 1 });
      await page.getByRole("button", { name: "提交审批" }).click();
      await expect(page.getByText("待审批", { exact: true }).first()).toBeVisible();
      await page.getByRole("link", { name: "交给另一位 Human 审批" }).click();
      await approveAsPlatformAdmin(page);

      await page.goto(proposalPath);
      const publishRules = page.getByRole("button", { name: "发布权威约束" });
      await waitForEnabled(page, publishRules);
      await publishRules.click();
      await expect(page.getByRole("dialog", { name: "确认发布权威约束" })).toBeVisible();
      await page.getByRole("button", { name: "确认发布" }).click();
      await expect(page.getByRole("heading", { name: "已发布为权威约束" })).toBeVisible();

      await page.goto(projectPath);
      const generationLink = page.getByRole("link", { name: /继续生成内容/u });
      const playtestLink = page.getByRole("link", { name: /进入自动试玩/u });
      await expect(generationLink).toHaveAttribute("href", /\/generation\?.*constraint=/u);
      await expect(playtestLink).toHaveAttribute("href", /\/playtest\?.*projectConstraint=/u);

      await generationLink.click();
      await expect(page.getByRole("heading", { name: "内容生成", level: 1 })).toBeVisible();
      await expect(page.getByText("已绑定天空港计划项目的当前版本与 1 份材料")).toBeVisible();
      await page
        .getByLabel("你想让 AI 做什么？")
        .fill("新增一名风暴观测员，并把他安排在天空港；保留材料来源并遵守已发布规则。");
      const generationAdvanced = page.locator("details.gf-generation__advanced-settings");
      if (!(await generationAdvanced.getAttribute("open"))) {
        await generationAdvanced.getByText("高级设置", { exact: true }).click();
      }
      await page.getByLabel("AI 运行方式").selectOption("record");
      await page.getByLabel("AI 生成方案").selectOption("builtin.generation@1");
      await page.getByLabel("试玩环境").selectOption("builtin.environment@1");
      const exportProfile = page.getByRole("group", { name: "候选配置格式" }).getByRole("checkbox");
      if (!(await exportProfile.isChecked())) await exportProfile.check();
      const generationDomain = page
        .getByRole("group", { name: "内容领域" })
        .getByRole("checkbox", { name: "内置规则域" });
      if (!(await generationDomain.isChecked())) await generationDomain.check();
      const startGeneration = page.getByRole("button", { name: "开始生成" });
      await expect(startGeneration).toBeEnabled();
      await startGeneration.click();
      await expect(page.getByRole("heading", { name: "候选内容已通过初步检查" })).toBeVisible({
        timeout: 45_000,
      });
      await expect(page.getByRole("rowheader", { name: "角色 风暴观测员" })).toBeVisible();

      await page.getByRole("link", { name: /打开修改详情/u }).click();
      const generatedPatchPath = new URL(page.url()).pathname;
      const validateGenerated = page.getByRole("button", { name: "开始验证", exact: true });
      await waitForEnabled(page, validateGenerated);
      await validateGenerated.click();
      await expect(page.getByRole("link", { name: "查看已受理的运行" })).toBeVisible();
      const submitGenerated = page.getByRole("button", { name: "提交独立审批" });
      await waitForEnabled(page, submitGenerated);
      await submitGenerated.click();
      const generatedApprovalLink = page.getByRole("link", { name: "打开审批详情" });
      await expect(generatedApprovalLink).toBeVisible();
      await generatedApprovalLink.click();
      await approveAsPlatformAdmin(page);

      await page.goto(generatedPatchPath);
      const applyGenerated = page.getByRole("button", { name: "应用已批准的修改" });
      await waitForEnabled(page, applyGenerated);
      await applyGenerated.click();
      await expect(page.getByRole("dialog", { name: "确认应用已批准的修改？" })).toBeVisible();
      await page.getByRole("button", { name: "确认应用" }).click();
      await expect(page.getByRole("heading", { name: "修改已应用" })).toBeVisible();

      await page.goto(projectPath);
      await expect(page.getByText(/项目当前内容已经是第 2 版/u)).toBeVisible();
      await page.getByRole("link", { name: /进入自动试玩/u }).click();
      await expect(page.getByRole("heading", { name: "自动试玩", level: 1 })).toBeVisible();
      const preparePlaytest = page.getByRole("button", { name: /^准备内容候选/u });
      await expect(preparePlaytest).toBeVisible();
      await preparePlaytest.click();

      const deriveTasks = page.getByRole("button", { name: "创建试玩任务" });
      await expect(deriveTasks).toBeEnabled();
      await deriveTasks.click();
      await expect(page.getByRole("heading", { name: /试玩任务集 1 · \d+ 个任务/u })).toBeVisible({
        timeout: 45_000,
      });
      const playtestLaunch = page.getByRole("region", { name: "开始自动试玩" });
      await expect(playtestLaunch).toBeVisible();
      await playtestLaunch.getByLabel("AI 运行方式").selectOption("record");
      const startPlaytest = playtestLaunch.getByRole("button", { name: "开始自动试玩" });
      await expect(startPlaytest).toBeEnabled();
      await startPlaytest.click();
      await expect(page.getByRole("heading", { name: "仍有试玩任务未完成" })).toBeVisible({
        timeout: 45_000,
      });
    } finally {
      await context.close();
    }

    expect([...unexpectedRequests]).toEqual([]);
  });
});
