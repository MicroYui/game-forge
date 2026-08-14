import { describe, expect, it } from "vitest";

import {
  PROJECT_DEMO_PROVENANCE_LABEL,
  PROJECT_DEMO_README_FRAMES,
  PROJECT_DEMO_SCENES,
  PROJECT_DEMO_TARGET_DURATION_MS,
  validateProjectDemoStoryboard,
} from "./project-demo-storyboard";

describe("project-first README demo storyboard", () => {
  it("leaves enough time for the real browser interactions", () => {
    const holdDuration = PROJECT_DEMO_SCENES.reduce((total, scene) => total + scene.holdMs, 0);

    expect(PROJECT_DEMO_TARGET_DURATION_MS).toBeGreaterThanOrEqual(85_000);
    expect(PROJECT_DEMO_TARGET_DURATION_MS).toBeLessThanOrEqual(100_000);
    expect(holdDuration).toBeLessThan(PROJECT_DEMO_TARGET_DURATION_MS);
    expect(PROJECT_DEMO_TARGET_DURATION_MS - holdDuration).toBeGreaterThanOrEqual(25_000);
  });

  it("tells the current project-first cause-and-effect story", () => {
    expect(PROJECT_DEMO_SCENES.map((scene) => scene.key)).toEqual([
      "intro",
      "project",
      "material",
      "proposal",
      "graph-edit",
      "content-v1",
      "rules",
      "continuation",
      "content-v2",
      "playtest",
      "outro",
    ]);

    const story = PROJECT_DEMO_SCENES.map((scene) => `${scene.title} ${scene.body}`).join(" ");
    expect(story).toContain("air.quality");
    expect(story).toContain("air_quality");
    expect(story).toContain("第 1 版");
    expect(story).toContain("第 2 版");
    expect(story).toContain("仍有试玩任务未完成");
  });

  it("uses honest hermetic provenance and avoids release claims", () => {
    expect(PROJECT_DEMO_PROVENANCE_LABEL).toBe("本地 API / Worker · 固定模型替身 · 浏览器外网已阻断");
    expect(validateProjectDemoStoryboard()).toEqual([]);
    for (const scene of PROJECT_DEMO_SCENES) {
      expect(`${scene.kicker} ${scene.title} ${scene.body}`).toMatch(/[\u3400-\u9fff]/u);
    }
  });

  it("defines an ordered gallery captured from the same Playwright run", () => {
    expect(PROJECT_DEMO_README_FRAMES.map((frame) => frame.filename)).toEqual([
      "project-flow-01-project.png",
      "project-flow-02-material.png",
      "project-flow-03-proposal.png",
      "project-flow-04-graph-edit.png",
      "project-flow-05-content-v1.png",
      "project-flow-06-rules.png",
      "project-flow-07-continuation.png",
      "project-flow-08-content-v2.png",
      "project-flow-09-playtest.png",
    ]);
    expect(new Set(PROJECT_DEMO_README_FRAMES.map((frame) => frame.sceneKey)).size).toBe(
      PROJECT_DEMO_README_FRAMES.length,
    );
    for (const frame of PROJECT_DEMO_README_FRAMES) {
      expect(PROJECT_DEMO_SCENES.some((scene) => scene.key === frame.sceneKey)).toBe(true);
    }
  });
});
