import { useQuery } from "@tanstack/react-query";
import { useEffect, useId } from "react";

import { ApiProblemError } from "../../api/problem";
import { StatePanel } from "../../components/ui";
import { modelsApi, type ModelsApi, type SelectableModel } from "./api";

/** What a run is bound to when the planner has made no choice yet. */
export function defaultModel(models: readonly SelectableModel[]): SelectableModel | null {
  return models.find((model) => model.is_default) ?? null;
}

function summary(model: SelectableModel): string {
  const context = `${Math.round(model.context_limit / 1000)}K 上下文`;
  return model.preview ? `${model.vendor} · ${context} · 预览版` : `${model.vendor} · ${context}`;
}

/**
 * Choose which model runs this task. The list is read from the gateway when the
 * card opens, so it shows what is actually being served rather than a list frozen
 * when the service started.
 *
 * Loading occupies the same space as the loaded control on purpose: this sits in
 * the middle of a form, and a control that grows when its list arrives moves
 * whatever the planner was about to click.
 */
export function ModelPicker({
  api = modelsApi,
  label = "用哪个模型",
  onChange,
  value,
}: {
  api?: ModelsApi;
  label?: string;
  onChange: (model: SelectableModel | null) => void;
  value: SelectableModel | null;
}) {
  const selectId = useId();
  const noteId = `${selectId}-note`;
  const query = useQuery({
    queryFn: () => api.listSelectableModels(),
    queryKey: ["selectable-models"],
    retry: false,
    staleTime: 0,
  });
  // What the card shows has to be what the run is started on. Only this component
  // knows the list, so it reports the opening selection instead of letting the
  // caller send "whatever the deployment does" and hope the two agree.
  const models = query.data;
  useEffect(() => {
    if (value === null && models !== undefined) {
      const opening = defaultModel(models);
      if (opening !== null) onChange(opening);
    }
  }, [models, onChange, value]);

  if (query.error) {
    // A deployment with no model gateway has no choice to offer. That is a fact
    // about the environment, not a failure to report where a control should be —
    // the run still starts on whatever this deployment routes to.
    if (query.error instanceof ApiProblemError && query.error.problem.status === 503) return null;
    return (
      <StatePanel
        action={
          <button className="gf-secondary-button" onClick={() => void query.refetch()} type="button">
            重新读取
          </button>
        }
        description="读不出这个环境现在能用哪些模型；稍后重试。"
        state="error"
        title="模型列表读取失败"
      />
    );
  }

  const selected = models === undefined ? null : (value ?? defaultModel(models));
  const note =
    models === undefined
      ? "正在读取这个环境现在能用的模型。"
      : models.length === 0
        ? "这个环境现在没有可用的模型。"
        : selected === null
          ? "默认模型当前不可用，请选择一个。"
          : summary(selected);

  return (
    <div className="gf-model-picker">
      <label htmlFor={selectId}>{label}</label>
      <select
        aria-describedby={noteId}
        disabled={models === undefined || models.length === 0}
        id={selectId}
        onChange={(event) => onChange(models?.find((item) => item.model === event.target.value) ?? null)}
        value={selected?.model ?? ""}
      >
        {selected === null ? (
          <option value="">{models === undefined ? "正在读取…" : "请选择一个模型"}</option>
        ) : null}
        {(models ?? []).map((model) => (
          <option key={model.model} value={model.model}>
            {model.display_name}
            {model.is_default ? "（默认）" : ""}
          </option>
        ))}
      </select>
      <small id={noteId}>{note}</small>
    </div>
  );
}
