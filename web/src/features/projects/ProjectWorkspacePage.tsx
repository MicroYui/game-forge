import { useQuery } from "@tanstack/react-query";
import {
  ArchiveX,
  ArrowRight,
  CalendarClock,
  Check,
  FileText,
  Gamepad2,
  GitPullRequestArrow,
  RefreshCw,
  Sparkles,
  Upload,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState, type ChangeEvent, type FormEvent } from "react";
import { Link } from "react-router-dom";

import { createMutationIntent } from "../../api/csrf";
import { ApiProblemError } from "../../api/problem";
import { compactDateTime, TechnicalDetails } from "../../components/identity";
import { ProblemPanel, StatePanel } from "../../components/ui";
import {
  projectsApi,
  sourceFormatForFile,
  type Project,
  type ProjectDraftResult,
  type ProjectExtraction,
  type ProjectMaterial,
  type ProjectsApi,
  type VersionedResource,
} from "./api";
import { ProjectGraphEditor } from "./ProjectGraphEditor";
import {
  applyIdentityConflictValue,
  snapshotArtifactToGraphDraft,
  type ProjectGraphDraft,
  type ProjectIdentityConflict,
} from "./model";
import "./projects.css";

const extractionGoal =
  "从所选策划材料中完整提取游戏实体、属性和关系草案。保留材料中的明确事实，不臆造缺失设定；同一概念使用一致标识。";

const planningScopeLabels: Readonly<Record<ProjectExtraction["planning_scope"], string>> = {
  auto: "让系统判断",
  game_foundation: "整个游戏 / 核心系统",
  limited_event: "限时活动 / 赛季",
  live_update: "已有内容调整",
  permanent_feature: "永久玩法 / 永久内容",
};

const planningScopeDescriptions: Readonly<Record<ProjectExtraction["planning_scope"], string>> = {
  auto: "系统会从材料判断是游戏本体、永久玩法还是限时内容，并把不确定项留给你确认。",
  game_foundation: "用于世界观、核心循环、全局经济和长期通用规则，不会整体包进活动生命周期。",
  limited_event: "会单独保留开放期、玩法结束期、奖励兑换期，以及活动专属内容的归属。",
  live_update: "优先修改当前项目已有实体，不重复创建同一角色、物品或系统。",
  permanent_feature: "上线后持续存在，不继承限时活动的到期隐藏规则。",
};

const extractionStatusLabels: Readonly<Record<ProjectExtraction["status"], string>> = {
  failed: "提取未完成",
  needs_resolution: "有内容需要确认",
  queued: "等待 AI 开始",
  ready: "草案可以编辑",
  running: "AI 正在阅读材料",
};

const validationSourceLabels = {
  economy: "经济模拟",
  structure: "结构检查",
} as const;

const validationSeverityLabels = {
  critical: "发布前必须处理",
  info: "供参考",
  major: "建议优先处理",
  minor: "建议检查",
} as const;

const sourceFormatLabels: Readonly<Record<ProjectMaterial["source_format"], string>> = {
  csv: "CSV 表格",
  docx: "DOCX 文档",
  feishu_blocks_json: "飞书文档 JSON",
  html: "网页 / 飞书复制内容",
  markdown: "Markdown",
  plain_text: "纯文本",
  xlsx: "XLSX 表格",
};

const conflictLabels: Readonly<Record<ProjectIdentityConflict["code"], string>> = {
  ambiguous_unqualified_alias: "名称可能指向多个内容",
  attribute_value_conflict: "同一属性出现了不同值",
  dangling_relation_endpoint: "关系引用的内容不存在",
  entity_type_conflict: "同一内容出现了不同类型",
  malformed_operation: "AI 建议的结构不完整",
  relation_shape_conflict: "同一关系出现了不同连接方式",
};

function normalizedError(error: unknown): Error {
  return error instanceof Error ? error : new Error("项目操作未完成。");
}

class ProjectInputError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "ProjectInputError";
  }
}

function currentContentArtifact(project: Project): string {
  return project.current_content_ref?.artifact_id ?? project.bootstrap_snapshot_artifact_id;
}

function jsonValue(value: unknown): string {
  if (typeof value === "string") return value;
  return JSON.stringify(value, null, 2);
}

function manualConflictValue(value: string): unknown {
  const trimmed = value.trim();
  if (!trimmed) throw new ProjectInputError("请填写最终值；如需空字符串，请输入两个英文双引号。");
  try {
    return JSON.parse(trimmed) as unknown;
  } catch {
    return trimmed;
  }
}

function conflictChoices(conflict: ProjectIdentityConflict): Array<{ key: string; value: unknown }> {
  return conflict.candidates.flatMap((candidate, candidateIndex) => {
    if (
      conflict.code === "ambiguous_unqualified_alias" &&
      Array.isArray(candidate.value) &&
      candidate.value.every((value) => typeof value === "string")
    ) {
      return candidate.value.map((value, valueIndex) => ({
        key: `${candidate.op_id}:${candidateIndex}:${valueIndex}`,
        value,
      }));
    }
    return [{ key: `${candidate.op_id}:${candidateIndex}`, value: candidate.value }];
  });
}

function materialLabel(material: ProjectMaterial): string {
  return `${material.display_name} · ${sourceFormatLabels[material.source_format]}`;
}

type ProjectValidationIssue = ProjectExtraction["validation_issues"][number];

function isEventScheduleIssue(issue: ProjectValidationIssue): boolean {
  return issue.code === "unbound_event_schedule" || issue.code === "invalid_event_lifecycle";
}

function plannerEntityName(entity: ProjectGraphDraft["entities"][number]): string | null {
  const candidate = entity.attrs?.display_name ?? entity.attrs?.name ?? entity.attrs?.title;
  return typeof candidate === "string" && candidate.trim() ? candidate.trim() : null;
}

function eventOwnerForIssue(draft: ProjectGraphDraft | null, issue: ProjectValidationIssue): string | null {
  if (!draft || !isEventScheduleIssue(issue)) return null;
  const owners = draft.entities.filter(
    (entity) =>
      entity.type === "EVENT" &&
      entity.attrs?.scope_kind === "event" &&
      (entity.attrs?.scope_role === "owner" || entity.attrs?.availability != null),
  );
  const affected = new Set(issue.affected_content);
  const exact = owners.find((entity) => {
    const name = plannerEntityName(entity);
    return name !== null && affected.has(name);
  });
  if (exact) return exact.id;
  return owners.length === 1 ? owners[0]!.id : null;
}

