import type { components } from "../api/generated/openapi";

type ExecutionProfile = components["schemas"]["ExecutionProfileViewV1"];
type ProfileRef = components["schemas"]["ProfileRefV1"];

/** The one identity string every page uses to select and submit an execution profile. */
export function profileRefKey(ref: ProfileRef): string {
  return `${ref.profile_id}@${ref.version}`;
}

export function profileKey(profile: ExecutionProfile): string {
  return profileRefKey(profile.profile);
}

/** What this profile does, in the words a planner uses. */
export function profileBusinessLabel(profile: ExecutionProfile): string {
  const labels: Partial<Record<ExecutionProfile["profile_kind"], string>> = {
    checker: "规则与关系检查",
    config_export: "配置可导出性检查",
    llm_triage: "AI 问题归纳",
    patch_repair: "AI 自动修复草案",
    review: "内容检查方案",
    simulation: "经济与数值仿真",
    validation: "完整验证流程",
  };
  const purpose =
    labels[profile.profile_kind] ??
    (/\p{Script=Han}/u.test(profile.display_name) ? profile.display_name : "扩展执行方案");
  const variant = profile.profile.profile_id.startsWith("builtin.")
    ? "内置标准方案"
    : profile.display_name.trim() || "自定义方案";
  return `${purpose} · ${variant} v${profile.profile.version}`;
}

export function profileBusinessContext(profile: ExecutionProfile): string {
  const descriptions: Partial<Record<ExecutionProfile["profile_kind"], string>> = {
    checker: "检查任务结构、引用关系和规则约束",
    config_export: "确认修改后的内容能够生成可运行配置",
    llm_triage: "把检查结果归纳成策划可读的问题",
    patch_repair: "仅起草修复，结果仍会重新接受确定性验证",
    review: "把检查、仿真和 AI 归纳合成一份内容检查报告",
    simulation: "模拟资源产出、消耗和长期数值变化",
    validation: "汇总本次选择的检查、仿真和已有证据",
  };
  return descriptions[profile.profile_kind] ?? "按当前工作流运行这项方案";
}
