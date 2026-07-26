import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { ApiProblemError, sanitizeProblem } from "../../api/problem";
import { createQueryClient } from "../../api/query-client";
import type { ModelsApi, SelectableModel } from "./api";
import { ModelPicker } from "./ModelPicker";

function model(overrides: Partial<SelectableModel> = {}): SelectableModel {
  return {
    context_limit: 1_050_000,
    display_name: "GPT-5.6 Sol",
    is_default: true,
    max_output_tokens: 128_000,
    model: "gpt-5.6-sol",
    model_catalog_digest: "a".repeat(64),
    model_catalog_version: 1,
    model_schema_version: "selectable-model@1",
    model_snapshot_id: "openai:sha256:" + "b".repeat(64),
    preview: false,
    routing_policy_digest: "c".repeat(64),
    routing_policy_version: 10_015,
    tier: "powerful",
    vendor: "OpenAI",
    ...overrides,
  };
}

const opus = model({
  context_limit: 1_000_000,
  display_name: "Claude Opus 5",
  is_default: false,
  model: "claude-opus-5",
  routing_policy_version: 10_003,
  vendor: "Anthropic",
});

function api(models: SelectableModel[]): ModelsApi {
  return { listSelectableModels: vi.fn().mockResolvedValue(models) };
}

function renderPicker(models: SelectableModel[], onChange = vi.fn()) {
  render(
    <QueryClientProvider client={createQueryClient()}>
      <ModelPicker api={api(models)} onChange={onChange} value={null} />
    </QueryClientProvider>,
  );
  return onChange;
}

describe("ModelPicker", () => {
  async function loadedSelect(): Promise<HTMLElement> {
    const select = screen.getByLabelText("用哪个模型");
    await waitFor(() => expect(select).toBeEnabled());
    return select;
  }

  it("opens on what a run starts on and says what it is", async () => {
    renderPicker([opus, model()]);

    expect(await loadedSelect()).toHaveValue("gpt-5.6-sol");
    expect(screen.getByText("OpenAI · 1050K 上下文")).toBeVisible();
    expect(screen.getByRole("option", { name: "GPT-5.6 Sol（默认）" })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "Claude Opus 5" })).toBeInTheDocument();
  });

  it("hands the caller the exact policy that routes to the chosen model", async () => {
    const onChange = renderPicker([opus, model()]);

    await userEvent.selectOptions(await loadedSelect(), "claude-opus-5");

    expect(onChange).toHaveBeenCalledWith(expect.objectContaining({ routing_policy_version: 10_003 }));
  });

  it("makes the planner choose when the default is not being served", async () => {
    renderPicker([opus]);

    expect(await loadedSelect()).toHaveValue("");
    expect(await screen.findByText("默认模型当前不可用，请选择一个。")).toBeVisible();
  });

  it("offers nothing where a deployment reaches no model gateway", async () => {
    const onChange = vi.fn();
    render(
      <QueryClientProvider client={createQueryClient()}>
        <ModelPicker
          api={{
            listSelectableModels: vi.fn().mockRejectedValue(
              new ApiProblemError(
                sanitizeProblem({
                  code: "dependency_unavailable",
                  detail: "A required dependency is unavailable.",
                  instance: "urn:gameforge:request:request:1",
                  request_id: "request:1",
                  status: 503,
                  title: "Dependency unavailable",
                  type: "urn:gameforge:problem:dependency_unavailable",
                }),
              ),
            ),
          }}
          onChange={onChange}
          value={null}
        />
      </QueryClientProvider>,
    );

    await waitFor(() => expect(screen.queryByLabelText("用哪个模型")).not.toBeInTheDocument());
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(onChange).not.toHaveBeenCalled();
  });

  it("holds its place while the list is still arriving", async () => {
    // It sits mid-form; a control that appears only once loaded moves whatever the
    // planner was about to click.
    let release: (value: unknown) => void = () => {};
    const pending = new Promise((resolve) => {
      release = resolve;
    });
    render(
      <QueryClientProvider client={createQueryClient()}>
        <ModelPicker
          api={{ listSelectableModels: vi.fn().mockReturnValue(pending.then(() => [model()])) }}
          onChange={vi.fn()}
          value={null}
        />
      </QueryClientProvider>,
    );

    const select = screen.getByLabelText("用哪个模型");
    expect(select).toBeDisabled();
    expect(screen.getByText("正在读取这个环境现在能用的模型。")).toBeVisible();

    release(null);

    await waitFor(() => expect(select).toBeEnabled());
    expect(select).toHaveValue("gpt-5.6-sol");
  });

  it("says a preview model is one", async () => {
    renderPicker([model({ preview: true })]);

    expect(await screen.findByText("OpenAI · 1050K 上下文 · 预览版")).toBeVisible();
  });
});