function projectLink(path: string, project: Project, materials: readonly ProjectMaterial[]): string {
  const query = new URLSearchParams({ project: project.project_id, projectName: project.display_name });
  if (path === "/specs") {
    query.set("section", "proposals");
    query.set("constraintRef", project.constraint_ref_name);
  }
  const content = project.current_content_ref;
  if (content) {
    query.set(path === "/playtest" ? "projectContent" : "content", content.artifact_id);
    query.set("contentRef", project.content_ref_name);
    query.set("contentRevision", String(content.revision));
  }
  const constraint = project.current_constraint_ref;
  if (constraint) {
    query.set(path === "/playtest" ? "projectConstraint" : "constraint", constraint.artifact_id);
    if (path !== "/playtest") query.set("constraintRef", project.constraint_ref_name);
    query.set("constraintRevision", String(constraint.revision));
  }
  for (const material of materials) query.append("source", material.rendered_source_artifact_id);
  return `${path}?${query.toString()}`;
}

function WorkspaceError({ error, retry }: { error: Error; retry(): void }) {
  if (error instanceof ApiProblemError) return <ProblemPanel problem={error.problem} />;
  return (
    <StatePanel
      action={
        <button className="gf-secondary-button" onClick={retry} type="button">
          重新读取
        </button>
      }
      description="暂时无法读取这个项目，请稍后重试。"
      state="error"
      title="项目读取失败"
    />
  );
}

function MutationError({ error }: { error: Error }) {
  if (error instanceof ApiProblemError) return <ProblemPanel problem={error.problem} />;
  if (error instanceof ProjectInputError) {
    return (
      <p className="gf-projects__inline-error" role="alert">
        {error.message}
      </p>
    );
  }
  return (
    <p className="gf-projects__inline-error" role="alert">
      这一步暂时没有完成。你的页面内容仍然保留，可以检查后重试。
    </p>
  );
}

