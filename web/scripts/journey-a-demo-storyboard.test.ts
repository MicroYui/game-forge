import { describe, expect, it } from "vitest";

import {
  DEMO_PROVENANCE_LABEL,
  DEMO_SCENES,
  DEMO_TARGET_DURATION_MS,
  validateDemoStoryboard,
} from "./journey-a-demo-storyboard";

describe("Journey A demo storyboard", () => {
  it("fits the agreed silent V1 window and leaves room for real page transitions", () => {
    const holdDuration = DEMO_SCENES.reduce((total, scene) => total + scene.holdMs, 0);

    expect(DEMO_TARGET_DURATION_MS).toBeGreaterThanOrEqual(75_000);
    expect(DEMO_TARGET_DURATION_MS).toBeLessThanOrEqual(90_000);
    expect(holdDuration).toBeLessThan(DEMO_TARGET_DURATION_MS);
    expect(DEMO_TARGET_DURATION_MS - holdDuration).toBeGreaterThanOrEqual(6_000);
  });

  it("uses unique Chinese chapters and an honest local replay provenance label", () => {
    expect(new Set(DEMO_SCENES.map((scene) => scene.key)).size).toBe(DEMO_SCENES.length);
    expect(DEMO_PROVENANCE_LABEL).toBe("本地 API · CASSETTE 回放 · 外网已阻断");
    for (const scene of DEMO_SCENES) {
      expect(`${scene.kicker} ${scene.body}`).toMatch(/[\u3400-\u9fff]/u);
    }
    expect(validateDemoStoryboard()).toEqual([]);
  });
});
