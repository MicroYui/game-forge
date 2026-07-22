import { describe, expect, it } from "vitest";

import {
  DEMO_AUTHORING_GOAL,
  DEMO_PROVENANCE_LABEL,
  DEMO_README_FRAMES,
  DEMO_SCENES,
  DEMO_TARGET_DURATION_MS,
  validateDemoStoryboard,
} from "./journey-a-demo-storyboard";

describe("Journey A demo storyboard", () => {
  it("fits the agreed silent demo window and leaves room for real page transitions", () => {
    const holdDuration = DEMO_SCENES.reduce((total, scene) => total + scene.holdMs, 0);

    expect(DEMO_TARGET_DURATION_MS).toBeGreaterThanOrEqual(75_000);
    expect(DEMO_TARGET_DURATION_MS).toBeLessThanOrEqual(90_000);
    expect(holdDuration).toBeLessThan(DEMO_TARGET_DURATION_MS);
    expect(DEMO_TARGET_DURATION_MS - holdDuration).toBeGreaterThanOrEqual(6_000);
  });

  it("starts from the exact authoring input and tells one concrete cause-and-effect story", () => {
    expect(DEMO_AUTHORING_GOAL).toBe("Raise the caravan emblem requirement from three to four.");
    expect(DEMO_SCENES.map((scene) => scene.key)).toEqual([
      "intro",
      "input",
      "generation",
      "candidate-diff",
      "review",
      "failed-playtest",
      "failed-validation",
      "repair",
      "passed-playtest",
      "approval",
      "apply",
      "outro",
    ]);

    const story = DEMO_SCENES.map((scene) => `${scene.title} ${scene.body}`).join(" ");
    expect(story).toContain("3");
    expect(story).toContain("4");
    expect(story).toContain("只提供 3 枚");
    expect(story).toContain("恢复为 3");
    expect(story).toContain("没有进入 live ref");
  });

  it("uses unique Chinese chapters and an honest local replay provenance label", () => {
    expect(new Set(DEMO_SCENES.map((scene) => scene.key)).size).toBe(DEMO_SCENES.length);
    expect(DEMO_PROVENANCE_LABEL).toBe("本地 API · CASSETTE 回放 · 外网已阻断");
    for (const scene of DEMO_SCENES) {
      expect(`${scene.kicker} ${scene.body}`).toMatch(/[\u3400-\u9fff]/u);
    }
    expect(validateDemoStoryboard()).toEqual([]);
  });

  it("defines a compact, ordered beginner gallery from the same real workflow", () => {
    expect(DEMO_README_FRAMES).toHaveLength(10);
    expect(DEMO_README_FRAMES.map((frame) => frame.filename)).toEqual([
      "flow-01-input.png",
      "flow-02-candidate.png",
      "flow-03-diff.png",
      "flow-04-review.png",
      "flow-05-playtest-failure.png",
      "flow-06-release-blocked.png",
      "flow-07-repair.png",
      "flow-08-regression-passed.png",
      "flow-09-independent-approval.png",
      "flow-10-live-ref-history.png",
    ]);
    expect(new Set(DEMO_README_FRAMES.map((frame) => frame.sceneKey)).size).toBe(DEMO_README_FRAMES.length);
    expect(
      Object.fromEntries(DEMO_README_FRAMES.map((frame) => [frame.sceneKey, frame.capturePosition])),
    ).toMatchObject({
      approval: "primary",
      generation: "secondary",
      input: "primary",
      review: "secondary",
      "failed-playtest": "secondary",
      "passed-playtest": "secondary",
    });
    for (const frame of DEMO_README_FRAMES) {
      expect(DEMO_SCENES.some((scene) => scene.key === frame.sceneKey)).toBe(true);
    }
  });
});