export function ProjectWorkspacePage({
  api = projectsApi,
  pollIntervalMs = 1500,
  projectId,
}: {
  api?: ProjectsApi;
  pollIntervalMs?: number;
  projectId: string;
}) {
  const workspace = useQuery({
    queryFn: async () => {
      const [project, materialPage, extractionPage] = await Promise.all([
        api.getProject(projectId),
        api.listMaterials(projectId),
        api.listExtractions(projectId),
      ]);
      let extraction = project.value.latest_extraction_id
        ? (extractionPage.items.find((item) => item.extraction_id === project.value.latest_extraction_id) ??
          null)
        : (extractionPage.items[0] ?? null);
      if (project.value.latest_extraction_id && extraction === null) {
        extraction = (await api.getExtraction(projectId, project.value.latest_extraction_id)).value;
      }
      return {
        extraction,
        extractionHistory: extractionPage.items,
        materials: materialPage.items,
        project,
      };
    },
    queryKey: ["project-workspace", projectId],
    retry: false,
  });
  const [projectResource, setProjectResource] = useState<VersionedResource<Project> | null>(null);
  const [materials, setMaterials] = useState<ProjectMaterial[]>([]);
  const [selectedMaterialIds, setSelectedMaterialIds] = useState<Set<string>>(new Set());
  const [extraction, setExtraction] = useState<ProjectExtraction | null>(null);
  const [extractionHistory, setExtractionHistory] = useState<ProjectExtraction[]>([]);
  const [graphDraft, setGraphDraft] = useState<ProjectGraphDraft | null>(null);
  const [graphArtifactId, setGraphArtifactId] = useState<string | null>(null);
  const [graphError, setGraphError] = useState<Error | null>(null);
  const [resolvedConflicts, setResolvedConflicts] = useState<Set<string>>(new Set());
  const [manualConflictValues, setManualConflictValues] = useState<Record<string, string>>({});
  const [conflictErrors, setConflictErrors] = useState<Record<string, string>>({});
  const [materialName, setMaterialName] = useState("");
  const [materialText, setMaterialText] = useState("");
  const [materialFormat, setMaterialFormat] = useState<
    "plain_text" | "markdown" | "html" | "feishu_blocks_json"
  >("plain_text");
  const [objective, setObjective] = useState(extractionGoal);
  const [planningScope, setPlanningScope] = useState<ProjectExtraction["planning_scope"]>("auto");
  const [materialBusy, setMaterialBusy] = useState(false);
  const [extractionBusy, setExtractionBusy] = useState(false);
  const [discardBusy, setDiscardBusy] = useState(false);
  const [discardOpen, setDiscardOpen] = useState(false);
  const [discardReason, setDiscardReason] = useState("");
  const [publishBusy, setPublishBusy] = useState(false);
  const [actionError, setActionError] = useState<Error | null>(null);
  const [uploadMessage, setUploadMessage] = useState<string | null>(null);
  const [proposalMessage, setProposalMessage] = useState<string | null>(null);
  const [draftResult, setDraftResult] = useState<ProjectDraftResult | null>(null);
  const [editorFocusRequest, setEditorFocusRequest] = useState<{
    entityId: string;
    requestId: number;
  } | null>(null);
  const graphLoadGeneration = useRef(0);
  const editorFocusSequence = useRef(0);

  useEffect(() => {
    if (!workspace.data) return;
    setProjectResource(workspace.data.project);
    setMaterials([...workspace.data.materials]);
    setSelectedMaterialIds(
      new Set(
        workspace.data.materials
          .filter((material) => material.status === "active" && material.parse_status === "ready")
          .map((material) => material.material_id),
      ),
    );
    setExtractionHistory([...workspace.data.extractionHistory]);
    selectExtraction(workspace.data.extraction);
  }, [workspace.data]);

  useEffect(() => {
    if (
      !extraction ||
      extraction.disposition === "discarded" ||
      (extraction.status !== "queued" && extraction.status !== "running")
    )
      return;
    let cancelled = false;
    const timer = window.setTimeout(() => {
      void api
        .getExtraction(projectId, extraction.extraction_id)
        .then((result) => {
          if (!cancelled) {
            setExtraction(result.value);
            setExtractionHistory((current) =>
              current.map((item) =>
                item.extraction_id === result.value.extraction_id ? result.value : item,
              ),
            );
          }
        })
        .catch((error: unknown) => {
          if (!cancelled) setActionError(normalizedError(error));
        });
    }, pollIntervalMs);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [api, extraction, pollIntervalMs, projectId]);

  const candidateArtifactId =
    extraction?.disposition === "discarded"
      ? null
      : extraction?.status === "ready" || extraction?.status === "needs_resolution"
        ? extraction.preview_snapshot_artifact_id
        : extraction === null
          ? (projectResource?.value.current_content_ref?.artifact_id ?? null)
          : null;

  useEffect(() => {
    if (candidateArtifactId !== null) return;
    graphLoadGeneration.current += 1;
    setGraphDraft(null);
    setGraphArtifactId(null);
    setGraphError(null);
    setResolvedConflicts(new Set());
    setManualConflictValues({});
    setConflictErrors({});
    setEditorFocusRequest(null);
  }, [candidateArtifactId]);

  useEffect(() => {
    if (!candidateArtifactId || graphArtifactId === candidateArtifactId) return;
    const generation = graphLoadGeneration.current + 1;
    graphLoadGeneration.current = generation;
    setGraphError(null);
    void api
      .getArtifact(candidateArtifactId)
      .then((view) => {
        if (graphLoadGeneration.current !== generation) return;
        setGraphDraft(snapshotArtifactToGraphDraft(view));
        setGraphArtifactId(candidateArtifactId);
        setResolvedConflicts(new Set());
        setManualConflictValues({});
        setConflictErrors({});
      })
      .catch((error: unknown) => {
        if (graphLoadGeneration.current === generation) setGraphError(normalizedError(error));
      });
  }, [api, candidateArtifactId, graphArtifactId]);

  const project = projectResource?.value ?? null;
  const hasPublishedRules = project?.current_constraint_ref != null;
  const readyMaterials = useMemo(
    () => materials.filter((material) => material.parse_status === "ready" && material.status === "active"),
    [materials],
  );
  const unresolvedConflictCount =
    extraction?.identity_conflicts.filter((conflict) => !resolvedConflicts.has(conflict.conflict_id))
      .length ?? 0;

  function selectExtraction(next: ProjectExtraction | null) {
    graphLoadGeneration.current += 1;
    setExtraction(next);
    setGraphDraft(null);
    setGraphArtifactId(null);
    setGraphError(null);
    setResolvedConflicts(new Set());
    setManualConflictValues({});
    setConflictErrors({});
    setEditorFocusRequest(null);
    setDiscardOpen(false);
    setDiscardReason("");
    setDraftResult(null);
  }

  function openEventSchedule(issue: ProjectValidationIssue) {
    const entityId = eventOwnerForIssue(graphDraft, issue);
    if (!entityId) return;
    editorFocusSequence.current += 1;
    setEditorFocusRequest({ entityId, requestId: editorFocusSequence.current });
  }

  async function refreshProject(): Promise<VersionedResource<Project>> {
    const fresh = await api.getProject(projectId);
    setProjectResource(fresh);
    return fresh;
  }

  async function saveTextMaterial(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setMaterialBusy(true);
    setActionError(null);
    setUploadMessage(null);
    try {
      const saved = await api.addTextMaterial(
        projectId,
        {
          display_name: materialName,
          request_schema_version: "project-material-text-request@1",
          source_format: materialFormat,
          text: materialText,
        },
        createMutationIntent(),
      );
      setMaterials((current) => [...current.filter((item) => item.material_id !== saved.material_id), saved]);
      setSelectedMaterialIds((current) => new Set([...current, saved.material_id]));
      setMaterialName("");
      setMaterialText("");
      setUploadMessage("材料已保存，可以交给 AI 提取。 ");
    } catch (error) {
      setActionError(normalizedError(error));
    } finally {
      setMaterialBusy(false);
    }
  }

  async function uploadFile(event: ChangeEvent<HTMLInputElement>) {
    const files = [...(event.target.files ?? [])];
    event.target.value = "";
    if (files.length === 0) return;
    const uploads = files.map((file) => ({ file, sourceFormat: sourceFormatForFile(file) }));
    const unsupported = uploads.filter((item) => item.sourceFormat === null);
    if (unsupported.length > 0) {
      const extensions = [
        ...new Set(unsupported.map((item) => item.file.name.split(".").pop()?.toUpperCase() || "这种文件")),
      ];
      setUploadMessage(null);
      setActionError(
        new ProjectInputError(
          `暂不支持 ${extensions.join("、")}。请使用 DOCX、XLSX、CSV、Markdown、HTML、JSON 或纯文本。`,
        ),
      );
      return;
    }
    setMaterialBusy(true);
    setActionError(null);
    setUploadMessage(null);
    let savedCount = 0;
    try {
      for (const upload of uploads) {
        const saved = await api.uploadMaterial(
          projectId,
          upload.file,
          upload.sourceFormat!,
          createMutationIntent(),
        );
        savedCount += 1;
        setMaterials((current) => [
          ...current.filter((item) => item.material_id !== saved.material_id),
          saved,
        ]);
        setSelectedMaterialIds((current) => new Set([...current, saved.material_id]));
      }
      setUploadMessage(
        savedCount === 1
          ? `已读取“${uploads[0]!.file.name}”，可以交给 AI 提取。`
          : `已读取 ${savedCount} 份策划文件，并全部选入本次提案。`,
      );
    } catch (error) {
      if (savedCount > 0) {
        setUploadMessage(`已读取前 ${savedCount} 份文件；其余文件尚未完成，请检查后重试。`);
      }
      setActionError(normalizedError(error));
    } finally {
      setMaterialBusy(false);
    }
  }

  async function startExtraction() {
    if (selectedMaterialIds.size === 0) return;
    setExtractionBusy(true);
    setActionError(null);
    setProposalMessage(null);
    setDraftResult(null);
    try {
      const created = await api.startExtraction(
        projectId,
        {
          candidate_export_profiles: [],
          cassette_artifact_id: null,
          execution_version_plan: null,
          generation_policy: null,
          llm_execution_mode: "record",
          material_ids: [...selectedMaterialIds].sort(),
          planning_scope: planningScope,
          objective_goal_text: objective,
          request_schema_version: "project-extraction-create-request@1",
        },
        createMutationIntent(),
      );
      setExtractionHistory((current) => [
        created,
        ...current.filter((item) => item.extraction_id !== created.extraction_id),
      ]);
      selectExtraction(created);
      await refreshProject();
    } catch (error) {
      setActionError(normalizedError(error));
    } finally {
      setExtractionBusy(false);
    }
  }

  async function discardCurrentExtraction() {
    if (!extraction || extraction.disposition === "discarded" || !discardReason.trim()) return;
    setDiscardBusy(true);
    setActionError(null);
    setProposalMessage(null);
    try {
      const current = await api.getExtraction(projectId, extraction.extraction_id);
      const discarded = await api.discardExtraction(
        projectId,
        extraction.extraction_id,
        {
          expected_revision: current.value.revision,
          reason: discardReason.trim(),
          request_schema_version: "project-extraction-discard-request@1",
        },
        createMutationIntent(),
        current.etag,
      );
      setExtractionHistory((items) =>
        items.map((item) => (item.extraction_id === discarded.extraction_id ? discarded : item)),
      );
      selectExtraction(discarded);
      setProposalMessage("这次提案已放弃。材料、AI 运行过程和检查证据仍然保留。");
      await refreshProject();
    } catch (error) {
      setActionError(normalizedError(error));
    } finally {
      setDiscardBusy(false);
    }
  }

  function resolveConflict(conflict: ProjectIdentityConflict, value: unknown) {
    if (!graphDraft) return;
    try {
      setGraphDraft(applyIdentityConflictValue(graphDraft, conflict, value));
      setResolvedConflicts((current) => new Set([...current, conflict.conflict_id]));
      setConflictErrors((current) => {
        const next = { ...current };
        delete next[conflict.conflict_id];
        return next;
      });
    } catch (error) {
      setResolvedConflicts((current) => {
        const next = new Set(current);
        next.delete(conflict.conflict_id);
        return next;
      });
      setConflictErrors((current) => ({
        ...current,
        [conflict.conflict_id]: normalizedError(error).message,
      }));
    }
  }

  function resolveManualConflict(conflict: ProjectIdentityConflict) {
    try {
      resolveConflict(conflict, manualConflictValue(manualConflictValues[conflict.conflict_id] ?? ""));
    } catch (error) {
      setConflictErrors((current) => ({
        ...current,
        [conflict.conflict_id]: normalizedError(error).message,
      }));
    }
  }

  async function createDraft() {
    if (!graphDraft || !project || !extraction || extraction.disposition === "discarded") return;
    setPublishBusy(true);
    setActionError(null);
    try {
      const fresh = await refreshProject();
      if (extraction && currentContentArtifact(fresh.value) !== extraction.base_snapshot_artifact_id) {
        throw new Error("项目内容已产生新版本，请基于最新版本重新提取后再发布。");
      }
      const created = await api.createContentDraft(
        projectId,
        {
          candidate_export_profiles: [],
          entities: graphDraft.entities,
          expected_source_extraction_revision: extraction.revision,
          expected_project_revision: fresh.value.revision,
          rationale: "策划已在项目图形编辑器中确认实体与关系，创建首个受治理内容版本。",
          relations: graphDraft.relations,
          request_schema_version: "project-graph-draft-request@1",
          side_effect_risk: "low",
          source_extraction_id: extraction.extraction_id,
        },
        createMutationIntent(),
        fresh.etag,
      );
      setDraftResult(created);
      await refreshProject();
    } catch (error) {
      setActionError(normalizedError(error));
    } finally {
      setPublishBusy(false);
    }
  }

  if (workspace.isPending) {
    return (
      <div className="gf-page">
        <StatePanel
          description="正在准备项目、材料和最新创作状态。"
          headingLevel={1}
          state="loading"
          title="正在打开项目"
        />
      </div>
    );
  }
  if (workspace.isError) {
    return (
      <div className="gf-page">
        <WorkspaceError error={normalizedError(workspace.error)} retry={() => void workspace.refetch()} />
      </div>
    );
  }
  if (!project) return null;

  const patchArtifactId =
    draftResult?.patch.artifact.artifact_id ?? extraction?.publication_patch_artifact_id ?? null;
  const active = project.current_content_ref !== null;

  return (
    <div className="gf-page gf-project-workspace">
      <header className="gf-project-workspace__hero">
        <div>
          <Link className="u-small" to="/projects">
            ← 所有游戏项目
          </Link>
          <p className="u-kicker">{project.genre || "新游戏项目"}</p>
          <h1>{project.display_name}</h1>
          <p>{project.description || "从一份创意或策划材料开始建立游戏世界。"}</p>
        </div>
        <div className="gf-project-workspace__authority">
          <span className={active ? "u-status u-status--ok" : "u-status"}>
            {active ? `内容已发布 · 第 ${project.current_content_ref?.revision} 版` : "尚未发布首个内容版本"}
          </span>
          <button className="gf-secondary-button" onClick={() => void refreshProject()} type="button">
            <RefreshCw aria-hidden="true" size={15} />
            刷新状态
          </button>
          <TechnicalDetails
            items={[
              { copyLabel: "复制项目标识", label: "项目标识", value: project.project_id },
              { label: "内容发布位置", value: project.content_ref_name },
              { label: "项目修订", value: String(project.revision) },
            ]}
            summary="查看项目技术信息"
          />
        </div>
      </header>

      <nav aria-label="项目创作步骤" className="gf-project-workspace__steps">
        {[
          ["1", "项目", true],
          ["2", "材料", readyMaterials.length > 0],
          ["3", "AI 提取", extractionHistory.length > 0],
          ["4", "编辑", graphDraft !== null],
          ["5", "发布", active],
        ].map(([number, label, complete]) => (
          <div data-complete={complete ? "true" : "false"} key={String(number)}>
            <span>{complete ? <Check aria-hidden="true" size={15} /> : number}</span>
            <strong>{label}</strong>
          </div>
        ))}
      </nav>

      {actionError && <MutationError error={actionError} />}
      {uploadMessage && (
        <p className="gf-projects__success" role="status">
          {uploadMessage}
        </p>
      )}
      {proposalMessage && (
        <p className="gf-projects__success" role="status">
          {proposalMessage}
        </p>
      )}

      <section className="gf-project-workspace__section" aria-labelledby="materials-title">
        <header className="gf-project-workspace__section-header">
          <span className="gf-projects__step-number">2</span>
          <div>
            <p className="u-kicker">输入你的想法</p>
            <h2 id="materials-title">添加策划材料</h2>
            <p>可以直接粘贴创意，也可以上传飞书导出的文档或表格。</p>
          </div>
        </header>
        <div className="gf-project-workspace__material-layout">
          <form
            className="gf-form gf-project-workspace__paste"
            onSubmit={(event) => void saveTextMaterial(event)}
          >
            <label>
              材料名称
              <input
                maxLength={256}
                onChange={(event) => setMaterialName(event.target.value)}
                placeholder="例如：核心创意、角色设定"
                required
                value={materialName}
              />
            </label>
            <label>
              粘贴格式
              <select
                onChange={(event) => setMaterialFormat(event.target.value as typeof materialFormat)}
                value={materialFormat}
              >
                <option value="plain_text">普通文字</option>
                <option value="markdown">Markdown</option>
                <option value="html">飞书 / 网页复制的 HTML</option>
                <option value="feishu_blocks_json">飞书文档 Block JSON</option>
              </select>
            </label>
            <label>
              策划内容
              <textarea
                maxLength={1_048_576}
                onChange={(event) => setMaterialText(event.target.value)}
                placeholder="写下世界观、角色、玩法、任务、数值或规则想法…"
                required
                rows={8}
                value={materialText}
              />
            </label>
            <button disabled={materialBusy} type="submit">
              <FileText aria-hidden="true" size={17} />
              {materialBusy ? "正在读取…" : "保存这份材料"}
            </button>
          </form>
          <div className="gf-project-workspace__upload">
            <Upload aria-hidden="true" size={28} />
            <h3>上传策划文件</h3>
            <p>支持飞书导出的 DOCX、XLSX、CSV，以及 Markdown、HTML、JSON、TXT。</p>
            <label className="gf-secondary-button">
              选择文件
              <input
                aria-label="上传策划文件"
                className="u-sr-only"
                disabled={materialBusy}
                onChange={(event) => void uploadFile(event)}
                multiple
                type="file"
              />
              <span className="u-sr-only">上传策划文件</span>
            </label>
            <small>单个文件最多 8 MB；原件和解析结果都会保留来源记录。</small>
          </div>
        </div>

        <div className="gf-project-workspace__materials">
          <h3>用于本次提取的材料</h3>
          {readyMaterials.length === 0 ? (
            <p className="u-small">还没有可用材料。保存或上传后，它会出现在这里。</p>
          ) : (
            <ul>
              {readyMaterials.map((material) => (
                <li key={material.material_id}>
                  <label>
                    <input
                      checked={selectedMaterialIds.has(material.material_id)}
                      onChange={(event) => {
                        setSelectedMaterialIds((current) => {
                          const next = new Set(current);
                          if (event.target.checked) next.add(material.material_id);
                          else next.delete(material.material_id);
                          return next;
                        });
                      }}
                      type="checkbox"
                    />
                    <span>
                      <strong>{material.display_name}</strong>
                      <small>
                        {materialLabel(material)} · {material.text_char_count.toLocaleString("zh-CN")} 字
                      </small>
                    </span>
                  </label>
                  {material.parse_warnings.length > 0 && (
                    <span className="u-status u-status--danger">
                      有 {material.parse_warnings.length} 条解析提示
                    </span>
                  )}
                  <TechnicalDetails
                    items={[
                      { label: "材料标识", value: material.material_id },
                      { label: "规范化来源", value: material.rendered_source_artifact_id },
                    ]}
                    summary="查看来源技术信息"
                  />
                </li>
              ))}
            </ul>
          )}
        </div>
      </section>

      <section className="gf-project-workspace__section" aria-labelledby="extraction-title">
        <header className="gf-project-workspace__section-header">
          <span className="gf-projects__step-number">3</span>
          <div>
            <p className="u-kicker">AI 只生成草案</p>
            <h2 id="extraction-title">从材料提取实体与关系</h2>
            <p>系统会先统一名称和属性写法；无法安全合并的内容会留给你确认。</p>
          </div>
        </header>
        <div className="gf-project-workspace__extraction-controls">
          <label>
            这份材料属于什么
            <select
              onChange={(event) =>
                setPlanningScope(event.target.value as ProjectExtraction["planning_scope"])
              }
              value={planningScope}
            >
              {Object.entries(planningScopeLabels).map(([value, label]) => (
                <option key={value} value={value}>
                  {label}
                </option>
              ))}
            </select>
            <small>{planningScopeDescriptions[planningScope]}</small>
          </label>
          <label>
            希望 AI 做什么
            <textarea
              maxLength={16_384}
              onChange={(event) => setObjective(event.target.value)}
              rows={4}
              value={objective}
            />
          </label>
          <button
            disabled={
              selectedMaterialIds.size === 0 ||
              selectedMaterialIds.size > 64 ||
              extractionBusy ||
              !objective.trim()
            }
            onClick={() => void startExtraction()}
            type="button"
          >
            <Sparkles aria-hidden="true" size={17} />
            {extractionBusy ? "正在启动…" : "AI 提取实体与关系"}
          </button>
          <p className="gf-project-workspace__selection-summary" role="status">
            {selectedMaterialIds.size > 64
              ? `已选择 ${selectedMaterialIds.size} 份材料，单次提案最多组合 64 份。请取消一部分后再提取。`
              : selectedMaterialIds.size > 0
                ? `已选择 ${selectedMaterialIds.size} 份材料。AI 会把它们一起作为这次提案的依据。`
                : "请至少选择 1 份材料；一次提案最多可组合 64 份材料。"}
          </p>
        </div>

        <section aria-labelledby="proposal-history-title" className="gf-project-workspace__proposal-history">
          <header>
            <div>
              <p className="u-kicker">保留每次策划思路</p>
              <h3 id="proposal-history-title">提案记录（{extractionHistory.length}）</h3>
              <p>
                一个游戏可以保存多次提案，每次可组合 1–64 份材料。放弃提案不会删除材料、运行过程或检查证据。
              </p>
            </div>
          </header>
          {extractionHistory.length === 0 ? (
            <p className="u-small">还没有提案。选择材料并启动 AI 提取后，会在这里留下记录。</p>
          ) : (
            <ol>
              {extractionHistory.map((item) => {
                const selected = extraction?.extraction_id === item.extraction_id;
                const discarded = item.disposition === "discarded";
                return (
                  <li data-current={selected ? "true" : "false"} key={item.extraction_id}>
                    <div>
                      <strong>{planningScopeLabels[item.planning_scope]}</strong>
                      <span>
                        {item.material_ids.length} 份材料 · 创建于 {compactDateTime(item.created_at)}
                      </span>
                    </div>
                    <span
                      className={
                        discarded
                          ? "u-status"
                          : item.status === "ready"
                            ? "u-status u-status--ok"
                            : item.status === "failed"
                              ? "u-status u-status--danger"
                              : item.status === "needs_resolution"
                                ? "u-status u-status--suggestion"
                                : "u-status"
                      }
                    >
                      {discarded ? "已放弃" : extractionStatusLabels[item.status]}
                    </span>
                    <button
                      className="gf-secondary-button"
                      disabled={selected}
                      onClick={() => {
                        setActionError(null);
                        setProposalMessage(null);
                        selectExtraction(item);
                      }}
                      type="button"
                    >
                      {selected ? "正在查看" : "查看这次提案"}
                    </button>
                  </li>
                );
              })}
            </ol>
          )}
        </section>

        {extraction && (
          <article className="gf-project-workspace__extraction-status">
            <header>
              <div>
                <span
                  className={
                    extraction.disposition === "discarded"
                      ? "u-status"
                      : extraction.status === "ready"
                        ? "u-status u-status--ok"
                        : extraction.status === "failed"
                          ? "u-status u-status--danger"
                          : extraction.status === "needs_resolution"
                            ? "u-status u-status--suggestion"
                            : "u-status"
                  }
                >
                  {extraction.disposition === "discarded"
                    ? "已放弃"
                    : extractionStatusLabels[extraction.status]}
                </span>
                <h3>
                  {extraction.disposition === "discarded"
                    ? "这次提案已放弃"
                    : extraction.status === "ready"
                      ? "已得到可编辑内容草案"
                      : extraction.status === "needs_resolution" && extraction.validation_issues.length > 0
                        ? `已生成草案，需处理 ${extraction.validation_issues.length} 个检查问题`
                        : extraction.status === "needs_resolution"
                          ? "已生成草案，有内容需要确认"
                          : "内容提取进度"}
                </h3>
                <p>最近更新：{compactDateTime(extraction.updated_at)}</p>
                <p>内容范围：{planningScopeLabels[extraction.planning_scope]}</p>
              </div>
              <div className="gf-project-workspace__extraction-actions">
                <Link className="gf-secondary-button" to={`/runs/${encodeURIComponent(extraction.run_id)}`}>
                  查看 AI 运行过程
                </Link>
                {extraction.disposition !== "discarded" &&
                  (extraction.status === "ready" ||
                    extraction.status === "needs_resolution" ||
                    extraction.status === "failed") && (
                    <button
                      className="gf-danger-button"
                      onClick={() => {
                        setActionError(null);
                        setDiscardOpen(true);
                      }}
                      type="button"
                    >
                      <ArchiveX aria-hidden="true" size={16} />
                      放弃这次提案
                    </button>
                  )}
              </div>
            </header>
            {discardOpen && extraction.disposition !== "discarded" && (
              <section aria-labelledby="discard-proposal-title" className="gf-project-workspace__discard">
                <div>
                  <h4 id="discard-proposal-title">确认放弃这次提案？</h4>
                  <p>
                    这不会删除原材料、AI
                    运行过程或检查证据，也不会改变已经发布的版本。若要撤回已发布内容，请在版本治理中发起回滚。
                  </p>
                  {patchArtifactId && (
                    <p>
                      这个项目已经另行创建过发布草案；它属于独立的治理记录，不会被连带删除。若也不采用，请不要继续审批，并用后续版本替代。
                    </p>
                  )}
                </div>
                <label>
                  放弃原因
                  <textarea
                    autoFocus
                    maxLength={1024}
                    onChange={(event) => setDiscardReason(event.target.value)}
                    placeholder="例如：活动方向调整，改用另一套奖励机制"
                    required
                    rows={3}
                    value={discardReason}
                  />
                </label>
                <div>
                  <button
                    className="gf-secondary-button"
                    disabled={discardBusy}
                    onClick={() => {
                      setDiscardOpen(false);
                      setDiscardReason("");
                    }}
                    type="button"
                  >
                    继续保留
                  </button>
                  <button
                    className="gf-danger-button"
                    disabled={discardBusy || !discardReason.trim()}
                    onClick={() => void discardCurrentExtraction()}
                    type="button"
                  >
                    {discardBusy ? "正在放弃…" : "确认放弃提案"}
                  </button>
                </div>
              </section>
            )}
            {extraction.disposition === "discarded" ? (
              <div className="gf-project-workspace__discarded-note">
                <strong>这次提案不再作为项目当前编辑提案。</strong>
                <p>
                  放弃原因：{extraction.discard_reason}。材料、AI
                  运行过程和确定性检查证据仍然保留，可从上方提案记录随时回来查看。
                </p>
                {patchArtifactId && (
                  <p>
                    已经创建的发布草案是独立审计记录，仍会保留；若它也不再采用，请停止后续审批。已经发布的内容必须走回滚。
                  </p>
                )}
                {extraction.discarded_at && <p>放弃时间：{compactDateTime(extraction.discarded_at)}</p>}
              </div>
            ) : (
              <>
                {(extraction.status === "queued" || extraction.status === "running") && (
                  <p role="status">系统正在读取材料、建立关系并执行确定性检查，页面会自动刷新。</p>
                )}
                {extraction.status === "failed" && (
                  <div>
                    <StatePanel
                      description={
                        extraction.failure_message ?? "AI 草案没有通过结构与一致性检查，本次草案未被采用。"
                      }
                      state="error"
                      title="这次没有生成可编辑草案"
                    />
                    <p>材料和项目内容均未改变，可以直接重新提取。</p>
                  </div>
                )}
                {extraction.status === "needs_resolution" && extraction.validation_issues.length > 0 && (
                  <section
                    aria-labelledby="extraction-validation-title"
                    className="gf-project-workspace__validation"
                  >
                    <div className="gf-project-workspace__validation-summary">
                      <div>
                        <p className="u-kicker">确定性检查结果</p>
                        <h4 id="extraction-validation-title">草案已保留，可以直接在下方修改</h4>
                      </div>
                      <p>
                        {extraction.failure_message ??
                          "这些不是 AI 的主观评分，而是结构检查与经济模拟发现的具体问题。"}
                      </p>
                    </div>
                    <ol className="gf-project-workspace__validation-list">
                      {extraction.validation_issues.map((issue) => {
                        const eventOwnerId = eventOwnerForIssue(graphDraft, issue);
                        return (
                          <li data-severity={issue.severity} key={issue.issue_id}>
                            <header>
                              <div>
                                <span className="u-status u-status--suggestion">
                                  {validationSourceLabels[issue.source]} ·{" "}
                                  {validationSeverityLabels[issue.severity]}
                                </span>
                                <h5>{issue.title}</h5>
                              </div>
                            </header>
                            <p>{issue.description}</p>
                            {issue.affected_content.length > 0 && (
                              <div aria-label="涉及内容" className="gf-project-workspace__affected-content">
                                {issue.affected_content.map((item) => (
                                  <span key={item}>{item}</span>
                                ))}
                              </div>
                            )}
                            <div className="gf-project-workspace__resolution-hint">
                              <strong>怎么处理</strong>
                              <p>{issue.resolution_hint}</p>
                            </div>
                            {isEventScheduleIssue(issue) && (
                              <div className="gf-project-workspace__issue-action">
                                <button
                                  className="gf-secondary-button"
                                  disabled={!eventOwnerId}
                                  onClick={() => openEventSchedule(issue)}
                                  type="button"
                                >
                                  <CalendarClock aria-hidden="true" size={16} />
                                  {eventOwnerId
                                    ? "设置活动档期"
                                    : graphDraft
                                      ? "没有找到对应活动"
                                      : "正在准备档期编辑器…"}
                                </button>
                                <p>点击后会自动选中对应活动，并定位到开始、结束与领奖截止时间。</p>
                              </div>
                            )}
                          </li>
                        );
                      })}
                    </ol>
                  </section>
                )}
                {extraction.normalization_summary && (
                  <div className="gf-project-workspace__metrics" aria-label="提取校验结果">
                    <div>
                      <strong>{extraction.normalization_summary.output_operation_count}</strong>
                      <span>结构化建议</span>
                    </div>
                    <div>
                      <strong>{extraction.normalization_summary.auto_merge_count}</strong>
                      <span>自动合并</span>
                    </div>
                    <div>
                      <strong>{extraction.normalization_summary.alias_group_count}</strong>
                      <span>同一内容组</span>
                    </div>
                    <div>
                      <strong>{extraction.normalization_summary.blocking_conflict_count}</strong>
                      <span>待确认冲突</span>
                    </div>
                  </div>
                )}
                {extraction.alias_groups.length > 0 && (
                  <section className="gf-project-workspace__aliases" aria-label="同一内容识别结果">
                    <h4>自动识别并合并了 {extraction.alias_groups.length} 组同一内容</h4>
                    <p>例如点号、下划线、大小写不同，但含义和值一致时会安全归为一项。</p>
                    <ul>
                      {extraction.alias_groups.map((group) => (
                        <li key={group.canonical_identity}>
                          <span>{group.aliases.join(" / ")}</span>
                          <ArrowRight aria-hidden="true" size={14} />
                          <strong>{group.canonical_identity}</strong>
                        </li>
                      ))}
                    </ul>
                  </section>
                )}
              </>
            )}
          </article>
        )}
      </section>

      {extraction && extraction.disposition !== "discarded" && extraction.identity_conflicts.length > 0 && (
        <section
          className="gf-project-workspace__section gf-project-workspace__conflicts"
          aria-labelledby="conflicts-title"
        >
          <header className="gf-project-workspace__section-header">
            <span className="gf-projects__step-number">!</span>
            <div>
              <p className="u-kicker">需要策划判断</p>
              <h2 id="conflicts-title">确认名称或属性冲突</h2>
              <p>系统不会静默覆盖不一致的值。请选择要保留的材料事实，再继续发布。</p>
            </div>
          </header>
          <div className="gf-project-workspace__conflict-list">
            {extraction.identity_conflicts.map((conflict) => (
              <fieldset
                data-resolved={resolvedConflicts.has(conflict.conflict_id) ? "true" : "false"}
                key={conflict.conflict_id}
              >
                <legend>{conflictLabels[conflict.code]}</legend>
                <p>
                  涉及：<strong>{conflict.canonical_identity}</strong>
                </p>
                <div>
                  {conflictChoices(conflict).map((choice) => (
                    <button
                      className="gf-secondary-button"
                      key={choice.key}
                      onClick={() => resolveConflict(conflict, choice.value)}
                      type="button"
                    >
                      保留“{jsonValue(choice.value)}”
                    </button>
                  ))}
                </div>
                <div className="gf-project-workspace__manual-conflict">
                  <label>
                    手工填写 {conflict.canonical_identity} 的最终值
                    <input
                      onChange={(event) =>
                        setManualConflictValues((current) => ({
                          ...current,
                          [conflict.conflict_id]: event.target.value,
                        }))
                      }
                      placeholder="文字可直接填写；数字、布尔值、对象或数组请使用 JSON"
                      value={manualConflictValues[conflict.conflict_id] ?? ""}
                    />
                  </label>
                  <button
                    className="gf-secondary-button"
                    onClick={() => resolveManualConflict(conflict)}
                    type="button"
                  >
                    使用手工值
                  </button>
                </div>
                {conflictErrors[conflict.conflict_id] && (
                  <p className="gf-projects__inline-error" role="alert">
                    {conflictErrors[conflict.conflict_id]}
                  </p>
                )}
                {resolvedConflicts.has(conflict.conflict_id) && (
                  <span className="u-status u-status--ok">已确认</span>
                )}
                <TechnicalDetails
                  items={conflict.candidates.map((candidate, index) => ({
                    label: `候选 ${index + 1} 来源`,
                    value: candidate.source_identity,
                  }))}
                  summary="查看冲突来源技术信息"
                />
              </fieldset>
            ))}
          </div>
        </section>
      )}

      {graphError && <MutationError error={graphError} />}
      {candidateArtifactId && !graphDraft && !graphError && (
        <StatePanel
          description="正在把 AI 草案转换为可编辑的关系图。"
          state="loading"
          title="正在准备编辑器"
        />
      )}
      {graphDraft && extraction?.disposition !== "discarded" && (
        <section
          className="gf-project-workspace__section gf-project-workspace__editing"
          aria-labelledby="editing-title"
        >
          <header className="gf-project-workspace__section-header">
            <span className="gf-projects__step-number">4</span>
            <div>
              <p className="u-kicker">由策划确认</p>
              <h2 id="editing-title">检查并修改内容草案</h2>
              <p>新增、删除或修改实体和关系都只发生在草案中，点击发布前不会改变正式内容。</p>
            </div>
          </header>
          <ProjectGraphEditor focusRequest={editorFocusRequest} onChange={setGraphDraft} value={graphDraft} />
        </section>
      )}

      {(graphDraft || patchArtifactId || active) && (
        <section
          className="gf-project-workspace__section gf-project-workspace__publication"
          aria-labelledby="publication-title"
        >
          <header className="gf-project-workspace__section-header">
            <span className="gf-projects__step-number">5</span>
            <div>
              <p className="u-kicker">验证、审批、发布</p>
              <h2 id="publication-title">发布首个内容版本</h2>
              <p>创建草案后仍需通过确定性验证和审批；只有应用成功才会成为项目当前版本。</p>
            </div>
          </header>
          {graphDraft && extraction?.disposition !== "discarded" && !patchArtifactId && (
            <div className="gf-project-workspace__publish-action">
              <ul>
                <li>
                  <Check aria-hidden="true" size={15} /> 实体和关系可在图形界面检查
                </li>
                <li>
                  <Check aria-hidden="true" size={15} /> 后端会再次执行同一化与结构校验
                </li>
                <li>
                  <Check aria-hidden="true" size={15} /> 正式版本必须通过验证和审批
                </li>
              </ul>
              <button
                disabled={publishBusy || unresolvedConflictCount > 0}
                onClick={() => void createDraft()}
                type="button"
              >
                <GitPullRequestArrow aria-hidden="true" size={17} />
                {publishBusy ? "正在创建…" : "创建发布草案"}
              </button>
              {unresolvedConflictCount > 0 && <p>还需确认 {unresolvedConflictCount} 个冲突。</p>}
            </div>
          )}
          {patchArtifactId && !active && extraction?.disposition === "discarded" && (
            <article className="gf-project-workspace__publish-ready">
              <div>
                <span className="u-status">独立发布草案仍保留</span>
                <h3>放弃提案不会删除治理记录</h3>
                <p>这个发布草案尚未成为正式内容。如果它也不再采用，无需继续验证或审批；记录会保留供审计。</p>
              </div>
              <Link className="gf-secondary-button" to={`/patches/${encodeURIComponent(patchArtifactId)}`}>
                查看保留的发布草案
              </Link>
            </article>
          )}
          {patchArtifactId && !active && extraction?.disposition !== "discarded" && (
            <article className="gf-project-workspace__publish-ready">
              <div>
                <span className="u-status u-status--info">发布草案已创建</span>
                <h3>下一步：验证并审批</h3>
                <p>进入发布页运行检查、提交审批并应用。管理员也必须走完这些确定性门禁。</p>
              </div>
              <Link className="gf-primary-button" to={`/patches/${encodeURIComponent(patchArtifactId)}`}>
                验证并发布这个版本
                <ArrowRight aria-hidden="true" size={16} />
              </Link>
            </article>
          )}
          {active && (
            <StatePanel
              description={`项目当前内容已经是第 ${project.current_content_ref?.revision} 版。现在可以继续生成规则、内容或试玩。`}
              state="terminal"
              title="首个内容版本已发布"
            />
          )}
        </section>
      )}

      <section
        className="gf-project-workspace__section gf-project-workspace__next"
        aria-labelledby="next-title"
      >
        <header className="gf-project-workspace__section-header">
          <span className="gf-projects__step-number">→</span>
          <div>
            <p className="u-kicker">继续完善游戏</p>
            <h2 id="next-title">基于当前项目继续创作</h2>
            <p>
              {active
                ? "入口会带上这个项目的当前内容版本和材料来源。"
                : "发布首个内容版本后，这些入口会自动绑定准确版本。"}
            </p>
          </div>
        </header>
        <div className="gf-project-workspace__next-grid">
          <Link
            aria-disabled={!active}
            className={!active ? "is-disabled" : ""}
            to={active ? projectLink("/specs", project, materials) : "#publication-title"}
          >
            <FileText aria-hidden="true" size={22} />
            <strong>生成与维护规则</strong>
            <span>建立任务、经济、战斗等确定性约束</span>
          </Link>
          <Link
            aria-disabled={!active || !hasPublishedRules}
            className={!active || !hasPublishedRules ? "is-disabled" : ""}
            to={active && hasPublishedRules ? projectLink("/generation", project, materials) : "#next-title"}
          >
            <Sparkles aria-hidden="true" size={22} />
            <strong>继续生成内容</strong>
            <span>{hasPublishedRules ? "从当前版本新增角色、任务或系统内容" : "先发布项目规则后可用"}</span>
          </Link>
          <Link
            aria-disabled={!active || !hasPublishedRules}
            className={!active || !hasPublishedRules ? "is-disabled" : ""}
            to={active && hasPublishedRules ? projectLink("/playtest", project, materials) : "#next-title"}
          >
            <Gamepad2 aria-hidden="true" size={22} />
            <strong>进入自动试玩</strong>
            <span>
              {hasPublishedRules ? "用当前版本建立任务并回归验证" : "先发布项目规则并生成配置后可用"}
            </span>
          </Link>
        </div>
      </section>
    </div>
  );
}
